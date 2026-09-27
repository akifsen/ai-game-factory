"""Version 0.3 capture scenario contract.

V0.2 headless scenarios stay on schema 0.2.0 and do not require screenshots.
Expected assertion values never enter the Godot request.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from gamefactory.adapters.engines.godot_contracts import (
    MAX_ACTIONS,
    MAX_ASSERTIONS,
    MAX_SCENARIO_BYTES,
    ScenarioAction,
    ScenarioAssertion,
    ScenePath,
    StrictModel,
    _reject_duplicate_keys,
)

CAPTURE_SCHEMA_VERSION = "0.3.0"
MAX_CAPTURES = 8
SUPPORTED_VIEWPORTS = ((1280, 720), (720, 1280))
RENDERER_PROFILE = "gl_compatibility"
REVIEW_CONTRACT = "0.3.0"


class ViewportSpec(StrictModel):
    width: Literal[1280, 720]
    height: Literal[720, 1280]

    @model_validator(mode="after")
    def supported_pair(self) -> ViewportSpec:
        if (self.width, self.height) not in SUPPORTED_VIEWPORTS:
            raise ValueError("viewport must be 1280x720 or 720x1280")
        return self


class CapturePoint(StrictModel):
    id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
    tick: Annotated[StrictInt, Field(ge=0, le=10_000)]


class CaptureScenario(StrictModel):
    schema_version: Literal["0.3.0"]
    scenario_id: Annotated[
        str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    ]
    scene: ScenePath
    max_tick: Annotated[StrictInt, Field(ge=1, le=10_000)]
    timeout_seconds: Annotated[float, Field(gt=0, le=120, allow_inf_nan=False)]
    import_timeout_seconds: Annotated[float, Field(gt=0, le=120, allow_inf_nan=False)] = 30.0
    viewport: ViewportSpec
    renderer_profile: Literal["gl_compatibility"]
    captures: Annotated[tuple[CapturePoint, ...], Field(min_length=1, max_length=MAX_CAPTURES)]
    actions: tuple[ScenarioAction, ...] = ()
    snapshots: Annotated[
        tuple[Annotated[StrictInt, Field(ge=0, le=10_000)], ...],
        Field(min_length=2, max_length=MAX_CAPTURES),
    ]
    assertions: Annotated[
        tuple[ScenarioAssertion, ...], Field(min_length=1, max_length=MAX_ASSERTIONS)
    ]

    @model_validator(mode="before")
    @classmethod
    def normalize_json_arrays(cls, value: object) -> object:
        if isinstance(value, dict):
            value = dict(value)
            for key in ("actions", "snapshots", "assertions", "captures"):
                if isinstance(value.get(key), list):
                    value[key] = tuple(value[key])
        return value

    @field_validator("scene")
    @classmethod
    def validate_scene_path(cls, value: str) -> str:
        from gamefactory.adapters.engines.godot_contracts import Scenario

        return Scenario.validate_scene_path(value)

    @model_validator(mode="after")
    def validate_relations(self) -> CaptureScenario:
        if len(self.actions) > MAX_ACTIONS:
            raise ValueError(f"at most {MAX_ACTIONS} actions are allowed")
        if not self.snapshots or self.snapshots[0] != 0 or self.snapshots[-1] != self.max_tick:
            raise ValueError("snapshots must start at tick 0 and end at max_tick")
        if tuple(sorted(set(self.snapshots))) != self.snapshots:
            raise ValueError("snapshots must be strictly increasing")
        if tuple(point.tick for point in self.captures) != self.snapshots:
            raise ValueError("captures must follow the snapshot ticks in order")
        ids = [point.id for point in self.captures]
        if len(ids) != len(set(ids)):
            raise ValueError("capture ids must be unique")
        assertion_ids = [item.id for item in self.assertions]
        if len(assertion_ids) != len(set(assertion_ids)):
            raise ValueError("assertion IDs must be unique")
        if any(item.tick not in self.snapshots for item in self.assertions):
            raise ValueError("each assertion tick must have a requested snapshot")
        if any(action.tick > self.max_tick for action in self.actions):
            raise ValueError("action tick exceeds max_tick")
        if tuple(sorted(action.tick for action in self.actions)) != tuple(
            action.tick for action in self.actions
        ):
            raise ValueError("actions must be ordered by tick")
        return self


def load_capture_scenario(path: str | Path) -> CaptureScenario:
    """Load a 0.3.0 capture scenario. A 0.2.0 headless scenario is rejected."""
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
        return CaptureScenario.model_validate(data)
    except Exception as exc:
        raise ValueError(f"invalid capture scenario: {exc}") from exc


def capture_fingerprint(scenario: CaptureScenario) -> str:
    canonical = json.dumps(
        scenario.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_capture_request(
    scenario: CaptureScenario, execution_id: str, capture_directory: str
) -> dict[str, object]:
    """Harness input without assertions or expected visual values."""
    from gamefactory.adapters.engines.godot_contracts import _SAFE_ID_RE

    if not _SAFE_ID_RE.fullmatch(execution_id):
        raise ValueError("execution_id must be a safe identifier")
    return {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "execution_id": execution_id,
        "scenario_id": scenario.scenario_id,
        "scenario_sha256": capture_fingerprint(scenario),
        "scene": scenario.scene,
        "max_tick": scenario.max_tick,
        "actions": [
            action.model_dump(mode="json", exclude_none=True) for action in scenario.actions
        ],
        "snapshots": list(scenario.snapshots),
        "captures": [point.model_dump(mode="json") for point in scenario.captures],
        "viewport_width": scenario.viewport.width,
        "viewport_height": scenario.viewport.height,
        "renderer_profile": scenario.renderer_profile,
        "capture_directory": capture_directory,
    }
