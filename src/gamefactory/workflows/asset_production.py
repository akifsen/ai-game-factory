"""V0.4 static-prop orchestration built on the existing WorkflowEngine.

This module creates one engine DAG per immutable asset revision. It does not
implement a second lifecycle machine: approval, resume, task attempts, artifacts,
and final workflow completion remain owned by WorkflowEngine.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import sys
import time
from dataclasses import dataclass, field
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.glb_validator import preflight_glb
from gamefactory.adapters.dcc.blender_processor import BlenderAssetProcessor
from gamefactory.adapters.engines.godot_execution import _ENGINE_ERROR_PATTERNS
from gamefactory.adapters.engines.godot_staging import sha256_file
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.provider_character_publication import (
    ProviderCharacterEvidencePublicationRepository,
)
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    AuditLogRepository,
    ConceptVersionRecord,
    ConceptVersionRepository,
    CostLedgerRepository,
    EvidenceRepository,
    ExecutionRepository,
    PaidRequestSnapshotRecord,
    PaidRequestSnapshotRepository,
    ProductionReadinessRecord,
    ProductionReadinessRepository,
    ProviderInvocationRepository,
    ProviderOperationIntentRepository,
    QualityGateRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.asset_contracts import (
    AssetSpecification,
    AssetSpecificationV07,
    parse_asset_specification,
    parse_asset_specification_v07,
    spec_fingerprint,
)
from gamefactory.core.domain.asset_profiles import (
    AssetProfileV07,
    ProfileRegistry,
    builtin_v07_registry,
    parse_profile_document_v07,
)
from gamefactory.core.domain.errors import (
    ApprovalRequired,
    ArtifactError,
    AssetValidationFailedError,
    DccFailedError,
    EngineImportFailedError,
    PaidRequestIncompatibleError,
    PaidRequestInvalidError,
    ProductionReadinessFailedError,
    ProviderUncertainError,
    RawArtifactInvalidError,
    RuntimeValidationFailedError,
    ToolUnavailableError,
    ValidationError,
)
from gamefactory.core.domain.models import (
    Artifact,
    AuditEvent,
    CostClass,
    Execution,
    ExecutionStatus,
    Task,
    TaskStatus,
    Workflow,
    WorkflowStatus,
    generate_id,
    utc_now_iso,
)
from gamefactory.core.domain.paid_request import (
    PAID_REQUEST_SCHEMA,
    PaidRequestSnapshot,
)
from gamefactory.core.execution.path_guard import PathGuard, assert_managed_directory
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner
from gamefactory.workflows.accounting import CostAccounting
from gamefactory.workflows.handlers import (
    HandlerOperation,
    HandlerRecovery,
    TaskHandlerMetadata,
    TaskHandlerRegistry,
    TaskHandlerResult,
)
from gamefactory.workflows.ports import AssetGenerationProvider, GenerationRequest
from gamefactory.workflows.production_readiness import (
    READINESS_SCHEMA,
    ReadinessCheck,
    ReadinessContext,
    ReadinessProbes,
    build_readiness_report,
)


def is_v06_graph(task: Task | Any) -> bool:
    """Detect whether a task belongs to a V0.6 workflow DAG."""
    if hasattr(task, "parameters") and isinstance(task.parameters, dict):
        return task.parameters.get("graph_version") == "0.6.0"
    if isinstance(task, dict):
        params = task.get("parameters", {})
        if isinstance(params, dict):
            return params.get("graph_version") == "0.6.0"
    return False


def is_v07_character_graph(task: Task | Any) -> bool:
    """Recognize the exact provider-generated static-character graph version.

    This is a routing predicate only. Paid dispatch still revalidates the typed
    spec, trusted profile registration, revision, and every persisted task
    binding before it reads approvals or creates paid state.
    """
    params = getattr(task, "parameters", None)
    if not isinstance(params, dict) and isinstance(task, dict):
        params = task.get("parameters")
    if not isinstance(params, dict) or params.get("graph_version") != "0.7.0":
        return False
    specification = params.get("specification")
    collider = specification.get("collider") if isinstance(specification, dict) else None
    return (
        isinstance(specification, dict)
        and specification.get("schema_version") == "0.7.0"
        and specification.get("source_kind") == "provider_generated"
        and specification.get("category") == "character"
        and specification.get("parts") is None
        and specification.get("sockets") is None
        and params.get("geometry_mode") == "single_mesh"
        and params.get("profile_version") == 1
        and params.get("profile_schema") == "asset-profile-0.7.0"
        and params.get("profile_source_kinds") == ["provider_generated"]
        and params.get("profile_assembly") is None
        and isinstance(collider, dict)
        and collider.get("policy") == "capsule"
    )


def is_immutable_paid_graph(task: Task | Any) -> bool:
    """Graphs that receive the V0.6/V0.7 immutable concept and snapshot rules."""
    return is_v06_graph(task) or is_v07_character_graph(task)


def _require_character_contract(
    specification: AssetSpecificationV07,
    profile: AssetProfileV07,
) -> None:
    """Keep the paid V0.7 bridge limited to the implemented static capsule character."""
    processing = getattr(getattr(profile, "document", None), "processing", None)
    godot = getattr(getattr(profile, "document", None), "godot", None)
    if (
        not isinstance(specification, AssetSpecificationV07)
        or not isinstance(profile, AssetProfileV07)
        or specification.source_kind != "provider_generated"
        or specification.category != "character"
        or specification.parts is not None
        or specification.sockets is not None
        or profile.geometry_mode != "single_mesh"
        or profile.accepted_source_kinds != ("provider_generated",)
        or profile.version != 1
        or profile.assembly is not None
        or "character" not in profile.document.categories
        or specification.collider.policy != "capsule"
        or specification.collider.capsule is None
        or specification.orientation.up != "+Y"
        or specification.orientation.front != "-Z"
        or processing is None
        or processing.rig_forbidden is not True
        or processing.animation_forbidden is not True
        or godot is None
        or godot.body_kind != "static_body"
        or godot.require_ray_hit is not True
        or godot.require_area is not False
        or len(profile.review_views) not in {5, 9}
        or not {
            "front",
            "rear",
            "left",
            "right",
            "three_quarter",
        }.issubset(set(profile.review_views))
        or not (
            profile.document.processing.lod1_required or specification.lod_policy == "lod0_lod1"
        )
    ):
        raise ValidationError(
            "Paid V0.7 processing supports only provider-generated, unrigged, "
            "single-mesh characters with no parts or sockets and a runtime capsule"
        )


def _character_runtime_requirements(profile: AssetProfileV07) -> dict[str, Any]:
    return {
        "require_mesh_visible": True,
        "require_collision": True,
        "require_physics_body": profile.document.godot.body_kind == "static_body",
        "require_area": profile.document.godot.require_area,
        "require_ray_hit": profile.document.godot.require_ray_hit,
        "bounds_tolerance_ratio": profile.document.runtime.bounds_tolerance_ratio,
        "bounds_tolerance_floor_m": profile.document.runtime.bounds_tolerance_floor_m,
        "dimension_tolerance_m": profile.document.processing.dimension_tolerance_m,
    }


def _parse_task_specification(
    params: dict[str, Any], profile_registry: ProfileRegistry
) -> AssetSpecification | AssetSpecificationV07:
    data = params.get("specification")
    if isinstance(data, dict) and data.get("schema_version") == "0.7.0":
        specification = parse_asset_specification_v07(data, registry=profile_registry)
        profile = specification.bound_profile()
        _require_character_contract(specification, profile)
        expected_profile = profile.document.model_dump(mode="json")
        expected_profile_hash = _canonical_hash(expected_profile)
        pinned_profile = params.get("profile_document")
        try:
            pinned_profile_hash = _canonical_hash(pinned_profile)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Persisted V0.7 character profile document is malformed") from exc
        if (
            params.get("graph_version") != "0.7.0"
            or not isinstance(pinned_profile, dict)
            or pinned_profile_hash != expected_profile_hash
            or params.get("profile_document_hash") != expected_profile_hash
            or params.get("profile_id") != profile.profile_id
            or type(params.get("profile_version")) is not int
            or params.get("profile_version") != profile.version
            or params.get("profile_qualified") != profile.qualified
            or params.get("profile_schema") != profile.schema_version
            or params.get("geometry_mode") != "single_mesh"
            or params.get("profile_source_kinds") != ["provider_generated"]
            or params.get("profile_assembly") is not None
            or params.get("source_kind") != "provider_generated"
            or params.get("category") != "character"
            or params.get("collider_policy") != "capsule"
            or params.get("specification_hash") != spec_fingerprint(specification)
        ):
            raise ValidationError("Persisted V0.7 character task profile/spec binding changed")
        return specification
    if not isinstance(data, (str, dict, Path)):
        raise ValidationError("Persisted asset task has no valid specification document")
    return parse_asset_specification(data)


def _embedded_profile_registry(params: dict[str, Any]) -> ProfileRegistry:
    """Decode a pinned profile for display helpers; never use as paid trust evidence."""
    document = params.get("profile_document")
    if not isinstance(document, dict) or _canonical_hash(document) != params.get(
        "profile_document_hash"
    ):
        raise ValidationError("Persisted V0.7 character profile document is missing or changed")
    profile = AssetProfileV07(parse_profile_document_v07(document))
    if profile.qualified != f"{params.get('profile_id')}@{params.get('profile_version')}":
        raise ValidationError("Persisted V0.7 character profile identity changed")
    return ProfileRegistry(
        available=(),
        unsupported=(),
        available_v07=(profile,),
    )


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    try:
        return str(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError("V0.7 character payload is not canonical JSON") from exc


def _portable_v07_readiness_report(report: dict[str, Any]) -> dict[str, Any]:
    """Replace local executable paths with stable identities in V0.7 evidence."""

    absolute_path = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|//|/)")

    def sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: sanitize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        if isinstance(value, str) and absolute_path.match(value):
            return Path(value).name
        return value

    projected = sanitize(report)
    if not isinstance(projected, dict):
        raise ValidationError("V0.7 readiness report is not a JSON object")
    identities = projected.get("tool_identities")
    checks = projected.get("checks")
    if not isinstance(identities, dict) or not isinstance(checks, list):
        raise ValidationError("V0.7 readiness report has malformed tool identity data")
    source_identities = report.get("tool_identities", {})
    for tool in ("blender", "godot"):
        configured = source_identities.get(tool) if isinstance(source_identities, dict) else None
        if not isinstance(configured, str):
            continue
        executable_check = next(
            (
                item
                for item in checks
                if isinstance(item, dict) and item.get("name") == f"{tool}_executable"
            ),
            {},
        )
        launch_check = next(
            (
                item
                for item in checks
                if isinstance(item, dict) and item.get("name") == f"{tool}_launch_version"
            ),
            {},
        )
        executable_observed = executable_check.get("observed", {})
        launch_observed = launch_check.get("observed", {})
        identity: dict[str, Any] = {"basename": Path(configured).name}
        if (
            isinstance(executable_observed, dict)
            and type(executable_observed.get("size_bytes")) is int
        ):
            identity["size_bytes"] = executable_observed["size_bytes"]
        if isinstance(launch_observed, dict) and isinstance(launch_observed.get("version"), str):
            identity["version"] = launch_observed["version"]
        if isinstance(executable_observed, dict) and isinstance(
            executable_observed.get("sha256"), str
        ):
            identity["sha256"] = executable_observed["sha256"]
        identities[tool] = identity
    return projected


def _read_bounded_json_document(path: Path, label: str, *, max_bytes: int = 1_000_000) -> Any:
    """Read small persisted JSON inputs without duplicate keys or non-finite values."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size > max_bytes:
            raise ArtifactError(f"{label} is not a bounded regular JSON file")
        raw = path.read_bytes()
        if len(raw) > max_bytes:
            raise ArtifactError(f"{label} exceeds its JSON size limit")

        def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON object key")
                result[key] = value
            return result

        def parse_integer(value: str) -> int:
            if len(value.lstrip("-")) > 64:
                raise ValueError("oversized JSON integer")
            return int(value)

        def reject_constant(_value: str) -> Any:
            raise ValueError("non-finite JSON number")

        return json.loads(
            raw,
            object_pairs_hook=object_pairs,
            parse_int=parse_integer,
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, ArtifactError):
            raise
        raise ArtifactError(f"{label} is malformed or unreadable") from exc


def _inspect_spec_or_params(target: Any) -> dict[str, Any]:
    """Collect paid-pipeline disqualifiers from every serialized spec layer.

    Task parameters are persisted input and may contain both task-level aliases
    and a nested ``specification``.  Inspect all of them: selecting only the
    first truthy alias lets a conflicting outer value hide an assembly spec.
    """
    result: dict[str, Any] = {
        "schema_versions": set(),
        "source_kinds": set(),
        "profile_ids": set(),
        "profile_versions": set(),
        "profile_qualified": set(),
        "profile_schemas": set(),
        "geometry_modes": set(),
        "categories": set(),
        "collider_policies": set(),
        "has_parts": False,
        "has_sockets": False,
        "has_assembly_contract": False,
        "malformed_specification": False,
    }
    # The routine accepts decoded JSON and typed specs, but it can also be
    # called with direct Python values in tests or internal APIs. Bound the
    # walk and reject aliases/cycles so malformed input cannot hang a paid
    # dispatch check.
    candidates: list[tuple[Any, int]] = [(target, 0)]
    visited: set[int] = set()
    traversed = 0
    while candidates:
        candidate, depth = candidates.pop()
        traversed += 1
        if traversed > 32 or depth > 8:
            result["malformed_specification"] = True
            break
        if candidate is None:
            continue
        if isinstance(candidate, str):
            if len(candidate) > 1_048_576:
                result["malformed_specification"] = True
                continue
            try:
                parsed = json.loads(candidate)
            except (json.JSONDecodeError, TypeError, RecursionError):
                result["malformed_specification"] = True
                continue
            if isinstance(parsed, dict):
                candidates.append((parsed, depth + 1))
            else:
                result["malformed_specification"] = True
            continue
        if isinstance(candidate, dict):
            if id(candidate) in visited:
                result["malformed_specification"] = True
                continue
            visited.add(id(candidate))
            for key in (
                "schema_version",
                "source_kind",
                "profile",
                "profile_id",
                "profile_version",
                "profile_qualified",
                "profile_schema",
                "geometry_mode",
                "profile_geometry_mode",
                "category",
                "collider_policy",
            ):
                value = candidate.get(key)
                if value is not None:
                    destination = {
                        "schema_version": "schema_versions",
                        "source_kind": "source_kinds",
                        "profile": "profile_ids",
                        "profile_id": "profile_ids",
                        "profile_version": "profile_versions",
                        "profile_qualified": "profile_qualified",
                        "profile_schema": "profile_schemas",
                        "geometry_mode": "geometry_modes",
                        "profile_geometry_mode": "geometry_modes",
                        "category": "categories",
                        "collider_policy": "collider_policies",
                    }[key]
                    if key == "profile_version":
                        if isinstance(value, bool) or not isinstance(value, int):
                            result["malformed_specification"] = True
                        else:
                            result[destination].add(str(value))
                    elif key == "profile_qualified":
                        if not isinstance(value, str) or "@" not in value:
                            result["malformed_specification"] = True
                        else:
                            qualified_id, separator, qualified_version = value.rpartition("@")
                            if not separator or not qualified_id or not qualified_version.isdigit():
                                result["malformed_specification"] = True
                            else:
                                # This is the actual legacy alias emitted by
                                # Profile.qualified (profile_id@integer-version).
                                result["profile_ids"].add(qualified_id)
                                result["profile_versions"].add(qualified_version)
                    else:
                        if not isinstance(value, str):
                            result["malformed_specification"] = True
                        else:
                            result[destination].add(value)
            # A present empty payload is still suspicious persisted assembly
            # metadata; ``parts: null`` and ``sockets: null`` are normal model
            # defaults for provider-generated V0.7 specs.
            result["has_parts"] |= "parts" in candidate and candidate["parts"] is not None
            result["has_sockets"] |= "sockets" in candidate and candidate["sockets"] is not None
            result["has_assembly_contract"] |= (
                "assembly" in candidate and candidate["assembly"] is not None
            )
            category = candidate.get("category")
            if category is not None:
                if isinstance(category, str):
                    result["categories"].add(category)
                else:
                    result["malformed_specification"] = True
            collider = candidate.get("collider")
            if collider is not None:
                policy = collider.get("policy") if isinstance(collider, dict) else None
                if isinstance(policy, str):
                    result["collider_policies"].add(policy)
                else:
                    result["malformed_specification"] = True
            if "specification" in candidate:
                nested_spec = candidate["specification"]
                if nested_spec is None:
                    result["malformed_specification"] = True
                elif not isinstance(nested_spec, (dict, str)):
                    nested_dump = getattr(nested_spec, "model_dump", None)
                    if not callable(nested_dump):
                        result["malformed_specification"] = True
                candidates.append((nested_spec, depth + 1))
            continue

        if not hasattr(candidate, "model_dump") and not any(
            hasattr(candidate, name)
            for name in ("schema_version", "source_kind", "profile", "bound_profile")
        ):
            result["malformed_specification"] = True
            continue
        if id(candidate) in visited:
            result["malformed_specification"] = True
            continue
        visited.add(id(candidate))
        dump = getattr(candidate, "model_dump", None)
        if callable(dump):
            try:
                dumped = dump(mode="json")
                if not isinstance(dumped, dict):
                    result["malformed_specification"] = True
                else:
                    candidates.append((dumped, depth + 1))
            except Exception:
                result["malformed_specification"] = True
        else:
            for key in (
                "schema_version",
                "source_kind",
                "profile",
                "profile_version",
                "parts",
                "sockets",
                "assembly",
            ):
                value = getattr(candidate, key, None)
                if key in {"parts", "sockets", "assembly"}:
                    result[
                        {
                            "parts": "has_parts",
                            "sockets": "has_sockets",
                            "assembly": "has_assembly_contract",
                        }[key]
                    ] |= value is not None
                elif key == "profile_version":
                    if value is not None:
                        if isinstance(value, bool) or not isinstance(value, int):
                            result["malformed_specification"] = True
                        else:
                            result["profile_versions"].add(str(value))
                elif value is not None:
                    result[
                        {
                            "schema_version": "schema_versions",
                            "source_kind": "source_kinds",
                            "profile": "profile_ids",
                        }[key]
                    ].add(str(value))
            bound_profile = getattr(candidate, "bound_profile", None)
            if callable(bound_profile):
                try:
                    profile = bound_profile()
                except Exception:
                    profile = None
                if profile is not None:
                    for attr, bucket in (
                        ("profile_id", "profile_ids"),
                        ("schema_version", "profile_schemas"),
                        ("geometry_mode", "geometry_modes"),
                    ):
                        value = getattr(profile, attr, None)
                        if value is not None:
                            result[bucket].add(str(value))
                    version = getattr(profile, "version", None)
                    if version is not None:
                        result["profile_versions"].add(str(version))
                    result["has_assembly_contract"] |= (
                        getattr(profile, "assembly", None) is not None
                    )

    # Bound profile objects may be accessible even when model serialization is
    # not.  For Pydantic specs, inspect that binding without trusting it as the
    # sole source of the serialized V0.7 marker.
    if target is not None:
        bound_profile = getattr(target, "bound_profile", None)
        if callable(bound_profile):
            try:
                profile = bound_profile()
            except Exception:
                profile = None
            if profile is not None:
                for attr, bucket in (
                    ("profile_id", "profile_ids"),
                    ("schema_version", "profile_schemas"),
                    ("geometry_mode", "geometry_modes"),
                ):
                    value = getattr(profile, attr, None)
                    if value is not None:
                        result[bucket].add(str(value))
            version = getattr(profile, "version", None)
            if version is not None:
                result["profile_versions"].add(str(version))
            result["has_assembly_contract"] |= getattr(profile, "assembly", None) is not None

    result["is_assembly"] = (
        "local_operator_assembly" in result["source_kinds"]
        or result["has_parts"]
        or result["has_sockets"]
        or "assembly" in result["geometry_modes"]
        or result["has_assembly_contract"]
    )
    result["is_v07"] = (
        "0.7.0" in result["schema_versions"]
        or "provider_generated" in result["source_kinds"]
        or "asset-profile-0.7.0" in result["profile_schemas"]
    )
    result["is_v07_character"] = (
        result["schema_versions"] == {"0.7.0"}
        and result["source_kinds"] == {"provider_generated"}
        and result["categories"] == {"character"}
        and result["profile_schemas"] == {"asset-profile-0.7.0"}
        and result["geometry_modes"] == {"single_mesh"}
        and result["collider_policies"] == {"capsule"}
        and not result["has_parts"]
        and not result["has_sockets"]
        and not result["has_assembly_contract"]
    )
    return result


