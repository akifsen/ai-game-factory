"""Workflow integration contract for the optional Godot gameplay quality feature."""

import hashlib
import json
from types import SimpleNamespace

from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.game_quality import GAMEPLAY_SCENARIO_VERSION
from gamefactory.core.domain.gameplay_harness import GAMEPLAY_HARNESS_REQUEST_VERSION
from gamefactory.core.domain.models import Execution, ExecutionStatus
from gamefactory.workflows.gameplay_quality import (
    _performance_samples_match_raw,
    _select_attempt_manifest,
    _select_completed_execution,
    create_gameplay_quality_workflow,
    register_gameplay_quality_handlers,
)
from gamefactory.workflows.handlers import TaskHandlerRegistry


def _request() -> dict[str, object]:
    scenario: dict[str, object] = {
        "schema_version": GAMEPLAY_SCENARIO_VERSION,
        "scenario_id": "integration-smoke",
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
    return {
        "schema_version": GAMEPLAY_HARNESS_REQUEST_VERSION,
        "execution_id": "attempt-placeholder",
        "seed": 7,
        "fixed_tick_hz": 60,
        "max_ticks": 10,
        "scenario": scenario,
        "actions": [
            {"tick": 0, "action": "start_game", "args": {"scene_id": "main"}},
            {"tick": 1, "action": "dump_state", "args": {}},
            {"tick": 1, "action": "quit", "args": {}},
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


def test_workflow_exposes_execute_evaluate_then_final_evidence(tmp_path) -> None:
    root = tmp_path / "godot-project"
    root.mkdir()
    (root / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    (root / "main.tscn").write_text("[gd_scene format=3]\n", encoding="utf-8")
    request_path = root / "gameplay-request.json"
    request_path.write_text(json.dumps(_request()), encoding="utf-8")
    executable = tmp_path / "godot"
    executable.write_bytes(b"test-only placeholder; never launched by workflow registration")

    workflow, tasks = create_gameplay_quality_workflow("PROJECT-1", root, executable, request_path)

    assert [task.task_type for task in tasks] == [
        "gameplay_harness_execute",
        "gameplay_quality_evaluate",
        "record_evidence",
    ]
    assert tasks[1].depends_on == [tasks[0].id]
    assert tasks[2].depends_on == [tasks[1].id]
    assert tasks[0].parameters["request_snapshot"]["scenario"]["scenario_id"] == "integration-smoke"
    assert tasks[0].parameters["source_manifest_sha256"]

    registry = TaskHandlerRegistry()
    register_gameplay_quality_handlers(
        registry, root, ArtifactManager(root), None, None, None, None
    )
    assert registry.get("gameplay_harness_execute") is not None
    assert registry.get("gameplay_quality_evaluate") is not None
    assert workflow.id == tasks[0].workflow_id


def test_retry_evaluation_selects_manifest_from_latest_completed_attempt_only() -> None:
    first = Execution(
        "exec-1", "execute-task", 1, ExecutionStatus.COMPLETED, "2026-01-01T00:00:00Z"
    )
    retry = Execution(
        "exec-2", "execute-task", 2, ExecutionStatus.COMPLETED, "2026-01-01T00:01:00Z"
    )
    failed_retry = Execution(
        "exec-3", "execute-task", 3, ExecutionStatus.FAILED, "2026-01-01T00:02:00Z"
    )
    selected = _select_completed_execution([first, retry, failed_retry], "execute-task")
    artifacts = [
        SimpleNamespace(
            artifact_type="gameplay-run-manifest",
            relative_path=".gamefactory/artifacts/wf/execute-task/attempt-1/exec-1/run-manifest.json",
            id="manifest-1",
        ),
        SimpleNamespace(
            artifact_type="gameplay-run-manifest",
            relative_path=".gamefactory/artifacts/wf/execute-task/attempt-2/exec-2/run-manifest.json",
            id="manifest-2",
        ),
        SimpleNamespace(
            artifact_type="gameplay-run-manifest",
            relative_path=".gamefactory/artifacts/wf/execute-task/attempt-3/exec-3/run-manifest.json",
            id="manifest-3",
        ),
    ]

    manifest = _select_attempt_manifest(artifacts, "wf", "execute-task", selected)

    assert selected.id == "exec-2"
    assert manifest.id == "manifest-2"


def test_performance_evidence_must_match_selected_raw_sample_window() -> None:
    raw = {
        "started_at": "2026-01-01T00:00:00Z",
        "ended_at": "2026-01-01T00:00:02Z",
        "sample_interval_ms": 500.0,
        "samples": [{"metric": "fps", "unit": "frames_per_second", "samples": [60.0, 59.0]}],
    }
    evidence = {
        "schema_version": "performance-evidence-1.0.0",
        "platform": "windows",
        "budget_sha256": "a" * 64,
        "engine": "Godot 4.7",
        "collection_method": "godot-profiler-v1",
        "started_at": raw["started_at"],
        "ended_at": raw["ended_at"],
        "sample_interval_ms": 500.0,
        "scenario_sha256": "b" * 64,
        "samples": raw["samples"],
        "evidence_refs": ["run-manifest.json"],
    }
    from gamefactory.core.domain.game_quality import PerformanceEvidence

    persisted = PerformanceEvidence.from_dict(evidence)
    assert _performance_samples_match_raw(persisted, raw)

    mixed_attempt = {
        **raw,
        "samples": [{"metric": "fps", "unit": "frames_per_second", "samples": [30.0, 29.0]}],
    }
    assert not _performance_samples_match_raw(persisted, mixed_attempt)
    wrong_window = {**raw, "ended_at": "2026-01-01T00:00:03Z"}
    assert not _performance_samples_match_raw(persisted, wrong_window)
