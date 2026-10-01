"""Post-publication candidate evidence readiness (distinct from C1 pre-evidence query)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from gamefactory.adapters.assets.v08_candidate_evidence import (
    assert_completion_marker_controls,
    assert_known_published_evidence_container_layout,
    assert_publication_controls_same_container,
    assert_publication_result_bindings,
    canonical_trusted_cold_result_json,
    fingerprint_cold_bundle_payload,
    fingerprint_publish_container,
    load_bounded_publication_control_json,
    trusted_cold_verify_candidate_bundle,
)
from gamefactory.adapters.engines.godot_staging import sha256_file
from gamefactory.adapters.persistence.repositories import ExecutionRepository, TaskRepository
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.models import Artifact, Execution, ExecutionStatus, Task, TaskStatus
from gamefactory.workflows.v08_candidate_currentness import (
    CandidateCurrentnessError,
    assert_latest_attempt_completed,
    select_artifact_for_execution,
    verify_artifact_bytes_and_hash,
)
from gamefactory.workflows.v08_candidate_snapshot import CandidateBoundSnapshot
from gamefactory.workflows.v08_candidate_workflow import (
    CandidateWorkflowHandlers,
    candidate_workflow_readiness,
)


@dataclass(frozen=True)
class CandidateEvidenceReadiness:
    """Typed evidence completion view; production/promotion remain ineligible."""

    snapshot: CandidateBoundSnapshot
    evidence_execution_id: str
    evidence_attempt_number: int
    manifest_artifact_id: str
    result_artifact_id: str
    marker_artifact_id: str
    candidate_evidence_complete: bool
    production_eligible: bool
    promotion_eligible: bool
    execution_provenance: str = "CONSISTENT_BUT_UNAUTHENTICATED"


@dataclass(frozen=True)
class _ArtifactRegistrationSnapshot:
    id: str
    workflow_id: str
    task_id: str
    artifact_type: str
    producer: str
    relative_path: str
    content_hash: str
    file_size: int
    validation_state: str
    created_at: str


@dataclass(frozen=True)
class _PublicationInspectionCapture:
    upstream_fingerprint: str
    upstream_payload_canonical: str
    evidence_task_id: str
    evidence_execution_id: str
    evidence_attempt_number: int
    marker: _ArtifactRegistrationSnapshot
    manifest: _ArtifactRegistrationSnapshot
    result_art: _ArtifactRegistrationSnapshot
    marker_doc_canonical: str
    result_doc_canonical: str
    stored_trusted_cold_canonical: str
    marker_d1: str
    d_ready: str
    container_root: str


def _evidence_task(tasks: TaskRepository, workflow_id: str) -> Task:
    rows = tasks.list_by_workflow(workflow_id)
    for row in rows:
        if row.task_type == "v08_candidate_evidence":
            return row
    raise ValidationError("workflow has no candidate evidence task")


def _assert_live_evidence_task(evidence_task: Task, workflow_id: str) -> None:
    if evidence_task.workflow_id != workflow_id:
        raise CandidateCurrentnessError("candidate evidence task workflow binding mismatch")
    if evidence_task.task_type != "v08_candidate_evidence":
        raise CandidateCurrentnessError("candidate evidence task type mismatch")
    if evidence_task.status != TaskStatus.COMPLETED:
        raise CandidateCurrentnessError("candidate evidence task is not completed")


def _artifact_snapshot(artifact: Artifact) -> _ArtifactRegistrationSnapshot:
    return _ArtifactRegistrationSnapshot(
        id=artifact.id,
        workflow_id=artifact.workflow_id,
        task_id=artifact.task_id,
        artifact_type=artifact.artifact_type,
        producer=artifact.producer,
        relative_path=artifact.relative_path,
        content_hash=artifact.content_hash,
        file_size=artifact.file_size,
        validation_state=artifact.validation_state,
        created_at=artifact.created_at,
    )


def _assert_registered_evidence_artifact(
    snap: _ArtifactRegistrationSnapshot,
    *,
    evidence_task: Task,
    workflow_id: str,
    expected_type: str,
    path: Any,
) -> None:
    if snap.workflow_id != workflow_id:
        raise CandidateCurrentnessError(f"{expected_type} workflow_id mismatch")
    if snap.task_id != evidence_task.id:
        raise CandidateCurrentnessError(f"{expected_type} task_id mismatch")
    if snap.artifact_type != expected_type:
        raise CandidateCurrentnessError(f"{expected_type} artifact_type mismatch")
    if snap.producer != evidence_task.task_type:
        raise CandidateCurrentnessError(f"{expected_type} producer mismatch")
    if snap.validation_state != "VALID":
        raise CandidateCurrentnessError(f"{expected_type} validation_state is not VALID")
    if not path.is_file():
        raise CandidateCurrentnessError(f"{expected_type} path missing on disk")
    on_disk_size = path.stat().st_size
    if on_disk_size != snap.file_size:
        raise CandidateCurrentnessError(f"{expected_type} registered file_size mismatch")
    on_disk_hash = sha256_file(path)
    if on_disk_hash != snap.content_hash:
        raise CandidateCurrentnessError(f"{expected_type} registered content_hash mismatch")


def _canonical_doc(doc: dict[str, object]) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _list_artifacts_on_connection(conn: sqlite3.Connection, workflow_id: str) -> list[Artifact]:
    cursor = conn.execute(
        "SELECT * FROM artifacts WHERE workflow_id = ? ORDER BY created_at ASC;",
        (workflow_id,),
    )
    rows = cursor.fetchall()
    out: list[Artifact] = []
    for row in rows:
        out.append(
            Artifact(
                id=row["id"],
                workflow_id=row["workflow_id"],
                task_id=row["task_id"],
                artifact_type=row["artifact_type"],
                producer=row["producer"],
                relative_path=row["relative_path"],
                content_hash=row["content_hash"],
                file_size=row["file_size"],
                validation_state=row["validation_state"],
                created_at=row["created_at"],
            )
        )
    return out


def _inspect_publication_triplet(
    handlers: CandidateWorkflowHandlers,
    workflow_id: str,
    evidence_task: Task,
    evidence_exec: Execution,
    upstream: CandidateBoundSnapshot,
    *,
    marker: Artifact,
    manifest: Artifact,
    result_art: Artifact,
    marker_snap: _ArtifactRegistrationSnapshot,
    manifest_snap: _ArtifactRegistrationSnapshot,
    result_snap: _ArtifactRegistrationSnapshot,
    marker_doc: dict[str, object],
    result_doc: dict[str, object],
) -> tuple[str, str, str, dict[str, object]]:
    if marker_doc.get("evidence_task_id") != evidence_task.id:
        raise CandidateCurrentnessError("completion marker evidence_task_id mismatch")
    if marker_doc.get("evidence_execution_id") != evidence_exec.id:
        raise CandidateCurrentnessError("completion marker execution binding mismatch")
    if marker_doc.get("evidence_attempt_number") != evidence_exec.attempt_number:
        raise CandidateCurrentnessError("completion marker attempt binding mismatch")
    if marker_doc.get("snapshot_fingerprint") != upstream.fingerprint():
        raise CandidateCurrentnessError("completion marker snapshot fingerprint is stale")

    manifest_path = handlers.root / manifest.relative_path
    result_path = handlers.root / result_art.relative_path
    marker_path = handlers.root / marker.relative_path
    for snap, _art, path, label in (
        (marker_snap, marker, marker_path, "candidate-c2-export-marker"),
        (manifest_snap, manifest, manifest_path, "candidate-c2-evidence-manifest"),
        (result_snap, result_art, result_path, "candidate-c2-evidence-result"),
    ):
        _assert_registered_evidence_artifact(
            snap,
            evidence_task=evidence_task,
            workflow_id=workflow_id,
            expected_type=label,
            path=path,
        )

    try:
        container_root = assert_publication_controls_same_container(
            manifest_path=manifest_path,
            result_path=result_path,
            marker_path=marker_path,
        )
    except ValidationError as exc:
        raise CandidateCurrentnessError(str(exc)) from exc
    try:
        assert_known_published_evidence_container_layout(
            container_root, execution_id=evidence_exec.id
        )
    except ValidationError as exc:
        raise CandidateCurrentnessError(str(exc)) from exc

    manifest_hash = sha256_file(manifest_path)
    result_hash = sha256_file(result_path)
    marker_hash = sha256_file(marker_path)
    if manifest_hash != manifest.content_hash:
        raise CandidateCurrentnessError("evidence manifest registration hash drifted")
    if result_hash != result_art.content_hash:
        raise CandidateCurrentnessError("evidence result registration hash drifted")
    if marker_hash != marker.content_hash:
        raise CandidateCurrentnessError("evidence marker registration hash drifted")
    if marker_doc.get("manifest_sha256") != manifest.content_hash:
        raise CandidateCurrentnessError("completion marker manifest hash mismatch")
    if marker_doc.get("result_sha256") != result_art.content_hash:
        raise CandidateCurrentnessError("completion marker result hash mismatch")
    if marker_doc.get("manifest_sha256") != manifest_hash:
        raise CandidateCurrentnessError("completion marker manifest bytes mismatch")
    if marker_doc.get("result_sha256") != result_hash:
        raise CandidateCurrentnessError("completion marker result bytes mismatch")

    cold_bundle_dir = manifest_path.parent
    try:
        marker_d1 = assert_completion_marker_controls(marker_doc, workflow_id=workflow_id)
    except ValidationError as exc:
        raise CandidateCurrentnessError(str(exc)) from exc
    live_d1 = fingerprint_cold_bundle_payload(cold_bundle_dir)
    if marker_d1 != live_d1:
        raise CandidateCurrentnessError("completion marker cold bundle digest drifted from payload")
    d_ready = fingerprint_publish_container(container_root)
    try:
        manifest_doc = load_bounded_publication_control_json(
            manifest_path, label=manifest.relative_path
        )
    except ValidationError as exc:
        raise CandidateCurrentnessError(str(exc)) from exc
    manifest_bundle_id = manifest_doc.get("bundle_id")
    if not isinstance(manifest_bundle_id, str) or not manifest_bundle_id:
        raise CandidateCurrentnessError("cold manifest bundle_id is missing")
    try:
        trusted_cold = assert_publication_result_bindings(
            result_doc,
            workflow_id=workflow_id,
            task_id=evidence_task.id,
            execution_id=evidence_exec.id,
            attempt_number=evidence_exec.attempt_number,
            snapshot_fingerprint=upstream.fingerprint(),
            cold_bundle_payload_digest=marker_d1,
            manifest_bundle_id=manifest_bundle_id,
        )
    except ValidationError as exc:
        raise CandidateCurrentnessError(str(exc)) from exc
    if trusted_cold.get("workflow_id") not in (None, workflow_id):
        raise CandidateCurrentnessError("trusted cold result workflow_id mismatch")

    try:
        bundled_payload = load_bounded_publication_control_json(
            cold_bundle_dir / "snapshot" / "snapshot.json", label="bundled snapshot"
        )
    except ValidationError as exc:
        raise CandidateCurrentnessError(str(exc)) from exc
    bundled_canonical = json.dumps(
        bundled_payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    upstream_canonical = json.dumps(
        upstream.payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    if bundled_canonical != upstream_canonical:
        raise CandidateCurrentnessError("published bundle snapshot does not match live upstream")

    return marker_d1, d_ready, str(container_root.resolve()), trusted_cold


def _load_evidence_triplet_with_snapshots(
    handlers: CandidateWorkflowHandlers,
    workflow_id: str,
    evidence_task: Task,
    evidence_exec: Execution,
) -> tuple[
    Artifact,
    Artifact,
    Artifact,
    _ArtifactRegistrationSnapshot,
    _ArtifactRegistrationSnapshot,
    _ArtifactRegistrationSnapshot,
    dict[str, object],
    dict[str, object],
]:
    artifact_rows = handlers.artifacts.list_by_workflow(workflow_id)
    marker = select_artifact_for_execution(
        artifact_rows,
        task_id=evidence_task.id,
        artifact_type="candidate-c2-export-marker",
        execution=evidence_exec,
    )
    manifest = select_artifact_for_execution(
        artifact_rows,
        task_id=evidence_task.id,
        artifact_type="candidate-c2-evidence-manifest",
        execution=evidence_exec,
    )
    result_art = select_artifact_for_execution(
        artifact_rows,
        task_id=evidence_task.id,
        artifact_type="candidate-c2-evidence-result",
        execution=evidence_exec,
    )
    marker_snap = _artifact_snapshot(marker)
    manifest_snap = _artifact_snapshot(manifest)
    result_snap = _artifact_snapshot(result_art)
    for art in (marker, manifest, result_art):
        verify_artifact_bytes_and_hash(
            art, root_path=handlers.root, artifact_manager=handlers.artifact_manager
        )
    root = handlers.root
    try:
        marker_doc = load_bounded_publication_control_json(
            root / marker.relative_path, label=marker.relative_path
        )
        result_doc = load_bounded_publication_control_json(
            root / result_art.relative_path, label=result_art.relative_path
        )
    except ValidationError as exc:
        raise CandidateCurrentnessError(str(exc)) from exc
    return (
        marker,
        manifest,
        result_art,
        marker_snap,
        manifest_snap,
        result_snap,
        marker_doc,
        result_doc,
    )


def _assert_snapshots_unchanged(
    before: _ArtifactRegistrationSnapshot,
    after: _ArtifactRegistrationSnapshot,
    *,
    label: str,
) -> None:
    if before != after:
        raise CandidateCurrentnessError(f"{label} registration metadata drifted after inspection")


def candidate_evidence_readiness(
    handlers: CandidateWorkflowHandlers, workflow_id: str
) -> CandidateEvidenceReadiness:
    """Evidence completion readiness: upstream snapshot + committed marker/manifest/result."""
    upstream = candidate_workflow_readiness(handlers, workflow_id)
    upstream_payload_canonical = json.dumps(
        upstream.payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    evidence_task = _evidence_task(handlers.tasks, workflow_id)
    _assert_live_evidence_task(evidence_task, workflow_id)
    evidence_exec = assert_latest_attempt_completed(
        handlers.executions, evidence_task, purpose="candidate evidence readiness"
    )
    if evidence_exec.status != ExecutionStatus.COMPLETED:
        raise CandidateCurrentnessError("latest candidate evidence execution is not COMPLETED")

    (
        marker,
        manifest,
        result_art,
        marker_snap,
        manifest_snap,
        result_snap,
        marker_doc,
        result_doc,
    ) = _load_evidence_triplet_with_snapshots(handlers, workflow_id, evidence_task, evidence_exec)
    marker_doc_canonical = _canonical_doc(marker_doc)
    result_doc_canonical = _canonical_doc(result_doc)

    marker_d1, d_ready, container_root, stored_trusted = _inspect_publication_triplet(
        handlers,
        workflow_id,
        evidence_task,
        evidence_exec,
        upstream,
        marker=marker,
        manifest=manifest,
        result_art=result_art,
        marker_snap=marker_snap,
        manifest_snap=manifest_snap,
        result_snap=result_snap,
        marker_doc=marker_doc,
        result_doc=result_doc,
    )
    stored_trusted_canonical = canonical_trusted_cold_result_json(stored_trusted)

    cold_bundle_dir = (handlers.root / manifest.relative_path).parent
    fresh_trusted = trusted_cold_verify_candidate_bundle(cold_bundle_dir)
    fresh_trusted_canonical = canonical_trusted_cold_result_json(fresh_trusted)
    if stored_trusted_canonical != fresh_trusted_canonical:
        raise CandidateCurrentnessError("stored trusted cold result does not match fresh cold")

    marker_d1_after, d_ready_after, container_after, stored_trusted_after = (
        _inspect_publication_triplet(
            handlers,
            workflow_id,
            evidence_task,
            evidence_exec,
            upstream,
            marker=marker,
            manifest=manifest,
            result_art=result_art,
            marker_snap=marker_snap,
            manifest_snap=manifest_snap,
            result_snap=result_snap,
            marker_doc=marker_doc,
            result_doc=result_doc,
        )
    )
    if (
        marker_d1_after != marker_d1
        or d_ready_after != d_ready
        or container_after != container_root
    ):
        raise CandidateCurrentnessError(
            "publication payload drifted after trusted cold verification"
        )
    if canonical_trusted_cold_result_json(stored_trusted_after) != stored_trusted_canonical:
        raise CandidateCurrentnessError(
            "stored trusted cold result drifted after cold verification"
        )

    capture = _PublicationInspectionCapture(
        upstream_fingerprint=upstream.fingerprint(),
        upstream_payload_canonical=upstream_payload_canonical,
        evidence_task_id=evidence_task.id,
        evidence_execution_id=evidence_exec.id,
        evidence_attempt_number=evidence_exec.attempt_number,
        marker=marker_snap,
        manifest=manifest_snap,
        result_art=result_snap,
        marker_doc_canonical=marker_doc_canonical,
        result_doc_canonical=result_doc_canonical,
        stored_trusted_cold_canonical=stored_trusted_canonical,
        marker_d1=marker_d1,
        d_ready=d_ready,
        container_root=container_root,
    )

    with handlers.artifacts.db.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        task_repo = TaskRepository(handlers.tasks.db)
        exec_repo = ExecutionRepository(handlers.executions.db)
        tasks = task_repo.list_by_workflow(workflow_id, conn=conn)
        live_task = next(
            (row for row in tasks if row.task_type == "v08_candidate_evidence"),
            None,
        )
        if live_task is None:
            raise CandidateCurrentnessError("candidate evidence task missing under writer lock")
        if live_task.id != capture.evidence_task_id:
            raise CandidateCurrentnessError("candidate evidence task identity drifted under lock")
        if live_task.status != TaskStatus.COMPLETED:
            raise CandidateCurrentnessError("candidate evidence task is not completed under lock")

        attempts = exec_repo.list_by_task(live_task.id, conn=conn)
        if not attempts:
            raise CandidateCurrentnessError("candidate evidence execution missing under lock")
        live_exec = max(attempts, key=lambda row: row.attempt_number)
        if live_exec.id != capture.evidence_execution_id:
            raise CandidateCurrentnessError("candidate evidence execution changed under lock")
        if live_exec.attempt_number != capture.evidence_attempt_number:
            raise CandidateCurrentnessError("candidate evidence attempt changed under lock")
        if live_exec.status != ExecutionStatus.COMPLETED:
            raise CandidateCurrentnessError(
                "latest candidate evidence execution is not COMPLETED under lock"
            )

        rechecked_upstream = candidate_workflow_readiness(handlers, workflow_id)
        if rechecked_upstream.fingerprint() != capture.upstream_fingerprint:
            raise CandidateCurrentnessError("upstream candidate snapshot drifted under writer lock")
        rechecked_payload = json.dumps(
            rechecked_upstream.payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if rechecked_payload != capture.upstream_payload_canonical:
            raise CandidateCurrentnessError(
                "upstream candidate snapshot payload drifted under lock"
            )

        artifact_rows = _list_artifacts_on_connection(conn, workflow_id)
        marker_live = select_artifact_for_execution(
            artifact_rows,
            task_id=live_task.id,
            artifact_type="candidate-c2-export-marker",
            execution=live_exec,
        )
        manifest_live = select_artifact_for_execution(
            artifact_rows,
            task_id=live_task.id,
            artifact_type="candidate-c2-evidence-manifest",
            execution=live_exec,
        )
        result_live = select_artifact_for_execution(
            artifact_rows,
            task_id=live_task.id,
            artifact_type="candidate-c2-evidence-result",
            execution=live_exec,
        )
        marker_live_snap = _artifact_snapshot(marker_live)
        manifest_live_snap = _artifact_snapshot(manifest_live)
        result_live_snap = _artifact_snapshot(result_live)
        _assert_snapshots_unchanged(capture.marker, marker_live_snap, label="completion marker")
        _assert_snapshots_unchanged(capture.manifest, manifest_live_snap, label="evidence manifest")
        _assert_snapshots_unchanged(capture.result_art, result_live_snap, label="evidence result")

        for art in (marker_live, manifest_live, result_live):
            verify_artifact_bytes_and_hash(
                art, root_path=handlers.root, artifact_manager=handlers.artifact_manager
            )

        marker_doc_live = load_bounded_publication_control_json(
            handlers.root / marker_live.relative_path,
            label=marker_live.relative_path,
        )
        result_doc_live = load_bounded_publication_control_json(
            handlers.root / result_live.relative_path,
            label=result_live.relative_path,
        )
        if _canonical_doc(marker_doc_live) != capture.marker_doc_canonical:
            raise CandidateCurrentnessError("completion marker metadata drifted under lock")
        if _canonical_doc(result_doc_live) != capture.result_doc_canonical:
            raise CandidateCurrentnessError("evidence result metadata drifted under lock")

        marker_d1_locked, d_ready_locked, container_locked, trusted_locked = (
            _inspect_publication_triplet(
                handlers,
                workflow_id,
                live_task,
                live_exec,
                rechecked_upstream,
                marker=marker_live,
                manifest=manifest_live,
                result_art=result_live,
                marker_snap=marker_live_snap,
                manifest_snap=manifest_live_snap,
                result_snap=result_live_snap,
                marker_doc=marker_doc_live,
                result_doc=result_doc_live,
            )
        )
        if marker_d1_locked != capture.marker_d1:
            raise CandidateCurrentnessError("cold bundle digest drifted under writer lock")
        if d_ready_locked != capture.d_ready:
            raise CandidateCurrentnessError(
                "publication container digest drifted under writer lock"
            )
        if container_locked != capture.container_root:
            raise CandidateCurrentnessError("publication container path drifted under writer lock")
        if (
            canonical_trusted_cold_result_json(trusted_locked)
            != capture.stored_trusted_cold_canonical
        ):
            raise CandidateCurrentnessError("trusted cold result drifted under writer lock")

    return CandidateEvidenceReadiness(
        snapshot=rechecked_upstream,
        evidence_execution_id=capture.evidence_execution_id,
        evidence_attempt_number=capture.evidence_attempt_number,
        manifest_artifact_id=manifest_live.id,
        result_artifact_id=result_live.id,
        marker_artifact_id=marker_live.id,
        candidate_evidence_complete=True,
        production_eligible=False,
        promotion_eligible=False,
    )


def assert_candidate_evidence_complete(
    handlers: CandidateWorkflowHandlers, workflow_id: str
) -> CandidateEvidenceReadiness:
    """Raise when evidence completion invariants fail; return typed readiness on success."""
    readiness = candidate_evidence_readiness(handlers, workflow_id)
    if not readiness.candidate_evidence_complete:
        raise CandidateCurrentnessError("candidate evidence is not complete")
    if readiness.production_eligible or readiness.promotion_eligible:
        raise CandidateCurrentnessError("candidate evidence must remain production-ineligible")
    return readiness


def assert_candidate_workflow_engine_finalized(
    handlers: CandidateWorkflowHandlers, workflow_id: str
) -> CandidateEvidenceReadiness:
    """Engine-level finalization gate: workflow completed is insufficient without evidence readiness."""
    from gamefactory.adapters.persistence.repositories import WorkflowRepository
    from gamefactory.core.domain.models import WorkflowStatus

    workflow = WorkflowRepository(handlers.artifacts.db).get(workflow_id)
    if workflow is None:
        raise ValidationError("workflow is missing")
    if workflow.status != WorkflowStatus.COMPLETED:
        raise CandidateCurrentnessError("workflow is not engine-finalized to COMPLETED")
    return assert_candidate_evidence_complete(handlers, workflow_id)


__all__ = [
    "CandidateEvidenceReadiness",
    "assert_candidate_evidence_complete",
    "assert_candidate_workflow_engine_finalized",
    "candidate_evidence_readiness",
    "trusted_cold_verify_candidate_bundle",
]
