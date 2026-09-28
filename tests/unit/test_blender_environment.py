"""Unit tests for Blender Python environment contracts and dependency preflight."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.dcc.blender_environment import (
    BLENDER_PYTHON_FAILURE_EXIT_CODE as ENV_FAILURE_EXIT_CODE,
)
from gamefactory.adapters.dcc.blender_environment import (
    BLENDER_PYTHONPATH_ENV,
    REQUIRED_BLENDER_PYTHON_MODULES,
    BlenderDependencyPreflight,
    blender_env_overrides,
    extract_preflight_result_path,
    format_blender_preflight_failure_message,
    parse_blender_python_paths,
    parse_blender_version,
    run_blender_dependency_preflight,
)
from gamefactory.adapters.dcc.blender_processor import (
    BLENDER_PYTHON_FAILURE_EXIT_CODE as PROCESSOR_FAILURE_EXIT_CODE,
)
from gamefactory.adapters.dcc.blender_processor import (
    BlenderAssetProcessor,
)
from gamefactory.core.execution.process_runner import (
    CommandRequest,
    CommandResult,
    ProcessRunner,
)


def test_constants_and_reexports() -> None:
    assert BLENDER_PYTHONPATH_ENV == "GAMEFACTORY_BLENDER_PYTHONPATH"
    assert REQUIRED_BLENDER_PYTHON_MODULES == ("numpy",)
    assert ENV_FAILURE_EXIT_CODE == 17
    assert PROCESSOR_FAILURE_EXIT_CODE == 17


def test_blender_dependency_preflight_dataclass() -> None:
    preflight = BlenderDependencyPreflight(
        status="PASS",
        executable="blender",
        blender_version="4.0.2",
        python_version="3.12.0",
        python_executable="/usr/bin/python3",
        python_prefix="/usr",
    )
    as_dict = preflight.to_dict()
    assert as_dict["status"] == "PASS"
    assert as_dict["executable"] == "blender"
    assert as_dict["blender_version"] == "4.0.2"


def test_blender_asset_processor_reads_env_pythonpath_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod_dir = tmp_path / "env_mods"
    mod_dir.mkdir()
    monkeypatch.setenv(BLENDER_PYTHONPATH_ENV, str(mod_dir))

    processor = BlenderAssetProcessor("blender-test", runner=ProcessRunner())
    assert processor.python_paths == (mod_dir.resolve(),)


def test_parse_blender_python_paths_none_and_empty() -> None:
    assert parse_blender_python_paths(None) == ()
    assert parse_blender_python_paths("") == ()
    assert parse_blender_python_paths("   ") == ()
    assert parse_blender_python_paths([]) == ()


def test_parse_blender_python_paths_valid_and_dedupe(tmp_path: Path) -> None:
    dir1 = tmp_path / "dir1"
    dir2 = tmp_path / "dir2"
    dir1.mkdir()
    dir2.mkdir()

    raw = os.pathsep.join([str(dir1), str(dir2), str(dir1)])
    parsed = parse_blender_python_paths(raw)
    assert len(parsed) == 2
    assert parsed[0] == dir1.resolve()
    assert parsed[1] == dir2.resolve()


def test_parse_blender_python_paths_strips_whitespace_and_normalizes(tmp_path: Path) -> None:
    dir1 = tmp_path / "dir1"
    dir1.mkdir()
    raw = f"  {dir1}  "
    parsed = parse_blender_python_paths(raw)
    assert parsed == (dir1.resolve(),)


def test_parse_blender_python_paths_rejects_empty_segments(tmp_path: Path) -> None:
    dir1 = tmp_path / "dir1"
    dir1.mkdir()

    with pytest.raises(ValueError, match="Invalid empty segment"):
        parse_blender_python_paths(f"{dir1}{os.pathsep}")

    with pytest.raises(ValueError, match="Invalid empty segment"):
        parse_blender_python_paths(f"{os.pathsep}{dir1}")

    with pytest.raises(ValueError, match="Invalid empty segment"):
        parse_blender_python_paths(f"{dir1}{os.pathsep}{os.pathsep}{dir1}")


def test_parse_blender_python_paths_rejects_relative_path() -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        parse_blender_python_paths("relative/path/to/modules")


def test_parse_blender_python_paths_rejects_nonexistent_path(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent_directory_12345"
    with pytest.raises(ValueError, match="does not exist"):
        parse_blender_python_paths(str(missing))


def test_parse_blender_python_paths_rejects_file_target(tmp_path: Path) -> None:
    file_path = tmp_path / "regular_file.txt"
    file_path.write_text("not a directory")
    with pytest.raises(ValueError, match="not a directory"):
        parse_blender_python_paths(str(file_path))


def test_blender_env_overrides() -> None:
    assert blender_env_overrides(()) == {}
    assert blender_env_overrides([]) == {}

    p1 = Path("/opt/modules/one")
    p2 = Path("/opt/modules/two")
    overrides = blender_env_overrides([p1, p2])
    expected = f"{p1}{os.pathsep}{p2}"
    assert overrides == {"PYTHONPATH": expected}


def test_host_pythonpath_not_inherited_by_minimal_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Monkeypatch host PYTHONPATH and verify ProcessRunner.build_env never leaks it."""
    monkeypatch.setenv("PYTHONPATH", "/host/secret/polluted/path")
    monkeypatch.setenv("PYTHONHOME", "/host/secret/home")

    runner = ProcessRunner(sanitize_output=True)

    # 1. Unconfigured request
    unconfigured_req = CommandRequest(
        args=["blender", "--version"],
        cwd=tmp_path,
        env_overrides=blender_env_overrides(()),
        minimal_env=True,
    )
    env1 = runner.build_env(unconfigured_req)
    assert "PYTHONPATH" not in env1
    assert "PYTHONHOME" not in env1

    # 2. Configured request
    module_dir = tmp_path / "blender_modules"
    module_dir.mkdir()
    configured_req = CommandRequest(
        args=["blender", "--version"],
        cwd=tmp_path,
        env_overrides=blender_env_overrides([module_dir]),
        minimal_env=True,
    )
    env2 = runner.build_env(configured_req)
    assert env2.get("PYTHONPATH") == str(module_dir)
    assert "PYTHONHOME" not in env2


