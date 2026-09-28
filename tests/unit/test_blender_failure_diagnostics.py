"""Blender failure propagation, export status, and output-path regressions."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from gamefactory.adapters.dcc.blender_environment import extract_preflight_result_path
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
    def __init__(
        self,
        result: CommandResult,
        writer: Any = None,
        preflight_result: CommandResult | None = None,
    ) -> None:
        self.result = result
        self.writer = writer
        self.preflight_result = preflight_result
        self.requests: list[CommandRequest] = []
        self.env = {"PATH": "secret-value", "HOME": "/home/runner"}

    def build_env(self, request: CommandRequest) -> dict[str, str]:
        env = dict(self.env)
        env.update(request.env_overrides)
        return env

    def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        if "--python-expr" in request.args:
            res_path = extract_preflight_result_path(request.args)
            if self.preflight_result is not None:
                if (
                    res_path is not None
                    and "GAMEFACTORY_BLENDER_PREFLIGHT=" in self.preflight_result.stdout
                ):
                    for line in self.preflight_result.stdout.splitlines():
                        if line.startswith("GAMEFACTORY_BLENDER_PREFLIGHT="):
                            res_path.write_text(line.split("=", 1)[1], encoding="utf-8")
                            break
                return self.preflight_result
            payload = {
                "blender_version": "4.0.2",
                "python_version": "3.12.3",
                "python_version_info": [3, 12, 3, "final", 0],
                "python_executable": "/usr/bin/python3",
                "python_prefix": "/usr",
                "python_base_prefix": "/usr",
                "sys_path": ["/usr/lib/python3/dist-packages"],
                "runtime_modules": {
                    "ctypes": {
                        "available": True,
                        "version": None,
                        "file": "/usr/lib/python3.12/ctypes/__init__.py",
                        "error": None,
                    }
                },
                "modules": {
                    "numpy": {
                        "available": True,
                        "version": "1.26.4",
                        "file": "/usr/lib/python3/dist-packages/numpy/__init__.py",
                        "error": None,
                    }
                },
            }
            if res_path is not None:
                res_path.write_text(json.dumps(payload), encoding="utf-8")
            return CommandResult(
                exit_code=0,
                stdout="GAMEFACTORY_BLENDER_PREFLIGHT_WRITTEN\nBlender 4.0.2\n",
                stderr="",
            )
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
    args = runner.requests[-1].args
    flag = args.index("--python-exit-code")
    assert args[flag + 1] == str(BLENDER_PYTHON_FAILURE_EXIT_CODE)
    assert args[flag + 2] == "--python"
    assert args.index("--") > flag
    output_arg = args[args.index("--output-glb") + 1]
    assert output_arg == os.path.abspath(output)
    assert Path(output_arg).is_absolute()
    assert "out dir" in output_arg
    assert output_arg.endswith(".glb")
    assert runner.requests[-1].cwd == output.parent
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


def test_env_override_pythonpath_reaches_blender_processing_request_when_configured(
    tmp_path: Path,
) -> None:
    module_dir = tmp_path / "blender_modules"
    module_dir.mkdir()
    runner = _RecordingRunner(CommandResult(exit_code=0, stdout="Blender 4.0.2\n", stderr=""))
    runner.writer = _write_success

    raw = tmp_path / "raw.glb"
    create_box_glb(output_path=raw)
    processor = BlenderAssetProcessor("blender-test", runner, python_paths=[module_dir])  # type: ignore[arg-type]
    output = tmp_path / "processed.glb"
    result = processor.process_asset(raw, output, _spec())

    assert result.exit_code == 0
    processing_req = runner.requests[-1]
    assert "--python" in processing_req.args
    assert "--python-expr" not in processing_req.args
    assert processing_req.env_overrides.get("PYTHONPATH") == str(module_dir.resolve())
    built_env = runner.build_env(processing_req)
    assert built_env.get("PYTHONPATH") == str(module_dir.resolve())


def test_preflight_failure_raises_dcc_failed_and_does_not_execute_processing_command(
    tmp_path: Path,
) -> None:
    bad_preflight = CommandResult(
        exit_code=0,
        stdout="GAMEFACTORY_BLENDER_PREFLIGHT="
        + json.dumps(
            {
                "blender_version": "4.0.2",
                "python_version": "3.12.3",
                "python_version_info": [3, 12, 3, "final", 0],
                "python_executable": "/usr/bin/python3",
                "python_prefix": "/usr",
                "python_base_prefix": "/usr",
                "sys_path": [],
                "runtime_modules": {
                    "ctypes": {
                        "available": True,
                        "version": None,
                        "file": "/usr/lib/python3.12/ctypes/__init__.py",
                        "error": None,
                    }
                },
                "modules": {
                    "numpy": {
                        "available": False,
                        "version": None,
                        "file": None,
                        "error": "No module named 'numpy'",
                    }
                },
            }
        )
        + "\n",
        stderr="",
    )
    runner = _RecordingRunner(
        CommandResult(exit_code=0, stdout="Blender 4.0.2\n", stderr=""),
        preflight_result=bad_preflight,
    )
    raw = tmp_path / "raw.glb"
    create_box_glb(output_path=raw)
    processor = BlenderAssetProcessor("blender-test", runner)  # type: ignore[arg-type]
    output = tmp_path / "processed.glb"
    report = tmp_path / "report.json"
    contract = tmp_path / "report.contract.json"

    with pytest.raises(DccFailedError, match="Blender dependency preflight failed") as caught:
        processor.process_asset(raw, output, _spec(), report_path=report)

    error = caught.value
    assert error.code == "DCC_FAILED"
    assert error.details.get("reason") == "BLENDER_DEPENDENCY_UNAVAILABLE"
    preflight_details = error.details.get("preflight", {})
    assert preflight_details.get("status") == "FAIL"
    assert not contract.exists(), "contract file must not be written if preflight fails"
    assert not output.exists(), "processing must not have run"

    # Verify only the preflight command was executed, not the processing command
    assert len(runner.requests) == 1
    assert "--python-expr" in runner.requests[0].args
    assert "--output-glb" not in runner.requests[0].args


def test_preflight_runs_once_per_processor_instance(tmp_path: Path) -> None:
    runner = _RecordingRunner(CommandResult(exit_code=0, stdout="Blender 4.0.2\n", stderr=""))
    runner.writer = _write_success

    raw = tmp_path / "raw.glb"
    create_box_glb(output_path=raw)
    processor = BlenderAssetProcessor("blender-test", runner)  # type: ignore[arg-type]

    output1 = tmp_path / "out1" / "processed.glb"
    output2 = tmp_path / "out2" / "processed.glb"

    res1 = processor.process_asset(raw, output1, _spec())
    assert res1.exit_code == 0
    # Two requests so far: preflight + processing
    assert len(runner.requests) == 2
    assert "--python-expr" in runner.requests[0].args
    assert "--python-expr" not in runner.requests[1].args

    res2 = processor.process_asset(raw, output2, _spec())
    assert res2.exit_code == 0
    # Three requests total: cached preflight was NOT rerun, only the second processing command
    assert len(runner.requests) == 3
    assert "--python-expr" not in runner.requests[2].args


def test_preflight_runtime_failure_raises_dcc_failed_with_runtime_reason_code_and_skips_processing(
    tmp_path: Path,
) -> None:
    bad_preflight = CommandResult(
        exit_code=0,
        stdout="GAMEFACTORY_BLENDER_PREFLIGHT="
        + json.dumps(
            {
                "blender_version": "4.0.2",
                "python_version": "3.12.3",
                "python_version_info": [3, 12, 3, "final", 0],
                "python_executable": "/usr/bin/python3",
                "python_prefix": "/usr",
                "python_base_prefix": "/usr",
                "sys_path": [],
                "runtime_modules": {
                    "ctypes": {
                        "available": False,
                        "version": None,
                        "file": None,
                        "error": "ImportError: undefined symbol: _PyErr_SetLocaleString",
                    }
                },
                "modules": {
                    "numpy": {
                        "available": True,
                        "version": "1.26.4",
                        "file": "/usr/lib/python3/dist-packages/numpy/__init__.py",
                        "error": None,
                    }
                },
            }
        )
        + "\n",
        stderr="",
    )
    runner = _RecordingRunner(
        CommandResult(exit_code=0, stdout="Blender 4.0.2\n", stderr=""),
        preflight_result=bad_preflight,
    )
    raw = tmp_path / "raw.glb"
    create_box_glb(output_path=raw)
    processor = BlenderAssetProcessor("blender-test", runner)  # type: ignore[arg-type]
    output = tmp_path / "processed.glb"
    report = tmp_path / "report.json"
    contract = tmp_path / "report.contract.json"

    with pytest.raises(DccFailedError, match="Blender dependency preflight failed") as caught:
        processor.process_asset(raw, output, _spec(), report_path=report)

    error = caught.value
    assert error.code == "DCC_FAILED"
    assert error.details.get("reason") == "BLENDER_PYTHON_RUNTIME_UNAVAILABLE"
    preflight_details = error.details.get("preflight", {})
    assert preflight_details.get("status") == "FAIL"
    assert preflight_details.get("reason_code") == "BLENDER_PYTHON_RUNTIME_UNAVAILABLE"
    assert not contract.exists(), "contract file must not be written if preflight fails"
    assert not output.exists(), "processing must not have run"

    # Verify only the preflight command was executed, not the processing command
    assert len(runner.requests) == 1
    assert "--python-expr" in runner.requests[0].args
    assert "--output-glb" not in runner.requests[0].args
    assert "process_asset.py" not in " ".join(runner.requests[0].args)
