"""Local, human-gated V0.7 authored-assembly workflow.

This module deliberately composes the existing WorkflowEngine and repositories.
It does not create provider operations, paid snapshots, readiness records, costs,
or a second lifecycle. The last evidence task publishes only a bundle that
passes the separate cold verifier; completed bundles are revalidated read-only.
"""

from __future__ import annotations

import hashlib
import io
import json
import weakref
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from PIL import Image, UnidentifiedImageError

from gamefactory.adapters.assets.assembly_ingest import verify_retained_assembly
from gamefactory.adapters.assets.v07_geometry_validation import (
    VerifiedSourceNormalization,
    selected_v07_groups,
    validate_glb_v07,
    verify_source_to_processed_preservation,
)
from gamefactory.adapters.dcc.assembly_processor import AssemblyProcessor
from gamefactory.adapters.dcc.godot_assembly import AssemblyRuntimeResult, verify_godot_assembly
from gamefactory.adapters.persistence.assembly_graph import AssemblyGraphRepository
from gamefactory.adapters.persistence.assembly_publication import AssemblyPublicationRepository
from gamefactory.adapters.persistence.repositories import AssetRevisionRepository
from gamefactory.core.approvals.approval_service import compute_operation_hash
from gamefactory.core.artifacts.artifact_manager import compute_sha256
from gamefactory.core.domain.assembly_source import AssemblyIngestResult, AssemblyPublicationMarker
from gamefactory.core.domain.asset_contracts import (
    AssetRevision,
    AssetSpecificationV07,
    AssetValidationResult,
    spec_fingerprint,
)
from gamefactory.core.domain.asset_profiles import AssetProfileV07
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import (
    ApprovalStatus,
    Artifact,
    CostClass,
    Execution,
    ExecutionStatus,
    Task,
    TaskStatus,
    Workflow,
    WorkflowStatus,
    generate_id,
)
from gamefactory.core.execution.path_guard import PathGuard
from gamefactory.workflows.handlers import (
    HandlerOperation,
    HandlerRecovery,
    TaskHandler,
    TaskHandlerMetadata,
    TaskHandlerRegistry,
    TaskHandlerResult,
)

if TYPE_CHECKING:
    from gamefactory.workflows.engine import WorkflowEngine

_GRAPH_VERSION = "0.7.0-local-assembly"
_MAX_CONCEPT_BYTES = 4 * 1024 * 1024
_MAX_CONCEPT_PIXELS = 16 * 1024 * 1024
_MAX_CONCEPT_PROVENANCE_BYTES = 1024 * 1024
_MAX_SOURCE_GLB_BYTES = 50 * 1024 * 1024
_MAX_JSON_ARTIFACT_BYTES = 1024 * 1024
_MAX_EVIDENCE_FILE_BYTES = 100_000_000
_INPUT_ARTIFACT_TYPES = frozenset(
    {
        "assembly-source-glb",
        "assembly-source-provenance",
        "assembly-source-publication-marker",
        "asset-concept",
        "asset-concept-provenance",
    }
)
_STAGES = (
    ("prepare", "asset_v07_assembly_prepare", None),
    ("concept_review", "asset_v07_assembly_concept_review", "concept_review"),
    ("source_review", "asset_v07_assembly_source_review", "source_review"),
    ("process", "asset_v07_assembly_process", None),
    ("validate", "asset_v07_assembly_validate", None),
    ("godot", "asset_v07_assembly_godot", None),
    ("final_review", "asset_v07_assembly_final_review", "final_visual_review"),
    ("evidence", "asset_v07_assembly_evidence", None),
)


def _json_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _contained_file(root: Path, path: Path | str, label: str) -> tuple[Path, str]:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        root_path = root.resolve(strict=True)
        relative = candidate.absolute().relative_to(root_path).as_posix()
        resolved = PathGuard(root_path).resolve_safe_path(relative)
    except (OSError, ValueError) as exc:
        raise ValidationError(f"{label} must be an existing file under the project root") from exc
    if not resolved.is_file():
        raise ValidationError(f"{label} must be a regular file")
    return resolved, relative


def _same_profile(spec: AssetSpecificationV07, profile: AssetProfileV07) -> None:
    if not isinstance(spec, AssetSpecificationV07) or not isinstance(profile, AssetProfileV07):
        raise ValidationError("V0.7 assembly workflow requires typed specification and profile")
    if spec.source_kind != "local_operator_assembly":
        raise ValidationError("local assembly workflow rejects provider-generated specifications")
    if spec.bound_profile() != profile or profile.geometry_mode != "assembly":
        raise ValidationError("specification must be bound to the exact injected assembly profile")
    profile.check_specification(spec)


def _write_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
    except FileExistsError as exc:
        raise ArtifactError(f"Refusing to overwrite assembly workflow output: {path.name}") from exc


def _read_bounded(path: Path, maximum: int, label: str) -> bytes:
    try:
        if path.stat().st_size > maximum:
            raise ValidationError(f"{label} exceeds the {maximum}-byte limit")
        with path.open("rb") as stream:
            payload = stream.read(maximum + 1)
    except OSError as exc:
        raise ValidationError(f"Cannot read {label}: {exc}") from exc
    if len(payload) > maximum:
        raise ValidationError(f"{label} exceeds the {maximum}-byte limit")
    return payload


