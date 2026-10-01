"""MANAGED coherent private result/marker negative readiness matrix (controls layer)."""

from __future__ import annotations

from typing import Any

import pytest

from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
from gamefactory.workflows.v08_candidate_evidence_readiness import candidate_evidence_readiness
from tests.unit.v08_candidate_c2b_readiness_fixtures import (
    CompletedEvidenceContext,
    fresh_trusted_cold_dict,
    run_completed_managed_evidence,
)
from tests.unit.v08_candidate_managed_control_fixtures import (
    assert_managed_baseline_passes_readiness,
    assert_on_disk_registration_coherent,
    mutate_coherent_completion_marker_doc,
    mutate_coherent_manifest_bundle_id,
    mutate_coherent_publication_result_doc,
    mutate_coherent_trusted_cold,
)


def _fresh_ctx(tmp_path: Any) -> CompletedEvidenceContext:
    ctx = run_completed_managed_evidence(tmp_path)
    assert_managed_baseline_passes_readiness(ctx)
    assert_on_disk_registration_coherent(ctx)
    return ctx


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("execution_provenance", "AUTHENTICATED_PRODUCTION", r"execution_provenance"),
        ("production_eligible", True, r"eligibility flags"),
        ("promotion_eligible", True, r"eligibility flags"),
        ("candidate_evidence_complete", False, r"candidate_evidence_complete"),
    ],
    ids=["provenance", "production_eligible", "promotion_eligible", "completion"],
)
def test_managed_rejects_coherent_trusted_cold_contradictory_stored_semantics(
    tmp_path: Any,
    field: str,
    value: object,
    match: str,
) -> None:
    ctx = _fresh_ctx(tmp_path)

    def _patch(trusted: dict[str, Any], _fresh: dict[str, Any]) -> None:
        trusted[field] = value

    mutate_coherent_trusted_cold(ctx, patch_trusted=_patch)
    with pytest.raises(CandidateCurrentnessError, match=match):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


def test_managed_rejects_coherent_rehash_stale_request_digest(tmp_path: Any) -> None:
    ctx = _fresh_ctx(tmp_path)
    fresh = fresh_trusted_cold_dict(ctx)
    stale_digest = "0" * 64
    assert stale_digest != fresh["request_digest"]

    def _patch(trusted: dict[str, Any], live: dict[str, Any]) -> None:
        assert trusted["request_digest"] == live["request_digest"]
        trusted["request_digest"] = stale_digest

    mutate_coherent_trusted_cold(ctx, patch_trusted=_patch)
    with pytest.raises(
        CandidateCurrentnessError,
        match=r"stored trusted cold result does not match fresh cold",
    ):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


def test_managed_rejects_publication_result_unknown_top_level_field(tmp_path: Any) -> None:
    ctx = _fresh_ctx(tmp_path)
    mutate_coherent_publication_result_doc(
        ctx,
        lambda doc: doc.update({"extra_managed_result_field": True}),
    )
    with pytest.raises(CandidateCurrentnessError, match=r"unknown control fields"):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


def test_managed_rejects_completion_marker_unknown_top_level_field(tmp_path: Any) -> None:
    ctx = _fresh_ctx(tmp_path)
    mutate_coherent_completion_marker_doc(
        ctx,
        lambda doc: doc.update({"extra_managed_marker_field": 1}),
    )
    with pytest.raises(CandidateCurrentnessError, match=r"unknown control fields"):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


@pytest.mark.parametrize(
    ("bad_attempt", "match"),
    [
        (True, r"must be a JSON integer"),
        (1.0, r"must be a JSON integer"),
        ("1", r"must be a JSON integer"),
    ],
    ids=["bool", "float", "string"],
)
def test_managed_rejects_publication_result_evidence_attempt_number_type(
    tmp_path: Any,
    bad_attempt: object,
    match: str,
) -> None:
    ctx = _fresh_ctx(tmp_path)
    mutate_coherent_publication_result_doc(
        ctx,
        lambda doc: doc.update({"evidence_attempt_number": bad_attempt}),
    )
    with pytest.raises(CandidateCurrentnessError, match=match):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


@pytest.mark.parametrize(
    ("bad_attempt", "match"),
    [
        (True, r"must be a JSON integer"),
        (1.0, r"must be a JSON integer"),
        ("1", r"attempt binding mismatch"),
    ],
    ids=["bool", "float", "string"],
)
def test_managed_rejects_completion_marker_evidence_attempt_number_type(
    tmp_path: Any,
    bad_attempt: object,
    match: str,
) -> None:
    ctx = _fresh_ctx(tmp_path)
    mutate_coherent_completion_marker_doc(
        ctx,
        lambda doc: doc.update({"evidence_attempt_number": bad_attempt}),
    )
    with pytest.raises(CandidateCurrentnessError, match=match):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


def test_managed_rejects_marker_candidate_evidence_complete_false(tmp_path: Any) -> None:
    ctx = _fresh_ctx(tmp_path)
    mutate_coherent_completion_marker_doc(
        ctx,
        lambda doc: doc.update({"candidate_evidence_complete": False}),
    )
    with pytest.raises(
        CandidateCurrentnessError,
        match=r"does not attest evidence completion",
    ):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


def test_managed_rejects_marker_production_eligible_true(tmp_path: Any) -> None:
    ctx = _fresh_ctx(tmp_path)
    mutate_coherent_completion_marker_doc(
        ctx,
        lambda doc: doc.update({"production_eligible": True}),
    )
    with pytest.raises(
        CandidateCurrentnessError,
        match=r"production_eligible must be false",
    ):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


def test_managed_rejects_marker_promotion_eligible_true(tmp_path: Any) -> None:
    ctx = _fresh_ctx(tmp_path)
    mutate_coherent_completion_marker_doc(
        ctx,
        lambda doc: doc.update({"promotion_eligible": True}),
    )
    with pytest.raises(
        CandidateCurrentnessError,
        match=r"promotion_eligible must be false",
    ):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


def test_managed_rejects_coherent_manifest_foreign_bundle_id(tmp_path: Any) -> None:
    ctx = _fresh_ctx(tmp_path)
    foreign_id = f"candidate-live-FOREIGN-{ctx.workflow_id[-8:]}"
    assert foreign_id != f"candidate-live-{ctx.workflow_id}"
    mutate_coherent_manifest_bundle_id(ctx, foreign_id)
    with pytest.raises(
        CandidateCurrentnessError,
        match=r"bundle_id does not match workflow binding",
    ):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
