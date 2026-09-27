"""Black-box live-process crash and recovery checks for real Godot execution.

Proves that when the Factory orchestrator is force-killed while Godot is STILL LIVE:
1. Godot process identity is verified with OS handle, PID, creation identity, and executable path.
2. No terminal receipt exists before the orchestrator is killed.
3. Child fate is recorded (observed terminated vs observed alive).
4. Unrelated sentinel child processes survive the orchestrator termination and remain alive
   through subsequent recovery attempts.
5. New CLI processes (inspect, resume, retry) safely handle the interrupted attempt:
   - Repeated resume detects unmatched intent and remains safely BLOCKED (exit 3).
   - Retry rejects re-execution of an uncertain attempt (exit 1).
   - No duplicate launches occur (verified via intercepted subprocess launch evidence and
     append-only runtime journal).
   - Historical attempt identity, artifacts, and logs are retained.
   - Quality gates remain closed (no gate pass).
6. The test fixture terminates independently even if the supervisor fails.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_TERMINATE = 0x0001
    SYNCHRONIZE = 0x00100000
    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102

    class FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    # Centralized Win32 prototypes preventing 64-bit HANDLE truncation
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL

    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD

    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL


def _win32_close_handle(handle: Any) -> None:
    """Safely close a Win32 HANDLE once."""
    if sys.platform == "win32" and handle:
        try:
            kernel32.CloseHandle(handle)
        except Exception:
            pass


class CheckFailure(AssertionError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _json_file(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CheckFailure(f"Expected JSON object in {path}")
    return value


def _hashes(root: Path) -> dict[str, str]:
    ignored = {".gamefactory", ".godot", ".git", "__pycache__"}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not ignored.intersection(path.relative_to(root).parts)
    }


# ---------------------------------------------------------------------------
# OS-level Process Identity Probing
# ---------------------------------------------------------------------------


def _get_process_identity(pid: int, expected_exe: Path) -> tuple[dict[str, Any], Any]:
    """Prove process identity and liveness using OS APIs.

    Returns (metadata_dict, process_handle_or_none).
    On Windows, the open process handle (with terminate and synchronize rights) is returned
    so caller can observe post-kill exit and perform cleanup without reopening PID.
    """
    if sys.platform == "win32":
        h_proc = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE | PROCESS_TERMINATE,
            False,
            pid,
        )
        if not h_proc:
            err = ctypes.get_last_error()
            raise CheckFailure(f"Failed to open handle to Godot PID {pid}: Windows error {err}")

        owned_by_caller = False
        try:
            creation = FILETIME()
            exit_time = FILETIME()
            kernel_time = FILETIME()
            user_time = FILETIME()
            if not kernel32.GetProcessTimes(
                h_proc,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                err = ctypes.get_last_error()
                raise CheckFailure(
                    f"Failed to get process times for PID {pid}: Windows error {err}"
                )

            creation_filetime = (creation.dwHighDateTime << 32) | creation.dwLowDateTime

            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                err = ctypes.get_last_error()
                raise CheckFailure(f"Failed to query image name for PID {pid}: Windows error {err}")

            exe_path = Path(buf.value).resolve()
            expected_resolved = expected_exe.resolve()
            expected_candidates = {
                expected_resolved,
                expected_resolved.with_name(
                    expected_resolved.stem.removesuffix("_console") + expected_resolved.suffix
                ),
                expected_resolved.with_name(
                    expected_resolved.stem + "_console" + expected_resolved.suffix
                ),
            }
            if exe_path not in expected_candidates:
                raise CheckFailure(
                    f"Process PID {pid} executable mismatch: found '{exe_path}', expected one of {expected_candidates}"
                )

            wait_res = kernel32.WaitForSingleObject(h_proc, 0)
            if wait_res != WAIT_TIMEOUT:
                raise CheckFailure(
                    f"Process PID {pid} was not alive (WaitForSingleObject returned {wait_res})"
                )

            owned_by_caller = True
            return {
                "pid": pid,
                "creation_filetime": creation_filetime,
                "exe_path": str(exe_path),
                "is_alive": True,
                "platform": "win32",
            }, h_proc
        finally:
            if not owned_by_caller:
                _win32_close_handle(h_proc)
    else:
        stat_file = Path(f"/proc/{pid}/stat")
        if not stat_file.is_file():
            raise CheckFailure(f"Process PID {pid} not found in /proc")

        try:
            os.kill(pid, 0)
        except ProcessLookupError as err:
            raise CheckFailure(f"Process PID {pid} is not alive") from err

        stat_text = stat_file.read_text(encoding="utf-8")
        paren_idx = stat_text.rfind(")")
        if paren_idx == -1:
            raise CheckFailure(f"Malformed /proc/{pid}/stat: missing closing paren")
        fields = stat_text[paren_idx + 1 :].split()
        if len(fields) < 20:
            raise CheckFailure(f"Malformed /proc/{pid}/stat: insufficient fields")

        proc_state = fields[0]
        if proc_state == "Z":
            raise CheckFailure(f"Process PID {pid} is a zombie")

        starttime = fields[19]

        try:
            raw_link = os.readlink(f"/proc/{pid}/exe")
        except OSError as err:
            raise CheckFailure(f"Failed to readlink /proc/{pid}/exe: {err}") from err

        exe_path = Path(raw_link).resolve()
        expected_resolved = expected_exe.resolve()
        expected_candidates = {
            expected_resolved,
            expected_resolved.with_name(
                expected_resolved.stem.removesuffix("_console") + expected_resolved.suffix
            ),
        }
        if exe_path not in expected_candidates:
            raise CheckFailure(
                f"Process PID {pid} executable mismatch: found '{exe_path}', expected one of {expected_candidates}"
            )

        cmdline_file = Path(f"/proc/{pid}/cmdline")
        cmdline = cmdline_file.read_bytes().split(b"\x00") if cmdline_file.is_file() else []

        return {
            "pid": pid,
            "starttime": starttime,
            "proc_state": proc_state,
            "exe_path": str(exe_path),
            "cmdline": [c.decode("utf-8", errors="replace") for c in cmdline if c],
            "is_alive": True,
            "platform": "posix",
        }, None


def _require_terminal_posix_reason(reason: str) -> None:
    """Unknown ownership or unreadable proc metadata is not proof of cleanup."""
    if reason not in {"not_found", "zombie"}:
        raise CheckFailure(f"Cannot confirm owned POSIX child stopped: {reason}")


def _revalidate_posix_child(
    pid: int, expected_exe: Path, expected_starttime: str
) -> tuple[bool, str]:
    """Revalidate child identity and liveness on POSIX using starttime and exe readlink.

    Never kill based on PID alone.
    """
    stat_file = Path(f"/proc/{pid}/stat")
    if not stat_file.is_file():
        return False, "not_found"
    try:
        stat_text = stat_file.read_text(encoding="utf-8")
        paren_idx = stat_text.rfind(")")
        if paren_idx == -1:
            return False, "malformed_stat"
        fields = stat_text[paren_idx + 1 :].split()
        if len(fields) < 20:
            return False, "insufficient_fields"
        if fields[0] == "Z":
            return False, "zombie"
        if fields[19] != expected_starttime:
            return False, f"starttime_mismatch: {fields[19]} != {expected_starttime}"

        raw_link = os.readlink(f"/proc/{pid}/exe")
        exe = Path(raw_link).resolve()
        expected_resolved = expected_exe.resolve()
        expected_candidates = {
            expected_resolved,
            expected_resolved.with_name(
                expected_resolved.stem.removesuffix("_console") + expected_resolved.suffix
            ),
        }
        if exe not in expected_candidates:
            return False, f"exe_mismatch: {exe} not in {expected_candidates}"

        return True, "valid"
    except OSError as err:
        return False, f"os_error: {err}"


# ---------------------------------------------------------------------------
# Intercepting Subprocess Instrumentation (Launch Logger)
# ---------------------------------------------------------------------------

_original_popen = subprocess.Popen


def _install_popen_interceptor(launch_log_path: Path) -> None:
    """Intercept subprocess.Popen across all engine and helper launches.

    Appends actual PID, argv, and creation time launch evidence to the append-only journal.
    """

    class InterceptingPopen(_original_popen):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            cmd = args[0] if args else kwargs.get("args")
            cwd = kwargs.get("cwd", os.getcwd())
            start_iso = _now()
            super().__init__(*args, **kwargs)
            child_pid = self.pid
            create_time: Any = None
            if sys.platform == "win32" and hasattr(self, "_handle") and self._handle:
                try:
                    creation = FILETIME()
                    exit_time = FILETIME()
                    kernel_time = FILETIME()
                    user_time = FILETIME()
                    if kernel32.GetProcessTimes(
                        wintypes.HANDLE(int(self._handle)),
                        ctypes.byref(creation),
                        ctypes.byref(exit_time),
                        ctypes.byref(kernel_time),
                        ctypes.byref(user_time),
                    ):
                        create_time = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                except Exception:
                    pass
            elif sys.platform != "win32":
                try:
                    stat_text = Path(f"/proc/{child_pid}/stat").read_text(encoding="utf-8")
                    paren_idx = stat_text.rfind(")")
                    if paren_idx != -1:
                        fields = stat_text[paren_idx + 1 :].split()
                        if len(fields) > 19:
                            create_time = fields[19]
                except Exception:
                    pass

            cmd_list = cmd if isinstance(cmd, (list, tuple)) else [str(cmd)]
            cmd_str = [str(x) for x in cmd_list]
            is_runtime = any("--script" in x or "res://" in x for x in cmd_str)

            _append_jsonl(
                launch_log_path,
                {
                    "event": "subprocess_launch",
                    "pid": child_pid,
                    "argv": cmd_str,
                    "cwd": str(cwd),
                    "create_time": create_time,
                    "is_runtime": is_runtime,
                    "parent_pid": os.getpid(),
                    "started_at": start_iso,
                },
            )

    subprocess.Popen = InterceptingPopen  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Sentinel Process Handling
# ---------------------------------------------------------------------------


def _spawn_sentinel(python_exe: Path) -> subprocess.Popen[bytes]:
    """Spawn an unrelated background process that must survive the Factory kill."""
    script = "import time; time.sleep(180)"
    return subprocess.Popen(
        [str(python_exe), "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )


def _check_sentinel_alive(sentinel: subprocess.Popen[bytes] | None) -> bool:
    if sentinel is None:
        return False
    return sentinel.poll() is None


def _cleanup_sentinel(sentinel: subprocess.Popen[bytes] | None) -> None:
    if sentinel is None:
        return
    try:
        sentinel.kill()
        sentinel.wait(timeout=5)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Controlled Fixture Preparation
# ---------------------------------------------------------------------------

_CONTROLLED_MAIN_GD = """extends Node

