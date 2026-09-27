"""Blender failure propagation, export status, and output-path regressions."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType

import pytest

from gamefactory.adapters.dcc.blender_processor import (
    BLENDER_PYTHON_FAILURE_EXIT_CODE,
    BlenderAssetProcessor,
)
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.core.domain.asset_contracts import AssetSpecification, parse_asset_specification
from gamefactory.core.domain.errors import DccFailedError
from gamefactory.core.execution.process_runner import CommandRequest, CommandResult


def _load_process_script() -> ModuleType:
    path = Path("src/gamefactory/resources/blender/process_asset.py")
    spec = importlib.util.spec_from_file_location("process_asset_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spec() -> AssetSpecification:
    return parse_asset_specification(
        Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
    )


class _RecordingRunner:
    def __init__(self, result: CommandResult, writer=None) -> None:
        self.result = result
        self.writer = writer
        self.requests: list[CommandRequest] = []
        self.env = {"PATH": "secret-value", "HOME": "/home/runner"}

    def build_env(self, request: CommandRequest) -> dict[str, str]:
        return dict(self.env)

    def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        if self.writer is not None:
            self.writer(request)
        return self.result


def _processor(tmp_path: Path, runner: _RecordingRunner) -> tuple[Path, BlenderAssetProcessor]:
    raw = tmp_path / "raw.glb"
    create_box_glb(output_path=raw)
    processor = BlenderAssetProcessor("blender-test", runner)  # type: ignore[arg-type]
    return raw, processor


def _write_success(request: CommandRequest) -> None:
    args = request.args
    raw = Path(args[args.index("--input-glb") + 1])
    output = Path(args[args.index("--output-glb") + 1])
    report = Path(args[args.index("--report-path") + 1])
    script = Path(args[args.index("--python") + 1])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"glb-bytes-nonempty")
    payload = {
        "status": "SUCCESS",
        "exit_code": 0,
        "blender_version": "4.0.2",
        "input_raw_glb_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "processing_script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "raw_metrics": {},
        "processed_metrics": {},
    }
    report.write_text(json.dumps(payload), encoding="utf-8")


def test_command_sets_python_exit_code_and_absolute_spaced_output(tmp_path: Path) -> None:
    runner = _RecordingRunner(CommandResult(exit_code=0, stdout="Blender 4.0.2\n", stderr=""))
    runner.writer = _write_success
    raw, processor = _processor(tmp_path, runner)
    output = tmp_path / "out dir" / "processed.glb"
    report = tmp_path / "out dir" / "report.json"
    result = processor.process_asset(raw, output, _spec(), report_path=report)
    args = runner.requests[0].args
    flag = args.index("--python-exit-code")
    assert args[flag + 1] == str(BLENDER_PYTHON_FAILURE_EXIT_CODE)
    assert args[flag + 2] == "--python"
    assert args.index("--") > flag
    output_arg = args[args.index("--output-glb") + 1]
    assert output_arg == os.path.abspath(output)
    assert Path(output_arg).is_absolute()
    assert "out dir" in output_arg
    assert output_arg.endswith(".glb")
    assert runner.requests[0].cwd == output.parent
    assert result.exit_code == 0
    assert output.stat().st_size > 0
    assert not report.with_suffix(".blender-diagnostics.txt").exists()


def test_python_failure_exit_is_dcc_failed_and_preserves_traceback(tmp_path: Path) -> None:
    stderr = 'Traceback (most recent call last):\n  File "process_asset.py"\nRuntimeError: boom\n'
    runner = _RecordingRunner(
        CommandResult(
            exit_code=BLENDER_PYTHON_FAILURE_EXIT_CODE, stdout="Blender 4.0.2\n", stderr=stderr
        )
    )
    raw, processor = _processor(tmp_path, runner)
    output = tmp_path / "processed.glb"
    with pytest.raises(DccFailedError, match="Blender processing failed") as caught:
        processor.process_asset(raw, output, _spec())
    error = caught.value
    assert error.code == "DCC_FAILED"
    assert error.exit_code == BLENDER_PYTHON_FAILURE_EXIT_CODE
    message = str(error)
    assert "Traceback (most recent call last):" in message
    assert "RuntimeError: boom" in message
    assert "secret-value" not in message
    assert "keys=HOME,PATH" in message
    assert "--python-exit-code" in message
    assert not output.exists()
    artifact = tmp_path / "prop_energy_crate_01_blender_report.blender-diagnostics.txt"
    assert artifact.is_file()
    assert "RuntimeError: boom" in artifact.read_text(encoding="utf-8")


def test_exit_zero_without_output_stays_dcc_failed_and_keeps_stderr(tmp_path: Path) -> None:
    stderr = "PROCESSING_FAILED: RuntimeError: export did not run\n"
    runner = _RecordingRunner(CommandResult(exit_code=0, stdout="Blender 4.0.2\n", stderr=stderr))
    raw, processor = _processor(tmp_path, runner)
    output = tmp_path / "processed.glb"
    with pytest.raises(DccFailedError, match="missing or empty") as caught:
        processor.process_asset(raw, output, _spec())
    message = str(caught.value)
    assert caught.value.code == "DCC_FAILED"
    assert caught.value.exit_code == 0
    assert "PROCESSING_FAILED: RuntimeError: export did not run" in message
    assert str(output) in message
    assert "processing_script:" in message
    assert "processing_contract:" in message


def test_exit_zero_empty_output_stays_dcc_failed(tmp_path: Path) -> None:
    def write_empty(request: CommandRequest) -> None:
        output = Path(request.args[request.args.index("--output-glb") + 1])
        output.write_bytes(b"")

    runner = _RecordingRunner(CommandResult(exit_code=0, stdout="", stderr=""), writer=write_empty)
    raw, processor = _processor(tmp_path, runner)
    output = tmp_path / "processed.glb"
    with pytest.raises(DccFailedError, match="missing or empty"):
        processor.process_asset(raw, output, _spec())
    assert output.stat().st_size == 0


def test_large_stderr_excerpt_keeps_traceback_and_full_artifact(tmp_path: Path) -> None:
    stderr = ("noise " * 8_000) + "\nTraceback (most recent call last):\nRuntimeError: kept\n"
    runner = _RecordingRunner(
        CommandResult(exit_code=BLENDER_PYTHON_FAILURE_EXIT_CODE, stdout="", stderr=stderr)
    )
    raw, processor = _processor(tmp_path, runner)
    with pytest.raises(DccFailedError, match="inline excerpt") as caught:
        processor.process_asset(raw, tmp_path / "processed.glb", _spec())
    message = str(caught.value)
    assert "RuntimeError: kept" in message
    artifact = Path(str(caught.value.details["diagnostics_artifact"]))
    stored = artifact.read_text(encoding="utf-8")
    assert "RuntimeError: kept" in stored
    assert "noise " * 100 in stored


def test_export_operator_must_finish_and_write_nonempty_file(tmp_path: Path) -> None:
    script = _load_process_script()
    output = tmp_path / "processed.glb"
    output.write_bytes(b"glb")
    script._require_finished_export({"FINISHED"}, output)
    with pytest.raises(RuntimeError, match="expected FINISHED, actual CANCELLED"):
        script._require_finished_export({"CANCELLED"}, output)
    output.write_bytes(b"")
    with pytest.raises(RuntimeError, match="output is empty"):
        script._require_finished_export({"FINISHED"}, output)
    output.unlink()
    with pytest.raises(RuntimeError, match="output is missing"):
        script._require_finished_export({"FINISHED"}, output)