def _guard_paid_assembly_defense(
    stage: str,
    *,
    spec: Any = None,
    task: Task | None = None,
    workflow: Workflow | None = None,
    task_repo: TaskRepository | None = None,
) -> None:
    """Entry guard enforcing bounded V0.7 assembly defense on paid workflows."""
    if spec is not None:
        info = _inspect_spec_or_params(spec)
        _validate_paid_spec_metadata(stage, info)
        return

    if task is not None:
        task_info = _inspect_spec_or_params(task.parameters)
        _validate_paid_spec_metadata(stage, task_info)
        if task_info["is_v07"] and not is_v07_character_graph(task):
            raise ValidationError(
                f"Persisted V0.7 paid task is not bound to the canonical character graph at stage '{stage}'",
                details={"stage": stage, "task_id": task.id},
            )

        if workflow is not None and task_repo is not None:
            try:
                tasks = task_repo.list_by_workflow(workflow.id)
            except Exception as exc:
                raise ValidationError(
                    f"Unable to verify the persisted prepare specification before paid stage '{stage}'",
                    details={"stage": stage, "reason": type(exc).__name__},
                ) from exc
            prepare_tasks = [t for t in tasks if getattr(t, "task_type", None) == "asset_prepare"]
            if len(prepare_tasks) != 1:
                raise ValidationError(
                    f"Workflow must have exactly one asset_prepare task before paid stage '{stage}'",
                    details={"stage": stage, "prepare_task_count": len(prepare_tasks)},
                )
            prepare_task = prepare_tasks[0] if prepare_tasks else None
            if prepare_task is not None and prepare_task.id != task.id:
                prep_info = _inspect_spec_or_params(prepare_task.parameters)
                _validate_paid_spec_metadata(stage, prep_info, source="asset_prepare")
                binding_fields = (
                    "schema_versions",
                    "source_kinds",
                    "profile_ids",
                    "profile_versions",
                    "profile_schemas",
                    "geometry_modes",
                    "categories",
                    "collider_policies",
                )
                contradictions = (
                    any(task_info[field] != prep_info[field] for field in binding_fields)
                    or (
                        task_info["has_parts"] != prep_info["has_parts"]
                        and (task_info["has_parts"] or prep_info["has_parts"])
                    )
                    or (
                        task_info["has_sockets"] != prep_info["has_sockets"]
                        and (task_info["has_sockets"] or prep_info["has_sockets"])
                    )
                )
                if contradictions:
                    raise ValidationError(
                        f"Persisted task parameters conflict with asset_prepare specification at stage '{stage}'",
                        details={
                            "stage": stage,
                            "task_id": task.id,
                            "prepare_task_id": prepare_task.id,
                        },
                    )
        if task_info["malformed_specification"]:
            raise ValidationError(
                f"Persisted specification cannot be inspected safely before paid stage '{stage}'",
                details={"stage": stage, "task_id": task.id},
            )


def _validate_paid_spec_metadata(
    stage: str, info: dict[str, Any], *, source: str = "specification"
) -> None:
    """Reject malformed or internally contradictory persisted spec layers."""
    if info["malformed_specification"]:
        subject = (
            "Specification" if source == "specification" else f"Persisted {source} specification"
        )
        raise ValidationError(
            f"{subject} cannot be inspected safely before paid stage '{stage}'",
            details={"stage": stage, "source": source},
        )
    _raise_if_paid_assembly_or_v07(stage, info)
    for field_name in (
        "schema_versions",
        "source_kinds",
        "profile_ids",
        "profile_versions",
        "profile_schemas",
        "geometry_modes",
    ):
        if len(info[field_name]) > 1:
            raise ValidationError(
                f"Persisted {source} has conflicting {field_name.replace('_', ' ')} at paid stage '{stage}'",
                details={"stage": stage, "source": source, "field": field_name},
            )


def _raise_if_paid_assembly_or_v07(stage: str, info: dict[str, Any]) -> None:
    """Raise a controlled validation error for unsupported paid bindings."""
    source = next(iter(sorted(info["source_kinds"])), "unknown")
    profile = next(iter(sorted(info["profile_ids"])), "unknown")
    if info["is_assembly"]:
        raise ValidationError(
            f"Assembly specification with source '{source}' and profile '{profile}' is forbidden from paid pipeline at stage '{stage}'",
            details={"stage": stage, "source": source, "profile": profile},
        )
    if info["is_v07"] and not info.get("is_v07_character", False):
        source = source if source != "unknown" else "provider_generated"
        raise ValidationError(
            f"V0.7 provider-generated paid binding is unsupported for profile '{profile}'; only the exact unrigged character@1 flow is enabled at stage '{stage}'",
            details={"stage": stage, "source": source, "profile": profile},
        )


def create_asset_production_workflow(
    project_id: str,
    project_root: Path | str,
    specification: AssetSpecification | AssetSpecificationV07,
    concept_image: Path | str,
    concept_provenance: Path | str,
    *,
    provider_name: str,
    provider_estimate: float | None = None,
    budget_reservation: float | None = None,
    provider_cost_unit: str = "credits",
    concept_source_type: str = "imported",
    workflow_id: str | None = None,
    revision_repository: AssetRevisionRepository | None = None,
    profile_registry: ProfileRegistry | None = None,
) -> tuple[Workflow, list[Task]]:
    """Create an asset DAG after validating inputs; no provider is contacted here."""
    _guard_paid_assembly_defense(
        stage="create_asset_production_workflow",
        spec=specification,
    )
    project_path = Path(project_root).resolve(strict=True)
    concept = Path(concept_image).resolve(strict=True)
    provenance = Path(concept_provenance).resolve(strict=True)
    if concept.suffix.lower() != ".png" or not concept.is_file() or not provenance.is_file():
        raise ValidationError("Concept must be a PNG and provenance must be a regular file")
    if provider_name not in {"fake", "meshy"}:
        raise ValidationError("Asset provider must be 'fake' or 'meshy'")
    try:
        concept_relative = concept.relative_to(project_path).as_posix()
        provenance_relative = provenance.relative_to(project_path).as_posix()
    except ValueError as exc:
        raise ValidationError(
            "Concept and provenance inputs must be within the project root"
        ) from exc
    reservation = provider_estimate if provider_estimate is not None else budget_reservation
    if reservation is None:
        reservation = 0.0  # No reservation; paid handler blocks before provider dispatch.
    if isinstance(reservation, bool) or not math.isfinite(float(reservation)) or reservation < 0:
        raise ValidationError("Provider estimate/reservation must be non-negative")
    if provider_estimate is not None and (
        isinstance(provider_estimate, bool)
        or not math.isfinite(float(provider_estimate))
        or provider_estimate < 0
    ):
        raise ValidationError("Provider estimate must be finite and non-negative")
    if budget_reservation is not None and (
        isinstance(budget_reservation, bool)
        or not math.isfinite(float(budget_reservation))
        or budget_reservation < 0
    ):
        raise ValidationError("Budget reservation must be non-negative")
    wf_id = workflow_id or generate_id("WF-ASSET")
    spec_hash = spec_fingerprint(specification)
    concept_hash = sha256_file(concept)
    profile = specification.bound_profile()
    graph_version = "0.6.0"
    v07_profile_fields: dict[str, Any] = {}
    if isinstance(specification, AssetSpecificationV07):
        trusted_registry = profile_registry or builtin_v07_registry()
        try:
            registered_profile = trusted_registry.get_v07(profile.profile_id, profile.version)
        except Exception as exc:
            raise ValidationError(
                "V0.7 character profiles must be explicitly registered; arbitrary profile documents cannot enter paid workflows"
            ) from exc
        if registered_profile != profile:
            raise ValidationError(
                "V0.7 specification is not bound to the trusted profile registration"
            )
        _require_character_contract(specification, profile)
        graph_version = "0.7.0"
        profile_document = profile.document.model_dump(mode="json")
        v07_profile_fields = {
            "profile_document": profile_document,
            "profile_document_hash": _canonical_hash(profile_document),
            "geometry_mode": profile.geometry_mode,
            "profile_source_kinds": list(profile.accepted_source_kinds),
            "profile_assembly": None,
            "source_kind": specification.source_kind,
            "category": specification.category,
            "collider_policy": specification.collider.policy,
        }
    if revision_repository is None:
        raise ValidationError("Asset workflow requires the durable AssetRevisionRepository")
    if graph_version == "0.7.0":
        from gamefactory.adapters.persistence.character_graph import CharacterGraphRepository

        # V0.7 allocates the revision and commits the complete provider DAG in
        # one transaction. The factory below only constructs immutable records.
        base_workflow = Workflow(
            id=wf_id,
            project_id=project_id,
            name=f"Asset production: {specification.asset_id}",
            status=WorkflowStatus.PENDING,
        )
        concept_provenance_hash = sha256_file(provenance)

        def character_tasks(assigned: Any) -> list[Task]:
            number = assigned.revision_number
            asset_dir = f".gamefactory/assets/{specification.asset_id}/r{number:03d}"
            common = {
                "graph_version": graph_version,
                "workflow_id": wf_id,
                "asset_id": specification.asset_id,
                "revision_number": number,
                "specification": specification.model_dump(mode="json"),
                "specification_hash": spec_hash,
                "concept_source": concept_relative,
                "concept_provenance_source": provenance_relative,
                "concept_source_hash": concept_hash,
                "concept_provenance_hash": concept_provenance_hash,
                "concept_source_type": concept_source_type,
                "provider": provider_name,
                "provider_estimate": provider_estimate,
                "budget_reservation": budget_reservation,
                "paid_reservation": float(reservation),
                "cost_unit": provider_cost_unit,
                "asset_dir": asset_dir,
                "profile_id": profile.profile_id,
                "profile_version": profile.version,
                "profile_qualified": profile.qualified,
                "profile_schema": profile.schema_version,
                **v07_profile_fields,
            }
            (
                prepare,
                concept,
                snapshot,
                readiness,
                paid,
                process,
                validate,
                godot,
                final,
                evidence,
            ) = (
                f"{wf_id}-{suffix}"
                for suffix in (
                    "PREPARE",
                    "CONCEPT-REVIEW",
                    "PAID-REQUEST",
                    "READINESS",
                    "PAID-GENERATION",
                    "PROCESS",
                    "VALIDATE",
                    "GODOT",
                    "FINAL-REVIEW",
                    "PROVIDER-EVIDENCE",
                )
            )
            return [
                Task(prepare, wf_id, "V0.7 character prepare", "asset_prepare", parameters=common),
                Task(
                    concept,
                    wf_id,
                    "V0.7 character concept review",
                    "asset_concept_review",
                    depends_on=[prepare],
                    parameters=common,
                ),
                Task(
                    snapshot,
                    wf_id,
                    "V0.7 character paid request",
                    "asset_paid_request_snapshot",
                    depends_on=[concept],
                    max_retries=10,
                    parameters=common,
                ),
                Task(
                    readiness,
                    wf_id,
                    "V0.7 character readiness",
                    "asset_production_readiness",
                    depends_on=[snapshot],
                    max_retries=10,
                    parameters=common,
                ),
                Task(
                    paid,
                    wf_id,
                    "V0.7 character paid generation",
                    "asset_paid_generation",
                    cost_class=CostClass.PAID,
                    depends_on=[readiness],
                    parameters={
                        **common,
                        "cost": float(reservation),
                        "cost_unit": provider_cost_unit,
                        "estimate_label": "UNKNOWN" if provider_estimate is None else "KNOWN",
                        "mandatory_approval_type": "paid_generation",
                    },
                ),
                Task(
                    process,
                    wf_id,
                    "V0.7 character process",
                    "asset_process",
                    depends_on=[paid],
                    parameters=common,
                ),
                Task(
                    validate,
                    wf_id,
                    "V0.7 character validate",
                    "asset_validate",
                    depends_on=[process],
                    parameters=common,
                ),
                Task(
                    godot,
                    wf_id,
                    "V0.7 character godot",
                    "asset_godot",
                    depends_on=[validate],
                    parameters=common,
                    timeout_seconds=180.0,
                ),
                Task(
                    final,
                    wf_id,
                    "V0.7 character final review",
                    "asset_final_review",
                    depends_on=[godot],
                    parameters=common,
                ),
                Task(
                    evidence,
                    wf_id,
                    "V0.7 character provider evidence",
                    "asset_v07_provider_evidence",
                    depends_on=[final],
                    parameters=common,
                ),
            ]

        _revision, tasks, _created = CharacterGraphRepository(revision_repository.db).create_graph(
            workflow=base_workflow,
            asset_id=specification.asset_id,
            spec_hash=spec_hash,
            profile_id=profile.profile_id,
            profile_version=profile.version,
            concept_hash=concept_hash,
            task_factory=character_tasks,
        )
        return base_workflow, tasks

    # The revision table references workflows. Persist the workflow identity before
    # atomically allocating its revision; register_workflow later adds the task DAG.
    base_workflow = Workflow(
        id=wf_id,
        project_id=project_id,
        name=f"Asset production: {specification.asset_id}",
        status=WorkflowStatus.PENDING,
    )
    WorkflowRepository(revision_repository.db).save(base_workflow)
    revision = revision_repository.allocate_revision(
        specification.asset_id,
        wf_id,
        spec_hash,
        concept_hash=concept_hash,
        profile_id=profile.profile_id,
        profile_version=profile.version,
    )
    revision_number = revision.revision_number
    asset_dir = f".gamefactory/assets/{specification.asset_id}/r{revision_number:03d}"
    workflow = Workflow(
        id=wf_id,
        project_id=project_id,
        name=f"Asset production: {specification.asset_id} r{revision_number:03d}",
        status=WorkflowStatus.PENDING,
    )
    common = {
        "graph_version": graph_version,
        "asset_id": specification.asset_id,
        "revision_number": revision_number,
        "specification": specification.model_dump(mode="json"),
        "specification_hash": spec_hash,
        "concept_source": concept_relative,
        "concept_provenance_source": provenance_relative,
        "concept_source_hash": concept_hash,
        "concept_provenance_hash": sha256_file(provenance),
        "concept_source_type": concept_source_type,
        "provider": provider_name,
        "provider_estimate": provider_estimate,
        "budget_reservation": budget_reservation,
        # Informational only: the paid task alone carries "cost", the key the engine
        # reserves budget for. Shared parameters must never reserve money.
        "paid_reservation": float(reservation),
        "cost_unit": provider_cost_unit,
        "asset_dir": asset_dir,
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "profile_qualified": profile.qualified,
        "profile_schema": profile.schema_version,
        **v07_profile_fields,
    }
    (
        prep,
        concept_review,
        paid_request,
        readiness,
        generate,
        process,
        validate,
        runtime,
        final,
        finish,
    ) = (
        f"{wf_id}-{suffix}"
        for suffix in (
            "PREPARE",
            "CONCEPT-REVIEW",
            "PAID-REQUEST",
            "READINESS",
            "PAID-GENERATION",
            "PROCESS",
            "VALIDATE",
            "GODOT",
            "FINAL-REVIEW",
            "EVIDENCE",
        )
    )
    tasks = [
        Task(
            id=prep,
            workflow_id=wf_id,
            name="Validate and retain specification and concept",
            task_type="asset_prepare",
            parameters=common,
        ),
        Task(
            id=concept_review,
            workflow_id=wf_id,
            name="Human concept approval",
            task_type="asset_concept_review",
            depends_on=[prep],
            parameters=common,
        ),
        Task(
            id=paid_request,
            workflow_id=wf_id,
            name="Resolve canonical paid request snapshot",
            task_type="asset_paid_request_snapshot",
            depends_on=[concept_review],
            max_retries=10,
            parameters=common,
        ),
        Task(
            id=readiness,
            workflow_id=wf_id,
            name="Evaluate pre-spend production readiness",
            task_type="asset_production_readiness",
            depends_on=[paid_request],
            max_retries=10,
            parameters=common,
        ),
        Task(
            id=generate,
            workflow_id=wf_id,
            name="Approved Meshy image-to-3D generation",
            task_type="asset_paid_generation",
            cost_class=CostClass.PAID,
            depends_on=[readiness],
            parameters={
                **common,
                "cost": float(reservation),
                "cost_unit": provider_cost_unit,
                "estimate_label": "UNKNOWN" if provider_estimate is None else "KNOWN",
                "mandatory_approval_type": "paid_generation",
            },
        ),
        Task(
            id=process,
            workflow_id=wf_id,
            name="Process raw GLB in Blender",
            task_type="asset_process",
            depends_on=[generate],
            parameters=common,
        ),
        Task(
            id=validate,
            workflow_id=wf_id,
            name="Independently validate processed GLB",
            task_type="asset_validate",
            depends_on=[process],
            parameters=common,
        ),
        Task(
            id=runtime,
            workflow_id=wf_id,
            name="Import, run, and render in staged Godot",
            task_type="asset_godot",
            depends_on=[validate],
            parameters=common,
            timeout_seconds=180.0,
        ),
        Task(
            id=final,
            workflow_id=wf_id,
            name="Human final runtime visual review",
            task_type="asset_final_review",
            depends_on=[runtime],
            parameters=common,
        ),
        Task(
            id=finish,
            workflow_id=wf_id,
            name=(
                "Export and verify provider character evidence"
                if graph_version == "0.7.0"
                else "Record verified asset evidence"
            ),
            task_type=(
                "asset_v07_provider_evidence" if graph_version == "0.7.0" else "record_evidence"
            ),
            depends_on=[final],
            parameters=common if graph_version == "0.7.0" else {},
        ),
    ]
    return workflow, tasks