var _player_hp: int = 100
var _enemies_remaining: int = 3
var _score: int = 0
var _snapshot_buffer: Dictionary = {}
var _handshake_done: bool = false


func apply_damage(amount: int) -> void:
\t_player_hp = maxi(0, _player_hp - amount)


func defeat_enemy() -> void:
\tif _enemies_remaining > 0:
\t\t_enemies_remaining -= 1
\t\t_score += 100


func _perform_handshake() -> void:
\tif _handshake_done:
\t\treturn
\t_handshake_done = true

\tif not FileAccess.file_exists("res://handshake_config.json"):
\t\treturn

\tvar cfg_file := FileAccess.open("res://handshake_config.json", FileAccess.READ)
\tif cfg_file == null:
\t\treturn
\tvar text := cfg_file.get_as_text()
\tcfg_file.close()

\tvar cfg: Variant = JSON.parse_string(text)
\tif typeof(cfg) != TYPE_DICTIONARY:
\t\treturn

\tvar journal_file: String = cfg.get("journal_file", "")
\tvar ready_file: String = cfg.get("ready_file", "")
\tvar release_file: String = cfg.get("release_file", "")
\tvar token: String = str(cfg.get("token", ""))
\tvar max_wait_ms: int = int(cfg.get("max_wait_ms", 25000))

