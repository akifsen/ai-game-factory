from __future__ import annotations

import io
import json
import sys
import wave
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from gamefactory.adapters.agents.process_provider import (
    OperatorProcessAgentProvider,
    ProcessProviderConfig,
    _validate_advertised_media,
)
from gamefactory.agents.context import BoundedContext
from gamefactory.core.domain.agent_contracts import (
    AgentKind,
    AgentTaskContract,
    CapabilityRequirement,
    CostConstraints,
    PromptTemplateRef,
    ToolConstraints,
)
from gamefactory.core.domain.errors import ValidationError as FactoryValidationError
from gamefactory.core.domain.provider_execution import (
    ProviderAuthorization,
    ProviderExecutionStatus,
)


def config(**overrides):
    values = {
        "provider_id": "local.image",
        "executable": "definitely-not-installed-image-tool",
        "fixed_args": ("--json",),
        "kind": AgentKind.IMAGE,
        "capabilities": ("image.generate",),
        "tool_name": "image.generate",
        "cost": CostConstraints(max_amount=0, cost_class="LOCAL"),
    }
    values.update(overrides)
    return ProcessProviderConfig(**values)


def test_operator_process_settings_are_fixed_bounded_and_no_fallback():
    assert config().fixed_args == ("--json",)
    with pytest.raises(ValidationError):
        config(fixed_args=("--token=secret",))
    with pytest.raises(ValidationError):
        config(fixed_args=tuple("x" * 4096 for _ in range(65)))
    with pytest.raises(ValidationError):
        config(cost=CostConstraints(fallback_max_amount=1))


def test_readiness_never_launches_an_unconfigured_process():
    provider = OperatorProcessAgentProvider(config())
    readiness = provider.readiness()
    assert readiness.status.value == "MISCONFIGURED"
    assert readiness.live_execution_verified is False


def test_media_validation_decodes_images_and_parses_wav_frames():
    with pytest.raises(FactoryValidationError):
        _validate_advertised_media("image/png", b"\x89PNG\r\n\x1a\n" + b"bad")
    with pytest.raises(FactoryValidationError):
        _validate_advertised_media("audio/flac", b"fLaC" + b"opaque")
    empty_wave = io.BytesIO()
    with wave.open(empty_wave, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"")
    with pytest.raises(FactoryValidationError):
        _validate_advertised_media("audio/wav", empty_wave.getvalue())


def test_supported_audio_mime_cannot_be_faked_by_a_signature():
    with pytest.raises(FactoryValidationError):
        _validate_advertised_media("audio/wav", b"RIFF\x00\x00\x00\x00WAVE")


class AllowProcessIntent:
    def verify(self, authorization, provider_id, request_fingerprint, operation_hash):
        return (
            authorization.approval_id == "approval-process"
            and provider_id == "local.image"
            and request_fingerprint == "a" * 64
            and operation_hash == "b" * 64
        )


def _authorized_process_provider(response, *, exit_code=0, timed_out=False):
    executable_config = config(
        executable=sys.executable,
        tool_name="process.execute",
        allows_network=True,
        cost=CostConstraints(max_amount=5, currency="USD", unit="request", cost_class="PAID"),
    )
    provider = OperatorProcessAgentProvider(
        executable_config,
        runner=SimpleNamespace(
            run=lambda _request: SimpleNamespace(
                timed_out=timed_out,
                exit_code=exit_code,
                protocol_stdout=response,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        ),
    )
    contract = AgentTaskContract(
        schema_version="1.0.0",
        task_id="task-process",
        selected_agent_id="local.image",
        task_type="image.generate",
        kind=AgentKind.IMAGE,
        objective="Generate an image",
        project_id="project-process",
        prompt_template=PromptTemplateRef(template_id="operator.process", version="1.0.0"),
        required_capabilities=(CapabilityRequirement(name="image.generate"),),
        allowed_tools=("process.execute",),
        tool_constraints=ToolConstraints(
            allowed_tools=("process.execute",),
            max_tool_calls=1,
            timeout_seconds=20,
            network_allowed=True,
            process_execution_allowed=True,
        ),
        cost_constraints=CostConstraints(
            max_amount=5, currency="USD", unit="request", cost_class="PAID"
        ),
    )
    authorization = ProviderAuthorization(
        approval_id="approval-process",
        operation_hash="b" * 64,
        request_fingerprint="a" * 64,
        max_cost=5,
        currency="USD",
        unit="request",
        cost_class="PAID",
    )
    return provider, contract, authorization


def test_nonzero_process_exit_is_uncertain_after_launch(tmp_path):
    provider, contract, authorization = _authorized_process_provider(None, exit_code=17)
    run = provider.execute(
        contract,
        BoundedContext((), 0),
        tmp_path / "workspace",
        "a" * 64,
        authorization,
        AllowProcessIntent(),
    )
    assert run.status is ProviderExecutionStatus.UNCERTAIN
    assert run.metadata["exit_code"] == 17


@pytest.mark.parametrize("status", ["FAILED", "UNCERTAIN"])
def test_failed_process_response_preserves_validated_actual_cost(tmp_path, status):
    response = json.dumps(
        {
            "schema_version": "agent-process-response-1.0.0",
            "status": status,
            "proposal": None,
            "visual_review": None,
            "files": [],
            "actual_cost": 1.25,
            "cost_currency": "USD",
            "cost_unit": "request",
            "external_id": None,
            "metadata": {"detail": "partial provider result"},
        }
    )
    provider, contract, authorization = _authorized_process_provider(response)
    run = provider.execute(
        contract,
        BoundedContext((), 0),
        tmp_path / f"workspace-{status.lower()}",
        "a" * 64,
        authorization,
        AllowProcessIntent(),
    )
    assert run.status is ProviderExecutionStatus(status)
    assert run.actual_cost == 1.25
    assert run.cost_currency == "USD"
    assert run.cost_unit == "request"
