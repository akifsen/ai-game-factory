"""Strict contract tests for finite gameplay harness requests and provenance hashes."""

import hashlib
import json

import pytest

from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.game_quality import GAMEPLAY_SCENARIO_VERSION
from gamefactory.core.domain.gameplay_harness import (
    GAMEPLAY_HARNESS_REQUEST_VERSION,
    GameplayHarnessRequest,
)


def _scenario() -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": GAMEPLAY_SCENARIO_VERSION,
        "scenario_id": "smoke-test",
        "domains": ["progression"],
        "fields": ["player.health"],
        "rules": [
            {
                "rule_id": "health-positive",
                "field": "player.health",
                "operator": "gt",
                "expected": 0,
            }
        ],
    }
    canonical = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    value["scenario_sha256"] = hashlib.sha256(canonical).hexdigest()
    return value


def _request() -> dict[str, object]:
    return {
        "schema_version": GAMEPLAY_HARNESS_REQUEST_VERSION,
        "execution_id": "attempt-placeholder",
        "seed": 17,
        "fixed_tick_hz": 60,
        "max_ticks": 20,
        "scenario": _scenario(),
        "actions": [
            {"tick": 0, "action": "start_game", "args": {"scene_id": "main"}},
            {"tick": 1, "action": "wait", "args": {"ticks": 1}},
            {"tick": 2, "action": "dump_state", "args": {}},
            {"tick": 2, "action": "quit", "args": {}},
        ],
        "scene_allowlist": {"main": "res://main.tscn"},
        "entity_allowlist": {},
        "parent_allowlist": {},
        "input_allowlist": {},
        "property_bindings": [
            {"field": "player.health", "node_path": "/root/Game/Player", "property": "health"}
        ],
        "metrics": [],
    }


def test_request_is_finite_allowlisted_and_hashes_execution_independent_contract() -> None:
    first = GameplayHarnessRequest.from_dict(_request())
    changed_execution = _request()
    changed_execution["execution_id"] = "EXEC-12345678"
    second = GameplayHarnessRequest.from_dict(changed_execution)

    assert first.scenario_sha256 == first.scenario.scenario_sha256
    assert first.contract_sha256 == second.contract_sha256
    assert first.scenario_sha256 == second.scenario_sha256


def test_request_rejects_arbitrary_action_method_and_extra_action_fields() -> None:
    unsafe = _request()
    unsafe["actions"] = [
        {"tick": 0, "action": "start_game", "args": {"scene_id": "main"}},
        {"tick": 1, "action": "call_method", "args": {"method": "quit"}},
        {"tick": 2, "action": "dump_state", "args": {}},
        {"tick": 2, "action": "quit", "args": {}},
    ]
    with pytest.raises(ValidationError, match="unsupported harness action"):
        GameplayHarnessRequest.from_dict(unsafe)

    extra = _request()
    extra["actions"] = list(extra["actions"])
    extra["actions"][0] = {**extra["actions"][0], "script": "res://arbitrary.gd"}
    with pytest.raises(ValidationError, match="unknown fields"):
        GameplayHarnessRequest.from_dict(extra)


def test_request_rejects_unallowlisted_scene_and_property_alias() -> None:
    scene = _request()
    scene["actions"] = list(scene["actions"])
    scene["actions"][0] = {"tick": 0, "action": "start_game", "args": {"scene_id": "other"}}
    with pytest.raises(ValidationError, match="outside the allowlist"):
        GameplayHarnessRequest.from_dict(scene)

    field = _request()
    field["property_bindings"] = [
        {"field": "unapproved", "node_path": "/root/Game/Player", "property": "health"}
    ]
    with pytest.raises(ValidationError, match="scenario fields"):
        GameplayHarnessRequest.from_dict(field)


def test_request_rejects_schedule_that_exceeds_fixed_tick_budget() -> None:
    request = _request()
    request["max_ticks"] = 2
    request["actions"] = list(request["actions"])
    request["actions"][1] = {"tick": 0, "action": "wait", "args": {"ticks": 3}}
    with pytest.raises(ValidationError, match="exceeds max_ticks"):
        GameplayHarnessRequest.from_dict(request)
