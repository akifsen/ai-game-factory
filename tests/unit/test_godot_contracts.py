from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from gamefactory.adapters.engines.godot_contracts import (
    Scenario,
    evaluate_assertions,
    load_scenario,
    make_runtime_request,
    scenario_fingerprint,
    validate_observation,
)

FIXTURE_SCENARIO = Path("examples/godot-verification/scenario.json")


def _scenario_payload() -> dict[str, object]:
    return json.loads(FIXTURE_SCENARIO.read_text(encoding="utf-8"))


def _observation(scenario: Scenario, execution_id: str = "exec-test") -> dict[str, object]:
    return {
        "schema_version": "0.2.0",
        "execution_id": execution_id,
        "scenario_id": scenario.scenario_id,
        "scenario_sha256": scenario_fingerprint(scenario),
        "completed_tick": scenario.max_tick,
        "snapshots": [
            {"tick": 0, "state": {"player_hp": 100, "enemies_remaining": 3, "score": 0}},
            {"tick": 30, "state": {"player_hp": 80, "enemies_remaining": 3, "score": 0}},
            {"tick": 60, "state": {"player_hp": 80, "enemies_remaining": 2, "score": 100}},
            {"tick": 90, "state": {"player_hp": 40, "enemies_remaining": 2, "score": 100}},
        ],
        "completion": "COMPLETED",
        "error": None,
    }


def test_load_fixture_scenario_and_build_oracle_free_request() -> None:
    scenario = load_scenario(FIXTURE_SCENARIO)
    request = make_runtime_request(scenario, "exec-test")
    roundtrip = Scenario.model_validate(scenario.model_dump(mode="json"))
    json_roundtrip = Scenario.model_validate_json(scenario.model_dump_json())

    assert scenario.scenario_id == "fixture-smoke"
    assert len(scenario.assertions) == 6
    assert request["scenario_sha256"] == scenario_fingerprint(scenario)
    assert "assertions" not in request
    assert roundtrip == scenario
    assert json_roundtrip == scenario
    assert scenario_fingerprint(roundtrip) == scenario_fingerprint(scenario)
    assert scenario.model_dump(mode="json")["actions"][1] == {
        "tick": 60,
        "action": "defeat_enemy",
    }
    assert request["actions"] == [
        {"tick": 30, "action": "apply_damage", "amount": 20},
        {"tick": 60, "action": "defeat_enemy"},
        {"tick": 90, "action": "apply_damage", "amount": 40},
    ]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"schema_version": "9.0"}, "schema_version"),
        ({"unknown": True}, "unknown"),
        ({"scene": "res://../secret.tscn"}, "scene"),
        ({"scene": "res://C:/secret.tscn"}, "scene"),
        ({"scene": "res://folder\\scene.tscn"}, "scene"),
        ({"scene": "res://./Main.tscn"}, "scene"),
        ({"scene": "res://folder/./Main.tscn"}, "scene"),
        ({"scene": "res://folder//Main.tscn"}, "scene"),
        ({"scene": "res://Main.TSCN"}, "scene"),
        ({"scene": "res://bad\nscene.tscn"}, "scene"),
        ({"max_tick": True}, "max_tick"),
        ({"timeout_seconds": float("inf")}, "finite"),
        ({"actions": [{"tick": 1, "action": "run_code"}]}, "action"),
        ({"actions": [{"tick": 1, "action": "apply_damage"}]}, "amount"),
        (
            {"actions": [{"tick": 1, "action": "defeat_enemy", "amount": None}]},
            "does not accept amount",
        ),
        ({"snapshots": [1, 90]}, "tick 0"),
        (
            {
                "assertions": [
                    {
                        "id": "bad",
                        "tick": 1,
                        "field": "player_hp",
                        "operator": "equals",
                        "expected": True,
                    }
                ]
            },
            "expected",
        ),
        ({"assertions": []}, "at least 1 item"),
    ],
)
def test_scenario_rejects_invalid_fields_and_values(
    change: dict[str, object], message: str
) -> None:
    payload = _scenario_payload()
    payload.update(change)

    with pytest.raises(ValueError, match=message):
        Scenario.model_validate(payload)


def test_scenario_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":"0.2.0","schema_version":"0.2.0"}', encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON field"):
        load_scenario(path)


def test_scenario_size_limit_is_enforced(tmp_path: Path) -> None:
    path = tmp_path / "oversize.json"
    path.write_bytes(b" " * (256 * 1024 + 1))

    with pytest.raises(ValueError, match="exceeds"):
        load_scenario(path)


def test_scenario_path_must_be_a_regular_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="regular file"):
        load_scenario(tmp_path)


