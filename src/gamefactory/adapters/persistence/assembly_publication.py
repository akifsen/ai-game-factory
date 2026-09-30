"""Atomic persistence boundary for locally authored V0.7 assembly attempts.

Filesystem work and verification happen before this boundary. This repository
only binds already-verified artifact metadata and revision pins to the current
database attempt under one SQLite writer transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import PurePosixPath
from typing import Any, cast

from gamefactory.adapters.persistence.database import Database
from gamefactory.core.domain.asset_contracts import AssetRevision
from gamefactory.core.domain.models import Artifact, Execution, Task, TaskStatus, utc_now_iso

_GRAPH_VERSION = "0.7.0-local-assembly"
_HASH = re.compile(r"^[0-9a-f]{64}$")
_PIN_FIELDS = (
    "concept_hash",
    "raw_glb_hash",
    "processed_glb_hash",
    "validation_report_hash",
)
_HASH_ARTIFACT_TYPES = {
    "concept_hash": "asset-concept",
    "raw_glb_hash": "assembly-source-glb",
    "processed_glb_hash": "processed-assembly-glb",
    "validation_report_hash": "assembly-validation-report",
}
_RUNTIME_ARTIFACT_TYPE = "assembly-review-capture"
_VALID_NEW_STATES = frozenset({"VERIFIED"})
_VALID_EXISTING_STATES = frozenset({"VALID", "VERIFIED"})
_PIN_OWNER_TASK_TYPES = {
    "concept_hash": "asset_v07_assembly_prepare",
    "raw_glb_hash": "asset_v07_assembly_prepare",
    "processed_glb_hash": "asset_v07_assembly_process",
    "validation_report_hash": "asset_v07_assembly_validate",
}
_ARTIFACT_TYPES = frozenset(
    {
        "assembly-source-glb",
        "assembly-source-provenance",
        "assembly-source-publication-marker",
        "asset-concept",
        "asset-concept-provenance",
        "asset-specification-v07",
        "assembly-concept-approval",
        "assembly-source-approval",
        "processed-assembly-glb",
        "assembly-blender-report",
        "assembly-validation-report",
        "assembly-review-capture",
        "assembly-runtime-request",
        "assembly-runtime-harness",
        "assembly-runtime-observation",
        "assembly-runtime-index",
        "assembly-final-approval",
        "assembly-evidence-boundary",
        "assembly-evidence-manifest",
    }
)


def _canonical_spec_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _artifact_identity(artifact: Artifact) -> tuple[Any, ...]:
    return (
        artifact.id,
        artifact.workflow_id,
        artifact.task_id,
        artifact.artifact_type,
        artifact.producer,
        artifact.relative_path,
        artifact.content_hash,
        artifact.file_size,
        artifact.validation_state,
        artifact.created_at,
    )


def _validate_artifact(artifact: Artifact, task: Task) -> None:
    if artifact.workflow_id != task.workflow_id or artifact.task_id != task.id:
        raise ValueError("V0.7 attempt artifact must belong to the supplied workflow and task")
    if (
        not isinstance(artifact.id, str)
        or not artifact.id
        or not isinstance(artifact.producer, str)
        or not artifact.producer
        or not isinstance(artifact.validation_state, str)
        or artifact.validation_state not in _VALID_NEW_STATES
        or not isinstance(artifact.created_at, str)
        or not artifact.created_at
    ):
        raise ValueError("V0.7 attempt artifact identity and state are required")
    if not isinstance(artifact.artifact_type, str) or artifact.artifact_type not in _ARTIFACT_TYPES:
        raise ValueError(f"Unsupported V0.7 assembly artifact type: {artifact.artifact_type!r}")
    if not isinstance(artifact.file_size, int) or isinstance(artifact.file_size, bool):
        raise ValueError("V0.7 attempt artifact size must be an integer")
    if (
        artifact.file_size < 0
        or not isinstance(artifact.content_hash, str)
        or not _HASH.fullmatch(artifact.content_hash)
    ):
        raise ValueError("V0.7 attempt artifact size or SHA-256 is invalid")
    path = artifact.relative_path
    if (
        not isinstance(path, str)
        or not path
        or "\\" in path
        or ":" in path
        or path.startswith("/")
        or any(part in ("", ".", "..") for part in path.split("/"))
        or PurePosixPath(path).as_posix() != path
    ):
        raise ValueError("V0.7 attempt artifact path must be a canonical relative POSIX path")


def _check_latest_attempt(conn: sqlite3.Connection, task_id: str, execution: Execution) -> None:
    persisted = conn.execute(
        "SELECT id, task_id, attempt_number, status FROM executions WHERE id = ?",
        (execution.id,),
    ).fetchone()
    latest = conn.execute(
        "SELECT id, attempt_number, status FROM executions WHERE task_id = ? "
        "ORDER BY attempt_number DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if (
        persisted is None
        or persisted["task_id"] != task_id
        or execution.task_id != task_id
        or persisted["attempt_number"] != execution.attempt_number
        or persisted["status"] != "RUNNING"
        or execution.status.value != "RUNNING"
        or latest is None
        or latest["id"] != execution.id
        or latest["attempt_number"] != execution.attempt_number
        or latest["status"] != "RUNNING"
    ):
        raise ValueError("V0.7 assembly attempt is stale or does not own the running task")


def _validate_binding(
    conn: sqlite3.Connection, task: Task, execution: Execution, revision: AssetRevision
) -> sqlite3.Row:
    task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task.id,)).fetchone()
    if (
        task_row is None
        or task_row["workflow_id"] != task.workflow_id
        or task.workflow_id != revision.workflow_id
        or task_row["status"] != TaskStatus.RUNNING.value
        or task.status != TaskStatus.RUNNING
        or not task.task_type.startswith("asset_v07_assembly_")
        or task_row["task_type"] != task.task_type
        or task_row["cost_class"] != "LOCAL"
    ):
        raise ValueError("V0.7 assembly task is not the persisted running task for this revision")
    _check_latest_attempt(conn, task.id, execution)

    try:
        persisted_parameters = json.loads(task_row["parameters_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Persisted V0.7 task parameters are invalid") from exc
    if persisted_parameters != task.parameters:
        raise ValueError("V0.7 task object does not match its persisted immutable parameters")
    parameters = persisted_parameters
    spec = parameters.get("specification") if isinstance(parameters, dict) else None
    if (
        not isinstance(parameters, dict)
        or parameters.get("graph_version") != _GRAPH_VERSION
        or parameters.get("workflow_id") != task.workflow_id
        or parameters.get("asset_id") != revision.asset_id
        or parameters.get("source_version") != revision.revision_number
        or parameters.get("specification_sha256") != revision.spec_hash
        or parameters.get("profile_id") != revision.profile_id
        or parameters.get("profile_version") != revision.profile_version
        or not isinstance(spec, dict)
        or spec.get("schema_version") != "0.7.0"
        or spec.get("source_kind") != "local_operator_assembly"
        or spec.get("asset_id") != revision.asset_id
        or spec.get("profile") != revision.profile_id
        or spec.get("profile_version") != revision.profile_version
        or _canonical_spec_hash(spec) != revision.spec_hash
        or parameters.get("source_glb_sha256") != revision.raw_glb_hash
        or parameters.get("concept_image_sha256") != revision.concept_hash
    ):
        raise ValueError("V0.7 task parameters do not exactly bind the supplied asset revision")

    persisted_revision = conn.execute(
        "SELECT * FROM asset_revisions WHERE asset_id = ? AND revision_number = ?",
        (revision.asset_id, revision.revision_number),
    ).fetchone()
    if (
        persisted_revision is None
        or persisted_revision["workflow_id"] != revision.workflow_id
        or persisted_revision["spec_hash"] != revision.spec_hash
        or persisted_revision["profile_id"] != revision.profile_id
        or persisted_revision["profile_version"] != revision.profile_version
    ):
        raise ValueError("V0.7 asset revision does not match its persisted workflow/spec/profile")
    return cast(sqlite3.Row, persisted_revision)


def _validate_pin_artifact_owner(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    pin_name: str,
    digest: str,
    artifact_batch: list[Artifact],
    current_task: Task,
    require_current_owner: bool,
) -> None:
    """Resolve a revision hash to its sole, canonical stage task and valid row."""
    if pin_name == "runtime_evidence_hashes":
        expected_type = _RUNTIME_ARTIFACT_TYPE
        owner_type = "asset_v07_assembly_godot"
    else:
        expected_type = _HASH_ARTIFACT_TYPES[pin_name]
        owner_type = _PIN_OWNER_TASK_TYPES[pin_name]
    owner_tasks = conn.execute(
        "SELECT id FROM tasks WHERE workflow_id = ? AND task_type = ?",
        (workflow_id, owner_type),
    ).fetchall()
    if len(owner_tasks) != 1:
        raise ValueError(f"V0.7 {pin_name} has no unique canonical role-owning task")
    owner_id = owner_tasks[0]["id"]

    candidates = [
        artifact
        for artifact in artifact_batch
        if artifact.artifact_type == expected_type and artifact.content_hash == digest
    ]
    if require_current_owner and (
        current_task.id != owner_id or current_task.task_type != owner_type
    ):
        raise ValueError(f"New V0.7 {pin_name} pin must be published by its canonical stage")
    if candidates:
        if any(artifact.task_id != owner_id for artifact in candidates):
            raise ValueError(f"New V0.7 {pin_name} artifact has the wrong owning task")

    rows = conn.execute(
        "SELECT a.task_id, a.validation_state, t.workflow_id AS owner_workflow, "
        "t.task_type AS owner_type FROM artifacts AS a "
        "LEFT JOIN tasks AS t ON t.id = a.task_id "
        "WHERE a.workflow_id = ? AND a.artifact_type = ? AND a.content_hash = ?",
        (workflow_id, expected_type, digest),
    ).fetchall()
    if not rows and not candidates:
        raise ValueError(f"V0.7 {pin_name} has no registered canonical {expected_type} artifact")
    for row in rows:
        if (
            row["task_id"] != owner_id
            or row["owner_workflow"] != workflow_id
            or row["owner_type"] != owner_type
            or row["validation_state"] not in _VALID_EXISTING_STATES
        ):
            raise ValueError(f"V0.7 {pin_name} resolves to an invalid or noncanonical artifact row")


class AssemblyPublicationRepository:
    """Publish one completed V0.7 handler result with its immutable revision pins."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def publish_attempt(
        self,
        *,
        task: Task,
        execution: Execution,
        revision: AssetRevision,
        artifacts: list[Artifact],
    ) -> None:
        """Atomically bind attempt artifacts and revision hashes.

        File reads, hashing, DCC work, and geometry checks must already be done.
        Existing artifact rows are accepted only as exact immutable retries.
        """
        if not artifacts:
            raise ValueError("V0.7 assembly publication requires at least one artifact")
        seen_ids: set[str] = set()
        seen_paths: set[str] = set()
        singleton_roles: set[str] = set()
        for artifact in artifacts:
            _validate_artifact(artifact, task)
            folded_path = artifact.relative_path.casefold()
            if artifact.id in seen_ids or folded_path in seen_paths:
                raise ValueError("V0.7 attempt artifacts contain duplicate IDs or paths")
            seen_ids.add(artifact.id)
            seen_paths.add(folded_path)
            if artifact.artifact_type != _RUNTIME_ARTIFACT_TYPE:
                if artifact.artifact_type in singleton_roles:
                    raise ValueError("V0.7 attempt artifacts contain a duplicate singleton role")
                singleton_roles.add(artifact.artifact_type)

        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = _validate_binding(conn, task, execution, revision)

            saved_runtime = json.loads(current["runtime_evidence_hashes_json"])
            proposed_runtime = revision.runtime_evidence_hashes
            if not isinstance(proposed_runtime, list) or any(
                not isinstance(item, str) or not _HASH.fullmatch(item) for item in proposed_runtime
            ):
                raise ValueError("V0.7 runtime evidence hashes must be SHA-256 strings")
            if proposed_runtime[: len(saved_runtime)] != saved_runtime:
                raise ValueError(
                    "V0.7 runtime evidence hashes cannot remove or reorder pinned values"
                )

            for field_name in _PIN_FIELDS:
                saved_value = current[field_name]
                proposed_value = getattr(revision, field_name)
                if proposed_value is not None and (
                    not isinstance(proposed_value, str) or not _HASH.fullmatch(proposed_value)
                ):
                    raise ValueError(f"V0.7 {field_name} must be a SHA-256 string")
                if saved_value is not None and proposed_value != saved_value:
                    raise ValueError(f"Cannot overwrite immutable V0.7 {field_name}")
                if proposed_value is not None:
                    _validate_pin_artifact_owner(
                        conn,
                        workflow_id=task.workflow_id,
                        pin_name=field_name,
                        digest=proposed_value,
                        artifact_batch=artifacts,
                        current_task=task,
                        require_current_owner=saved_value is None,
                    )
            for index, digest in enumerate(proposed_runtime):
                _validate_pin_artifact_owner(
                    conn,
                    workflow_id=task.workflow_id,
                    pin_name="runtime_evidence_hashes",
                    digest=digest,
                    artifact_batch=artifacts,
                    current_task=task,
                    require_current_owner=index >= len(saved_runtime),
                )

            existing_paths = conn.execute(
                "SELECT * FROM artifacts WHERE workflow_id = ?", (task.workflow_id,)
            ).fetchall()
            existing_paths_by_folded: dict[str, list[sqlite3.Row]] = {}
            for row in existing_paths:
                existing_paths_by_folded.setdefault(row["relative_path"].casefold(), []).append(row)
            for artifact in artifacts:
                by_id = conn.execute(
                    "SELECT * FROM artifacts WHERE id = ?", (artifact.id,)
                ).fetchone()
                by_path = existing_paths_by_folded.get(artifact.relative_path.casefold())
                identity = _artifact_identity(artifact)
                if by_id is not None and tuple(by_id) != identity:
                    raise ValueError(
                        f"V0.7 artifact ID conflicts with an existing row: {artifact.id}"
                    )
                if by_path is not None and (len(by_path) != 1 or tuple(by_path[0]) != identity):
                    raise ValueError(
                        f"V0.7 artifact path conflicts with an existing row: {artifact.relative_path}"
                    )
                if by_id is None and by_path is None:
                    conn.execute(
                        "INSERT INTO artifacts (id, workflow_id, task_id, artifact_type, producer, "
                        "relative_path, content_hash, file_size, validation_state, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        identity,
                    )

            changed = (
                any(
                    current[field_name] != getattr(revision, field_name)
                    for field_name in _PIN_FIELDS
                )
                or saved_runtime != proposed_runtime
            )
            revision.updated_at = utc_now_iso() if changed else current["updated_at"]
            conn.execute(
                "UPDATE asset_revisions SET concept_hash=?, raw_glb_hash=?, processed_glb_hash=?, "
                "validation_report_hash=?, runtime_evidence_hashes_json=?, updated_at=? "
                "WHERE asset_id=? AND revision_number=?",
                (
                    revision.concept_hash,
                    revision.raw_glb_hash,
                    revision.processed_glb_hash,
                    revision.validation_report_hash,
                    json.dumps(proposed_runtime, separators=(",", ":")),
                    revision.updated_at,
                    revision.asset_id,
                    revision.revision_number,
                ),
            )