def _parse_json_artifact(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ArtifactError(f"{label} is missing, oversized, or invalid JSON") from exc
    if not isinstance(value, dict):
        raise ArtifactError(f"{label} must contain a JSON object")
    return value


def _read_json_artifact(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = _read_bounded(path, _MAX_JSON_ARTIFACT_BYTES, label)
    except ValidationError as exc:
        raise ArtifactError(f"{label} is missing, oversized, or invalid JSON") from exc
    return _parse_json_artifact(payload, label)


def _validate_concept_png(payload: bytes) -> None:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValidationError("Concept image is not a PNG file")
    if len(payload) > _MAX_CONCEPT_BYTES:
        raise ValidationError("Concept image exceeds the bounded PNG byte limit")
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.format != "PNG" or image.width < 1 or image.height < 1:
                raise ValidationError("Concept image is not a valid non-empty PNG")
            if image.width * image.height > _MAX_CONCEPT_PIXELS:
                raise ValidationError("Concept image exceeds the bounded pixel limit")
            image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValidationError("Concept image cannot be fully decoded as a PNG") from exc


def _parse_concept_provenance(payload: bytes) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON number: {value}")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
        # Ensure nested numeric values are finite and the document remains JSON-safe.
        json.dumps(value, allow_nan=False)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ValidationError("Concept provenance must be bounded, valid JSON") from exc
    if not isinstance(value, dict):
        raise ValidationError("Concept provenance must contain a JSON object")
    return value


def _publication_marker_hash(package: AssemblyIngestResult) -> str:
    marker_path = package.package_dir / "publication_marker.json"
    marker_bytes = _read_bounded(
        marker_path, _MAX_CONCEPT_PROVENANCE_BYTES, "Assembly publication marker"
    )
    try:
        marker_data = json.loads(marker_bytes.decode("utf-8"))
        marker = AssemblyPublicationMarker.model_validate(marker_data)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValidationError("Assembly publication marker changed or is invalid") from exc
    if (
        marker.spec_fingerprint != package.spec_fingerprint
        or marker.source_artifact_sha256 != package.retained_glb_sha256
        or marker.source_artifact_byte_size != package.retained_glb_byte_size
        or marker.provenance_sha256 != package.retained_provenance_sha256
        or marker.provenance_byte_size != len(package.retained_provenance_bytes)
    ):
        raise ValidationError("Assembly publication marker does not bind the retained package")
    return hashlib.sha256(marker_bytes).hexdigest()


@dataclass(frozen=True)
class LocalAssemblyWorkflow:
    workflow: Workflow
    tasks: tuple[Task, ...]
    revision: AssetRevision


@dataclass(frozen=True)
class AssemblyAdapters:
    """Narrow test seams around external executables; production defaults are real adapters."""

    blender: Any | None = None
    godot_verifier: Callable[..., AssemblyRuntimeResult] = verify_godot_assembly
    godot_executable: Path | str | None = None
    godot_runner: Any | None = None


def _runtime_artifact_payloads(
    result: AssemblyRuntimeResult, review_views: tuple[str, ...]
) -> dict[str, bytes]:
    """Require the exact immutable byte roles returned by the Godot adapter."""
    if set(result.captures) != set(review_views):
        raise ArtifactError("Godot adapter captures do not match the bound profile views")
    capture_roles = {f"{view}.png" for view in review_views}
    expected_roles = {
        "runtime-request.json",
        "runtime-observation.json",
        "asset_runtime_harness_v07.gd",
        *capture_roles,
    }
    if set(result.artifacts) != expected_roles:
        raise ArtifactError(
            "Godot adapter must return request, observation, harness, and each declared PNG capture"
        )
    for view in review_views:
        if result.artifacts[f"{view}.png"] != result.captures[view]:
            raise ArtifactError(f"Godot adapter artifact bytes disagree for review capture {view}")
    return result.artifacts


class AssemblyProductionHandlers:
    def __init__(
        self,
        engine: Any,
        spec: AssetSpecificationV07,
        profile: AssetProfileV07,
        package_dir: Path,
        provenance_sha256: str,
        concept_image: Path,
        concept_provenance: Path,
        concept_image_relative: str,
        concept_provenance_relative: str,
        adapters: AssemblyAdapters,
    ) -> None:
        self.engine = engine
        self.root = engine.project_root.resolve(strict=True)
        self.spec = spec
        self.profile = profile
        self.package_dir = package_dir
        self.provenance_sha256 = provenance_sha256.lower()
        self.concept_image = concept_image
        self.concept_provenance = concept_provenance
        self.concept_image_relative = concept_image_relative
        self.concept_provenance_relative = concept_provenance_relative
        self.adapters = adapters
        self.assets = engine.artifact_mgr
        self.artifacts = engine.art_repo
        self.revisions = AssetRevisionRepository(engine.db)
        self.publication = AssemblyPublicationRepository(engine.db)
        self.tasks = engine.task_repo
        self.executions = engine.exec_repo
        self.approvals = engine.app_repo
        self.workflow_id = ""
        self.revision_number = 0

    def bind(self, workflow: Workflow, revision: AssetRevision) -> None:
        self.workflow_id = workflow.id
        self.revision_number = revision.revision_number

    def _param(self, task: Task, key: str) -> Any:
        value = task.parameters.get(key)
        if value is None:
            raise ValidationError(f"Assembly workflow task is missing pinned parameter '{key}'")
        return value

    def _revision(self, workflow: Workflow) -> AssetRevision:
        revision = self.revisions.get(self.spec.asset_id, self.revision_number)
        profile_hash = _json_hash(self.profile.document.model_dump(mode="json"))
        workflow_tasks = self.tasks.list_by_workflow(workflow.id)
        if (
            revision is None
            or revision.workflow_id != workflow.id
            or revision.spec_hash != spec_fingerprint(self.spec)
            or revision.profile_id != self.profile.profile_id
            or revision.profile_version != self.profile.version
            or not workflow_tasks
            or any(
                task.parameters.get("profile_document_sha256") != profile_hash
                for task in workflow_tasks
            )
        ):
            raise ValidationError(
                "Durable assembly revision or current profile document does not match the pinned workflow"
            )
        return revision

    def _package(self) -> AssemblyIngestResult:
        return verify_retained_assembly(
            self.package_dir,
            self.spec,
            expected_provenance_sha256=self.provenance_sha256,
            managed_root=self.root,
        )

    def _artifact(
        self,
        workflow: Workflow,
        task: Task,
        kind: str,
        relative: str,
        *,
        execution: Execution | None = None,
    ) -> Artifact:
        """Register or verify one immutable file role, rejecting duplicate-path aliases."""
        path = self.root / relative
        actual_hash = compute_sha256(path)
        matches = [
            a for a in self.artifacts.list_by_workflow(workflow.id) if a.relative_path == relative
        ]
        if matches:
            existing = matches[0]
            info = path.stat()
            input_owner_mismatch = (
                kind in _INPUT_ARTIFACT_TYPES
                and existing.task_id != self._task(workflow, "asset_v07_assembly_prepare").id
            )
            if (
                len(matches) != 1
                or existing.workflow_id != workflow.id
                or input_owner_mismatch
                or existing.artifact_type != kind
                or existing.producer != "local_operator_assembly"
                or existing.relative_path != relative
                or existing.content_hash != actual_hash
                or existing.file_size != info.st_size
            ):
                raise ArtifactError(
                    f"Assembly artifact role conflicts with an existing pinned row: {relative}"
                )
            self.engine.artifact_mgr.verify_artifact_integrity(matches[0])
            return cast(Artifact, existing)
        if kind in _INPUT_ARTIFACT_TYPES and (
            execution is None
            or task.task_type != "asset_v07_assembly_prepare"
            or task.status != TaskStatus.RUNNING
        ):
            raise ArtifactError(
                f"Pinned input artifact is missing before the prepare stage: {relative}"
            )
        artifact = self.engine.artifact_mgr.register_file_artifact(
            workflow.id, task.id, kind, "local_operator_assembly", relative
        )
        if execution is None:
            self.artifacts.save(artifact)
        return cast(Artifact, artifact)

    def _bytes_artifact(
        self, workflow: Workflow, task: Task, kind: str, relative: str, payload: bytes
    ) -> Artifact:
        path = self.root / relative
        _write_exclusive(path, payload)
        artifact = self.engine.artifact_mgr.register_file_artifact(
            workflow.id, task.id, kind, "godot_assembly", relative
        )
        if artifact.content_hash != hashlib.sha256(payload).hexdigest():
            raise ArtifactError("Assembly adapter bytes changed while creating an artifact row")
        return cast(Artifact, artifact)

    def _create_json_artifact(
        self,
        workflow: Workflow,
        task: Task,
        kind: str,
        relative: str,
        value: dict[str, Any],
        *,
        execution: Execution | None = None,
    ) -> Artifact:
        payload = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
        path = self.root / relative
        existing = [
            a for a in self.artifacts.list_by_workflow(workflow.id) if a.relative_path == relative
        ]
        if existing:
            expected_hash = hashlib.sha256(payload).hexdigest()
            info = path.stat()
            if (
                len(existing) != 1
                or existing[0].workflow_id != workflow.id
                or existing[0].task_id != task.id
                or existing[0].artifact_type != kind
                or existing[0].producer != "local_operator_assembly"
                or existing[0].relative_path != relative
                or existing[0].content_hash != expected_hash
                or compute_sha256(path) != expected_hash
                or existing[0].file_size != info.st_size
            ):
                raise ArtifactError(
                    f"Assembly report conflicts with a prior artifact row: {relative}"
                )
            self.engine.artifact_mgr.verify_artifact_integrity(existing[0])
            return cast(Artifact, existing[0])
        _write_exclusive(path, payload)
        artifact = self.engine.artifact_mgr.register_file_artifact(
            workflow.id, task.id, kind, "local_operator_assembly", relative
        )
        if execution is None:
            self.artifacts.save(artifact)
        return cast(Artifact, artifact)

    def _publish_attempt(
        self,
        workflow: Workflow,
        task: Task,
        execution: Execution,
        revision: AssetRevision,
        artifacts: list[Artifact],
    ) -> None:
        """Atomically publish verified filesystem output and write-once DB pins."""
        self._current_attempt(workflow, task, execution)
        verified_artifacts: list[Artifact] = []
        for artifact in artifacts:
            verified = replace(artifact)
            self.engine.artifact_mgr.verify_artifact_integrity(verified)
            verified_artifacts.append(verified)
        self.publication.publish_attempt(
            task=task,
            execution=execution,
            revision=revision,
            artifacts=verified_artifacts,
        )

    def _current_attempt(self, workflow: Workflow, task: Task, execution: Execution) -> None:
        latest = self.executions.get_latest_attempt(task.id)
        current_task = self.tasks.get(task.id)
        if (
            latest is None
            or latest.id != execution.id
            or latest.attempt_number != execution.attempt_number
            or latest.status != ExecutionStatus.RUNNING
            or current_task is None
            or current_task.status != TaskStatus.RUNNING
            or current_task.workflow_id != workflow.id
        ):
            raise ArtifactError(
                "Stale assembly worker lost its execution fence; output was not published"
            )

    def _approval(self, workflow: Workflow, task: Task, approval_type: str) -> Any:
        rows = [a for a in self.approvals.list_by_workflow(workflow.id) if a.task_id == task.id]
        latest = max(rows, key=lambda item: (item.requested_at, item.id), default=None)
        if (
            latest is None
            or latest.approval_type != approval_type
            or latest.status != ApprovalStatus.APPROVED
        ):
            raise ArtifactError(f"Required human {approval_type} approval is missing")
        # The engine stores the exact artifact set presented with the request.
        # Later receipt artifacts must not retroactively invalidate an approval.
        inputs = self.engine.approval_inputs(
            workflow, task, CostClass.LOCAL, artifact_ids=latest.artifact_ids
        )
        current_hash = compute_operation_hash(task.id, approval_type, inputs)
        if current_hash != latest.operation_hash:
            raise ArtifactError(
                f"Human {approval_type} approval is stale for current assembly inputs "
                f"(approved={latest.operation_hash[:12]}, current={current_hash[:12]})"
            )
        return latest

    def _source_context(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        package = self._package()
        revision = self._revision(workflow)
        roles = self._input_artifacts(workflow, task)
        provenance = package.provenance
        return {
            "stage": task.task_type,
            "workflow_id": workflow.id,
            "asset_id": self.spec.asset_id,
            "source_version": revision.revision_number,
            "spec_sha256": revision.spec_hash,
            "profile_id": self.profile.profile_id,
            "profile_version": self.profile.version,
            "profile_document_sha256": _json_hash(self.profile.document.model_dump(mode="json")),
            "source_glb_artifact_id": roles["source_glb"].id,
            "source_glb_sha256": package.retained_glb_sha256,
            "source_provenance_artifact_id": roles["source_provenance"].id,
            "source_provenance_sha256": package.retained_provenance_sha256,
            "source_front": provenance.source_front,
            "authoring_tool_name": provenance.authoring_tool_name,
            "authoring_tool_version": provenance.authoring_tool_version,
            "source_actor": provenance.actor,
            "source_reason": provenance.reason,
        }

    def _input_artifacts(
        self, workflow: Workflow, task: Task, *, execution: Execution | None = None
    ) -> dict[str, Artifact]:
        package = self._package()
        rows = {
            "source_glb": self._artifact(
                workflow,
                task,
                "assembly-source-glb",
                package.retained_glb_path.relative_to(self.root).as_posix(),
                execution=execution,
            ),
            "source_provenance": self._artifact(
                workflow,
                task,
                "assembly-source-provenance",
                package.retained_provenance_path.relative_to(self.root).as_posix(),
                execution=execution,
            ),
            "concept_image": self._artifact(
                workflow,
                task,
                "asset-concept",
                self.concept_image_relative,
                execution=execution,
            ),
            "concept_provenance": self._artifact(
                workflow,
                task,
                "asset-concept-provenance",
                self.concept_provenance_relative,
                execution=execution,
            ),
            "source_publication_marker": self._artifact(
                workflow,
                task,
                "assembly-source-publication-marker",
                (package.package_dir / "publication_marker.json")
                .relative_to(self.root)
                .as_posix(),
                execution=execution,
            ),
        }
        pinned_hashes = {
            "source_glb": "source_glb_sha256",
            "source_provenance": "source_provenance_sha256",
            "concept_image": "concept_image_sha256",
            "concept_provenance": "concept_provenance_sha256",
            "source_publication_marker": "source_publication_marker_sha256",
        }
        for role, parameter in pinned_hashes.items():
            if rows[role].content_hash != task.parameters.get(parameter):
                raise ArtifactError(f"Assembly input {role} no longer matches its creation-time pin")
        if len({item.id for item in rows.values()}) != len(rows):
            raise ArtifactError("Assembly input roles must resolve to unique artifact rows")
        return rows

    def approval_context(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        if task.task_type.endswith("concept_review"):
            revision = self._revision(workflow)
            roles = self._input_artifacts(workflow, task)
            return {
                "stage": "concept_review",
                "workflow_id": workflow.id,
                "asset_id": self.spec.asset_id,
                "source_version": revision.revision_number,
                "spec_sha256": revision.spec_hash,
                "profile_id": self.profile.profile_id,
                "profile_version": self.profile.version,
                "profile_document_sha256": _json_hash(
                    self.profile.document.model_dump(mode="json")
                ),
                "concept_image_sha256": roles["concept_image"].content_hash,
                "concept_provenance_sha256": roles["concept_provenance"].content_hash,
            }
        if task.task_type.endswith("source_review"):
            return self._source_context(workflow, task)
        if task.task_type.endswith("final_review"):
            return self._final_context(workflow, task)
        raise ValidationError("Unknown assembly human approval stage")

    def _final_context(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        package = self._package()
        revision = self._revision(workflow)
        tasks = self.tasks.list_by_workflow(workflow.id)
        process_task = next((t for t in tasks if t.task_type == "asset_v07_assembly_process"), None)
        validation_task = next(
            (t for t in tasks if t.task_type == "asset_v07_assembly_validate"), None
        )
        if process_task is None or validation_task is None:
            raise ArtifactError("Process and validation tasks are required for final review")
        process_attempt = self.executions.get_latest_attempt(process_task.id)
        validation_attempt = self.executions.get_latest_attempt(validation_task.id)
        if process_attempt is None or process_attempt.status != ExecutionStatus.COMPLETED:
            raise ArtifactError(
                "Final review requires the latest successful Blender process attempt"
            )
        if validation_attempt is None or validation_attempt.status != ExecutionStatus.COMPLETED:
            raise ArtifactError(
                "Final review requires the latest successful geometry validation attempt"
            )
        processed_row, _ = self._processed(workflow)
        if processed_row.content_hash != revision.processed_glb_hash:
            raise ArtifactError("Latest Blender output differs from the immutable revision hash")
        process_reports = [
            a
            for a in self.artifacts.list_by_task(process_task.id)
            if a.artifact_type == "assembly-blender-report"
            and process_attempt.id in a.relative_path
            and f"-a{process_attempt.attempt_number}" in a.relative_path
        ]
        if len(process_reports) != 1:
            raise ArtifactError("Latest Blender attempt has no unique processing report")
        self.engine.artifact_mgr.verify_artifact_integrity(process_reports[0])
        blender_data = _read_json_artifact(
            self.root / process_reports[0].relative_path, "Blender processing report"
        )
        if (
            blender_data.get("source_glb_sha256") != package.retained_glb_sha256
            or blender_data.get("provenance_sha256") != package.retained_provenance_sha256
            or blender_data.get("output_glb_sha256", blender_data.get("processed_glb_sha256"))
            != revision.processed_glb_hash
            or blender_data.get("spec_fingerprint") != revision.spec_hash
            or blender_data.get("status") != "SUCCESS"
        ):
            raise ArtifactError(
                "Latest Blender report does not bind current source/spec/output hashes"
            )
        script_path = AssemblyProcessor.get_script_path()
        expected_script_hash = compute_sha256(script_path)
        if (
            blender_data.get("processing_script_sha256", blender_data.get("script_sha256"))
            != expected_script_hash
        ):
            raise ArtifactError(
                "Latest Blender report does not bind the packaged processing script"
            )
        validation_reports = [
            a
            for a in self.artifacts.list_by_task(validation_task.id)
            if a.artifact_type == "assembly-validation-report"
            and validation_attempt.id in a.relative_path
            and f"-a{validation_attempt.attempt_number}" in a.relative_path
        ]
        if (
            len(validation_reports) != 1
            or validation_reports[0].content_hash != revision.validation_report_hash
        ):
            raise ArtifactError("Latest geometry validation report is missing or stale")
        self.engine.artifact_mgr.verify_artifact_integrity(validation_reports[0])
        validation_data = _read_json_artifact(
            self.root / validation_reports[0].relative_path, "Geometry validation report"
        )
        if (
            validation_data.get("source_glb_sha256") != package.retained_glb_sha256
            or validation_data.get("processed_glb_sha256") != revision.processed_glb_hash
            or validation_data.get("profile_document_sha256")
            != _json_hash(self.profile.document.model_dump(mode="json"))
            or not validation_data.get("preservation_passed")
        ):
            raise ArtifactError("Latest geometry validation is not bound to the current assembly")
        godot_task = next((t for t in tasks if t.task_type == "asset_v07_assembly_godot"), None)
        if godot_task is None:
            raise ArtifactError("Runtime task is missing from the assembly DAG")
        attempt = self.executions.get_latest_attempt(godot_task.id)
        if attempt is None or attempt.status != ExecutionStatus.COMPLETED:
            raise ArtifactError(
                "Final review requires the latest successful Godot execution attempt"
            )
        runtime_rows = [
            a
            for a in self.artifacts.list_by_task(godot_task.id)
            if a.artifact_type == "assembly-runtime-index"
            and attempt.id in a.relative_path
            and f"-a{attempt.attempt_number}" in a.relative_path
        ]
        if len(runtime_rows) != 1:
            raise ArtifactError("Current successful Godot attempt has no unique runtime index")
        runtime = runtime_rows[0]
        self.engine.artifact_mgr.verify_artifact_integrity(runtime)
        data = _read_json_artifact(self.root / runtime.relative_path, "Godot runtime index")
        if (
            data.get("execution_id") != attempt.id
            or data.get("attempt_number") != attempt.attempt_number
            or data.get("processed_glb_sha256") != revision.processed_glb_hash
            or data.get("status") != "PASS"
        ):
            raise ArtifactError(
                "Runtime index is stale or does not match the current successful attempt"
            )
        capture_hashes = data.get("capture_sha256")
        if not isinstance(capture_hashes, dict) or set(capture_hashes) != set(
            self.profile.review_views
        ):
            raise ArtifactError("Final review requires every current profile capture")
        capture_rows = [
            a
            for a in self.artifacts.list_by_task(godot_task.id)
            if a.artifact_type == "assembly-review-capture"
            and a.relative_path.startswith(
                f".gamefactory/assets/{self.spec.asset_id}/r{revision.revision_number:03d}/review/{attempt.id}-a{attempt.attempt_number}/"
            )
        ]
        if len(capture_rows) != len(capture_hashes):
            raise ArtifactError(
                "Runtime capture artifacts do not match the latest successful attempt"
            )
        for row in capture_rows:
            self.engine.artifact_mgr.verify_artifact_integrity(row)
            view = Path(row.relative_path).stem
            if capture_hashes.get(view) != row.content_hash:
                raise ArtifactError("Runtime capture hash changed after Godot verification")
        return {
            "stage": "final_review",
            "workflow_id": workflow.id,
            "asset_id": self.spec.asset_id,
            "source_version": revision.revision_number,
            "spec_sha256": revision.spec_hash,
            "profile_id": self.profile.profile_id,
            "profile_version": self.profile.version,
            "profile_document_sha256": _json_hash(self.profile.document.model_dump(mode="json")),
            "source_glb_sha256": package.retained_glb_sha256,
            "source_provenance_sha256": package.retained_provenance_sha256,
            "processed_glb_sha256": revision.processed_glb_hash,
            "process_execution_id": process_attempt.id,
            "process_attempt_number": process_attempt.attempt_number,
            "process_report_sha256": process_reports[0].content_hash,
            "processing_script_sha256": expected_script_hash,
            "validation_report_sha256": revision.validation_report_hash,
            "validation_execution_id": validation_attempt.id,
            "validation_attempt_number": validation_attempt.attempt_number,
            "runtime_execution_id": attempt.id,
            "runtime_attempt_number": attempt.attempt_number,
            "runtime_index_sha256": runtime.content_hash,
            "capture_sha256": dict(sorted(capture_hashes.items())),
        }

    def current_evidence_inputs(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> dict[str, Any]:
        """Build a rehashed, current-attempt snapshot for the cold evidence exporter.

        This selector does not trust caller-provided role files or receipt claims.
        It resolves every artifact from this workflow's SQLite rows, rechecks the
        retained package/profile and three human approvals, and rejects stale
        process, validation, or runtime attempts.
        """
        from gamefactory.workflows.assembly_evidence import EvidenceFile

        self._evidence_attempt(workflow, task, execution)
        if task.task_type != "asset_v07_assembly_evidence":
            raise ValidationError("Evidence snapshot requires the assembly evidence task")
        self._final_context(workflow, task)
        package = self._package()
        revision = self._revision(workflow)
        all_rows = self.artifacts.list_by_workflow(workflow.id)

        def one(
            artifact_type: str, *, task_id: str | None = None, path_part: str | None = None
        ) -> Artifact:
            matches = [
                row
                for row in all_rows
                if row.artifact_type == artifact_type
                and (task_id is None or row.task_id == task_id)
                and (path_part is None or path_part in row.relative_path)
            ]
            if len(matches) != 1:
                raise ArtifactError(
                    f"Current assembly evidence requires exactly one {artifact_type} artifact"
                )
            row = cast(Artifact, matches[0])
            self.engine.artifact_mgr.verify_artifact_integrity(row)
            return row

        def latest_successful(task_type: str) -> tuple[Task, Execution]:
            stage_task = self._task(workflow, task_type)
            attempts = self.executions.list_by_task(stage_task.id)
            latest = self.executions.get_latest_attempt(stage_task.id)
            if (
                latest is None
                or latest.status != ExecutionStatus.COMPLETED
                or stage_task.status != TaskStatus.COMPLETED
            ):
                raise ArtifactError(f"Latest {task_type} attempt is not successful")
            history = [
                {
                    "id": item.id,
                    "attempt_number": item.attempt_number,
                    "status": item.status.value,
                }
                for item in sorted(attempts, key=lambda value: value.attempt_number)
            ]
            if not history or history[-1]["id"] != latest.id:
                raise ArtifactError(f"Latest {task_type} attempt history is inconsistent")
            return stage_task, latest

        process_task, process_attempt = latest_successful("asset_v07_assembly_process")
        validation_task, validation_attempt = latest_successful("asset_v07_assembly_validate")
        godot_task, godot_attempt = latest_successful("asset_v07_assembly_godot")
        attempt_suffix = f"{process_attempt.id}-a{process_attempt.attempt_number}"
        process_report = one(
            "assembly-blender-report", task_id=process_task.id, path_part=attempt_suffix
        )
        process_report_data = _read_json_artifact(
            self.root / process_report.relative_path, "Current Blender report"
        )
        script_path = AssemblyProcessor.get_script_path()
        if not script_path.is_file():
            raise ArtifactError("Packaged Blender processing script is missing")
        script_bytes = script_path.read_bytes()
        script_hash = hashlib.sha256(script_bytes).hexdigest()
        if (
            process_report_data.get(
                "processing_script_sha256", process_report_data.get("script_sha256")
            )
            != script_hash
            or process_report_data.get("source_glb_sha256") != package.retained_glb_sha256
            or process_report_data.get("provenance_sha256") != package.retained_provenance_sha256
            or process_report_data.get(
                "output_glb_sha256", process_report_data.get("processed_glb_sha256")
            )
            != revision.processed_glb_hash
            or process_report_data.get("spec_fingerprint") != revision.spec_hash
            or process_report_data.get("status") != "SUCCESS"
        ):
            raise ArtifactError(
                "Current processing report does not bind the packaged Blender script"
            )

        processed_row, _processed_path = self._processed(workflow)
        validation_suffix = f"{validation_attempt.id}-a{validation_attempt.attempt_number}"
        validation_row = one(
            "assembly-validation-report",
            task_id=validation_task.id,
            path_part=validation_suffix,
        )
        validation_data = _read_json_artifact(
            self.root / validation_row.relative_path, "Current validation report"
        )
        if (
            validation_row.content_hash != revision.validation_report_hash
            or validation_data.get("source_glb_sha256") != package.retained_glb_sha256
            or validation_data.get("processed_glb_sha256") != processed_row.content_hash
            or validation_data.get("preservation_passed") is not True
            or validation_data.get("profile_document_sha256")
            != _json_hash(self.profile.document.model_dump(mode="json"))
            or validation_data.get("passed") is not True
        ):
            raise ArtifactError(
                "Current validation report does not bind current source/profile/output"
            )
        runtime_suffix = f"{godot_attempt.id}-a{godot_attempt.attempt_number}"
        runtime_index = one(
            "assembly-runtime-index", task_id=godot_task.id, path_part=runtime_suffix
        )
        runtime_request = one(
            "assembly-runtime-request", task_id=godot_task.id, path_part=runtime_suffix
        )
        runtime_observation = one(
            "assembly-runtime-observation", task_id=godot_task.id, path_part=runtime_suffix
        )
        runtime_harness = one(
            "assembly-runtime-harness", task_id=godot_task.id, path_part=runtime_suffix
        )
        runtime_index_data = _read_json_artifact(
            self.root / runtime_index.relative_path, "Current runtime index"
        )
        request_bytes = _read_bounded(
            self.root / runtime_request.relative_path,
            _MAX_JSON_ARTIFACT_BYTES,
            "Current runtime request",
        )
        observation_bytes = _read_bounded(
            self.root / runtime_observation.relative_path,
            _MAX_JSON_ARTIFACT_BYTES,
            "Current runtime observation",
        )
        request_data = _read_json_artifact(
            self.root / runtime_request.relative_path, "Current runtime request"
        )
        observation_data = _read_json_artifact(
            self.root / runtime_observation.relative_path, "Current runtime observation"
        )
        bare_request = {
            key: value for key, value in request_data.items() if key != "request_digest"
        }
        canonical_request_hash = hashlib.sha256(
            json.dumps(
                bare_request,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        if (
            runtime_index_data.get("runtime_request_sha256") != runtime_request.content_hash
            or runtime_index_data.get("runtime_observation_sha256")
            != runtime_observation.content_hash
            or runtime_index_data.get("runtime_harness_sha256") != runtime_harness.content_hash
            or request_data.get("request_digest") != canonical_request_hash
            or request_data.get("request_digest") != runtime_index_data.get("request_digest")
            or request_data.get("workflow_id") != workflow.id
            or request_data.get("revision") != revision.revision_number
            or request_data.get("execution_id") != godot_attempt.id
            or request_data.get("attempt_number") != godot_attempt.attempt_number
            or request_data.get("processed_glb_sha256") != processed_row.content_hash
            or observation_data.get("status") != "PASS"
            or observation_data.get("workflow_id") != workflow.id
            or observation_data.get("revision") != revision.revision_number
            or observation_data.get("execution_id") != godot_attempt.id
            or observation_data.get("attempt_number") != godot_attempt.attempt_number
            or observation_data.get("request_digest") != canonical_request_hash
            or observation_data.get("harness_sha256") != runtime_harness.content_hash
            or observation_data.get("processed_glb_sha256") != processed_row.content_hash
            or hashlib.sha256(observation_bytes).hexdigest() != runtime_observation.content_hash
            or hashlib.sha256(request_bytes).hexdigest() != runtime_request.content_hash
        ):
            raise ArtifactError(
                "Runtime index hashes differ from current persisted attempt artifacts"
            )

        source_task = self._task(workflow, "asset_v07_assembly_source_review")
        concept_task = self._task(workflow, "asset_v07_assembly_concept_review")
        final_task = self._task(workflow, "asset_v07_assembly_final_review")

        def receipt(task_row: Task, approval_type: str) -> dict[str, Any]:
            approval = self._approval(workflow, task_row, approval_type)
            inputs = self.engine.approval_inputs(
                workflow, task_row, CostClass.LOCAL, artifact_ids=approval.artifact_ids
            )
            fingerprint = compute_operation_hash(task_row.id, approval_type, inputs)
            if fingerprint != approval.operation_hash:
                raise ArtifactError(f"Human {approval_type} approval changed after decision")
            return {
                "schema_version": "production-receipt-0.7.0",
                "workflow_id": workflow.id,
                "revision": revision.revision_number,
                "source_version": revision.revision_number,
                "approval_type": approval_type,
                "status": approval.status.value,
                "task_id": task_row.id,
                "inputs": inputs,
                "operation_hash": approval.operation_hash,
                "fingerprint": fingerprint,
                "actor": approval.actor,
                "comment": approval.comment,
            }

        concept_receipt = receipt(concept_task, "concept_review")
        source_receipt = receipt(source_task, "source_review")
        final_receipt = receipt(final_task, "final_visual_review")

        def verify_receipt_artifact(
            task_row: Task,
            receipt_value: dict[str, Any],
            artifact_type: str,
            context_key: str | None = None,
        ) -> None:
            approval = self._approval(workflow, task_row, receipt_value["approval_type"])
            matches = []
            for candidate in all_rows:
                if candidate.artifact_type != artifact_type or candidate.task_id != task_row.id:
                    continue
                self.engine.artifact_mgr.verify_artifact_integrity(candidate)
                value = _read_json_artifact(
                    self.root / candidate.relative_path, "Human approval receipt"
                )
                if value.get("approval_id") != approval.id:
                    continue
                if (
                    value.get("operation_hash") != receipt_value["operation_hash"]
                    or value.get("actor") != receipt_value["actor"]
                    or value.get("status") != "APPROVED"
                ):
                    raise ArtifactError("Persisted human receipt differs from the current approval")
                if (
                    context_key is not None
                    and value.get(context_key)
                    != receipt_value["inputs"]["scope"]["handler_context"]
                ):
                    raise ArtifactError(
                        "Persisted human receipt context differs from approved inputs"
                    )
                matches.append(candidate)
            if len(matches) != 1:
                raise ArtifactError(
                    "Current human approval receipt artifact is missing or ambiguous"
                )

        verify_receipt_artifact(concept_task, concept_receipt, "assembly-concept-approval")
        verify_receipt_artifact(
            source_task, source_receipt, "assembly-source-approval", "source_review"
        )
        verify_receipt_artifact(
            final_task, final_receipt, "assembly-final-approval", "review_context"
        )
        package_prefix = package.package_dir.relative_to(self.root).as_posix()
        marker_rel = f"{package_prefix}/publication_marker.json"
        concept_row = one("asset-concept", path_part=self.concept_image_relative)
        concept_provenance_row = one(
            "asset-concept-provenance", path_part=self.concept_provenance_relative
        )
        raw_row = one(
            "assembly-source-glb",
            path_part=package.retained_glb_path.relative_to(self.root).as_posix(),
        )
        source_provenance_row = one(
            "assembly-source-provenance",
            path_part=package.retained_provenance_path.relative_to(self.root).as_posix(),
        )
        marker_row = one("assembly-source-publication-marker", path_part=marker_rel)
        if (
            raw_row.content_hash != revision.raw_glb_hash
            or source_provenance_row.content_hash != self.provenance_sha256
            or concept_row.content_hash != revision.concept_hash
            or concept_row.content_hash != task.parameters.get("concept_image_sha256")
            or concept_provenance_row.content_hash
            != task.parameters.get("concept_provenance_sha256")
        ):
            raise ArtifactError("Retained source/concept rows differ from durable workflow pins")
        specification_row = one("asset-specification-v07", path_part="/specification.json")
        specification_data = _read_json_artifact(
            self.root / specification_row.relative_path, "Persisted V0.7 specification"
        )
        if specification_data != self.spec.model_dump(mode="json"):
            raise ArtifactError("Persisted V0.7 specification differs from its durable revision")
        profile_bytes = (
            json.dumps(
                self.profile.document.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        ).encode("utf-8")
        profile_doc = self.profile.document.model_dump(mode="json")
        profile_document_hash = _json_hash(profile_doc)
        runtime_profile_hash = hashlib.sha256(
            json.dumps(
                profile_doc,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        runtime_spec_hash = hashlib.sha256(
            json.dumps(
                self.spec.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()

        runtime_request_data = _read_json_artifact(
            self.root / runtime_request.relative_path, "Current runtime request"
        )
        runtime_observation_data = _read_json_artifact(
            self.root / runtime_observation.relative_path, "Current runtime observation"
        )
        if (
            runtime_request_data.get("profile_sha256") != runtime_profile_hash
            or runtime_request_data.get("specification_sha256") != runtime_spec_hash
        ):
            raise ArtifactError(
                "Runtime request does not bind the current typed specification/profile"
            )
        runtime_captures = [
            row
            for row in all_rows
            if row.artifact_type == "assembly-review-capture"
            and row.task_id == godot_task.id
            and row.relative_path.startswith(
                f".gamefactory/assets/{self.spec.asset_id}/r{revision.revision_number:03d}/review/{runtime_suffix}/"
            )
        ]
        capture_by_view = {Path(row.relative_path).stem: row for row in runtime_captures}
        if set(capture_by_view) != set(self.profile.review_views):
            raise ArtifactError("Evidence snapshot does not contain every latest runtime capture")
        for row in runtime_captures:
            self.engine.artifact_mgr.verify_artifact_integrity(row)

        provenance = package.provenance
        binding = {
            "schema_version": "asset-evidence-0.7.0",
            "workflow_id": workflow.id,
            "revision": revision.revision_number,
            "source_version": revision.revision_number,
            "asset_id": self.spec.asset_id,
            "generation_mode": "local_operator_assembly",
            "paid": False,
            "product_ready": False,
            "spec_sha256": revision.spec_hash,
            "profile_id": self.profile.profile_id,
            "profile_version": self.profile.version,
            "profile_document_sha256": profile_document_hash,
            "review_views": list(self.profile.review_views),
            "source_glb_sha256": raw_row.content_hash,
            "source_provenance_sha256": source_provenance_row.content_hash,
            "source_front": provenance.source_front,
            "normalization_applied": provenance.source_front == "+Z",
            "selected_rule_groups": [
                group.value for group in selected_v07_groups(self.spec, self.profile)
            ],
            "processed_glb_sha256": processed_row.content_hash,
            "processing_execution_id": process_attempt.id,
            "processing_attempt_number": process_attempt.attempt_number,
            "processing_report_sha256": process_report.content_hash,
            "process_report_sha256": process_report.content_hash,
            "processing_script_sha256": script_hash,
            "validation_execution_id": validation_attempt.id,
            "validation_attempt_number": validation_attempt.attempt_number,
            "validation_sha256": validation_row.content_hash,
            "runtime_execution_id": godot_attempt.id,
            "runtime_attempt_number": godot_attempt.attempt_number,
            "runtime_index_sha256": runtime_index.content_hash,
            "runtime_request_digest": runtime_request_data.get("request_digest"),
            "runtime_profile_sha256": runtime_profile_hash,
            "runtime_specification_sha256": runtime_spec_hash,
            "runtime_observation_sha256": runtime_observation.content_hash,
            "runtime_harness_sha256": runtime_harness.content_hash,
            "capture_sha256": dict(
                sorted((view, row.content_hash) for view, row in capture_by_view.items())
            ),
            "current_attempt_history": {
                "process": [
                    {
                        "id": item.id,
                        "attempt_number": item.attempt_number,
                        "status": item.status.value,
                    }
                    for item in sorted(
                        self.executions.list_by_task(process_task.id),
                        key=lambda item: item.attempt_number,
                    )
                ],
                "validate": [
                    {
                        "id": item.id,
                        "attempt_number": item.attempt_number,
                        "status": item.status.value,
                    }
                    for item in sorted(
                        self.executions.list_by_task(validation_task.id),
                        key=lambda item: item.attempt_number,
                    )
                ],
                "godot": [
                    {
                        "id": item.id,
                        "attempt_number": item.attempt_number,
                        "status": item.status.value,
                    }
                    for item in sorted(
                        self.executions.list_by_task(godot_task.id),
                        key=lambda item: item.attempt_number,
                    )
                ],
            },
        }

        def file(role: str, row: Artifact, *, view: str | None = None) -> EvidenceFile:
            return EvidenceFile(
                role=role,
                path=self.root / row.relative_path,
                view=view,
                artifact_id=row.id,
                relative_path=row.relative_path,
                expected_sha256=row.content_hash,
                expected_size=row.file_size,
            )

        files: list[EvidenceFile] = [
            file("specification", specification_row),
            file("concept", concept_row),
            file("concept_provenance", concept_provenance_row),
            file("raw_glb", raw_row),
            file("source_provenance", source_provenance_row),
            file("source_publication_marker", marker_row),
            EvidenceFile(
                role="bound_profile",
                data=profile_bytes,
                expected_sha256=hashlib.sha256(profile_bytes).hexdigest(),
                expected_size=len(profile_bytes),
            ),
            file("processed_glb", processed_row),
            file("processing_report", process_report),
            EvidenceFile(
                role="processing_script",
                data=script_bytes,
                expected_sha256=hashlib.sha256(script_bytes).hexdigest(),
                expected_size=len(script_bytes),
            ),
            file("validation", validation_row),
            file("runtime_index", runtime_index),
            file("runtime_observation", runtime_observation),
            file("runtime_request", runtime_request),
            file("runtime_harness", runtime_harness),
            *[
                file("runtime_capture", row, view=view)
                for view, row in sorted(capture_by_view.items())
            ],
        ]
        # Keep names consumed by the verifier explicit rather than trusting the
        # nested in-memory observation returned by a runtime adapter stub.
        if runtime_observation_data != runtime_index_data.get("observation"):
            raise ArtifactError(
                "Runtime observation file differs from its indexed verification result"
            )
        return {
            "files": files,
            "binding": binding,
            "source_review_receipt": source_receipt,
            "final_review_receipt": final_receipt,
            "concept_review_receipt": concept_receipt,
        }

    def _evidence_attempt(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> None:
        """Fence evidence selection to the current running or completed attempt."""
        latest = self.executions.get_latest_attempt(task.id)
        current_task = self.tasks.get(task.id)
        valid_pairs = {
            (ExecutionStatus.RUNNING, TaskStatus.RUNNING),
            (ExecutionStatus.COMPLETED, TaskStatus.COMPLETED),
        }
        if (
            latest is None
            or latest.id != execution.id
            or latest.attempt_number != execution.attempt_number
            or (latest.status, current_task.status if current_task else None) not in valid_pairs
            or current_task is None
            or current_task.workflow_id != workflow.id
        ):
            raise ArtifactError("Assembly evidence attempt is not the current running or completed attempt")

    def verify_completed_evidence_bundle(
        self,
        workflow: Workflow,
        task: Task,
        execution: Execution,
        manifest_artifact: Artifact,
    ) -> dict[str, Any]:
        """Read-only verify that a completed evidence bundle still matches current state."""
        if workflow.status != WorkflowStatus.COMPLETED or task.status != TaskStatus.COMPLETED:
            raise ArtifactError("Completed evidence revalidation requires a completed workflow")
        self._evidence_attempt(workflow, task, execution)
        snapshot = self.current_evidence_inputs(workflow, task, execution)
        self.engine.artifact_mgr.verify_artifact_integrity(manifest_artifact)
        manifest_path = self.root / manifest_artifact.relative_path
        manifest = _read_json_artifact(manifest_path, "Completed evidence manifest")
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
            raise ArtifactError("Completed evidence manifest is malformed")
        bundle = manifest_path.parent

        binding = snapshot["binding"]
        for key, value in binding.items():
            expected = True if key == "product_ready" else value
            if manifest.get(key) != expected:
                raise ArtifactError(f"Completed evidence binding changed: {key}")

        entries: dict[tuple[str, str | None], dict[str, Any]] = {}
        for item in manifest["files"]:
            if not isinstance(item, dict) or not isinstance(item.get("role"), str):
                raise ArtifactError("Completed evidence file index is malformed")
            identity = (item["role"], item.get("view"))
            if identity in entries:
                raise ArtifactError("Completed evidence contains duplicate role entries")
            entries[identity] = item

        expected_identities: set[tuple[str, str | None]] = set()
        for source in snapshot["files"]:
            identity = (source.role, source.view)
            if identity in expected_identities:
                raise ArtifactError("Current evidence snapshot contains duplicate roles")
            expected_identities.add(identity)
            actual = entries.get(identity)
            if actual is None:
                raise ArtifactError(f"Completed bundle is missing current role {source.role}")
            if source.data is not None:
                raw = bytes(source.data)
            else:
                source_path = source.path
                if source_path is None:
                    raise ArtifactError("Current evidence snapshot has no source path")
                raw = _read_bounded(
                    source_path, _MAX_EVIDENCE_FILE_BYTES, "Current evidence source"
                )
            digest = hashlib.sha256(raw).hexdigest()
            if (
                actual.get("sha256") != digest
                or actual.get("size") != len(raw)
                or actual.get("artifact_id") != source.artifact_id
                or actual.get("source_relative_path") != source.relative_path
            ):
                raise ArtifactError(f"Completed bundle role differs from current {source.role}")

        receipts = {
            "concept_approval": snapshot["concept_review_receipt"],
            "source_approval": snapshot["source_review_receipt"],
            "final_approval": snapshot["final_review_receipt"],
        }
        for role, expected_receipt in receipts.items():
            row = entries.get((role, None))
            if row is None:
                raise ArtifactError(f"Completed bundle is missing current {role}")
            expected_path = f"files/{role}/{role}.json"
            if row.get("path") != expected_path:
                raise ArtifactError(f"Completed bundle {role} path is not canonical")
            receipt_path = bundle / expected_path
            receipt = _read_json_artifact(receipt_path, "Completed human approval receipt")
            if not isinstance(receipt, dict):
                raise ArtifactError(f"Completed bundle {role} is malformed")
            expected_inputs = expected_receipt.get("inputs", {})
            receipt_inputs = receipt.get("inputs", {})
            expected_context = (
                expected_inputs.get("scope", {}).get("handler_context")
                if isinstance(expected_inputs, dict)
                else None
            )
            receipt_context = (
                receipt_inputs.get("scope", {}).get("handler_context")
                if isinstance(receipt_inputs, dict)
                else None
            )
            scalar_fields = set(expected_receipt) - {"inputs"}
            if (
                any(receipt.get(key) != expected_receipt.get(key) for key in scalar_fields)
                or receipt_context != expected_context
                or receipt.get("operation_hash") != expected_receipt["operation_hash"]
                or receipt.get("fingerprint") != expected_receipt["fingerprint"]
            ):
                raise ArtifactError(f"Completed bundle {role} differs from current approval")
        approval_receipts = {
            "concept_review": snapshot["concept_review_receipt"],
            "source_review": snapshot["source_review_receipt"],
            "final_visual_review": snapshot["final_review_receipt"],
        }
        if manifest.get("human_reviews") != {
            key: receipt["operation_hash"] for key, receipt in approval_receipts.items()
        }:
            raise ArtifactError("Completed bundle human approval fingerprints changed")
        final_receipt = snapshot["final_review_receipt"]
        if manifest.get("final_review") != {
            "decision": final_receipt["status"],
            "fingerprint": final_receipt["fingerprint"],
        }:
            raise ArtifactError("Completed bundle final approval binding changed")

        from gamefactory.workflows.assembly_evidence import (
            verify_local_assembly_evidence_bundle,
        )

        result = verify_local_assembly_evidence_bundle(bundle)
        # Rehash project sources after cold verification too, closing the window
        # where a project artifact could change while the bundle was checked.
        for source in snapshot["files"]:
            if source.path is not None:
                raw = _read_bounded(
                    source.path, _MAX_EVIDENCE_FILE_BYTES, "Current evidence source"
                )
                if (
                    source.expected_sha256 is not None
                    and hashlib.sha256(raw).hexdigest() != source.expected_sha256
                ):
                    raise ArtifactError("Current project evidence changed during completed revalidation")
        final_snapshot = self.current_evidence_inputs(workflow, task, execution)

        def snapshot_identity(value: dict[str, Any]) -> tuple[Any, ...]:
            files_identity = []
            for source in value["files"]:
                digest = source.expected_sha256
                size = source.expected_size
                if source.data is not None:
                    raw = bytes(source.data)
                    digest = hashlib.sha256(raw).hexdigest()
                    size = len(raw)
                files_identity.append(
                    (
                        source.role,
                        source.view,
                        source.artifact_id,
                        source.relative_path,
                        digest,
                        size,
                    )
                )
            return (
                value["binding"],
                value["concept_review_receipt"],
                value["source_review_receipt"],
                value["final_review_receipt"],
                tuple(sorted(files_identity)),
            )

        if snapshot_identity(final_snapshot) != snapshot_identity(snapshot):
            raise ArtifactError("Current assembly evidence changed during completed revalidation")
        latest_workflow = self.engine.wf_repo.get(workflow.id)
        if latest_workflow is None or latest_workflow.status != WorkflowStatus.COMPLETED:
            raise ArtifactError("Workflow state changed during completed evidence revalidation")
        return result

    def prepare(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        self._current_attempt(workflow, task, execution)
        self._package()
        revision = self._revision(workflow)
        rows = self._input_artifacts(workflow, task, execution=execution)
        spec_artifact = self._create_json_artifact(
            workflow,
            task,
            "asset-specification-v07",
            f".gamefactory/assets/{self.spec.asset_id}/r{revision.revision_number:03d}/specification.json",
            self.spec.model_dump(mode="json"),
            execution=execution,
        )
        self._current_attempt(workflow, task, execution)
        for row in rows.values():
            self.engine.artifact_mgr.verify_artifact_integrity(row)
        self._publish_attempt(
            workflow,
            task,
            execution,
            revision,
            [*rows.values(), spec_artifact],
        )
        return TaskHandlerResult(
            1,
            "Pinned local V0.7 assembly inputs and retained source package",
            [*(r.id for r in rows.values()), spec_artifact.id],
        )

    def concept_review(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        self._current_attempt(workflow, task, execution)
        approval = self._approval(workflow, task, "concept_review")
        rel = f".gamefactory/assets/{self.spec.asset_id}/r{self.revision_number:03d}/approvals/concept-{execution.id}.json"
        row = self._create_json_artifact(
            workflow,
            task,
            "assembly-concept-approval",
            rel,
            {
                "approval_id": approval.id,
                "approval_type": approval.approval_type,
                "status": approval.status.value,
                "operation_hash": approval.operation_hash,
                "actor": approval.actor,
            },
            execution=execution,
        )
        self._publish_attempt(workflow, task, execution, self._revision(workflow), [row])
        return TaskHandlerResult(1, "Human concept review approval recorded", [row.id])

    def source_review(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        self._current_attempt(workflow, task, execution)
        approval = self._approval(workflow, task, "source_review")
        scope = self._source_context(workflow, task)
        rel = f".gamefactory/assets/{self.spec.asset_id}/r{self.revision_number:03d}/approvals/source-{execution.id}.json"
        row = self._create_json_artifact(
            workflow,
            task,
            "assembly-source-approval",
            rel,
            {
                "approval_id": approval.id,
                "approval_type": approval.approval_type,
                "status": approval.status.value,
                "operation_hash": approval.operation_hash,
                "actor": approval.actor,
                "source_review": scope,
            },
            execution=execution,
        )
        self._publish_attempt(workflow, task, execution, self._revision(workflow), [row])
        return TaskHandlerResult(
            1, "Human authored-source semantics approved and cryptographically pinned", [row.id]
        )

    def process(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        self._current_attempt(workflow, task, execution)
        source_task = self._task(workflow, "asset_v07_assembly_source_review")
        self._approval(workflow, source_task, "source_review")
        package = self._package()
        source_scope = self._source_context(workflow, source_task)
        if source_scope["source_glb_sha256"] != self._revision(workflow).raw_glb_hash:
            raise ArtifactError("Retained source bytes changed after the human source approval")
        out_dir = (
            self.root
            / ".gamefactory"
            / "assets"
            / self.spec.asset_id
            / f"r{self.revision_number:03d}"
            / "process"
            / f"{execution.id}-a{execution.attempt_number}"
        )
        out_dir.mkdir(parents=True, exist_ok=False)
        processed = out_dir / "processed.glb"
        report_path = out_dir / "blender-report.json"
        adapter = self.adapters.blender or AssemblyProcessor()
        result = adapter.process_assembly(
            package.package_dir,
            self.spec,
            expected_provenance_sha256=self.provenance_sha256,
            processed_glb_path=processed,
            report_path=report_path,
        )
        self._current_attempt(workflow, task, execution)
        if (
            result.source_glb_sha256 != package.retained_glb_sha256
            or result.provenance_sha256 != package.retained_provenance_sha256
            or result.spec_fingerprint != spec_fingerprint(self.spec)
        ):
            raise ArtifactError(
                "Blender proof does not bind the current pinned source and specification"
            )
        script_path = AssemblyProcessor.get_script_path()
        if not script_path.is_file() or compute_sha256(script_path) != result.script_sha256:
            raise ArtifactError("Blender report script hash differs from the packaged script bytes")
        try:
            child_report = _read_json_artifact(report_path, "Blender processing report")
        except ArtifactError as exc:
            raise ArtifactError("Blender processing report is not valid JSON") from exc
        if (
            not isinstance(child_report, dict)
            or child_report.get("status") != "SUCCESS"
            or child_report.get("asset_id") != self.spec.asset_id
            or child_report.get("spec_fingerprint") != spec_fingerprint(self.spec)
            or child_report.get("source_glb_sha256") != package.retained_glb_sha256
            or child_report.get("provenance_sha256") != package.retained_provenance_sha256
            or child_report.get("output_glb_sha256") != result.processed_glb_sha256
            or child_report.get("processing_script_sha256") != result.script_sha256
        ):
            raise ArtifactError(
                "Blender report bytes do not bind current source/spec/script/output"
            )
        base = out_dir.relative_to(self.root).as_posix()
        glb_row = self._artifact(
            workflow,
            task,
            "processed-assembly-glb",
            f"{base}/processed.glb",
            execution=execution,
        )
        report_row = self._artifact(
            workflow,
            task,
            "assembly-blender-report",
            f"{base}/blender-report.json",
            execution=execution,
        )
        revision = self._revision(workflow)
        revision.processed_glb_hash = result.processed_glb_sha256
        self._publish_attempt(workflow, task, execution, revision, [glb_row, report_row])
        return TaskHandlerResult(
            1,
            "Blender processed assembly and independently verified source preservation",
            [glb_row.id, report_row.id],
        )

    def _task(self, workflow: Workflow, task_type: str) -> Task:
        matches = [t for t in self.tasks.list_by_workflow(workflow.id) if t.task_type == task_type]
        if len(matches) != 1:
            raise ArtifactError(f"Workflow must contain exactly one '{task_type}' task")
        return cast(Task, matches[0])

    def _processed(self, workflow: Workflow) -> tuple[Artifact, Path]:
        task = self._task(workflow, "asset_v07_assembly_process")
        latest = self.executions.get_latest_attempt(task.id)
        if latest is None or latest.status != ExecutionStatus.COMPLETED:
            raise ArtifactError("Validation requires the latest successful Blender process attempt")
        rows = [
            a
            for a in self.artifacts.list_by_task(task.id)
            if a.artifact_type == "processed-assembly-glb"
            and latest.id in a.relative_path
            and f"-a{latest.attempt_number}" in a.relative_path
        ]
        if len(rows) != 1:
            raise ArtifactError("Current Blender attempt has no unique processed GLB artifact")
        self.engine.artifact_mgr.verify_artifact_integrity(rows[0])
        return rows[0], self.root / rows[0].relative_path

    def validate(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        self._current_attempt(workflow, task, execution)
        package = self._package()
        processed_row, processed_path = self._processed(workflow)
        obs = VerifiedSourceNormalization(
            source_sha256=package.retained_glb_sha256,
            processed_sha256=processed_row.content_hash,
            source_front=package.provenance.source_front,
            normalization_applied=package.provenance.source_front == "+Z",
            root_rotation_xyzw=(0.0, 1.0, 0.0, 0.0)
            if package.provenance.source_front == "+Z"
            else (0.0, 0.0, 0.0, 1.0),
            source_glb_bytes=_read_bounded(
                package.retained_glb_path, _MAX_SOURCE_GLB_BYTES, "Retained source GLB"
            ),
        )
        if hashlib.sha256(obs.source_glb_bytes).hexdigest() != package.retained_glb_sha256:
            raise ArtifactError("Retained source GLB changed during bounded observation read")
        preservation = verify_source_to_processed_preservation(obs, processed_path, self.spec)
        if not preservation.passed:
            raise ValidationError("Independent source-to-processed preservation proof failed")
        result: AssetValidationResult = validate_glb_v07(
            processed_path, self.spec, source_observation=obs
        )
        if not result.passed:
            raise ValidationError(
                "V0.7 assembly geometry validation failed",
                details={"findings": [f.to_dict() for f in result.findings]},
            )
        self._current_attempt(workflow, task, execution)
        report = result.to_dict()
        report.update(
            {
                "source_glb_sha256": package.retained_glb_sha256,
                "processed_glb_sha256": processed_row.content_hash,
                "preservation_passed": preservation.passed,
                "profile_id": self.profile.profile_id,
                "profile_version": self.profile.version,
                "profile_document_sha256": _json_hash(
                    self.profile.document.model_dump(mode="json")
                ),
            }
        )
        relative = f".gamefactory/assets/{self.spec.asset_id}/r{self.revision_number:03d}/validation/{execution.id}-a{execution.attempt_number}.json"
        row = self._create_json_artifact(
            workflow,
            task,
            "assembly-validation-report",
            relative,
            report,
            execution=execution,
        )
        revision = self._revision(workflow)
        revision.validation_report_hash = row.content_hash
        self._publish_attempt(workflow, task, execution, revision, [row])
        return TaskHandlerResult(
            1,
            "Independent V0.7 geometry, source preservation, and policy validation passed",
            [row.id],
        )

    def godot(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        self._current_attempt(workflow, task, execution)
        revision = self._revision(workflow)
        processed_row, processed_path = self._processed(workflow)
        validation_task = self._task(workflow, "asset_v07_assembly_validate")
        validation_latest = self.executions.get_latest_attempt(validation_task.id)
        if validation_latest is None or validation_latest.status != ExecutionStatus.COMPLETED:
            raise ArtifactError("Godot runtime requires the latest successful geometry validation")
        validations = [
            a
            for a in self.artifacts.list_by_task(validation_task.id)
            if a.artifact_type == "assembly-validation-report"
            and validation_latest.id in a.relative_path
        ]
        if len(validations) != 1 or validations[0].content_hash != revision.validation_report_hash:
            raise ArtifactError(
                "Geometry validation report is stale or does not match this revision"
            )
        out_dir = (
            self.root
            / ".gamefactory"
            / "assets"
            / self.spec.asset_id
            / f"r{self.revision_number:03d}"
            / "runtime"
            / f"{execution.id}-a{execution.attempt_number}"
        )
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        result = self.adapters.godot_verifier(
            self.spec,
            self.profile,
            processed_path,
            processed_row.content_hash,
            execution.id,
            attempt_number=execution.attempt_number,
            workflow_id=workflow.id,
            revision=self.revision_number,
            output_dir=out_dir,
            godot_executable=self.adapters.godot_executable,
            runner=self.adapters.godot_runner,
        )
        self._current_attempt(workflow, task, execution)
        if (
            result.status != "PASS"
            or result.processed_glb_sha256 != processed_row.content_hash
            or set(result.captures) != set(self.profile.review_views)
        ):
            raise ValidationError(
                "Godot runtime failed or did not return every current review capture"
            )
        runtime_artifacts = _runtime_artifact_payloads(result, self.profile.review_views)
        request_bytes = runtime_artifacts["runtime-request.json"]
        observation_bytes = runtime_artifacts["runtime-observation.json"]
        harness_bytes = runtime_artifacts["asset_runtime_harness_v07.gd"]
        try:
            request = json.loads(request_bytes)
            observation = json.loads(observation_bytes)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ArtifactError(
                "Godot runtime request or observation bytes are invalid JSON"
            ) from exc
        if not isinstance(request, dict) or not isinstance(observation, dict):
            raise ArtifactError("Godot runtime request and observation must be JSON objects")
        request_digest = request.pop("request_digest", None)
        canonical_request_digest = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
        ).hexdigest()
        harness_sha256 = hashlib.sha256(harness_bytes).hexdigest()
        runtime_profile_hash = hashlib.sha256(
            json.dumps(
                self.profile.document.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        if (
            request_digest != result.request_digest
            or canonical_request_digest != result.request_digest
            or request.get("workflow_id") != workflow.id
            or request.get("revision") != revision.revision_number
            or request.get("execution_id") != execution.id
            or request.get("attempt_number") != execution.attempt_number
            or request.get("asset_id") != self.spec.asset_id
            or request.get("processed_glb_sha256") != processed_row.content_hash
            or request.get("specification_sha256") != revision.spec_hash
            or request.get("profile_sha256") != runtime_profile_hash
            or request.get("harness_sha256") != harness_sha256
            or observation != result.observation
            or request.get("schema_version") != "assembly-runtime-request-0.7.0"
            or observation.get("schema_version") != "assembly-runtime-observation-0.7.0"
            or observation.get("workflow_id") != workflow.id
            or observation.get("revision") != revision.revision_number
            or observation.get("execution_id") != execution.id
            or observation.get("attempt_number") != execution.attempt_number
            or observation.get("asset_id") != self.spec.asset_id
            or observation.get("processed_glb_sha256") != processed_row.content_hash
            or observation.get("request_digest") != result.request_digest
            or observation.get("harness_sha256") != harness_sha256
            or observation.get("profile_sha256") != runtime_profile_hash
            or observation.get("specification_sha256") != request.get("specification_sha256")
            or observation.get("status") != "PASS"
            or observation.get("errors") != []
        ):
            raise ArtifactError("Godot bytes do not independently bind the current runtime attempt")
        capture_hashes: dict[str, str] = {}
        candidates: list[Artifact] = []
        review_rel = f".gamefactory/assets/{self.spec.asset_id}/r{self.revision_number:03d}/review/{execution.id}-a{execution.attempt_number}"
        for view in self.profile.review_views:
            self._current_attempt(workflow, task, execution)
            payload = result.captures[view]
            digest = hashlib.sha256(payload).hexdigest()
            observed_captures = observation.get("captures")
            if (
                not isinstance(observed_captures, dict)
                or not isinstance(observed_captures.get(view), dict)
                or observed_captures[view].get("sha256") != digest
            ):
                raise ArtifactError(f"Godot observation does not bind the {view} review capture")
            relative = f"{review_rel}/{view}.png"
            capture = self._bytes_artifact(
                workflow,
                task,
                "assembly-review-capture",
                relative,
                payload,
            )
            if capture.content_hash != digest:
                raise ArtifactError("Capture hash changed while registering Godot review image")
            candidates.append(capture)
            capture_hashes[view] = digest
        request_row = self._bytes_artifact(
            workflow,
            task,
            "assembly-runtime-request",
            f".gamefactory/assets/{self.spec.asset_id}/r{self.revision_number:03d}/runtime/{execution.id}-a{execution.attempt_number}/retained/runtime-request.json",
            request_bytes,
        )
        observation_row = self._bytes_artifact(
            workflow,
            task,
            "assembly-runtime-observation",
            f".gamefactory/assets/{self.spec.asset_id}/r{self.revision_number:03d}/runtime/{execution.id}-a{execution.attempt_number}/retained/runtime-observation.json",
            observation_bytes,
        )
        harness_row = self._bytes_artifact(
            workflow,
            task,
            "assembly-runtime-harness",
            f".gamefactory/assets/{self.spec.asset_id}/r{self.revision_number:03d}/runtime/{execution.id}-a{execution.attempt_number}/retained/asset_runtime_harness_v07.gd",
            harness_bytes,
        )
        candidates.extend((request_row, observation_row, harness_row))
        index = {
            "schema_version": 1,
            "status": result.status,
            "workflow_id": workflow.id,
            "execution_id": execution.id,
            "attempt_number": execution.attempt_number,
            "processed_glb_sha256": processed_row.content_hash,
            "request_digest": result.request_digest,
            "runtime_request_sha256": request_row.content_hash,
            "runtime_observation_sha256": observation_row.content_hash,
            "runtime_harness_sha256": harness_row.content_hash,
            "observation": result.observation,
            "capture_sha256": capture_hashes,
        }
        relative = f".gamefactory/assets/{self.spec.asset_id}/r{self.revision_number:03d}/runtime/{execution.id}-a{execution.attempt_number}/runtime-index.json"
        row = self._create_json_artifact(
            workflow,
            task,
            "assembly-runtime-index",
            relative,
            index,
            execution=execution,
        )
        candidates.append(row)
        revision.runtime_evidence_hashes.extend(capture_hashes.values())
        self._publish_attempt(workflow, task, execution, revision, candidates)
        return TaskHandlerResult(
            1,
            "Godot verified the current processed assembly and all profile review views",
            [*(artifact.id for artifact in candidates)],
        )

    def final_review(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        self._current_attempt(workflow, task, execution)
        approval = self._approval(workflow, task, "final_visual_review")
        scope = self._final_context(workflow, task)
        relative = f".gamefactory/assets/{self.spec.asset_id}/r{self.revision_number:03d}/approvals/final-{execution.id}.json"
        row = self._create_json_artifact(
            workflow,
            task,
            "assembly-final-approval",
            relative,
            {
                "approval_id": approval.id,
                "status": approval.status.value,
                "operation_hash": approval.operation_hash,
                "actor": approval.actor,
                "review_context": scope,
            },
            execution=execution,
        )
        self._publish_attempt(workflow, task, execution, self._revision(workflow), [row])
        return TaskHandlerResult(
            1, "Human final visual review bound to current successful Godot captures", [row.id]
        )

    def evidence_pending(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        self._current_attempt(workflow, task, execution)
        revision = self._revision(workflow)
        from gamefactory.workflows.assembly_evidence import export_current_workflow_evidence

        relative_destination = (
            f".gamefactory/assets/{self.spec.asset_id}/r{revision.revision_number:03d}/evidence/"
            f"{execution.id}-a{execution.attempt_number}"
        )
        destination = PathGuard(self.root).ensure_safe_parent(relative_destination)
        if destination.exists() or destination.is_symlink():
            raise ArtifactError("Evidence output path already exists; refusing to overwrite it")
        result = export_current_workflow_evidence(self, workflow, task, execution, destination)
        if result.get("status") != "PASS" or result.get("product_ready") is not True:
            raise ValidationError(
                "Cold assembly evidence export did not pass both readiness checks"
            )
        manifest_path = destination / "manifest.json"
        row = self.engine.artifact_mgr.register_file_artifact(
            workflow.id,
            task.id,
            "assembly-evidence-manifest",
            "cold_verified_local_assembly_bundle",
            manifest_path.relative_to(self.root).as_posix(),
        )
        self._publish_attempt(workflow, task, execution, self._revision(workflow), [row])
        return TaskHandlerResult(
            1,
            "Local assembly evidence bundle passed the independent cold verifier",
            [row.id],
        )


class _AssemblyHandlerRouter:
    """Route one engine task type to the per-workflow immutable handler binding."""

    def __init__(self) -> None:
        self.by_workflow: dict[str, AssemblyProductionHandlers] = {}

    def add(self, workflow_id: str, handlers: AssemblyProductionHandlers) -> None:
        self.by_workflow.setdefault(workflow_id, handlers)

    def dispatch(
        self, method: str, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        handler = self.by_workflow.get(workflow.id)
        if handler is None:
            raise ValidationError("No V0.7 assembly binding is registered for this workflow")
        return cast(TaskHandlerResult, getattr(handler, method)(workflow, task, execution))

    def approval_context(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        handler = self.by_workflow.get(workflow.id)
        if handler is None:
            raise ValidationError("No V0.7 assembly binding is registered for this workflow")
        return handler.approval_context(workflow, task)


_ASSEMBLY_ROUTERS: weakref.WeakKeyDictionary[TaskHandlerRegistry, _AssemblyHandlerRouter] = (
    weakref.WeakKeyDictionary()
)


def verify_completed_local_assembly_evidence(
    engine: WorkflowEngine, workflow_id: str, manifest_artifact: Artifact
) -> dict[str, Any]:
    """Read-only revalidate a completed workflow's current evidence and bundle."""
    workflow = engine.wf_repo.get(workflow_id)
    if workflow is None or workflow.status != WorkflowStatus.COMPLETED:
        raise ArtifactError("Completed evidence revalidation requires a completed workflow")
    tasks = engine.task_repo.list_by_workflow(workflow_id)
    evidence_tasks = [task for task in tasks if task.task_type == "asset_v07_assembly_evidence"]
    manifests = [
        row
        for row in engine.art_repo.list_by_workflow(workflow_id)
        if row.artifact_type == "assembly-evidence-manifest"
    ]
    if (
        len(evidence_tasks) != 1
        or len(manifests) != 1
        or manifests[0].id != manifest_artifact.id
        or manifest_artifact.task_id != evidence_tasks[0].id
        or evidence_tasks[0].status != TaskStatus.COMPLETED
    ):
        raise ArtifactError("Completed workflow has no unique current evidence manifest")
    execution = engine.exec_repo.get_latest_attempt(evidence_tasks[0].id)
    if execution is None or execution.status != ExecutionStatus.COMPLETED:
        raise ArtifactError("Latest evidence attempt is not successfully completed")
    router = _ASSEMBLY_ROUTERS.get(engine.handler_registry)
    handler = router.by_workflow.get(workflow_id) if router is not None else None
    if handler is None:
        raise ArtifactError("Completed workflow's authoritative assembly handler is unavailable")
    return handler.verify_completed_evidence_bundle(
        workflow, evidence_tasks[0], execution, manifest_artifact
    )


def _task_id(workflow_id: str, suffix: str) -> str:
    return f"{workflow_id}-{suffix.upper()}"


def _build_tasks(workflow_id: str, parameters: dict[str, Any]) -> tuple[Task, ...]:
    tasks: list[Task] = []
    previous: str | None = None
    for stage, task_type, _ in _STAGES:
        task = Task(
            id=_task_id(workflow_id, stage),
            workflow_id=workflow_id,
            name=f"V0.7 assembly {stage.replace('_', ' ')}",
            task_type=task_type,
            cost_class=CostClass.LOCAL,
            depends_on=[previous] if previous else [],
            parameters=dict(parameters),
            max_retries=1,
            timeout_seconds=900.0,
        )
        tasks.append(task)
        previous = task.id
    return tuple(tasks)


def _bind_handlers(
    engine: Any,
    workflow: Workflow,
    revision: AssetRevision,
    spec: AssetSpecificationV07,
    profile: AssetProfileV07,
    package_dir: Path,
    provenance_sha256: str,
    concept: Path,
    concept_meta: Path,
    concept_relative: str,
    concept_meta_relative: str,
    adapters: AssemblyAdapters,
) -> None:
    handlers = AssemblyProductionHandlers(
        engine,
        spec,
        profile,
        package_dir,
        provenance_sha256,
        concept,
        concept_meta,
        concept_relative,
        concept_meta_relative,
        adapters,
    )
    handlers.bind(workflow, revision)
    register_assembly_production_handlers(engine.handler_registry, handlers)


def register_assembly_production_handlers(
    registry: TaskHandlerRegistry, handlers: AssemblyProductionHandlers
) -> None:
    """Register V0.7 task handlers with engine-owned approval and recovery gates."""
    router = _ASSEMBLY_ROUTERS.get(registry)
    if router is None:
        router = _AssemblyHandlerRouter()
        _ASSEMBLY_ROUTERS[registry] = router
    router.add(handlers.workflow_id, handlers)

    def route(method: str) -> Callable[[Workflow, Task, Execution], TaskHandlerResult]:
        return lambda workflow, task, execution: router.dispatch(method, workflow, task, execution)

    task_types = [task_type for _, task_type, _ in _STAGES]
    present = [registry.get(task_type) is not None for task_type in task_types]
    if any(present):
        if not all(present):
            raise ValidationError("Assembly handler registry is partially initialized")
        return
    registry.register("asset_v07_assembly_prepare", cast(TaskHandler, route("prepare")))
    registry.register(
        "asset_v07_assembly_concept_review",
        cast(TaskHandler, route("concept_review")),
        TaskHandlerMetadata(
            mandatory_approval_type="concept_review",
            approval_context=router.approval_context,
            changes_requested_blocks=lambda task: True,
        ),
    )
    registry.register(
        "asset_v07_assembly_source_review",
        cast(TaskHandler, route("source_review")),
        TaskHandlerMetadata(
            mandatory_approval_type="source_review",
            approval_context=router.approval_context,
        ),
    )
    registry.register(
        "asset_v07_assembly_process",
        cast(TaskHandler, route("process")),
        TaskHandlerMetadata(
            operation=HandlerOperation.PROCESS_EXECUTION,
            recovery=HandlerRecovery.CONSERVATIVE_PROCESS,
        ),
    )
    registry.register("asset_v07_assembly_validate", cast(TaskHandler, route("validate")))
    registry.register(
        "asset_v07_assembly_godot",
        cast(TaskHandler, route("godot")),
        TaskHandlerMetadata(
            operation=HandlerOperation.PROCESS_EXECUTION,
            recovery=HandlerRecovery.CONSERVATIVE_PROCESS,
        ),
    )
    registry.register(
        "asset_v07_assembly_final_review",
        cast(TaskHandler, route("final_review")),
        TaskHandlerMetadata(
            mandatory_approval_type="final_visual_review",
            approval_context=router.approval_context,
        ),
    )
    registry.register("asset_v07_assembly_evidence", cast(TaskHandler, route("evidence_pending")))


def create_local_assembly_workflow(
    engine: Any,
    project_id: str,
    spec: AssetSpecificationV07,
    profile: AssetProfileV07,
    package_dir: Path | str,
    *,
    expected_provenance_sha256: str,
    concept_image: Path | str,
    concept_provenance: Path | str,
    workflow_id: str | None = None,
    adapters: AssemblyAdapters | None = None,
) -> LocalAssemblyWorkflow:
    """Validate and create a durable, local-only, human-gated V0.7 assembly DAG.

    The source package must already be complete and have a trusted external pin.
    The function authenticates every input before allocating SQLite state.
    """
    _same_profile(spec, profile)
    project = engine.proj_repo.get(project_id)
    if project is None:
        raise ValidationError(f"Project '{project_id}' is not registered")
    root = Path(project.root_path).resolve(strict=True)
    if root != engine.project_root.resolve(strict=True):
        raise ValidationError("Workflow engine project root does not match registered project root")
    package_path = Path(package_dir)
    if not package_path.is_absolute():
        package_path = root / package_path
    package = verify_retained_assembly(
        package_path,
        spec,
        expected_provenance_sha256=expected_provenance_sha256,
        managed_root=root,
    )
    concept, concept_relative = _contained_file(root, concept_image, "Concept image")
    concept_meta, concept_meta_relative = _contained_file(
        root, concept_provenance, "Concept provenance"
    )
    if concept.suffix.lower() != ".png":
        raise ValidationError("Concept image must be a PNG")
    concept_bytes = _read_bounded(concept, _MAX_CONCEPT_BYTES, "Concept PNG")
    _validate_concept_png(concept_bytes)
    concept_provenance_bytes = _read_bounded(
        concept_meta, _MAX_CONCEPT_PROVENANCE_BYTES, "Concept provenance"
    )
    _parse_concept_provenance(concept_provenance_bytes)

    wf_id = workflow_id or generate_id("WF-V07-ASSEMBLY")
    if not wf_id or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in wf_id
    ):
        raise ValidationError("workflow_id must be a safe identifier")
    spec_hash = spec_fingerprint(spec)
    concept_hash = hashlib.sha256(concept_bytes).hexdigest()
    concept_provenance_hash = hashlib.sha256(concept_provenance_bytes).hexdigest()
    publication_marker_hash = _publication_marker_hash(package)
    workflow = Workflow(
        wf_id, project_id, f"Local assembly: {spec.asset_id}", WorkflowStatus.PENDING
    )
    package_relative = package.package_dir.relative_to(root).as_posix()

    def task_factory(revision: AssetRevision) -> tuple[Task, ...]:
        parameters = _task_parameters(
            wf_id,
            revision,
            spec,
            profile,
            package,
            package_relative,
            concept_hash,
            concept_meta,
            concept_meta_relative,
            concept_relative,
            expected_provenance_sha256,
            concept_provenance_hash,
            publication_marker_hash,
        )
        return _build_tasks(wf_id, parameters)

    revision, allocated_tasks, _created = AssemblyGraphRepository(engine.db).create_graph(
        workflow=workflow,
        asset_id=spec.asset_id,
        spec_hash=spec_hash,
        profile_id=profile.profile_id,
        profile_version=profile.version,
        concept_hash=concept_hash,
        raw_glb_hash=package.retained_glb_sha256,
        task_factory=task_factory,
    )
    persisted_workflow = engine.wf_repo.get(wf_id)
    if persisted_workflow is None:
        raise ValidationError("Assembly graph committed without a retrievable workflow")
    tasks = tuple(allocated_tasks)

    _bind_handlers(
        engine,
        persisted_workflow,
        revision,
        spec,
        profile,
        package.package_dir,
        expected_provenance_sha256,
        concept,
        concept_meta,
        concept_relative,
        concept_meta_relative,
        adapters or AssemblyAdapters(),
    )
    return LocalAssemblyWorkflow(persisted_workflow, tasks, revision)


def _task_parameters(
    workflow_id: str,
    revision: AssetRevision,
    spec: AssetSpecificationV07,
    profile: AssetProfileV07,
    package: AssemblyIngestResult,
    package_relative: str,
    concept_hash: str,
    concept_provenance: Path,
    concept_provenance_relative: str,
    concept_relative: str,
    provenance_sha256: str,
    concept_provenance_sha256: str,
    publication_marker_sha256: str,
) -> dict[str, Any]:
    return {
        "graph_version": _GRAPH_VERSION,
        "workflow_id": workflow_id,
        "asset_id": spec.asset_id,
        "source_version": revision.revision_number,
        "specification": spec.model_dump(mode="json"),
        "specification_sha256": revision.spec_hash,
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "profile_document_sha256": _json_hash(profile.document.model_dump(mode="json")),
        "source_package": package_relative,
        "source_glb_sha256": package.retained_glb_sha256,
        "source_provenance_sha256": provenance_sha256.lower(),
        "source_publication_marker_sha256": publication_marker_sha256,
        "concept_image": concept_relative,
        "concept_image_sha256": concept_hash,
        "concept_provenance": concept_provenance_relative,
        "concept_provenance_sha256": concept_provenance_sha256,
    }
