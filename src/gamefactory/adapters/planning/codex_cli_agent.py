"""Codex CLI agent provider for bounded static_prop natural-language planning."""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from gamefactory.core.domain.errors import (
    ProviderUnavailable,
    TimeoutError,
    ToolExecutionError,
    ToolUnavailableError,
    ValidationError,
)
from gamefactory.core.domain.natural_language_plan import (
    CODEX_MESSAGE_FILE_NAME,
    build_planner_prompt,
    codex_draft_json_schema,
    plan_to_asset_specification,
    read_bounded_codex_message_json,
)
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner

_DEFAULT_TIMEOUT_SECONDS = 180.0
_MIN_TIMEOUT_SECONDS = 1.0
_MAX_TIMEOUT_SECONDS = 600.0
_PROVIDER_ID = "codex-cli-static-prop-planner"


class _CodexExecResult(Protocol):
    exit_code: int
    timed_out: bool


CodexExecRunner = Callable[..., _CodexExecResult]


def validate_codex_timeout_seconds(value: float) -> float:
    """Reject non-finite or out-of-range Codex planner timeouts."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("timeout must be a finite number between 1 and 600 seconds")
    if not math.isfinite(value) or value < _MIN_TIMEOUT_SECONDS or value > _MAX_TIMEOUT_SECONDS:
        raise ValidationError("timeout must be a finite number between 1 and 600 seconds")
    return float(value)


@dataclass(frozen=True)
class CodexCliAgentProvider:
    """Strict Codex CLI planner for static_prop@1 (no fallback providers)."""

    codex_path: str | None = None
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    runner: ProcessRunner | None = None
    exec_runner: CodexExecRunner | None = None

    def plan(self, user_request: str) -> dict[str, Any]:
        """Run Codex exec and return a validated asset-spec-0.4.0 dict."""
        prompt = build_planner_prompt(user_request)
        raw_draft = self._invoke_codex(prompt)
        spec = plan_to_asset_specification(raw_draft)
        return spec.model_dump(mode="json")

    def _invoke_codex(self, prompt: str) -> dict[str, Any]:
        timeout = validate_codex_timeout_seconds(self.timeout_seconds)
        executable = resolve_codex_executable(self.codex_path)
        with tempfile.TemporaryDirectory(prefix="gamefactory-plan-") as temp_dir:
            workspace = Path(temp_dir)
            schema_path = workspace / "draft.schema.json"
            message_path = workspace / CODEX_MESSAGE_FILE_NAME
            schema_path.write_text(
                json.dumps(codex_draft_json_schema(), indent=2) + "\n",
                encoding="utf-8",
            )
            args = [
                *executable,
                "exec",
                "--sandbox",
                "read-only",
                "--ephemeral",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(message_path),
                "-",
            ]
            if self.exec_runner is not None:
                result = self.exec_runner(
                    args=args,
                    cwd=workspace,
                    stdin_text=prompt,
                    timeout_seconds=timeout,
                    message_path=message_path,
                )
            else:
                result = _run_codex_via_process_runner(
                    args=args,
                    cwd=workspace,
                    stdin_text=prompt,
                    timeout_seconds=timeout,
                    runner=self.runner or ProcessRunner(sanitize_output=True),
                )
            if result.timed_out:
                raise TimeoutError(
                    "Codex planner timed out",
                    timeout_seconds=timeout,
                )
            if result.exit_code != 0:
                raise ToolExecutionError(
                    "Codex planner exited with a non-zero status",
                    exit_code=result.exit_code,
                    details={"provider": _PROVIDER_ID},
                )
            return read_bounded_codex_message_json(message_path)


def resolve_codex_executable(custom_path: str | None = None) -> list[str]:
    """Locate the Codex CLI executable without shell wrappers on Windows."""
    if custom_path:
        candidate = Path(custom_path).expanduser().resolve()
        if not candidate.is_file():
            raise ToolUnavailableError(
                "Configured Codex executable was not found",
                tool="codex",
                reason="configured_path_missing",
                configured_path=str(candidate),
            )
        if candidate.suffix.lower() in {".bat", ".cmd"}:
            raise ToolUnavailableError(
                "Batch script Codex launchers are not supported",
                tool="codex",
                reason="batch_launcher_forbidden",
                configured_path=str(candidate),
            )
        return [str(candidate)]

    env_path = os.environ.get("GAMEFACTORY_CODEX_PATH")
    if env_path:
        return resolve_codex_executable(env_path)

    names = ("codex", "codex.exe") if sys.platform == "win32" else ("codex",)
    for name in names:
        found = shutil.which(name)
        if not found:
            continue
        path = Path(found)
        if path.suffix.lower() == ".cmd":
            sibling = path.with_suffix(".exe")
            if sibling.is_file():
                return [str(sibling.resolve())]
            continue
        if path.suffix.lower() == ".bat":
            continue
        return [str(path.resolve())]

    raise ProviderUnavailable(
        "Codex CLI was not found on PATH; install Codex or pass --codex-path",
        provider=_PROVIDER_ID,
    )


@dataclass
class CodexExecResult:
    exit_code: int
    timed_out: bool = False


def _run_codex_via_process_runner(
    *,
    args: list[str],
    cwd: Path,
    stdin_text: str,
    timeout_seconds: float,
    runner: ProcessRunner,
) -> CodexExecResult:
    """Run Codex with stdin prompt via ProcessRunner (bounded I/O, descendant cleanup)."""
    request = CommandRequest(
        args=args,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        minimal_env=True,
        stdin_text=stdin_text,
        env_drop_key_substrings=("MCP",),
    )
    try:
        result = runner.run(request)
    except TimeoutError:
        return CodexExecResult(exit_code=1, timed_out=True)
    return CodexExecResult(exit_code=result.exit_code, timed_out=False)
