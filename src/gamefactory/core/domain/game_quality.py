"""Strict data contracts for deterministic gameplay, visual, and performance QA."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from gamefactory.core.domain.errors import ValidationError

PERFORMANCE_BUDGET_VERSION = "performance-budget-1.0.0"
PERFORMANCE_EVIDENCE_VERSION = "performance-evidence-1.0.0"
GAMEPLAY_SCENARIO_VERSION = "gameplay-scenario-1.0.0"
GAMEPLAY_OBSERVATION_VERSION = "gameplay-observation-1.0.0"
VISUAL_REVIEW_VERSION = "visual-review-1.0.0"
QUALITY_REPORT_VERSION = "game-quality-report-1.0.0"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

METRIC_UNITS: dict[str, str] = {
    "fps": "frames_per_second",
    "frame_time_ms": "milliseconds",
    "cpu_time_ms": "milliseconds",
    "gpu_time_ms": "milliseconds",
    "memory_mb": "mebibytes",
    "draw_calls": "count",
    "entity_count": "count",
    "particle_count": "count",
    "texture_size_px": "pixels",
}
_MAX_SAMPLES = 100_000
_MAX_RULES = 256
_MAX_EVIDENCE_REFS = 128
_MAX_JSON_BYTES = 2_000_000


def _exact(data: Any, required: set[str], optional: set[str], label: str) -> dict[str, Any]:
    if not isinstance(data, dict) or any(not isinstance(k, str) for k in data):
        raise ValidationError(f"{label} must be an object with string keys")
    missing, extra = required - data.keys(), data.keys() - required - optional
    if missing:
        raise ValidationError(f"{label} is missing {sorted(missing)[0]}")
    if extra:
        raise ValidationError(f"{label} has unknown fields: {sorted(extra)[0]}")
    return data


def _text(value: Any, field: str, *, max_length: int = 512) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > max_length:
        raise ValidationError(
            f"{field} must be a non-empty trimmed string of at most {max_length} chars"
        )
    return value


def _sha(value: Any, field: str) -> str:
    result = _text(value, field, max_length=64)
    if not SHA256_RE.fullmatch(result):
        raise ValidationError(f"{field} must be a lowercase SHA-256 hex digest")
    return result


def _number(value: Any, field: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValidationError(f"{field} must be a finite number") from exc
    if not math.isfinite(result) or result < minimum:
        raise ValidationError(f"{field} must be finite and >= {minimum}")
    return result


def _utc_datetime(value: Any, field: str) -> datetime:
    raw = _text(value, field, max_length=64)
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError as exc:
        raise ValidationError(f"{field} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{field} must include a timezone offset")
    normalized = parsed.astimezone(UTC)
    return normalized


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValidationError(f"{field} must be an integer >= {minimum}")
    return value


def _string_list(value: Any, field: str, *, maximum: int = _MAX_EVIDENCE_REFS) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValidationError(f"{field} must be an array of at most {maximum} strings")
    items = tuple(_text(item, field) for item in value)
    if len(set(items)) != len(items):
        raise ValidationError(f"{field} must not contain duplicates")
    return items


def _safe_json(value: Any, field: str) -> Any:
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must contain finite JSON values") from exc
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValidationError(f"{field} exceeds the JSON size limit")
    return json.loads(encoded)


@dataclass(frozen=True)
class MetricBudget:
    metric: str
    unit: str
    max_value: float | None = None
    min_value: float | None = None
    max_p95: float | None = None
    min_p05: float | None = None
    min_samples: int = 1

    @classmethod
    def from_dict(cls, data: Any) -> MetricBudget:
        d = _exact(
            data,
            {"metric", "unit"},
            {"max_value", "min_value", "max_p95", "min_p05", "min_samples"},
            "metric budget",
        )
        metric = _text(d["metric"], "metric", max_length=64)
        if metric not in METRIC_UNITS:
            raise ValidationError(f"unknown metric {metric!r}")
        unit = _text(d["unit"], "unit", max_length=64)
        if unit != METRIC_UNITS[metric]:
            raise ValidationError(f"{metric} requires unit {METRIC_UNITS[metric]!r}")
        bounds = {
            key: (_number(d[key], key) if key in d else None)
            for key in ("max_value", "min_value", "max_p95", "min_p05")
        }
        if not any(value is not None for value in bounds.values()):
            raise ValidationError("metric budget requires at least one bound")
        if (
            bounds["min_value"] is not None
            and bounds["max_value"] is not None
            and bounds["min_value"] > bounds["max_value"]
        ):
            raise ValidationError("min_value exceeds max_value")
        samples = _integer(d.get("min_samples", 1), "min_samples", minimum=1)
        return cls(metric, unit, **bounds, min_samples=samples)

    def to_dict(self) -> dict[str, Any]:
        return {
            k: v
            for k, v in {
                "metric": self.metric,
                "unit": self.unit,
                "max_value": self.max_value,
                "min_value": self.min_value,
                "max_p95": self.max_p95,
                "min_p05": self.min_p05,
                "min_samples": self.min_samples,
            }.items()
            if v is not None
        }


@dataclass(frozen=True)
class PerformanceBudget:
    platform: str
    collection_methods: tuple[str, ...]
    metrics: tuple[MetricBudget, ...]
    schema_version: str = PERFORMANCE_BUDGET_VERSION

    @classmethod
    def from_dict(cls, data: Any) -> PerformanceBudget:
        d = _exact(
            data,
            {"schema_version", "platform", "collection_methods", "metrics"},
            set(),
            "performance budget",
        )
        if d["schema_version"] != PERFORMANCE_BUDGET_VERSION:
            raise ValidationError("unsupported performance budget schema_version")
        platform = _text(d["platform"], "platform", max_length=128)
        methods = _string_list(d["collection_methods"], "collection_methods", maximum=32)
        if not methods:
            raise ValidationError("collection_methods must be non-empty")
        if (
            not isinstance(d["metrics"], list)
            or not d["metrics"]
            or len(d["metrics"]) > len(METRIC_UNITS)
        ):
            raise ValidationError("metrics must be a non-empty bounded array")
        metrics = tuple(MetricBudget.from_dict(item) for item in d["metrics"])
        names = [m.metric for m in metrics]
        if len(names) != len(set(names)):
            raise ValidationError("metrics must not contain duplicate metric names")
        return cls(platform, methods, metrics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "platform": self.platform,
            "collection_methods": list(self.collection_methods),
            "metrics": [m.to_dict() for m in self.metrics],
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class MetricSamples:
    metric: str
    unit: str
    samples: tuple[float, ...]

    @classmethod
    def from_dict(cls, data: Any) -> MetricSamples:
        d = _exact(data, {"metric", "unit", "samples"}, set(), "metric samples")
        metric = _text(d["metric"], "metric", max_length=64)
        if metric not in METRIC_UNITS or d["unit"] != METRIC_UNITS[metric]:
            raise ValidationError(f"invalid metric unit for {metric}")
        if (
            not isinstance(d["samples"], list)
            or not d["samples"]
            or len(d["samples"]) > _MAX_SAMPLES
        ):
            raise ValidationError(f"{metric} samples must contain 1..{_MAX_SAMPLES} values")
        samples = tuple(_number(v, f"{metric} sample") for v in d["samples"])
        return cls(metric, d["unit"], samples)

    def to_dict(self) -> dict[str, Any]:
        return {"metric": self.metric, "unit": self.unit, "samples": list(self.samples)}


@dataclass(frozen=True)
class PerformanceEvidence:
    platform: str
    budget_sha256: str
    engine: str
    collection_method: str
    started_at: str
    ended_at: str
    sample_interval_ms: float
    scenario_sha256: str
    samples: tuple[MetricSamples, ...]
    evidence_refs: tuple[str, ...]
    schema_version: str = PERFORMANCE_EVIDENCE_VERSION

    @classmethod
    def from_dict(cls, data: Any) -> PerformanceEvidence:
        req = {
            "schema_version",
            "platform",
            "budget_sha256",
            "engine",
            "collection_method",
            "started_at",
            "ended_at",
            "sample_interval_ms",
            "scenario_sha256",
            "samples",
            "evidence_refs",
        }
        d = _exact(data, req, set(), "performance evidence")
        if d["schema_version"] != PERFORMANCE_EVIDENCE_VERSION:
            raise ValidationError("unsupported performance evidence schema_version")
        if (
            not isinstance(d["samples"], list)
            or not d["samples"]
            or len(d["samples"]) > len(METRIC_UNITS)
        ):
            raise ValidationError("samples must be a non-empty bounded array")
        metrics = tuple(MetricSamples.from_dict(v) for v in d["samples"])
        names = [m.metric for m in metrics]
        if len(names) != len(set(names)):
            raise ValidationError("samples must not contain duplicate metrics")
        start, end = (
            _utc_datetime(d["started_at"], "started_at"),
            _utc_datetime(d["ended_at"], "ended_at"),
        )
        if end <= start:
            raise ValidationError("ended_at must be later than started_at")
        interval = _number(d["sample_interval_ms"], "sample_interval_ms", minimum=0.001)
        return cls(
            _text(d["platform"], "platform", max_length=128),
            _sha(d["budget_sha256"], "budget_sha256"),
            _text(d["engine"], "engine", max_length=128),
            _text(d["collection_method"], "collection_method", max_length=128),
            start.isoformat().replace("+00:00", "Z"),
            end.isoformat().replace("+00:00", "Z"),
            interval,
            _sha(d["scenario_sha256"], "scenario_sha256"),
            metrics,
            _string_list(d["evidence_refs"], "evidence_refs"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "platform": self.platform,
            "budget_sha256": self.budget_sha256,
            "engine": self.engine,
            "collection_method": self.collection_method,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "sample_interval_ms": self.sample_interval_ms,
            "scenario_sha256": self.scenario_sha256,
            "samples": [s.to_dict() for s in self.samples],
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class GameplayRule:
    rule_id: str
    field: str
    operator: Literal["eq", "ne", "lt", "lte", "gt", "gte", "contains", "exists"]
    expected: Any = None

    @classmethod
    def from_dict(cls, data: Any) -> GameplayRule:
        d = _exact(data, {"rule_id", "field", "operator"}, {"expected"}, "gameplay rule")
        op = d["operator"]
        if op not in {"eq", "ne", "lt", "lte", "gt", "gte", "contains", "exists"}:
            raise ValidationError("unsupported gameplay operator")
        if op != "exists" and "expected" not in d:
            raise ValidationError("expected is required for this operator")
        if op == "exists" and "expected" in d:
            raise ValidationError("expected is forbidden for exists operator")
        expected = _safe_json(d["expected"], "expected") if "expected" in d else None
        return cls(
            _text(d["rule_id"], "rule_id", max_length=128),
            _text(d["field"], "field", max_length=256),
            op,
            expected,
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "rule_id": self.rule_id,
            "field": self.field,
            "operator": self.operator,
        }
        if self.operator != "exists":
            d["expected"] = self.expected
        return d


@dataclass(frozen=True)
class GameplayScenario:
    scenario_id: str
    scenario_sha256: str
    domains: tuple[str, ...]
    fields: tuple[str, ...]
    rules: tuple[GameplayRule, ...]
    schema_version: str = GAMEPLAY_SCENARIO_VERSION

    @classmethod
    def from_dict(cls, data: Any) -> GameplayScenario:
        req = {"schema_version", "scenario_id", "scenario_sha256", "domains", "fields", "rules"}
        d = _exact(data, req, set(), "gameplay scenario")
        if d["schema_version"] != GAMEPLAY_SCENARIO_VERSION:
            raise ValidationError("unsupported gameplay scenario schema_version")
        domains = _string_list(d["domains"], "domains", maximum=5)
        allowed_domains = {"progression", "spawn", "economy", "reachability", "win_loss"}
        if not domains or set(domains) - allowed_domains:
            raise ValidationError(
                "domains must select from progression, spawn, economy, reachability, win_loss"
            )
        fields = _string_list(d["fields"], "fields", maximum=256)
        if not fields:
            raise ValidationError("fields must be non-empty")
        if not isinstance(d["rules"], list) or not d["rules"] or len(d["rules"]) > _MAX_RULES:
            raise ValidationError("rules must be a non-empty bounded array")
        rules = tuple(GameplayRule.from_dict(r) for r in d["rules"])
        if len({r.rule_id for r in rules}) != len(rules) or any(
            r.field not in fields for r in rules
        ):
            raise ValidationError(
                "rule ids must be unique and every rule field must be allowlisted"
            )
        scenario_id = _text(d["scenario_id"], "scenario_id", max_length=128)
        scenario_hash = _sha(d["scenario_sha256"], "scenario_sha256")
        canonical = {
            "schema_version": GAMEPLAY_SCENARIO_VERSION,
            "scenario_id": scenario_id,
            "domains": list(domains),
            "fields": list(fields),
            "rules": [rule.to_dict() for rule in rules],
        }
        actual_hash = hashlib.sha256(
            json.dumps(
                canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if scenario_hash != actual_hash:
            raise ValidationError("scenario_sha256 does not match canonical scenario content")
        return cls(scenario_id, scenario_hash, domains, fields, rules)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scenario_id": self.scenario_id,
            "scenario_sha256": self.scenario_sha256,
            "domains": list(self.domains),
            "fields": list(self.fields),
            "rules": [r.to_dict() for r in self.rules],
        }


@dataclass(frozen=True)
class GameplayObservation:
    scenario_sha256: str
    engine: str
    observed_at: str
    state: dict[str, Any]
    evidence_refs: tuple[str, ...]
    schema_version: str = GAMEPLAY_OBSERVATION_VERSION

    @classmethod
    def from_dict(cls, data: Any) -> GameplayObservation:
        req = {
            "schema_version",
            "scenario_sha256",
            "engine",
            "observed_at",
            "state",
            "evidence_refs",
        }
        d = _exact(data, req, set(), "gameplay observation")
        if d["schema_version"] != GAMEPLAY_OBSERVATION_VERSION:
            raise ValidationError("unsupported gameplay observation schema_version")
        state = _safe_json(d["state"], "state")
        if not isinstance(state, dict) or len(state) > 256:
            raise ValidationError("state must be a bounded object")
        observed = _utc_datetime(d["observed_at"], "observed_at").isoformat().replace("+00:00", "Z")
        return cls(
            _sha(d["scenario_sha256"], "scenario_sha256"),
            _text(d["engine"], "engine", max_length=128),
            observed,
            state,
            _string_list(d["evidence_refs"], "evidence_refs"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scenario_sha256": self.scenario_sha256,
            "engine": self.engine,
            "observed_at": self.observed_at,
            "state": self.state,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class VisualFinding:
    dimension: str
    severity: Literal["info", "warning", "concern"]
    confidence: float
    summary: str
    evidence_refs: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: Any) -> VisualFinding:
        d = _exact(
            data,
            {"dimension", "severity", "confidence", "summary", "evidence_refs"},
            set(),
            "visual finding",
        )
        if d["severity"] not in {"info", "warning", "concern"}:
            raise ValidationError("invalid visual finding severity")
        confidence = _number(d["confidence"], "confidence")
        if confidence > 1:
            raise ValidationError("confidence must be in [0, 1]")
        return cls(
            _text(d["dimension"], "dimension", max_length=128),
            d["severity"],
            confidence,
            _text(d["summary"], "summary", max_length=2048),
            _string_list(d["evidence_refs"], "evidence_refs"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "severity": self.severity,
            "confidence": self.confidence,
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class VisualReview:
    screenshot_sha256: str
    reference_sha256: tuple[str, ...]
    art_bible_sha256: str
    reviewer: str
    findings: tuple[VisualFinding, ...]
    schema_version: str = VISUAL_REVIEW_VERSION

    @classmethod
    def from_dict(cls, data: Any) -> VisualReview:
        req = {
            "schema_version",
            "screenshot_sha256",
            "reference_sha256",
            "art_bible_sha256",
            "reviewer",
            "findings",
        }
        d = _exact(data, req, set(), "visual review")
        if d["schema_version"] != VISUAL_REVIEW_VERSION:
            raise ValidationError("unsupported visual review schema_version")
        refs = _string_list(d["reference_sha256"], "reference_sha256", maximum=64)
        refs = tuple(_sha(v, "reference hash") for v in refs)
        if not isinstance(d["findings"], list) or len(d["findings"]) > _MAX_RULES:
            raise ValidationError("findings must be a bounded array")
        return cls(
            _sha(d["screenshot_sha256"], "screenshot_sha256"),
            refs,
            _sha(d["art_bible_sha256"], "art_bible_sha256"),
            _text(d["reviewer"], "reviewer", max_length=256),
            tuple(VisualFinding.from_dict(f) for f in d["findings"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "screenshot_sha256": self.screenshot_sha256,
            "reference_sha256": list(self.reference_sha256),
            "art_bible_sha256": self.art_bible_sha256,
            "reviewer": self.reviewer,
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass(frozen=True)
class QualityFinding:
    rule_id: str
    status: Literal["PASS", "WARNING", "FAIL"]
    observed: Any
    expected: Any
    artifact_sha256: str
    evidence_refs: tuple[str, ...]
    message: str
    channel: Literal["deterministic", "advisory"] = "deterministic"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "status": self.status,
            "observed": self.observed,
            "expected": self.expected,
            "artifact_sha256": self.artifact_sha256,
            "evidence_refs": list(self.evidence_refs),
            "message": self.message,
            "channel": self.channel,
        }


@dataclass(frozen=True)
class QualityReport:
    subject: str
    findings: tuple[QualityFinding, ...]
    visual_advisories: tuple[QualityFinding, ...] = ()
    schema_version: str = QUALITY_REPORT_VERSION

    @property
    def status(self) -> Literal["PASS", "WARNING", "FAIL"]:
        statuses = [finding.status for finding in self.findings]
        return "FAIL" if "FAIL" in statuses else "WARNING" if "WARNING" in statuses else "PASS"

    @property
    def approval_eligible(self) -> bool:
        return (
            self.subject != "visual-review" and self.status == "PASS" and not self.visual_advisories
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "subject": self.subject,
            "status": self.status,
            "approval_eligible": self.approval_eligible,
            "findings": [f.to_dict() for f in self.findings],
            "visual_advisories": [f.to_dict() for f in self.visual_advisories],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_dict(cls, data: Any) -> QualityReport:
        d = _exact(
            data,
            {
                "schema_version",
                "subject",
                "status",
                "approval_eligible",
                "findings",
                "visual_advisories",
            },
            set(),
            "quality report",
        )
        if (
            d["schema_version"] != QUALITY_REPORT_VERSION
            or d["status"] not in {"PASS", "WARNING", "FAIL"}
            or type(d["approval_eligible"]) is not bool
        ):
            raise ValidationError("invalid quality report header")

        def findings(
            items: Any, channel: Literal["deterministic", "advisory"]
        ) -> tuple[QualityFinding, ...]:
            if not isinstance(items, list) or len(items) > _MAX_RULES * 2:
                raise ValidationError("report findings must be a bounded array")
            result = []
            for item in items:
                f = _exact(
                    item,
                    {
                        "rule_id",
                        "status",
                        "observed",
                        "expected",
                        "artifact_sha256",
                        "evidence_refs",
                        "message",
                        "channel",
                    },
                    set(),
                    "quality finding",
                )
                if f["status"] not in {"PASS", "WARNING", "FAIL"} or f["channel"] != channel:
                    raise ValidationError("invalid finding status or channel")
                result.append(
                    QualityFinding(
                        _text(f["rule_id"], "rule_id"),
                        f["status"],
                        _safe_json(f["observed"], "observed"),
                        _safe_json(f["expected"], "expected"),
                        _sha(f["artifact_sha256"], "artifact_sha256"),
                        _string_list(f["evidence_refs"], "evidence_refs"),
                        _text(f["message"], "message", max_length=2048),
                        channel,
                    )
                )
            return tuple(result)

        report = cls(
            _text(d["subject"], "subject"),
            findings(d["findings"], "deterministic"),
            findings(d["visual_advisories"], "advisory"),
        )
        if report.status != d["status"] or report.approval_eligible != d["approval_eligible"]:
            raise ValidationError("report summary fields do not match findings")
        return report
