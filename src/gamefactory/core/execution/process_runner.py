"""Controlled process execution with minimal environments and request-scoped redaction."""

import ctypes
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, NamedTuple, cast

from gamefactory.core.domain.errors import TimeoutError, ToolExecutionError
from gamefactory.core.execution.redaction import (
    _SENSITIVE_ENV_SUBSTRINGS,
    is_eligible_exact_secret,
    redactor,
)


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat()


_ESSENTIAL_ENV_VARS = {
    "PATH",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
}
_MAX_CAPTURE_CHARS = 1_000_000


class BoundedReadResult(NamedTuple):
    """Result of reading bounded stream output."""

    data: bytes
    truncated: bool
    read_completed: bool = True


@dataclass
class CommandRequest:
    """Specification of an external command to execute."""

    args: list[str]
    cwd: Path | str
    env_overrides: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 60.0
    minimal_env: bool = True
    structured_json_output: bool = False
    stdin_text: str | None = None
    env_drop_key_substrings: tuple[str, ...] = ()


@dataclass
class CommandResult:
    """Outcome of an executed command."""

    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float = 0.0
    timed_out: bool = False
    command_display: str = ""
    pid: int | None = None
    started_at: str | None = None
    completed_at: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    termination_status: str = "completed"
    cleanup_completed: bool = True
    cleanup_status: str = "completed"
    args: list[str] = field(default_factory=list)
    cwd: str = ""
    # In-memory protocol data: raw unredacted stdout populated ONLY when request.structured_json_output is True.
    # Must NOT appear in to_dict(), must not be logged, and must not be persisted.
    protocol_stdout: str | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Return serializable dictionary representation of the command result."""
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": self.duration_seconds,
            "timed_out": self.timed_out,
            "command_display": self.command_display,
            "pid": self.pid,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "termination_status": self.termination_status,
            "cleanup_completed": self.cleanup_completed,
            "cleanup_status": self.cleanup_status,
            "args": self.args,
            "cwd": self.cwd,
        }


class ProcessRunner:
    """Executes external commands safely without shell interpolation."""

    def __init__(self, sanitize_output: bool = True) -> None:
        self.sanitize_output = sanitize_output

    def build_env(self, request: CommandRequest) -> dict[str, str]:
        """Construct a minimal environment unless inheritance is explicitly requested."""
        env = (
            dict(os.environ)
            if not request.minimal_env
            else {k: v for k, v in os.environ.items() if k.upper() in _ESSENTIAL_ENV_VARS}
        )
        env.update(request.env_overrides)
        if request.env_drop_key_substrings:
            for key in list(env):
                upper = key.upper()
                if any(part in upper for part in request.env_drop_key_substrings):
                    del env[key]
        return env

    @staticmethod
    def _sensitive_argument_values(args: list[str]) -> list[str]:
        """Collect values attached to explicit common credential flags."""
        flags = {"--api-key", "--api_key", "--token", "--password", "--authorization"}
        values: list[str] = []
        next_value_is_secret = False
        for arg in args:
            if next_value_is_secret:
                values.append(str(arg))
                next_value_is_secret = False
            if "=" in arg:
                flag, value = arg.split("=", 1)
                if flag.lower() in flags and value:
                    values.append(value)
            elif arg.lower() in flags:
                next_value_is_secret = True
        return values

    @staticmethod
    def _attach_windows_job(proc: subprocess.Popen[bytes]) -> int | None:
        """Place a child in a kill-on-close Job Object so descendants are contained."""
        if sys.platform != "win32":
            return None

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            kernel32.CloseHandle(job)
            return None
        process_handle = wintypes.HANDLE(int(cast(Any, proc)._handle))
        if not kernel32.AssignProcessToJobObject(job, process_handle):
            kernel32.CloseHandle(job)
            return None
        return int(job)

    @staticmethod
    def _close_windows_job(job: int | None) -> None:
        if job and sys.platform == "win32":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            if not kernel32.CloseHandle(wintypes.HANDLE(job)):
                raise OSError(ctypes.get_last_error(), "Failed to close Windows Job Object")

    @staticmethod
    def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
        """Terminate the process tree and bound cleanup even if a child ignores TERM."""
        if sys.platform == "win32":
            try:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                get_system_directory = kernel32.GetSystemDirectoryW
                get_system_directory.argtypes = [wintypes.LPWSTR, wintypes.UINT]
                get_system_directory.restype = wintypes.UINT
                system_buffer = ctypes.create_unicode_buffer(32768)
                length = get_system_directory(system_buffer, len(system_buffer))
                if not length or length >= len(system_buffer):
                    raise OSError("Windows system directory is unavailable")
                taskkill = Path(system_buffer.value) / "taskkill.exe"
                subprocess.run(
                    [str(taskkill), "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=5,
                    check=False,
                    shell=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            # Kill the entire group after the grace period even if its leader exited:
            # descendants may still be alive and holding inherited output pipes.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _trim_incomplete_multibyte(data: bytearray | bytes) -> bytes:
        """Trim incomplete UTF-8 trailing sequence from truncated byte buffer."""
        b = bytes(data)
        if not b:
            return b
        for i in range(1, min(5, len(b) + 1)):
            byte = b[-i]
            if byte & 0b10000000 == 0:
                return b
            if byte & 0b11000000 == 0b11000000:
                if byte & 0b11100000 == 0b11000000:
                    expected = 2
                elif byte & 0b11110000 == 0b11100000:
                    expected = 3
                elif byte & 0b11111000 == 0b11110000:
                    expected = 4
                else:
                    expected = 1
                if i < expected:
                    return b[:-i]
                return b
        return b

    @classmethod
    def _cap_redacted_text(cls, value: str) -> tuple[str, bool]:
        """Keep replacement-heavy redaction within the same output byte budget."""
        encoded = value.encode("utf-8")
        if len(encoded) <= _MAX_CAPTURE_CHARS:
            return value, False
        bounded = cls._trim_incomplete_multibyte(encoded[:_MAX_CAPTURE_CHARS])
        return bounded.decode("utf-8"), True

    @classmethod
    def _read_bounded(
        cls, stream: IO[Any], max_bytes: int = _MAX_CAPTURE_CHARS
    ) -> BoundedReadResult:
        """Drain a pipe to avoid child deadlock while retaining bounded output."""
        kept = bytearray()
        truncated = False
        read_completed = False
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    read_completed = True
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                remaining = max_bytes - len(kept)
                if remaining > 0:
                    if len(chunk) > remaining:
                        kept.extend(chunk[:remaining])
                        truncated = True
                    else:
                        kept.extend(chunk)
                else:
                    truncated = True
        except (OSError, ValueError):
            truncated = True
            read_completed = False

        if truncated:
            return BoundedReadResult(cls._trim_incomplete_multibyte(kept), True, read_completed)
        return BoundedReadResult(bytes(kept), False, read_completed)

    def run(self, request: CommandRequest) -> CommandResult:
        """Run the requested command synchronously with shell=False."""
        if not request.args:
            raise ToolExecutionError("Cannot execute empty argument list")
        if (
            isinstance(request.timeout_seconds, bool)
            or not isinstance(request.timeout_seconds, (int, float))
            or not math.isfinite(request.timeout_seconds)
            or request.timeout_seconds <= 0
        ):
            raise ToolExecutionError("Command timeout must be a finite positive number")
        if sys.platform == "win32" and Path(str(request.args[0])).suffix.lower() in {
            ".bat",
            ".cmd",
        }:
            raise ToolExecutionError("Batch script executables are not allowed")
        cwd_path = Path(request.cwd).resolve()
        secrets = [
            v
            for k, v in request.env_overrides.items()
            if is_eligible_exact_secret(v, explicit=True)
            and any(part in k.upper() for part in _SENSITIVE_ENV_SUBSTRINGS)
        ]
        secrets.extend(
            [
                v
                for v in self._sensitive_argument_values(request.args)
                if is_eligible_exact_secret(v, explicit=True)
            ]
        )

        def redact(value: str) -> str:
            return redactor.redact_text(value, secrets)

        command_display = redact(" ".join(str(a) for a in request.args))
        redacted_args = [redact(str(a)) for a in request.args]
        redacted_cwd = redact(str(cwd_path))

        if not cwd_path.is_dir():
            raise ToolExecutionError(
                f"Working directory does not exist: {redacted_cwd}",
                details={
                    "cwd": redacted_cwd,
                    "command": command_display,
                    "args": redacted_args,
                },
            )

        start_wall_time = utc_now_iso()
        start_time = time.perf_counter()
        deadline = start_time + request.timeout_seconds

        popen_kwargs: dict[str, Any] = {
            "args": request.args,
            "cwd": str(cwd_path),
            "env": self.build_env(request),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
        }
        if request.stdin_text is not None:
            popen_kwargs["stdin"] = subprocess.PIPE
        try:
            if sys.platform == "win32":
                proc = subprocess.Popen(  # noqa: S603
                    **popen_kwargs,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                proc = subprocess.Popen(  # noqa: S603
                    **popen_kwargs,
                    start_new_session=True,
                )
        except FileNotFoundError as exc:
            safe_exe = redact(str(request.args[0]))
            raise ToolExecutionError(
                f"Executable not found: '{safe_exe}'",
                details={
                    "executable": safe_exe,
                    "command": command_display,
                    "args": redacted_args,
                    "cwd": redacted_cwd,
                },
            ) from exc
        except OSError as exc:
            safe_error = redact(str(exc))
            raise ToolExecutionError(
                f"Failed to execute command '{command_display}': {safe_error}",
                details={
                    "command": command_display,
                    "error": safe_error,
                    "args": redacted_args,
                    "cwd": redacted_cwd,
                },
            ) from exc

        windows_job: int | None = None
        if sys.platform == "win32":
            try:
                windows_job = self._attach_windows_job(proc)
            except Exception:
                windows_job = None

            if windows_job is None:
                # Fail closed: Tree containment cannot be guaranteed without job object
                try:
                    self._kill_tree(proc)
                except Exception:
                    pass
                completed_wall_time = utc_now_iso()
                duration = round(time.perf_counter() - start_time, 3)
                raise ToolExecutionError(
                    f"Failed to attach process {proc.pid} to Windows Job Object: {command_display}",
                    details={
                        "command": command_display,
                        "args": redacted_args,
                        "cwd": redacted_cwd,
                        "pid": proc.pid,
                        "started_at": start_wall_time,
                        "completed_at": completed_wall_time,
                        "duration": duration,
                        "duration_seconds": duration,
                        "cleanup_completed": False,
                        "cleanup_status": "uncertain",
                        "stdout": "",
                        "stderr": "",
                        "stdout_truncated": False,
                        "stderr_truncated": False,
                        "timed_out": False,
                        "exit_code": proc.returncode,
                        "termination_status": "failed",
                    },
                )

        assert proc.stdout is not None and proc.stderr is not None
        stdout_pipe = proc.stdout
        stderr_pipe = proc.stderr
        captured: dict[str, BoundedReadResult] = {}
        readers = [
            threading.Thread(
                target=lambda: captured.setdefault("stdout", self._read_bounded(stdout_pipe)),
                daemon=True,
            ),
            threading.Thread(
                target=lambda: captured.setdefault("stderr", self._read_bounded(stderr_pipe)),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()

        stdin_thread: threading.Thread | None = None
        if request.stdin_text is not None:
            stdin_payload = request.stdin_text
            stdin_stream = proc.stdin
            assert stdin_stream is not None

            def _write_stdin(payload: str = stdin_payload, stream: IO[Any] = stdin_stream) -> None:
                try:
                    stream.write(payload.encode("utf-8"))
                    stream.flush()
                except OSError:
                    pass
                finally:
                    try:
                        stream.close()
                    except OSError:
                        pass

            stdin_thread = threading.Thread(target=_write_stdin, daemon=True)
            stdin_thread.start()

        cleanup_completed = True
        cleanup_status = "completed"
        cleanup_error: Exception | None = None
        timed_out = False
        timeout_exc: Exception | None = None

        try:
            # 1. Bounded wait for process exit up to deadline
            remaining_proc_wait = max(0.0, deadline - time.perf_counter())
            try:
                proc.wait(timeout=remaining_proc_wait)
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                timeout_exc = exc

            # 2. Cleanup process tree / descendants
            if timed_out:
                try:
                    if windows_job is not None:
                        self._close_windows_job(windows_job)
                        windows_job = None
                except Exception as exc:
                    cleanup_completed = False
                    cleanup_status = "failed"
                    cleanup_error = exc

                try:
                    self._kill_tree(proc)
                except Exception as exc:
                    cleanup_completed = False
                    cleanup_status = "failed"
                    if cleanup_error is None:
                        cleanup_error = exc
            else:
                # Parent exited before deadline!
                # Terminate any surviving pipe-inheriting descendants.
                if windows_job is not None:
                    try:
                        self._close_windows_job(windows_job)
                        windows_job = None
                    except Exception as exc:
                        cleanup_completed = False
                        cleanup_status = "failed"
                        cleanup_error = exc

                if sys.platform != "win32":
                    try:
                        # Reap the owned group even when descendants close inherited pipes
                        # or ignore SIGTERM. A child that starts its own session is outside
                        # this group's ownership and cannot be claimed as cleaned up here.
                        self._kill_tree(proc)
                    except Exception as exc:
                        cleanup_completed = False
                        cleanup_status = "failed"
                        if cleanup_error is None:
                            cleanup_error = exc

            # 3. Drain readers boundedly, honoring overall deadline.
            # Never block close concurrently with reader!
            for reader in readers:
                remaining_time = max(0.0, deadline - time.perf_counter())
                if timed_out:
                    join_timeout = min(0.5, remaining_time) if remaining_time > 0 else 0.2
                else:
                    join_timeout = min(2.0, remaining_time)
                reader.join(timeout=join_timeout)

            # Check if any reader is still active or overall deadline expired
            now = time.perf_counter()
            readers_alive = any(reader.is_alive() for reader in readers)
            if readers_alive or now >= deadline:
                if not timed_out and now >= deadline:
                    timed_out = True
                if readers_alive:
                    cleanup_completed = False
                    if cleanup_status == "completed":
                        cleanup_status = "uncertain"

            if sys.platform != "win32" and (timed_out or readers_alive):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except Exception as exc:
                    cleanup_completed = False
                    cleanup_status = "failed"
                    if cleanup_error is None:
                        cleanup_error = exc

        finally:
            if stdin_thread is not None:
                stdin_thread.join(timeout=0.5)
            if windows_job is not None:
                try:
                    self._close_windows_job(windows_job)
                except Exception as exc:
                    cleanup_completed = False
                    cleanup_status = "failed"
                    if cleanup_error is None:
                        cleanup_error = exc
                windows_job = None

            # NEVER call close() concurrently with reader! Only close pipe if reader has stopped!
            if not readers[0].is_alive():
                try:
                    stdout_pipe.close()
                except OSError:
                    pass
            if not readers[1].is_alive():
                try:
                    stderr_pipe.close()
                except OSError:
                    pass

        stdout_res = captured.get("stdout", BoundedReadResult(b"", False))
        stderr_res = captured.get("stderr", BoundedReadResult(b"", False))
        stdout_str = stdout_res.data.decode("utf-8", errors="replace")
        stderr_str = stderr_res.data.decode("utf-8", errors="replace")
        safe_stdout = redact(stdout_str)
        if request.structured_json_output:
            # Redact decoded strings before serializing: text regexes can otherwise
            # consume a JSON closing quote (for example CLI help containing "auth login").
            # This option never exposes an unredacted fallback on malformed output.
            def redact_json(value: Any, sensitive: bool = False) -> Any:
                if isinstance(value, str):
                    return "[REDACTED]" if sensitive else redact(value)
                if sensitive and isinstance(value, (int, float)) and not isinstance(value, bool):
                    return "[REDACTED]"
                # Booleans/null describe presence, not credential values (doctor
                # reports credential_sources.env and stored_profile.exists).
                if isinstance(value, list):
                    return [redact_json(item, sensitive) for item in value]
                if isinstance(value, dict):
                    return {
                        redact(key): redact_json(
                            item,
                            sensitive
                            or any(part in key.upper() for part in _SENSITIVE_ENV_SUBSTRINGS),
                        )
                        for key, item in value.items()
                    }
                return value

            try:
                safe_stdout = json.dumps(redact_json(json.loads(stdout_str)), allow_nan=False)
            except (ValueError, TypeError, RecursionError):
                pass
        stdout_redacted, stdout_redaction_truncated = self._cap_redacted_text(safe_stdout)
        stderr_redacted, stderr_redaction_truncated = self._cap_redacted_text(redact(stderr_str))
        stdout_truncated = stdout_res.truncated or stdout_redaction_truncated
        stderr_truncated = stderr_res.truncated or stderr_redaction_truncated
        completed_wall_time = utc_now_iso()
        duration = round(time.perf_counter() - start_time, 3)

        if cleanup_error is not None:
            if timed_out:
                raise TimeoutError(
                    f"Command timed out after {request.timeout_seconds} seconds: {command_display}",
                    timeout_seconds=request.timeout_seconds,
                    details={
                        "command": command_display,
                        "args": redacted_args,
                        "cwd": redacted_cwd,
                        "duration": duration,
                        "duration_seconds": duration,
                        "pid": proc.pid,
                        "exit_code": proc.returncode,
                        "timed_out": True,
                        "termination_status": "timed_out",
                        "cleanup_completed": False,
                        "cleanup_status": "failed",
                        "stdout": stdout_redacted,
                        "stderr": stderr_redacted,
                        "stdout_truncated": stdout_truncated,
                        "stderr_truncated": stderr_truncated,
                        "started_at": start_wall_time,
                        "completed_at": completed_wall_time,
                    },
                ) from cleanup_error
            raise ToolExecutionError(
                f"Command cleanup failed for '{command_display}': {redact(str(cleanup_error))}",
                details={
                    "command": command_display,
                    "args": redacted_args,
                    "cwd": redacted_cwd,
                    "duration": duration,
                    "duration_seconds": duration,
                    "pid": proc.pid,
                    "exit_code": proc.returncode,
                    "timed_out": False,
                    "termination_status": "failed",
                    "cleanup_completed": False,
                    "cleanup_status": "failed",
                    "stdout": stdout_redacted,
                    "stderr": stderr_redacted,
                    "stdout_truncated": stdout_truncated,
                    "stderr_truncated": stderr_truncated,
                    "started_at": start_wall_time,
                    "completed_at": completed_wall_time,
                },
            ) from cleanup_error

        if timed_out:
            raise TimeoutError(
                f"Command timed out after {request.timeout_seconds} seconds: {command_display}",
                timeout_seconds=request.timeout_seconds,
                details={
                    "command": command_display,
                    "args": redacted_args,
                    "cwd": redacted_cwd,
                    "duration": duration,
                    "duration_seconds": duration,
                    "pid": proc.pid,
                    "exit_code": proc.returncode,
                    "timed_out": True,
                    "termination_status": "timed_out",
                    "cleanup_completed": cleanup_completed,
                    "cleanup_status": cleanup_status,
                    "stdout": stdout_redacted,
                    "stderr": stderr_redacted,
                    "stdout_truncated": stdout_truncated,
                    "stderr_truncated": stderr_truncated,
                    "started_at": start_wall_time,
                    "completed_at": completed_wall_time,
                },
            ) from timeout_exc

        if not stdout_res.read_completed or not stderr_res.read_completed:
            raise ToolExecutionError(
                f"Command output capture incomplete for '{command_display}'",
                details={
                    "command": command_display,
                    "args": redacted_args,
                    "cwd": redacted_cwd,
                    "duration": duration,
                    "duration_seconds": duration,
                    "pid": proc.pid,
                    "exit_code": proc.returncode,
                    "timed_out": False,
                    "termination_status": "completed",
                    "cleanup_completed": cleanup_completed,
                    "cleanup_status": cleanup_status,
                    "capture_completed": False,
                    "stdout_read_completed": stdout_res.read_completed,
                    "stderr_read_completed": stderr_res.read_completed,
                    "stdout": stdout_redacted,
                    "stderr": stderr_redacted,
                    "stdout_truncated": stdout_truncated,
                    "stderr_truncated": stderr_truncated,
                    "started_at": start_wall_time,
                    "completed_at": completed_wall_time,
                },
            )

        was_killed = proc.returncode is not None and proc.returncode < 0
        termination_status = "terminated" if was_killed else "completed"
        return CommandResult(
            exit_code=proc.returncode if proc.returncode is not None else 0,
            stdout=stdout_redacted,
            stderr=stderr_redacted,
            duration_seconds=duration,
            timed_out=False,
            command_display=command_display,
            pid=proc.pid,
            started_at=start_wall_time,
            completed_at=completed_wall_time,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            termination_status=termination_status,
            cleanup_completed=cleanup_completed,
            cleanup_status=cleanup_status,
            args=redacted_args,
            cwd=redacted_cwd,
            protocol_stdout=stdout_str if request.structured_json_output else None,
        )
