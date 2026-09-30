"""Atomic registration for cold-verified provider-character evidence manifests."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import PurePosixPath

from gamefactory.adapters.persistence.database import Database
from gamefactory.core.domain.models import Artifact, Execution, Task, Workflow

_HASH = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_TYPE = "provider-character-evidence-manifest"
_PRODUCER = "cold_verified_provider_character_evidence"


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ProviderCharacterEvidencePublicationRepository:
    """Insert one attempt-bound manifest only while its entire live selector is current."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def publish_attempt(
        self,
        *,
        workflow: Workflow,
        task: Task,
        execution: Execution,
        artifact: Artifact,
        expected_snapshot_fingerprint: str,
        current_snapshot_fingerprint: Callable[[], str],
    ) -> None:
        """Atomically fence the live graph and insert its already cold-verified manifest.

        Cold verification, source hashing, and artifact-file verification happen
        before this method. The selector callback is read-only; ``BEGIN IMMEDIATE``
        prevents another committed writer from changing its database inputs before
        this manifest row is inserted.
        """
        if not _HASH.fullmatch(expected_snapshot_fingerprint):
            raise ValueError("Provider-character expected snapshot fingerprint is invalid")
        artifact_errors = []
        if artifact.workflow_id != workflow.id:
            artifact_errors.append("workflow")
        if artifact.task_id != task.id:
            artifact_errors.append("task")
        if artifact.artifact_type != _MANIFEST_TYPE:
            artifact_errors.append("type")
        if artifact.producer != _PRODUCER:
            artifact_errors.append("producer")
        if artifact.validation_state != "VERIFIED":
            artifact_errors.append("validation state")
        if not isinstance(artifact.id, str) or not artifact.id:
            artifact_errors.append("id")
        if type(artifact.file_size) is not int or artifact.file_size < 0:
            artifact_errors.append("file size")
        if not isinstance(artifact.content_hash, str) or not _HASH.fullmatch(artifact.content_hash):
            artifact_errors.append("content hash")
        if not isinstance(artifact.created_at, str) or not artifact.created_at:
            artifact_errors.append("creation timestamp")
        if artifact_errors:
            raise ValueError(
                "Provider-character manifest artifact identity is invalid: "
                + ", ".join(artifact_errors)
            )
        path = artifact.relative_path
        if (
            not isinstance(path, str)
            or not path
            or "\\" in path
            or ":" in path
            or path.startswith("/")
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or PurePosixPath(path).as_posix() != path
        ):
            raise ValueError("Provider-character manifest path must be canonical relative POSIX")

        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            workflow_row = conn.execute(
                "SELECT project_id, status FROM workflows WHERE id = ?", (workflow.id,)
            ).fetchone()
            task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task.id,)).fetchone()
            execution_row = conn.execute(
                "SELECT task_id, attempt_number, status FROM executions WHERE id = ?",
                (execution.id,),
            ).fetchone()
            latest = conn.execute(
                "SELECT id, attempt_number, status FROM executions WHERE task_id = ? "
                "ORDER BY attempt_number DESC LIMIT 1",
                (task.id,),
            ).fetchone()
            if (
                workflow_row is None
                or workflow_row["project_id"] != workflow.project_id
                or workflow_row["status"] != "RUNNING"
                or workflow.status.value != "RUNNING"
                or task_row is None
                or task_row["workflow_id"] != workflow.id
                or task.workflow_id != workflow.id
                or task_row["task_type"] != "asset_v07_provider_evidence"
                or task.task_type != "asset_v07_provider_evidence"
                or task_row["cost_class"] != "LOCAL"
                or task.cost_class.value != "LOCAL"
                or task_row["status"] != "RUNNING"
                or task.status.value != "RUNNING"
                or execution_row is None
                or execution_row["task_id"] != task.id
                or execution_row["attempt_number"] != execution.attempt_number
                or execution_row["status"] != "RUNNING"
                or execution.task_id != task.id
                or execution.status.value != "RUNNING"
                or type(execution.attempt_number) is not int
                or execution.attempt_number < 1
                or latest is None
                or latest["id"] != execution.id
                or latest["attempt_number"] != execution.attempt_number
                or latest["status"] != "RUNNING"
            ):
                raise ValueError(
                    "Provider-character manifest publication lost the current RUNNING attempt"
                )
            try:
                persisted_parameters = json.loads(task_row["parameters_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "Provider-character persisted task parameters are malformed"
                ) from exc
            if not isinstance(persisted_parameters, dict):
                raise ValueError("Provider-character persisted task parameters are malformed")
            if _canonical_json(persisted_parameters) != _canonical_json(task.parameters):
                raise ValueError("Provider-character task parameters changed before publication")
            specification = (
                persisted_parameters.get("specification")
                if isinstance(persisted_parameters, dict)
                else None
            )
            if not isinstance(specification, dict):
                raise ValueError("Provider-character publication specification is malformed")
            revision_number = persisted_parameters.get("revision_number")
            asset_id = specification.get("asset_id")
            if (
                persisted_parameters.get("graph_version") != "0.7.0"
                or type(revision_number) is not int
                or revision_number < 1
                or not isinstance(asset_id, str)
                or not asset_id
            ):
                raise ValueError("Provider-character publication task graph is malformed")
            revision = conn.execute(
                "SELECT workflow_id, spec_hash, profile_id, profile_version "
                "FROM asset_revisions WHERE asset_id = ? AND revision_number = ?",
                (asset_id, revision_number),
            ).fetchone()
            if (
                revision is None
                or revision["workflow_id"] != workflow.id
                or revision["spec_hash"] != persisted_parameters.get("specification_hash")
                or revision["profile_id"] != persisted_parameters.get("profile_id")
                or revision["profile_version"] != persisted_parameters.get("profile_version")
                or specification.get("profile") != revision["profile_id"]
                or specification.get("profile_version") != revision["profile_version"]
            ):
                raise ValueError("Provider-character asset revision changed before publication")
            expected_path = (
                f".gamefactory/assets/{asset_id}/r{revision_number:03d}/provider-evidence/"
                f"{execution.id}-a{execution.attempt_number}/manifest.json"
            )
            if path != expected_path:
                raise ValueError("Provider-character manifest is not in its private attempt path")

            current_fingerprint = current_snapshot_fingerprint()
            if current_fingerprint != expected_snapshot_fingerprint:
                raise ValueError("Provider-character snapshot changed before manifest publication")

            conflicts = conn.execute(
                "SELECT id, relative_path FROM artifacts WHERE id = ? OR "
                "(workflow_id = ? AND relative_path = ?)",
                (artifact.id, workflow.id, path),
            ).fetchall()
            if conflicts:
                raise ValueError("Provider-character manifest artifact ID or path already exists")
            conn.execute(
                "INSERT INTO artifacts (id, workflow_id, task_id, artifact_type, producer, "
                "relative_path, content_hash, file_size, validation_state, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
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
                ),
            )
