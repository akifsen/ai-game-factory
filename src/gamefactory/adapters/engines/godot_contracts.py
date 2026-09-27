"""Strict, versioned contracts for the bounded Godot verification harness.

The harness reports observations only. Assertions and expected values remain in
the Python-owned scenario and are evaluated here, outside the game process.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)

SCHEMA_VERSION = "0.2.0"
MAX_SCENARIO_BYTES = 256 * 1024
MAX_ACTIONS = 256
MAX_SNAPSHOTS = 256
MAX_ASSERTIONS = 256
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

ScenarioId = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
]
SafeId = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
]
ScenePath = Annotated[str, StringConstraints(min_length=7, max_length=512)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ScenarioAction(StrictModel):
    tick: Annotated[StrictInt, Field(ge=1, le=10_000)]
    action: Literal["apply_damage", "defeat_enemy"]
    amount: Annotated[StrictInt, Field(gt=0, le=1_000_000)] | None = None

    @model_serializer(mode="wrap")
    def omit_absent_amount(self, handler: Any) -> dict[str, Any]:
        data = cast(dict[str, Any], handler(self))
        if self.action == "defeat_enemy" and "amount" not in self.model_fields_set:
            data.pop("amount", None)
        return data

    @model_validator(mode="after")
    def validate_action_shape(self) -> ScenarioAction:
        if self.action == "apply_damage" and self.amount is None:
            raise ValueError("apply_damage requires a positive integer amount")
        if self.action == "defeat_enemy" and "amount" in self.model_fields_set:
            raise ValueError("defeat_enemy does not accept amount")
        return self


class ScenarioAssertion(StrictModel):
    id: SafeId
    tick: Annotated[StrictInt, Field(ge=0, le=10_000)]
    field: Literal["player_hp", "enemies_remaining", "score"]
    operator: Literal["equals", "minimum", "maximum"]
    expected: StrictInt


class Scenario(StrictModel):
    schema_version: Literal["0.2.0"]
    scenario_id: ScenarioId
    scene: ScenePath
    max_tick: Annotated[StrictInt, Field(ge=1, le=10_000)]
    timeout_seconds: Annotated[float, Field(gt=0, le=120, allow_inf_nan=False)]
    import_timeout_seconds: Annotated[float, Field(gt=0, le=120, allow_inf_nan=False)] = 30.0
    actions: tuple[ScenarioAction, ...] = ()
    snapshots: Annotated[
        tuple[Annotated[StrictInt, Field(ge=0, le=10_000)], ...],
        Field(min_length=2, max_length=MAX_SNAPSHOTS),
    ]
    assertions: Annotated[
        tuple[ScenarioAssertion, ...], Field(min_length=1, max_length=MAX_ASSERTIONS)
    ]

    @model_validator(mode="before")
    @classmethod
    def normalize_json_arrays(cls, value: object) -> object:
        if isinstance(value, dict):
            value = dict(value)
            for key in ("actions", "snapshots", "assertions"):
                if isinstance(value.get(key), list):
                    value[key] = tuple(value[key])
        return value

    @field_validator("scene")
    @classmethod
    def validate_scene_path(cls, value: str) -> str:
        if not value.startswith("res://"):
            raise ValueError("scene must be a project-relative res:// path")
        relative = value[6:]
        parts = relative.split("/")
        if (
            not relative
            or relative.startswith("/")
            or "\\" in relative
            or ":" in relative
            or any(unicodedata.category(character) == "Cc" for character in relative)
            or any(part in ("", ".", "..") for part in parts)
            or not relative.endswith(".tscn")
        ):
            raise ValueError("scene must be a normalized res:// path to a .tscn file")
        return value

    @field_validator("timeout_seconds", "import_timeout_seconds")
    @classmethod
    def validate_finite_timeout(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("timeout must be finite")
        return value

    @model_validator(mode="after")
    def validate_scenario_relations(self) -> Scenario:
        if len(self.actions) > MAX_ACTIONS:
            raise ValueError(f"at most {MAX_ACTIONS} actions are allowed")
        if len(self.snapshots) > MAX_SNAPSHOTS:
            raise ValueError(f"at most {MAX_SNAPSHOTS} snapshots are allowed")
        if len(self.assertions) > MAX_ASSERTIONS:
            raise ValueError(f"at most {MAX_ASSERTIONS} assertions are allowed")
        if not self.assertions:
            raise ValueError("at least one assertion is required")
        if not self.snapshots or self.snapshots[0] != 0:
            raise ValueError("snapshots must include tick 0")
        if self.snapshots[-1] != self.max_tick:
            raise ValueError("snapshots must include max_tick as the final sample")
        if tuple(sorted(set(self.snapshots))) != self.snapshots:
            raise ValueError("snapshots must be strictly increasing")
        if any(tick > self.max_tick for tick in self.snapshots):
            raise ValueError("snapshot tick exceeds max_tick")
        if any(action.tick > self.max_tick for action in self.actions):
            raise ValueError("action tick exceeds max_tick")
        if tuple(sorted(action.tick for action in self.actions)) != tuple(
            action.tick for action in self.actions
        ):
            raise ValueError("actions must be ordered by tick")
        ids = [assertion.id for assertion in self.assertions]
        if len(ids) != len(set(ids)):
            raise ValueError("assertion IDs must be unique")
        if any(assertion.tick not in self.snapshots for assertion in self.assertions):
            raise ValueError("each assertion tick must have a requested snapshot")
        return self


class ObservedState(StrictModel):
    player_hp: StrictInt
    enemies_remaining: Annotated[StrictInt, Field(ge=0)]
    score: Annotated[StrictInt, Field(ge=0)]


class ObservationSnapshot(StrictModel):
    tick: Annotated[StrictInt, Field(ge=0, le=10_000)]
    state: ObservedState


class Observation(StrictModel):
    schema_version: Literal["0.2.0"]
    execution_id: SafeId
    scenario_id: ScenarioId
    scenario_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    completed_tick: Annotated[StrictInt, Field(ge=0, le=10_000)]
    snapshots: Annotated[
        tuple[ObservationSnapshot, ...], Field(min_length=2, max_length=MAX_SNAPSHOTS)
    ]
    completion: Literal["COMPLETED"]
    error: None

    @model_validator(mode="before")
    @classmethod
    def normalize_snapshots(cls, value: object) -> object:
        if isinstance(value, dict) and isinstance(value.get("snapshots"), list):
            value = dict(value)
            value["snapshots"] = tuple(value["snapshots"])
        return value

    @model_validator(mode="after")
    def validate_ordered_snapshots(self) -> Observation:
        ticks = tuple(snapshot.tick for snapshot in self.snapshots)
        if tuple(sorted(set(ticks))) != ticks:
            raise ValueError("observation snapshots must be strictly increasing")
        return self


class AssertionFinding(StrictModel):
    assertion_id: SafeId
    tick: StrictInt
    field: Literal["player_hp", "enemies_remaining", "score"]
    expected: StrictInt
    actual: StrictInt
    status: Literal["PASS", "FAIL"]
    explanation: str


class AssertionReport(StrictModel):
    schema_version: Literal["0.2.0"] = "0.2.0"
    status: Literal["PASS", "FAIL"]
    findings: tuple[AssertionFinding, ...]


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def load_scenario(path: str | Path) -> Scenario:
    """Load a bounded UTF-8 JSON scenario and reject duplicate/unknown fields."""
    scenario_path = Path(path)
    if not scenario_path.is_file():
        raise ValueError("scenario path must refer to a regular file")
    with scenario_path.open("rb") as stream:
        raw = stream.read(MAX_SCENARIO_BYTES + 1)
    if len(raw) > MAX_SCENARIO_BYTES:
        raise ValueError(f"scenario exceeds {MAX_SCENARIO_BYTES} bytes")
    try:
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid scenario JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("scenario root must be a JSON object")
    try:
        return Scenario.model_validate(data)
    except Exception as exc:
        raise ValueError(f"invalid scenario: {exc}") from exc


def scenario_fingerprint(scenario: Scenario) -> str:
    """Return the stable SHA-256 of the complete validated scenario."""
    canonical = json.dumps(
        scenario.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_runtime_request(scenario: Scenario, execution_id: str) -> dict[str, object]:
    """Build harness input without carrying Python-owned assertion/oracle data."""
    if not _SAFE_ID_RE.fullmatch(execution_id):
        raise ValueError("execution_id must be a safe identifier")
    return {
        "schema_version": SCHEMA_VERSION,
        "execution_id": execution_id,
        "scenario_id": scenario.scenario_id,
        "scenario_sha256": scenario_fingerprint(scenario),
        "scene": scenario.scene,
        "max_tick": scenario.max_tick,
        "actions": [
            action.model_dump(mode="json", exclude_none=True) for action in scenario.actions
        ],
        "snapshots": list(scenario.snapshots),
    }


def validate_observation(
    data: dict[str, object], scenario: Scenario, execution_id: str
) -> Observation:
    """Validate the current attempt report and bind it to the exact scenario."""
    try:
        observation = Observation.model_validate(data)
    except Exception as exc:
        raise ValueError(f"invalid Godot observation: {exc}") from exc
    expected_hash = scenario_fingerprint(scenario)
    if observation.execution_id != execution_id:
        raise ValueError("observation execution_id does not match the current execution")
    if (
        observation.scenario_id != scenario.scenario_id
        or observation.scenario_sha256 != expected_hash
    ):
        raise ValueError("observation scenario identity or fingerprint does not match")
    if observation.completed_tick != scenario.max_tick:
        raise ValueError("observation completed_tick does not match max_tick")
    ticks = tuple(snapshot.tick for snapshot in observation.snapshots)
    if ticks != scenario.snapshots:
        raise ValueError("observation snapshots do not match the requested sample ticks")
    return observation


def evaluate_assertions(scenario: Scenario, observation: Observation) -> dict[str, object]:
    """Evaluate Python-owned assertions exclusively against observed state."""
    snapshots = {snapshot.tick: snapshot.state for snapshot in observation.snapshots}
    findings: list[AssertionFinding] = []
    for assertion in scenario.assertions:
        state = snapshots.get(assertion.tick)
        if state is None:
            raise ValueError(f"required observation at tick {assertion.tick} is missing")
        actual = getattr(state, assertion.field)
        passed = {
            "equals": actual == assertion.expected,
            "minimum": actual >= assertion.expected,
            "maximum": actual <= assertion.expected,
        }[assertion.operator]
        findings.append(
            AssertionFinding(
                assertion_id=assertion.id,
                tick=assertion.tick,
                field=assertion.field,
                expected=assertion.expected,
                actual=actual,
                status="PASS" if passed else "FAIL",
                explanation=(
                    f"Observed {assertion.field}={actual}; {assertion.operator} {assertion.expected} "
                    f"{'is satisfied' if passed else 'is not satisfied'}"
                ),
            )
        )
    report = AssertionReport(
        status="PASS" if all(item.status == "PASS" for item in findings) else "FAIL",
        findings=tuple(findings),
    )
    return report.model_dump(mode="json")
