"""Atomic allocation and registration for the paid V0.7 character graph.

The task factory is a pure constructor. Revision allocation, workflow creation,
all ten immutable task records, and the registration audit commit together.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.repositories import (
    _insert_audit_event,
    _validate_task_parameters,
)
from gamefactory.core.domain.asset_contracts import AssetRevision
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.models import (
    AuditEvent,
    CostClass,
    Task,
    TaskStatus,
    Workflow,
    WorkflowStatus,
    generate_id,
    utc_now_iso,
)

_GRAPH_VERSION = "0.7.0"
_STAGES = (
    ("PREPARE", "asset_prepare", CostClass.LOCAL, None, 60.0),
    ("CONCEPT-REVIEW", "asset_concept_review", CostClass.LOCAL, "PREPARE", 60.0),
    ("PAID-REQUEST", "asset_paid_request_snapshot", CostClass.LOCAL, "CONCEPT-REVIEW", 60.0),
    ("READINESS", "asset_production_readiness", CostClass.LOCAL, "PAID-REQUEST", 60.0),
    ("PAID-GENERATION", "asset_paid_generation", CostClass.PAID, "READINESS", 60.0),
    ("PROCESS", "asset_process", CostClass.LOCAL, "PAID-GENERATION", 60.0),
    ("VALIDATE", "asset_validate", CostClass.LOCAL, "PROCESS", 60.0),
    ("GODOT", "asset_godot", CostClass.LOCAL, "VALIDATE", 180.0),
    ("FINAL-REVIEW", "asset_final_review", CostClass.LOCAL, "GODOT", 60.0),
    ("PROVIDER-EVIDENCE", "asset_v07_provider_evidence", CostClass.LOCAL, "FINAL-REVIEW", 60.0),
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_COMMON_FIELDS = (
    "graph_version",
    "workflow_id",
    "asset_id",
    "revision_number",
    "specification",
    "specification_hash",
    "concept_source",
    "concept_provenance_source",
    "concept_source_hash",
    "concept_provenance_hash",
    "concept_source_type",
    "provider",
    "provider_estimate",
    "budget_reservation",
    "paid_reservation",
    "cost_unit",
    "asset_dir",
    "profile_id",
    "profile_version",
    "profile_qualified",
    "profile_schema",
    "profile_document",
    "profile_document_hash",
    "geometry_mode",
    "profile_source_kinds",
    "profile_assembly",
    "source_kind",
    "category",
    "collider_policy",
)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError("Character graph payload is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _strict_json(raw: str, label: str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite number: {value}")

    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
    except (TypeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ValidationError(f"Persisted character graph {label} is malformed") from exc


@dataclass(frozen=True)
class CharacterGraphRepository:
    """Persist an exact paid character DAG without partial revision allocation."""

    db: Database

    def create_graph(
        self,
        *,
        workflow: Workflow,
        asset_id: str,
        spec_hash: str,
        profile_id: str,
        profile_version: int,
        concept_hash: str,
        task_factory: Callable[[AssetRevision], Sequence[Task]],
    ) -> tuple[AssetRevision, list[Task], bool]:
        self._validate_request(
            workflow, asset_id, spec_hash, profile_id, profile_version, concept_hash
        )
        result: tuple[AssetRevision, list[Task], bool]
        committed_name: str
        committed_status: WorkflowStatus
        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if (
                conn.execute(
                    "SELECT 1 FROM projects WHERE id = ?", (workflow.project_id,)
                ).fetchone()
                is None
            ):
                raise ValidationError("Character graph project is not registered")
            saved_workflow = conn.execute(
                "SELECT * FROM workflows WHERE id = ?", (workflow.id,)
            ).fetchone()
            if saved_workflow is not None:
                revision, tasks = self._load_exact_retry(
                    conn,
                    saved_workflow,
                    workflow,
                    asset_id,
                    spec_hash,
                    profile_id,
                    profile_version,
                    concept_hash,
                    task_factory,
                )
                committed_name = saved_workflow["name"]
                try:
                    committed_status = WorkflowStatus(saved_workflow["status"])
                except ValueError as exc:
                    raise ValidationError("Saved character workflow status is invalid") from exc
                result = (revision, tasks, False)
            else:
                self._reject_orphan_identity(conn, workflow.id)
                maximum = conn.execute(
                    "SELECT COALESCE(MAX(revision_number), 0) FROM asset_revisions WHERE asset_id = ?",
                    (asset_id,),
                ).fetchone()[0]
                if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
                    raise ValidationError("Character graph revision sequence is corrupt")
                revision = AssetRevision(
                    asset_id=asset_id,
                    revision_number=maximum + 1,
                    workflow_id=workflow.id,
                    spec_hash=spec_hash,
                    profile_id=profile_id,
                    profile_version=profile_version,
                    concept_hash=concept_hash,
                )
                tasks = self._make_and_validate_tasks(task_factory, revision, workflow)
                now = utc_now_iso()
                committed_name = workflow.name
                committed_status = WorkflowStatus.PENDING
                conn.execute(
                    "INSERT INTO workflows (id, project_id, name, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        workflow.id,
                        workflow.project_id,
                        workflow.name,
                        WorkflowStatus.PENDING.value,
                        workflow.created_at or now,
                        workflow.updated_at or now,
                    ),
                )
                conn.execute(
                    "INSERT INTO asset_revisions (asset_id, revision_number, workflow_id, spec_hash, "
                    "concept_hash, raw_glb_hash, processed_glb_hash, validation_report_hash, "
                    "runtime_evidence_hashes_json, created_at, updated_at, profile_id, profile_version) "
                    "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, '[]', ?, ?, ?, ?)",
                    (
                        revision.asset_id,
                        revision.revision_number,
                        revision.workflow_id,
                        revision.spec_hash,
                        revision.concept_hash,
                        revision.created_at,
                        revision.updated_at,
                        revision.profile_id,
                        revision.profile_version,
                    ),
                )
                self._insert_tasks(conn, tasks)
                _insert_audit_event(
                    conn,
                    AuditEvent(
                        id=generate_id("AUDIT"),
                        entity_type="Workflow",
                        entity_id=workflow.id,
                        action="REGISTERED",
                        actor="System",
                        timestamp=now,
                        details={"task_count": len(_STAGES)},
                    ),
                )
                result = (revision, tasks, True)
        workflow.name = committed_name
        workflow.status = committed_status
        return result

    @staticmethod
    def _validate_request(
        workflow: Workflow,
        asset_id: str,
        spec_hash: str,
        profile_id: str,
        profile_version: int,
        concept_hash: str,
    ) -> None:
        if (
            not isinstance(workflow.id, str)
            or not _IDENTIFIER.fullmatch(workflow.id)
            or not isinstance(workflow.project_id, str)
            or not _IDENTIFIER.fullmatch(workflow.project_id)
            or workflow.status != WorkflowStatus.PENDING
        ):
            raise ValidationError("Character graph workflow/project identity is invalid")
        if not isinstance(asset_id, str) or not _IDENTIFIER.fullmatch(asset_id):
            raise ValidationError("Character graph asset identity is invalid")
        if not isinstance(profile_id, str) or not _IDENTIFIER.fullmatch(profile_id):
            raise ValidationError("Character graph profile identity is invalid")
        if type(profile_version) is not int or profile_version != 1:
            raise ValidationError(
                "Character graph supports only the trusted character profile version 1"
            )
        for label, digest in (("specification", spec_hash), ("concept", concept_hash)):
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise ValidationError(f"Character graph {label} pin must be a lowercase SHA-256")

    @classmethod
    def _make_and_validate_tasks(
        cls,
        task_factory: Callable[[AssetRevision], Sequence[Task]],
        revision: AssetRevision,
        workflow: Workflow,
    ) -> list[Task]:
        try:
            produced = task_factory(revision)
        except Exception as exc:
            raise ValidationError("Character graph task factory failed") from exc
        if not isinstance(produced, Sequence) or isinstance(produced, (str, bytes)):
            raise ValidationError("Character graph task factory must return a task sequence")
        tasks = list(produced)
        if len(tasks) != len(_STAGES) or any(not isinstance(item, Task) for item in tasks):
            raise ValidationError("Character graph must contain the complete ten-stage DAG")

        first_common: str | None = None
        stage_ids = {stage: f"{workflow.id}-{stage}" for stage, *_rest in _STAGES}
        for task, (stage, task_type, cost, dependency, timeout) in zip(tasks, _STAGES, strict=True):
            expected_id = stage_ids[stage]
            expected_dependency = [] if dependency is None else [stage_ids[dependency]]
            expected_retry = (
                10
                if task_type in {"asset_paid_request_snapshot", "asset_production_readiness"}
                else 1
            )
            if (
                task.id != expected_id
                or task.workflow_id != workflow.id
                or task.name != f"V0.7 character {stage.lower().replace('-', ' ')}"
                or task.task_type != task_type
                or task.cost_class != cost
                or task.status != TaskStatus.PENDING
                or task.depends_on != expected_dependency
                or type(task.max_retries) is not int
                or task.max_retries != expected_retry
                or type(task.timeout_seconds) not in (int, float)
                or isinstance(task.timeout_seconds, bool)
                or task.timeout_seconds != timeout
            ):
                raise ValidationError("Character graph differs from its canonical task DAG")
            _validate_task_parameters(task)
            if any(key not in task.parameters for key in _COMMON_FIELDS):
                raise ValidationError("Character graph task is missing an immutable common binding")
            common = {key: task.parameters[key] for key in _COMMON_FIELDS}
            common_json = _canonical_json(common)
            if first_common is None:
                first_common = common_json
            elif common_json != first_common:
                raise ValidationError(
                    "Character graph tasks have conflicting shared immutable pins"
                )
            cls._validate_revision_binding(task.parameters, workflow, revision)
        return tasks

    @staticmethod
    def _validate_revision_binding(
        params: dict[str, Any], workflow: Workflow, revision: AssetRevision
    ) -> None:
        specification = params.get("specification")
        profile = params.get("profile_document")
        if not isinstance(specification, dict) or not isinstance(profile, dict):
            raise ValidationError(
                "Character graph requires typed specification and profile documents"
            )
        review_views = profile.get("review_views")
        processing = profile.get("processing")
        godot = profile.get("godot")
        profile_version = params.get("profile_version")
        if (
            params.get("graph_version") != _GRAPH_VERSION
            or params.get("workflow_id") != workflow.id
            or params.get("asset_id") != revision.asset_id
            or type(params.get("revision_number")) is not int
            or params.get("revision_number") != revision.revision_number
            or params.get("specification_hash") != revision.spec_hash
            or params.get("profile_id") != revision.profile_id
            or type(profile_version) is not int
            or profile_version != revision.profile_version
            or params.get("concept_source_hash") != revision.concept_hash
            or params.get("source_kind") != "provider_generated"
            or params.get("category") != "character"
            or params.get("collider_policy") != "capsule"
            or params.get("geometry_mode") != "single_mesh"
            or params.get("profile_source_kinds") != ["provider_generated"]
            or params.get("profile_assembly") is not None
            or params.get("profile_schema") != "asset-profile-0.7.0"
            or type(specification.get("profile_version")) is not int
            or specification.get("profile_version") != revision.profile_version
            or specification.get("schema_version") != "0.7.0"
            or specification.get("asset_id") != revision.asset_id
            or specification.get("profile") != revision.profile_id
            or specification.get("category") != "character"
            or specification.get("source_kind") != "provider_generated"
            or specification.get("parts") is not None
            or specification.get("sockets") is not None
            or not isinstance(specification.get("collider"), dict)
            or specification["collider"].get("policy") != "capsule"
            or specification["collider"].get("capsule") is None
            or profile.get("schema_version") != "asset-profile-0.7.0"
            or profile.get("profile_id") != revision.profile_id
            or type(profile.get("version")) is not int
            or profile.get("version") != revision.profile_version
            or "character" not in profile.get("categories", [])
            or profile.get("geometry_mode") != "single_mesh"
            or profile.get("accepted_source_kinds") != ["provider_generated"]
            or profile.get("assembly") is not None
            or not isinstance(review_views, list)
            or len(review_views) not in {5, 9}
            or any(not isinstance(view, str) or not view for view in review_views)
            or len(set(review_views)) != len(review_views)
            or not {
                "front",
                "rear",
                "left",
                "right",
                "three_quarter",
            }.issubset(set(review_views))
            or not isinstance(processing, dict)
            or processing.get("rig_forbidden") is not True
            or processing.get("animation_forbidden") is not True
            or not isinstance(godot, dict)
            or godot.get("body_kind") != "static_body"
            or godot.get("require_ray_hit") is not True
            or godot.get("require_area") is not False
            or params.get("profile_document_hash") != _digest(profile)
            or params.get("profile_qualified")
            != f"{revision.profile_id}@{revision.profile_version}"
            or params.get("asset_dir")
            != f".gamefactory/assets/{revision.asset_id}/r{revision.revision_number:03d}"
        ):
            raise ValidationError("Character graph task does not bind its canonical asset revision")
        if _digest(specification) != revision.spec_hash:
            raise ValidationError(
                "Character graph specification fingerprint does not match its revision"
            )
        try:
            from gamefactory.core.domain.asset_contracts import parse_asset_specification_v07
            from gamefactory.core.domain.asset_profiles import (
                AssetProfileV07,
                ProfileRegistry,
                parse_profile_document_v07,
            )

            trusted_profile = AssetProfileV07(parse_profile_document_v07(profile))
            typed_spec = parse_asset_specification_v07(
                specification,
                registry=ProfileRegistry(
                    available=(), unsupported=(), available_v07=(trusted_profile,)
                ),
            )
            if typed_spec.bound_profile() != trusted_profile:
                raise ValueError("specification profile differs from its pinned document")
        except Exception as exc:
            raise ValidationError(
                "Character graph contains an invalid typed profile/specification"
            ) from exc
        for label in ("concept_source_hash", "concept_provenance_hash"):
            if not isinstance(params.get(label), str) or not _SHA256.fullmatch(params[label]):
                raise ValidationError(f"Character graph {label} is invalid")
        if not isinstance(params.get("concept_source"), str) or not params["concept_source"]:
            raise ValidationError("Character graph concept path is missing")
        if (
            not isinstance(params.get("concept_provenance_source"), str)
            or not params["concept_provenance_source"]
        ):
            raise ValidationError("Character graph concept provenance path is missing")
        if params.get("provider") not in {"fake", "meshy"}:
            raise ValidationError("Character graph provider binding is unsupported")
        if (
            not isinstance(params.get("concept_source_type"), str)
            or not params["concept_source_type"]
        ):
            raise ValidationError("Character graph concept source type is missing")
        if not isinstance(params.get("cost_unit"), str) or not params["cost_unit"]:
            raise ValidationError("Character graph provider cost unit is invalid")
        for key in ("provider_estimate", "budget_reservation", "paid_reservation"):
            value = params.get(key)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
            ):
                raise ValidationError(f"Character graph {key} must be non-negative numeric data")
        if not isinstance(params.get("profile_document_hash"), str) or not _SHA256.fullmatch(
            params["profile_document_hash"]
        ):
            raise ValidationError("Character graph profile document digest is invalid")

    def _load_exact_retry(
        self,
        conn: sqlite3.Connection,
        saved_workflow: sqlite3.Row,
        requested_workflow: Workflow,
        asset_id: str,
        spec_hash: str,
        profile_id: str,
        profile_version: int,
        concept_hash: str,
        task_factory: Callable[[AssetRevision], Sequence[Task]],
    ) -> tuple[AssetRevision, list[Task]]:
        if saved_workflow["project_id"] != requested_workflow.project_id:
            raise ValidationError("Character workflow ID already belongs to another project")
        revisions = conn.execute(
            "SELECT * FROM asset_revisions WHERE workflow_id = ?", (requested_workflow.id,)
        ).fetchall()
        task_rows = conn.execute(
            "SELECT * FROM tasks WHERE workflow_id = ?", (requested_workflow.id,)
        ).fetchall()
        if len(revisions) != 1 or len(task_rows) != len(_STAGES):
            raise ValidationError("Existing character workflow is orphaned or incomplete")
        revision = self._row_to_revision(revisions[0])
        if saved_workflow["name"] != requested_workflow.name:
            raise ValidationError("Existing character workflow has a conflicting immutable name")
        if (
            revision.asset_id != asset_id
            or revision.workflow_id != requested_workflow.id
            or revision.spec_hash != spec_hash
            or revision.profile_id != profile_id
            or revision.profile_version != profile_version
            or revision.concept_hash != concept_hash
            or revision.raw_glb_hash is not None
        ):
            raise ValidationError("Existing character workflow has conflicting pinned inputs")
        expected = self._make_and_validate_tasks(task_factory, revision, requested_workflow)
        loaded = [self._row_to_task(row) for row in task_rows]
        saved_by_id = {task.id: task for task in loaded}
        if len(saved_by_id) != len(loaded):
            raise ValidationError("Existing character graph contains duplicate task identities")
        ordered = [saved_by_id.get(task.id) for task in expected]
        if any(task is None for task in ordered):
            raise ValidationError("Existing character graph omits canonical task identities")
        saved = [cast(Task, task) for task in ordered]
        if any(
            not self._same_immutable_task(left, right)
            for left, right in zip(saved, expected, strict=True)
        ):
            raise ValidationError("Existing character workflow differs from the canonical graph")
        audit_rows = conn.execute(
            "SELECT details_json FROM audit_events WHERE entity_type='Workflow' AND entity_id=? AND action='REGISTERED'",
            (requested_workflow.id,),
        ).fetchall()
        if len(audit_rows) != 1:
            raise ValidationError("Existing character workflow has an ambiguous registration audit")
        details = _strict_json(audit_rows[0]["details_json"], "registration audit")
        if _canonical_json(details) != _canonical_json({"task_count": len(_STAGES)}):
            raise ValidationError("Existing character registration audit does not match its graph")
        return revision, saved

    @staticmethod
    def _same_immutable_task(saved: Task, expected: Task) -> bool:
        return (
            saved.id == expected.id
            and saved.workflow_id == expected.workflow_id
            and saved.name == expected.name
            and saved.task_type == expected.task_type
            and saved.cost_class == expected.cost_class
            and saved.depends_on == expected.depends_on
            and _canonical_json(saved.parameters) == _canonical_json(expected.parameters)
            and type(saved.max_retries) is int
            and saved.max_retries == expected.max_retries
            and type(saved.timeout_seconds) in (int, float)
            and not isinstance(saved.timeout_seconds, bool)
            and saved.timeout_seconds == expected.timeout_seconds
        )

    @staticmethod
    def _reject_orphan_identity(conn: sqlite3.Connection, workflow_id: str) -> None:
        checks = (
            ("SELECT 1 FROM asset_revisions WHERE workflow_id = ? LIMIT 1", (workflow_id,)),
            ("SELECT 1 FROM tasks WHERE workflow_id = ? LIMIT 1", (workflow_id,)),
            (
                "SELECT 1 FROM audit_events WHERE entity_type='Workflow' AND entity_id=? LIMIT 1",
                (workflow_id,),
            ),
        )
        if any(conn.execute(sql, args).fetchone() is not None for sql, args in checks):
            raise ValidationError(
                "Workflow ID has orphaned graph state; refusing to reconstruct it"
            )

    @staticmethod
    def _insert_tasks(conn: sqlite3.Connection, tasks: list[Task]) -> None:
        for task in tasks:
            conn.execute(
                "INSERT INTO tasks (id, workflow_id, name, task_type, cost_class, depends_on_json, status, parameters_json, max_retries, timeout_seconds, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task.id,
                    task.workflow_id,
                    task.name,
                    task.task_type,
                    task.cost_class.value,
                    json.dumps(task.depends_on, allow_nan=False),
                    task.status.value,
                    _canonical_json(task.parameters),
                    task.max_retries,
                    task.timeout_seconds,
                    task.created_at,
                    task.updated_at,
                ),
            )

    @staticmethod
    def _row_to_revision(row: sqlite3.Row) -> AssetRevision:
        return AssetRevision(
            asset_id=row["asset_id"],
            revision_number=row["revision_number"],
            workflow_id=row["workflow_id"],
            spec_hash=row["spec_hash"],
            profile_id=row["profile_id"],
            profile_version=row["profile_version"],
            concept_hash=row["concept_hash"],
            raw_glb_hash=row["raw_glb_hash"],
            processed_glb_hash=row["processed_glb_hash"],
            validation_report_hash=row["validation_report_hash"],
            runtime_evidence_hashes=_strict_json(
                row["runtime_evidence_hashes_json"], "revision runtime hashes"
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @classmethod
    def _row_to_task(cls, row: sqlite3.Row) -> Task:
        params = _strict_json(row["parameters_json"], "task parameters")
        if not isinstance(params, dict):
            raise ValidationError("Persisted character graph task parameters are not an object")
        depends = _strict_json(row["depends_on_json"], "task dependencies")
        if not isinstance(depends, list) or any(not isinstance(item, str) for item in depends):
            raise ValidationError("Persisted character graph task dependencies are malformed")
        return Task(
            id=row["id"],
            workflow_id=row["workflow_id"],
            name=row["name"],
            task_type=row["task_type"],
            cost_class=CostClass(row["cost_class"]),
            depends_on=depends,
            status=TaskStatus(row["status"]),
            parameters=params,
            max_retries=row["max_retries"],
            timeout_seconds=row["timeout_seconds"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
