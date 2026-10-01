"""Test-only helpers for C2-B evidence readiness matrix cases."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from gamefactory.adapters.assets.v08_candidate_evidence import (
    bind_production_candidate_evidence_exporter,
    trusted_cold_verify_candidate_bundle,
)
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    ExecutionRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.domain.models import (
    ApprovalStatus,
    Execution,
    ExecutionStatus,
    WorkflowStatus,
    generate_id,
    utc_now_iso,
)
from gamefactory.workflows.v08_candidate_workflow import CandidateWorkflowHandlers
from tests.unit.test_v08_candidate_workflow import _approve, _handlers, _run_to_test_only_gate

C2_MARKER = "candidate-c2-export-marker"
C2_MANIFEST = "candidate-c2-evidence-manifest"
C2_RESULT = "candidate-c2-evidence-result"

ARTIFACT_REGISTRATION_LABEL: dict[str, str] = {
    C2_MARKER: "completion marker",
    C2_MANIFEST: "evidence manifest",
    C2_RESULT: "evidence result",
}

ARTIFACT_REGISTRATION_FIELDS: tuple[str, ...] = (
    "id",
    "workflow_id",
    "task_id",
    "artifact_type",
    "producer",
    "relative_path",
    "content_hash",
    "file_size",
    "validation_state",
    "created_at",
)


@dataclass(frozen=True)
class CompletedEvidenceContext:
    workspace: Any
    workflow_id: str
    handlers: CandidateWorkflowHandlers
    evidence_task_id: str
    evidence_execution_id: str


def run_completed_managed_evidence(tmp_path: Path) -> CompletedEvidenceContext:
    """Run managed unit workflow through evidence publication to COMPLETED."""
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    blocked = engine.run_workflow(workflow_id)
    assert blocked.pending_approval_id
    _approve(engine, workspace.db, blocked.pending_approval_id)
    engine.run_workflow(workflow_id)
    final = engine.run_workflow(workflow_id)
    if final.status != WorkflowStatus.COMPLETED:
        evidence = next(
            t
            for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
            if t.task_type == "v08_candidate_evidence"
        )
        latest = ExecutionRepository(workspace.db).get_latest_attempt(evidence.id)
        raise AssertionError(latest.error_message if latest else final.error_message)
    evidence_task = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    evidence_exec = ExecutionRepository(workspace.db).get_latest_attempt(evidence_task.id)
    assert evidence_exec is not None
    return CompletedEvidenceContext(
        workspace=workspace,
        workflow_id=workflow_id,
        handlers=handlers,
        evidence_task_id=evidence_task.id,
        evidence_execution_id=evidence_exec.id,
    )


def cold_bundle_dir_for_workflow(ctx: CompletedEvidenceContext) -> Path:
    manifest = next(
        a
        for a in ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if a.artifact_type == C2_MANIFEST
    )
    return (ctx.workspace.root / manifest.relative_path).parent


def fresh_trusted_cold_dict(ctx: CompletedEvidenceContext) -> dict[str, Any]:
    return trusted_cold_verify_candidate_bundle(cold_bundle_dir_for_workflow(ctx))


@contextmanager
def patch_during_only_cold(
    workspace_db: Any,
    mutate: Callable[[], None],
) -> Iterator[dict[str, bool]]:
    from gamefactory.workflows import v08_candidate_evidence_readiness as readiness_mod

    flags = {"cold_ran": False, "mutated": False}
    original = readiness_mod.trusted_cold_verify_candidate_bundle

    def _cold_then_mutate(bundle_dir: Path) -> dict[str, Any]:
        result = original(bundle_dir)
        flags["cold_ran"] = True
        mutate()
        flags["mutated"] = True
        return result

    with patch.object(readiness_mod, "trusted_cold_verify_candidate_bundle", _cold_then_mutate):
        yield flags


def coherent_rehash_result_trusted(
    ctx: CompletedEvidenceContext,
    *,
    patch_trusted: Callable[[dict[str, Any], dict[str, Any]], None],
) -> None:
    """Update stored result trusted_cold_result with coherent file + marker cross-hash."""
    repo = ArtifactRepository(ctx.workspace.db)
    result = next(a for a in repo.list_by_workflow(ctx.workflow_id) if a.artifact_type == C2_RESULT)
    marker = next(a for a in repo.list_by_workflow(ctx.workflow_id) if a.artifact_type == C2_MARKER)
    result_path = ctx.workspace.root / result.relative_path
    marker_path = ctx.workspace.root / marker.relative_path
    doc = json.loads(result_path.read_text(encoding="utf-8"))
    trusted = dict(doc["trusted_cold_result"])
    fresh = fresh_trusted_cold_dict(ctx)
    patch_trusted(trusted, fresh)
    doc["trusted_cold_result"] = trusted
    result_path.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    result_hash = hashlib.sha256(result_path.read_bytes()).hexdigest()
    repo.save(replace(result, content_hash=result_hash, file_size=result_path.stat().st_size))
    marker_doc = json.loads(marker_path.read_text(encoding="utf-8"))
    marker_doc["result_sha256"] = result_hash
    marker_path.write_text(
        json.dumps(marker_doc, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    repo.save(
        replace(
            marker,
            content_hash=hashlib.sha256(marker_path.read_bytes()).hexdigest(),
            file_size=marker_path.stat().st_size,
        )
    )


def inject_newer_evidence_attempt(
    ctx: CompletedEvidenceContext,
    *,
    status: ExecutionStatus,
    attempt_number: int = 99,
) -> str:
    exec_id = generate_id("EXEC")
    ExecutionRepository(ctx.workspace.db).save(
        Execution(
            id=exec_id,
            task_id=ctx.evidence_task_id,
            attempt_number=attempt_number,
            status=status,
            started_at=utc_now_iso(),
            completed_at=utc_now_iso() if status != ExecutionStatus.RUNNING else None,
            external_op_id=None,
            pid=None,
            host=None,
            error_message="readiness-matrix adversarial attempt"
            if status == ExecutionStatus.FAILED
            else None,
            exit_code=1 if status == ExecutionStatus.FAILED else None,
            stdout="",
            stderr="",
            cost=0,
            estimated_cost=0,
            cost_unit="usd",
            provider=None,
            retryable=False,
        )
    )
    return exec_id


def clone_sibling_workflow_row(ctx: CompletedEvidenceContext) -> str:
    """Insert a second workflow row in the same DB for FK-valid artifact workflow_id drift."""
    other_id = generate_id("WF")
    workflow = WorkflowRepository(ctx.workspace.db).get(ctx.workflow_id)
    if workflow is None:
        raise AssertionError("completed evidence context missing workflow row")
    WorkflowRepository(ctx.workspace.db).save(
        replace(workflow, id=other_id, name=f"{workflow.name}-readiness-matrix-clone")
    )
    return other_id


def registration_field_drift_expected_cause(field: str, artifact_type: str) -> str:
    """Branch-specific readiness rejection causes for artifact registration drift under lock."""
    label = ARTIFACT_REGISTRATION_LABEL[artifact_type]
    type_token = re.escape(artifact_type)
    if field == "workflow_id":
        return rf"expected exactly one {type_token} bound to execution"
    if field == "task_id":
        return (
            rf"expected exactly one {type_token} bound to execution|"
            rf"{label} registration metadata drifted after inspection|"
            rf"{type_token} task_id mismatch"
        )
    if field == "artifact_type":
        return (
            rf"expected exactly one {type_token} bound to execution|"
            rf"{label} registration metadata drifted after inspection"
        )
    if field == "relative_path":
        return (
            r"Artifact integrity failure: content hash mismatch|"
            rf"{label} registration metadata drifted after inspection|"
            rf"{type_token} path missing on disk|"
            rf"{type_token} registered file_size mismatch|"
            rf"{type_token} registered content_hash mismatch"
        )
    if field == "content_hash":
        return (
            r"Artifact integrity failure: content hash mismatch|"
            rf"{label} registration metadata drifted after inspection|"
            rf"{type_token} registered content_hash mismatch"
        )
    if field == "file_size":
        return (
            rf"{label} registration metadata drifted after inspection|"
            rf"{type_token} registered file_size mismatch"
        )
    if field == "validation_state":
        return (
            rf"{label} registration metadata drifted after inspection|"
            rf"{type_token} validation_state is not VALID"
        )
    if field == "producer":
        return (
            rf"{label} registration metadata drifted after inspection|"
            rf"{type_token} producer mismatch"
        )
    return rf"{label} registration metadata drifted after inspection"


def publication_result_binding_mismatch_cause(binding_field: str) -> str:
    return rf"publication result binding mismatch for {re.escape(binding_field)}"


def mutate_artifact_registration_field(
    ctx: CompletedEvidenceContext,
    *,
    artifact_type: str,
    field: str,
    value: str,
) -> str:
    art = next(
        a
        for a in ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if a.artifact_type == artifact_type
    )
    allowed = set(ARTIFACT_REGISTRATION_FIELDS)
    if field not in allowed:
        raise ValueError(f"unsupported artifact registration field: {field}")
    with ctx.workspace.db.transaction() as conn:
        conn.execute(
            f"UPDATE artifacts SET {field} = ? WHERE id = ?;",
            (value, art.id),
        )
    refreshed = ArtifactRepository(ctx.workspace.db).get(value if field == "id" else art.id)
    assert refreshed is not None
    persisted = getattr(refreshed, field)
    return str(persisted)


def assert_db_test_only_approval_still_approved(ctx: CompletedEvidenceContext) -> None:
    from gamefactory.workflows.v08_candidate_snapshot import CANDIDATE_TEST_ONLY_APPROVAL

    rows = [
        a
        for a in ApprovalRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if a.approval_type == CANDIDATE_TEST_ONLY_APPROVAL
    ]
    assert rows
    assert rows[0].status == ApprovalStatus.APPROVED
