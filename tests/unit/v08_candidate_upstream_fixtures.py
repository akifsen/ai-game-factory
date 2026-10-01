"""Test-only helpers for completed-evidence upstream race/currentness cases."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import pytest

from gamefactory.adapters.assets.v08_candidate_evidence import (
    bind_production_candidate_evidence_exporter,
)
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    ExecutionRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.domain.errors import ArtifactError
from gamefactory.core.domain.models import ApprovalStatus, WorkflowStatus
from gamefactory.core.domain.v08_candidate_contracts import (
    AssetProfileV08Candidate,
    ProfileDocumentV08Candidate,
    parse_asset_specification_v08_candidate,
)
from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
from gamefactory.workflows.v08_candidate_evidence_readiness import candidate_evidence_readiness
from gamefactory.workflows.v08_candidate_snapshot import CANDIDATE_TEST_ONLY_APPROVAL
from tests.unit.test_v08_candidate_workflow import _approve, _handlers, _run_to_test_only_gate
from tests.unit.v08_candidate_c2b_readiness_fixtures import (
    CompletedEvidenceContext,
    patch_during_only_cold,
)

UpstreamTiming = Literal["after_completion", "during_only_cold"]


def sha256_hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rehash_registered_artifact(ctx: CompletedEvidenceContext, art: Any) -> Any:
    path = ctx.workspace.root / art.relative_path
    updated = replace(
        art,
        content_hash=sha256_hex(path),
        file_size=path.stat().st_size,
    )
    ArtifactRepository(ctx.workspace.db).save(updated)
    return updated


def assert_upstream_baseline_passes_readiness(ctx: CompletedEvidenceContext) -> None:
    readiness = candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert readiness.candidate_evidence_complete is True


def assert_matched_processed_glb_baseline_bindings(ctx: CompletedEvidenceContext) -> None:
    processed = artifact_by_type(ctx, "candidate-processed-glb")
    row = asset_revision_row(ctx)
    assert row.processed_glb_hash == processed.content_hash
    readiness = candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert readiness.candidate_evidence_complete is True
    assert (
        readiness.snapshot.payload.get("asset_revision_processed_glb_hash")
        == processed.content_hash
    )
    receipt_art = artifact_by_type(ctx, "candidate-test-only-receipt")
    receipt = json.loads(
        (ctx.workspace.root / receipt_art.relative_path).read_text(encoding="utf-8")
    )
    assert receipt.get("snapshot_fingerprint") == readiness.snapshot.fingerprint()
    assert receipt.get("approval_operation_hash")


def assert_test_only_approval_still_approved(ctx: CompletedEvidenceContext) -> None:
    rows = [
        a
        for a in ApprovalRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if a.approval_type == CANDIDATE_TEST_ONLY_APPROVAL
    ]
    assert rows
    assert rows[0].status == ApprovalStatus.APPROVED
    assert rows[0].operation_hash


def run_negative_upstream_readiness(
    ctx: CompletedEvidenceContext,
    *,
    timing: UpstreamTiming,
    mutate: Callable[[], None],
    match: str,
) -> dict[str, bool]:
    flags = {"cold_ran": False, "mutated": False}
    assert_upstream_baseline_passes_readiness(ctx)
    if timing == "after_completion":
        mutate()
        flags["mutated"] = True
        with pytest.raises((CandidateCurrentnessError, ArtifactError), match=match):
            candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
        return flags

    with patch_during_only_cold(ctx.workspace.db, mutate) as cold_flags:
        with pytest.raises((CandidateCurrentnessError, ArtifactError), match=match):
            candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    flags["cold_ran"] = cold_flags["cold_ran"]
    flags["mutated"] = cold_flags["mutated"]
    return flags


def artifact_by_type(ctx: CompletedEvidenceContext, artifact_type: str) -> Any:
    return next(
        a
        for a in ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if a.artifact_type == artifact_type
    )


def asset_revision_row(ctx: CompletedEvidenceContext) -> Any:
    rows = AssetRevisionRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
    assert len(rows) == 1
    return rows[0]


def set_revision_processed_glb_hash(
    ctx: CompletedEvidenceContext,
    value: str | None,
) -> tuple[str | None, str | None]:
    row = asset_revision_row(ctx)
    before = row.processed_glb_hash
    with ctx.workspace.db.transaction() as conn:
        conn.execute(
            """
            UPDATE asset_revisions
            SET processed_glb_hash = ?
            WHERE asset_id = ? AND revision_number = ?;
            """,
            (value, row.asset_id, row.revision_number),
        )
    after = AssetRevisionRepository(ctx.workspace.db).get(row.asset_id, row.revision_number)
    assert after is not None
    after_hash = after.processed_glb_hash
    assert before != after_hash
    assert after_hash == value
    return before, after_hash


def drift_source_retained_registration_hash(ctx: CompletedEvidenceContext) -> dict[str, str]:
    art = artifact_by_type(ctx, "candidate-source-retained")
    path = ctx.workspace.root / art.relative_path
    before_hash = art.content_hash
    forged = "a" * 64
    assert forged != before_hash
    with ctx.workspace.db.transaction() as conn:
        conn.execute(
            "UPDATE artifacts SET content_hash = ? WHERE id = ?;",
            (forged, art.id),
        )
    return {
        "artifact_id": art.id,
        "path": str(path),
        "before_registration_hash": before_hash,
        "on_disk_hash": sha256_hex(path),
        "after_registration_hash": forged,
    }


def drift_source_retained_relative_path(ctx: CompletedEvidenceContext) -> dict[str, str]:
    art = artifact_by_type(ctx, "candidate-source-retained")
    prepare = next(
        t
        for t in TaskRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if t.task_type == "v08_candidate_prepare"
    )
    prepare_exec = ExecutionRepository(ctx.workspace.db).get_latest_attempt(prepare.id)
    assert prepare_exec is not None
    src = ctx.workspace.root / art.relative_path
    alt_dir = ctx.workspace.root / "upstream-retained-path-drift"
    alt_dir.mkdir(exist_ok=True)
    alt = alt_dir / f"source-retained-{prepare_exec.id}.glb"
    alt.write_bytes(src.read_bytes())
    rel = alt.relative_to(ctx.workspace.root).as_posix()
    before_path = art.relative_path
    with ctx.workspace.db.transaction() as conn:
        conn.execute(
            "UPDATE artifacts SET relative_path = ?, file_size = ?, content_hash = ? WHERE id = ?;",
            (rel, alt.stat().st_size, sha256_hex(alt), art.id),
        )
    return {
        "artifact_id": art.id,
        "before_path": before_path,
        "after_path": rel,
        "before_hash": art.content_hash,
        "after_hash": sha256_hex(alt),
    }


def mutate_retained_json_semantic(
    ctx: CompletedEvidenceContext,
    *,
    artifact_type: str,
    patch_doc: Callable[[dict[str, Any]], None],
) -> dict[str, str]:
    art = artifact_by_type(ctx, artifact_type)
    path = ctx.workspace.root / art.relative_path
    before_bytes = path.read_bytes()
    before_hash = sha256_hex(path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    patch_doc(doc)
    path.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    after_bytes = path.read_bytes()
    after_hash = sha256_hex(path)
    rehash_registered_artifact(ctx, art)
    assert before_hash != after_hash
    return {
        "artifact_id": art.id,
        "before_hash": before_hash,
        "after_hash": after_hash,
        "before_bytes_len": str(len(before_bytes)),
        "after_bytes_len": str(len(after_bytes)),
    }


def mutate_retained_spec_semantic_intent(ctx: CompletedEvidenceContext) -> dict[str, str]:
    def _patch(doc: dict[str, Any]) -> None:
        doc["intent"] = f"{doc['intent']} upstream semantic drift probe"

    meta = mutate_retained_json_semantic(
        ctx,
        artifact_type="candidate-specification",
        patch_doc=_patch,
    )
    path = ctx.workspace.root / artifact_by_type(ctx, "candidate-specification").relative_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    parse_asset_specification_v08_candidate(doc)
    return meta


def mutate_retained_profile_bounds_tolerance(ctx: CompletedEvidenceContext) -> dict[str, str]:
    profile_art = artifact_by_type(ctx, "candidate-profile-document")
    spec_art = artifact_by_type(ctx, "candidate-specification")

    def _patch_profile(doc: dict[str, Any]) -> None:
        runtime = doc["runtime"]
        assert runtime["bounds_tolerance_ratio"] == 0.2
        runtime["bounds_tolerance_ratio"] = 0.21

    profile_meta = mutate_retained_json_semantic(
        ctx,
        artifact_type="candidate-profile-document",
        patch_doc=_patch_profile,
    )
    profile_path = ctx.workspace.root / profile_art.relative_path
    profile_doc = json.loads(profile_path.read_text(encoding="utf-8"))
    validated_profile = ProfileDocumentV08Candidate.model_validate(profile_doc)
    from gamefactory.core.domain.v08_candidate_contracts import profile_document_hash

    new_profile_hash = profile_document_hash(validated_profile)
    spec_path = ctx.workspace.root / spec_art.relative_path
    spec_before_hash = sha256_hex(spec_path)
    spec_doc = json.loads(spec_path.read_text(encoding="utf-8"))
    spec_doc["profile_document_hash"] = new_profile_hash
    spec_path.write_text(json.dumps(spec_doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    spec_after_hash = sha256_hex(spec_path)
    rehash_registered_artifact(ctx, spec_art)
    bound_profile = AssetProfileV08Candidate(document=validated_profile)
    parse_asset_specification_v08_candidate(spec_doc, profile=bound_profile)
    assert profile_meta["before_hash"] != profile_meta["after_hash"]
    assert spec_before_hash != spec_after_hash
    return {
        **profile_meta,
        "spec_artifact_id": spec_art.id,
        "spec_before_hash": spec_before_hash,
        "spec_after_hash": spec_after_hash,
    }


def mutate_retained_spec_whitespace(ctx: CompletedEvidenceContext) -> dict[str, str]:
    art = artifact_by_type(ctx, "candidate-specification")
    path = ctx.workspace.root / art.relative_path
    before_hash = sha256_hex(path)
    raw = path.read_bytes()
    path.write_bytes(raw + b"\n")
    after_hash = sha256_hex(path)
    rehash_registered_artifact(ctx, art)
    assert before_hash != after_hash
    return {"before_hash": before_hash, "after_hash": after_hash, "artifact_id": art.id}


def mutate_approval_operation_hash(ctx: CompletedEvidenceContext) -> dict[str, str]:
    receipt_art = artifact_by_type(ctx, "candidate-test-only-receipt")
    receipt = json.loads(
        (ctx.workspace.root / receipt_art.relative_path).read_text(encoding="utf-8")
    )
    approval = ApprovalRepository(ctx.workspace.db).get(str(receipt["approval_id"]))
    assert approval is not None
    assert approval.operation_hash
    stale = "0" * 64
    before_operation_hash = approval.operation_hash
    with ctx.workspace.db.transaction() as conn:
        conn.execute(
            "UPDATE approvals SET operation_hash = ? WHERE id = ?;",
            (stale, approval.id),
        )
    assert_test_only_approval_still_approved(ctx)
    reloaded = ApprovalRepository(ctx.workspace.db).get(approval.id)
    assert reloaded is not None
    assert reloaded.operation_hash == stale
    assert before_operation_hash != stale
    return {
        "approval_id": approval.id,
        "before_operation_hash": before_operation_hash,
        "after_operation_hash": stale,
    }


def mutate_approval_foreign_task(ctx: CompletedEvidenceContext) -> dict[str, str]:
    receipt_art = artifact_by_type(ctx, "candidate-test-only-receipt")
    receipt = json.loads(
        (ctx.workspace.root / receipt_art.relative_path).read_text(encoding="utf-8")
    )
    approval = ApprovalRepository(ctx.workspace.db).get(str(receipt["approval_id"]))
    assert approval is not None
    review = next(
        t
        for t in TaskRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if t.task_type == "v08_candidate_test_only_review"
    )
    foreign = next(
        t
        for t in TaskRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if t.id != review.id
    )
    before_task = approval.task_id
    with ctx.workspace.db.transaction() as conn:
        conn.execute(
            "UPDATE approvals SET task_id = ? WHERE id = ?;",
            (foreign.id, approval.id),
        )
    assert_test_only_approval_still_approved(ctx)
    return {
        "approval_id": approval.id,
        "before_task_id": before_task,
        "after_task_id": foreign.id,
    }


def mutate_receipt_coherent_field(
    ctx: CompletedEvidenceContext,
    *,
    field: str,
    value: str,
) -> dict[str, str]:
    art = artifact_by_type(ctx, "candidate-test-only-receipt")
    path = ctx.workspace.root / art.relative_path
    before_hash = sha256_hex(path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    before_value = str(doc.get(field, ""))
    doc[field] = value
    path.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    rehash_registered_artifact(ctx, art)
    after_hash = sha256_hex(path)
    return {
        "artifact_id": art.id,
        "field": field,
        "before_value": before_value,
        "after_value": value,
        "before_hash": before_hash,
        "after_hash": after_hash,
    }


def _run_to_test_only_gate_with_matched_processed_hash(
    workspace: Any,
    handlers: Any,
) -> tuple[Any, str, Any]:
    """Bind matched processed_glb_hash, refresh TEST_ONLY pending approval, then stop at gate."""
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    db = workspace.db
    revisions = AssetRevisionRepository(db)
    row = revisions.list_by_workflow(workflow_id)[0]
    processed = next(
        a
        for a in ArtifactRepository(db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-processed-glb"
    )
    processed_hash = processed.content_hash
    revisions.save(replace(row, processed_glb_hash=processed_hash))
    blocked = engine.run_workflow(workflow_id)
    assert blocked.pending_approval_id
    prepare = next(
        t
        for t in TaskRepository(db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_prepare"
    )
    wf = WorkflowRepository(db).get(workflow_id)
    assert wf is not None
    bound = handlers._bound_snapshot(wf, prepare)
    assert bound.payload.get("asset_revision_processed_glb_hash") == processed_hash
    approval = ApprovalRepository(db).get(blocked.pending_approval_id)
    assert approval is not None
    assert approval.approval_type == CANDIDATE_TEST_ONLY_APPROVAL
    return engine, workflow_id, handlers


def _complete_managed_evidence_from_test_only_gate(
    workspace: Any,
    engine: Any,
    workflow_id: str,
    handlers: Any,
) -> CompletedEvidenceContext:
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


def run_completed_managed_evidence_with_matched_processed_hash(
    tmp_path: Path,
) -> CompletedEvidenceContext:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate_with_matched_processed_hash(
        workspace, handlers
    )
    ctx = _complete_managed_evidence_from_test_only_gate(workspace, engine, workflow_id, handlers)
    processed = next(
        a
        for a in ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-processed-glb"
    )
    matched_row = AssetRevisionRepository(workspace.db).list_by_workflow(workflow_id)[0]
    assert matched_row.processed_glb_hash == processed.content_hash
    assert_matched_processed_glb_baseline_bindings(ctx)
    return ctx


def upstream_drift_expected_cause(case_id: str) -> str:
    patterns: dict[str, str] = {
        "source_retained_hash_registration": (
            r"retained source|registration hash drifted|content_hash mismatch|"
            r"Artifact integrity failure|upstream candidate snapshot drifted"
        ),
        "source_retained_path_registration": (
            r"persisted approval operation_hash is stale for approved artifact scope bindings"
        ),
        "spec_semantic": (
            r"retained specification (artifact does not match prepare specification_hash|"
            r"semantics drift from prepare params)|upstream candidate snapshot (drifted|payload drifted)"
        ),
        "profile_semantic": (
            r"retained profile (document artifact does not match prepare profile_document_hash|"
            r"semantics drift from prepare params)|"
            r"retained specification artifact invalid: profile_document_hash does not match|"
            r"upstream candidate snapshot (drifted under writer lock|payload drifted under lock)"
        ),
        "spec_whitespace": (
            r"persisted approval operation_hash is stale for approved artifact scope bindings"
        ),
        "approval_operation_hash": (
            r"persisted approval operation_hash is stale|operation_hash is stale"
        ),
        "approval_foreign_task": r"persisted approval task_id mismatch",
        "receipt_snapshot_fingerprint": r"TEST_ONLY receipt snapshot is stale for current bindings",
        "receipt_approval_operation_hash": r"TEST_ONLY receipt approval_operation_hash mismatch",
        "revision_null_to_processed": (
            r"asset revision processed_glb_hash does not match|"
            r"persisted approval operation_hash is stale for approved artifact scope bindings|"
            r"upstream candidate snapshot (drifted under writer lock|payload drifted under lock)|"
            r"asset_revision_processed_glb_hash"
        ),
        "revision_matched_to_null": (
            r"asset revision processed_glb_hash does not match|"
            r"persisted approval operation_hash is stale for approved artifact scope bindings|"
            r"upstream candidate snapshot (drifted under writer lock|payload drifted under lock)|"
            r"asset_revision_processed_glb_hash"
        ),
        "revision_matched_to_foreign_hash": (
            r"asset revision processed_glb_hash does not match|"
            r"persisted approval operation_hash is stale for approved artifact scope bindings|"
            r"upstream candidate snapshot (drifted under writer lock|payload drifted under lock)|"
            r"asset_revision_processed_glb_hash"
        ),
    }
    return patterns[case_id]


__all__ = [
    "UpstreamTiming",
    "assert_matched_processed_glb_baseline_bindings",
    "assert_upstream_baseline_passes_readiness",
    "artifact_by_type",
    "asset_revision_row",
    "drift_source_retained_registration_hash",
    "drift_source_retained_relative_path",
    "mutate_approval_foreign_task",
    "mutate_approval_operation_hash",
    "mutate_receipt_coherent_field",
    "mutate_retained_json_semantic",
    "mutate_retained_profile_bounds_tolerance",
    "mutate_retained_spec_semantic_intent",
    "mutate_retained_spec_whitespace",
    "rehash_registered_artifact",
    "run_completed_managed_evidence_with_matched_processed_hash",
    "run_negative_upstream_readiness",
    "set_revision_processed_glb_hash",
    "upstream_drift_expected_cause",
]
