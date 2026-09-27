"""Integration tests for ProcessRunner command execution, timeouts, env filtering, and redaction."""

import os
import sys
import time
from pathlib import Path

import pytest

from gamefactory.core.domain.errors import TimeoutError, ToolExecutionError
from gamefactory.core.execution.process_runner import (
    CommandRequest,
    ProcessRunner,
)


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
        assert "�" in result.stdout

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