def test_parse_blender_version() -> None:
    assert parse_blender_version("4.0.2") == (4, 0, 2)
    assert parse_blender_version("4.0.2+dfsg-1ubuntu8") == (4, 0, 2)
    assert parse_blender_version("5.2.1 LTS") == (5, 2, 1)
    assert parse_blender_version("3.6") == (3, 6, 0)
    assert parse_blender_version(None) is None
    assert parse_blender_version("") is None
    assert parse_blender_version("invalid") is None


class _FakePreflightRunner:
    def __init__(
        self,
        exit_code: int,
        stdout: str = "",
        stderr: str = "",
        payload: dict[str, Any] | None = None,
        write_file: bool = True,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.payload = payload
        self.write_file = write_file
        self.requests: list[CommandRequest] = []

    def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        if self.write_file and self.exit_code == 0:
            result_path = extract_preflight_result_path(request.args)
            if result_path is not None:
                data_to_write = self.payload
                if data_to_write is None and "GAMEFACTORY_BLENDER_PREFLIGHT=" in self.stdout:
                    for line in self.stdout.splitlines():
                        if line.startswith("GAMEFACTORY_BLENDER_PREFLIGHT="):
                            data_to_write = json.loads(line.split("=", 1)[1])
                            break
                if data_to_write is not None:
                    result_path.write_text(json.dumps(data_to_write), encoding="utf-8")
        return CommandResult(
            exit_code=self.exit_code,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def _preflight_payload(
    blender_version: str = "4.0.2",
    modules: dict[str, Any] | None = None,
    runtime_modules: dict[str, Any] | None = None,
) -> str:
    if modules is None:
        modules = {
            "numpy": {
                "available": True,
                "version": "1.26.4",
                "file": "/usr/lib/python3/dist-packages/numpy/__init__.py",
                "error": None,
            }
        }
    if runtime_modules is None:
        runtime_modules = {
            "ctypes": {
                "available": True,
                "version": None,
                "file": "/usr/lib/python3.12/ctypes/__init__.py",
                "error": None,
            }
        }
    data = {
        "blender_version": blender_version,
        "python_version": "3.12.3",
        "python_version_info": [3, 12, 3, "final", 0],
        "python_executable": "/usr/bin/python3.12",
        "python_prefix": "/usr",
        "python_base_prefix": "/usr",
        "sys_path": ["/usr/lib/python3/dist-packages"],
        "runtime_modules": runtime_modules,
        "modules": modules,
    }
    return f"GAMEFACTORY_BLENDER_PREFLIGHT={json.dumps(data)}\nGAMEFACTORY_BLENDER_PREFLIGHT_WRITTEN\nBlender {blender_version}\n"


def test_preflight_pass_parsing() -> None:
    runner = _FakePreflightRunner(exit_code=0, stdout=_preflight_payload("4.0.2"))
    result = run_blender_dependency_preflight("blender", runner)  # type: ignore[arg-type]
    assert result.status == "PASS"
    assert result.blender_version == "4.0.2"
    assert result.python_version == "3.12.3"
    assert result.python_version_info == [3, 12, 3, "final", 0]
    assert result.python_base_prefix == "/usr"
    assert result.runtime_modules["ctypes"]["available"] is True
    assert result.modules["numpy"]["available"] is True
    assert result.exit_code == 0
    assert result.reason is None
    assert result.reason_code is None


def test_preflight_fails_on_missing_module() -> None:
    payload = _preflight_payload(
        modules={
            "numpy": {
                "available": False,
                "version": None,
                "file": None,
                "error": "No module named numpy",
            }
        }
    )
    runner = _FakePreflightRunner(exit_code=0, stdout=payload)
    result = run_blender_dependency_preflight("blender", runner)  # type: ignore[arg-type]
    assert result.status == "FAIL"
    assert result.reason_code == "BLENDER_DEPENDENCY_UNAVAILABLE"
    assert "numpy" in (result.reason or "")
    msg = format_blender_preflight_failure_message(result)
    assert "blender_executable: blender" in msg
    assert "blender_version: 4.0.2" in msg
    assert "missing_modules: numpy" in msg
    assert "python_version: 3.12.3" in msg
    assert "configured_python_paths:" in msg
    assert "hint:" in msg


def test_preflight_fails_on_missing_runtime_module_even_when_numpy_available() -> None:
    payload = _preflight_payload(
        runtime_modules={
            "ctypes": {
                "available": False,
                "version": None,
                "file": None,
                "error": "ImportError: /usr/lib/python3.12/lib-dynload/_ctypes.cpython-312-x86_64-linux-gnu.so: undefined symbol: _PyErr_SetLocaleString",
            }
        },
        modules={
            "numpy": {
                "available": True,
                "version": "1.26.4",
                "file": "/usr/lib/python3/dist-packages/numpy/__init__.py",
                "error": None,
            }
        },
    )
    runner = _FakePreflightRunner(exit_code=0, stdout=payload)
    result = run_blender_dependency_preflight("blender", runner)  # type: ignore[arg-type]
    assert result.status == "FAIL"
    assert result.reason_code == "BLENDER_PYTHON_RUNTIME_UNAVAILABLE"
    assert "ctypes" in (result.reason or "")
    assert "_PyErr_SetLocaleString" in (result.reason or "")
    assert result.modules["numpy"]["available"] is True


def test_preflight_evaluates_runtime_check_before_dependencies() -> None:
    payload = _preflight_payload(
        runtime_modules={
            "ctypes": {
                "available": False,
                "version": None,
                "file": None,
                "error": "ImportError: ctypes broken",
            }
        },
        modules={
            "numpy": {
                "available": False,
                "version": None,
                "file": None,
                "error": "No module named numpy",
            }
        },
    )
    runner = _FakePreflightRunner(exit_code=0, stdout=payload)
    result = run_blender_dependency_preflight("blender", runner)  # type: ignore[arg-type]
    assert result.status == "FAIL"
    assert result.reason_code == "BLENDER_PYTHON_RUNTIME_UNAVAILABLE"
    assert "ctypes" in (result.reason or "")


def test_preflight_failure_message_contents_for_runtime_and_dependency_errors() -> None:
    # Runtime integrity error message formatting
    payload_runtime = _preflight_payload(
        runtime_modules={
            "ctypes": {
                "available": False,
                "version": None,
                "file": None,
                "error": "ImportError: broken ctypes",
            }
        }
    )
    runner = _FakePreflightRunner(exit_code=0, stdout=payload_runtime)
    res_runtime = run_blender_dependency_preflight("blender", runner)  # type: ignore[arg-type]
    msg_runtime = format_blender_preflight_failure_message(res_runtime)
    assert "failed_runtime_modules: ctypes" in msg_runtime
    assert "python_prefix: /usr" in msg_runtime
    assert "python_base_prefix: /usr" in msg_runtime
    assert "python_executable: /usr/bin/python3.12" in msg_runtime
    assert "failure_reason_code: BLENDER_PYTHON_RUNTIME_UNAVAILABLE" in msg_runtime
    assert "Blender's embedded Python resolved a stdlib prefix" in msg_runtime

    # Dependency error message formatting
    payload_dep = _preflight_payload(
        modules={
            "numpy": {
                "available": False,
                "version": None,
                "file": None,
                "error": "No module named numpy",
            }
        }
    )
    runner_dep = _FakePreflightRunner(exit_code=0, stdout=payload_dep)
    res_dep = run_blender_dependency_preflight("blender", runner_dep)  # type: ignore[arg-type]
    msg_dep = format_blender_preflight_failure_message(res_dep)
    assert "missing_modules: numpy" in msg_dep
    assert "failure_reason_code: BLENDER_DEPENDENCY_UNAVAILABLE" in msg_dep
    assert "Ensure required Python modules are installed" in msg_dep


def test_preflight_fails_on_nonzero_exit() -> None:
    runner = _FakePreflightRunner(exit_code=17, stdout="", stderr="Error: something crashed")
    result = run_blender_dependency_preflight("blender", runner)  # type: ignore[arg-type]
    assert result.status == "FAIL"
    assert result.exit_code == 17
    assert result.reason_code == "BLENDER_PREFLIGHT_FAILED"
    assert "exited with code 17" in (result.reason or "")


def test_preflight_fails_on_missing_result_file() -> None:
    runner = _FakePreflightRunner(
        exit_code=0, stdout="Blender 4.0.2\nBlender quit\n", write_file=False
    )
    result = run_blender_dependency_preflight("blender", runner)  # type: ignore[arg-type]
    assert result.status == "FAIL"
    assert result.reason_code == "BLENDER_PREFLIGHT_FAILED"
    assert "not found" in (result.reason or "")


def test_preflight_fails_on_invalid_json() -> None:
    class _CorruptRunner:
        def run(self, request: CommandRequest) -> CommandResult:
            path = extract_preflight_result_path(request.args)
            if path is not None:
                path.write_text("invalid json {", encoding="utf-8")
            return CommandResult(exit_code=0, stdout="", stderr="")

    result = run_blender_dependency_preflight("blender", _CorruptRunner())  # type: ignore[arg-type]
    assert result.status == "FAIL"
    assert result.reason_code == "BLENDER_PREFLIGHT_FAILED"
    assert "Failed to parse preflight JSON" in (result.reason or "")


def test_preflight_fails_on_version_3_6() -> None:
    runner = _FakePreflightRunner(exit_code=0, stdout=_preflight_payload("3.6.0"))
    result = run_blender_dependency_preflight("blender", runner)  # type: ignore[arg-type]
    assert result.status == "FAIL"
    assert result.reason_code == "BLENDER_VERSION_UNSUPPORTED"
    assert "below minimum supported version 4.0.2" in (result.reason or "")


def test_preflight_passes_on_version_4_0_2_and_newer() -> None:
    runner_402 = _FakePreflightRunner(exit_code=0, stdout=_preflight_payload("4.0.2"))
    assert run_blender_dependency_preflight("blender", runner_402).status == "PASS"  # type: ignore[arg-type]

    runner_521 = _FakePreflightRunner(exit_code=0, stdout=_preflight_payload("5.2.1 LTS"))
    assert run_blender_dependency_preflight("blender", runner_521).status == "PASS"  # type: ignore[arg-type]


class _PythonPreflightDelegatingRunner:
    """Delegates to real ProcessRunner by rewriting the command to python -c <expr>."""

    def __init__(self, real_runner: ProcessRunner) -> None:
        self.real_runner = real_runner

    def run(self, request: CommandRequest) -> CommandResult:
        # Args: [exe, "--background", "--factory-startup", "--python-exit-code", "17", "--python-expr", <code>, "--", "--result", <path>]
        expr_index = request.args.index("--python-expr")
        code = request.args[expr_index + 1]
        trailing_args = list(request.args[expr_index + 2 :])
        rewritten_cmd = [sys.executable, "-c", code, *trailing_args]
        new_req = CommandRequest(
            args=rewritten_cmd,
            cwd=request.cwd,
            env_overrides=request.env_overrides,
            timeout_seconds=request.timeout_seconds,
            minimal_env=request.minimal_env,
        )
        return self.real_runner.run(new_req)


def test_preflight_real_process_runner_negative_and_positive_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Process-level regression using REAL ProcessRunner without real Blender or numpy.

    Proves host PYTHONPATH does not leak to the probe and explicit python_paths resolves
    the required module inside the probe.
    """
    fake_mod_dir = tmp_path / "fake_module_dir"
    fake_mod_dir.mkdir()
    mod_file = fake_mod_dir / "gf_preflight_probe_dep.py"
    mod_file.write_text("__version__ = '0.9.9'\n", encoding="utf-8")

    # Set host PYTHONPATH to fake_mod_dir to verify it is NOT leaked
    monkeypatch.setenv("PYTHONPATH", str(fake_mod_dir))

    delegating_runner = _PythonPreflightDelegatingRunner(ProcessRunner(sanitize_output=True))

    # Negative case: With path omitted, probe cannot import gf_preflight_probe_dep -> FAIL
    res_neg = run_blender_dependency_preflight(
        blender_executable="dummy-blender",
        runner=delegating_runner,  # type: ignore[arg-type]
        python_paths=(),
        required_modules=("gf_preflight_probe_dep",),
        check_version=False,
    )
    assert res_neg.status == "FAIL"
    assert res_neg.reason_code == "BLENDER_DEPENDENCY_UNAVAILABLE"
    assert res_neg.modules["gf_preflight_probe_dep"]["available"] is False

    # Positive case: With explicit path configured, probe imports module -> PASS
    res_pos = run_blender_dependency_preflight(
        blender_executable="dummy-blender",
        runner=delegating_runner,  # type: ignore[arg-type]
        python_paths=(fake_mod_dir,),
        required_modules=("gf_preflight_probe_dep",),
        check_version=False,
    )
    assert res_pos.status == "PASS"
    assert res_pos.reason_code is None
    assert res_pos.modules["gf_preflight_probe_dep"]["available"] is True
    assert res_pos.modules["gf_preflight_probe_dep"]["version"] == "0.9.9"


def test_preflight_real_process_runner_runtime_integrity_regression(
    tmp_path: Path,
) -> None:
    """Process-level regression for runtime integrity using REAL ProcessRunner.

    Tests that runtime integrity failure returns BLENDER_PYTHON_RUNTIME_UNAVAILABLE.
    """
    delegating_runner = _PythonPreflightDelegatingRunner(ProcessRunner(sanitize_output=True))

    # Negative case: Synthetic missing runtime module
    res_neg = run_blender_dependency_preflight(
        blender_executable="dummy-blender",
        runner=delegating_runner,  # type: ignore[arg-type]
        python_paths=(),
        required_modules=(),
        runtime_modules=("synthetic_missing_runtime_mod",),
        check_version=False,
    )
    assert res_neg.status == "FAIL"
    assert res_neg.reason_code == "BLENDER_PYTHON_RUNTIME_UNAVAILABLE"
    assert res_neg.runtime_modules["synthetic_missing_runtime_mod"]["available"] is False

    # Negative case: Broken runtime module raising ImportError
    broken_dir = tmp_path / "broken_runtime_dir"
    broken_dir.mkdir()
    broken_mod = broken_dir / "synthetic_broken_runtime.py"
    broken_mod.write_text(
        "raise ImportError('simulated undefined symbol: _PyErr_SetLocaleString')\n",
        encoding="utf-8",
    )

    res_broken = run_blender_dependency_preflight(
        blender_executable="dummy-blender",
        runner=delegating_runner,  # type: ignore[arg-type]
        python_paths=(broken_dir,),
        required_modules=(),
        runtime_modules=("synthetic_broken_runtime",),
        check_version=False,
    )
    assert res_broken.status == "FAIL"
    assert res_broken.reason_code == "BLENDER_PYTHON_RUNTIME_UNAVAILABLE"
    assert "_PyErr_SetLocaleString" in (res_broken.reason or "")

    # Positive case: standard ctypes module in runner Python runtime
    res_pos = run_blender_dependency_preflight(
        blender_executable="dummy-blender",
        runner=delegating_runner,  # type: ignore[arg-type]
        python_paths=(),
        required_modules=(),
        runtime_modules=("ctypes",),
        check_version=False,
    )
    assert res_pos.status == "PASS"
    assert res_pos.reason_code is None
    assert res_pos.runtime_modules["ctypes"]["available"] is True


def test_preflight_fails_when_requested_module_absent_from_probe_payload() -> None:
    """A probe result that omits a requested module must not pass silently."""
    runner = _FakePreflightRunner(exit_code=0, stdout=_preflight_payload(runtime_modules={}))
    result = run_blender_dependency_preflight("blender", runner)  # type: ignore[arg-type]
    assert result.status == "FAIL"
    assert result.reason_code == "BLENDER_PYTHON_RUNTIME_UNAVAILABLE"
    assert "ctypes" in (result.reason or "")

    runner_dep = _FakePreflightRunner(exit_code=0, stdout=_preflight_payload(modules={}))
    result_dep = run_blender_dependency_preflight("blender", runner_dep)  # type: ignore[arg-type]
    assert result_dep.status == "FAIL"
    assert result_dep.reason_code == "BLENDER_DEPENDENCY_UNAVAILABLE"
    assert "numpy" in (result_dep.reason or "")
