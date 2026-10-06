"""Focused regression tests for GDScript batch-parse output validation."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.engines.godot_staging import GodotStager
from gamefactory.cli.factory_gates import (
    _PARSE_OUTPUT_MAX_BYTES,
    _PARSE_OUTPUT_NAME,
    GodotImportGateRunner,
    _validated_gdscript_parse_scripts,
)
from gamefactory.core.execution.process_runner import CommandRequest, CommandResult, ProcessRunner


def _ok_result() -> CommandResult:
    return CommandResult(exit_code=0, stdout="", stderr="", cleanup_completed=True)


@pytest.mark.parametrize(
    ("payload", "expected_paths"),
    [
        (["not-an-object"], ["a.gd"]),
        ({"scripts": "nope"}, ["a.gd"]),
        ({"scripts": [{"path": "a.gd", "status": "MAYBE"}]}, ["a.gd"]),
        (
            {"scripts": [{"path": "a.gd", "status": "PASS"}, {"path": "a.gd", "status": "PASS"}]},
            ["a.gd"],
        ),
        ({"scripts": [{"path": "a.gd", "status": "PASS"}]}, ["a.gd", "b.gd"]),
        ({"scripts": [{"path": "extra.gd", "status": "PASS"}]}, ["a.gd"]),
        ({"scripts": [{"status": "PASS"}]}, ["a.gd"]),
    ],
)
def test_validated_gdscript_parse_scripts_rejects_malformed(
    payload: Any, expected_paths: list[str]
) -> None:
    assert _validated_gdscript_parse_scripts(payload, expected_paths) is None


def test_validated_gdscript_parse_scripts_accepts_exact_manifest() -> None:
    payload = {
        "scripts": [
            {"path": "a.gd", "status": "PASS", "message": ""},
            {"path": "b.gd", "status": "FAIL", "message": "load_failed"},
        ]
    }
    parsed = _validated_gdscript_parse_scripts(payload, ["a.gd", "b.gd"])
    assert parsed == {
        "a.gd": {"status": "PASS", "message": ""},
        "b.gd": {"status": "FAIL", "message": "load_failed"},
    }


def test_validated_gdscript_parse_scripts_accepts_empty_manifest() -> None:
    assert _validated_gdscript_parse_scripts({"scripts": []}, []) == {}


class _GateFakeRunner(ProcessRunner):
    def __init__(self, *, parse_output: bytes | None = None, oversize_output: bool = False) -> None:
        self._parse_output = parse_output
        self._oversize_output = oversize_output
        self.import_calls = 0
        self.parse_calls = 0

    def run(self, request: CommandRequest) -> CommandResult:
        if "--import" in request.args:
            self.import_calls += 1
            return _ok_result()
        self.parse_calls += 1
        cwd = Path(request.cwd)
        output_path = cwd / _PARSE_OUTPUT_NAME
        if self._oversize_output:
            output_path.write_bytes(b"x" * (_PARSE_OUTPUT_MAX_BYTES + 1))
        elif self._parse_output is not None:
            output_path.write_bytes(self._parse_output)
        return _ok_result()


def _init_code_gate_project(root: Path, *, with_script: bool) -> str:
    root.mkdir(parents=True)
    (root / "project.godot").write_text(
        'config_version=5\n\n[application]\nconfig/name="Parse gate fixture"\n',
        encoding="utf-8",
    )
    if with_script:
        (root / "player.gd").write_text("extends Node2D\n", encoding="utf-8")
    else:
        (root / "main.tscn").write_text(
            '[gd_scene format=3]\n\n[node name="Main" type="Node2D"]\n',
            encoding="utf-8",
        )
    _, candidate_sha256 = GodotStager(root, root / ".gamefactory" / "scratch").source_manifest()
    return candidate_sha256


def _evaluate_with_runner(
    root: Path, executable: Path, runner: _GateFakeRunner, candidate_sha256: str
) -> dict[str, object]:
    gate = GodotImportGateRunner(root, executable, timeout_seconds=120)
    receipts: list[str] = []

    def record_process_intent(_kind: str, _details: dict[str, object]) -> str:
        receipts.append(_kind)
        return f"receipt-{len(receipts)}"

    import gamefactory.cli.factory_gates as factory_gates

    original_runner = factory_gates.ProcessRunner
    factory_gates.ProcessRunner = lambda **kwargs: runner  # type: ignore[misc, assignment]
    try:
        return gate.evaluate(
            root,
            candidate_sha256,
            gate="code",
            task_spec={"task_id": "TASK-PARSE-OUTPUT", "parameters": {}},
            parameters={
                "execution_id": "EXEC-PARSE-OUTPUT",
                "runner_config_sha256": gate.config_fingerprint,
                "record_process_intent": record_process_intent,
                "timeout_seconds": 120,
            },
        )
    finally:
        factory_gates.ProcessRunner = original_runner


def test_code_gate_skips_batch_parse_when_no_gdscripts(tmp_path: Path) -> None:
    root = tmp_path / "scene-only"
    candidate_sha256 = _init_code_gate_project(root, with_script=False)
    executable = tmp_path / "godot"
    executable.write_bytes(b"fake-godot")
    runner = _GateFakeRunner()

    result = _evaluate_with_runner(root, executable, runner, candidate_sha256)
    evidence = json.loads(base64.b64decode(result["evidence_files"][0]["content_base64"]))

    assert runner.import_calls == 1
    assert runner.parse_calls == 0
    assert result["process_receipt_ids"] == ["receipt-1"]
    assert result["report"]["findings"][0]["status"] == "PASS"
    assert evidence["script_results"] == []


def test_code_gate_fails_on_non_object_parse_json(tmp_path: Path) -> None:
    root = tmp_path / "with-script"
    candidate_sha256 = _init_code_gate_project(root, with_script=True)
    executable = tmp_path / "godot"
    executable.write_bytes(b"fake-godot")
    runner = _GateFakeRunner(parse_output=json.dumps(["array-instead-of-object"]).encode())

    result = _evaluate_with_runner(root, executable, runner, candidate_sha256)
    evidence = json.loads(base64.b64decode(result["evidence_files"][0]["content_base64"]))

    assert runner.parse_calls == 1
    assert result["report"]["findings"][0]["status"] == "FAIL"
    assert evidence["script_results"][0]["status"] == "FAIL"
    assert evidence["script_results"][0]["message"] == "missing_parse_result"


def test_code_gate_fails_on_duplicate_parse_rows(tmp_path: Path) -> None:
    root = tmp_path / "duplicate-rows"
    candidate_sha256 = _init_code_gate_project(root, with_script=True)
    executable = tmp_path / "godot"
    executable.write_bytes(b"fake-godot")
    payload = json.dumps(
        {
            "scripts": [
                {"path": "player.gd", "status": "PASS", "message": ""},
                {"path": "player.gd", "status": "PASS", "message": ""},
            ]
        }
    ).encode()
    runner = _GateFakeRunner(parse_output=payload)

    result = _evaluate_with_runner(root, executable, runner, candidate_sha256)
    evidence = json.loads(base64.b64decode(result["evidence_files"][0]["content_base64"]))

    assert result["report"]["findings"][0]["status"] == "FAIL"
    assert evidence["script_results"][0]["status"] == "FAIL"


def test_code_gate_fails_on_oversize_parse_output(tmp_path: Path) -> None:
    root = tmp_path / "oversize-output"
    candidate_sha256 = _init_code_gate_project(root, with_script=True)
    executable = tmp_path / "godot"
    executable.write_bytes(b"fake-godot")
    runner = _GateFakeRunner(oversize_output=True)

    result = _evaluate_with_runner(root, executable, runner, candidate_sha256)
    evidence = json.loads(base64.b64decode(result["evidence_files"][0]["content_base64"]))

    assert result["report"]["findings"][0]["status"] == "FAIL"
    assert evidence["script_results"][0]["status"] == "FAIL"
    assert evidence["script_results"][0]["log_error"] is True
