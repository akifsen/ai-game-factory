"""Controlled process execution with minimal environments and request-scoped redaction."""

import ctypes
import math
import os
import signal
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, cast

from gamefactory.core.domain.errors import TimeoutError, ToolExecutionError
from gamefactory.core.execution.redaction import _SENSITIVE_ENV_SUBSTRINGS, redactor

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


@dataclass
class CommandRequest:
    """Specification of an external command to execute."""

    args: list[str]
    cwd: Path | str
    env_overrides: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 60.0
    minimal_env: bool = True


@dataclass
class CommandResult:
    """Outcome of an executed command."""

    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    command_display: str = ""
    pid: int | None = None


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
            kernel32.CloseHandle(wintypes.HANDLE(job))

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
            proc.wait(timeout=2)

    @staticmethod
    def _read_bounded(stream: IO[Any]) -> bytes:
        """Drain a pipe to avoid child deadlock while retaining bounded output."""
        kept = bytearray()
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            remaining = _MAX_CAPTURE_CHARS - len(kept)
            if remaining > 0:
                kept.extend(chunk[:remaining])
        return bytes(kept)

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
            if v and any(part in k.upper() for part in _SENSITIVE_ENV_SUBSTRINGS)
        ]
        secrets.extend(self._sensitive_argument_values(request.args))

        def redact(value: str) -> str:
            return redactor.redact_text(value, secrets)

        command_display = redact(" ".join(str(a) for a in request.args))
        if not cwd_path.is_dir():
            raise ToolExecutionError(
                f"Working directory does not exist: {redact(str(cwd_path))}",
                details={"cwd": redact(str(cwd_path))},
            )
        start_time = time.perf_counter()
        try:
            if sys.platform == "win32":
                proc = subprocess.Popen(  # noqa: S603
                    request.args,
                    cwd=str(cwd_path),
                    env=self.build_env(request),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                proc = subprocess.Popen(  # noqa: S603
                    request.args,
                    cwd=str(cwd_path),
                    env=self.build_env(request),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    start_new_session=True,
                )
            windows_job = self._attach_windows_job(proc)
            assert proc.stdout is not None and proc.stderr is not None
            stdout_pipe = proc.stdout
            stderr_pipe = proc.stderr
            captured: dict[str, bytes] = {}
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
            try:
                proc.wait(timeout=request.timeout_seconds)
                self._close_windows_job(windows_job)
                windows_job = None
                for reader in readers:
                    reader.join(timeout=2)
                if any(reader.is_alive() for reader in readers):
                    proc.stdout.close()
                    proc.stderr.close()
                    for reader in readers:
                        reader.join(timeout=1)
            except subprocess.TimeoutExpired as exc:
                self._close_windows_job(windows_job)
                windows_job = None
                self._kill_tree(proc)
                for reader in readers:
                    reader.join(timeout=2)
                stdout = captured.get("stdout", b"").decode("utf-8", errors="replace")
                stderr = captured.get("stderr", b"").decode("utf-8", errors="replace")
                raise TimeoutError(
                    f"Command timed out after {request.timeout_seconds} seconds: {command_display}",
                    timeout_seconds=request.timeout_seconds,
                    details={
                        "command": command_display,
                        "duration": time.perf_counter() - start_time,
                        "pid": proc.pid,
                        "stdout": redact(stdout),
                        "stderr": redact(stderr),
                    },
                ) from exc
            finally:
                self._close_windows_job(windows_job)
            stdout = captured.get("stdout", b"").decode("utf-8", errors="replace")
            stderr = captured.get("stderr", b"").decode("utf-8", errors="replace")
            stdout, stderr = redact(stdout), redact(stderr)
            return CommandResult(
                exit_code=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=round(time.perf_counter() - start_time, 3),
                command_display=command_display,
                pid=proc.pid,
            )
        except FileNotFoundError as exc:
            safe_exe = redact(str(request.args[0]))
            raise ToolExecutionError(
                f"Executable not found: '{safe_exe}'",
                details={"executable": safe_exe, "command": command_display},
            ) from exc
        except OSError as exc:
            safe_error = redact(str(exc))
            raise ToolExecutionError(
                f"Failed to execute command '{command_display}': {safe_error}",
                details={"command": command_display, "error": safe_error},
            ) from exc
