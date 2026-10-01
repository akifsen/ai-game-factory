"""Atomic candidate evidence publication under held DB writer lock."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.v08_candidate_evidence import (
    COLD_BUNDLE_DIRNAME,
    CandidateEvidenceStagingLayout,
    assert_completion_marker_controls,
    assert_publication_result_bindings,
    atomic_publish_staged_container,
    fingerprint_cold_bundle_payload,
    fingerprint_publish_container,
    load_bounded_publication_control_json,
    parse_staging_container,
)
from gamefactory.adapters.engines.godot_staging import sha256_file
from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    ExecutionRepository,
    TaskRepository,
)
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import (
    Artifact,
    Execution,
    ExecutionStatus,
    Task,
    TaskStatus,
    Workflow,
    generate_id,
    utc_now_iso,
)
from gamefactory.workflows.handlers import TaskHandlerResult
from gamefactory.workflows.v08_candidate_currentness import (
    BLOCKING_EXECUTION_STATUSES,
    CandidateCurrentnessError,
    assert_zero_provider_activity,
)
from gamefactory.workflows.v08_candidate_snapshot import CandidateBoundSnapshot


def _marker_artifact_id(workflow_id: str) -> str:
    return f"ART-C2-MARKER-{workflow_id}"


def _commit_writer_connection(conn: Any) -> None:
    conn.commit()


def _assert_active_evidence_execution(
    conn: Any,
    handlers: Any,
    workflow: Workflow,
    *,
    task_id: str,
    execution_id: str,
) -> tuple[Task, Execution]:
    task_repo = TaskRepository(handlers.artifacts.db)
    exec_repo = ExecutionRepository(handlers.executions.db)
    task_row = next(
        (row for row in task_repo.list_by_workflow(workflow.id, conn=conn) if row.id == task_id),
        None,
    )
    if task_row is None or task_row.status != TaskStatus.RUNNING:
        raise ValidationError("candidate evidence task must remain RUNNING during publication")
    attempts = exec_repo.list_by_task(task_id, conn=conn)
    if not attempts:
        raise ValidationError("candidate evidence execution is missing under writer lock")
    latest = max(attempts, key=lambda row: row.attempt_number)
    if latest.id != execution_id:
        raise ValidationError(
            "candidate evidence execution is no longer the authoritative latest attempt"
        )
    active = next((row for row in attempts if row.id == execution_id), None)
    if active is None or active.status != ExecutionStatus.RUNNING:
        raise ValidationError(
            "candidate evidence execution must remain RUNNING until publication commits"
        )
    if latest.attempt_number != active.attempt_number:
        raise ValidationError("candidate evidence attempt number drifted during publication")
    return task_row, active


def _assert_no_blocking_executions(
    conn: Any,
    handlers: Any,
    workflow: Workflow,
    *,
    evidence_task_id: str,
    evidence_execution_id: str,
) -> None:
    task_repo = TaskRepository(handlers.artifacts.db)
    exec_repo = ExecutionRepository(handlers.executions.db)
    for row in task_repo.list_by_workflow(workflow.id, conn=conn):
        attempts = exec_repo.list_by_task(row.id, conn=conn)
        if not attempts:
            continue
        latest = max(attempts, key=lambda item: item.attempt_number)
        if row.id == evidence_task_id:
            if latest.id != evidence_execution_id:
                raise ValidationError(
                    "A newer candidate evidence attempt appeared during publication"
                )
            if latest.status != ExecutionStatus.RUNNING:
                raise ValidationError(
                    "candidate evidence latest attempt is not the active RUNNING publication"
                )
            continue
        if latest.status not in BLOCKING_EXECUTION_STATUSES:
            continue
        raise ValidationError(
            f"Execution {latest.id} entered blocking status during candidate evidence publication"
        )


def _snapshot_bytes(snapshot: CandidateBoundSnapshot) -> str:
    return json.dumps(snapshot.payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def publish_candidate_evidence_from_staging(
    handlers: Any,
    workflow: Workflow,
    task: Task,
    execution: Execution,
    *,
    snapshot_before: CandidateBoundSnapshot,
    staging_container: Path,
    d_ready: str,
) -> TaskHandlerResult:
    """Rename staged container and insert-only register manifest/result/marker on one connection."""
    assert_zero_provider_activity(handlers.artifacts.db, workflow.id)
    layout = parse_staging_container(staging_container)
    result_name = f"c2-evidence-result-{execution.id}.json"
    marker_name = f"c2-export-marker-{execution.id}.json"
    result_path = layout.container_root / result_name
    marker_path = layout.container_root / marker_name
    if not result_path.is_file() or not marker_path.is_file():
        raise ValidationError("publication container is missing prepared result/marker siblings")
    if layout.result_path.is_file() or layout.marker_path.is_file():
        raise ValidationError("legacy publication sibling filenames must not be present")
    pre_lock_digest = fingerprint_publish_container(layout.container_root)
    if pre_lock_digest != d_ready:
        raise ValidationError("publication container digest drifted before writer lock")

    before_bytes = _snapshot_bytes(snapshot_before)
    manifest_rel = None
    result_rel = None
    marker_rel = None
    artifact_ids: list[str] = []

    with handlers.artifacts.db.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing_marker = conn.execute(
            """
            SELECT id FROM artifacts
            WHERE workflow_id = ? AND artifact_type = ? LIMIT 1;
            """,
            (workflow.id, "candidate-c2-export-marker"),
        ).fetchone()
        if existing_marker is not None:
            raise ValidationError("candidate evidence completion marker already published")

        prepare = handlers._prepare_task(workflow.id)
        try:
            snapshot_after = handlers._bound_snapshot(workflow, prepare)
        except (CandidateCurrentnessError, ArtifactError) as exc:
            raise ValidationError(
                "Authoritative candidate snapshot changed before evidence publication"
            ) from exc
        if _snapshot_bytes(snapshot_after) != before_bytes:
            raise ValidationError(
                "Authoritative candidate snapshot changed before evidence publication"
            )
        task_row, execution_row = _assert_active_evidence_execution(
            conn,
            handlers,
            workflow,
            task_id=task.id,
            execution_id=execution.id,
        )
        _assert_no_blocking_executions(
            conn,
            handlers,
            workflow,
            evidence_task_id=task.id,
            evidence_execution_id=execution.id,
        )

        d_pre_rename = fingerprint_publish_container(layout.container_root)
        if d_pre_rename != d_ready:
            raise ValidationError("publication container digest drifted under writer lock")

        publish_parent = handlers.root / ".gamefactory" / "candidate-evidence" / workflow.id
        final_container = atomic_publish_staged_container(
            layout.container_root, publish_parent, execution.id
        )
        layout = CandidateEvidenceStagingLayout(
            container_root=final_container, cold_bundle_dir=final_container / COLD_BUNDLE_DIRNAME
        )
        d_post_rename = fingerprint_publish_container(final_container)
        if d_post_rename != d_ready:
            raise ValidationError("publication container digest drifted after rename")

        manifest_path = layout.cold_bundle_dir / "manifest.json"
        result_file = final_container / result_name
        marker_file = final_container / marker_name
        manifest_hash = sha256_file(manifest_path)
        result_hash = sha256_file(result_file)
        marker_hash = sha256_file(marker_file)
        marker_doc = load_bounded_publication_control_json(marker_file, label=marker_file.name)
        assert_completion_marker_controls(marker_doc, workflow_id=workflow.id)
        if marker_doc.get("manifest_sha256") != manifest_hash:
            raise ValidationError("completion marker manifest hash mismatch under lock")
        if marker_doc.get("result_sha256") != result_hash:
            raise ValidationError("completion marker result hash mismatch under lock")
        if marker_doc.get("snapshot_fingerprint") != snapshot_before.fingerprint():
            raise ValidationError("completion marker snapshot fingerprint mismatch under lock")
        cold_bundle_digest = fingerprint_cold_bundle_payload(layout.cold_bundle_dir)
        marker_d1 = marker_doc.get("cold_bundle_payload_digest")
        if not isinstance(marker_d1, str) or marker_d1 != cold_bundle_digest:
            raise ValidationError("completion marker cold bundle digest mismatch under lock")
        manifest_doc = load_bounded_publication_control_json(manifest_path, label="manifest.json")
        manifest_bundle_id = manifest_doc.get("bundle_id")
        if not isinstance(manifest_bundle_id, str) or not manifest_bundle_id:
            raise ValidationError("cold manifest bundle_id is missing")
        result_doc = load_bounded_publication_control_json(result_file, label=result_file.name)
        assert_publication_result_bindings(
            result_doc,
            workflow_id=workflow.id,
            task_id=task_row.id,
            execution_id=execution_row.id,
            attempt_number=execution_row.attempt_number,
            snapshot_fingerprint=snapshot_before.fingerprint(),
            cold_bundle_payload_digest=cold_bundle_digest,
            manifest_bundle_id=manifest_bundle_id,
        )

        manifest_rel = manifest_path.relative_to(handlers.root).as_posix()
        result_rel = result_file.relative_to(handlers.root).as_posix()
        marker_rel = marker_file.relative_to(handlers.root).as_posix()

        manifest_art = Artifact(
            id=generate_id("ART-C2-MAN"),
            workflow_id=workflow.id,
            task_id=task_row.id,
            artifact_type="candidate-c2-evidence-manifest",
            producer=task_row.task_type,
            relative_path=manifest_rel,
            content_hash=manifest_hash,
            file_size=manifest_path.stat().st_size,
            validation_state="VALID",
            created_at=utc_now_iso(),
        )
        result_art = Artifact(
            id=generate_id("ART-C2-RES"),
            workflow_id=workflow.id,
            task_id=task_row.id,
            artifact_type="candidate-c2-evidence-result",
            producer=task_row.task_type,
            relative_path=result_rel,
            content_hash=result_hash,
            file_size=result_file.stat().st_size,
            validation_state="VALID",
            created_at=utc_now_iso(),
        )
        marker_art = Artifact(
            id=_marker_artifact_id(workflow.id),
            workflow_id=workflow.id,
            task_id=task_row.id,
            artifact_type="candidate-c2-export-marker",
            producer=task_row.task_type,
            relative_path=marker_rel,
            content_hash=marker_hash,
            file_size=marker_file.stat().st_size,
            validation_state="VALID",
            created_at=utc_now_iso(),
        )
        ArtifactRepository(handlers.artifacts.db).save_many_on_connection(
            conn, [manifest_art, result_art, marker_art]
        )
        artifact_ids = [manifest_art.id, result_art.id, marker_art.id]
        _commit_writer_connection(conn)

    return TaskHandlerResult(
        1,
        "Candidate evidence envelope published with trusted cold verification",
        artifact_ids,
    )


__all__ = ["publish_candidate_evidence_from_staging"]
