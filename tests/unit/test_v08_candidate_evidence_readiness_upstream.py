"""UPSTREAM-READINESS race/currentness for completed managed candidate evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gamefactory.workflows.v08_candidate_evidence_readiness import candidate_evidence_readiness
from tests.unit.v08_candidate_c2b_readiness_fixtures import run_completed_managed_evidence
from tests.unit.v08_candidate_upstream_fixtures import (
    UpstreamTiming,
    artifact_by_type,
    assert_matched_processed_glb_baseline_bindings,
    assert_upstream_baseline_passes_readiness,
    asset_revision_row,
    drift_source_retained_registration_hash,
    drift_source_retained_relative_path,
    mutate_approval_foreign_task,
    mutate_approval_operation_hash,
    mutate_receipt_coherent_field,
    mutate_retained_profile_bounds_tolerance,
    mutate_retained_spec_semantic_intent,
    mutate_retained_spec_whitespace,
    run_completed_managed_evidence_with_matched_processed_hash,
    run_negative_upstream_readiness,
    set_revision_processed_glb_hash,
    upstream_drift_expected_cause,
)

pytestmark = pytest.mark.candidate_slow


@pytest.fixture
def completed_upstream_ctx(tmp_path: Path):
    return run_completed_managed_evidence(tmp_path)


_UPSTREAM_DRIFT_CASES: tuple[tuple[str, Any], ...] = (
    (
        "source_retained_hash_registration",
        lambda ctx: drift_source_retained_registration_hash(ctx),
    ),
    (
        "source_retained_path_registration",
        lambda ctx: drift_source_retained_relative_path(ctx),
    ),
    (
        "spec_semantic",
        lambda ctx: mutate_retained_spec_semantic_intent(ctx),
    ),
    (
        "profile_semantic",
        lambda ctx: mutate_retained_profile_bounds_tolerance(ctx),
    ),
    (
        "spec_whitespace",
        lambda ctx: mutate_retained_spec_whitespace(ctx),
    ),
    (
        "approval_operation_hash",
        lambda ctx: mutate_approval_operation_hash(ctx),
    ),
    (
        "approval_foreign_task",
        lambda ctx: mutate_approval_foreign_task(ctx),
    ),
    (
        "receipt_snapshot_fingerprint",
        lambda ctx: mutate_receipt_coherent_field(
            ctx, field="snapshot_fingerprint", value="c" * 64
        ),
    ),
    (
        "receipt_approval_operation_hash",
        lambda ctx: mutate_receipt_coherent_field(
            ctx, field="approval_operation_hash", value="d" * 64
        ),
    ),
)


@pytest.mark.parametrize(
    "timing", ["after_completion", "during_only_cold"], ids=["after", "during_cold"]
)
@pytest.mark.parametrize(
    "case_id,mutator", _UPSTREAM_DRIFT_CASES, ids=[c[0] for c in _UPSTREAM_DRIFT_CASES]
)
def test_upstream_rejects_live_drift_after_positive_readiness(
    completed_upstream_ctx,
    timing: UpstreamTiming,
    case_id: str,
    mutator: Any,
) -> None:
    ctx = completed_upstream_ctx
    recorded: dict[str, Any] = {}

    def _mutate() -> None:
        recorded["mutation"] = mutator(ctx)

    flags = run_negative_upstream_readiness(
        ctx,
        timing=timing,
        mutate=_mutate,
        match=upstream_drift_expected_cause(case_id),
    )
    assert recorded["mutation"]
    mutation = recorded["mutation"]
    if "before_path" in mutation:
        assert mutation["before_path"] != mutation["after_path"]
        assert mutation["before_hash"] == mutation["after_hash"]
    elif "before_hash" in mutation and "after_hash" in mutation:
        assert mutation["before_hash"] != mutation["after_hash"]
    if "before_operation_hash" in mutation and "after_operation_hash" in mutation:
        assert mutation["before_operation_hash"] != mutation["after_operation_hash"]
    if timing == "during_only_cold":
        assert flags["cold_ran"] is True
        assert flags["mutated"] is True
    else:
        assert flags["cold_ran"] is False
        assert flags["mutated"] is True


def test_upstream_null_processed_glb_baseline_readiness_and_snapshot(
    tmp_path: Path,
) -> None:
    ctx = run_completed_managed_evidence(tmp_path)
    row = asset_revision_row(ctx)
    assert row.processed_glb_hash is None
    readiness = candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert readiness.snapshot.payload.get("asset_revision_processed_glb_hash") is None
    assert_upstream_baseline_passes_readiness(ctx)


def test_upstream_matched_processed_glb_baseline_readiness_and_snapshot(
    tmp_path: Path,
) -> None:
    ctx = run_completed_managed_evidence_with_matched_processed_hash(tmp_path)
    processed = artifact_by_type(ctx, "candidate-processed-glb")
    row = asset_revision_row(ctx)
    assert row.processed_glb_hash == processed.content_hash
    readiness = candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert (
        readiness.snapshot.payload.get("asset_revision_processed_glb_hash")
        == processed.content_hash
    )
    assert_matched_processed_glb_baseline_bindings(ctx)


_REVISION_TRANSITIONS: tuple[tuple[str, Any], ...] = (
    (
        "revision_null_to_processed",
        lambda ctx: set_revision_processed_glb_hash(
            ctx,
            artifact_by_type(ctx, "candidate-processed-glb").content_hash,
        ),
    ),
    ("revision_matched_to_null", lambda ctx: set_revision_processed_glb_hash(ctx, None)),
    (
        "revision_matched_to_foreign_hash",
        lambda ctx: set_revision_processed_glb_hash(ctx, "e" * 64),
    ),
)


@pytest.mark.parametrize(
    "timing", ["after_completion", "during_only_cold"], ids=["after", "during_cold"]
)
@pytest.mark.parametrize(
    "case_id,mutator", _REVISION_TRANSITIONS, ids=[c[0] for c in _REVISION_TRANSITIONS]
)
def test_upstream_rejects_processed_glb_hash_transition(
    tmp_path: Path,
    timing: UpstreamTiming,
    case_id: str,
    mutator: Any,
) -> None:
    if case_id == "revision_null_to_processed":
        ctx = run_completed_managed_evidence(tmp_path)
        assert asset_revision_row(ctx).processed_glb_hash is None
    else:
        ctx = run_completed_managed_evidence_with_matched_processed_hash(tmp_path)
        processed = artifact_by_type(ctx, "candidate-processed-glb")
        assert asset_revision_row(ctx).processed_glb_hash == processed.content_hash
        assert_matched_processed_glb_baseline_bindings(ctx)

    sql_before = asset_revision_row(ctx).processed_glb_hash
    recorded: dict[str, Any] = {}

    def _mutate() -> None:
        recorded["sql_transition"] = mutator(ctx)

    flags = run_negative_upstream_readiness(
        ctx,
        timing=timing,
        mutate=_mutate,
        match=upstream_drift_expected_cause(case_id),
    )
    before_hash, after_hash = recorded["sql_transition"]
    assert before_hash == sql_before
    assert before_hash != after_hash
    if timing == "during_only_cold":
        assert flags["cold_ran"] is True
        assert flags["mutated"] is True
