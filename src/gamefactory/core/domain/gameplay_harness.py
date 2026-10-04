"""Strict, finite contracts for the optional Godot development harness."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Set
from dataclasses import dataclass
from typing import Any

from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.game_quality import METRIC_UNITS, GameplayScenario, PerformanceBudget

GAMEPLAY_HARNESS_REQUEST_VERSION = "gameplay-harness-request-1.0.0"
GAMEPLAY_HARNESS_OUTPUT_VERSION = "gameplay-harness-output-1.0.0"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_MAX_ACTIONS = 10_000
_MAX_MAP = 256
_MAX_TICKS = 1_000_000
_ALLOWED_ACTIONS = {
    "start_game",
    "load_scene",
    "start_level",
    "spawn_entity",
    "simulate_input",
    "wait",
    "capture_screenshot",
    "dump_state",
    "collect_metrics",
    "quit",
}


def _object(
    value: Any, label: str, required: set[str], optional: Set[str] = frozenset()
) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValidationError(f"{label} must be an object")
    missing, extra = required - value.keys(), value.keys() - required - optional
    if missing or extra:
        raise ValidationError(f"{label} has missing or unknown fields")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValidationError(f"{label} must be a safe identifier")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValidationError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _safe_project_resource(value: Any, label: str, suffix: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("res://")
        or "\\" in value
        or ".." in value.split("/")
    ):
        raise ValidationError(f"{label} must be an allowlisted project resource path")
    if not value.endswith(suffix) or len(value) > 512:
        raise ValidationError(f"{label} has an invalid resource type")
    return value


def _mapping(value: Any, label: str, suffix: str | None = None) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict) or not value or len(value) > _MAX_MAP:
        raise ValidationError(f"{label} must be a non-empty bounded allowlist")
    rows: list[tuple[str, str]] = []
    for key, target in value.items():
        _identifier(key, f"{label} key")
        if suffix is not None:
            target = _safe_project_resource(target, label, suffix)
        elif label == "input_allowlist":
            if not isinstance(target, str) or not _SAFE_ID.fullmatch(target):
                raise ValidationError("input actions must be safe identifiers")
        elif label == "parent_allowlist":
            if (
                not isinstance(target, str)
                or not target.startswith("/")
                or len(target) > 512
                or ".." in target.split("/")
            ):
                raise ValidationError("parent paths must be explicit absolute scene-tree paths")
        rows.append((key, target))
    return tuple(sorted(rows))


@dataclass(frozen=True)
class PropertyBinding:
    field: str
    node_path: str
    property_name: str

    @classmethod
    def from_dict(cls, value: Any) -> PropertyBinding:
        data = _object(value, "property binding", {"field", "node_path", "property"})
        field, path, prop = data["field"], data["node_path"], data["property"]
        if not isinstance(field, str) or not field or len(field) > 256:
            raise ValidationError("property binding field is invalid")
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or len(path) > 512
            or ".." in path.split("/")
        ):
            raise ValidationError("property binding node_path must be explicit and absolute")
        if not isinstance(prop, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", prop):
            raise ValidationError("property binding property must be a direct scalar property name")
        return cls(field, path, prop)

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "node_path": self.node_path, "property": self.property_name}


@dataclass(frozen=True)
class HarnessAction:
    tick: int
    action: str
    args: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Any, index: int) -> HarnessAction:
        data = _object(value, f"actions[{index}]", {"tick", "action", "args"})
        tick, name, args = data["tick"], data["action"], data["args"]
        if type(tick) is not int or tick < 0 or tick > _MAX_TICKS:
            raise ValidationError("action tick is out of range")
        if name not in _ALLOWED_ACTIONS:
            raise ValidationError("unsupported harness action")
        if not isinstance(args, dict):
            raise ValidationError("action args must be an object")
        shape: dict[str, tuple[set[str], set[str]]] = {
            "start_game": ({"scene_id"}, set()),
            "load_scene": ({"scene_id"}, set()),
            "start_level": ({"scene_id"}, set()),
            "spawn_entity": ({"entity_id", "parent_id"}, set()),
            "simulate_input": ({"input_id", "pressed", "duration_ticks"}, set()),
            "wait": ({"ticks"}, set()),
            "capture_screenshot": (set(), set()),
            "dump_state": (set(), set()),
            "collect_metrics": ({"ticks", "sample_interval_ticks"}, set()),
            "quit": (set(), set()),
        }
        required, optional = shape[name]
        _object(args, f"{name} args", required, optional)
        for key in ("scene_id", "entity_id", "parent_id", "input_id"):
            if key in args:
                _identifier(args[key], key)
        if "pressed" in args and type(args["pressed"]) is not bool:
            raise ValidationError("pressed must be boolean")
        for key in ("duration_ticks", "ticks", "sample_interval_ticks"):
            if key in args and (type(args[key]) is not int or not 1 <= args[key] <= _MAX_TICKS):
                raise ValidationError(f"{key} must be a positive bounded integer")
        if name == "collect_metrics" and args["sample_interval_ticks"] > args["ticks"]:
            raise ValidationError("metric sample interval cannot exceed collection ticks")
        return cls(tick, name, dict(args))

    def to_dict(self) -> dict[str, Any]:
        return {"tick": self.tick, "action": self.action, "args": self.args}


@dataclass(frozen=True)
class GameplayHarnessRequest:
    execution_id: str
    seed: int
    fixed_tick_hz: int
    max_ticks: int
    scenario: GameplayScenario
    actions: tuple[HarnessAction, ...]
    scene_allowlist: tuple[tuple[str, str], ...]
    entity_allowlist: tuple[tuple[str, str], ...]
    parent_allowlist: tuple[tuple[str, str], ...]
    input_allowlist: tuple[tuple[str, str], ...]
    property_bindings: tuple[PropertyBinding, ...]
    metrics: tuple[str, ...]
    performance_budget: PerformanceBudget | None = None
    viewport: tuple[int, int] | None = None
    schema_version: str = GAMEPLAY_HARNESS_REQUEST_VERSION

    @classmethod
    def from_dict(cls, value: Any) -> GameplayHarnessRequest:
        data = _object(
            value,
            "harness request",
            {
                "schema_version",
                "execution_id",
                "seed",
                "fixed_tick_hz",
                "max_ticks",
                "scenario",
                "actions",
                "scene_allowlist",
                "entity_allowlist",
                "parent_allowlist",
                "input_allowlist",
                "property_bindings",
                "metrics",
            },
            {"performance_budget", "viewport"},
        )
        if data["schema_version"] != GAMEPLAY_HARNESS_REQUEST_VERSION:
            raise ValidationError("unsupported harness request schema")
        execution_id = _identifier(data["execution_id"], "execution_id")
        if type(data["seed"]) is not int or not -(2**31) <= data["seed"] < 2**31:
            raise ValidationError("seed must be a signed 32-bit integer")
        if type(data["fixed_tick_hz"]) is not int or not 1 <= data["fixed_tick_hz"] <= 240:
            raise ValidationError("fixed_tick_hz must be between 1 and 240")
        if type(data["max_ticks"]) is not int or not 1 <= data["max_ticks"] <= _MAX_TICKS:
            raise ValidationError("max_ticks is out of range")
        scenario = GameplayScenario.from_dict(data["scenario"])
        action_data = data["actions"]
        if not isinstance(action_data, list) or not action_data or len(action_data) > _MAX_ACTIONS:
            raise ValidationError("actions must be a non-empty bounded array")
        actions = tuple(HarnessAction.from_dict(item, i) for i, item in enumerate(action_data))
        if tuple(sorted(actions, key=lambda item: item.tick)) != actions or any(
            a.tick > data["max_ticks"] for a in actions
        ):
            raise ValidationError("actions must be ordered within max_ticks")
        if (
            actions[0].action not in {"start_game", "load_scene", "start_level"}
            or actions[0].tick != 0
        ):
            raise ValidationError(
                "the first action must load an allowlisted start scene at fixed tick zero"
            )
        cursor_tick = 0
        for action in actions:
            if action.tick < cursor_tick:
                raise ValidationError(
                    "actions cannot be scheduled before prior timed actions finish"
                )
            duration = action.args.get("ticks", action.args.get("duration_ticks", 0))
            if action.action == "collect_metrics":
                if action.args["ticks"] < action.args["sample_interval_ticks"] * 2:
                    raise ValidationError(
                        "metric collection must include at least two real sample intervals"
                    )
                duration = action.args["ticks"]
            cursor_tick = action.tick + duration
            if cursor_tick > data["max_ticks"]:
                raise ValidationError("timed action exceeds max_ticks")
        if sum(a.action == "quit" for a in actions) != 1 or actions[-1].action != "quit":
            raise ValidationError("a single final quit action is required")
        if sum(a.action == "dump_state" for a in actions) != 1:
            raise ValidationError("exactly one selected-tick dump_state action is required")
        if sum(a.action == "collect_metrics" for a in actions) > 1:
            raise ValidationError("at most one bounded metrics collection action is allowed")
        metric_actions = [a for a in actions if a.action == "collect_metrics"]
        if (
            metric_actions
            and (metric_actions[0].args["ticks"] // metric_actions[0].args["sample_interval_ticks"])
            * len(data["metrics"])
            > 100_000
        ):
            raise ValidationError("metric sample plan exceeds the evidence sample limit")
        if sum(a.action == "capture_screenshot" for a in actions) > 16:
            raise ValidationError("at most 16 rendered screenshot captures are allowed")
        scenes = _mapping(data["scene_allowlist"], "scene_allowlist", ".tscn")
        entities = (
            _mapping(data["entity_allowlist"], "entity_allowlist", ".tscn")
            if data["entity_allowlist"]
            else ()
        )
        parents = (
            _mapping(data["parent_allowlist"], "parent_allowlist")
            if data["parent_allowlist"]
            else ()
        )
        inputs = (
            _mapping(data["input_allowlist"], "input_allowlist") if data["input_allowlist"] else ()
        )
        for action in actions:
            if action.action in {"start_game", "load_scene", "start_level"} and action.args[
                "scene_id"
            ] not in dict(scenes):
                raise ValidationError("action references a scene outside the allowlist")
            if action.action == "spawn_entity" and (
                action.args["entity_id"] not in dict(entities)
                or action.args["parent_id"] not in dict(parents)
            ):
                raise ValidationError(
                    "spawn action references an entity or parent outside the allowlist"
                )
            if action.action == "simulate_input" and action.args["input_id"] not in dict(inputs):
                raise ValidationError("input action references an input outside the allowlist")
        bindings_data = data["property_bindings"]
        if not isinstance(bindings_data, list) or len(bindings_data) > 256:
            raise ValidationError("property_bindings must be a bounded array")
        bindings = tuple(PropertyBinding.from_dict(item) for item in bindings_data)
        if len({b.field for b in bindings}) != len(bindings) or {b.field for b in bindings} - set(
            scenario.fields
        ):
            raise ValidationError("property bindings must uniquely map allowlisted scenario fields")
        metrics = data["metrics"]
        if (
            not isinstance(metrics, list)
            or len(metrics) > len(METRIC_UNITS)
            or len(set(metrics)) != len(metrics)
            or any(m not in METRIC_UNITS for m in metrics)
        ):
            raise ValidationError("metrics must select unique documented metric names")
        budget = (
            PerformanceBudget.from_dict(data["performance_budget"])
            if "performance_budget" in data
            else None
        )
        if budget is not None and set(metrics) != {item.metric for item in budget.metrics}:
            raise ValidationError("requested metrics must exactly match the performance budget")
        if metrics and budget is None:
            raise ValidationError("performance metrics require a versioned performance budget")
        if sum(a.action == "dump_state" for a in actions) != 1 or not bindings:
            raise ValidationError("dump_state requires explicit property bindings")
        if any(a.action == "collect_metrics" for a in actions) and not metrics:
            raise ValidationError("collect_metrics requires requested metric names")
        if "fps" in metrics and any(
            a.action == "collect_metrics"
            and a.args["sample_interval_ticks"] < data["fixed_tick_hz"]
            for a in actions
        ):
            raise ValidationError(
                "Godot FPS monitor updates once per second; sample interval must be at least one second"
            )
        viewport = None
        if "viewport" in data:
            v = _object(data["viewport"], "viewport", {"width", "height"})
            if (
                any(type(v[k]) is not int or not 1 <= v[k] <= 1280 for k in ("width", "height"))
                or v["width"] * v["height"] > 1280 * 1280
            ):
                raise ValidationError("viewport dimensions exceed the capture limit")
            viewport = (v["width"], v["height"])
        if any(a.action == "capture_screenshot" for a in actions) and viewport is None:
            raise ValidationError("rendered screenshot capture requires a bounded viewport")
        return cls(
            execution_id,
            data["seed"],
            data["fixed_tick_hz"],
            data["max_ticks"],
            scenario,
            actions,
            scenes,
            entities,
            parents,
            inputs,
            bindings,
            tuple(metrics),
            budget,
            viewport,
        )

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "execution_id": self.execution_id,
            "seed": self.seed,
            "fixed_tick_hz": self.fixed_tick_hz,
            "max_ticks": self.max_ticks,
            "scenario": self.scenario.to_dict(),
            "actions": [a.to_dict() for a in self.actions],
            "scene_allowlist": dict(self.scene_allowlist),
            "entity_allowlist": dict(self.entity_allowlist),
            "parent_allowlist": dict(self.parent_allowlist),
            "input_allowlist": dict(self.input_allowlist),
            "property_bindings": [b.to_dict() for b in self.property_bindings],
            "metrics": list(self.metrics),
        }
        if self.performance_budget is not None:
            value["performance_budget"] = self.performance_budget.to_dict()
        if self.viewport is not None:
            value["viewport"] = {"width": self.viewport[0], "height": self.viewport[1]}
        return value

    @property
    def scenario_sha256(self) -> str:
        return self.scenario.scenario_sha256

    @property
    def contract_sha256(self) -> str:
        value = self.to_dict()
        value.pop("execution_id")
        return hashlib.sha256(
            json.dumps(
                value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()


def scalar(value: Any) -> bool:
    return (
        value is None
        or type(value) in (bool, int, str)
        or (type(value) is float and math.isfinite(value))
    )