@dataclass
class AssetProductionHandlers:
    root: Path
    artifacts: ArtifactRepository
    revisions: AssetRevisionRepository
    approvals: ApprovalRepository
    intents: ProviderOperationIntentRepository
    evidence: EvidenceRepository
    gates: QualityGateRepository
    executions: ExecutionRepository
    artifact_manager: ArtifactManager
    provider: AssetGenerationProvider
    blender_path: str | None = None
    godot_path: str | None = None
    runner: ProcessRunner | None = None
    ledger: CostLedgerRepository | None = None
    accounting: CostAccounting | None = None
    paid_snapshots: PaidRequestSnapshotRepository | None = None
    readiness_reports: ProductionReadinessRepository | None = None
    readiness_probes: ReadinessProbes | None = None
    audit: AuditLogRepository | None = None
    tasks: TaskRepository | None = None
    concept_versions: ConceptVersionRepository | None = None
    profile_registry: ProfileRegistry | None = None
    _snapshot_repo: PaidRequestSnapshotRepository = field(init=False, repr=False)
    _readiness_repo: ProductionReadinessRepository = field(init=False, repr=False)
    _audit_repo: AuditLogRepository = field(init=False, repr=False)
    _task_repo: TaskRepository = field(init=False, repr=False)
    _concept_version_repo: ConceptVersionRepository = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = self.root.resolve(strict=True)
        self.runner = self.runner or ProcessRunner(sanitize_output=True)
        db = self.artifacts.db
        if self.accounting is None:
            ledger_repo = self.ledger or CostLedgerRepository(db)
            self.accounting = CostAccounting(ledger_repo, AuditLogRepository(db))
        self._snapshot_repo = self.paid_snapshots or PaidRequestSnapshotRepository(db)
        self._readiness_repo = self.readiness_reports or ProductionReadinessRepository(db)
        self._audit_repo = self.audit or AuditLogRepository(db)
        self._task_repo = self.tasks or TaskRepository(db)
        self._concept_version_repo = self.concept_versions or ConceptVersionRepository(db)

    def _task_specification(
        self, workflow: Workflow, task: Task
    ) -> AssetSpecification | AssetSpecificationV07:
        if is_v07_character_graph(task):
            specification = _parse_task_specification(
                task.parameters, self.profile_registry or builtin_v07_registry()
            )
            assert isinstance(specification, AssetSpecificationV07)
            revision_number = task.parameters.get("revision_number")
            if isinstance(revision_number, bool) or not isinstance(revision_number, int):
                raise ValidationError("V0.7 character revision binding is malformed")
            revision = self.revisions.get(str(task.parameters.get("asset_id")), revision_number)
            profile = specification.bound_profile()
            active_concept = self._concept_version_repo.active_for_workflow(workflow.id)
            expected_concept_hash = (
                active_concept.content_hash
                if active_concept is not None
                else task.parameters.get("concept_source_hash")
            )
            if (
                task.workflow_id != workflow.id
                or revision is None
                or revision.workflow_id != workflow.id
                or revision.spec_hash != task.parameters.get("specification_hash")
                or revision.profile_id != profile.profile_id
                or revision.profile_version != profile.version
                or revision.concept_hash != expected_concept_hash
            ):
                raise ValidationError("V0.7 character task differs from its durable asset revision")
            self._validate_persisted_character_dag(workflow, task, specification)
            return specification
        return parse_asset_specification(task.parameters["specification"])

    def _validate_persisted_character_dag(
        self,
        workflow: Workflow,
        current_task: Task,
        specification: AssetSpecificationV07,
    ) -> None:
        """Validate all immutable V0.7 graph bindings before a handler can act."""
        persisted_current = self._task_repo.get(current_task.id)
        try:
            current_parameters = _canonical_json(current_task.parameters)
            persisted_parameters = (
                _canonical_json(persisted_current.parameters)
                if persisted_current is not None
                else None
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError("V0.7 character handler task parameters are malformed") from exc
        if (
            persisted_current is None
            or persisted_current.workflow_id != workflow.id
            or persisted_current.task_type != current_task.task_type
            or persisted_current.cost_class != current_task.cost_class
            or persisted_current.depends_on != current_task.depends_on
            or persisted_parameters != current_parameters
        ):
            raise ValidationError("V0.7 character handler task differs from its persisted binding")
        task_rows = self._task_repo.list_by_workflow(workflow.id)
        expected = (
            ("PREPARE", "asset_prepare", CostClass.LOCAL, None, 60.0),
            ("CONCEPT-REVIEW", "asset_concept_review", CostClass.LOCAL, "PREPARE", 60.0),
            (
                "PAID-REQUEST",
                "asset_paid_request_snapshot",
                CostClass.LOCAL,
                "CONCEPT-REVIEW",
                60.0,
            ),
            ("READINESS", "asset_production_readiness", CostClass.LOCAL, "PAID-REQUEST", 60.0),
            ("PAID-GENERATION", "asset_paid_generation", CostClass.PAID, "READINESS", 60.0),
            ("PROCESS", "asset_process", CostClass.LOCAL, "PAID-GENERATION", 60.0),
            ("VALIDATE", "asset_validate", CostClass.LOCAL, "PROCESS", 60.0),
            ("GODOT", "asset_godot", CostClass.LOCAL, "VALIDATE", 180.0),
            ("FINAL-REVIEW", "asset_final_review", CostClass.LOCAL, "GODOT", 60.0),
            (
                "PROVIDER-EVIDENCE",
                "asset_v07_provider_evidence",
                CostClass.LOCAL,
                "FINAL-REVIEW",
                60.0,
            ),
        )
        if len(task_rows) != len(expected):
            raise ValidationError("Persisted V0.7 character graph is incomplete or has extra tasks")
        by_id = {item.id: item for item in task_rows}
        if len(by_id) != len(task_rows):
            raise ValidationError("Persisted V0.7 character graph has duplicate task identities")
        first_common: str | None = None
        # Reuse the atomic repository's one canonical inventory of graph-wide
        # immutable pins, so persisted-task rechecks cannot silently drift from
        # creation-time checks.
        from gamefactory.adapters.persistence.character_graph import _COMMON_FIELDS

        shared_fields = _COMMON_FIELDS
        for suffix, task_type, cost, depends_on, timeout in expected:
            row = by_id.get(f"{workflow.id}-{suffix}")
            expected_dependencies = [] if depends_on is None else [f"{workflow.id}-{depends_on}"]
            expected_retries = (
                10
                if task_type
                in {
                    "asset_paid_request_snapshot",
                    "asset_production_readiness",
                }
                else 1
            )
            if (
                row is None
                or row.workflow_id != workflow.id
                or row.task_type != task_type
                or row.cost_class != cost
                or row.depends_on != expected_dependencies
                or row.name != f"V0.7 character {suffix.lower().replace('-', ' ')}"
                or type(row.max_retries) is not int
                or row.max_retries != expected_retries
                or row.timeout_seconds != timeout
                or row.parameters.get("graph_version") != "0.7.0"
                or row.parameters.get("workflow_id") != workflow.id
                or type(row.parameters.get("revision_number")) is not int
                or row.parameters.get("revision_number")
                != current_task.parameters.get("revision_number")
            ):
                raise ValidationError(
                    "Persisted V0.7 character graph differs from its canonical DAG"
                )
            try:
                common_json = _canonical_json({key: row.parameters[key] for key in shared_fields})
            except (KeyError, TypeError, ValueError) as exc:
                raise ValidationError(
                    "Persisted V0.7 character graph has malformed shared pins"
                ) from exc
            if first_common is None:
                first_common = common_json
            elif common_json != first_common:
                raise ValidationError("Persisted V0.7 character graph has conflicting shared pins")
        if (
            current_task.parameters.get("specification_hash") != spec_fingerprint(specification)
            or current_task.parameters.get("asset_id") != specification.asset_id
        ):
            raise ValidationError(
                "Persisted V0.7 character graph has a stale task revision binding"
            )

    def _guard_paid_task(self, stage: str, workflow: Workflow, task: Task) -> None:
        _guard_paid_assembly_defense(
            stage=stage,
            task=task,
            workflow=workflow,
            task_repo=self._task_repo,
        )
        if is_v07_character_graph(task):
            self._task_specification(workflow, task)
            if stage != "prepare":
                self._require_current_character_concept(workflow, task)

    def _require_current_character_concept(self, workflow: Workflow, task: Task) -> None:
        """Require one active concept/provenance pair for this exact V0.7 revision."""
        asset_id = task.parameters.get("asset_id")
        revision_number = task.parameters.get("revision_number")
        if (
            not isinstance(asset_id, str)
            or not asset_id
            or type(revision_number) is not int
            or revision_number < 1
        ):
            raise ArtifactError("V0.7 character concept binding has an invalid revision identity")
        workflow_records = self._concept_version_repo.list_by_workflow(workflow.id)
        active_workflow = [row for row in workflow_records if row.status == "ACTIVE"]
        revision_records = self._concept_version_repo.list_for_revision(asset_id, revision_number)
        active_revision = [row for row in revision_records if row.status == "ACTIVE"]
        if (
            len(active_workflow) != 1
            or len(active_revision) != 1
            or active_workflow[0].id != active_revision[0].id
        ):
            raise ArtifactError(
                "V0.7 character requires exactly one active concept for its current workflow and revision"
            )
        row = active_workflow[0]
        if (
            row.workflow_id != workflow.id
            or row.asset_id != asset_id
            or type(row.revision_number) is not int
            or row.revision_number != revision_number
            or type(row.version) is not int
            or row.version < 1
            or row.status != "ACTIVE"
        ):
            raise ArtifactError("V0.7 character active concept row has a foreign revision binding")
        owner_task = self._unique_character_task(
            workflow,
            "asset_prepare" if row.version == 1 else "asset_concept_review",
            "current concept validation",
        )
        concept = self.artifacts.get(row.artifact_id)
        provenance = self.artifacts.get(row.provenance_artifact_id or "")
        if (
            concept is None
            or provenance is None
            or concept.workflow_id != workflow.id
            or provenance.workflow_id != workflow.id
            or concept.task_id != owner_task.id
            or provenance.task_id != owner_task.id
            or concept.artifact_type != "asset-concept"
            or provenance.artifact_type != "asset-concept-provenance"
            or concept.content_hash != row.content_hash
            or provenance.content_hash != row.provenance_hash
        ):
            raise ArtifactError(
                "V0.7 character active concept artifacts have stale workflow, owner, role, or hash bindings"
            )
        self.artifact_manager.verify_artifact_integrity(concept)
        self.artifact_manager.verify_artifact_integrity(provenance)
        provenance_document = _read_bounded_json_document(
            self.root / provenance.relative_path, "V0.7 character concept provenance"
        )
        if (
            not isinstance(provenance_document, dict)
            or provenance_document.get("asset_spec_hash")
            != task.parameters.get("specification_hash")
            or provenance_document.get("artifact_hash") != row.content_hash
        ):
            raise ArtifactError("V0.7 character provenance document is not bound to its concept")
        sidecar_path = provenance_document.get("sidecar_path")
        sidecar_hash = provenance_document.get("sidecar_hash")
        if sidecar_path is not None:
            if (
                not isinstance(sidecar_path, str)
                or not sidecar_path
                or Path(sidecar_path).is_absolute()
                or "\\" in sidecar_path
            ):
                raise ArtifactError("V0.7 character provenance sidecar path is not portable")
            sidecar = PathGuard(self.root).resolve_safe_path(sidecar_path)
            if not isinstance(sidecar_hash, str) or sha256_file(sidecar) != sidecar_hash:
                raise ArtifactError("V0.7 character provenance sidecar hash differs")

    def _unique_character_task(self, workflow: Workflow, task_type: str, stage: str) -> Task:
        matches = [
            item
            for item in self._task_repo.list_by_workflow(workflow.id)
            if item.task_type == task_type
        ]
        if len(matches) != 1:
            raise ArtifactError(
                f"V0.7 character graph requires exactly one {task_type} task before {stage}"
            )
        return matches[0]

    def _latest_character_execution(
        self, workflow: Workflow, task_type: str, stage: str
    ) -> tuple[Task, Execution]:
        task = self._unique_character_task(workflow, task_type, stage)
        if task.status != TaskStatus.COMPLETED:
            raise ArtifactError(
                f"V0.7 character {stage} requires the current {task_type} task to be COMPLETED"
            )
        attempts = self.executions.list_by_task(task.id)
        if not attempts:
            raise ArtifactError(f"V0.7 character {stage} has no {task_type} execution")
        latest_number = max(item.attempt_number for item in attempts)
        latest = [item for item in attempts if item.attempt_number == latest_number]
        if len(latest) != 1 or latest[0].status != ExecutionStatus.COMPLETED:
            latest_status = latest[0].status.value if len(latest) == 1 else "AMBIGUOUS"
            raise ArtifactError(
                f"V0.7 character {stage} requires the latest {task_type} execution to be COMPLETED; got {latest_status}"
            )
        return task, latest[0]

    def _character_stage_artifact(
        self,
        workflow: Workflow,
        task_type: str,
        artifact_type: str,
        stage: str,
        *,
        suffix: str,
    ) -> tuple[Task, Execution, Artifact]:
        task, attempt = self._latest_character_execution(workflow, task_type, stage)
        expected_name = (
            f"{suffix}-attempt-{attempt.attempt_number}.glb"
            if suffix
            in {
                "raw",
                "processed",
            }
            else f"{suffix}-attempt-{attempt.attempt_number}.json"
        )
        artifacts = [
            artifact
            for artifact in self.artifacts.list_by_task(task.id)
            if artifact.artifact_type == artifact_type
            and Path(artifact.relative_path).name == expected_name
        ]
        if len(artifacts) != 1:
            raise ArtifactError(
                f"V0.7 character {stage} requires exactly one {artifact_type} for latest attempt {attempt.attempt_number}"
            )
        artifact = artifacts[0]
        if artifact.workflow_id != workflow.id or artifact.task_id != task.id:
            raise ArtifactError(f"V0.7 character {stage} artifact identity changed")
        self.artifact_manager.verify_artifact_integrity(artifact)
        return task, attempt, artifact

    def _character_raw_artifact(self, workflow: Workflow, stage: str) -> Artifact:
        _task, _attempt, artifact = self._character_stage_artifact(
            workflow,
            "asset_paid_generation",
            "asset-raw-glb",
            stage,
            suffix="raw",
        )
        revision_number = _task.parameters.get("revision_number")
        if isinstance(revision_number, bool) or not isinstance(revision_number, int):
            raise ArtifactError(f"V0.7 character {stage} revision number is malformed")
        revision = self.revisions.get(str(_task.parameters.get("asset_id")), revision_number)
        if (
            revision is None
            or revision.workflow_id != workflow.id
            or revision.revision_number != _task.parameters.get("revision_number")
            or not revision.raw_glb_hash
            or artifact.content_hash != revision.raw_glb_hash
        ):
            raise ArtifactError(
                f"V0.7 character {stage} raw artifact differs from its revision pin"
            )
        return artifact

    def _character_processed_artifact(
        self, workflow: Workflow, stage: str, *, require_revision_pin: bool = True
    ) -> Artifact:
        process_task, _attempt, artifact = self._character_stage_artifact(
            workflow,
            "asset_process",
            "asset-processed-glb",
            stage,
            suffix="processed",
        )
        self._character_raw_artifact(workflow, stage)
        revision_number = process_task.parameters.get("revision_number")
        if isinstance(revision_number, bool) or not isinstance(revision_number, int):
            raise ArtifactError(f"V0.7 character {stage} revision number is malformed")
        revision = self.revisions.get(str(process_task.parameters.get("asset_id")), revision_number)
        if (
            revision is None
            or revision.workflow_id != workflow.id
            or revision.revision_number != process_task.parameters.get("revision_number")
            or (require_revision_pin and revision.processed_glb_hash != artifact.content_hash)
        ):
            raise ArtifactError(
                f"V0.7 character {stage} processed artifact differs from its revision pin"
            )
        return artifact

    def _character_validation_artifact(self, workflow: Workflow, stage: str) -> Artifact:
        _task, _attempt, artifact = self._character_stage_artifact(
            workflow,
            "asset_validate",
            "asset-validation-report",
            stage,
            suffix="validation",
        )
        self._character_processed_artifact(workflow, stage)
        revision_number = _task.parameters.get("revision_number")
        if isinstance(revision_number, bool) or not isinstance(revision_number, int):
            raise ArtifactError(f"V0.7 character {stage} revision number is malformed")
        revision = self.revisions.get(str(_task.parameters.get("asset_id")), revision_number)
        if (
            revision is None
            or revision.workflow_id != workflow.id
            or revision.revision_number != _task.parameters.get("revision_number")
            or revision.validation_report_hash != artifact.content_hash
        ):
            raise ArtifactError(
                f"V0.7 character {stage} validation report differs from its revision pin"
            )
        report = _read_bounded_json_document(
            self.root / artifact.relative_path,
            f"V0.7 character {stage} validation report",
        )
        if not isinstance(report, dict) or report.get("passed") is not True:
            raise ArtifactError(f"V0.7 character {stage} validation report is not a PASS")
        return artifact

    def _path(self, task: Task, name: str) -> Path:
        relative = f"{task.parameters['asset_dir']}/{name}"
        return PathGuard(self.root).ensure_safe_parent(relative)

    def _register(
        self, workflow: Workflow, task: Task, execution: Execution, kind: str, path: Path
    ) -> str:
        relative = path.resolve(strict=True).relative_to(self.root).as_posix()
        artifact = self.artifact_manager.register_file_artifact(
            workflow.id, task.id, kind, task.task_type, relative
        )
        self.artifacts.save(artifact)
        return artifact.id

    def prepare(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        if is_v07_character_graph(task):
            self._guard_paid_task("prepare", workflow, task)
            spec = self._task_specification(workflow, task)
        else:
            spec = parse_asset_specification(task.parameters["specification"])
        if spec_fingerprint(spec) != task.parameters["specification_hash"]:
            raise ValidationError("Asset specification fingerprint changed")
        bound = spec.bound_profile()
        if task.parameters.get("profile_id") not in (None, bound.profile_id) or (
            task.parameters.get("profile_version") not in (None, bound.version)
        ):
            raise ValidationError("Asset revision profile binding does not match the specification")
        path_guard = PathGuard(self.root)
        image_source = path_guard.resolve_safe_path(task.parameters["concept_source"])
        provenance_source = path_guard.resolve_safe_path(
            task.parameters["concept_provenance_source"]
        )
        if sha256_file(image_source) != task.parameters["concept_source_hash"]:
            raise ValidationError("Concept image changed after workflow creation")
        if sha256_file(provenance_source) != task.parameters["concept_provenance_hash"]:
            raise ValidationError("Concept provenance changed after workflow creation")
        directory = self._path(task, "specification.yml").parent
        directory.mkdir(parents=True, exist_ok=True)
        spec_path = self._path(task, "specification.json")
        spec_path.write_text(
            json.dumps(spec.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        profile_path: Path | None = None
        if isinstance(spec, AssetSpecificationV07):
            profile = spec.bound_profile()
            profile_path = self._path(task, "profile-v07.json")
            profile_path.write_text(
                json.dumps(
                    profile.document.model_dump(mode="json"),
                    sort_keys=True,
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
        provenance_path = self._path(task, "concept-provenance.json")
        from gamefactory.adapters.images.concept_ingest import ingest_concept_image

        concept_path, provenance = ingest_concept_image(
            image_source,
            self._path(task, "concept.png"),
            spec_fingerprint(spec),
            sidecar_provenance_path=provenance_source,
            source_type=task.parameters["concept_source_type"],
        )
        provenance_document = provenance.to_dict()
        if is_v07_character_graph(task) and provenance_document.get("sidecar_path") is not None:
            # V0.7 provider evidence must remain portable across workspaces.
            # This is a source path already pinned in the immutable task DAG.
            provenance_document["sidecar_path"] = task.parameters["concept_provenance_source"]
        provenance_path.write_text(
            json.dumps(provenance_document, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        ids = [
            self._register(workflow, task, execution, "asset-specification", spec_path),
            self._register(workflow, task, execution, "asset-concept", concept_path),
            self._register(workflow, task, execution, "asset-concept-provenance", provenance_path),
        ]
        if profile_path is not None:
            ids.append(self._register(workflow, task, execution, "asset-profile-v07", profile_path))
        if is_immutable_paid_graph(task):
            concept_hash = sha256_file(concept_path)
            provenance_hash = sha256_file(provenance_path)
            concept_record = ConceptVersionRecord(
                id=generate_id("VER"),
                workflow_id=workflow.id,
                asset_id=task.parameters["asset_id"],
                revision_number=int(task.parameters["revision_number"]),
                version=1,
                artifact_id=ids[1],
                content_hash=concept_hash,
                provenance_artifact_id=ids[2],
                provenance_hash=provenance_hash,
                provenance_type=provenance.provenance_type,
                source_type=provenance.source_type,
                status="ACTIVE",
                actor="system",
                reason="initial concept",
            )
            self._concept_version_repo.add(concept_record)
        return TaskHandlerResult(
            1, "Specification, concept, and provenance retained with immutable digests", ids
        )

    def concept_review_context(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        if is_v07_character_graph(task):
            self._guard_paid_task("concept_review_context", workflow, task)
        if not is_immutable_paid_graph(task):
            # Historical approvals were fingerprinted with this exact V0.5 shape;
            # changing it would invalidate every pre-V0.6 concept approval.
            values = self._artifact_hashes(workflow.id)
            legacy_expected = {
                key: values[key]
                for key in ("asset-specification", "asset-concept", "asset-concept-provenance")
            }
            return {
                "asset_id": task.parameters["asset_id"],
                "revision": task.parameters["revision_number"],
                "specification_hash": task.parameters["specification_hash"],
                "artifacts": legacy_expected,
                "style_constraints": task.parameters["specification"]["style_constraints"],
                "profile_id": bound_profile_id(task),
                "profile_version": bound_profile_version(task),
                "profile_qualified": bound_profile_qualified(task),
            }
        concept = self._active_concept_artifact(workflow.id)
        provenance = self._active_concept_provenance_artifact(workflow.id)
        concept_version = self._active_concept_version(workflow.id)
        spec_artifacts = [
            a
            for a in self.artifacts.list_by_workflow(workflow.id)
            if a.artifact_type == "asset-specification"
        ]
        if not spec_artifacts:
            raise ArtifactError("Asset specification artifact is missing")
        spec_art = spec_artifacts[0]
        self.artifact_manager.verify_artifact_integrity(spec_art)
        expected = {
            "asset-specification": spec_art.content_hash,
            "asset-concept": concept.content_hash,
            "asset-concept-provenance": provenance.content_hash,
        }
        if is_v07_character_graph(task):
            profile_artifacts = [
                a
                for a in self.artifacts.list_by_workflow(workflow.id)
                if a.artifact_type == "asset-profile-v07"
            ]
            if len(profile_artifacts) != 1:
                raise ArtifactError(
                    "Pinned V0.7 character profile artifact is missing or duplicated"
                )
            self.artifact_manager.verify_artifact_integrity(profile_artifacts[0])
            profile_path = self.root / profile_artifacts[0].relative_path
            try:
                retained_profile = json.loads(profile_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ArtifactError("Retained V0.7 character profile artifact is invalid") from exc
            if _canonical_hash(retained_profile) != task.parameters["profile_document_hash"]:
                raise ArtifactError("V0.7 character profile artifact differs from the graph pin")
            expected["asset-profile-v07"] = profile_artifacts[0].content_hash
        return {
            "concept_version": concept_version,
            "concept_sha256": concept.content_hash,
            "concept_provenance_sha256": provenance.content_hash,
            "asset_id": task.parameters["asset_id"],
            "revision": task.parameters["revision_number"],
            "specification_hash": task.parameters["specification_hash"],
            "artifacts": expected,
            "style_constraints": task.parameters["specification"]["style_constraints"],
            "profile_id": bound_profile_id(task),
            "profile_version": bound_profile_version(task),
            "profile_qualified": bound_profile_qualified(task),
        }

    def _active_concept_version(self, workflow_id: str) -> int:
        row = self._concept_version_repo.active_for_workflow(workflow_id)
        if row is not None:
            return int(row.version)
        return 1

    def _active_concept_artifact(self, workflow_id: str) -> Artifact:
        row = self._concept_version_repo.active_for_workflow(workflow_id)
        if row is not None:
            concept = self.artifacts.get(row.artifact_id)
            if concept is None:
                raise ArtifactError(f"Active concept artifact '{row.artifact_id}' is missing")
            self.artifact_manager.verify_artifact_integrity(concept)
            if concept.content_hash != row.content_hash:
                raise ArtifactError(
                    f"Active concept artifact content_hash '{concept.content_hash}' does not match concept_version record '{row.content_hash}'"
                )
            return concept
        concept_arts = [
            a
            for a in self.artifacts.list_by_workflow(workflow_id)
            if a.artifact_type == "asset-concept"
        ]
        if not concept_arts:
            raise ArtifactError("Active concept artifact is missing")
        if len(concept_arts) > 1:
            raise ArtifactError(f"Ambiguous concept artifacts found for workflow {workflow_id}")
        concept = concept_arts[0]
        self.artifact_manager.verify_artifact_integrity(concept)
        return concept

    def _active_concept_provenance_artifact(self, workflow_id: str) -> Artifact:
        row = self._concept_version_repo.active_for_workflow(workflow_id)
        if row is not None and row.provenance_artifact_id:
            prov = self.artifacts.get(row.provenance_artifact_id)
            if prov is None:
                raise ArtifactError(
                    f"Active concept provenance artifact '{row.provenance_artifact_id}' is missing"
                )
            self.artifact_manager.verify_artifact_integrity(prov)
            if row.provenance_hash and prov.content_hash != row.provenance_hash:
                raise ArtifactError(
                    f"Active concept provenance content_hash '{prov.content_hash}' does not match concept_version record '{row.provenance_hash}'"
                )
            return prov
        prov_arts = [
            a
            for a in self.artifacts.list_by_workflow(workflow_id)
            if a.artifact_type == "asset-concept-provenance"
        ]
        if not prov_arts:
            raise ArtifactError("Active concept provenance artifact is missing")
        if len(prov_arts) > 1:
            raise ArtifactError(
                f"Ambiguous concept provenance artifacts found for workflow {workflow_id}"
            )
        prov = prov_arts[0]
        self.artifact_manager.verify_artifact_integrity(prov)
        return prov

    def paid_review_context(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        if is_v07_character_graph(task):
            self._guard_paid_task("paid_review_context", workflow, task)
        concept = self._active_concept_artifact(workflow.id)
        context = {
            "provider": task.parameters["provider"],
            "operation": "image-to-3d",
            "asset_id": task.parameters["asset_id"],
            "revision": task.parameters["revision_number"],
            "concept_sha256": concept.content_hash,
            "asset_specification_sha256": task.parameters["specification_hash"],
            "estimated_cost": task.parameters["provider_estimate"]
            if task.parameters["provider_estimate"] is not None
            else "UNKNOWN",
            "budget_reservation": task.parameters["budget_reservation"],
            "cost_unit": task.parameters["cost_unit"],
            "profile_id": bound_profile_id(task),
            "profile_version": bound_profile_version(task),
            "profile_qualified": bound_profile_qualified(task),
        }
        if is_v07_character_graph(task):
            context.update(
                {
                    "graph_version": task.parameters["graph_version"],
                    "profile_document_sha256": task.parameters["profile_document_hash"],
                    "source_kind": task.parameters["source_kind"],
                    "category": task.parameters["category"],
                    "collider_policy": task.parameters["collider_policy"],
                }
            )
        return context

    def _artifact_hashes(self, workflow_id: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for item in self.artifacts.list_by_workflow(workflow_id):
            self.artifact_manager.verify_artifact_integrity(item)
            values[item.artifact_type] = item.content_hash
        return values

    def review_concept(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        approval = next(
            (
                a
                for a in self.approvals.list_by_workflow(workflow.id)
                if a.task_id == task.id
                and a.approval_type == "concept_review"
                and a.status.value == "APPROVED"
            ),
            None,
        )
        if approval is None:
            raise ArtifactError("Concept evidence is missing")
        concept_version = self._active_concept_version(workflow.id)
        attempt = execution.attempt_number
        receipt = self._path(task, f"concept-approval-v{concept_version}-a{attempt}.json")
        if receipt.exists():
            relative = receipt.resolve(strict=True).relative_to(self.root).as_posix()
            existing_artifacts = self.artifacts.list_by_workflow(workflow.id)
            if any(a.relative_path == relative for a in existing_artifacts):
                raise ArtifactError(f"Approval receipt file is already registered: {receipt.name}")
        receipt.write_text(
            json.dumps(
                {
                    "approval_id": approval.id,
                    "approval_type": approval.approval_type,
                    "status": approval.status.value,
                    "fingerprint": approval.operation_hash,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        artifact_id = self._register(
            workflow, task, execution, "asset-concept-approval-record", receipt
        )
        return TaskHandlerResult(
            1,
            "Concept human approval recorded; paid generation remains a separate gate",
            [artifact_id],
        )

    def paid_request_snapshot(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        self._guard_paid_task("paid_request_snapshot", workflow, task)
        concept_task = next(
            (
                t
                for t in self._task_repo.list_by_workflow(workflow.id)
                if t.task_type == "asset_concept_review"
            ),
            None,
        )
        concept_task_id = concept_task.id if concept_task else f"{workflow.id}-CONCEPT-REVIEW"
        approval = next(
            (
                a
                for a in self.approvals.list_by_workflow(workflow.id)
                if a.task_id == concept_task_id
                and a.approval_type == "concept_review"
                and a.status.value == "APPROVED"
            ),
            None,
        )
        if approval is None:
            raise ApprovalRequired(
                f"Approved concept review approval for '{concept_task_id}' is required before paid request snapshot can be generated"
            )

        params = task.parameters
        concept_art = self._active_concept_artifact(workflow.id)
        concept_ver = self._active_concept_version(workflow.id)

        binding = {
            "asset_id": params["asset_id"],
            "revision_number": int(params["revision_number"]),
            "concept_version": concept_ver,
            "concept_sha256": concept_art.content_hash,
            "specification_sha256": params["specification_hash"],
            "profile_id": bound_profile_id(task),
            "profile_version": bound_profile_version(task),
        }
        cost = {
            "estimate": params.get("provider_estimate"),
            "reservation": float(params["paid_reservation"]),
            "unit": params.get("cost_unit", "credits"),
        }

        if not hasattr(self.provider, "resolve_paid_request") or not callable(
            getattr(self.provider, "resolve_paid_request", None)
        ):
            raise PaidRequestIncompatibleError(
                f"Provider {getattr(self.provider, 'name', 'unknown')} does not implement resolve_paid_request",
                provider=getattr(self.provider, "name", None),
            )

        content = self.provider.resolve_paid_request(binding, params["specification"], cost)
        snapshot = PaidRequestSnapshot.from_content(content)

        if not hasattr(self.provider, "check_paid_request") or not callable(
            getattr(self.provider, "check_paid_request", None)
        ):
            raise PaidRequestIncompatibleError(
                f"Provider {getattr(self.provider, 'name', 'unknown')} does not implement check_paid_request",
                provider=getattr(self.provider, "name", None),
            )
        self.provider.check_paid_request(snapshot.content)

        attempt = execution.attempt_number
        filename = f"paid-request-snapshot-v{concept_ver}-a{attempt}.json"
        snapshot_bytes = snapshot.canonical_json.encode("utf-8")
        path = self._path(task, filename)
        path.write_bytes(snapshot_bytes)

        artifact_id = self._register(workflow, task, execution, "asset-paid-request-snapshot", path)
        registered_art = self.artifacts.get(artifact_id)
        assert registered_art is not None, "Failed to retrieve registered snapshot artifact"
        assert registered_art.content_hash == snapshot.sha256, (
            f"Artifact content hash {registered_art.content_hash} does not match snapshot sha256 {snapshot.sha256}"
        )

        active_rows = [
            r for r in self._snapshot_repo.list_by_workflow(workflow.id) if r.status == "ACTIVE"
        ]
        if active_rows:
            self._snapshot_repo.mark_superseded([r.id for r in active_rows])

        snap_record = PaidRequestSnapshotRecord(
            id=generate_id("SNAP"),
            workflow_id=workflow.id,
            task_id=task.id,
            asset_id=params["asset_id"],
            revision_number=int(params["revision_number"]),
            concept_version=concept_ver,
            schema_version=PAID_REQUEST_SCHEMA,
            snapshot_sha256=snapshot.sha256,
            canonical_json=snapshot.canonical_json,
            status="ACTIVE",
            artifact_id=artifact_id,
            created_at=utc_now_iso(),
        )
        self._snapshot_repo.save(snap_record)

        self._audit_repo.append(
            AuditEvent(
                id=generate_id("AUDIT"),
                entity_type="Task",
                entity_id=task.id,
                action="PAID_REQUEST_SNAPSHOT_CREATED",
                actor="AssetProductionHandlers",
                details={
                    "snapshot_sha256": snapshot.sha256,
                    "concept_version": concept_ver,
                    "provider": getattr(self.provider, "name", params.get("provider")),
                    "operation": "image-to-3d",
                },
            )
        )

        return TaskHandlerResult(
            1,
            "Paid request snapshot created and verified",
            [artifact_id],
        )

    def production_readiness(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        self._guard_paid_task("production_readiness", workflow, task)
        if self.readiness_probes is None:
            raise ProductionReadinessFailedError("no readiness probes configured")

        active_snap_record = self._snapshot_repo.get_active_for_workflow(workflow.id)
        if active_snap_record is None or not active_snap_record.artifact_id:
            raise ArtifactError(
                f"No active paid request snapshot record for workflow {workflow.id}"
            )

        art = self.artifacts.get(active_snap_record.artifact_id)
        if art is None:
            raise ArtifactError(
                f"Active paid request snapshot artifact {active_snap_record.artifact_id} not found"
            )
        self.artifact_manager.verify_artifact_integrity(art)

        snap_path = self.root / art.relative_path
        snap_text = snap_path.read_text(encoding="utf-8")
        snapshot = PaidRequestSnapshot.load_verified(snap_text, active_snap_record.snapshot_sha256)

        params = task.parameters
        spec = params.get("specification", {})
        prof = bound_profile_id(task)
        asset_dir_path = self._path(task, "").parent

        ctx = ReadinessContext(
            root=self.root,
            asset_dir=asset_dir_path,
            snapshot_content=snapshot.content,
            snapshot_sha256=snapshot.sha256,
            provider=self.provider,
            blender_path=self.blender_path,
            godot_path=self.godot_path,
            specification=spec,
            profile=prof,
            snapshot=snapshot,
        )

        checks = self.readiness_probes.evaluate(ctx)
        if is_v07_character_graph(task):
            # The legacy probe parser/registry is intentionally V0.6-only. For
            # the private V0.7 character bridge, replace only those profile
            # findings with checks against the injected trusted typed registry.
            v07_profile = self._task_specification(workflow, task).bound_profile()
            try:
                registered = (self.profile_registry or builtin_v07_registry()).get_v07(
                    v07_profile.profile_id, v07_profile.version
                )
                profile_supported = registered == v07_profile
            except Exception:
                profile_supported = False
            from gamefactory.core.domain.camera_framing import PLACED_VIEWS

            view_check = (
                len(v07_profile.review_views) in {5, 9}
                and all(view in PLACED_VIEWS for view in v07_profile.review_views)
                and {
                    "front",
                    "rear",
                    "left",
                    "right",
                    "three_quarter",
                }.issubset(set(v07_profile.review_views))
            )
            runtime_requirements = _character_runtime_requirements(v07_profile)
            runtime_check = isinstance(runtime_requirements, dict) and {
                "require_physics_body",
                "require_area",
                "require_ray_hit",
                "bounds_tolerance_floor_m",
                "bounds_tolerance_ratio",
            } <= set(runtime_requirements)
            profile_names = {
                "profile_supported",
                "profile_review_views_implemented",
                "profile_runtime_validations_implemented",
            }
            checks = [check for check in checks if check.name not in profile_names]
            checks.extend(
                (
                    ReadinessCheck(
                        "profile_supported",
                        "profile",
                        "PASS" if profile_supported else "FAIL",
                        True,
                        "V0.7 character profile matches the injected trusted registry",
                        {"profile": v07_profile.qualified},
                    ),
                    ReadinessCheck(
                        "profile_review_views_implemented",
                        "profile",
                        "PASS" if view_check else "FAIL",
                        True,
                        "V0.7 character review views are supported",
                        {"views": list(v07_profile.review_views)},
                    ),
                    ReadinessCheck(
                        "profile_runtime_validations_implemented",
                        "profile",
                        "PASS" if runtime_check else "FAIL",
                        True,
                        "V0.7 character runtime requirements are implemented",
                        {"requirements": sorted(runtime_requirements)},
                    ),
                )
            )
        now = utc_now_iso()
        report = build_readiness_report(ctx, checks, generated_at=now)
        if is_v07_character_graph(task):
            report = _portable_v07_readiness_report(report)
        result = report["result"]

        attempt = execution.attempt_number
        report_json = json.dumps(report, sort_keys=True, indent=2) + "\n"
        rep_file = self._path(task, f"production-readiness-a{attempt}.json")
        rep_file.write_text(report_json, encoding="utf-8")

        art_id = self._register(
            workflow, task, execution, "asset-production-readiness-report", rep_file
        )
        saved_art = self.artifacts.get(art_id)
        report_sha = (
            saved_art.content_hash
            if saved_art
            else hashlib.sha256(report_json.encode("utf-8")).hexdigest()
        )

        active_prior = [
            r for r in self._readiness_repo.list_by_workflow(workflow.id) if r.status == "ACTIVE"
        ]
        if active_prior:
            self._readiness_repo.mark_superseded([r.id for r in active_prior])

        rec = ProductionReadinessRecord(
            id=generate_id("READINESS"),
            workflow_id=workflow.id,
            task_id=task.id,
            snapshot_sha256=snapshot.sha256,
            result=result,
            schema_version=READINESS_SCHEMA,
            report_json=report_json,
            report_sha256=report_sha,
            status="ACTIVE",
            artifact_id=art_id,
            created_at=now,
        )
        self._readiness_repo.save(rec)

        self._audit_repo.append(
            AuditEvent(
                id=generate_id("AUDIT"),
                entity_type="Task",
                entity_id=task.id,
                action="PRODUCTION_READINESS_EVALUATED",
                actor="AssetProductionHandlers",
                details={
                    "result": result,
                    "snapshot_sha256": snapshot.sha256,
                    "report_sha256": report_sha,
                    "checks_count": len(checks),
                },
            )
        )

        if result == "FAIL":
            failing = [c.name for c in checks if c.status == "FAIL"]
            raise ProductionReadinessFailedError(
                f"Production readiness evaluation failed: {', '.join(failing)}",
                details={"failing_checks": failing, "report_sha256": report_sha},
            )

        return TaskHandlerResult(1, "Production readiness evaluated: PASS", [art_id])

    def bind_paid_request_parameters(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        self._guard_paid_task("bind_paid_request_parameters", workflow, task)
        params = dict(task.parameters)
        if not is_immutable_paid_graph(task):
            return params

        active_snap = self._snapshot_repo.get_active_for_workflow(workflow.id)
        active_readiness = self._readiness_repo.get_active_for_workflow(workflow.id)

        if (
            active_snap is not None
            and active_readiness is not None
            and active_readiness.result == "PASS"
        ):
            snap_content = json.loads(active_snap.canonical_json)
            params["paid_request_snapshot_sha256"] = active_snap.snapshot_sha256
            params["paid_request_snapshot"] = snap_content
            params["production_readiness_report_sha256"] = active_readiness.report_sha256
        else:
            params.pop("paid_request_snapshot_sha256", None)
            params.pop("paid_request_snapshot", None)
            params.pop("production_readiness_report_sha256", None)

        return params

    def _recheck_before_submission(
        self, workflow: Workflow, task: Task, snapshot: PaidRequestSnapshot
    ) -> None:
        failing: list[str]
        if self.readiness_probes is None:
            failing = ["readiness_probes_configured"]
        else:
            context = ReadinessContext(
                root=self.root,
                asset_dir=self._path(task, "readiness.probe").parent,
                snapshot_content=snapshot.content,
                snapshot_sha256=snapshot.sha256,
                provider=self.provider,
                blender_path=self.blender_path,
                godot_path=self.godot_path,
                specification=task.parameters.get("specification", {}),
                profile=bound_profile_id(task),
                snapshot=snapshot,
            )
            failing = [
                check.name
                for check in self.readiness_probes.recheck_critical(context)
                if check.status != "PASS"
            ]
        if not failing:
            return
        self._audit_repo.append(
            AuditEvent(
                id=generate_id("AUDIT"),
                entity_type="Task",
                entity_id=task.id,
                action="PRODUCTION_READINESS_RECHECK_FAILED",
                actor="AssetProductionHandlers",
                details={
                    "workflow_id": workflow.id,
                    "failing_checks": failing,
                    "paid_request_snapshot_sha256": snapshot.sha256,
                    "provider_submission": False,
                },
            )
        )
        raise ProductionReadinessFailedError(
            "Critical readiness recheck failed before paid submission; no provider "
            f"request was sent: {', '.join(failing)}",
            details={"failing_checks": failing},
        )

    def paid_generate(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        self._guard_paid_task("paid_generate", workflow, task)
        params = task.parameters
        concept = self._active_concept_artifact(workflow.id)
        approval_rows = self.approvals.list_by_workflow(workflow.id)
        approval = next(
            (
                a
                for a in approval_rows
                if a.task_id == task.id
                and a.approval_type == "paid_generation"
                and a.status.value == "APPROVED"
            ),
            None,
        )
        if approval is None or approval.status.value != "APPROVED":
            raise ValidationError("Current paid-generation approval is missing or stale")
        if (
            params.get("provider_estimate") is None
            and float(params.get("budget_reservation", 0.0)) <= 0
        ):
            raise ValidationError(
                "Provider cost is UNKNOWN and no explicit budget reservation was authorized; no paid request was sent"
            )
        raw_path = self._path(task, f"raw-attempt-{execution.attempt_number}.glb")
        if raw_path.exists():
            raise RawArtifactInvalidError("Refusing to overwrite a raw GLB from an earlier attempt")

        snapshot: PaidRequestSnapshot | None = None
        if is_immutable_paid_graph(task):
            snap_sha = params.get("paid_request_snapshot_sha256")
            readiness_sha = params.get("production_readiness_report_sha256")
            if not snap_sha or not readiness_sha:
                raise PaidRequestInvalidError(
                    "Provider paid generation requires paid_request_snapshot_sha256 and production_readiness_report_sha256 in task parameters"
                )
            active_snap_record = self._snapshot_repo.get_active_for_workflow(workflow.id)
            if active_snap_record is None or active_snap_record.snapshot_sha256 != snap_sha:
                raise PaidRequestInvalidError(
                    f"Task parameters snapshot sha '{snap_sha}' does not match active snapshot row '{active_snap_record.snapshot_sha256 if active_snap_record else None}'"
                )
            if approval.paid_request_snapshot_hash != snap_sha:
                raise PaidRequestInvalidError(
                    f"Approval paid_request_snapshot_hash '{approval.paid_request_snapshot_hash}' does not match snapshot sha '{snap_sha}'"
                )
            if not active_snap_record.artifact_id:
                raise PaidRequestInvalidError("Active snapshot row is missing artifact_id")
            snap_artifact = self.artifacts.get(active_snap_record.artifact_id)
            if snap_artifact is None:
                raise PaidRequestInvalidError(
                    f"Active snapshot artifact '{active_snap_record.artifact_id}' not found"
                )
            self.artifact_manager.verify_artifact_integrity(snap_artifact)
            if snap_artifact.content_hash != snap_sha:
                raise PaidRequestInvalidError(
                    f"Snapshot artifact content_hash '{snap_artifact.content_hash}' does not match expected '{snap_sha}'"
                )
            snap_file = self.root / snap_artifact.relative_path
            snap_text = snap_file.read_text(encoding="utf-8")
            snapshot = PaidRequestSnapshot.load_verified(snap_text, snap_sha)
            # Provider-agnostic: the approved request must describe the ACTIVE concept
            # of this revision (an interrupted concept replacement can never be paid for).
            binding = snapshot.content["binding"]
            if (
                binding.get("concept_sha256") != concept.content_hash
                or binding.get("concept_version") != self._active_concept_version(workflow.id)
                or binding.get("asset_id") != params["asset_id"]
                or binding.get("revision_number") != int(params["revision_number"])
                or binding.get("specification_sha256") != params["specification_hash"]
            ):
                raise PaidRequestInvalidError(
                    "Approved paid request snapshot is not bound to the active concept "
                    "version of this revision; no provider request was sent"
                )
            if self.intents.get_by_task(task.id) is None:
                # A NEW paid submission is about to happen: recheck the critical
                # prerequisites cheaply. Query-only recovery is never blocked here.
                self._recheck_before_submission(workflow, task, snapshot)

        concept_path = self.root / concept.relative_path
        request_params = {
            **params,
            "workflow_id": workflow.id,
            "task_id": task.id,
            "approval_id": approval.id,
            "concept_hash": concept.content_hash,
            "concept_image_path": str(concept_path),
            "output_path": str(raw_path),
            "cost": params.get("provider_estimate"),
            "budget_reservation": params.get("budget_reservation"),
            "max_triangles_lod0": params["specification"]["geometry_budget"]["max_triangles_lod0"],
        }
        if is_immutable_paid_graph(task) and params.get("paid_request_snapshot_sha256"):
            request_params["paid_request_snapshot_sha256"] = params["paid_request_snapshot_sha256"]

        request = GenerationRequest(
            prompt=f"Image-to-3D {bound_profile_qualified(task)}: {params['asset_id']}",
            target_format="glb",
            parameters=request_params,
            operation_hash=approval.operation_hash,
            paid_request=snapshot,
        )

        def account_known_cost(response_cost: object = None, *, settled: bool = False) -> None:
            """Persist known billing before any later download or validation can fail.

            While the provider task is in flight or its outcome is uncertain, the
            authorized reservation stays held even if a lower cost was reported.
            Once the provider reports terminal success, the reported actual is the
            settled charge and replaces the reservation.
            """
            intent = self.intents.get_by_task(task.id)
            values = [
                value
                for value in (response_cost, getattr(intent, "actual_cost", None))
                if value is not None
            ]
            if not values:
                return
            candidates: list[float] = []
            for value in values:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or float(value) < 0
                ):
                    raise ValidationError(
                        "Provider actual cost must be a finite non-negative number"
                    )
                candidates.append(float(value))
            known_actual = max(candidates)
            previous_liability = sum(
                max(0.0, attempt.cost)
                for attempt in self.executions.list_by_task(task.id)
                if attempt.id != execution.id
            )
            authorized_reservation = max(0.0, float(params.get("cost", 0.0)))
            cumulative_liability = (
                known_actual if settled else max(authorized_reservation, known_actual)
            )
            execution.cost = max(0.0, cumulative_liability - previous_liability)
            execution.cost_unit = getattr(intent, "cost_unit", "credits")

        def record_ledger_cost(response_cost: object = None, *, settled: bool = False) -> None:
            if self.accounting is None:
                return
            intent = self.intents.get_by_task(task.id)
            values = [
                value
                for value in (response_cost, getattr(intent, "actual_cost", None))
                if value is not None
                and not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(float(value))
                and float(value) >= 0
            ]
            actual = max(values) if values else None
            if settled:
                self.accounting.settle_terminal_success(
                    task.id,
                    actual,
                    execution_id=execution.id,
                    intent=intent,
                    cost_unit=getattr(intent, "cost_unit", "credits"),
                )
            elif actual is not None:
                self.accounting.handle_in_flight(
                    task.id,
                    actual,
                    execution_id=execution.id,
                    intent=intent,
                    cost_unit=getattr(intent, "cost_unit", "credits"),
                )

        try:
            response = self.provider.generate(request)
            account_known_cost(
                response.details.get("actual_cost"),
                settled=response.status == "SUCCESS",
            )
            record_ledger_cost(
                response.details.get("actual_cost"),
                settled=response.status == "SUCCESS",
            )
        except ProviderUncertainError as exc:
            account_known_cost()
            if self.accounting is not None:
                self.accounting.handle_failure(
                    task.id,
                    execution_id=execution.id,
                    intent=self.intents.get_by_task(task.id),
                    submission_uncertain=True,
                )
            raise ProviderUncertainError(
                str(exc),
                task_id=task.id,
                execution_id=execution.id,
                provider=self.provider.name,
                details={**(exc.details or {}), "process_state_uncertain": True},
            ) from exc
        except Exception:
            account_known_cost()
            if self.accounting is not None:
                self.accounting.handle_failure(
                    task.id,
                    execution_id=execution.id,
                    intent=self.intents.get_by_task(task.id),
                )
            raise
        if response.status == "SUBMITTED":
            deadline = time.monotonic() + min(task.timeout_seconds, 300.0)
            while response.status == "SUBMITTED" and time.monotonic() < deadline:
                time.sleep(2.0)
                try:
                    response = self.provider.generate(request)
                    account_known_cost(
                        response.details.get("actual_cost"),
                        settled=response.status == "SUCCESS",
                    )
                    record_ledger_cost(
                        response.details.get("actual_cost"),
                        settled=response.status == "SUCCESS",
                    )
                except ProviderUncertainError as exc:
                    account_known_cost()
                    if self.accounting is not None:
                        self.accounting.handle_failure(
                            task.id,
                            execution_id=execution.id,
                            intent=self.intents.get_by_task(task.id),
                            submission_uncertain=True,
                        )
                    raise ProviderUncertainError(
                        str(exc),
                        task_id=task.id,
                        execution_id=execution.id,
                        provider=self.provider.name,
                        details={**(exc.details or {}), "process_state_uncertain": True},
                    ) from exc
                except Exception:
                    account_known_cost()
                    if self.accounting is not None:
                        self.accounting.handle_failure(
                            task.id,
                            execution_id=execution.id,
                            intent=self.intents.get_by_task(task.id),
                        )
                    raise
        if response.status != "SUCCESS":
            if self.accounting is not None:
                self.accounting.handle_failure(
                    task.id,
                    execution_id=execution.id,
                    intent=self.intents.get_by_task(task.id),
                    submission_uncertain=True,
                )
            raise ProviderUncertainError(
                "Provider operation has no confirmed downloadable terminal result; retry is forbidden",
                task_id=task.id,
                execution_id=execution.id,
                provider=self.provider.name,
                details={
                    "process_state_uncertain": True,
                    "external_op_id": response.external_op_id,
                },
            )
        output = Path(response.output_path).resolve(strict=True) if response.output_path else None
        if output is None and hasattr(self.provider, "cli_runner"):
            download_dir = self._path(
                task, f"download-attempt-{execution.attempt_number}/.keep"
            ).parent
            download_dir.mkdir(parents=True, exist_ok=True)
            output, _ = self.provider.cli_runner.download_glb(response.external_op_id, download_dir)
        if output is None or not output.is_file() or output.stat().st_size <= 0:
            raise RawArtifactInvalidError(
                "Provider terminal success did not produce a verified raw GLB"
            )
        try:
            preflight_glb(output)
        except (ValueError, OSError) as exc:
            raise RawArtifactInvalidError(
                f"Provider raw GLB failed structural validation: {exc}"
            ) from exc
        if output != raw_path.resolve():
            shutil.copyfile(output, raw_path)
        raw = self._register(workflow, task, execution, "asset-raw-glb", raw_path)
        revision = self.revisions.get(params["asset_id"], params["revision_number"])
        if revision is None:
            raise ValidationError("Asset revision record disappeared")
        revision.raw_glb_hash = sha256_file(raw_path)
        self.revisions.save(revision)
        execution.external_op_id = response.external_op_id
        execution.provider = self.provider.name
        # Keep actual provider billing in the durable intent as well. On query-only
        # recovery, only the incremental delta above the original reservation is
        # charged, preventing duplicate liability while covering overages.
        execution.cost_unit = response.cost_unit
        return TaskHandlerResult(
            1, "Provider task completed and a raw GLB was downloaded and retained", [raw]
        )

    def recovery_check(self, workflow: Workflow, task: Task, execution: Execution) -> bool:
        """Return true only when the durable intent has a known remote ID safe to query."""
        self._guard_paid_task("paid_recovery_check", workflow, task)
        del execution
        intent = self.intents.get_by_task(task.id)
        if intent is None or not intent.external_task_id:
            return False
        cli = getattr(self.provider, "cli_runner", None)
        if cli is None:
            return True  # Deterministic fake adapter resolves the persisted intent locally.
        try:
            remote = cli.get_task(intent.external_task_id)
        except Exception:
            return False
        return isinstance(remote, dict) and bool(remote.get("status"))

    def process(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        self._guard_paid_task("process", workflow, task)
        spec = self._task_specification(workflow, task)
        raw_artifact: Artifact | None
        if isinstance(spec, AssetSpecificationV07):
            raw_artifact = self._character_raw_artifact(workflow, "Blender processing")
        else:
            raw_artifact = next(
                (
                    a
                    for a in self.artifacts.list_by_workflow(workflow.id)
                    if a.artifact_type == "asset-raw-glb"
                ),
                None,
            )
        if raw_artifact is None:
            raise ArtifactError("Raw generation GLB is missing")
        raw = self.root / raw_artifact.relative_path
        self.artifact_manager.verify_artifact_integrity(raw_artifact)
        processed = self._path(task, f"processed-attempt-{execution.attempt_number}.glb")
        report = self._path(task, f"processing-attempt-{execution.attempt_number}.json")
        if isinstance(spec, AssetSpecificationV07):
            profile = spec.bound_profile()
            from gamefactory.adapters.dcc.character_processor import CharacterProcessor

            character_result = CharacterProcessor(self.blender_path, self.runner).process_character(
                raw,
                spec,
                profile,
                expected_raw_glb_sha256=raw_artifact.content_hash,
                processed_glb_path=processed,
                report_path=report,
                timeout_seconds=task.timeout_seconds,
            )
            if character_result.status != "SUCCESS":
                raise DccFailedError(
                    "Blender character processor did not produce SUCCESS",
                    details={"asset_id": spec.asset_id, "status": character_result.status},
                )
            ids = [
                self._register(workflow, task, execution, "asset-processed-glb", processed),
                self._register(workflow, task, execution, "asset-processing-report", report),
            ]
            return TaskHandlerResult(
                1,
                "Blender produced a separate bounded character GLB from the retained provider artifact",
                ids,
            )
        try:
            legacy_result = BlenderAssetProcessor(self.blender_path, self.runner).process_asset(
                raw, processed, spec, report
            )
        except ValueError as exc:
            raise RawArtifactInvalidError(
                "Raw provider GLB failed safe preflight before Blender processing",
                details={"reason": str(exc)},
            ) from exc
        if legacy_result.status != "SUCCESS":
            raise DccFailedError(
                "Blender processor did not produce SUCCESS",
                exit_code=legacy_result.exit_code,
                details={"asset_id": spec.asset_id, "status": legacy_result.status},
            )
        ids = [
            self._register(workflow, task, execution, "asset-processed-glb", processed),
            self._register(workflow, task, execution, "asset-processing-report", report),
        ]
        revision = self.revisions.get(spec.asset_id, int(task.parameters["revision_number"]))
        if revision is None:
            raise ValidationError("Asset revision record disappeared")
        return TaskHandlerResult(
            1, "Blender produced a separate processed GLB from the retained raw artifact", ids
        )

    def validate(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        from gamefactory.adapters.assets.glb_validator import validate_glb

        self._guard_paid_task("validate", workflow, task)
        spec = self._task_specification(workflow, task)
        artifact: Artifact | None
        if isinstance(spec, AssetSpecificationV07):
            artifact = self._character_processed_artifact(
                workflow, "independent validation", require_revision_pin=False
            )
        else:
            artifact = next(
                (
                    a
                    for a in self.artifacts.list_by_workflow(workflow.id)
                    if a.artifact_type == "asset-processed-glb"
                ),
                None,
            )
        if artifact is None:
            raise ArtifactError("Processed GLB is missing")
        path = self.root / artifact.relative_path
        self.artifact_manager.verify_artifact_integrity(artifact)
        if isinstance(spec, AssetSpecificationV07):
            from gamefactory.adapters.assets.v07_geometry_validation import validate_v07_geometry

            v07_result = validate_v07_geometry(path, spec, spec.bound_profile())
            result_data = {
                "schema_version": "asset-validation-report-0.7.0",
                "passed": v07_result.passed,
                "findings": [
                    {
                        "rule_id": item.rule_id,
                        "passed": item.passed,
                        "expected": item.expected,
                        "actual": item.actual,
                        "message": item.message,
                    }
                    for item in v07_result.findings
                ],
            }
            passed = v07_result.passed
        else:
            legacy_result = validate_glb(path, spec)
            result_data = legacy_result.to_dict()
            passed = legacy_result.passed
        report = self._path(task, f"validation-attempt-{execution.attempt_number}.json")
        report.write_text(
            json.dumps(result_data, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        report_id = self._register(workflow, task, execution, "asset-validation-report", report)
        revision = self.revisions.get(spec.asset_id, int(task.parameters["revision_number"]))
        if revision is None:
            raise ValidationError("Asset revision record disappeared")
        if not passed:
            raise AssetValidationFailedError("Processed GLB failed deterministic asset validation")
        revision.processed_glb_hash = artifact.content_hash
        revision.validation_report_hash = sha256_file(report)
        self.revisions.save(revision)
        return TaskHandlerResult(1, "Independent GLB validator passed", [report_id])

    def godot(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        self._guard_paid_task("godot", workflow, task)
        specification = self._task_specification(workflow, task)
        if isinstance(specification, AssetSpecificationV07):
            self._character_validation_artifact(workflow, "Godot runtime")
            result = self._run_godot_character(workflow, task, execution, specification)
            revision = self.revisions.get(
                str(task.parameters["asset_id"]), int(task.parameters["revision_number"])
            )
            if revision is None:
                raise ValidationError("Asset revision record disappeared")
            runtime_artifacts = [
                artifact
                for artifact in self.artifacts.list_by_workflow(workflow.id)
                if artifact.id in result
            ]
            revision.runtime_evidence_hashes = list(
                dict.fromkeys(
                    [
                        *revision.runtime_evidence_hashes,
                        *(artifact.content_hash for artifact in runtime_artifacts),
                    ]
                )
            )
            self.revisions.save(revision)
            return TaskHandlerResult(
                1,
                "Godot verified the current processed character and captured all profile views",
                result,
            )
        result = run_asset_in_godot(
            self.root,
            self.artifacts,
            self.artifact_manager,
            workflow,
            task,
            execution,
            self.godot_path,
            self.runner,
        )
        revision = self.revisions.get(
            str(task.parameters["asset_id"]), int(task.parameters["revision_number"])
        )
        if revision is None:
            raise ValidationError("Asset revision record disappeared")
        runtime_artifacts = [
            a for a in self.artifacts.list_by_workflow(workflow.id) if a.id in result
        ]
        revision.runtime_evidence_hashes = list(
            dict.fromkeys(
                [*revision.runtime_evidence_hashes, *(a.content_hash for a in runtime_artifacts)]
            )
        )
        self.revisions.save(revision)
        return TaskHandlerResult(
            1, "Staged Godot imported, observed, and rendered the validated asset", result
        )

    def _run_godot_character(
        self,
        workflow: Workflow,
        task: Task,
        execution: Execution,
        specification: AssetSpecificationV07,
    ) -> list[str]:
        from gamefactory.adapters.dcc.godot_character import verify_godot_character

        profile = specification.bound_profile()
        revision = self.revisions.get(
            specification.asset_id, int(task.parameters["revision_number"])
        )
        if revision is None or not revision.raw_glb_hash or not revision.processed_glb_hash:
            raise ArtifactError("V0.7 character raw/processed revision pins are incomplete")
        raw_artifact = self._character_raw_artifact(workflow, "Godot runtime")
        processed_artifact = self._character_processed_artifact(workflow, "Godot runtime")
        self.artifact_manager.verify_artifact_integrity(raw_artifact)
        self.artifact_manager.verify_artifact_integrity(processed_artifact)
        result = verify_godot_character(
            specification,
            profile,
            self.root / raw_artifact.relative_path,
            raw_artifact.content_hash,
            self.root / processed_artifact.relative_path,
            processed_artifact.content_hash,
            execution.id,
            attempt_number=execution.attempt_number,
            workflow_id=workflow.id,
            revision=int(task.parameters["revision_number"]),
            godot_executable=self.godot_path,
            runner=self.runner,
            timeout_seconds=task.timeout_seconds,
        )
        if result.status != "PASS" or set(result.captures) != set(profile.review_views):
            raise RuntimeValidationFailedError(
                "Godot did not verify every current V0.7 character review view",
                details={"status": result.status, "errors": result.findings},
            )
        artifact_ids: list[str] = []
        attempt_dir = f"runtime/{execution.id}-a{execution.attempt_number}"
        for name, content in result.artifacts.items():
            if name in {
                "runtime-request.json",
                "runtime-observation.json",
                "character_runtime_harness_v07.gd",
            }:
                role = {
                    "runtime-request.json": "asset-runtime-request",
                    "runtime-observation.json": "asset-runtime-observation",
                    "character_runtime_harness_v07.gd": "asset-runtime-harness",
                }[name]
                filename = name
            elif name.endswith(".png") and name[:-4] in profile.review_views:
                role = "asset-runtime-capture"
                filename = f"{execution.id}-{name}"
            else:
                raise ArtifactError(
                    f"Godot character adapter returned an unexpected artifact: {name}"
                )
            path = self._path(task, f"{attempt_dir}/{filename}")
            try:
                with path.open("xb") as stream:
                    stream.write(content)
            except FileExistsError as exc:
                raise ArtifactError(
                    f"Refusing to overwrite retained runtime artifact: {path.name}"
                ) from exc
            artifact_ids.append(self._register(workflow, task, execution, role, path))
        return artifact_ids

    def final_review_context(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        v07_runtime_task: Task | None = None
        v07_runtime_execution: Execution | None = None
        if is_v07_character_graph(task):
            self._guard_paid_task("final_review_context", workflow, task)
            self._character_validation_artifact(workflow, "final visual review")
            v07_runtime_task, v07_runtime_execution = self._latest_character_execution(
                workflow, "asset_godot", "final visual review"
            )
        hashes = self._artifact_hashes(workflow.id)
        artifact_list = self.artifacts.list_by_workflow(workflow.id)
        roles = {artifact.artifact_type: artifact.content_hash for artifact in artifact_list}
        active_concept = self._active_concept_artifact(workflow.id)
        roles["asset-concept"] = active_concept.content_hash
        required = (
            "asset-concept",
            "asset-processed-glb",
            "asset-validation-report",
            "asset-runtime-observation",
            "asset-runtime-capture",
        )
        if any(role not in roles for role in required):
            raise ArtifactError(
                "Final review cannot open: required concept, processing, validation, or runtime evidence is missing"
            )
        if v07_runtime_task is not None and v07_runtime_execution is not None:
            runtime_prefix = f"/{v07_runtime_execution.id}-a{v07_runtime_execution.attempt_number}/"
            observations = [
                artifact
                for artifact in artifact_list
                if artifact.task_id == v07_runtime_task.id
                and artifact.artifact_type == "asset-runtime-observation"
                and runtime_prefix in f"/{artifact.relative_path}"
            ]
            if len(observations) != 1:
                raise ArtifactError("Latest V0.7 character Godot attempt has no unique observation")
            observation_artifact = observations[0]
            self.artifact_manager.verify_artifact_integrity(observation_artifact)
        else:
            observation_artifact = next(
                a for a in reversed(artifact_list) if a.artifact_type == "asset-runtime-observation"
            )
        observation = json.loads(
            (self.root / observation_artifact.relative_path).read_text(encoding="utf-8")
        )
        selected_captures = select_review_captures(
            [
                artifact
                for artifact in artifact_list
                if artifact.artifact_type == "asset-runtime-capture"
            ],
            str(observation.get("execution_id", "")),
            self._task_specification(workflow, task).bound_profile().review_views,
        )
        runtime_capture_hashes = sorted(artifact.content_hash for artifact in selected_captures)
        context = {
            "workflow_id": workflow.id,
            "revision": task.parameters["revision_number"],
            "specification_hash": task.parameters["specification_hash"],
            "artifacts": {key: roles[key] for key in required},
            "runtime_capture_hashes": runtime_capture_hashes,
            "all_artifacts_verified": bool(hashes),
            "profile_id": bound_profile_id(task),
            "profile_version": bound_profile_version(task),
            "profile_qualified": bound_profile_qualified(task),
        }
        if is_v07_character_graph(task):
            assert v07_runtime_task is not None and v07_runtime_execution is not None
            godot_task = v07_runtime_task
            runtime_execution = v07_runtime_execution
            current_rows = [
                item
                for item in artifact_list
                if item.task_id == godot_task.id
                and f"/{runtime_execution.id}-a{runtime_execution.attempt_number}/"
                in f"/{item.relative_path}"
            ]
            request_rows = [
                item for item in current_rows if item.artifact_type == "asset-runtime-request"
            ]
            observation_rows = [
                item for item in current_rows if item.artifact_type == "asset-runtime-observation"
            ]
            harness_rows = [
                item for item in current_rows if item.artifact_type == "asset-runtime-harness"
            ]
            if len(request_rows) != 1 or len(observation_rows) != 1 or len(harness_rows) != 1:
                raise ArtifactError(
                    "Current V0.7 character Godot request, observation, or harness is missing"
                )
            specification = self._task_specification(workflow, task)
            assert isinstance(specification, AssetSpecificationV07)
            profile_document = specification.bound_profile().document.model_dump(mode="json")
            request_row, observation_row, harness_row = (
                request_rows[0],
                observation_rows[0],
                harness_rows[0],
            )
            request_path = self.root / request_row.relative_path
            observation_path = self.root / observation_row.relative_path
            for item in (request_row, observation_row, harness_row):
                self.artifact_manager.verify_artifact_integrity(item)
            request = _read_bounded_json_document(
                request_path, "Current V0.7 character Godot request"
            )
            observation = _read_bounded_json_document(
                observation_path, "Current V0.7 character Godot observation"
            )
            revision = self.revisions.get(
                str(task.parameters["asset_id"]), int(task.parameters["revision_number"])
            )
            if (
                revision is None
                or not revision.processed_glb_hash
                or not revision.raw_glb_hash
                or not revision.validation_report_hash
            ):
                raise ArtifactError("Current V0.7 character revision pins are missing")
            request_digest = request.get("request_digest")
            unsigned_request = dict(request)
            unsigned_request.pop("request_digest", None)
            if (
                not isinstance(request_digest, str)
                or _canonical_hash(unsigned_request) != request_digest
            ):
                raise ArtifactError("Godot character runtime request digest is invalid")
            processed_rows = [
                item
                for item in artifact_list
                if item.artifact_type == "asset-processed-glb"
                and item.content_hash == revision.processed_glb_hash
            ]
            if len(processed_rows) != 1:
                raise ArtifactError("Current V0.7 character processed GLB is missing or duplicated")
            validation_rows = [
                item
                for item in artifact_list
                if item.artifact_type == "asset-validation-report"
                and item.content_hash == revision.validation_report_hash
            ]
            if len(validation_rows) != 1:
                raise ArtifactError(
                    "Current V0.7 character validation report is missing or duplicated"
                )
            if (
                request.get("workflow_id") != workflow.id
                or request.get("revision") != task.parameters["revision_number"]
                or request.get("execution_id") != runtime_execution.id
                or request.get("attempt_number") != runtime_execution.attempt_number
                or request.get("specification_sha256") != task.parameters["specification_hash"]
                or request.get("profile_sha256") != _canonical_hash(profile_document)
                or request.get("raw_glb_sha256") != revision.raw_glb_hash
                or request.get("processed_glb_sha256") != revision.processed_glb_hash
                or observation.get("execution_id") != runtime_execution.id
                or observation.get("attempt_number") != runtime_execution.attempt_number
                or observation.get("request_digest") != request.get("request_digest")
                or observation.get("status") != "PASS"
            ):
                raise ArtifactError(
                    "Godot character runtime evidence is not bound to the current revision"
                )
            current_captures = select_review_captures(
                [item for item in current_rows if item.artifact_type == "asset-runtime-capture"],
                runtime_execution.id,
                specification.bound_profile().review_views,
            )
            if len(current_captures) != len(specification.bound_profile().review_views):
                raise ArtifactError("Current Godot character attempt is missing review views")
            for capture in current_captures:
                view = Path(capture.relative_path).stem.removeprefix(f"{runtime_execution.id}-")
                if (
                    observation.get("captures", {}).get(view, {}).get("sha256")
                    != capture.content_hash
                ):
                    raise ArtifactError(
                        f"Godot character capture is not bound to its observation: {view}"
                    )
            runtime_capture_hashes = sorted(item.content_hash for item in current_captures)
            context["runtime_capture_hashes"] = runtime_capture_hashes
            context["artifacts"].update(
                {
                    "asset-concept": active_concept.content_hash,
                    "asset-processed-glb": revision.processed_glb_hash,
                    "asset-validation-report": revision.validation_report_hash,
                    "asset-runtime-observation": observation_row.content_hash,
                    "asset-runtime-capture": runtime_capture_hashes[0],
                }
            )
            context["artifacts"].update(
                {
                    "asset-profile-v07": next(
                        item.content_hash
                        for item in artifact_list
                        if item.artifact_type == "asset-profile-v07"
                    ),
                    "asset-runtime-request": request_row.content_hash,
                    "asset-runtime-harness": harness_row.content_hash,
                }
            )
            context["runtime_request_digest"] = request["request_digest"]
            context["runtime_capture_hashes"] = runtime_capture_hashes
            context["graph_version"] = task.parameters["graph_version"]
        return context

    def final_review(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        self._guard_paid_task("final_review", workflow, task)
        approval = next(
            (
                a
                for a in self.approvals.list_by_workflow(workflow.id)
                if a.task_id == task.id
                and a.approval_type == "final_visual_review"
                and a.status.value == "APPROVED"
            ),
            None,
        )
        if approval is None:
            raise ArtifactError("Final visual approval is missing")
        attempt = execution.attempt_number
        receipt = self._path(task, f"final-approval-a{attempt}.json")
        if receipt.exists():
            relative = receipt.resolve(strict=True).relative_to(self.root).as_posix()
            existing_artifacts = self.artifacts.list_by_workflow(workflow.id)
            if any(a.relative_path == relative for a in existing_artifacts):
                raise ArtifactError(
                    f"Final approval receipt file is already registered: {receipt.name}"
                )
        receipt.write_text(
            json.dumps(
                {
                    "approval_id": approval.id,
                    "approval_type": approval.approval_type,
                    "status": approval.status.value,
                    "fingerprint": approval.operation_hash,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        artifact_id = self._register(
            workflow, task, execution, "asset-final-approval-record", receipt
        )
        return TaskHandlerResult(
            1,
            "Final asset visual approval recorded for the current evidence fingerprint",
            [artifact_id],
        )

    def publish_provider_v07_evidence_bundle(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        """Export, cold-check, then atomically register current provider-character evidence."""
        self._guard_paid_task("provider_v07_evidence", workflow, task)
        from gamefactory.workflows.provider_character_evidence import (
            _snapshot_fingerprint,
            export_current_provider_character_evidence,
        )

        snapshot = self.current_provider_character_evidence_inputs(workflow, task, execution)
        expected_fingerprint = _snapshot_fingerprint(snapshot)
        binding = snapshot["binding"]
        relative_parent = (
            f".gamefactory/assets/{binding['asset_id']}/r{binding['revision']:03d}/"
            "provider-evidence"
        )
        guard = PathGuard(self.root)
        marker_path = guard.ensure_safe_parent(f"{relative_parent}/.attempt-parent")
        parent = marker_path.parent
        if parent.is_symlink() or not parent.is_dir():
            raise ArtifactError("Provider-character evidence parent is not a regular directory")
        attempt_name = f"{execution.id}-a{execution.attempt_number}"
        relative_bundle = f"{relative_parent}/{attempt_name}"
        destination = guard.resolve_safe_path(relative_bundle)
        if destination.exists() or destination.is_symlink():
            raise ArtifactError(
                f"Provider-character attempt evidence path already exists: {relative_bundle}"
            )

        report = export_current_provider_character_evidence(
            self, workflow, task, execution, destination
        )
        if report.get("status") != "PASS" or report.get("product_ready") is not True:
            raise ArtifactError(
                f"Provider-character cold verifier did not pass for {relative_bundle}"
            )
        current_snapshot = self.current_provider_character_evidence_inputs(
            workflow, task, execution
        )
        if _snapshot_fingerprint(current_snapshot) != expected_fingerprint:
            raise ArtifactError(
                f"Provider-character source snapshot changed after cold verification: {relative_bundle}"
            )

        relative_manifest = f"{relative_bundle}/manifest.json"
        manifest_artifact = self.artifact_manager.register_file_artifact(
            workflow.id,
            task.id,
            "provider-character-evidence-manifest",
            "cold_verified_provider_character_evidence",
            relative_manifest,
        )
        self.artifact_manager.verify_artifact_integrity(manifest_artifact)
        publisher = ProviderCharacterEvidencePublicationRepository(self.artifacts.db)
        try:
            publisher.publish_attempt(
                workflow=workflow,
                task=task,
                execution=execution,
                artifact=manifest_artifact,
                expected_snapshot_fingerprint=expected_fingerprint,
                current_snapshot_fingerprint=lambda: _snapshot_fingerprint(
                    self.current_provider_character_evidence_inputs(workflow, task, execution)
                ),
            )
        except (ArtifactError, ValueError, sqlite3.Error) as exc:
            raise ArtifactError(
                f"Provider-character manifest publication failed; immutable candidate retained at "
                f"{relative_bundle}: {exc}"
            ) from exc
        return TaskHandlerResult(
            1,
            "Provider-generated V0.7 character evidence passed both cold verifiers",
            [manifest_artifact.id],
        )

    def current_provider_character_evidence_inputs(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> dict[str, Any]:
        """Select a read-only, fully pinned V0.7 character evidence snapshot.

        This deliberately does not create the evidence bundle or alter workflow state.
        The external exporter still has to pass both cold verifier copies before it
        can publish anything.
        """
        self._guard_paid_task("provider_character_evidence_snapshot", workflow, task)
        if not is_v07_character_graph(task):
            raise ArtifactError("Provider-character evidence requires the exact V0.7 graph")
        expected_evidence_task = self._unique_character_task(
            workflow, "asset_v07_provider_evidence", "evidence export"
        )
        persisted_workflow = WorkflowRepository(self._task_repo.db).get(workflow.id)
        if (
            persisted_workflow is None
            or persisted_workflow.project_id != workflow.project_id
            or persisted_workflow.status != workflow.status
            or persisted_workflow.status not in {WorkflowStatus.RUNNING, WorkflowStatus.COMPLETED}
        ):
            raise ArtifactError(
                "Provider-character evidence requires the current persisted workflow status"
            )
        if (
            task.id != expected_evidence_task.id
            or task.workflow_id != workflow.id
            or execution.task_id != task.id
            or not isinstance(execution.id, str)
            or not execution.id
            or type(execution.attempt_number) is not int
            or execution.attempt_number < 1
        ):
            raise ArtifactError("Provider-character evidence execution identity is stale")
        evidence_attempts = self.executions.list_by_task(task.id)
        latest_evidence_number = max((item.attempt_number for item in evidence_attempts), default=0)
        latest_evidence = [
            item for item in evidence_attempts if item.attempt_number == latest_evidence_number
        ]
        if (
            len(latest_evidence) != 1
            or latest_evidence[0].id != execution.id
            or latest_evidence[0].attempt_number != execution.attempt_number
            or latest_evidence[0].status != execution.status
            or execution.status not in {ExecutionStatus.RUNNING, ExecutionStatus.COMPLETED}
            or (
                execution.status == ExecutionStatus.RUNNING
                and (
                    expected_evidence_task.status != TaskStatus.RUNNING
                    or workflow.status != WorkflowStatus.RUNNING
                )
            )
            or (
                execution.status == ExecutionStatus.COMPLETED
                and (
                    expected_evidence_task.status != TaskStatus.COMPLETED
                    or workflow.status != WorkflowStatus.COMPLETED
                )
            )
        ):
            raise ArtifactError(
                "Provider-character evidence requires a current task/workflow and RUNNING or COMPLETED attempt"
            )

        from gamefactory.core.accounting.ledger import EntryType
        from gamefactory.core.approvals.approval_service import compute_operation_hash
        from gamefactory.core.approvals.operation_scope import build_operation_inputs
        from gamefactory.workflows.provider_character_evidence import (
            ProviderCharacterEvidenceFile,
        )

        specification = self._task_specification(workflow, task)
        assert isinstance(specification, AssetSpecificationV07)
        profile = specification.bound_profile()
        revision_number = task.parameters.get("revision_number")
        if isinstance(revision_number, bool) or not isinstance(revision_number, int):
            raise ArtifactError("Provider-character evidence revision is malformed")
        revision = self.revisions.get(specification.asset_id, revision_number)
        if (
            revision is None
            or revision.workflow_id != workflow.id
            or revision.spec_hash != task.parameters.get("specification_hash")
            or revision.profile_id != profile.profile_id
            or revision.profile_version != profile.version
        ):
            raise ArtifactError("Provider-character evidence revision is stale")

        # Every prerequisite and human gate must be the latest successful attempt.
        prepare_task, _ = self._latest_character_execution(
            workflow, "asset_prepare", "evidence export"
        )
        concept_task, _ = self._latest_character_execution(
            workflow, "asset_concept_review", "evidence export"
        )
        snapshot_task, _ = self._latest_character_execution(
            workflow, "asset_paid_request_snapshot", "evidence export"
        )
        readiness_task, _ = self._latest_character_execution(
            workflow, "asset_production_readiness", "evidence export"
        )
        final_review_task, _ = self._latest_character_execution(
            workflow, "asset_final_review", "evidence export"
        )

        artifact_rows = self.artifacts.list_by_workflow(workflow.id)

        def unique_artifact(
            role: str,
            artifact_type: str,
            *,
            expected_hash: str | None = None,
            expected_task_id: str | None = None,
        ) -> Artifact:
            matches = [
                item
                for item in artifact_rows
                if item.workflow_id == workflow.id
                and item.artifact_type == artifact_type
                and (expected_hash is None or item.content_hash == expected_hash)
                and (expected_task_id is None or item.task_id == expected_task_id)
            ]
            if len(matches) != 1:
                raise ArtifactError(
                    f"Provider-character evidence requires one current {role} artifact"
                )
            self.artifact_manager.verify_artifact_integrity(matches[0])
            return matches[0]

        def evidence_file(
            role: str, artifact: Artifact, *, view: str | None = None
        ) -> ProviderCharacterEvidenceFile:
            return ProviderCharacterEvidenceFile(
                role,
                path=self.root / artifact.relative_path,
                view=view,
                artifact_id=artifact.id,
                source_relative_path=artifact.relative_path,
                expected_sha256=artifact.content_hash,
                expected_size=artifact.file_size,
            )

        spec_artifact = unique_artifact(
            "specification", "asset-specification", expected_task_id=prepare_task.id
        )
        concept = self._active_concept_artifact(workflow.id)
        provenance = self._active_concept_provenance_artifact(workflow.id)
        active_concept_version = self._concept_version_repo.active_for_workflow(workflow.id)
        expected_concept_owner = (
            concept_task.id
            if active_concept_version is not None and active_concept_version.version > 1
            else prepare_task.id
        )
        if (
            concept.task_id != expected_concept_owner
            or provenance.task_id != expected_concept_owner
        ):
            raise ArtifactError("Active provider-character concept artifacts have a stale owner")
        profile_artifact = unique_artifact(
            "bound profile", "asset-profile-v07", expected_task_id=prepare_task.id
        )
        profile_document = _read_bounded_json_document(
            self.root / profile_artifact.relative_path, "Provider-character pinned profile"
        )
        if _canonical_hash(profile_document) != task.parameters.get("profile_document_hash"):
            raise ArtifactError("Provider-character profile artifact differs from its trusted pin")

        paid_task = self._unique_character_task(
            workflow, "asset_paid_generation", "evidence export"
        )
        paid_execution = self._latest_character_execution(
            workflow, "asset_paid_generation", "evidence export"
        )[1]
        paid_execution_rows = self.executions.list_by_task(paid_task.id)
        paid_execution_history: list[dict[str, Any]] = []
        allowed_paid_execution_statuses = {
            ExecutionStatus.UNCERTAIN,
            ExecutionStatus.FAILED,
            ExecutionStatus.COMPLETED,
        }
        if not paid_execution_rows:
            raise ArtifactError("Provider-character paid execution history is empty")
        paid_execution_ids_seen: set[str] = set()
        for index, paid_attempt in enumerate(paid_execution_rows, start=1):
            if (
                paid_attempt.task_id != paid_task.id
                or paid_attempt.attempt_number != index
                or not isinstance(paid_attempt.id, str)
                or not paid_attempt.id
                or paid_attempt.id in paid_execution_ids_seen
                or paid_attempt.status not in allowed_paid_execution_statuses
                or isinstance(paid_attempt.cost, bool)
                or not isinstance(paid_attempt.cost, (int, float))
                or not math.isfinite(float(paid_attempt.cost))
                or paid_attempt.cost < 0
                or not isinstance(paid_attempt.cost_unit, str)
                or not paid_attempt.cost_unit
                or (
                    paid_attempt.provider is not None
                    and (not isinstance(paid_attempt.provider, str) or not paid_attempt.provider)
                )
                or (
                    paid_attempt.external_op_id is not None
                    and (
                        not isinstance(paid_attempt.external_op_id, str)
                        or not paid_attempt.external_op_id
                    )
                )
            ):
                raise ArtifactError(
                    "Provider-character paid execution history is incomplete or malformed"
                )
            paid_execution_ids_seen.add(paid_attempt.id)
            paid_execution_history.append(
                {
                    "id": paid_attempt.id,
                    "attempt_number": paid_attempt.attempt_number,
                    "status": paid_attempt.status.value,
                    "task_id": paid_task.id,
                    "workflow_id": workflow.id,
                    "revision_number": revision_number,
                    "provider": paid_attempt.provider,
                    "external_op_id": paid_attempt.external_op_id,
                    "cost": paid_attempt.cost,
                    "cost_unit": paid_attempt.cost_unit,
                }
            )
        latest_paid_history = paid_execution_history[-1]
        if (
            latest_paid_history["id"] != paid_execution.id
            or latest_paid_history["attempt_number"] != paid_execution.attempt_number
            or latest_paid_history["status"] != ExecutionStatus.COMPLETED.value
            or latest_paid_history["provider"] != paid_execution.provider
            or latest_paid_history["external_op_id"] != paid_execution.external_op_id
            or latest_paid_history["cost"] != paid_execution.cost
            or latest_paid_history["cost_unit"] != paid_execution.cost_unit
        ):
            raise ArtifactError(
                "Provider-character paid execution history does not end at its current COMPLETED attempt"
            )
        raw = self._character_raw_artifact(workflow, "evidence export")
        process_task, process_execution, processed = self._character_stage_artifact(
            workflow,
            "asset_process",
            "asset-processed-glb",
            "evidence export",
            suffix="processed",
        )
        validation_task, validation_execution, validation = self._character_stage_artifact(
            workflow,
            "asset_validate",
            "asset-validation-report",
            "evidence export",
            suffix="validation",
        )
        runtime_task, runtime_execution = self._latest_character_execution(
            workflow, "asset_godot", "evidence export"
        )
        self._character_processed_artifact(workflow, "evidence export")
        self._character_validation_artifact(workflow, "evidence export")
        runtime_context = self.final_review_context(
            workflow,
            final_review_task,
        )

        runtime_prefix = f"/{runtime_execution.id}-a{runtime_execution.attempt_number}/"
        runtime_rows = [
            item
            for item in artifact_rows
            if item.task_id == runtime_task.id and runtime_prefix in f"/{item.relative_path}"
        ]

        def runtime_artifact(artifact_type: str, role: str) -> Artifact:
            matches = [item for item in runtime_rows if item.artifact_type == artifact_type]
            if len(matches) != 1:
                raise ArtifactError(f"Current Godot attempt has no unique {role} artifact")
            self.artifact_manager.verify_artifact_integrity(matches[0])
            return matches[0]

        runtime_request = runtime_artifact("asset-runtime-request", "runtime request")
        runtime_observation = runtime_artifact("asset-runtime-observation", "runtime observation")
        runtime_harness = runtime_artifact("asset-runtime-harness", "runtime harness")
        captures = select_review_captures(
            [item for item in runtime_rows if item.artifact_type == "asset-runtime-capture"],
            runtime_execution.id,
            profile.review_views,
        )
        if len(captures) != len(profile.review_views):
            raise ArtifactError("Current Godot attempt does not contain all trusted review views")

        processing_report = self._character_stage_artifact(
            workflow,
            "asset_process",
            "asset-processing-report",
            "evidence export",
            suffix="processing",
        )[2]
        script_path = Path(
            str(resource_files("gamefactory").joinpath("resources/blender/process_character.py"))
        ).resolve(strict=True)
        script_bytes = script_path.read_bytes()
        script_hash = hashlib.sha256(script_bytes).hexdigest()
        try:
            processing_document = _read_bounded_json_document(
                self.root / processing_report.relative_path, "Current Blender processing report"
            )
        except ArtifactError:
            raise
        if (
            processing_document.get(
                "processing_script_sha256", processing_document.get("script_sha256")
            )
            != script_hash
        ):
            raise ArtifactError("Packaged Blender character script differs from current report")

        active_snapshots = [
            item
            for item in self._snapshot_repo.list_by_workflow(workflow.id)
            if item.status == "ACTIVE"
        ]
        active_readiness_rows = [
            item
            for item in self._readiness_repo.list_by_workflow(workflow.id)
            if item.status == "ACTIVE"
        ]
        snapshot_record = active_snapshots[0] if len(active_snapshots) == 1 else None
        readiness_record = active_readiness_rows[0] if len(active_readiness_rows) == 1 else None
        if (
            snapshot_record is None
            or readiness_record is None
            or readiness_record.result != "PASS"
            or readiness_record.status != "ACTIVE"
            or snapshot_record.workflow_id != workflow.id
            or snapshot_record.task_id != snapshot_task.id
            or snapshot_record.asset_id != specification.asset_id
            or snapshot_record.revision_number != revision_number
            or readiness_record.workflow_id != workflow.id
            or readiness_record.task_id != readiness_task.id
            or readiness_record.snapshot_sha256 != snapshot_record.snapshot_sha256
        ):
            raise ArtifactError("Current paid snapshot or PASS readiness evidence is missing")
        snapshot_artifact = unique_artifact(
            "paid request snapshot",
            "asset-paid-request-snapshot",
            expected_hash=snapshot_record.snapshot_sha256,
            expected_task_id=snapshot_task.id,
        )
        readiness_artifact = unique_artifact(
            "production readiness report",
            "asset-production-readiness-report",
            expected_hash=readiness_record.report_sha256,
            expected_task_id=readiness_task.id,
        )

        intent = self.intents.get_by_task(paid_task.id)
        if (
            intent is None
            or intent.status != "SUCCEEDED"
            or not intent.external_task_id
            or intent.workflow_id != workflow.id
            or intent.revision_number != revision_number
            or intent.asset_id != specification.asset_id
            or not isinstance(intent.provider, str)
            or not intent.provider
            or paid_execution.provider != intent.provider
            or paid_execution.cost_unit != intent.cost_unit
            or intent.operation != "image-to-3d"
            or intent.concept_hash != concept.content_hash
            or intent.paid_request_snapshot_hash != snapshot_record.snapshot_sha256
            or intent.request_fingerprint != snapshot_record.snapshot_sha256
            or intent.external_task_id != paid_execution.external_op_id
            or intent.approval_id == ""
        ):
            raise ArtifactError(
                "Provider operation is not terminal and pinned to the current snapshot"
            )
        if any(
            (item["provider"] is not None and item["provider"] != intent.provider)
            or (
                item["external_op_id"] is not None
                and item["external_op_id"] != intent.external_task_id
            )
            for item in paid_execution_history
        ):
            raise ArtifactError(
                "Provider-character paid execution history contains a foreign provider or operation ID"
            )
        ledger_repo = self.ledger
        if ledger_repo is None and self.accounting is not None:
            ledger_repo = self.accounting.ledger_repo
        if ledger_repo is None:
            raise ArtifactError("Provider-character evidence has no cost ledger repository")
        ledger_entries = ledger_repo.list_by_task(paid_task.id)
        ledger_slice_sha256 = hashlib.sha256(
            _canonical_json([entry.to_dict() for entry in ledger_entries]).encode()
        ).hexdigest()
        settlements = [entry for entry in ledger_entries if entry.entry_type == EntryType.SETTLE]
        actual_cost = intent.actual_cost
        if (
            len(settlements) != 1
            or isinstance(actual_cost, bool)
            or not isinstance(actual_cost, (int, float))
        ):
            raise ArtifactError("Provider operation does not have one terminal settled ledger row")
        settled_cost = float(actual_cost)
        if not math.isfinite(settled_cost) or settled_cost < 0:
            raise ArtifactError("Provider operation does not have one terminal settled ledger row")
        settlement = settlements[0]
        account = ledger_repo.operation_account(paid_task.id)
        ledger_consistency_failures = []
        if settlement.intent_id != intent.id:
            ledger_consistency_failures.append("settlement intent")
        if settlement.execution_id != paid_execution.id:
            ledger_consistency_failures.append("settlement execution")
        if settlement.request_fingerprint != intent.request_fingerprint:
            ledger_consistency_failures.append("settlement fingerprint")
        if settlement.workflow_id != workflow.id or settlement.project_id != workflow.project_id:
            ledger_consistency_failures.append("settlement ownership")
        if settlement.cost_unit != intent.cost_unit:
            ledger_consistency_failures.append("settlement unit")
        paid_execution_ids = {row["id"] for row in paid_execution_history}
        if any(
            entry.task_id != paid_task.id
            or entry.workflow_id != workflow.id
            or entry.project_id != workflow.project_id
            or entry.cost_unit != intent.cost_unit
            or entry.entry_type not in {EntryType.RESERVE, EntryType.SETTLE, EntryType.RELEASE}
            or (
                entry.entry_type == EntryType.RESERVE
                and not (
                    (
                        entry.intent_id is None
                        and entry.execution_id is None
                        and entry.request_fingerprint is None
                    )
                    or (
                        entry.intent_id is None
                        and entry.execution_id in paid_execution_ids
                        and entry.request_fingerprint is None
                    )
                )
            )
            or (
                entry.entry_type in {EntryType.SETTLE, EntryType.RELEASE}
                and (
                    entry.intent_id != intent.id
                    or entry.execution_id != paid_execution.id
                    or entry.request_fingerprint != intent.request_fingerprint
                )
            )
            for entry in ledger_entries
        ):
            ledger_consistency_failures.append("ledger row metadata or adjustment")
        if account.settled_total != settled_cost or settlement.amount != settled_cost:
            ledger_consistency_failures.append("settlement amount")
        if account.held != 0:
            ledger_consistency_failures.append("held reservation")
        if account.net != settled_cost:
            ledger_consistency_failures.append("account net")
        if account.cost_unit != intent.cost_unit:
            ledger_consistency_failures.append("account unit")
        if paid_execution.cost != settled_cost:
            ledger_consistency_failures.append("execution cost")
        if ledger_consistency_failures:
            raise ArtifactError(
                "Provider-character ledger history is conflicting, unsettled, or inconsistent: "
                + ", ".join(ledger_consistency_failures)
            )

        final_task = self._unique_character_task(workflow, "asset_final_review", "evidence export")
        approval_rows = self.approvals.list_by_workflow(workflow.id)

        def selected_approval(
            task_row: Task, approval_type: str, context: dict[str, Any] | None
        ) -> Any:
            candidates = [
                item
                for item in approval_rows
                if item.task_id == task_row.id
                and item.approval_type == approval_type
                and item.status.value == "APPROVED"
            ]
            matched = []
            for item in candidates:
                approved_artifacts = [
                    artifact for artifact in artifact_rows if artifact.id in item.artifact_ids
                ]
                inputs = build_operation_inputs(
                    workflow,
                    task_row,
                    approved_artifacts,
                    item.cost_class,
                    handler_context=context,
                )
                if (
                    compute_operation_hash(task_row.id, approval_type, inputs)
                    == item.operation_hash
                ):
                    matched.append((item, inputs))
            if len(matched) != 1:
                raise ArtifactError(
                    f"Provider-character evidence has no unique current {approval_type} approval"
                )
            return matched[0]

        concept_task = self._unique_character_task(
            workflow, "asset_concept_review", "evidence export"
        )
        paid_review_task = paid_task
        concept_approval, concept_inputs = selected_approval(
            concept_task,
            "concept_review",
            self.concept_review_context(workflow, concept_task),
        )
        paid_approval, paid_inputs = selected_approval(
            paid_review_task,
            "paid_generation",
            None,
        )
        final_approval, final_inputs = selected_approval(
            final_task, "final_visual_review", runtime_context
        )
        if (
            paid_approval.paid_request_snapshot_hash != snapshot_record.snapshot_sha256
            or intent.approval_id != paid_approval.id
        ):
            raise ArtifactError("Paid approval differs from the current provider operation")

        def receipt(approval: Any, inputs: dict[str, Any]) -> dict[str, Any]:
            return {
                "id": approval.id,
                "workflow_id": workflow.id,
                "revision": revision_number,
                "task_id": approval.task_id,
                "approval_type": approval.approval_type,
                "status": approval.status.value,
                "inputs": inputs,
                "operation_hash": approval.operation_hash,
                "fingerprint": approval.operation_hash,
                "actor": approval.actor,
                "reason": approval.reason,
                "comment": approval.comment,
                "decided_at": approval.decided_at,
                "paid_request_snapshot_hash": approval.paid_request_snapshot_hash,
                "concept_sha256": (
                    concept.content_hash if approval.approval_type == "concept_review" else None
                ),
            }

        def attempt_history(task_type: str) -> list[dict[str, Any]]:
            stage_task = self._unique_character_task(workflow, task_type, "evidence export")
            return [
                {
                    "id": item.id,
                    "attempt_number": item.attempt_number,
                    "status": item.status.value,
                }
                for item in self.executions.list_by_task(stage_task.id)
            ]

        def record_file(
            role: str, artifact: Artifact, *, view: str | None = None
        ) -> ProviderCharacterEvidenceFile:
            return evidence_file(role, artifact, view=view)

        files: list[Any] = [
            record_file("specification", spec_artifact),
            record_file("concept", concept),
            record_file("concept_provenance", provenance),
            record_file("paid_request_snapshot", snapshot_artifact),
            record_file("production_readiness_report", readiness_artifact),
            record_file("provider_generated_glb", raw),
            record_file("processed_glb", processed),
            record_file("processing_report", processing_report),
            ProviderCharacterEvidenceFile("processing_script", data=script_bytes),
            record_file("validation", validation),
            record_file("runtime_request", runtime_request),
            record_file("runtime_observation", runtime_observation),
            record_file("runtime_harness", runtime_harness),
            record_file("bound_profile", profile_artifact),
        ]
        files.extend(
            record_file(
                "runtime_capture",
                item,
                view=Path(item.relative_path).stem.removeprefix(f"{runtime_execution.id}-"),
            )
            for item in captures
        )
        provider_operation = {
            **intent.to_dict(),
            "raw_glb_sha256": raw.content_hash,
            "paid_execution_history": paid_execution_history,
            "execution_id": paid_execution.id,
            "attempt_number": paid_execution.attempt_number,
            "paid_execution_external_id": paid_execution.external_op_id,
            "paid_execution_status": paid_execution.status.value,
            "paid_execution_cost": paid_execution.cost,
            "paid_execution_provider": paid_execution.provider,
        }
        files.extend(
            (
                ProviderCharacterEvidenceFile(
                    "provider_operation",
                    data=(
                        json.dumps(provider_operation, sort_keys=True, separators=(",", ":")) + "\n"
                    ).encode(),
                ),
                ProviderCharacterEvidenceFile(
                    "cost_record",
                    data=(
                        json.dumps(
                            {
                                "schema_version": "provider-character-cost-record-0.7.0",
                                "entries": [entry.to_dict() for entry in ledger_entries],
                                "operation_account": account.to_dict(),
                                "row_count": len(ledger_entries),
                                "slice_sha256": ledger_slice_sha256,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode(),
                ),
            )
        )
        capture_hashes = {
            Path(item.relative_path).stem.removeprefix(
                f"{runtime_execution.id}-"
            ): item.content_hash
            for item in captures
        }
        binding = {
            "workflow_id": workflow.id,
            "revision": revision_number,
            "source_version": revision_number,
            "asset_id": specification.asset_id,
            "spec_sha256": task.parameters["specification_hash"],
            "profile_id": profile.profile_id,
            "profile_version": profile.version,
            "profile_document_sha256": task.parameters["profile_document_hash"],
            "review_views": list(profile.review_views),
            "concept_sha256": concept.content_hash,
            "paid_request_snapshot_sha256": snapshot_record.snapshot_sha256,
            "production_readiness_report_sha256": readiness_artifact.content_hash,
            "provider_operation_sha256": hashlib.sha256(files[-2].data).hexdigest(),
            "cost_record_sha256": hashlib.sha256(files[-1].data).hexdigest(),
            "cost_ledger_row_count": len(ledger_entries),
            "cost_ledger_slice_sha256": ledger_slice_sha256,
            "raw_glb_sha256": raw.content_hash,
            "processed_glb_sha256": processed.content_hash,
            "processing_execution_id": process_execution.id,
            "processing_attempt_number": process_execution.attempt_number,
            "processing_report_sha256": processing_report.content_hash,
            "processing_script_sha256": script_hash,
            "validation_execution_id": validation_execution.id,
            "validation_attempt_number": validation_execution.attempt_number,
            "validation_sha256": validation.content_hash,
            "runtime_execution_id": runtime_execution.id,
            "runtime_attempt_number": runtime_execution.attempt_number,
            "runtime_request_digest": runtime_context["runtime_request_digest"],
            "runtime_observation_sha256": runtime_observation.content_hash,
            "runtime_harness_sha256": runtime_harness.content_hash,
            "capture_sha256": capture_hashes,
        }
        latest_ledger_entries = ledger_repo.list_by_task(paid_task.id)
        latest_ledger_slice_sha256 = hashlib.sha256(
            _canonical_json([entry.to_dict() for entry in latest_ledger_entries]).encode()
        ).hexdigest()
        latest_account = ledger_repo.operation_account(paid_task.id)
        if (
            len(latest_ledger_entries) != len(ledger_entries)
            or latest_ledger_slice_sha256 != ledger_slice_sha256
            or latest_account.to_dict() != account.to_dict()
        ):
            raise ArtifactError("Provider-character cost ledger changed during evidence selection")
        return {
            "files": files,
            "binding": binding,
            "concept_review_receipt": receipt(concept_approval, concept_inputs),
            "paid_review_receipt": receipt(paid_approval, paid_inputs),
            "final_review_receipt": receipt(final_approval, final_inputs),
            "attempt_history": {
                "process": attempt_history("asset_process"),
                "validate": attempt_history("asset_validate"),
                "godot": attempt_history("asset_godot"),
            },
        }


def register_asset_production_handlers(
    registry: TaskHandlerRegistry,
    handlers: AssetProductionHandlers,
) -> None:
    """Register the domain stages; engine metadata owns approval/recovery semantics."""
    registry.register("asset_prepare", handlers.prepare)
    registry.register(
        "asset_concept_review",
        handlers.review_concept,
        TaskHandlerMetadata(
            operation=HandlerOperation.LOCAL_READ,
            mandatory_approval_type="concept_review",
            approval_context=handlers.concept_review_context,
            changes_requested_blocks=is_immutable_paid_graph,
        ),
    )
    registry.register("asset_paid_request_snapshot", handlers.paid_request_snapshot)
    registry.register("asset_production_readiness", handlers.production_readiness)
    registry.register(
        "asset_paid_generation",
        handlers.paid_generate,
        TaskHandlerMetadata(
            operation=HandlerOperation.LOCAL_READ,
            recovery=HandlerRecovery.CONSERVATIVE_PROCESS,
            safe_paid_recovery=True,
            recovery_check=handlers.recovery_check,
            mandatory_approval_type="paid_generation",
            refresh_parameters=handlers.bind_paid_request_parameters,
        ),
    )
    registry.register(
        "asset_process",
        handlers.process,
        TaskHandlerMetadata(
            operation=HandlerOperation.PROCESS_EXECUTION,
            recovery=HandlerRecovery.CONSERVATIVE_PROCESS,
        ),
    )
    registry.register("asset_validate", handlers.validate)
    registry.register(
        "asset_godot",
        handlers.godot,
        TaskHandlerMetadata(
            operation=HandlerOperation.PROCESS_EXECUTION,
            recovery=HandlerRecovery.CONSERVATIVE_PROCESS,
        ),
    )
    registry.register(
        "asset_final_review",
        handlers.final_review,
        TaskHandlerMetadata(
            operation=HandlerOperation.LOCAL_READ,
            mandatory_approval_type="final_visual_review",
            approval_context=handlers.final_review_context,
        ),
    )
    registry.register("asset_v07_provider_evidence", handlers.publish_provider_v07_evidence_bundle)


def bound_profile_id(task: Task) -> str:
    spec = (
        _parse_task_specification(task.parameters, _embedded_profile_registry(task.parameters))
        if is_v07_character_graph(task)
        else parse_asset_specification(task.parameters["specification"])
    )
    return str(spec.bound_profile().profile_id)


def bound_profile_version(task: Task) -> int:
    spec = (
        _parse_task_specification(task.parameters, _embedded_profile_registry(task.parameters))
        if is_v07_character_graph(task)
        else parse_asset_specification(task.parameters["specification"])
    )
    return int(spec.bound_profile().version)


def bound_profile_qualified(task: Task) -> str:
    spec = (
        _parse_task_specification(task.parameters, _embedded_profile_registry(task.parameters))
        if is_v07_character_graph(task)
        else parse_asset_specification(task.parameters["specification"])
    )
    return str(spec.bound_profile().qualified)


REVIEW_CAPTURE_ANGLES = ("front", "three_quarter", "side")
SIDE_HEIGHT_MIN = 0.55
SIDE_HEIGHT_MAX = 0.75


def capture_belongs_to_execution(relative_path: str, execution_id: str) -> bool:
    """True when a capture path is bound to this execution attempt."""
    if not execution_id:
        return False
    normalized = relative_path.replace("\\", "/")
    filename = normalized.rsplit("/", 1)[-1]
    if filename.startswith(f"{execution_id}-") or filename.startswith(f"{execution_id}."):
        return True
    return f"-{execution_id}/" in normalized or f"/{execution_id}/" in normalized


def select_review_captures(
    captures: list[Any],
    execution_id: str,
    angles: tuple[str, ...] | None = None,
) -> list[Any]:
    """Select one capture per required view from the bound runtime attempt.

    A later side-only recapture replaces the review side view when side is required.
    The earlier side file remains registered and is not selected.
    """
    required = angles or REVIEW_CAPTURE_ANGLES
    by_angle: dict[str, list[Any]] = {angle: [] for angle in required}
    for artifact in captures:
        angle = Path(artifact.relative_path).stem
        if angle.startswith(f"{execution_id}-"):
            angle = angle[len(execution_id) + 1 :]
        if angle in by_angle:
            by_angle[angle].append(artifact)
    selected: list[Any] = []
    for angle in required:
        owned = [
            artifact
            for artifact in by_angle[angle]
            if capture_belongs_to_execution(artifact.relative_path, execution_id)
        ]
        if angle == "side":
            corrections = [
                artifact
                for artifact in by_angle[angle]
                if not capture_belongs_to_execution(artifact.relative_path, execution_id)
            ]
            if corrections:
                selected.append(
                    max(corrections, key=lambda artifact: (artifact.created_at, artifact.id))
                )
                continue
        if len(owned) != 1:
            raise ArtifactError(
                f"Final review requires one {angle} capture from the bound runtime attempt"
            )
        selected.append(owned[0])
    return selected


def validate_view_framing(
    framing: Any,
    *,
    view: str,
    minimum: float,
    maximum: float,
) -> None:
    """Reject a capture whose projected bounds miss the profile framing contract."""
    if not isinstance(framing, dict):
        raise RuntimeValidationFailedError(f"{view} capture has no framing measurement")
    height = framing.get("height_ratio")
    if (
        isinstance(height, bool)
        or not isinstance(height, (int, float))
        or not math.isfinite(float(height))
        or float(height) < minimum
        or float(height) > maximum
    ):
        raise RuntimeValidationFailedError(
            f"{view} capture height fraction is outside {minimum}-{maximum}"
        )
    if framing.get("inside_viewport") is not True or framing.get("margin_ok") is not True:
        raise RuntimeValidationFailedError(
            f"{view} capture bounds are outside the viewport safety margin"
        )
    if framing.get("horizontally_centered") is not True:
        raise RuntimeValidationFailedError(f"{view} capture is not horizontally centered")
    if framing.get("reference_between_camera_and_asset") is not False:
        raise RuntimeValidationFailedError(
            f"{view} capture reference object occludes the production asset"
        )
    if view == "side" and framing.get("view_axis") != "+X":
        raise RuntimeValidationFailedError("side capture is not a principal-side view")


def validate_side_framing(framing: Any) -> None:
    """Reject a side capture outside the static_prop viewport contract."""
    validate_view_framing(framing, view="side", minimum=SIDE_HEIGHT_MIN, maximum=SIDE_HEIGHT_MAX)


def run_asset_in_godot(
    root: Path,
    artifacts: ArtifactRepository,
    artifact_manager: ArtifactManager,
    workflow: Workflow,
    task: Task,
    execution: Execution,
    godot_path: str | None,
    runner: ProcessRunner | None,
    *,
    angles: tuple[str, ...] | None = None,
    observation_artifact_type: str = "asset-runtime-observation",
) -> list[str]:
    """Stage a validated GLB, invoke the real Godot renderer, then independently inspect outputs."""
    from gamefactory.core.domain.asset_profiles import render_scene_contract
    from gamefactory.core.domain.camera_framing import PLACED_VIEWS

    if observation_artifact_type not in {"asset-runtime-observation", "asset-side-correction"}:
        raise ValidationError("Unsupported runtime observation artifact type")
    if not godot_path:
        raise ToolUnavailableError(
            "Godot executable is required for asset runtime verification",
            tool="godot",
            reason="executable_missing",
            configured_path=None,
            task_id=task.id,
        )
    candidate_path = Path(godot_path)
    if not candidate_path.exists():
        raise ToolUnavailableError(
            f"Configured Godot executable does not exist: {godot_path}",
            tool="godot",
            reason="executable_not_found",
            configured_path=godot_path,
            task_id=task.id,
        )
    if not candidate_path.is_file():
        raise ToolUnavailableError(
            f"Configured Godot path is not a regular file: {godot_path}",
            tool="godot",
            reason="executable_not_file",
            configured_path=godot_path,
            task_id=task.id,
        )
    if not os.access(candidate_path, os.X_OK):
        raise ToolUnavailableError(
            f"Configured Godot executable is not executable: {godot_path}",
            tool="godot",
            reason="executable_not_executable",
            configured_path=godot_path,
            task_id=task.id,
        )
    processed = next(
        (
            a
            for a in artifacts.list_by_workflow(workflow.id)
            if a.artifact_type == "asset-processed-glb"
        ),
        None,
    )
    validation = next(
        (
            a
            for a in artifacts.list_by_workflow(workflow.id)
            if a.artifact_type == "asset-validation-report"
        ),
        None,
    )
    if processed is None or validation is None:
        raise ArtifactError("Godot stage requires processed asset and validation report artifacts")
    artifact_manager.verify_artifact_integrity(processed)
    report = json.loads((root / validation.relative_path).read_text(encoding="utf-8"))
    if report.get("status") != "PASS":
        raise ValidationError(
            "Godot stage is blocked because the independent asset validator did not pass"
        )
    spec = parse_asset_specification(task.parameters["specification"])
    profile = spec.bound_profile()
    capture_angles = profile.review_views if angles is None else angles
    if (
        not capture_angles
        or any(not isinstance(angle, str) for angle in capture_angles)
        or len(capture_angles) != len(set(capture_angles))
        or any(angle not in PLACED_VIEWS for angle in capture_angles)
    ):
        raise ValidationError("Asset capture angles are not an implemented review view set")
    scratch = assert_managed_directory(root, ".gamefactory/scratch")
    relative_stage = f".gamefactory/scratch/asset-{workflow.id}-{execution.id}"
    stage = PathGuard(root).resolve_safe_path(relative_stage)
    if not stage.is_relative_to(scratch.resolve()):
        raise ValidationError("Godot stage escapes the managed scratch directory")
    if stage.exists():
        raise ValidationError("Refusing to overwrite an existing Godot attempt stage")
    stage.mkdir(parents=True)
    (stage / "assets").mkdir()
    shutil.copyfile(root / processed.relative_path, stage / "assets" / "asset.glb")
    resources = Path(__file__).parents[1] / "resources" / "godot" / "asset_runtime_harness.gd"
    shutil.copyfile(resources, stage / "asset_runtime_harness.gd")
    (stage / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="GameFactory Asset Stage"\n[display]\nwindow/size/viewport_width=1280\nwindow/size/viewport_height=720\n[rendering]\nrenderer/rendering_method="gl_compatibility"\nrenderer/rendering_method.mobile="gl_compatibility"\n',
        encoding="utf-8",
    )
    observation_path = stage / "observation.json"
    capture_dir = stage / "captures"
    capture_dir.mkdir()
    request = {
        "workflow_id": workflow.id,
        "revision": task.parameters["revision_number"],
        "asset_id": spec.asset_id,
        "execution_id": execution.id,
        "attempt_number": execution.attempt_number,
        "glb": "res://assets/asset.glb",
        "processed_glb_sha256": processed.content_hash,
        "output_dir": str(capture_dir),
        "observation_path": str(observation_path),
        "angles": list(capture_angles),
        "profile": profile.capture_request_profile(),
    }
    (stage / "asset_wrapper.tscn").write_text(
        render_scene_contract(profile.scene_contract()),
        encoding="utf-8",
    )
    request_path = stage / "request.json"
    request_path.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
    proc = runner or ProcessRunner(sanitize_output=True)
    render_env: dict[str, str] = {}
    if sys.platform.startswith("linux"):
        display = os.environ.get("DISPLAY")
        if not display:
            raise ValidationError(
                "Linux asset capture requires DISPLAY; headless fallback is not a capture"
            )
        render_env["DISPLAY"] = display
        for name in ("XAUTHORITY", "LIBGL_ALWAYS_SOFTWARE"):
            if value := os.environ.get(name):
                render_env[name] = value
    importer = proc.run(
        CommandRequest(
            [godot_path, "--headless", "--path", str(stage), "--editor", "--import", "--quit"],
            stage,
            env_overrides=render_env,
            timeout_seconds=90,
        )
    )
    import_diagnostics = importer.stdout + "\n" + importer.stderr
    import_errors = [p.pattern for p in _ENGINE_ERROR_PATTERNS if p.search(import_diagnostics)]
    import_log = stage / "godot-import.log"
    import_log.write_text(import_diagnostics, encoding="utf-8")
    import_artifact = artifact_manager.register_file_artifact(
        workflow.id,
        task.id,
        "asset-godot-import-log",
        task.task_type,
        import_log.relative_to(root).as_posix(),
    )
    artifacts.save(import_artifact)
    if importer.exit_code != 0 or importer.timed_out or import_errors:
        raise EngineImportFailedError(
            f"Godot GLB import failed (exit={importer.exit_code}, diagnostics={import_errors})"
        )
    render = proc.run(
        CommandRequest(
            [
                godot_path,
                "--path",
                str(stage),
                "--script",
                "res://asset_runtime_harness.gd",
                "--",
                "--request",
                str(request_path),
            ],
            stage,
            env_overrides=render_env,
            timeout_seconds=90,
        )
    )
    render_diagnostics = render.stdout + "\n" + render.stderr
    render_errors = [p.pattern for p in _ENGINE_ERROR_PATTERNS if p.search(render_diagnostics)]
    render_log = stage / "godot-render.log"
    render_log.write_text(render_diagnostics, encoding="utf-8")
    render_artifact = artifact_manager.register_file_artifact(
        workflow.id,
        task.id,
        "asset-godot-render-log",
        task.task_type,
        render_log.relative_to(root).as_posix(),
    )
    artifacts.save(render_artifact)
    if render.exit_code != 0 or render.timed_out or render_errors or not observation_path.is_file():
        raise RuntimeValidationFailedError(
            f"Godot runtime/render verification failed (exit={render.exit_code}, diagnostics={render_errors})"
        )
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    if (
        observation.get("workflow_id") != workflow.id
        or observation.get("revision") != task.parameters["revision_number"]
        or observation.get("asset_id") != spec.asset_id
        or observation.get("execution_id") != execution.id
        or observation.get("attempt_number") != execution.attempt_number
        or observation.get("processed_glb_sha256") != processed.content_hash
    ):
        raise RuntimeValidationFailedError(
            "Godot runtime observation is bound to a different workflow, revision, or GLB"
        )
    requirements = profile.runtime_requirements()
    if (
        observation.get("status") != "PASS"
        or not observation.get("mesh_visible")
        or not observation.get("collision_shape_present")
        or observation.get("errors") != []
        or (
            requirements["require_physics_body"]
            and observation.get("physics_body_present") is not True
        )
        or (requirements["require_area"] and observation.get("area_present") is not True)
        or (requirements["require_ray_hit"] and observation.get("physics_ray_hit") is not True)
    ):
        raise RuntimeValidationFailedError(
            "Independent Godot observation failed mesh, collision, or error checks"
        )
    bounds = observation.get("mesh_bounds", {}).get("size", [])
    expected = spec.dimensions.model_dump()
    if len(bounds) != 3 or any(
        abs(float(actual) - float(target))
        > max(
            float(requirements["bounds_tolerance_floor_m"]),
            float(target) * float(requirements["bounds_tolerance_ratio"]),
        )
        for actual, target in zip(
            bounds,
            (expected["width_m"], expected["height_m"], expected["depth_m"]),
            strict=True,
        )
    ):
        raise RuntimeValidationFailedError(
            "Godot transformed mesh bounds differ materially from the specification"
        )
    view_framing = observation.get("view_framing")
    if not isinstance(view_framing, dict):
        view_framing = {}
    framing_policy = profile.framing
    for angle in capture_angles:
        measured = view_framing.get(angle)
        if angle == "side" and measured is None:
            measured = observation.get("side_framing")
        validate_view_framing(
            measured,
            view=angle,
            minimum=framing_policy.min_screen_fraction,
            maximum=framing_policy.max_screen_fraction,
        )
    output_ids: list[str] = []
    observation_relative = observation_path.relative_to(root).as_posix()
    obs_art = artifact_manager.register_file_artifact(
        workflow.id, task.id, observation_artifact_type, task.task_type, observation_relative
    )
    artifacts.save(obs_art)
    output_ids.append(obs_art.id)
    for angle in capture_angles:
        image = capture_dir / f"{angle}.png"
        from gamefactory.adapters.engines.godot_image import decode_png

        decode_png(image.read_bytes(), 1280, 720)
        relative = image.relative_to(root).as_posix()
        artifact = artifact_manager.register_file_artifact(
            workflow.id, task.id, "asset-runtime-capture", task.task_type, relative
        )
        artifacts.save(artifact)
        output_ids.append(artifact.id)
    return output_ids


def recapture_side_evidence(
    root: Path,
    db: Database,
    workflow_id: str,
    *,
    godot_path: str,
    expected_asset_id: str,
    expected_revision_number: int,
    expected_processed_sha256: str,
    expected_external_task_id: str,
    expected_paid_invocations: int,
    expected_approval_id: str,
) -> dict[str, Any]:
    """Render a new side view of the current processed GLB without a new provider call."""
    project = Path(root).resolve(strict=True)
    workflow = WorkflowRepository(db).get(workflow_id)
    if workflow is None:
        raise ValidationError(f"Workflow not found: {workflow_id}")
    tasks = TaskRepository(db).list_by_workflow(workflow_id)
    godot_task = next((task for task in tasks if task.task_type == "asset_godot"), None)
    review_task = next((task for task in tasks if task.task_type == "asset_final_review"), None)
    if godot_task is None or review_task is None:
        raise ValidationError("Asset workflow is missing Godot or final-review tasks")
    if str(godot_task.parameters.get("asset_id")) != expected_asset_id:
        raise ValidationError("Refusing side recapture: asset id does not match")
    if int(godot_task.parameters.get("revision_number", -1)) != expected_revision_number:
        raise ValidationError("Refusing side recapture: revision does not match")
    artifacts = ArtifactRepository(db)
    artifact_manager = ArtifactManager(project)
    processed = next(
        (
            artifact
            for artifact in artifacts.list_by_workflow(workflow_id)
            if artifact.artifact_type == "asset-processed-glb"
        ),
        None,
    )
    if processed is None or processed.content_hash != expected_processed_sha256:
        raise ValidationError("Refusing side recapture: processed GLB hash does not match")
    artifact_manager.verify_artifact_integrity(processed)
    intents = ProviderOperationIntentRepository(db).list_by_workflow(workflow_id)
    external_ids = [intent.external_task_id for intent in intents if intent.external_task_id]
    paid_count = ProviderInvocationRepository(db).count(workflow_id) + len(external_ids)
    if paid_count != expected_paid_invocations or external_ids != [expected_external_task_id]:
        raise ValidationError("Refusing side recapture: paid provider identity does not match")
    approval = ApprovalRepository(db).get(expected_approval_id)
    if (
        approval is None
        or approval.workflow_id != workflow_id
        or approval.task_id != review_task.id
        or approval.approval_type != "final_visual_review"
        or approval.status.value != "PENDING"
    ):
        raise ValidationError(
            "Refusing side recapture: final review is not the expected pending approval"
        )
    preserved = [
        artifact
        for artifact in artifacts.list_by_workflow(workflow_id)
        if artifact.artifact_type == "asset-runtime-capture"
    ]
    preserved_hashes = {artifact.id: artifact.content_hash for artifact in preserved}
    executions = ExecutionRepository(db)
    attempt_number = (
        max((item.attempt_number for item in executions.list_by_task(godot_task.id)), default=0) + 1
    )
    execution = Execution(
        id=generate_id("EXEC"),
        task_id=godot_task.id,
        attempt_number=attempt_number,
        status=ExecutionStatus.RUNNING,
        cost=0.0,
        estimated_cost=0.0,
        provider=None,
    )
    executions.save(execution)
    try:
        output_ids = run_asset_in_godot(
            project,
            artifacts,
            artifact_manager,
            workflow,
            godot_task,
            execution,
            godot_path,
            None,
            angles=("side",),
            observation_artifact_type="asset-side-correction",
        )
    except Exception:
        execution.status = ExecutionStatus.FAILED
        execution.completed_at = utc_now_iso()
        execution.retryable = False
        executions.save(execution)
        raise
    for artifact_id, content_hash in preserved_hashes.items():
        current = artifacts.get(artifact_id)
        if current is None or current.content_hash != content_hash:
            execution.status = ExecutionStatus.FAILED
            execution.completed_at = utc_now_iso()
            execution.retryable = False
            executions.save(execution)
            raise ValidationError("Side recapture changed a previously registered capture")
        artifact_manager.verify_artifact_integrity(current)
    execution.status = ExecutionStatus.COMPLETED
    execution.completed_at = utc_now_iso()
    execution.exit_code = 0
    execution.cost = 0.0
    execution.stdout = (
        "Corrected side capture rendered from the existing processed GLB\n"
        "provider generation submitted = false\n"
    )
    executions.save(execution)
    registered = [artifacts.get(artifact_id) for artifact_id in output_ids]
    side = next(
        artifact
        for artifact in registered
        if artifact is not None and artifact.artifact_type == "asset-runtime-capture"
    )
    correction = next(
        artifact
        for artifact in registered
        if artifact is not None and artifact.artifact_type == "asset-side-correction"
    )
    paid_after = ProviderInvocationRepository(db).count(workflow_id) + len(
        [
            intent.external_task_id
            for intent in ProviderOperationIntentRepository(db).list_by_workflow(workflow_id)
            if intent.external_task_id
        ]
    )
    if paid_after != expected_paid_invocations:
        raise ValidationError("Side recapture changed the paid invocation count")
    return {
        "execution_id": execution.id,
        "attempt_number": execution.attempt_number,
        "task_id": godot_task.id,
        "side_artifact_id": side.id,
        "side_relative_path": side.relative_path,
        "side_sha256": side.content_hash,
        "correction_artifact_id": correction.id,
        "correction_relative_path": correction.relative_path,
        "processed_glb_sha256": processed.content_hash,
        "paid_invocations": paid_after,
    }
