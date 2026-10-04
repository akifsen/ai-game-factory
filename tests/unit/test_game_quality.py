"""Contract and evaluator coverage for master requirements 18–20 (not executed here)."""

from __future__ import annotations

import hashlib
import json

import pytest

from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.game_quality import (
    GAMEPLAY_OBSERVATION_VERSION,
    GAMEPLAY_SCENARIO_VERSION,
    PERFORMANCE_BUDGET_VERSION,
    PERFORMANCE_EVIDENCE_VERSION,
    VISUAL_REVIEW_VERSION,
    GameplayObservation,
    PerformanceBudget,
    PerformanceEvidence,
    QualityReport,
    VisualReview,
)
from gamefactory.validators.game_quality import (
    evaluate_gameplay,
    evaluate_performance,
    record_visual_review,
)

H = "a" * 64
REF = "evidence://capture-1"


def _budget() -> PerformanceBudget:
    return PerformanceBudget.from_dict(
        {
            "schema_version": PERFORMANCE_BUDGET_VERSION,
            "platform": "android_mid",
            "collection_methods": ["engine_frame_sampler_v1"],
            "metrics": [
                {"metric": "fps", "unit": "frames_per_second", "min_value": 50, "min_samples": 4},
                {
                    "metric": "frame_time_ms",
                    "unit": "milliseconds",
                    "max_p95": 20,
                    "min_samples": 4,
                },
                {"metric": "memory_mb", "unit": "mebibytes", "max_value": 750},
                {"metric": "draw_calls", "unit": "count", "max_value": 150},
                {"metric": "entity_count", "unit": "count", "max_value": 1000},
                {"metric": "particle_count", "unit": "count", "max_value": 500},
                {"metric": "texture_size_px", "unit": "pixels", "max_value": 2048},
                {"metric": "cpu_time_ms", "unit": "milliseconds", "max_p95": 16},
                {"metric": "gpu_time_ms", "unit": "milliseconds", "max_p95": 16},
            ],
        }
    )


def _evidence(
    budget: PerformanceBudget, *, fps: list[int] | None = None, samples: list[dict] | None = None
) -> dict:
    return {
        "schema_version": PERFORMANCE_EVIDENCE_VERSION,
        "platform": budget.platform,
        "budget_sha256": budget.sha256,
        "engine": "godot-4.7",
        "collection_method": "engine_frame_sampler_v1",
        "started_at": "2026-10-04T12:00:00Z",
        "ended_at": "2026-10-04T12:00:10Z",
        "sample_interval_ms": 100,
        "scenario_sha256": H,
        "samples": samples
        if samples is not None
        else [
            {"metric": "fps", "unit": "frames_per_second", "samples": fps or [60, 59, 58, 57]},
            {"metric": "frame_time_ms", "unit": "milliseconds", "samples": [15, 16, 17, 18]},
            {"metric": "memory_mb", "unit": "mebibytes", "samples": [680]},
            {"metric": "draw_calls", "unit": "count", "samples": [120]},
            {"metric": "entity_count", "unit": "count", "samples": [80]},
            {"metric": "particle_count", "unit": "count", "samples": [45]},
            {"metric": "texture_size_px", "unit": "pixels", "samples": [1024]},
            {"metric": "cpu_time_ms", "unit": "milliseconds", "samples": [8, 9, 10, 12]},
            {"metric": "gpu_time_ms", "unit": "milliseconds", "samples": [7, 8, 10, 11]},
        ],
        "evidence_refs": [REF],
    }


def test_performance_missing_required_metrics_fail_closed() -> None:
    budget = _budget()
    evidence = _evidence(budget, samples=[])
    # Empty source samples are invalid syntax; truly absent named measurements remain valid.
    evidence["samples"] = [
        {"metric": "fps", "unit": "frames_per_second", "samples": [60, 60, 60, 60]}
    ]
    report = evaluate_performance(budget, evidence)
    assert report.status == "FAIL"
    missing = [finding for finding in report.findings if finding.rule_id != "fps"]
    assert all(f.status == "FAIL" and f.observed is None for f in missing)


def test_performance_p95_checks_distribution_and_serializes() -> None:
    budget = _budget()
    records = _evidence(budget)
    records["samples"][1]["samples"] = [10, 10, 10, 100]
    report = evaluate_performance(budget, records)
    frame = next(f for f in report.findings if f.rule_id == "frame_time_ms")
    assert frame.status == "FAIL"
    assert frame.observed["p95"] > 20
    assert QualityReport.from_dict(json.loads(report.to_json())).to_dict() == report.to_dict()


@pytest.mark.parametrize(
    "change",
    [
        {"platform": "ios"},
        {"budget_sha256": "b" * 64},
        {"samples": [{"metric": "fps", "unit": "milliseconds", "samples": [60]}]},
    ],
)
def test_performance_evidence_rejects_identity_and_unit_mismatch(change: dict) -> None:
    budget = _budget()
    evidence = _evidence(budget)
    evidence.update(change)
    with pytest.raises(ValidationError):
        evaluate_performance(budget, evidence)


