from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace

import pytest

from gamefactory.adapters.agents.codex import (
    CodexAgentConfig,
    CodexAgentProvider,
    _path_in_scopes,
    _strict_output_schema,
)
from gamefactory.agents.context import BoundedContext
from gamefactory.core.domain.agent_contracts import (
    AgentKind,
    AgentOutcome,
    AgentResultProposal,
    AgentTaskContract,
    CostConstraints,
    PromptTemplateRef,
    ProposedFile,
    ToolConstraints,
)
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.provider_execution import ProviderAuthorization


def make_contract(**overrides):
    values = {
        "schema_version": "1.0.0",
        "task_id": "task-42",
        "task_type": "code.propose",
        "kind": AgentKind.CODE,
        "objective": "Propose a bounded code change",
        "project_id": "project-1",
        "prompt_template": PromptTemplateRef(template_id="codex.readonly", version="1.0.0"),
        "allowed_output_paths": ("src/game/player.gd",),
        "acceptance_criteria": ("Preserve behavior",),
    }
    values.update(overrides)
    return AgentTaskContract(**values)


def test_prompt_bundle_contains_task_and_provider_identity():
    provider = CodexAgentProvider(CodexAgentConfig(model="operator-selected-model"))
    prompt, prompt_hash, bundle_hash = provider._build_prompt(
        make_contract(), BoundedContext((), 0)
    )
    assert "task-42" in prompt
    assert provider.provider_id in prompt
    assert len(prompt_hash) == len(bundle_hash) == 64


def test_prompt_template_hash_must_match_installed_versioned_bundle():
    provider = CodexAgentProvider(CodexAgentConfig(model="operator-selected-model"))
    contract = make_contract(
        prompt_template=PromptTemplateRef(
            template_id="codex.readonly", version="1.0.0", sha256="0" * 64
        )
    )
    with pytest.raises(ValidationError):
        provider._build_prompt(contract, BoundedContext((), 0))


def test_output_scope_requires_exact_file_or_scoped_directory():
    assert _path_in_scopes("src/game/player.gd", ("src/game/player.gd",))
    assert not _path_in_scopes("src/game-old/player.gd", ("src/game/player.gd",))
    assert not _path_in_scopes("project.cfg", ("src/game/",))


def test_codex_structured_output_schema_is_recursively_closed_and_required():
    schema = _strict_output_schema()

    def visit(node):
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                assert node.get("additionalProperties") is False
                assert node.get("required") == list(node.get("properties", {}))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)


class AllowCodexIntent:
    def verify(self, authorization, provider_id, request_fingerprint, operation_hash):
        return provider_id == "agent.codex.readonly" and request_fingerprint == "a" * 64


def authorized_contract(**overrides):
    values = make_contract(
        allowed_tools=("codex.execute_readonly",),
        tool_constraints=ToolConstraints(
            allowed_tools=("codex.execute_readonly",),
            max_tool_calls=1,
            timeout_seconds=15,
            network_allowed=True,
            process_execution_allowed=True,
        ),
        cost_constraints=CostConstraints(
            max_amount=1, currency="USD", unit="request", cost_class="PAID"
        ),
    ).model_dump()
    values.update(overrides)
    return AgentTaskContract.model_validate(values)