\t# Append-only runtime startup journal entry
\tif not journal_file.is_empty():
\t\tvar j_file: FileAccess
\t\tif FileAccess.file_exists(journal_file):
\t\t\tj_file = FileAccess.open(journal_file, FileAccess.READ_WRITE)
\t\t\tif j_file != null:
\t\t\t\tj_file.seek_end()
\t\telse:
\t\t\tj_file = FileAccess.open(journal_file, FileAccess.WRITE)
\t\tif j_file != null:
\t\t\tj_file.store_line(JSON.stringify({
\t\t\t\t"pid": OS.get_process_id(),
\t\t\t\t"token": token,
\t\t\t\t"status": "LIVE",
\t\t\t\t"time_msec": Time.get_ticks_msec(),
\t\t\t}))
\t\t\tj_file.flush()
\t\t\tj_file.close()

\t# Also write ready_file if specified and not yet present
\tif not ready_file.is_empty() and not FileAccess.file_exists(ready_file):
\t\tvar out := FileAccess.open(ready_file, FileAccess.WRITE)
\t\tif out != null:
\t\t\tout.store_string(JSON.stringify({
\t\t\t\t"pid": OS.get_process_id(),
\t\t\t\t"token": token,
\t\t\t\t"status": "LIVE",
\t\t\t\t"time_msec": Time.get_ticks_msec(),
\t\t\t}))
\t\t\tout.flush()
\t\t\tout.close()

\t# Bounded progress wait loop: terminates independently even if supervisor fails
\tif not release_file.is_empty():
\t\tvar deadline := Time.get_ticks_msec() + max_wait_ms
\t\twhile Time.get_ticks_msec() < deadline:
\t\t\tif FileAccess.file_exists(release_file):
\t\t\t\tbreak
\t\t\tOS.delay_msec(20)


