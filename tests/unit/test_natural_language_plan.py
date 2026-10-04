"""Unit tests for bounded static_prop natural-language planning."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gamefactory.adapters.planning.codex_cli_agent import (
    CodexCliAgentProvider,
    CodexExecResult,
    resolve_codex_executable,
    validate_codex_timeout_seconds,
)
from gamefactory.cli import main as cli_main
from gamefactory.cli.plan_command import run_plan_command, write_plan_output_atomic
from gamefactory.core.domain.asset_contracts import parse_any_asset_specification
from gamefactory.core.domain.errors import (
    ConfigurationError,
    ProviderUnavailable,
    TimeoutError,
    ToolExecutionError,
    ToolUnavailableError,
    ValidationError,
)
from gamefactory.core.domain.natural_language_plan import (
    _MAX_CODEX_MESSAGE_BYTES,
    CODEX_MESSAGE_FILE_NAME,
    assemble_static_prop_specification,
    build_planner_prompt,
    codex_draft_json_schema,
    parse_plan_draft,
    plan_to_asset_specification,
    read_bounded_codex_message_json,
)

_VALID_DRAFT: dict[str, Any] = {
    "asset_id": "prop_tide_crate_01",
    "intent": "Tide Bastion salvage crate for dock clutter",
    "dimensions": {"width_m": 1.0, "depth_m": 0.9, "height_m": 1.1},
    "style_constraints": {"silhouette": "chunky", "detail_density": "medium"},
}


def _write_message(message_path: Path, payload: dict[str, Any] | str) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    message_path.write_text(text, encoding="utf-8")


def _mock_exec_runner(
    *,
    draft: dict[str, Any] | None = None,
    exit_code: int = 0,
    timed_out: bool = False,
    message_payload: dict[str, Any] | str | None = None,
    skip_message: bool = False,
) -> Any:
    resolved = message_payload if message_payload is not None else draft
    if resolved is None:
        resolved = _VALID_DRAFT

    def _runner(**kwargs: Any) -> CodexExecResult:
        message_path = kwargs.get("message_path")
        if not skip_message and message_path is not None and isinstance(resolved, (dict, str)):
            _write_message(Path(message_path), resolved)
        return CodexExecResult(exit_code=exit_code, timed_out=timed_out)

    return _runner


class TestNaturalLanguagePlanDomain:
    def test_assembled_spec_parses_with_domain_contract(self) -> None:
        draft = parse_plan_draft(_VALID_DRAFT)
        assembled = assemble_static_prop_specification(draft)
        spec = parse_any_asset_specification(assembled)
        assert spec.asset_id == "prop_tide_crate_01"
        assert spec.bound_profile().qualified == "static_prop@1"
        assert assembled["target_import_path"] == "assets/generated/props/prop_tide_crate_01/"
        assert assembled["style_constraints"]["family"] == "stylized_fantasy"

    def test_plan_to_asset_specification_round_trip(self) -> None:
        spec = plan_to_asset_specification(_VALID_DRAFT)
        assert spec.profile == "static_prop"
        assert spec.schema_version == "0.4.0"

    def test_rejects_extra_draft_fields(self) -> None:
        bad = {**_VALID_DRAFT, "profile": "pickup"}
        with pytest.raises(ValidationError, match="Planner draft failed validation"):
            parse_plan_draft(bad)

    def test_rejects_string_dimension_values(self) -> None:
        bad = {
            **_VALID_DRAFT,
            "dimensions": {"width_m": "1.0", "depth_m": 0.9, "height_m": 1.1},
        }
        with pytest.raises(ValidationError):
            parse_plan_draft(bad)

    def test_rejects_invalid_silhouette(self) -> None:
        bad = {
            **_VALID_DRAFT,
            "style_constraints": {"silhouette": "spiky", "detail_density": "medium"},
        }
        with pytest.raises(ValidationError):
            parse_plan_draft(bad)

    def test_rejects_tall_silhouette_without_schema_fallback(self) -> None:
        bad = {
            **_VALID_DRAFT,
            "style_constraints": {"silhouette": "tall", "detail_density": "medium"},
        }
        with pytest.raises(ValidationError):
            parse_plan_draft(bad)

    def test_codex_draft_json_schema_advertises_style_enums(self) -> None:
        schema = codex_draft_json_schema()
        style_props = schema["$defs"]["PlanStyleDraft"]["properties"]
        assert set(style_props["silhouette"]["enum"]) == {
            "angular",
            "chunky",
            "organic",
            "planar",
        }
        assert set(style_props["detail_density"]["enum"]) == {"high", "low", "medium"}

    def test_build_planner_prompt_lists_allowed_style_enums(self) -> None:
        prompt = build_planner_prompt("rusted buoy")
        assert "chunky" in prompt and "planar" in prompt and "angular" in prompt
        assert "low" in prompt and "medium" in prompt and "high" in prompt

    def test_rejects_path_like_asset_id(self) -> None:
        bad = {**_VALID_DRAFT, "asset_id": "../escape"}
        with pytest.raises(ValidationError):
            parse_plan_draft(bad)

    def test_rejects_reserved_windows_asset_id(self) -> None:
        bad = {**_VALID_DRAFT, "asset_id": "con"}
        with pytest.raises(ValidationError):
            parse_plan_draft(bad)


class TestCodexMessageProtocol:
    def test_read_bounded_message_rejects_duplicate_keys(self, tmp_path: Path) -> None:
        path = tmp_path / CODEX_MESSAGE_FILE_NAME
        path.write_text('{"asset_id":"a","asset_id":"b"}', encoding="utf-8")
        with pytest.raises(ValidationError, match="duplicate JSON key"):
            read_bounded_codex_message_json(path)

    def test_read_bounded_message_rejects_oversize(self, tmp_path: Path) -> None:
        path = tmp_path / CODEX_MESSAGE_FILE_NAME
        path.write_bytes(b"x" * (_MAX_CODEX_MESSAGE_BYTES + 1))
        with pytest.raises(ValidationError, match="too large"):
            read_bounded_codex_message_json(path)


class TestCodexCliAgentProvider:
    def test_plan_invokes_expected_codex_flags(self) -> None:
        captured: dict[str, Any] = {}

        def recorder(**kwargs: Any) -> CodexExecResult:
            captured.update(kwargs)
            args = kwargs["args"]
            schema_idx = args.index("--output-schema")
            schema_path = Path(args[schema_idx + 1])
            captured["written_schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
            return _mock_exec_runner()(**kwargs)

        provider = CodexCliAgentProvider(
            codex_path=str(Path(sys.executable)),
            exec_runner=recorder,
            timeout_seconds=42.0,
        )
        spec = provider.plan("rusted tide buoy prop")
        assert spec["asset_id"] == "prop_tide_crate_01"
        args = captured["args"]
        assert args[1] == "exec"
        assert "--sandbox" in args and "read-only" in args
        assert "--ephemeral" in args
        assert "--ignore-user-config" in args
        assert "--skip-git-repo-check" in args
        schema_idx = args.index("--output-schema")
        assert args[schema_idx + 1].endswith("draft.schema.json")
        written_schema = captured["written_schema"]
        style_props = written_schema["$defs"]["PlanStyleDraft"]["properties"]
        assert set(style_props["silhouette"]["enum"]) == {
            "angular",
            "chunky",
            "organic",
            "planar",
        }
        assert set(style_props["detail_density"]["enum"]) == {"high", "low", "medium"}
        message_idx = args.index("--output-last-message")
        assert args[message_idx + 1].endswith(CODEX_MESSAGE_FILE_NAME)
        assert args[-1] == "-"
        assert captured["timeout_seconds"] == 42.0
        assert "Tide Bastion" in captured["stdin_text"]
        message_path = Path(captured["message_path"])
        assert message_path.name == CODEX_MESSAGE_FILE_NAME

    def test_noisy_stdout_still_reads_message_file(self) -> None:
        provider = CodexCliAgentProvider(
            codex_path=str(Path(sys.executable)),
            exec_runner=_mock_exec_runner(),
        )
        spec = provider.plan("dock rope")
        assert spec["asset_id"] == "prop_tide_crate_01"

    def test_missing_message_file_raises(self) -> None:
        provider = CodexCliAgentProvider(
            codex_path=str(Path(sys.executable)),
            exec_runner=_mock_exec_runner(skip_message=True),
        )
        with pytest.raises(ValidationError, match="message file is missing"):
            provider.plan("any request")

    def test_malformed_message_file_raises(self) -> None:
        provider = CodexCliAgentProvider(
            codex_path=str(Path(sys.executable)),
            exec_runner=_mock_exec_runner(message_payload="{not-json"),
        )
        with pytest.raises(ValidationError, match="not valid JSON"):
            provider.plan("any request")

    def test_timeout_surfaces_timeout_error(self) -> None:
        provider = CodexCliAgentProvider(
            codex_path=str(Path(sys.executable)),
            exec_runner=_mock_exec_runner(timed_out=True),
        )
        with pytest.raises(TimeoutError):
            provider.plan("any request")

    def test_nonzero_exit_raises_tool_execution_error(self) -> None:
        provider = CodexCliAgentProvider(
            codex_path=str(Path(sys.executable)),
            exec_runner=_mock_exec_runner(exit_code=2),
        )
        with pytest.raises(ToolExecutionError, match="non-zero"):
            provider.plan("any request")

    def test_timeout_validation_rejects_nonfinite(self) -> None:
        with pytest.raises(ValidationError):
            validate_codex_timeout_seconds(math.nan)
        with pytest.raises(ValidationError):
            validate_codex_timeout_seconds(math.inf)
        with pytest.raises(ValidationError):
            validate_codex_timeout_seconds(0.5)
        with pytest.raises(ValidationError):
            validate_codex_timeout_seconds(601.0)

    def test_empty_request_raises_before_codex(self) -> None:
        provider = CodexCliAgentProvider(
            codex_path=str(Path(sys.executable)),
            exec_runner=_mock_exec_runner(),
        )
        with pytest.raises(ValidationError, match="cannot be empty"):
            provider.plan("   ")

    def test_missing_codex_path_raises_tool_unavailable(self) -> None:
        with pytest.raises(ToolUnavailableError):
            resolve_codex_executable(str(Path("/nonexistent/codex-binary")))


class TestPlanCommandCli:
    def test_cli_writes_spec_without_factory_init(self, tmp_path: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()
        out = root / "plans" / "prop.json"

        class _Args:
            request = "tide bastion rope coil"
            output = str(out.relative_to(root))
            codex_path = str(Path(sys.executable))
            timeout = 30.0

        with patch.object(
            CodexCliAgentProvider,
            "plan",
            return_value=assemble_static_prop_specification(parse_plan_draft(_VALID_DRAFT)),
        ):
            payload, code, human = run_plan_command(root, _Args())
        assert code == 0
        assert out.is_file()
        written = json.loads(out.read_text(encoding="utf-8"))
        parse_any_asset_specification(written)
        assert payload["asset_id"] == "prop_tide_crate_01"
        assert "does not create assets" in (human or "")

    def test_refuses_existing_output_before_codex(self, tmp_path: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()
        target = root / "spec.json"
        target.write_text("{}", encoding="utf-8")

        class _Args:
            request = "crate"
            output = "spec.json"
            codex_path = None
            timeout = 30.0

        with patch.object(CodexCliAgentProvider, "plan") as mock_plan:
            with pytest.raises(ConfigurationError, match="Refusing to overwrite"):
                run_plan_command(root, _Args())
            mock_plan.assert_not_called()

    def test_refuses_overwrite_on_publish(self, tmp_path: Path) -> None:
        target = tmp_path / "spec.json"
        target.write_text('{"keep": true}', encoding="utf-8")
        spec = assemble_static_prop_specification(parse_plan_draft(_VALID_DRAFT))
        with pytest.raises(ConfigurationError, match="Refusing to overwrite"):
            write_plan_output_atomic(target, spec)
        assert json.loads(target.read_text(encoding="utf-8")) == {"keep": True}

    def test_atomic_publish_does_not_leak_tempfiles(self, tmp_path: Path) -> None:
        target = tmp_path / "spec.json"
        spec = assemble_static_prop_specification(parse_plan_draft(_VALID_DRAFT))
        write_plan_output_atomic(target, spec)
        assert target.is_file()
        assert list(tmp_path.glob(".*.tmp")) == []

    def test_output_path_traversal_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        class _Args:
            request = "crate"
            output = "../../outside.json"
            codex_path = None
            timeout = 30.0

        with pytest.raises(ValidationError):
            run_plan_command(root, _Args())

    def test_main_plan_exit_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        out = root / "planned.json"
        spec = assemble_static_prop_specification(parse_plan_draft(_VALID_DRAFT))

        monkeypatch.chdir(root)
        with patch.object(CodexCliAgentProvider, "plan", return_value=spec):
            code = cli_main.main(
                [
                    "plan",
                    "tide bastion lantern",
                    "--output",
                    str(out),
                    "--codex-path",
                    sys.executable,
                ]
            )
        assert code == 0
        parse_any_asset_specification(json.loads(out.read_text(encoding="utf-8")))

    def test_main_plan_tool_unavailable_exit_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with patch.object(
            CodexCliAgentProvider,
            "plan",
            side_effect=ProviderUnavailable("missing", provider="codex"),
        ):
            code = cli_main.main(["plan", "crate", "--output", "out.json", "--codex-path", "x"])
        assert code == 4
