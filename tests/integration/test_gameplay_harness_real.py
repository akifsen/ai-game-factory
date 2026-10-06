"""Opt-in black-box run of the bounded harness against a real Godot executable."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
from pathlib import Path

import pytest

from gamefactory.adapters.engines.gameplay_harness import (
    COLLECTION_METHOD,
    execute_gameplay_harness,
)
from gamefactory.adapters.engines.godot_staging import GodotStager, sha256_file
from gamefactory.cli.factory_gates import GodotImportGateRunner
from gamefactory.core.domain.game_quality import (
    GAMEPLAY_SCENARIO_VERSION,
    PERFORMANCE_BUDGET_VERSION,
)
from gamefactory.core.domain.gameplay_harness import (
    GAMEPLAY_HARNESS_REQUEST_VERSION,
    GameplayHarnessRequest,
)


@pytest.mark.real_godot
def test_real_godot_gameplay_harness_observes_state_and_raw_metrics(tmp_path: Path) -> None:
    executable_value = os.environ.get("GAMEFACTORY_TEST_GODOT")
    if not executable_value:
        pytest.skip("Set GAMEFACTORY_TEST_GODOT to run real gameplay harness acceptance")
    executable = Path(executable_value).expanduser().resolve(strict=True)
    project = tmp_path / "bounded-harness-project"
    project.mkdir()
    (project / "project.godot").write_text(
        'config_version=5\n\n[application]\nconfig/name="Harness fixture"\n',
        encoding="utf-8",
    )
    (project / "player.gd").write_text(
        "extends Node2D\n@export var health: float = 100.0\n", encoding="utf-8"
    )
    (project / "main.tscn").write_text(
        "[gd_scene load_steps=2 format=3]\n\n"
        '[ext_resource type="Script" path="res://player.gd" id="1"]\n\n'
        '[node name="Game" type="Node2D"]\n\n'
        '[node name="Player" type="Node2D" parent="."]\n'
        'script = ExtResource("1")\n',
        encoding="utf-8",
    )

    scenario: dict[str, object] = {
        "schema_version": GAMEPLAY_SCENARIO_VERSION,
        "scenario_id": "real-godot-harness-smoke",
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
    scenario["scenario_sha256"] = hashlib.sha256(
        json.dumps(scenario, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    budget = {
        "schema_version": PERFORMANCE_BUDGET_VERSION,
        "platform": "windows"
        if platform.system().lower() == "windows"
        else platform.system().lower(),
        "collection_methods": [COLLECTION_METHOD],
        "metrics": [
            {
                "metric": "entity_count",
                "unit": "count",
                "min_value": 0,
                "max_value": 100_000,
                "min_samples": 2,
            },
        ],
    }
    request_data = {
        "schema_version": GAMEPLAY_HARNESS_REQUEST_VERSION,
        "execution_id": "attempt-placeholder",
        "seed": 23,
        "fixed_tick_hz": 60,
        "max_ticks": 12,
        "scenario": scenario,
        "actions": [
            {"tick": 0, "action": "start_game", "args": {"scene_id": "main"}},
            {"tick": 1, "action": "dump_state", "args": {}},
            {
                "tick": 2,
                "action": "collect_metrics",
                "args": {"ticks": 8, "sample_interval_ticks": 2},
            },
            {"tick": 10, "action": "quit", "args": {}},
        ],
        "scene_allowlist": {"main": "res://main.tscn"},
        "entity_allowlist": {},
        "parent_allowlist": {},
        "input_allowlist": {},
        "property_bindings": [
            {
                "field": "player.health",
                "node_path": "/root/Game/Player",
                "property": "health",
            }
        ],
        "metrics": ["entity_count"],
        "performance_budget": budget,
    }
    request_path = project / "harness-request.json"
    request_path.write_text(json.dumps(request_data), encoding="utf-8")
    request = GameplayHarnessRequest.from_dict(request_data)
    _, source_sha256 = GodotStager(project, project / ".gamefactory" / "scratch").source_manifest(
        request_path
    )
    harness_bytes = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "gamefactory"
        / "resources"
        / "godot"
        / "gameplay_harness.gd"
    ).read_bytes()

    run = execute_gameplay_harness(
        project,
        executable,
        request_path,
        request,
        "WF-REAL-HARNESS",
        "TASK-REAL-HARNESS",
        1,
        timeout_seconds=120,
        expected_execution_id="EXEC-REAL-HARNESS",
        expected_request_sha256=sha256_file(request_path),
        expected_source_sha256=source_sha256,
        expected_executable_sha256=sha256_file(executable),
        expected_harness_sha256=hashlib.sha256(harness_bytes).hexdigest(),
    )

    raw_output = json.loads(run.output_path.read_text(encoding="utf-8"))
    assert run.observation.state == {"player.health": 100.0}
    assert run.observation.scenario_sha256 == request.scenario_sha256
    assert run.performance is not None
    assert run.performance.scenario_sha256 == request.scenario_sha256
    assert run.performance.collection_method == COLLECTION_METHOD
    assert {sample.metric for sample in run.performance.samples} == {"entity_count"}
    for sample in run.performance.samples:
        assert len(sample.samples) >= 2
        assert all(value >= 0 for value in sample.samples)
    assert raw_output["source_sha256"] == source_sha256
    assert raw_output["execution_id"] == "EXEC-REAL-HARNESS"
    assert raw_output["execution_mode"] == "wall_clock_render_fixed_physics"
    assert run.engine.startswith("Godot 4.")

    gate = GodotImportGateRunner(project, executable, timeout_seconds=120)
    gate_receipts: list[tuple[str, dict[str, object]]] = []
    _, candidate_sha256 = GodotStager(
        project, project / ".gamefactory" / "scratch"
    ).source_manifest()

    def record_process_intent(kind: str, details: dict[str, object]) -> str:
        gate_receipts.append((kind, details))
        return f"receipt-{len(gate_receipts)}"

    gate_result = gate.evaluate(
        project,
        candidate_sha256,
        gate="code",
        task_spec={"task_id": "TASK-REAL-CODE-GATE", "parameters": {}},
        parameters={
            "execution_id": "EXEC-REAL-CODE-GATE",
            "runner_config_sha256": gate.config_fingerprint,
            "record_process_intent": record_process_intent,
            "timeout_seconds": 120,
        },
    )
    assert gate_result["candidate_sha256"] == candidate_sha256
    assert gate_result["process_receipt_ids"] == ["receipt-1", "receipt-2"]
    assert gate_receipts[0][0] == "factory-gate-code-godot-import"
    assert gate_receipts[1][0] == "factory-gate-code-gdscript-parse"
    gate_evidence = json.loads(base64.b64decode(gate_result["evidence_files"][0]["content_base64"]))
    assert gate_result["report"]["findings"][0]["status"] == "PASS", repr(
        (gate_result["report"]["findings"], gate_evidence)
    )
    assert gate_evidence["script_results"][0]["status"] == "PASS"

    (project / "broken.gd").write_text("extends Node2D\nfunc broken(:\n", encoding="utf-8")
    _, broken_candidate_sha256 = GodotStager(
        project, project / ".gamefactory" / "scratch"
    ).source_manifest()
    broken_result = gate.evaluate(
        project,
        broken_candidate_sha256,
        gate="code",
        task_spec={"task_id": "TASK-REAL-BROKEN-CODE-GATE", "parameters": {}},
        parameters={
            "execution_id": "EXEC-REAL-BROKEN-CODE-GATE",
            "runner_config_sha256": gate.config_fingerprint,
            "record_process_intent": record_process_intent,
            "timeout_seconds": 120,
        },
    )
    assert broken_result["report"]["findings"][0]["status"] == "FAIL"
    broken_evidence = json.loads(
        base64.b64decode(broken_result["evidence_files"][0]["content_base64"])
    )
    assert any(
        result["path"] == "broken.gd" and result["status"] == "FAIL"
        for result in broken_evidence["script_results"]
    )


@pytest.mark.real_godot
def test_real_godot_import_gate_compiles_scripts_with_autoload_context(tmp_path: Path) -> None:
    executable_value = os.environ.get("GAMEFACTORY_TEST_GODOT")
    if not executable_value:
        pytest.skip("Set GAMEFACTORY_TEST_GODOT to run real Godot import gate acceptance")
    executable = Path(executable_value).expanduser().resolve(strict=True)
    project = tmp_path / "autoload-import-gate"
    project.mkdir()
    (project / "project.godot").write_text(
        'config_version=5\n\n[application]\nconfig/name="Autoload import gate"\n\n'
        '[autoload]\nGateAutoload="*res://gate_autoload.gd"\n',
        encoding="utf-8",
    )
    (project / "gate_helper.gd").write_text(
        "extends RefCounted\nclass_name GateHelper\n\nstatic func value() -> int:\n\treturn 7\n",
        encoding="utf-8",
    )
    (project / "gate_autoload.gd").write_text(
        "extends Node\n\nfunc value() -> int:\n\treturn GateHelper.value()\n",
        encoding="utf-8",
    )
    (project / "uses_autoload.gd").write_text(
        "extends Node\n\nfunc _ready() -> void:\n\tvar total := GateAutoload.value()\n",
        encoding="utf-8",
    )

    gate = GodotImportGateRunner(project, executable, timeout_seconds=120)
    gate_receipts: list[tuple[str, dict[str, object]]] = []

    def record_process_intent(kind: str, details: dict[str, object]) -> str:
        gate_receipts.append((kind, details))
        return f"receipt-{len(gate_receipts)}"

    _, candidate_sha256 = GodotStager(
        project, project / ".gamefactory" / "scratch"
    ).source_manifest()
    valid_result = gate.evaluate(
        project,
        candidate_sha256,
        gate="code",
        task_spec={"task_id": "TASK-AUTOLOAD-VALID", "parameters": {}},
        parameters={
            "execution_id": "EXEC-AUTOLOAD-VALID",
            "runner_config_sha256": gate.config_fingerprint,
            "record_process_intent": record_process_intent,
            "timeout_seconds": 120,
        },
    )
    valid_evidence = json.loads(
        base64.b64decode(valid_result["evidence_files"][0]["content_base64"])
    )
    by_path = {item["path"]: item for item in valid_evidence["script_results"]}
    assert valid_result["report"]["findings"][0]["status"] == "PASS", repr(valid_evidence)
    assert by_path["uses_autoload.gd"]["status"] == "PASS"
    assert gate_receipts[1][1]["profile"] == "project-context-batch-parse"

    (project / "invalid_unused.gd").write_text("extends Node\nfunc broken(:\n", encoding="utf-8")
    _, invalid_candidate_sha256 = GodotStager(
        project, project / ".gamefactory" / "scratch"
    ).source_manifest()
    invalid_result = gate.evaluate(
        project,
        invalid_candidate_sha256,
        gate="code",
        task_spec={"task_id": "TASK-AUTOLOAD-INVALID", "parameters": {}},
        parameters={
            "execution_id": "EXEC-AUTOLOAD-INVALID",
            "runner_config_sha256": gate.config_fingerprint,
            "record_process_intent": record_process_intent,
            "timeout_seconds": 120,
        },
    )
    invalid_evidence = json.loads(
        base64.b64decode(invalid_result["evidence_files"][0]["content_base64"])
    )
    assert invalid_result["report"]["findings"][0]["status"] == "FAIL"
    invalid_by_path = {item["path"]: item for item in invalid_evidence["script_results"]}
    assert invalid_by_path["invalid_unused.gd"]["status"] == "FAIL"


@pytest.mark.real_godot
def test_real_godot_import_gate_passes_scene_only_candidate_without_gdscripts(
    tmp_path: Path,
) -> None:
    executable_value = os.environ.get("GAMEFACTORY_TEST_GODOT")
    if not executable_value:
        pytest.skip("Set GAMEFACTORY_TEST_GODOT to run real Godot import gate acceptance")
    executable = Path(executable_value).expanduser().resolve(strict=True)
    project = tmp_path / "scene-only-import-gate"
    project.mkdir()
    (project / "project.godot").write_text(
        'config_version=5\n\n[application]\nconfig/name="Scene only import gate"\n',
        encoding="utf-8",
    )
    (project / "main.tscn").write_text(
        '[gd_scene format=3]\n\n[node name="Main" type="Node2D"]\n',
        encoding="utf-8",
    )

    gate = GodotImportGateRunner(project, executable, timeout_seconds=120)

    def record_process_intent(_kind: str, _details: dict[str, object]) -> str:
        return "receipt-scene-only"

    _, candidate_sha256 = GodotStager(
        project, project / ".gamefactory" / "scratch"
    ).source_manifest()
    result = gate.evaluate(
        project,
        candidate_sha256,
        gate="code",
        task_spec={"task_id": "TASK-SCENE-ONLY", "parameters": {}},
        parameters={
            "execution_id": "EXEC-SCENE-ONLY",
            "runner_config_sha256": gate.config_fingerprint,
            "record_process_intent": record_process_intent,
            "timeout_seconds": 120,
        },
    )
    evidence = json.loads(base64.b64decode(result["evidence_files"][0]["content_base64"]))
    assert result["report"]["findings"][0]["status"] == "PASS", repr(evidence)
    assert evidence["script_results"] == []
    assert result["process_receipt_ids"] == ["receipt-scene-only"]
