"""Integration tests for execution metadata, truncation, timeouts, and redaction in ProcessRunner."""

import io
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest

from gamefactory.core.domain.errors import TimeoutError, ToolExecutionError
from gamefactory.core.execution.process_runner import (
    CommandRequest,
    CommandResult,
    ProcessRunner,
)


class TestProcessMetadata:
    def setup_method(self) -> None:
        self.runner = ProcessRunner(sanitize_output=True)

    def test_command_result_defaults_and_properties(self) -> None:
        res = CommandResult(
            exit_code=0,
            stdout="standard output",
            stderr="standard error",
            duration_seconds=1.234,
        )
        assert res.exit_code == 0
        assert res.stdout == "standard output"
        assert res.stderr == "standard error"
        assert res.duration_seconds == 1.234
        assert res.timed_out is False
        assert res.command_display == ""
        assert res.pid is None
        assert res.started_at is None
        assert res.completed_at is None
        assert res.stdout_truncated is False
        assert res.stderr_truncated is False
        assert res.termination_status == "completed"
        assert res.cleanup_completed is True
        assert res.cleanup_status == "completed"
        assert res.args == []
        assert res.cwd == ""

        # Test to_dict serialization
        data = res.to_dict()
        assert data["duration_seconds"] == 1.234
        assert data["termination_status"] == "completed"
        assert data["cleanup_completed"] is True
        assert data["cleanup_status"] == "completed"
        assert data["args"] == []
        assert data["cwd"] == ""
        # Unneeded aliases must not exist in serialized dictionary
        assert "duration" not in data
        assert "termination_state" not in data
        assert "cleaned_up" not in data
        assert "argv" not in data
        assert "exitcode" not in data
        assert "terminated" not in data
        assert json.dumps(data)

    def test_command_result_canonical_construction(self) -> None:
        res = CommandResult(
            exit_code=0,
            stdout="",
            stderr="",
            duration_seconds=4.56,
            args=["python", "script.py"],
            cwd="/test/path",
        )
        assert res.duration_seconds == 4.56
        assert res.args == ["python", "script.py"]
        assert res.cwd == "/test/path"

    def test_multibyte_trim_incomplete_helper(self) -> None:
        # Full characters
        text = "Hello 世界 🚀"
        b_full = text.encode("utf-8")
        assert ProcessRunner._trim_incomplete_multibyte(b_full) == b_full

        # Sliced inside 3-byte Chinese character
        cut_3byte = b_full[:7]  # Cuts inside '世' (\xe4\xb8\x96)
        trimmed = ProcessRunner._trim_incomplete_multibyte(cut_3byte)
        assert trimmed.decode("utf-8") == "Hello "

        # Sliced inside 4-byte emoji
        b_emoji = "🚀".encode()  # 4 bytes: \xf0\x9f\x9a\x80
        for cut_len in range(1, 4):
            trimmed_emoji = ProcessRunner._trim_incomplete_multibyte(b_emoji[:cut_len])
            assert trimmed_emoji == b""
        assert ProcessRunner._trim_incomplete_multibyte(b_emoji) == b_emoji

    def test_read_bounded_with_multibyte_stream(self) -> None:
        # Test stream containing multibyte Turkish characters repeated beyond 100 bytes limit
        raw_text = "öçşiğü" * 20  # 120 chars, 240 bytes
        stream = io.BytesIO(raw_text.encode("utf-8"))
        res = self.runner._read_bounded(stream, max_bytes=100)
        assert res.truncated is True
        assert len(res.data) <= 100
        # Must decode cleanly without replacement character
        decoded = res.data.decode("utf-8")
        assert "\ufffd" not in decoded
        assert all(c in "öçşiğü" for c in decoded)

    def test_read_error_does_not_silently_return_untruncated_success(self) -> None:
        """Pipe reader encountering OSError must report truncation and incomplete read."""

        class FaultyStream:
            def __init__(self) -> None:
                self.calls = 0

            def read(self, size: int = -1) -> bytes:
                self.calls += 1
                if self.calls == 1:
                    return b"partial prefix data"
                raise OSError("Simulated pipe read failure")

        stream = FaultyStream()
        res = ProcessRunner._read_bounded(cast(Any, stream), max_bytes=1000)
        assert res.data == b"partial prefix data"
        # Must not report untruncated success!
        assert res.truncated is True
        assert res.read_completed is False

    def test_successful_execution_metadata_contract(self, tmp_path: Path) -> None:
        req = CommandRequest(
            args=[
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('test-out'); sys.stderr.write('test-err')",
            ],
            cwd=tmp_path,
        )
        result = self.runner.run(req)
        assert result.exit_code == 0
        assert result.stdout == "test-out"
        assert result.stderr == "test-err"
        assert result.duration_seconds >= 0.0
        assert result.timed_out is False
        assert result.termination_status == "completed"
        assert result.cleanup_completed is True
        assert result.cleanup_status == "completed"
        assert result.stdout_truncated is False
        assert result.stderr_truncated is False
        assert result.pid is not None and result.pid > 0

        # Verify UTC ISO timestamps
        assert result.started_at is not None
        assert result.completed_at is not None
        t_start = datetime.fromisoformat(result.started_at)
        t_end = datetime.fromisoformat(result.completed_at)
        assert t_start.tzinfo is not None
        assert t_end.tzinfo is not None
        assert t_end >= t_start

    def test_nonzero_execution_metadata_contract(self, tmp_path: Path) -> None:
        req = CommandRequest(
            args=[sys.executable, "-c", "import sys; sys.exit(3)"],
            cwd=tmp_path,
        )
        result = self.runner.run(req)
        assert result.exit_code == 3
        assert result.timed_out is False
        assert result.termination_status == "completed"
        assert result.cleanup_completed is True
        assert result.cleanup_status == "completed"
        assert result.started_at is not None
        assert result.completed_at is not None

    def test_timeout_metadata_contract_and_serializability(self, tmp_path: Path) -> None:
        req = CommandRequest(
            args=[
                sys.executable,
                "-c",
                "import time, sys; sys.stdout.write('partial stdout'); sys.stdout.flush(); time.sleep(10)",
            ],
            cwd=tmp_path,
            timeout_seconds=0.5,
        )
        with pytest.raises(TimeoutError) as exc_info:
            self.runner.run(req)

        exc = exc_info.value
        details = exc.details
        required_keys = [
            "command",
            "args",
            "cwd",
            "duration",
            "duration_seconds",
            "pid",
            "exit_code",
            "timed_out",
            "termination_status",
            "cleanup_completed",
            "cleanup_status",
            "stdout",
            "stderr",
            "stdout_truncated",
            "stderr_truncated",
            "started_at",
            "completed_at",
        ]
        for key in required_keys:
            assert key in details, f"Missing key in TimeoutError.details: {key}"

        # Ensure unneeded aliases are absent
        forbidden_keys = [
            "argv",
            "exitcode",
            "termination_state",
            "cleaned_up",
            "start",
            "end",
            "truncated",
            "terminated",
        ]
        for key in forbidden_keys:
            assert key not in details, f"Forbidden alias key found in TimeoutError.details: {key}"

        assert details["timed_out"] is True
        assert details["termination_status"] == "timed_out"
        assert details["cleanup_completed"] is True
        assert details["cleanup_status"] == "completed"
        assert details["stdout_truncated"] is False
        assert details["stderr_truncated"] is False
        assert "partial stdout" in details["stdout"]

        # Ensure ISO timestamps are valid
        t_start = datetime.fromisoformat(details["started_at"])
        t_end = datetime.fromisoformat(details["completed_at"])
        assert t_start.tzinfo is not None
        assert t_end.tzinfo is not None
        assert t_end >= t_start

        # Full JSON serialization check
        serialized = json.dumps(details)
        deserialized = json.loads(serialized)
        assert deserialized["timed_out"] is True

    def test_timeout_redaction_metadata_safety(self, tmp_path: Path) -> None:
        secret_token = "my_hidden_jwt_token_12345"
        req = CommandRequest(
            args=[
                sys.executable,
                "-c",
                "import time; time.sleep(10)",
                "--api-key",
                secret_token,
            ],
            cwd=tmp_path,
            env_overrides={"SENSITIVE_KEY": secret_token},
            timeout_seconds=0.5,
        )
        with pytest.raises(TimeoutError) as exc_info:
            self.runner.run(req)

        exc = exc_info.value
        serialized = json.dumps(exc.details)
        assert secret_token not in str(exc)
        assert secret_token not in serialized
        assert "[REDACTED]" in exc.details["command"]
        assert any("[REDACTED]" in arg for arg in exc.details["args"])

    def test_truncation_stdout_and_stderr(self, tmp_path: Path) -> None:
        # Stdout truncation
        req_out = CommandRequest(
            args=[
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('A' * 1100000)",
            ],
            cwd=tmp_path,
        )
        res_out = self.runner.run(req_out)
        assert res_out.stdout_truncated is True
        assert res_out.stderr_truncated is False
        assert len(res_out.stdout) <= 1_000_000

        # Stderr truncation
        req_err = CommandRequest(
            args=[
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('B' * 1100000)",
            ],
            cwd=tmp_path,
        )
        res_err = self.runner.run(req_err)
        assert res_err.stderr_truncated is True
        assert res_err.stdout_truncated is False
        assert len(res_err.stderr) <= 1_000_000

    def test_tool_execution_error_redaction(self, tmp_path: Path) -> None:
        secret = "secret_db_pass_999"
        req = CommandRequest(
            args=["non_existing_tool_exe", "--password", secret],
            cwd=tmp_path,
        )
        with pytest.raises(ToolExecutionError) as exc_info:
            self.runner.run(req)

        exc = exc_info.value
        assert secret not in str(exc)
        assert secret not in json.dumps(exc.details)
        assert "[REDACTED]" in str(exc.details)
