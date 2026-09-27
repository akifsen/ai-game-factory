import json
import os
import select
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gamefactory.core.domain.errors import TimeoutError, ToolExecutionError
from gamefactory.core.execution.process_runner import (
    BoundedReadResult,
    CommandRequest,
    CommandResult,
    ProcessRunner,
)


class _ProcessIdentityHandle:
    """Retained process handle / pidfd for cross-platform reliable identity tracking."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._win_handle: int | None = None
        self._linux_fd: int | None = None

        if sys.platform == "win32":
            import _winapi

            # SYNCHRONIZE (0x00100000) | PROCESS_QUERY_LIMITED_INFORMATION (0x1000) | PROCESS_TERMINATE (0x0001)
            self._win_handle = _winapi.OpenProcess(
                _winapi.SYNCHRONIZE | 0x1000 | 0x0001,
                False,
                pid,
            )
        elif hasattr(os, "pidfd_open"):
            self._linux_fd = os.pidfd_open(pid, 0)
        else:
            raise RuntimeError("pidfd_open is unavailable on this platform")

    def is_alive(self) -> bool:
        """Return True if the identified process is actively running."""
        if sys.platform == "win32":
            if self._win_handle is None:
                raise RuntimeError("Process handle is closed")
            import _winapi

            res = _winapi.WaitForSingleObject(self._win_handle, 0)
            if res == _winapi.WAIT_TIMEOUT:
                return True
            if res == _winapi.WAIT_OBJECT_0:
                return False
            raise RuntimeError(f"Unexpected WaitForSingleObject result: {res}")

        if self._linux_fd is None:
            raise RuntimeError("Process handle is closed")
        poller = select.poll()
        poller.register(self._linux_fd, select.POLLIN)
        events = poller.poll(0)
        if any(bool(revents & (select.POLLERR | select.POLLNVAL)) for _, revents in events):
            raise RuntimeError(f"Poll error on pidfd: {events}")
        if any(bool(revents & select.POLLIN) for _, revents in events):
            return False
        return True

    def wait_terminal(self, timeout_seconds: float = 1.0) -> bool:
        """Wait boundedly for process termination. Return True if terminated."""
        if sys.platform == "win32":
            if self._win_handle is None:
                raise RuntimeError("Process handle is closed")
            import _winapi

            timeout_ms = max(0, int(timeout_seconds * 1000))
            res = _winapi.WaitForSingleObject(self._win_handle, timeout_ms)
            if res == _winapi.WAIT_OBJECT_0:
                return True
            if res == _winapi.WAIT_TIMEOUT:
                return False
            raise RuntimeError(f"Unexpected WaitForSingleObject result: {res}")

        if self._linux_fd is None:
            raise RuntimeError("Process handle is closed")
        poller = select.poll()
        poller.register(self._linux_fd, select.POLLIN)
        timeout_ms = max(0, int(timeout_seconds * 1000))
        events = poller.poll(timeout_ms)
        if any(bool(revents & (select.POLLERR | select.POLLNVAL)) for _, revents in events):
            raise RuntimeError(f"pidfd poll error: {events}")
        if any(bool(revents & select.POLLIN) for _, revents in events):
            return True
        return False

    def close(self) -> None:
        """Release underlying system handles."""
        if self._win_handle is not None:
            import _winapi

            try:
                _winapi.CloseHandle(self._win_handle)
            except OSError:
                pass
            self._win_handle = None

        if self._linux_fd is not None:
            try:
                os.close(self._linux_fd)
            except OSError:
                pass
            self._linux_fd = None

    def cleanup_if_alive(self) -> None:
        """Bounded failure cleanup to prevent leaked processes on test failure."""
        if self._win_handle is None and self._linux_fd is None:
            return
        try:
            if self.is_alive():
                if sys.platform == "win32":
                    import _winapi

                    try:
                        _winapi.TerminateProcess(self._win_handle, 1)
                    except OSError as exc:
                        if not self.wait_terminal(timeout_seconds=1.0):
                            raise RuntimeError(
                                f"TerminateProcess failed for PID {self.pid}"
                            ) from exc
                    else:
                        if not self.wait_terminal(timeout_seconds=1.0):
                            raise RuntimeError(
                                f"Process {self.pid} did not terminate after TerminateProcess"
                            )
                else:
                    sig = getattr(signal, "SIGKILL", signal.SIGTERM)
                    try:
                        signal.pidfd_send_signal(self._linux_fd, sig)
                    except ProcessLookupError:
                        pass
                    except OSError as exc:
                        if not self.wait_terminal(timeout_seconds=1.0):
                            raise RuntimeError(
                                f"pidfd_send_signal failed for PID {self.pid}"
                            ) from exc
                    else:
                        if not self.wait_terminal(timeout_seconds=1.0):
                            raise RuntimeError(
                                f"Process {self.pid} did not terminate after pidfd_send_signal"
                            )
        finally:
            self.close()


class TestProcessRunner:
    def setup_method(self) -> None:
        self.runner = ProcessRunner(sanitize_output=True)

    def test_run_simple_command_stdout(self, tmp_path: Path) -> None:
        req = CommandRequest(
            args=[sys.executable, "-c", "print('hello from child process')"],
            cwd=tmp_path,
        )
        res = self.runner.run(req)
        assert res.exit_code == 0
        assert "hello from child process" in res.stdout
        assert res.stderr == ""
        assert res.duration_seconds >= 0.0

    def test_run_command_stderr(self, tmp_path: Path) -> None:
        req = CommandRequest(
            args=[sys.executable, "-c", "import sys; print('an error occurred', file=sys.stderr)"],
            cwd=tmp_path,
        )
        res = self.runner.run(req)
        assert res.exit_code == 0
        assert "an error occurred" in res.stderr
        assert res.stdout == ""

    def test_minimal_env_filters_secrets(self, tmp_path: Path) -> None:
        # Set a sensitive env var in parent
        os.environ["SUPER_SECRET_TOKEN"] = "my_private_token_999"
        try:
            req = CommandRequest(
                args=[
                    sys.executable,
                    "-c",
                    "import os; print('ENV_VAL:' + os.environ.get('SUPER_SECRET_TOKEN', 'NOT_FOUND'))",
                ],
                cwd=tmp_path,
                minimal_env=True,
            )
            res = self.runner.run(req)
            assert res.exit_code == 0
            assert "ENV_VAL:NOT_FOUND" in res.stdout
        finally:
            os.environ.pop("SUPER_SECRET_TOKEN", None)

    def test_timeout_enforcement(self, tmp_path: Path) -> None:
        req = CommandRequest(
            args=[sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=tmp_path,
            timeout_seconds=0.5,
        )
        with pytest.raises(TimeoutError, match="Command timed out after 0.5 seconds"):
            self.runner.run(req)

    def test_secret_redaction_in_output(self, tmp_path: Path) -> None:
        req = CommandRequest(
            args=[sys.executable, "-c", "print('Output: Authorization: Bearer abcdef1234567890')"],
            cwd=tmp_path,
        )
        res = self.runner.run(req)
        assert res.exit_code == 0
        assert "abcdef1234567890" not in res.stdout
        assert "Bearer [REDACTED]" in res.stdout

    def test_nonexistent_executable_raises_error(self, tmp_path: Path) -> None:
        req = CommandRequest(
            args=["non_existent_executable_12345", "--version"],
            cwd=tmp_path,
        )
        with pytest.raises(ToolExecutionError, match="Executable not found"):
            self.runner.run(req)

    def test_request_override_secret_is_redacted_from_output_and_errors(
        self, tmp_path: Path
    ) -> None:
        secret = "u$p:9!q+2"
        request = CommandRequest(
            args=[sys.executable, "-c", "import os; print(os.environ['MY_SECRET_TOKEN'])"],
            cwd=tmp_path,
            env_overrides={"MY_SECRET_TOKEN": secret},
        )
        result = self.runner.run(request)
        assert secret not in result.stdout
        assert "[REDACTED]" in result.stdout

        missing = CommandRequest(
            args=[secret],
            cwd=tmp_path,
            env_overrides={"MY_SECRET_TOKEN": secret},
        )
        with pytest.raises(ToolExecutionError) as caught:
            self.runner.run(missing)
        assert secret not in str(caught.value)
        assert secret not in str(caught.value.details)

    def test_sensitive_argument_flag_secret_is_redacted(self, tmp_path: Path) -> None:
        secret = "u$p:9!q+2"
        request = CommandRequest(
            args=[
                sys.executable,
                "-c",
                "import sys; print(' '.join(sys.argv[1:]))",
                "--password",
                secret,
            ],
            cwd=tmp_path,
        )
        result = self.runner.run(request)
        assert secret not in result.command_display
        assert secret not in result.stdout
        assert "[REDACTED]" in result.stdout
        assert result.pid is not None

    def test_short_override_secret_and_malformed_unicode_are_safe(self, tmp_path: Path) -> None:
        request = CommandRequest(
            args=[
                sys.executable,
                "-c",
                "import os,sys; sys.stdout.buffer.write(os.environ['TOKEN'].encode()+b'\\xff')",
            ],
            cwd=tmp_path,
            env_overrides={"TOKEN": "x!"},
        )
        result = self.runner.run(request)
        assert "x!" not in result.stdout
        assert "[REDACTED]" in result.stdout
        assert "" in result.stdout

    def test_output_is_bounded(self, tmp_path: Path) -> None:
        result = self.runner.run(
            CommandRequest(
                args=[sys.executable, "-c", "print('x' * 1200000)"],
                cwd=tmp_path,
            )
        )
        assert len(result.stdout) <= 1_000_000

    def test_timeout_kills_descendant_process(self, tmp_path: Path) -> None:
        marker = tmp_path / "grandchild-survived.txt"
        grandchild = tmp_path / "grandchild.py"
        grandchild.write_text(
            "import time; from pathlib import Path; time.sleep(1.3); Path(r'"
            + str(marker)
            + "').write_text('survived')",
            encoding="utf-8",
        )
        parent_code = (
            "import subprocess,sys,time; subprocess.Popen([sys.executable, r'"
            + str(grandchild)
            + "']); time.sleep(10)"
        )
        with pytest.raises(TimeoutError):
            self.runner.run(
                CommandRequest(
                    args=[sys.executable, "-c", parent_code],
                    cwd=tmp_path,
                    timeout_seconds=0.5,
                )
            )
        time.sleep(1.5)
        assert not marker.exists()

    def test_run_success_with_metadata(self, tmp_path: Path) -> None:
        req = CommandRequest(
            args=[sys.executable, "-c", "print('success execution')"],
            cwd=tmp_path,
        )
        res = self.runner.run(req)
        assert res.exit_code == 0
        assert "success execution" in res.stdout
        assert res.stderr == ""
        assert res.duration_seconds >= 0.0
        assert res.timed_out is False
        assert res.termination_status == "completed"
        assert res.cleanup_completed is True
        assert res.cleanup_status == "completed"
        assert res.stdout_truncated is False
        assert res.stderr_truncated is False
        assert res.started_at is not None
        assert res.completed_at is not None
        assert isinstance(res.args, list)
        assert isinstance(res.cwd, str)
        dt_start = datetime.fromisoformat(res.started_at)
        dt_end = datetime.fromisoformat(res.completed_at)
        assert dt_start.tzinfo is not None
        assert dt_end.tzinfo is not None
        assert dt_end >= dt_start
        d = res.to_dict()
        assert d["exit_code"] == 0
        assert json.dumps(d)

    def test_run_nonzero_exit_code(self, tmp_path: Path) -> None:
        req = CommandRequest(
            args=[
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('failing'); sys.stderr.write('bad err'); sys.exit(42)",
            ],
            cwd=tmp_path,
        )
        res = self.runner.run(req)
        assert res.exit_code == 42
        assert "failing" in res.stdout
        assert "bad err" in res.stderr
        assert res.timed_out is False
        assert res.termination_status == "completed"
        assert res.cleanup_completed is True
        assert res.cleanup_status == "completed"
        assert res.stdout_truncated is False
        assert res.stderr_truncated is False
        assert res.started_at is not None
        assert res.completed_at is not None

    def test_timeout_attaches_serializable_metadata(self, tmp_path: Path) -> None:
        req = CommandRequest(
            args=[
                sys.executable,
                "-c",
                "import time, sys; sys.stdout.write('pre-timeout-out'); sys.stdout.flush(); time.sleep(8)",
            ],
            cwd=tmp_path,
            timeout_seconds=2.0,
        )
        with pytest.raises(TimeoutError) as exc_info:
            self.runner.run(req)
        exc = exc_info.value
        assert "Command timed out after 2.0 seconds" in str(exc)
        details = exc.details
        assert details["timed_out"] is True
        assert details["termination_status"] == "timed_out"
        assert details["cleanup_completed"] is True
        assert details["cleanup_status"] == "completed"
        assert details["duration"] >= 1.5
        assert details["duration_seconds"] == details["duration"]
        assert isinstance(details["pid"], int)
        assert details["exit_code"] is not None
        assert "pre-timeout-out" in details["stdout"]
        assert details["stdout_truncated"] is False
        assert details["stderr_truncated"] is False
        assert isinstance(details["started_at"], str)
        assert isinstance(details["completed_at"], str)
        assert isinstance(details["args"], list)
        assert isinstance(details["cwd"], str)
        # Duplicate/unneeded aliases must not be present.
        for alias in (
            "argv",
            "exitcode",
            "termination_state",
            "cleaned_up",
            "start",
            "end",
            "truncated",
            "terminated",
        ):
            assert alias not in details
        assert json.dumps(details)

    def test_redaction_expansion_stays_within_output_limit(self, tmp_path: Path) -> None:
        request = CommandRequest(
            args=[sys.executable, "-c", "print('x!' * 200_000)"],
            cwd=tmp_path,
            env_overrides={"TOKEN": "x!"},
        )
        result = self.runner.run(request)
        assert result.exit_code == 0
        assert result.stdout_truncated is True
        assert len(result.stdout.encode("utf-8")) <= 1_000_000
        assert "x!" not in result.stdout
        assert "[REDACTED]" in result.stdout

    @pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup")
    def test_posix_parent_exit_kills_descendant_ignoring_sigterm(self, tmp_path: Path) -> None:
        ready = tmp_path / "ready"
        marker = tmp_path / "descendant-survived"
        grandchild = (
            "import signal,time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"Path({str(ready)!r}).write_text('ready'); "
            f"time.sleep(1.5); Path({str(marker)!r}).write_text('survived')"
        )
        parent = (
            "import subprocess,sys,time; from pathlib import Path; "
            f"subprocess.Popen([sys.executable,'-c',{grandchild!r}], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
            f"ready=Path({str(ready)!r}); end=time.monotonic()+3; "
            "\nwhile not ready.exists() and time.monotonic()<end: time.sleep(.01)"
        )
        result = self.runner.run(
            CommandRequest(args=[sys.executable, "-c", parent], cwd=tmp_path, timeout_seconds=4)
        )
        assert result.exit_code == 0
        assert result.cleanup_completed is True
        time.sleep(1.7)
        assert not marker.exists()

    def test_incomplete_pipe_read_fails_with_evidence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            self.runner,
            "_read_bounded",
            lambda stream: BoundedReadResult(b"partial", truncated=True, read_completed=False),
        )
        request = CommandRequest(args=[sys.executable, "-c", "print('short output')"], cwd=tmp_path)
        with pytest.raises(ToolExecutionError, match="output capture incomplete") as caught:
            self.runner.run(request)
        details = caught.value.details
        assert details["capture_completed"] is False
        assert details["stdout_read_completed"] is False
        assert details["stderr_read_completed"] is False
        assert details["stdout_truncated"] is True
        assert details["exit_code"] == 0
        assert details["stdout"] == "partial"
        assert details["started_at"] and details["completed_at"]

    def test_cleanup_error_message_redacts_request_secret(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret = "cleanup-secret-987"
        if sys.platform == "win32":
            original_cleanup = self.runner._close_windows_job

            def failing_cleanup(job: int | None) -> None:
                original_cleanup(job)
                raise OSError(f"cleanup failed for {secret}")

            monkeypatch.setattr(self.runner, "_close_windows_job", failing_cleanup)
        else:
            original_cleanup = self.runner._kill_tree

            def failing_cleanup(proc: Any) -> None:
                original_cleanup(proc)
                raise OSError(f"cleanup failed for {secret}")

            monkeypatch.setattr(self.runner, "_kill_tree", failing_cleanup)
        request = CommandRequest(
            args=[sys.executable, "-c", "print('done')"],
            cwd=tmp_path,
            env_overrides={"SECRET_TOKEN": secret},
        )
        with pytest.raises(ToolExecutionError, match="cleanup failed") as caught:
            self.runner.run(request)
        assert secret not in str(caught.value)
        assert secret not in json.dumps(caught.value.details)
        assert caught.value.details["cleanup_completed"] is False

    def test_windows_job_close_failure_is_reported(self) -> None:
        if sys.platform != "win32":
            pytest.skip("Windows Job Objects are only available on Windows")
        with patch("gamefactory.core.execution.process_runner.ctypes.WinDLL") as dll:
            dll.return_value.CloseHandle.return_value = 0
            with pytest.raises(OSError, match="Failed to close Windows Job Object"):
                ProcessRunner._close_windows_job(123)

    def test_timeout_redacts_secrets_in_details(self, tmp_path: Path) -> None:
        secret = "super_secret_tok_9988"
        req = CommandRequest(
            args=[
                sys.executable,
                "-c",
                "import time; time.sleep(5)",
                "--token",
                secret,
            ],
            cwd=tmp_path,
            env_overrides={"SENSITIVE_SECRET": secret},
            timeout_seconds=0.5,
        )
        with pytest.raises(TimeoutError) as exc_info:
            self.runner.run(req)
        exc = exc_info.value
        assert secret not in str(exc)
        serialized = json.dumps(exc.details)
        assert secret not in serialized
        assert "[REDACTED]" in exc.details["command"]

    def test_stdout_truncation_flag_and_multibyte_safety(self, tmp_path: Path) -> None:
        req = CommandRequest(
            args=[
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(('ğ' * 600000).encode('utf-8'))",
            ],
            cwd=tmp_path,
        )
        res = self.runner.run(req)
        assert res.stdout_truncated is True
        assert res.stderr_truncated is False
        assert len(res.stdout.encode("utf-8")) <= 1_000_000
        assert "\ufffd" not in res.stdout
        assert all(c == "ğ" for c in res.stdout)

    def test_stderr_truncation_flag(self, tmp_path: Path) -> None:
        req = CommandRequest(
            args=[
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('e' * 1200000)",
            ],
            cwd=tmp_path,
        )
        res = self.runner.run(req)
        assert res.stderr_truncated is True
        assert res.stdout_truncated is False
        assert len(res.stderr) <= 1_000_000

    def test_error_redacts_credentials_in_details(self, tmp_path: Path) -> None:
        secret = "secret_pass_443"
        req = CommandRequest(
            args=["non_existent_exe_999", "--password", secret],
            cwd=tmp_path,
            env_overrides={"SECRET_TOKEN": secret},
        )
        with pytest.raises(ToolExecutionError) as exc_info:
            self.runner.run(req)
        assert secret not in str(exc_info.value)
        assert secret not in json.dumps(exc_info.value.details)
        assert "[REDACTED]" in str(exc_info.value.details)

    def test_command_result_backward_compatibility(self) -> None:
        # Positional 4 arguments (V0.1 backward compatibility)
        cr = CommandResult(0, "stdout_data", "stderr_data", 1.25)
        assert cr.exit_code == 0
        assert cr.stdout == "stdout_data"
        assert cr.stderr == "stderr_data"
        assert cr.duration_seconds == 1.25
        assert cr.timed_out is False
        assert cr.command_display == ""
        assert cr.pid is None
        assert cr.started_at is None
        assert cr.completed_at is None
        assert cr.stdout_truncated is False
        assert cr.stderr_truncated is False
        assert cr.termination_status == "completed"
        assert cr.cleanup_completed is True
        assert cr.cleanup_status == "completed"

        # Positional 7 arguments (V0.1 backward compatibility)
        cr7 = CommandResult(1, "out", "err", 2.0, False, "cmd_display", 999)
        assert cr7.exit_code == 1
        assert cr7.duration_seconds == 2.0
        assert cr7.pid == 999
        assert cr7.command_display == "cmd_display"

        # Full keyword construction with canonical fields
        cr_full = CommandResult(
            exit_code=0,
            stdout="out",
            stderr="err",
            duration_seconds=0.5,
            timed_out=False,
            command_display="test",
            pid=123,
            started_at="2026-09-27T00:00:00Z",
            completed_at="2026-09-27T00:00:01Z",
            stdout_truncated=True,
            stderr_truncated=False,
            termination_status="completed",
            cleanup_completed=True,
            cleanup_status="completed",
            args=["test"],
            cwd="/tmp",
        )
        assert cr_full.stdout_truncated is True
        assert cr_full.started_at == "2026-09-27T00:00:00Z"
        d = cr_full.to_dict()
        assert d["stdout_truncated"] is True
        assert d["args"] == ["test"]
        assert d["cwd"] == "/tmp"
        assert "duration" not in d
        assert "termination_state" not in d
        assert "cleaned_up" not in d
        assert json.dumps(d)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects only")
    def test_job_attach_failure_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When Windows job attachment fails, runner must fail closed with honest metadata."""
        marker = tmp_path / "attach-failure-marker.txt"
        grandchild = tmp_path / "grandchild.py"
        grandchild.write_text(
            "import time; from pathlib import Path; time.sleep(20.0); Path(r'"
            + str(marker)
            + "').write_text('survived', encoding='utf-8')",
            encoding="utf-8",
        )
        parent_code = (
            "import subprocess, sys; subprocess.Popen([sys.executable, r'"
            + str(grandchild)
            + "']); sys.exit(0)"
        )

        monkeypatch.setattr(self.runner, "_attach_windows_job", lambda proc: None)

        t_start = time.perf_counter()
        req = CommandRequest(
            args=[sys.executable, "-c", parent_code],
            cwd=tmp_path,
            timeout_seconds=0.5,
        )
        with pytest.raises(ToolExecutionError) as exc_info:
            self.runner.run(req)
        elapsed = time.perf_counter() - t_start

        # taskkill (5 s), process waits (2 + 1 s), and startup allow bounded cleanup.
        assert elapsed < 12.0
        time.sleep(0.5)
        # Descendant marker must not exist
        assert not marker.exists()

        exc = exc_info.value
        assert "Failed to attach process" in str(exc)
        details = exc.details
        assert details["cleanup_completed"] is False
        assert details["cleanup_status"] == "uncertain"
        assert details["termination_status"] == "failed"
        assert details["timed_out"] is False
        assert isinstance(details["pid"], int)
        assert details["started_at"] is not None
        assert details["completed_at"] is not None
        assert isinstance(details["args"], list)
        assert isinstance(details["cwd"], str)

    def test_parent_exit_descendant_bounded_and_cleaned(self, tmp_path: Path) -> None:
        """When parent spawns descendant inheriting pipes and exits, runner bounds wait and cleans descendant."""
        marker = tmp_path / "descendant-marker.txt"
        ready_file = tmp_path / "descendant-ready.txt"
        release_file = tmp_path / "parent-release.txt"
        grandchild = tmp_path / "grandchild.py"

        # Descendant writes its PID to signal READY, writes to stdout to verify pipe inheritance,
        # then sleeps long enough to exceed runner deadline unless terminated by cleanup.
        grandchild_code = (
            "import os, sys, time\n"
            "from pathlib import Path\n"
            f"ready_path = Path({str(ready_file)!r})\n"
            "tmp_ready = ready_path.with_suffix('.tmp')\n"
            "tmp_ready.write_text(str(os.getpid()), encoding='utf-8')\n"
            "sys.stdout.write('descendant pipe active\\n')\n"
            "sys.stdout.flush()\n"
            "tmp_ready.replace(ready_path)\n"
            "time.sleep(3.0)\n"
            f"Path({str(marker)!r}).write_text('survived', encoding='utf-8')\n"
        )
        grandchild.write_text(grandchild_code, encoding="utf-8")

        # Parent spawns grandchild (inheriting stdout/stderr pipes), waits for bounded READY handshake
        # and release signal, then exits 0 ONLY if both ready and release were observed.
        parent_code = (
            "import subprocess, sys, time\n"
            "from pathlib import Path\n"
            f"ready_path = Path({str(ready_file)!r})\n"
            f"release_path = Path({str(release_file)!r})\n"
            f"subprocess.Popen([sys.executable, {str(grandchild)!r}])\n"
            "end = time.monotonic() + 0.35\n"
            "while not ready_path.exists() and time.monotonic() < end:\n"
            "    time.sleep(0.002)\n"
            "while not release_path.exists() and time.monotonic() < end:\n"
            "    time.sleep(0.002)\n"
            "if ready_path.exists() and release_path.exists():\n"
            "    sys.exit(0)\n"
            "sys.exit(1)\n"
        )

        handle_holder: list[_ProcessIdentityHandle] = []
        coord_error: list[Exception] = []
        stop_coord = threading.Event()

        def coordinator() -> None:
            try:
                ready_deadline = time.monotonic() + 1.0
                pid_text = ""
                while not stop_coord.is_set() and time.monotonic() < ready_deadline:
                    if ready_file.exists():
                        try:
                            pid_text = ready_file.read_text(encoding="utf-8").strip()
                            if pid_text:
                                break
                        except OSError:
                            pass
                    time.sleep(0.002)

                if stop_coord.is_set():
                    return

                if not pid_text:
                    raise RuntimeError("Descendant failed to signal READY within bounded timeout")

                descendant_pid = int(pid_text)
                handle = _ProcessIdentityHandle(descendant_pid)
                handle_holder.append(handle)

                if not handle.is_alive():
                    raise RuntimeError(
                        f"Descendant PID {descendant_pid} was not alive before release"
                    )

                release_file.write_text("release", encoding="utf-8")
            except Exception as exc:
                coord_error.append(exc)
                try:
                    release_file.write_text("release", encoding="utf-8")
                except OSError:
                    pass

        coord_thread = threading.Thread(target=coordinator, daemon=True)
        coord_thread.start()

        try:
            t_start = time.perf_counter()
            req = CommandRequest(
                args=[sys.executable, "-c", parent_code],
                cwd=tmp_path,
                timeout_seconds=0.5,
            )
            res = self.runner.run(req)

            coord_thread.join(timeout=1.0)
            assert not coord_thread.is_alive(), "Coordinator thread failed to stop"
            if coord_error:
                raise coord_error[0]

            assert len(handle_holder) == 1, "Process identity handle was not acquired"
            handle = handle_holder[0]

            # Normal parent exit
            assert res.exit_code == 0
            assert res.timed_out is False
            assert "descendant pipe active" in res.stdout
            assert res.cleanup_completed is True

            # Terminal observation bounded from runner start (<2 sec) to precede natural 3.0s exit
            remaining = 2.0 - (time.perf_counter() - t_start)
            assert remaining > 0, "Execution exceeded 2.0s bound before terminal observation"
            assert handle.wait_terminal(timeout_seconds=remaining) is True
            assert time.perf_counter() - t_start < 2.0

            # Marker check supplements only
            time.sleep(1.0)
            assert not marker.exists()
        finally:
            stop_coord.set()
            coord_thread.join(timeout=1.0)
            assert not coord_thread.is_alive(), "Coordinator thread failed to stop"
            for h in handle_holder:
                h.cleanup_if_alive()

    def test_cleanup_failure_retains_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When cleanup encounters an error, metadata must be retained and fail closed."""
        original_kill = self.runner._kill_tree

        def failing_kill(proc: Any) -> None:
            original_kill(proc)
            raise OSError("Simulated cleanup error")

        monkeypatch.setattr(self.runner, "_kill_tree", failing_kill)

        req = CommandRequest(
            args=[
                sys.executable,
                "-c",
                "import time, sys; sys.stdout.write('partial output data'); sys.stdout.flush(); time.sleep(5)",
            ],
            cwd=tmp_path,
            timeout_seconds=0.2,
        )
        with pytest.raises(TimeoutError) as exc_info:
            self.runner.run(req)

        details = exc_info.value.details
        assert details["cleanup_completed"] is False
        assert details["cleanup_status"] == "failed"
        assert details["timed_out"] is True
        assert "partial output data" in details["stdout"]
        assert isinstance(details["pid"], int)
        assert details["started_at"] is not None
        assert details["completed_at"] is not None
        assert isinstance(details["args"], list)
        assert isinstance(details["cwd"], str)
