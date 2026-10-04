"""Pure evaluators for performance budgets, gameplay observations, and visual advice."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.game_quality import (
    GameplayObservation,
    GameplayScenario,
    PerformanceBudget,
    PerformanceEvidence,
    QualityFinding,
    QualityReport,
    VisualReview,
)


def _hash_record(record: Any) -> str:
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(
            record.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _percentile(values: tuple[float, ...], percentile: float) -> float:
    """Linear interpolation over sorted bounded samples (nearest rank is unstable at p95)."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def evaluate_performance(
    budget: PerformanceBudget | dict[str, Any],
    evidence: PerformanceEvidence | dict[str, Any],
) -> QualityReport:
    """Evaluate actual caller supplied metric samples against a platform budget.

    This compares declarations and provenance only. It cannot attest that the
    caller physically collected those values; evidence references and hashes
    must be independently verified by the consuming workflow.
    """
    budget = PerformanceBudget.from_dict(
        budget.to_dict() if isinstance(budget, PerformanceBudget) else budget
    )
    evidence = PerformanceEvidence.from_dict(
        evidence.to_dict() if isinstance(evidence, PerformanceEvidence) else evidence
    )
    if evidence.platform != budget.platform:
        raise ValidationError("performance evidence platform does not match budget")
    if evidence.budget_sha256 != budget.sha256:
        raise ValidationError("performance evidence budget_sha256 does not match budget")
    if evidence.collection_method not in budget.collection_methods:
        raise ValidationError("performance evidence collection_method is not allowed by budget")
    supplied = {sample.metric: sample for sample in evidence.samples}
    artifact_hash = _hash_record(evidence)
    findings: list[QualityFinding] = []
    for rule in budget.metrics:
        samples = supplied.get(rule.metric)
        if samples is None:
            findings.append(
                QualityFinding(
                    rule.metric,
                    "FAIL",
                    None,
                    rule.to_dict(),
                    artifact_hash,
                    evidence.evidence_refs,
                    "Required performance measurement is missing.",
                )
            )
            continue
        if samples.unit != rule.unit:
            # The contract normally catches this during parsing; retain fail-closed defense.
            raise ValidationError(f"unit mismatch for {rule.metric}")
        observed: dict[str, float | int] = {
            "sample_count": len(samples.samples),
            "min": min(samples.samples),
            "max": max(samples.samples),
            "mean": sum(samples.samples) / len(samples.samples),
            "p05": _percentile(samples.samples, 0.05),
            "p50": _percentile(samples.samples, 0.50),
            "p95": _percentile(samples.samples, 0.95),
        }
        violations: list[str] = []
        if observed["sample_count"] < rule.min_samples:
            violations.append(f"sample_count<{rule.min_samples}")
        if rule.max_value is not None and observed["max"] > rule.max_value:
            violations.append(f"max>{rule.max_value}")
        if rule.min_value is not None and observed["min"] < rule.min_value:
            violations.append(f"min<{rule.min_value}")
        if rule.max_p95 is not None and observed["p95"] > rule.max_p95:
            violations.append(f"p95>{rule.max_p95}")
        if rule.min_p05 is not None and observed["p05"] < rule.min_p05:
            violations.append(f"p05<{rule.min_p05}")
        findings.append(
            QualityFinding(
                rule.metric,
                "FAIL" if violations else "PASS",
                observed,
                rule.to_dict(),
                artifact_hash,
                evidence.evidence_refs,
                "; ".join(violations) if violations else "Measured distribution satisfies budget.",
            )
        )
    return QualityReport(f"performance:{budget.platform}", tuple(findings))


def _numeric_relation(operator: str, actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    if not isinstance(actual, (int, float)) or not isinstance(expected, (int, float)):
        return False
    return {
        "lt": actual < expected,
        "lte": actual <= expected,
        "gt": actual > expected,
        "gte": actual >= expected,
    }[operator]


_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda actual, expected: type(actual) is type(expected) and actual == expected,
    "ne": lambda actual, expected: type(actual) is not type(expected) or actual != expected,
    "lt": lambda actual, expected: _numeric_relation("lt", actual, expected),
    "lte": lambda actual, expected: _numeric_relation("lte", actual, expected),
    "gt": lambda actual, expected: _numeric_relation("gt", actual, expected),
    "gte": lambda actual, expected: _numeric_relation("gte", actual, expected),
    "contains": lambda actual, expected: (
        isinstance(actual, (str, list, dict)) and expected in actual
    ),
}


def evaluate_gameplay(
    scenario: GameplayScenario | dict[str, Any],
    observation: GameplayObservation | dict[str, Any],
) -> QualityReport:
    """Evaluate declared deterministic state assertions; no AI truth is inferred."""
    scenario = GameplayScenario.from_dict(
        scenario.to_dict() if isinstance(scenario, GameplayScenario) else scenario
    )
    observation = GameplayObservation.from_dict(
        observation.to_dict() if isinstance(observation, GameplayObservation) else observation
    )
    if scenario.scenario_sha256 != observation.scenario_sha256:
        raise ValidationError("gameplay observation scenario hash does not match scenario")
    unexpected = observation.state.keys() - set(scenario.fields)
    if unexpected:
        raise ValidationError(
            f"observation contains non-allowlisted field {sorted(unexpected)[0]!r}"
        )
    artifact_hash = _hash_record(observation)
    findings: list[QualityFinding] = []
    for rule in scenario.rules:
        present = rule.field in observation.state
        actual = observation.state.get(rule.field)
        if rule.operator == "exists":
            passed = present
        elif not present:
            passed = False
        else:
            try:
                passed = _OPERATORS[rule.operator](actual, rule.expected)
            except (TypeError, ValueError):
                passed = False
        findings.append(
            QualityFinding(
                rule.rule_id,
                "PASS" if passed else "FAIL",
                {"present": present, "value": actual},
                {"operator": rule.operator, "value": rule.expected},
                artifact_hash,
                observation.evidence_refs,
                "Scenario assertion satisfied."
                if passed
                else "Scenario assertion failed or required state was absent.",
            )
        )
    return QualityReport(f"gameplay:{scenario.scenario_id}", tuple(findings))


def record_visual_review(review: VisualReview | dict[str, Any]) -> QualityReport:
    """Convert visual-review opinions to an advisory-only report channel."""
    review = VisualReview.from_dict(
        review.to_dict() if isinstance(review, VisualReview) else review
    )
    artifact_hash = _hash_record(review)
    advisories = tuple(
        QualityFinding(
            f"visual:{finding.dimension}",
            "WARNING"
            if finding.severity == "concern"
            else "WARNING"
            if finding.severity == "warning"
            else "PASS",
            {
                "severity": finding.severity,
                "confidence": finding.confidence,
                "summary": finding.summary,
            },
            {"dimension": finding.dimension, "advisory": True},
            artifact_hash,
            finding.evidence_refs,
            finding.summary,
            "advisory",
        )
        for finding in review.findings
    )
    return QualityReport("visual-review", (), advisories)