def test_performance_contract_rejects_nonfinite_unbounded_and_wrong_units() -> None:
    budget = _budget()
    raw = _evidence(budget)
    raw["samples"][0]["samples"] = [float("nan")]
    with pytest.raises(ValidationError):
        PerformanceEvidence.from_dict(raw)
    raw = _evidence(budget)
    raw["samples"][0]["samples"] = list(range(100_001))
    with pytest.raises(ValidationError):
        PerformanceEvidence.from_dict(raw)
    with pytest.raises(ValidationError):
        PerformanceBudget.from_dict(
            {
                "schema_version": PERFORMANCE_BUDGET_VERSION,
                "platform": "android_mid",
                "collection_methods": ["engine_frame_sampler_v1"],
                "metrics": [{"metric": "memory_mb", "unit": "megabytes", "max_value": 750}],
            }
        )


def test_performance_rejects_wrong_collection_method_and_invalid_time() -> None:
    budget = _budget()
    evidence = _evidence(budget)
    evidence["collection_method"] = "self_reported_guess"
    with pytest.raises(ValidationError, match="collection_method"):
        evaluate_performance(budget, evidence)
    evidence = _evidence(budget)
    evidence["started_at"] = "yesterday"
    with pytest.raises(ValidationError, match="ISO-8601"):
        PerformanceEvidence.from_dict(evidence)
    evidence = _evidence(budget)
    evidence["started_at"] = "2026-10-04T12:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        PerformanceEvidence.from_dict(evidence)


def _scenario() -> dict:
    base = {
        "schema_version": GAMEPLAY_SCENARIO_VERSION,
        "scenario_id": "economy_progression_01",
        "domains": ["progression", "spawn", "economy", "reachability", "win_loss"],
        "fields": ["level", "spawn_count", "coins", "goal_reachable", "result"],
        "rules": [
            {"rule_id": "level_progresses", "field": "level", "operator": "gte", "expected": 2},
            {"rule_id": "spawn_bounded", "field": "spawn_count", "operator": "lte", "expected": 20},
            {"rule_id": "economy_nonnegative", "field": "coins", "operator": "gte", "expected": 0},
            {
                "rule_id": "goal_reachable",
                "field": "goal_reachable",
                "operator": "eq",
                "expected": True,
            },
            {
                "rule_id": "terminal_result",
                "field": "result",
                "operator": "contains",
                "expected": "win",
            },
        ],
    }
    raw = json.dumps(base, sort_keys=True, separators=(",", ":")).encode()
    base["scenario_sha256"] = hashlib.sha256(raw).hexdigest()
    return base


def test_gameplay_asserts_real_generic_state_values_without_ai_claims() -> None:
    scenario = _scenario()
    observation = GameplayObservation.from_dict(
        {
            "schema_version": GAMEPLAY_OBSERVATION_VERSION,
            "scenario_sha256": scenario["scenario_sha256"],
            "engine": "godot-4.7",
            "observed_at": "2026-10-04T12:01:00Z",
            "state": {
                "level": 2,
                "spawn_count": 20,
                "coins": 5,
                "goal_reachable": True,
                "result": "win",
            },
            "evidence_refs": [REF],
        }
    )
    report = evaluate_gameplay(scenario, observation)
    assert report.status == "PASS"
    assert all(f.observed["present"] for f in report.findings)


def test_gameplay_rejects_scenario_hash_and_unknown_state_keys() -> None:
    scenario = _scenario()
    observation = {
        "schema_version": GAMEPLAY_OBSERVATION_VERSION,
        "scenario_sha256": "b" * 64,
        "engine": "godot-4.7",
        "observed_at": "2026-10-04T12:01:00Z",
        "state": {"level": 2},
        "evidence_refs": [REF],
    }
    with pytest.raises(ValidationError, match="scenario hash"):
        evaluate_gameplay(scenario, observation)
    observation["scenario_sha256"] = scenario["scenario_sha256"]
    observation["state"] = {"level": 2, "untrusted_extra": 99}
    with pytest.raises(ValidationError, match="non-allowlisted"):
        evaluate_gameplay(scenario, observation)


def test_visual_findings_are_advisory_and_never_auto_approve() -> None:
    review = VisualReview.from_dict(
        {
            "schema_version": VISUAL_REVIEW_VERSION,
            "screenshot_sha256": H,
            "reference_sha256": ["b" * 64],
            "art_bible_sha256": "c" * 64,
            "reviewer": "vision-reviewer-v1",
            "findings": [
                {
                    "dimension": "hud_readability",
                    "severity": "info",
                    "confidence": 0.8,
                    "summary": "HUD text is legible.",
                    "evidence_refs": [REF],
                }
            ],
        }
    )
    report = record_visual_review(review)
    assert report.status == "PASS"
    assert not report.approval_eligible
    assert report.visual_advisories[0].channel == "advisory"
    assert QualityReport.from_dict(json.loads(report.to_json())).to_dict() == report.to_dict()