def test_codex_returns_strict_non_authoritative_proposal_under_persisted_auth(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "gamefactory.adapters.agents.codex.resolve_codex_executable",
        lambda _path: ("operator-codex",),
    )
    invoked = {}

    def fake_exec_runner(**kwargs):
        invoked.update(kwargs)
        content = b"extends Node\n"
        proposal = AgentResultProposal(
            schema_version="1.0.0",
            task_id="task-42",
            agent_id="agent.codex.readonly",
            outcome=AgentOutcome.PROPOSED,
            summary="One bounded proposal",
            proposed_files=(
                ProposedFile(
                    path="src/game/player.gd",
                    operation="CREATE",
                    content_base64=base64.b64encode(content).decode("ascii"),
                    output_sha256=hashlib.sha256(content).hexdigest(),
                ),
            ),
        )
        kwargs["message_path"].write_text(proposal.model_dump_json(), encoding="utf-8")
        return SimpleNamespace(timed_out=False, exit_code=0)

    provider = CodexAgentProvider(
        CodexAgentConfig(model="operator-selected-model", codex_path="configured"),
        exec_runner=fake_exec_runner,
    )
    auth = ProviderAuthorization(
        approval_id="approval-1",
        operation_hash="b" * 64,
        request_fingerprint="a" * 64,
        max_cost=1,
        currency="USD",
        unit="request",
        cost_class="PAID",
    )
    run = provider.execute(
        authorized_contract(),
        BoundedContext((), 0),
        tmp_path / "codex-work",
        "a" * 64,
        auth,
        AllowCodexIntent(),
    )
    assert run.proposal.task_id == "task-42"
    assert run.proposal.outcome is AgentOutcome.PROPOSED
    assert run.files[0].path == "src/game/player.gd"
    assert run.files[0].content_bytes() == b"extends Node\n"
    assert "--sandbox" in invoked["args"]
    assert "read-only" in invoked["args"]
    assert invoked["timeout_seconds"] == 15
    args = invoked["args"]
    configured = {args[index + 1] for index, value in enumerate(args[:-1]) if value == "-c"}
    assert {
        'web_search="disabled"',
        "features.apps=false",
        "features.goals=false",
        "features.hooks=false",
        "features.memories=false",
        "features.multi_agent=false",
        "features.remote_plugin=false",
        "features.shell_snapshot=false",
        "features.shell_tool=false",
        "features.unified_exec=false",
        "mcp_servers={}",
    } <= configured


@pytest.mark.parametrize("exit_code", [1, 17])
def test_codex_nonzero_exit_is_uncertain_after_remote_invocation(monkeypatch, tmp_path, exit_code):
    monkeypatch.setattr(
        "gamefactory.adapters.agents.codex.resolve_codex_executable",
        lambda _path: ("operator-codex",),
    )
    provider = CodexAgentProvider(
        CodexAgentConfig(model="operator-selected-model", codex_path="configured"),
        exec_runner=lambda **_kwargs: SimpleNamespace(timed_out=False, exit_code=exit_code),
    )
    auth = ProviderAuthorization(
        approval_id="approval-1",
        operation_hash="b" * 64,
        request_fingerprint="a" * 64,
        max_cost=1,
        currency="USD",
        unit="request",
        cost_class="PAID",
    )
    run = provider.execute(
        authorized_contract(),
        BoundedContext((), 0),
        tmp_path / f"codex-exit-{exit_code}",
        "a" * 64,
        auth,
        AllowCodexIntent(),
    )
    assert run.status.value == "UNCERTAIN"
    assert run.metadata["exit_code"] == exit_code


def test_codex_malformed_post_invocation_response_is_uncertain(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "gamefactory.adapters.agents.codex.resolve_codex_executable",
        lambda _path: ("operator-codex",),
    )

    def malformed_response(**kwargs):
        kwargs["message_path"].write_text("{not-json", encoding="utf-8")
        return SimpleNamespace(timed_out=False, exit_code=0)

    provider = CodexAgentProvider(
        CodexAgentConfig(model="operator-selected-model", codex_path="configured"),
        exec_runner=malformed_response,
    )
    auth = ProviderAuthorization(
        approval_id="approval-1",
        operation_hash="b" * 64,
        request_fingerprint="a" * 64,
        max_cost=1,
        currency="USD",
        unit="request",
        cost_class="PAID",
    )
    run = provider.execute(
        authorized_contract(),
        BoundedContext((), 0),
        tmp_path / "codex-malformed",
        "a" * 64,
        auth,
        AllowCodexIntent(),
    )
    assert run.status.value == "UNCERTAIN"
    assert run.proposal is None