func verification_snapshot() -> Dictionary:
\t_perform_handshake()
\t_snapshot_buffer["player_hp"] = _player_hp
\t_snapshot_buffer["enemies_remaining"] = _enemies_remaining
\t_snapshot_buffer["score"] = _score
\treturn _snapshot_buffer
"""


def _prepare_controlled_fixture(
    source_fixture: Path,
    dest_project: Path,
    ready_file: Path,
    release_file: Path,
    journal_file: Path | None = None,
    token: str = "",
    max_wait_ms: int = 25000,
) -> None:
    dest_project.mkdir(parents=True, exist_ok=True)
    import shutil

    ignored = shutil.ignore_patterns(".godot", ".gamefactory", ".git", "__pycache__")
    shutil.copytree(source_fixture, dest_project, dirs_exist_ok=True, ignore=ignored)

    if journal_file is None:
        journal_file = ready_file.with_name("fixture-runtime-journal.jsonl")
    if not token:
        token = uuid.uuid4().hex

    handshake_config = {
        "journal_file": journal_file.as_posix(),
        "ready_file": ready_file.as_posix(),
        "release_file": release_file.as_posix(),
        "token": token,
        "max_wait_ms": max_wait_ms,
    }
    _write_json(dest_project / "handshake_config.json", handshake_config)
    (dest_project / "main.gd").write_text(_CONTROLLED_MAIN_GD, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# CLI Command Execution via Test-only Entry Wrapper
# ---------------------------------------------------------------------------


def _cli(
    records: list[dict[str, Any]],
    cli: Path,
    python: Path,
    project: Path,
    godot: Path,
    *parts: str,
    launch_log: Path,
    deadline: float,
    expected: set[int] | None = None,
    timeout: float = 60.0,
) -> Any:
    if expected is None:
        expected = {0}

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CheckFailure("Overall execution deadline exceeded prior to CLI command")
    actual_timeout = min(remaining, timeout)

    # Use test-only Python entry wrapper to invoke installed CLI main in a fresh process
    wrapper_argv = [
        str(python),
        str(Path(__file__).resolve()),
        "--entry-wrapper",
        "--launch-log",
        str(launch_log),
        "--cli-exe",
        str(cli),
        "--",
        "--json",
        "--project",
        str(project),
        "--godot-path",
        str(godot),
        *parts,
    ]
    start = _now()
    try:
        proc = subprocess.run(
            wrapper_argv,
            cwd=project,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=actual_timeout,
            shell=False,
        )
        timed_out = False
        returncode = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = -1
        stdout = (
            exc.stdout.decode("utf-8", errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode("utf-8", errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )

    end = _now()
    item: dict[str, Any] = {
        "argv": wrapper_argv,
        "python": str(python),
        "installed_cli": str(cli),
        "installed_cli_entrypoint": "gamefactory.cli.main:main",
        "cwd": str(project),
        "started_at": start,
        "completed_at": end,
        "exit_code": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
    }
    try:
        item["json"] = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        pass
    records.append(item)

    if timed_out:
        raise CheckFailure(f"CLI command timed out after {actual_timeout:.1f}s: {wrapper_argv}")
    if returncode not in expected:
        raise CheckFailure(
            f"CLI exit {returncode}, expected {sorted(expected)}:\nSTDOUT: {stdout}\nSTDERR: {stderr}"
        )
    return item.get("json")


def _inspect(
    records: list[dict[str, Any]],
    cli: Path,
    python: Path,
    project: Path,
    godot: Path,
    workflow_id: str,
    launch_log: Path,
    deadline: float,
) -> dict[str, Any]:
    value = _cli(
        records,
        cli,
        python,
        project,
        godot,
        "inspect",
        workflow_id,
        launch_log=launch_log,
        deadline=deadline,
    )
    if not isinstance(value, dict):
        raise CheckFailure(f"inspect did not return a JSON object: {value}")
    return value


def _artifacts(
    records: list[dict[str, Any]],
    cli: Path,
    python: Path,
    project: Path,
    godot: Path,
    workflow_id: str,
    launch_log: Path,
    deadline: float,
) -> list[dict[str, Any]]:
    value = _cli(
        records,
        cli,
        python,
        project,
        godot,
        "artifacts",
        "--workflow",
        workflow_id,
        launch_log=launch_log,
        deadline=deadline,
    )
    value = (
        value
        if isinstance(value, list)
        else value.get("artifacts", [])
        if isinstance(value, dict)
        else []
    )
    if not isinstance(value, list):
        raise CheckFailure("artifacts did not return a JSON array")
    return [item for item in value if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# Test-only Entry Wrapper & Factory Helper Processes
# ---------------------------------------------------------------------------


def _entry_wrapper(args: argparse.Namespace, remaining: list[str]) -> int:
    """Test-only Python entry wrapper that intercepts subprocess.Popen and calls installed CLI main."""
    launch_log = args.launch_log
    _install_popen_interceptor(launch_log)

    if args.wrapper_probe:
        _append_jsonl(
            launch_log,
            {
                "event": "wrapper_invocation_start",
                "mode": "probe",
                "pid": os.getpid(),
                "started_at": _now(),
            },
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=10)
        _append_jsonl(
            launch_log,
            {
                "event": "wrapper_invocation_end",
                "mode": "probe",
                "pid": os.getpid(),
                "exit_code": proc.returncode,
                "completed_at": _now(),
            },
        )
        return proc.returncode

    _append_jsonl(
        launch_log,
        {
            "event": "wrapper_invocation_start",
            "pid": os.getpid(),
            "cli_exe": str(args.cli_exe),
            "cli_args": remaining,
            "started_at": _now(),
        },
    )

    package_file = getattr(__import__("gamefactory"), "__file__", None)
    if not isinstance(package_file, str):
        raise RuntimeError("Could not locate the installed gamefactory package")
    package_path = Path(package_file).resolve()
    source_tree = (Path(__file__).resolve().parents[1] / "src").resolve()
    if package_path.is_relative_to(source_tree):
        raise RuntimeError(
            f"Wrapper imported checkout source instead of installed package: {package_path}"
        )

    from gamefactory.cli.main import main as cli_main

    sys.argv = [str(args.cli_exe), *remaining]
    exit_code = cli_main(remaining)
    _append_jsonl(
        launch_log,
        {
            "event": "wrapper_invocation_end",
            "pid": os.getpid(),
            "exit_code": exit_code,
            "completed_at": _now(),
        },
    )
    return exit_code


def _factory_helper(args: argparse.Namespace) -> int:
    """Run real Factory workflow in separate process using installed package and real ProcessRunner."""
    package_file = getattr(__import__("gamefactory"), "__file__", None)
    if not isinstance(package_file, str):
        raise RuntimeError("Could not locate the installed gamefactory package")
    package_path = Path(package_file).resolve()
    source_tree = (Path(__file__).resolve().parents[1] / "src").resolve()
    if package_path.is_relative_to(source_tree):
        raise RuntimeError(
            f"Factory helper imported checkout source instead of installed package: {package_path}"
        )

    project = args.project.resolve(strict=True)
    marker = Path(str(args.marker))
    ready_marker = marker.with_name("helper-ready.json")
    launch_log_path = marker.with_name("launch-observations.jsonl")

    # Install the same Popen interceptor inside the helper process
    _install_popen_interceptor(launch_log_path)

    _write_json(
        ready_marker,
        {
            "package_path": str(package_path),
            "pid": os.getpid(),
            "started_at": _now(),
        },
    )

    from gamefactory.adapters.persistence.database import Database
    from gamefactory.adapters.persistence.migrations import MigrationRunner
    from gamefactory.config.loader import ConfigLoader
    from gamefactory.core.execution.process_runner import ProcessRunner
    from gamefactory.core.policies.policy_engine import PolicyEngine, PolicyRule
    from gamefactory.workflows.engine import WorkflowEngine
    from gamefactory.workflows.godot_verification import (
        create_godot_verification_workflow,
        register_godot_handlers,
    )

    db = Database(project / ".gamefactory" / "state" / "factory.db")
    migrations = MigrationRunner(db)
    with db.transaction() as conn:
        migrations.init_migration_table(conn)
    migrations.apply_all()

    cfg = ConfigLoader.load_config(project)
    p = cfg.policies
    policy = PolicyEngine(
        PolicyRule(
            require_approval_for_paid=p.paid_operations_require_approval,
            require_approval_for_destructive=p.destructive_operations_require_approval,
            require_approval_for_repo_write=p.require_approval_for_repo_write,
            require_approval_for_process_execution=p.require_approval_for_process_execution,
            max_operation_cost=p.max_operation_cost,
            project_budget=p.project_budget,
        )
    )

    runner = ProcessRunner(sanitize_output=True)
    engine = WorkflowEngine(project, db, policy_engine=policy, process_runner=runner)
    register_godot_handlers(
        engine.handler_registry,
        project,
        engine.artifact_mgr,
        engine.art_repo,
        engine.exec_repo,
        engine.evi_repo,
        engine.gate_repo,
        runner,
    )

    workflow, tasks = create_godot_verification_workflow(
        cfg.project.id,
        project,
        args.godot,
        args.scenario,
    )
    engine.register_workflow(workflow, tasks)
    _write_json(
        args.workflow_file,
        {
            "workflow_id": workflow.id,
            "task_ids": [task.id for task in tasks],
            "execute_task_id": tasks[0].id,
            "helper_pid": os.getpid(),
        },
    )
    # The workflow stages the fixture and runs version, help, import, and runtime.
    # When runtime starts, Godot will write journal_file / ready_file and enter the bounded wait loop.
    # The supervisor will force-kill this helper process while Godot is still live.
    engine.run_workflow(workflow.id)
    return 0


# ---------------------------------------------------------------------------
# Main Supervisor Verification Logic
# ---------------------------------------------------------------------------


def _main(args: argparse.Namespace) -> int:
    cli, python, fixture = (
        p.expanduser().resolve(strict=True) for p in (args.cli, args.python, args.fixture)
    )

    if args.godot:
        godot = args.godot.expanduser().resolve(strict=True)
    else:
        # Fallback to path recorded in closeout installation report
        install_report = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "reports"
            / "v0.2-closeout"
            / "installation.json"
        )
        if not install_report.is_file():
            raise CheckFailure(
                f"--godot was not provided and installation report not found: {install_report}"
            )
        data = json.loads(install_report.read_text(encoding="utf-8"))
        candidate = data.get("godot")
        if not candidate or not Path(candidate).is_file():
            raise CheckFailure(f"No valid Godot executable in installation report: {candidate}")
        godot = Path(candidate).resolve(strict=True)

    output = args.output.expanduser().resolve()
    workspace = (
        (args.workspace or output.parent / (output.stem + "-evidence")).expanduser().resolve()
    )
    checkout = Path(__file__).resolve().parents[1]
    if workspace.is_relative_to(checkout):
        raise CheckFailure(f"--workspace must be outside the source checkout: {workspace}")

    run_root = (
        workspace
        / f"godot-live-crash-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    )
    run_root.mkdir(parents=True, exist_ok=False)

    # Identity is measured from this run; never inherit another platform's report.
    # The invoking installation/CI record supplies the matching wheel hash separately.
    wheel_info: dict[str, Any] = {
        "godot_sha256": hashlib.sha256(godot.read_bytes()).hexdigest(),
        "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "wheel_identity": "See invoking installation/CI record; not inferred from historical files",
    }

    overall_deadline = time.monotonic() + args.timeout

    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "RUNNING",
        "started_at": _now(),
        "environment": {
            "platform": sys.platform,
            "python": str(python),
            "python_version": sys.version,
            "cli": str(cli),
            "godot": str(godot),
            "fixture": str(fixture),
            "workspace": str(run_root),
            "timeout_seconds": args.timeout,
        },
        "source_identity": {
            "source_checkout": str(checkout),
            **wheel_info,
        },
        "commands": [],
        "checks": {},
    }
    records: list[dict[str, Any]] = report["commands"]

    sentinel_proc: subprocess.Popen[bytes] | None = None
    factory_proc: subprocess.Popen[str] | None = None
    godot_handle: Any = None
    godot_pid: int | None = None
    godot_identity: dict[str, Any] = {}
    release_file: Path | None = None

    # Watchdog state for independent cleanup
    watchdog_state: dict[str, Any] = {
        "completed": False,
        "sentinel_proc": None,
        "factory_proc": None,
        "godot_handle": None,
        "godot_pid": None,
        "godot_identity": None,
        "release_file": None,
    }

    def _emergency_cleanup() -> None:
        rel = watchdog_state.get("release_file")
        if rel and isinstance(rel, Path):
            try:
                rel.write_text("release\n", encoding="utf-8")
            except Exception:
                pass
        h = watchdog_state.get("godot_handle")
        if sys.platform == "win32" and h:
            try:
                kernel32.TerminateProcess(h, 1)
            except Exception:
                pass
            _win32_close_handle(h)
            watchdog_state["godot_handle"] = None
        elif sys.platform != "win32":
            g_pid = watchdog_state.get("godot_pid")
            g_ident = watchdog_state.get("godot_identity")
            if g_pid and g_ident:
                try:
                    is_val, _ = _revalidate_posix_child(g_pid, godot, g_ident.get("starttime", ""))
                    if is_val:
                        os.kill(g_pid, signal.SIGKILL)
                except Exception:
                    pass
        fp = watchdog_state.get("factory_proc")
        if fp and fp.poll() is None:
            try:
                fp.kill()
            except Exception:
                pass
        sp = watchdog_state.get("sentinel_proc")
        if sp and sp.poll() is None:
            try:
                _cleanup_sentinel(sp)
            except Exception:
                pass

    def _watchdog_target() -> None:
        while time.monotonic() < overall_deadline:
            if watchdog_state["completed"]:
                return
            time.sleep(0.25)
        if watchdog_state["completed"]:
            return
        sys.stderr.write(
            f"\n[SUPERVISOR WATCHDOG] Overall timeout ({args.timeout}s) exceeded. Performing emergency cleanup...\n"
        )
        sys.stderr.flush()
        _emergency_cleanup()
        os._exit(124)

    watchdog_thread = threading.Thread(target=_watchdog_target, daemon=True)
    watchdog_thread.start()

    try:
        # 1. Spawn unrelated sentinel child process
        sentinel_proc = _spawn_sentinel(python)
        watchdog_state["sentinel_proc"] = sentinel_proc
        sentinel_pid = sentinel_proc.pid
        if not _check_sentinel_alive(sentinel_proc):
            raise CheckFailure(f"Sentinel process {sentinel_pid} failed to start")
        report["checks"]["sentinel"] = {
            "pid": sentinel_pid,
            "initial_alive": True,
        }

        # 2. Prepare controlled project fixture in external workspace
        project = run_root / "controlled-game-project"
        ready_file = run_root / "fixture-ready.json"
        release_file = run_root / "fixture-release.txt"
        journal_file = run_root / "fixture-runtime-journal.jsonl"
        handshake_token = uuid.uuid4().hex
        watchdog_state["release_file"] = release_file

        finite_bound_ms = min(int(args.timeout * 1000), 25000)
        _prepare_controlled_fixture(
            fixture,
            project,
            ready_file,
            release_file,
            journal_file=journal_file,
            token=handshake_token,
            max_wait_ms=finite_bound_ms,
        )
        baseline_source_hashes = _hashes(project)

        marker = run_root / "factory-marker.json"
        launch_log_path = marker.with_name("launch-observations.jsonl")

        # 3. Initialize Factory in the project via test-only entry wrapper
        _cli(
            records,
            cli,
            python,
            project,
            godot,
            "init",
            launch_log=launch_log_path,
            deadline=overall_deadline,
        )

        scenario = project / "scenario.json"
        workflow_file = run_root / "workflow-info.json"

        # 4. Launch Factory process (the helper) in background
        helper_argv = [
            str(python),
            str(Path(__file__).resolve()),
            "--factory-helper",
            "--project",
            str(project),
            "--godot",
            str(godot),
            "--scenario",
            str(scenario),
            "--workflow-file",
            str(workflow_file),
            "--marker",
            str(marker),
        ]
        factory_start_time = _now()
        factory_proc = subprocess.Popen(
            helper_argv,
            cwd=project,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        watchdog_state["factory_proc"] = factory_proc
        factory_pid = factory_proc.pid
        report["checks"]["factory_process"] = {
            "pid": factory_pid,
            "argv": helper_argv,
            "cwd": str(project),
            "started_at": factory_start_time,
        }

        # 5. Wait boundedly for Godot live handshake (polling journal and ready file)
        supervisor_deadline = min(overall_deadline, time.monotonic() + 45.0)
        handshake_data: dict[str, Any] = {}
        while time.monotonic() < supervisor_deadline:
            if journal_file.is_file():
                try:
                    for line in journal_file.read_text(encoding="utf-8").splitlines():
                        if line.strip():
                            parsed = json.loads(line)
                            if (
                                isinstance(parsed, dict)
                                and parsed.get("pid")
                                and parsed.get("token") == handshake_token
                            ):
                                handshake_data = parsed
                                break
                    if handshake_data:
                        break
                except (json.JSONDecodeError, OSError):
                    pass
            elif ready_file.is_file():
                try:
                    parsed = json.loads(ready_file.read_text(encoding="utf-8"))
                    if isinstance(parsed, dict) and parsed.get("pid"):
                        handshake_data = parsed
                        break
                except (json.JSONDecodeError, OSError):
                    pass

            if factory_proc.poll() is not None:
                out, err = factory_proc.communicate()
                raise CheckFailure(
                    f"Factory helper exited prematurely with code {factory_proc.returncode} before Godot handshake:\n"
                    f"STDOUT:\n{out}\nSTDERR:\n{err}"
                )
            time.sleep(0.05)
        else:
            raise CheckFailure("Supervisor timed out waiting for Godot live handshake")

        godot_pid = int(handshake_data["pid"])
        watchdog_state["godot_pid"] = godot_pid

        # 6. Prove actual Godot identity and current liveness using OS APIs
        godot_identity, godot_handle = _get_process_identity(godot_pid, godot)
        watchdog_state["godot_handle"] = godot_handle
        watchdog_state["godot_identity"] = godot_identity

        report["checks"]["godot_identity"] = {
            **godot_identity,
            "handshake": handshake_data,
            "verified_live_before_factory_kill": True,
        }

        # 7. Confirm NO terminal receipt exists before killing Factory
        scratch = project / ".gamefactory" / "scratch"
        if scratch.is_dir():
            terminal_receipts = [
                str(p.relative_to(project))
                for p in scratch.rglob("*terminal.json")
                if "runtime" in p.name
            ]
            if terminal_receipts:
                raise CheckFailure(
                    f"Runtime terminal receipt unexpectedly exists before Factory kill: {terminal_receipts}"
                )

        # Confirm in SQLite artifacts table
        db_path = project / ".gamefactory" / "state" / "factory.db"
        if db_path.is_file():
            import sqlite3

            conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT relative_path FROM artifacts WHERE relative_path LIKE '%runtime-terminal.json%'"
                )
                rows = cur.fetchall()
                if rows:
                    raise CheckFailure(f"Terminal receipt artifact already recorded in DB: {rows}")
            finally:
                conn.close()

        report["checks"]["pre_kill_terminal_receipt_absent"] = True

        # 8. Force-kill the separate Factory process using retained process handle
        factory_killed_at = _now()
        factory_proc.kill()
        rem_kill_wait = max(0.5, min(10.0, overall_deadline - time.monotonic()))
        factory_out, factory_err = factory_proc.communicate(timeout=rem_kill_wait)
        watchdog_state["factory_proc"] = None

        report["checks"]["factory_process"].update(
            {
                "killed_at": factory_killed_at,
                "exit_code": factory_proc.returncode,
                "stdout": factory_out,
                "stderr": factory_err,
            }
        )

        # 9. Record child alive state BEFORE cleanup.
        # DO NOT kill or clean up child here: on POSIX, child stays alive to test blocking while alive.
        if sys.platform == "win32":
            wait_res = kernel32.WaitForSingleObject(godot_handle, 500)
            if wait_res == WAIT_OBJECT_0:
                exit_code_var = wintypes.DWORD()
                kernel32.GetExitCodeProcess(godot_handle, ctypes.byref(exit_code_var))
                child_fate_post_kill = {
                    "platform": "win32",
                    "status": "terminated",
                    "observed_terminated": True,
                    "exit_code": int(exit_code_var.value),
                }
            else:
                child_fate_post_kill = {
                    "platform": "win32",
                    "status": "alive",
                    "observed_terminated": False,
                }
        else:
            stat_file = Path(f"/proc/{godot_pid}/stat")
            if not stat_file.is_file():
                child_fate_post_kill = {
                    "platform": "posix",
                    "status": "terminated",
                    "observed_terminated": True,
                }
            else:
                stat_text = stat_file.read_text(encoding="utf-8")
                paren_idx = stat_text.rfind(")")
                fields = stat_text[paren_idx + 1 :].split() if paren_idx != -1 else []
                proc_state = fields[0] if fields else "unknown"
                if proc_state == "Z":
                    child_fate_post_kill = {
                        "platform": "posix",
                        "status": "zombie",
                        "proc_state": proc_state,
                        "observed_terminated": True,
                    }
                else:
                    child_fate_post_kill = {
                        "platform": "posix",
                        "status": "alive",
                        "proc_state": proc_state,
                        "observed_terminated": False,
                    }

        report["checks"]["child_fate_post_kill"] = child_fate_post_kill

        # 10. Verify unrelated sentinel child survived Factory termination
        # Keep sentinel alive THROUGH resume/retry (do not clean up yet)
        if not _check_sentinel_alive(sentinel_proc):
            raise CheckFailure(
                "Sentinel process was killed unexpectedly when Factory was terminated"
            )
        report["checks"]["sentinel"]["survived_factory_kill"] = True

        # 11. Read workflow info recorded by helper before crash
        if not workflow_file.is_file():
            raise CheckFailure(f"Workflow file was not written by helper: {workflow_file}")
        workflow_info = _json_file(workflow_file)
        workflow_id = str(workflow_info["workflow_id"])
        task_id = str(workflow_info["execute_task_id"])

        # 12. Pre-recovery inspection via CLI
        pre_inspect = _inspect(
            records,
            cli,
            python,
            project,
            godot,
            workflow_id,
            launch_log=launch_log_path,
            deadline=overall_deadline,
        )
        pre_artifacts = _artifacts(
            records,
            cli,
            python,
            project,
            godot,
            workflow_id,
            launch_log=launch_log_path,
            deadline=overall_deadline,
        )
        report["checks"]["pre_recovery_inspect"] = pre_inspect
        report["checks"]["pre_recovery_artifacts"] = pre_artifacts

        pre_wf_status = pre_inspect.get("workflow", {}).get("status")
        if pre_wf_status not in ("RUNNING", "BLOCKED"):
            raise CheckFailure(f"Unexpected pre-recovery workflow status: {pre_wf_status!r}")

        # 13. First CLI resume: must detect unmatched intent and exit with code 3 (APPROVAL_BLOCKED)
        report["checks"]["resume_1_output"] = _cli(
            records,
            cli,
            python,
            project,
            godot,
            "resume",
            workflow_id,
            launch_log=launch_log_path,
            deadline=overall_deadline,
            expected={3},
        )
        after_resume_1 = _inspect(
            records,
            cli,
            python,
            project,
            godot,
            workflow_id,
            launch_log=launch_log_path,
            deadline=overall_deadline,
        )
        report["checks"]["after_resume_1"] = after_resume_1

        wf_status_1 = after_resume_1.get("workflow", {}).get("status")
        if wf_status_1 != "BLOCKED":
            raise CheckFailure(
                f"Workflow status after resume 1 is {wf_status_1!r}, expected 'BLOCKED'"
            )

        tasks_1 = {t["id"]: t for t in after_resume_1.get("tasks", [])}
        exec_task_1 = tasks_1.get(task_id, {})
        if exec_task_1.get("status") != "BLOCKED":
            raise CheckFailure(
                f"Execute task status after resume 1 is {exec_task_1.get('status')!r}, expected 'BLOCKED'"
            )

        execs_1 = [e for e in after_resume_1.get("executions", []) if e.get("task_id") == task_id]
        if len(execs_1) != 1:
            raise CheckFailure(f"Expected exactly 1 execution attempt, found {len(execs_1)}")
        if execs_1[0].get("status") != "UNCERTAIN":
            raise CheckFailure(
                f"Execution status after resume 1 is {execs_1[0].get('status')!r}, expected 'UNCERTAIN'"
            )

        initial_attempt_id = execs_1[0].get("id")

        # 14. Second CLI resume: must remain safely BLOCKED (exit code 3) without re-executing
        report["checks"]["resume_2_output"] = _cli(
            records,
            cli,
            python,
            project,
            godot,
            "resume",
            workflow_id,
            launch_log=launch_log_path,
            deadline=overall_deadline,
            expected={3},
        )
        after_resume_2 = _inspect(
            records,
            cli,
            python,
            project,
            godot,
            workflow_id,
            launch_log=launch_log_path,
            deadline=overall_deadline,
        )
        report["checks"]["after_resume_2"] = after_resume_2

        execs_2 = [e for e in after_resume_2.get("executions", []) if e.get("task_id") == task_id]
        if (
            len(execs_2) != 1
            or execs_2[0].get("id") != initial_attempt_id
            or execs_2[0].get("status") != "UNCERTAIN"
        ):
            raise CheckFailure("Repeated resume changed execution identity or attempt status")

        # 15. CLI retry: must reject retrying an UNCERTAIN / BLOCKED task (exit code 1)
        report["checks"]["retry_output"] = _cli(
            records,
            cli,
            python,
            project,
            godot,
            "retry",
            workflow_id,
            task_id,
            launch_log=launch_log_path,
            deadline=overall_deadline,
            expected={1},
        )
        after_retry = _inspect(
            records,
            cli,
            python,
            project,
            godot,
            workflow_id,
            launch_log=launch_log_path,
            deadline=overall_deadline,
        )
        post_artifacts = _artifacts(
            records,
            cli,
            python,
            project,
            godot,
            workflow_id,
            launch_log=launch_log_path,
            deadline=overall_deadline,
        )
        report["checks"]["after_retry"] = after_retry
        report["checks"]["post_artifacts"] = post_artifacts

        wf_status_retry = after_retry.get("workflow", {}).get("status")
        if wf_status_retry != "BLOCKED":
            raise CheckFailure(
                f"Workflow status after retry is {wf_status_retry!r}, expected 'BLOCKED'"
            )

        execs_retry = [e for e in after_retry.get("executions", []) if e.get("task_id") == task_id]
        if len(execs_retry) != 1 or execs_retry[0].get("id") != initial_attempt_id:
            raise CheckFailure("Retry created a duplicate execution attempt for uncertain task")

        # Quality gates: NO gate must have passed
        gates = after_retry.get("gates", [])
        passed_gates = [g for g in gates if g.get("status") == "PASSED"]
        if passed_gates:
            raise CheckFailure(
                f"Quality gate unexpectedly passed for crashed attempt: {passed_gates}"
            )

        # 16. Assert no duplicate launches using actual intercepted launch evidence and journal
        launch_records: list[dict[str, Any]] = []
        if launch_log_path.is_file():
            for line in launch_log_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        launch_records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

        # Verify that recovery invocations were observed through the entry wrapper
        wrapper_starts = [r for r in launch_records if r.get("event") == "wrapper_invocation_start"]
        if len(wrapper_starts) < 5:
            raise CheckFailure(
                f"Expected all recovery CLI invocations to be observed by entry wrapper, found {len(wrapper_starts)}"
            )

        runtime_launches = [
            rec
            for rec in launch_records
            if rec.get("event") == "subprocess_launch" and rec.get("is_runtime")
        ]
        if len(runtime_launches) != 1:
            raise CheckFailure(
                f"Expected exactly 1 runtime process launch before crash, found {len(runtime_launches)}: {runtime_launches}"
            )

        # Check append-only fixture runtime journal
        journal_entries: list[dict[str, Any]] = []
        if journal_file.is_file():
            for line in journal_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        journal_entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        if len(journal_entries) != 1:
            raise CheckFailure(
                f"Expected exactly 1 runtime startup journal entry, found {len(journal_entries)}: {journal_entries}"
            )
        if journal_entries[0].get("token") != handshake_token:
            raise CheckFailure(
                f"Runtime journal token mismatch: {journal_entries[0].get('token')} != {handshake_token}"
            )

        report["checks"]["launch_evidence"] = {
            "total_launch_records": len(launch_records),
            "wrapper_invocations_observed": len(wrapper_starts),
            "runtime_launches_count": len(runtime_launches),
            "runtime_launch_record": runtime_launches[0],
            "journal_entries_count": len(journal_entries),
            "handshake_token": handshake_token,
            "no_duplicate_launches_proven": True,
        }

        # 17. Verify historical artifacts were retained and no terminal receipt was forged
        pre_artifact_ids = {a.get("id") for a in pre_artifacts}
        post_artifact_ids = {a.get("id") for a in post_artifacts}
        if not pre_artifact_ids.issubset(post_artifact_ids):
            raise CheckFailure("Recovery lost historical artifacts recorded before the crash")

        if any(
            a.get("relative_path", "").endswith("runtime-terminal.json") for a in post_artifacts
        ):
            raise CheckFailure("Uncertain recovery unexpectedly has a runtime terminal receipt")

        # 18. Verify source project files were not modified
        current_source_hashes = _hashes(project)
        if current_source_hashes != baseline_source_hashes:
            raise CheckFailure("Source project files were modified under Factory execution")

        # 19. Verify sentinel remained alive THROUGH resume/retry recovery steps
        sentinel_survived_full_recovery = _check_sentinel_alive(sentinel_proc)
        _cleanup_sentinel(sentinel_proc)
        sentinel_proc = None
        watchdog_state["sentinel_proc"] = None
        if not sentinel_survived_full_recovery:
            raise CheckFailure("Sentinel process died during resume/retry recovery execution")
        report["checks"]["sentinel"]["survived_full_recovery"] = True

        # 20. Bounded cleanup of Godot child: prefer releasing finite fixture, terminate if needed
        release_file.write_text("release\n", encoding="utf-8")
        child_final_status = "terminated"

        if sys.platform == "win32" and godot_handle:
            wait_res = kernel32.WaitForSingleObject(godot_handle, 5000)
            if wait_res != WAIT_OBJECT_0:
                if not kernel32.TerminateProcess(godot_handle, 1):
                    raise CheckFailure("Owned Windows child termination failed")
                if kernel32.WaitForSingleObject(godot_handle, 2000) != WAIT_OBJECT_0:
                    raise CheckFailure("Owned Windows child exit was not observed")
                child_final_status = "terminated_by_supervisor"
            exit_code_var = wintypes.DWORD()
            kernel32.GetExitCodeProcess(godot_handle, ctypes.byref(exit_code_var))
            _win32_close_handle(godot_handle)
            godot_handle = None
            watchdog_state["godot_handle"] = None
            report["checks"]["child_fate_final"] = {
                "platform": "win32",
                "status": child_final_status,
                "exit_code": int(exit_code_var.value),
                "cleaned_up": True,
            }
        elif sys.platform != "win32" and godot_pid:
            deadline_posix = time.monotonic() + 5.0
            still_alive = True
            while time.monotonic() < deadline_posix:
                is_valid, reason = _revalidate_posix_child(
                    godot_pid, godot, godot_identity.get("starttime", "")
                )
                if not is_valid:
                    _require_terminal_posix_reason(reason)
                    still_alive = False
                    break
                time.sleep(0.05)
            if still_alive:
                is_valid, reason = _revalidate_posix_child(
                    godot_pid, godot, godot_identity.get("starttime", "")
                )
                if not is_valid:
                    raise CheckFailure(f"Child identity mismatch before SIGKILL on POSIX: {reason}")
                try:
                    os.kill(godot_pid, signal.SIGKILL)
                    child_final_status = "terminated_by_sigkill"
                except ProcessLookupError:
                    pass
                for _ in range(40):
                    valid, reason = _revalidate_posix_child(
                        godot_pid, godot, godot_identity.get("starttime", "")
                    )
                    if not valid:
                        _require_terminal_posix_reason(reason)
                        break
                    time.sleep(0.05)
                else:
                    raise CheckFailure("Owned POSIX child exit was not observed after SIGKILL")
            report["checks"]["child_fate_final"] = {
                "platform": "posix",
                "status": child_final_status,
                "cleaned_up": True,
            }

        report["status"] = "PASSED"
        report["completed_at"] = _now()
        report["duration_seconds"] = round(
            (
                datetime.fromisoformat(report["completed_at"])
                - datetime.fromisoformat(report["started_at"])
            ).total_seconds(),
            3,
        )

    except Exception as exc:
        report["status"] = "FAILED"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
        report["completed_at"] = _now()
    finally:
        watchdog_state["completed"] = True
        _emergency_cleanup()

    output.parent.mkdir(parents=True, exist_ok=True)
    _write_json(output, report)

    summary = {
        "status": report["status"],
        "report": str(output),
        "workspace": str(run_root),
        "godot_pid": godot_pid,
        "child_fate_post_kill": report.get("checks", {})
        .get("child_fate_post_kill", {})
        .get("status"),
        "child_fate_final": report.get("checks", {}).get("child_fate_final", {}).get("status"),
        "sentinel_survived": report.get("checks", {})
        .get("sentinel", {})
        .get("survived_full_recovery"),
        "no_duplicate_launches": report.get("checks", {})
        .get("launch_evidence", {})
        .get("no_duplicate_launches_proven"),
        "error": report.get("error"),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if report["status"] == "PASSED" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, help="path to installed gamefactory CLI executable")
    parser.add_argument(
        "--python", type=Path, help="path to Python executable with installed package"
    )
    parser.add_argument("--fixture", type=Path, help="path to Godot project fixture")
    parser.add_argument("--godot", type=Path, help="path to Godot executable")
    parser.add_argument("--output", type=Path, help="path for JSON output report")
    parser.add_argument("--workspace", type=Path, help="external workspace parent directory")
    parser.add_argument("--timeout", type=float, default=120.0, help="overall timeout in seconds")

    # Internal helper arguments
    parser.add_argument("--factory-helper", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--project", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--scenario", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--workflow-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--marker", type=Path, help=argparse.SUPPRESS)

    # Test-only entry wrapper arguments
    parser.add_argument("--entry-wrapper", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--launch-log", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--cli-exe", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--wrapper-probe", action="store_true", help=argparse.SUPPRESS)

    args, unknown = parser.parse_known_args()

    if args.entry_wrapper:
        if not args.launch_log or (not args.wrapper_probe and not args.cli_exe):
            parser.error("Entry wrapper arguments incomplete")
        remaining = list(unknown)
        if remaining and remaining[0] == "--":
            remaining = remaining[1:]
        return _entry_wrapper(args, remaining)

    if args.factory_helper:
        if not all((args.project, args.godot, args.scenario, args.workflow_file, args.marker)):
            parser.error("Factory helper arguments are incomplete")
        return _factory_helper(args)

    if not all((args.cli, args.python, args.fixture, args.output)):
        parser.error("--cli, --python, --fixture, and --output are required")

    return _main(args)


if __name__ == "__main__":
    raise SystemExit(main())