def test_schema_scene_pattern_rejects_non_normalized_paths() -> None:
    schema = json.loads(
        Path("src/gamefactory/schemas/godot-scenario-0.2.0.schema.json").read_text(encoding="utf-8")
    )
    pattern = re.compile(schema["properties"]["scene"]["pattern"])

    assert pattern.fullmatch("res://Main.tscn")
    assert pattern.fullmatch("res://levels/arena-01.tscn")
    for rejected in (
        "res://./Main.tscn",
        "res://../Main.tscn",
        "res://levels/./Main.tscn",
        "res://levels//Main.tscn",
        "res://C:/Main.tscn",
        "res://levels\\Main.tscn",
        "res://Main.TSCN",
    ):
        assert pattern.fullmatch(rejected) is None


def test_observation_is_bound_to_execution_scenario_and_requested_ticks() -> None:
    scenario = Scenario.model_validate(_scenario_payload())
    observation = validate_observation(_observation(scenario), scenario, "exec-test")

    assert observation.completed_tick == 90
    wrong_execution = _observation(scenario, "old-execution")
    with pytest.raises(ValueError, match="execution_id"):
        validate_observation(wrong_execution, scenario, "exec-test")

    wrong_ticks = _observation(scenario)
    wrong_ticks["snapshots"] = wrong_ticks["snapshots"][:-1]
    with pytest.raises(ValueError, match="snapshot"):
        validate_observation(wrong_ticks, scenario, "exec-test")


def test_observation_rejects_report_supplied_oracle_or_spoofed_state() -> None:
    scenario = Scenario.model_validate(_scenario_payload())
    report = _observation(scenario)
    report["expected"] = {"player_hp": 40}
    with pytest.raises(ValueError, match="extra"):
        validate_observation(report, scenario, "exec-test")

    report = _observation(scenario)
    last = report["snapshots"][-1]
    last["state"].pop("score")
    with pytest.raises(ValueError, match="score"):
        validate_observation(report, scenario, "exec-test")


def test_python_assertions_compare_expected_values_to_observed_state() -> None:
    scenario = Scenario.model_validate(_scenario_payload())
    observation = validate_observation(_observation(scenario), scenario, "exec-test")

    result = evaluate_assertions(scenario, observation)
    assert result["status"] == "PASS"
    assert result["findings"][-3:] == [
        {
            "assertion_id": "final-player-hp",
            "tick": 90,
            "field": "player_hp",
            "expected": 40,
            "actual": 40,
            "status": "PASS",
            "explanation": "Observed player_hp=40; equals 40 is satisfied",
        },
        {
            "assertion_id": "final-enemies",
            "tick": 90,
            "field": "enemies_remaining",
            "expected": 2,
            "actual": 2,
            "status": "PASS",
            "explanation": "Observed enemies_remaining=2; equals 2 is satisfied",
        },
        {
            "assertion_id": "final-score",
            "tick": 90,
            "field": "score",
            "expected": 100,
            "actual": 100,
            "status": "PASS",
            "explanation": "Observed score=100; equals 100 is satisfied",
        },
    ]

    failed_payload = _observation(scenario)
    failed_payload["snapshots"][-1]["state"]["player_hp"] = 39
    failed_observation = validate_observation(failed_payload, scenario, "exec-test")
    failed_result = evaluate_assertions(scenario, failed_observation)
    assert failed_result["status"] == "FAIL"
    assert failed_result["findings"][3]["actual"] == 39
    assert failed_result["findings"][3]["status"] == "FAIL"


@pytest.mark.parametrize(
    "operator,expected,actual,passed",
    [
        ("equals", 5, 5, True),
        ("minimum", 5, 5, True),
        ("minimum", 6, 5, False),
        ("maximum", 5, 5, True),
        ("maximum", 4, 5, False),
    ],
)
def test_assertion_operators_are_inclusive(
    operator: str, expected: int, actual: int, passed: bool
) -> None:
    scenario_data = _scenario_payload()
    scenario_data["actions"] = []
    scenario_data["snapshots"] = [0, 90]
    scenario_data["assertions"] = [
        {"id": "operator", "tick": 90, "field": "score", "operator": operator, "expected": expected}
    ]
    scenario = Scenario.model_validate(scenario_data)
    report = _observation(scenario)
    report["snapshots"] = [
        {"tick": 0, "state": {"player_hp": 100, "enemies_remaining": 3, "score": 0}},
        {"tick": 90, "state": {"player_hp": 100, "enemies_remaining": 3, "score": actual}},
    ]
    result = evaluate_assertions(scenario, validate_observation(report, scenario, "exec-test"))

    assert (result["status"] == "PASS") is passed
