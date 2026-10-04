"""Pure contract coverage for strict Factory CLI composition; no engine/process launch."""

from __future__ import annotations

import json

import pytest

from gamefactory.cli.exit_codes import (
    EXIT_APPROVAL_BLOCKED,
    EXIT_SUCCESS,
    EXIT_TOOL_UNAVAILABLE,
    EXIT_WORKFLOW_FAILURE,
)
from gamefactory.cli.factory_commands import (
    _preflight_code,
    _validate_declared_outputs,
    _workflow_code,
    dispatch_factory_command,
    load_factory_registries,
)
from gamefactory.cli.main import build_parser
from gamefactory.core.domain.agent_contracts import AgentKind
from gamefactory.core.domain.errors import ValidationError


def test_general_factory_and_native_operation_commands_are_registered() -> None:
    parser = build_parser()
    commands = parser._subparsers._group_actions[0].choices
    assert {
        "new",
        "discover",
        "factory",
        "test-game",
        "performance-review",
        "editor",
        "run-scene",
        "build",
        "release",
        "operation",
    } <= set(commands)
    factory = commands["factory"]._subparsers._group_actions[0].choices
    assert {"manifest", "providers", "run", "recover-apply"} <= set(factory)
    provider_actions = factory["providers"]._subparsers._group_actions[0].choices
    assert {
        "add-codex",
        "add-process",
        "add-openai-image",
        "add-openai-speech",
        "add-openai-vision",
    } <= set(provider_actions)


def test_game_write_requires_exact_output_artifact_paths() -> None:
    _validate_declared_outputs(True, ["scripts/player.gd"], ["scripts/player.gd"])
    _validate_declared_outputs(False, ["scripts/player.gd"], [])
    with pytest.raises(ValidationError):
        _validate_declared_outputs(True, ["scripts/player.gd"], [])
    with pytest.raises(ValidationError):
        _validate_declared_outputs(True, ["scripts/player.gd"], ["scenes/player.tscn"])


def test_factory_workflow_status_maps_to_non_success_exit_codes() -> None:
    assert _workflow_code("COMPLETED") == EXIT_SUCCESS
    assert _workflow_code("BLOCKED") == EXIT_APPROVAL_BLOCKED
    assert _workflow_code("FAILED") == EXIT_WORKFLOW_FAILURE
    assert _preflight_code("READY") == EXIT_SUCCESS
    assert _preflight_code("NOT_VERIFIED") == EXIT_TOOL_UNAVAILABLE


def test_recovery_apply_requires_hash_and_audit_context_in_cli_contract() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "factory",
            "recover-apply",
            "--journal",
            ".gamefactory/operations/factory-apply/OWNED_JOURNAL.json",
            "--apply",
            "--expected-journal-sha256",
            "a" * 64,
            "--actor",
            "operator",
            "--comment",
            "recover interrupted apply",
        ]
    )
    assert args.factory_command == "recover-apply"
    assert args.journal == ".gamefactory/operations/factory-apply/OWNED_JOURNAL.json"
    assert args.apply is True


@pytest.mark.parametrize(
    ("command", "provider_id", "agent_kind", "tool", "capability", "extra_args"),
    [
        (
            "add-openai-image",
            "provider.openai.image",
            AgentKind.IMAGE,
            "openai.images.generate",
            "image.generate",
            [],
        ),
        (
            "add-openai-speech",
            "provider.openai.audio-speech",
            AgentKind.AUDIO,
            "openai.audio.speech",
            "audio.speech",
            ["--voice", "alloy"],
        ),
        (
            "add-openai-vision",
            "provider.openai.vision-review",
            AgentKind.VISION,
            "openai.responses.vision",
            "vision.review",
            [],
        ),
    ],
)
def test_openai_media_provider_dispatch_persists_typed_definition_and_readiness(
    tmp_path, monkeypatch, command, provider_id, agent_kind, tool, capability, extra_args
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_path = tmp_path / "operator-config.json"
    config_path.write_text(
        json.dumps(
            {
                "model_id": "gpt-test-model",
                "credential_env_name": "OPENAI_API_KEY",
                "cost": {
                    "cost_class": "PAID",
                    "max_amount": 1.0,
                    "currency": "USD",
                    "unit": "request",
                    "approval_required": True,
                },
            }
        ),
        encoding="utf-8",
    )
    args = build_parser().parse_args(
        [
            "factory",
            "providers",
            command,
            "--config",
            str(config_path),
            *extra_args,
        ]
    )

    result, exit_code, _ = dispatch_factory_command(args, tmp_path, None)

    assert exit_code == EXIT_SUCCESS
    assert result["provider_id"] == provider_id
    assert result["credential_values_stored"] is False
    assert result["live_execution_verified"] is False
    executors, agents, gates, readiness = load_factory_registries(tmp_path)
    registered = agents.get(provider_id)
    assert registered is not None
    assert registered.definition.kinds == (agent_kind,)
    assert registered.definition.allowed_tools == (tool,)
    assert registered.definition.capabilities == (capability,)
    executor = executors.get(provider_id)
    assert executor is not None
    assert executor[1] == frozenset({capability})
    assert executor[2] is False
    assert readiness[0]["credential_env_names"] == ["OPENAI_API_KEY"]
    assert readiness[0]["live_execution_verified"] is False
