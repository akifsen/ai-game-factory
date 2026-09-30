"""Atomic registration for the canonical local V0.7 assembly task graph."""

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

_GRAPH_VERSION = "0.7.0-local-assembly"
_ASSEMBLY_STAGES = (
    ("prepare", "asset_v07_assembly_prepare"),
    ("concept_review", "asset_v07_assembly_concept_review"),
    ("source_review", "asset_v07_assembly_source_review"),
    ("process", "asset_v07_assembly_process"),
    ("validate", "asset_v07_assembly_validate"),
    ("godot", "asset_v07_assembly_godot"),
    ("final_review", "asset_v07_assembly_final_review"),
    ("evidence", "asset_v07_assembly_evidence"),
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WORKFLOW_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def _canonical_json(value: Any) -> str:
    """Serialize JSON without Python's loose bool/int equality semantics."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class AssemblyGraphRepository:
    """Persist a whole V0.7 graph and its revision allocation in one SQLite transaction.

    ``task_factory`` must be pure and bounded: it receives the next revision
    number chosen under the write lock and may only construct the canonical
    in-memory task records. It must not perform file, network, DCC, or provider
    operations.
    """

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
        raw_glb_hash: str,
        task_factory: Callable[[AssetRevision], Sequence[Task]],
    ) -> tuple[AssetRevision, list[Task], bool]:
        """Create a workflow/revision/eight-task graph atomically.

        Returns ``(revision, tasks, created)``. ``created`` is false only when
        this call safely rehydrates an exact previously committed graph after
        an in-memory registration interruption. Orphaned or conflicting rows
        fail closed and are never repaired or removed here.
        """
        self._validate_request(
            workflow, asset_id, spec_hash, profile_id, profile_version, concept_hash, raw_glb_hash
        )
        result: tuple[AssetRevision, list[Task], bool]
        committed_name: str
        committed_status: WorkflowStatus
        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project = conn.execute(
                "SELECT id FROM projects WHERE id = ?", (workflow.project_id,)
            ).fetchone()
            if project is None:
                raise ValidationError("Assembly graph project is not registered")

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
                    raw_glb_hash,
                    task_factory,
                )
                committed_name = saved_workflow["name"]
                committed_status = WorkflowStatus(saved_workflow["status"])
                result = (revision, tasks, False)
            else:
                self._reject_orphan_identity(conn, workflow.id)
                maximum = conn.execute(
                    "SELECT COALESCE(MAX(revision_number), 0) FROM asset_revisions WHERE asset_id = ?",
                    (asset_id,),
                ).fetchone()[0]
                revision = AssetRevision(
                    asset_id=asset_id,
                    revision_number=int(maximum) + 1,
                    workflow_id=workflow.id,
                    spec_hash=spec_hash,
                    profile_id=profile_id,
                    profile_version=profile_version,
                    concept_hash=concept_hash,
                    raw_glb_hash=raw_glb_hash,
                )
                tasks = self._make_and_validate_tasks(task_factory, revision, workflow)
                now = utc_now_iso()
                committed_name = f"Local assembly: {asset_id} {revision.revision_id}"
                committed_status = WorkflowStatus.PENDING
                conn.execute(
                    "INSERT INTO workflows (id, project_id, name, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        workflow.id,
                        workflow.project_id,
                        committed_name,
                        WorkflowStatus.PENDING.value,
                        workflow.created_at or now,
                        workflow.updated_at or now,
                    ),
                )
                conn.execute(
                    "INSERT INTO asset_revisions (asset_id, revision_number, workflow_id, spec_hash, "
                    "concept_hash, raw_glb_hash, processed_glb_hash, validation_report_hash, "
                    "runtime_evidence_hashes_json, created_at, updated_at, profile_id, profile_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, '[]', ?, ?, ?, ?)",
                    (
                        revision.asset_id,
                        revision.revision_number,
                        revision.workflow_id,
                        revision.spec_hash,
                        revision.concept_hash,
                        revision.raw_glb_hash,
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
                        details={"task_count": len(tasks)},
                    ),
                )
                result = (revision, tasks, True)

        # Reflect committed/recovered canonical fields to the caller only after
        # the database transaction has completed successfully.
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
        raw_glb_hash: str,
    ) -> None:
        if (
            not isinstance(workflow.id, str)
            or not workflow.id
            or not _WORKFLOW_ID.fullmatch(workflow.id)
            or not isinstance(workflow.project_id, str)
            or not workflow.project_id
        ):
            raise ValidationError("Assembly graph workflow/project identity is invalid")
        if workflow.status != WorkflowStatus.PENDING:
            raise ValidationError("New assembly graph must start in PENDING state")
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise ValidationError("Assembly graph asset_id is required")
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise ValidationError("Assembly graph profile_id is required")
        if (
            not isinstance(profile_version, int)
            or isinstance(profile_version, bool)
            or profile_version < 1
        ):
            raise ValidationError("Assembly graph profile_version must be a positive integer")
        for label, digest in (
            ("specification", spec_hash),
            ("concept", concept_hash),
            ("raw source", raw_glb_hash),
        ):
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise ValidationError(f"Assembly graph {label} pin must be a lowercase SHA-256")

    @staticmethod
    def _make_and_validate_tasks(
        task_factory: Callable[[AssetRevision], Sequence[Task]],
        revision: AssetRevision,
        workflow: Workflow,
    ) -> list[Task]:
        try:
            produced = task_factory(revision)
        except Exception as exc:
            raise ValidationError("Assembly graph task factory failed") from exc
        if not isinstance(produced, Sequence) or isinstance(produced, (str, bytes)):
            raise ValidationError("Assembly graph task factory must return a task sequence")
        tasks = list(produced)
        if len(tasks) != len(_ASSEMBLY_STAGES):
            raise ValidationError("Assembly graph must contain the complete canonical V0.7 DAG")
        if any(not isinstance(task, Task) for task in tasks):
            raise ValidationError("Assembly graph task factory returned a non-Task value")

        first_parameters: dict[str, Any] | None = None
        previous_id: str | None = None
        seen_ids: set[str] = set()
        for task, (stage, task_type) in zip(tasks, _ASSEMBLY_STAGES, strict=True):
            expected_id = f"{workflow.id}-{stage.upper()}"
            if (
                task.id != expected_id
                or task.workflow_id != workflow.id
                or task.name != f"V0.7 assembly {stage.replace('_', ' ')}"
                or task.task_type != task_type
                or task.cost_class != CostClass.LOCAL
                or task.status != TaskStatus.PENDING
                or task.depends_on != ([previous_id] if previous_id is not None else [])
                or task.max_retries != 1
                or task.timeout_seconds != 900.0
                or task.id in seen_ids
            ):
                raise ValidationError("Assembly graph task sequence differs from its canonical DAG")
            _validate_task_parameters(task)
            if first_parameters is None:
                first_parameters = task.parameters
            elif task.parameters != first_parameters:
                raise ValidationError("Assembly graph tasks must share one immutable parameter pin")
            AssemblyGraphRepository._validate_task_revision_binding(
                task.parameters, workflow, revision
            )
            previous_id = task.id
            seen_ids.add(task.id)
        return tasks

    @staticmethod
    def _validate_task_revision_binding(
        parameters: dict[str, Any], workflow: Workflow, revision: AssetRevision
    ) -> None:
        spec = parameters.get("specification")
        if not isinstance(spec, dict):
            raise ValidationError("Assembly graph task parameters require a V0.7 specification")
        try:
            spec_json = _canonical_json(spec)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Assembly graph V0.7 specification is not canonical JSON") from exc
        spec_hash = hashlib.sha256(spec_json.encode("utf-8")).hexdigest()
        if (
            parameters.get("graph_version") != _GRAPH_VERSION
            or parameters.get("workflow_id") != workflow.id
            or parameters.get("asset_id") != revision.asset_id
            or type(parameters.get("source_version")) is not int
            or parameters.get("source_version") != revision.revision_number
            or parameters.get("specification_sha256") != revision.spec_hash
            or parameters.get("profile_id") != revision.profile_id
            or type(parameters.get("profile_version")) is not int
            or parameters.get("profile_version") != revision.profile_version
            or parameters.get("source_glb_sha256") != revision.raw_glb_hash
            or parameters.get("concept_image_sha256") != revision.concept_hash
            or spec.get("schema_version") != "0.7.0"
            or spec.get("source_kind") != "local_operator_assembly"
            or spec.get("asset_id") != revision.asset_id
            or spec.get("profile") != revision.profile_id
            or spec.get("profile_version") != revision.profile_version
            or spec_hash != revision.spec_hash
        ):
            raise ValidationError("Assembly graph task parameters do not bind its assigned revision")

    @staticmethod
    def _insert_tasks(conn: sqlite3.Connection, tasks: list[Task]) -> None:
        for task in tasks:
            conn.execute(
                "INSERT INTO tasks (id, workflow_id, name, task_type, cost_class, depends_on_json, "
                "status, parameters_json, max_retries, timeout_seconds, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task.id,
                    task.workflow_id,
                    task.name,
                    task.task_type,
                    task.cost_class.value,
                    json.dumps(task.depends_on),
                    task.status.value,
                    json.dumps(task.parameters),
                    task.max_retries,
                    task.timeout_seconds,
                    task.created_at,
                    task.updated_at,
                ),
            )

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
        raw_glb_hash: str,
        task_factory: Callable[[AssetRevision], Sequence[Task]],
    ) -> tuple[AssetRevision, list[Task]]:
        if saved_workflow["project_id"] != requested_workflow.project_id:
            raise ValidationError("Workflow ID already belongs to another project")
        revisions = conn.execute(
            "SELECT * FROM asset_revisions WHERE workflow_id = ?", (requested_workflow.id,)
        ).fetchall()
        task_rows = conn.execute(
            "SELECT * FROM tasks WHERE workflow_id = ?",
            (requested_workflow.id,),
        ).fetchall()
        if len(revisions) != 1 or len(task_rows) != len(_ASSEMBLY_STAGES):
            raise ValidationError("Existing assembly workflow is orphaned or incomplete")
        revision = self._row_to_revision(revisions[0])
        if saved_workflow["name"] != f"Local assembly: {asset_id} {revision.revision_id}":
            raise ValidationError("Existing assembly workflow has a conflicting immutable name")
        if (
            revision.asset_id != asset_id
            or revision.workflow_id != requested_workflow.id
            or revision.spec_hash != spec_hash
            or revision.profile_id != profile_id
            or revision.profile_version != profile_version
            or revision.concept_hash != concept_hash
            or revision.raw_glb_hash != raw_glb_hash
        ):
            raise ValidationError("Existing assembly workflow has conflicting pinned inputs")
        expected_tasks = self._make_and_validate_tasks(task_factory, revision, requested_workflow)
        saved_by_id = {row["id"]: self._row_to_task(row) for row in task_rows}
        if len(saved_by_id) != len(task_rows):
            raise ValidationError("Existing assembly graph contains duplicate task identities")
        maybe_tasks = [
            saved_by_id.get(f"{requested_workflow.id}-{stage.upper()}")
            for stage, _task_type in _ASSEMBLY_STAGES
        ]
        if any(task is None for task in maybe_tasks):
            raise ValidationError("Existing assembly graph omits canonical task identities")
        saved_tasks = [cast(Task, task) for task in maybe_tasks]
        if any(
            not self._same_immutable_task(saved, expected)
            for saved, expected in zip(saved_tasks, expected_tasks, strict=True)
        ):
            raise ValidationError("Existing assembly workflow differs from the complete canonical graph")
        registered = conn.execute(
            "SELECT details_json FROM audit_events WHERE entity_type='Workflow' AND entity_id=? "
            "AND action='REGISTERED'",
            (requested_workflow.id,),
        ).fetchall()
        if len(registered) != 1:
            raise ValidationError("Existing assembly workflow has an ambiguous registration audit")
        try:
            details = json.loads(registered[0]["details_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValidationError("Existing assembly registration audit is malformed") from exc
        if _canonical_json(details) != _canonical_json(
            {"task_count": len(_ASSEMBLY_STAGES)}
        ):
            raise ValidationError("Existing assembly registration audit does not match its graph")
        return revision, saved_tasks

    @staticmethod
    def _same_immutable_task(saved: Task, expected: Task) -> bool:
        return (
            saved.id == expected.id
            and saved.workflow_id == expected.workflow_id
            and saved.name == expected.name
            and saved.task_type == expected.task_type
            and saved.cost_class == expected.cost_class == CostClass.LOCAL
            and saved.depends_on == expected.depends_on
            and _canonical_json(saved.parameters) == _canonical_json(expected.parameters)
            and saved.max_retries == expected.max_retries
            and saved.timeout_seconds == expected.timeout_seconds
        )

    @staticmethod
    def _reject_orphan_identity(conn: sqlite3.Connection, workflow_id: str) -> None:
        revision = conn.execute(
            "SELECT 1 FROM asset_revisions WHERE workflow_id = ? LIMIT 1", (workflow_id,)
        ).fetchone()
        task = conn.execute(
            "SELECT 1 FROM tasks WHERE workflow_id = ? LIMIT 1", (workflow_id,)
        ).fetchone()
        audit = conn.execute(
            "SELECT 1 FROM audit_events WHERE entity_type='Workflow' AND entity_id=? LIMIT 1",
            (workflow_id,),
        ).fetchone()
        if revision is not None or task is not None or audit is not None:
            raise ValidationError("Workflow ID has orphaned graph state; refusing to reconstruct it")

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
            runtime_evidence_hashes=json.loads(row["runtime_evidence_hashes_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            workflow_id=row["workflow_id"],
            name=row["name"],
            task_type=row["task_type"],
            cost_class=CostClass(row["cost_class"]),
            depends_on=json.loads(row["depends_on_json"]),
            status=TaskStatus(row["status"]),
            parameters=json.loads(row["parameters_json"]),
            max_retries=row["max_retries"],
            timeout_seconds=row["timeout_seconds"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
