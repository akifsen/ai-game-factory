"""Focused ProcessRunner tests for stdin delivery and timeout validation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gamefactory.core.domain.errors import TimeoutError, ToolExecutionError
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner


class TestProcessRunnerStdin:
    def setup_method(self) -> None:
        self.runner = ProcessRunner(sanitize_output=True)

    def test_stdin_text_is_delivered_to_child(self, tmp_path: Path) -> None:
        req = CommandRequest(
            args=[sys.executable, "-c", "import sys; print(sys.stdin.read(), end='')"],
            cwd=tmp_path,
            stdin_text="prompt-bytes\n",
        )
        result = self.runner.run(req)
        assert result.exit_code == 0
        assert result.stdout.replace("\r\n", "\n") == "prompt-bytes\n"

    def test_empty_args_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ToolExecutionError, match="empty argument"):
            self.runner.run(CommandRequest(args=[], cwd=tmp_path))

    def test_timeout_rejects_nonfinite(self, tmp_path: Path) -> None:
        with pytest.raises(ToolExecutionError, match="finite positive"):
            self.runner.run(
                CommandRequest(
                    args=[sys.executable, "-c", "print('x')"],
                    cwd=tmp_path,
                    timeout_seconds=float("nan"),
                )
            )

    def test_stdin_with_slow_child_respects_timeout(self, tmp_path: Path) -> None:
        req = CommandRequest(
            args=[sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=tmp_path,
            stdin_text="ignored",
            timeout_seconds=0.5,
        )
        with pytest.raises(TimeoutError):
            self.runner.run(req)

    def test_env_drop_key_substrings(self, tmp_path: Path) -> None:
        req = CommandRequest(
            args=[
                sys.executable,
                "-c",
                "import os; print(os.environ.get('GAMEFACTORY_MCP_TOKEN', 'MISSING'))",
            ],
            cwd=tmp_path,
            minimal_env=True,
            env_drop_key_substrings=("MCP",),
            env_overrides={"GAMEFACTORY_MCP_TOKEN": "secret"},
        )
        result = self.runner.run(req)
        assert result.exit_code == 0
        assert "MISSING" in result.stdout
