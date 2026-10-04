from __future__ import annotations

import hashlib
import io
import json

import pytest
from PIL import Image

from gamefactory.adapters.agents.openai_media import HttpResponse
from gamefactory.adapters.agents.openai_vision import OpenAIVisionConfig, OpenAIVisionReviewProvider
from gamefactory.agents.context import BoundedContext, ContextItem
from gamefactory.core.domain.agent_contracts import (
    AgentKind,
    AgentTaskContract,
    CapabilityRequirement,
    CostConstraints,
    PromptTemplateRef,
    SourceReference,
    ToolConstraints,
)
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.provider_execution import ProviderAuthorization


def _image_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 12), color=(32, 80, 120)).save(buffer, format="PNG")
    return buffer.getvalue()


def _context(raw: bytes | None = None) -> BoundedContext:
    content = raw if raw is not None else _image_bytes()
    source = SourceReference(
        path="art/reference.png",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        purpose="approved style reference",
    )
    return BoundedContext((ContextItem(source, content),), len(content))


def _contract(context: BoundedContext | None = None, **overrides):
    selected = context or _context()
    values = {
        "schema_version": "1.0.0",
        "task_id": "vision-task-1",
        "selected_agent_id": "provider.openai.vision-review",
        "task_type": "vision.review",
        "kind": AgentKind.VISION,
        "objective": "Assess whether the scene matches the supplied reference.",
        "project_id": "project-1",
        "prompt_template": PromptTemplateRef(template_id="openai.vision", version="1.0.0"),
        "sources": selected.sources,
        "required_capabilities": (CapabilityRequirement(name="vision.review"),),
        "allowed_tools": ("openai.responses.vision",),
        "tool_constraints": ToolConstraints(
            allowed_tools=("openai.responses.vision",),
            max_tool_calls=1,
            timeout_seconds=20,
            network_allowed=True,
        ),
        "cost_constraints": CostConstraints(
            max_amount=2, currency="USD", unit="request", cost_class="PAID"
        ),
        "max_output_bytes": 0,
        "max_output_files": 0,
    }
    values.update(overrides)
    return AgentTaskContract(**values)


class Verifier:
    def __init__(self, allowed: bool = True):
        self.allowed = allowed
        self.calls = 0

    def verify(self, authorization, provider_id, request_fingerprint, operation_hash):
        self.calls += 1
        return (
            self.allowed
            and provider_id == "provider.openai.vision-review"
            and request_fingerprint == "a" * 64
        )


class FakeTransport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, *, headers, body, timeout_seconds, max_response_bytes):
        self.calls.append((url, headers, json.loads(body), timeout_seconds, max_response_bytes))
        if self.error:
            raise self.error
        return self.response


def _config() -> OpenAIVisionConfig:
    return OpenAIVisionConfig(
        model_id="operator-selected-vision-model",
        credential_env_name="OPENAI_API_KEY",
        cost=CostConstraints(max_amount=2, currency="USD", unit="request", cost_class="PAID"),
    )


def _authorization() -> ProviderAuthorization:
    return ProviderAuthorization(
        approval_id="approval-1",
        operation_hash="b" * 64,
        request_fingerprint="a" * 64,
        max_cost=2,
        currency="USD",
        unit="request",
        cost_class="PAID",
    )


def _response(body: dict, headers: dict[str, str] | None = None) -> HttpResponse:
    return HttpResponse(status=200, headers=headers or {}, body=json.dumps(body).encode())


def _completed(text: str) -> dict:
    return {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }


def test_authorized_review_posts_selected_image_and_returns_non_authoritative_review(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-secret-value")
    payload = json.dumps(
        {
            "conclusion": "LIKELY_MATCH",
            "findings": ["Palette and silhouette are similar"],
            "criteria": ["palette", "silhouette"],
        }
    )
    transport = FakeTransport(_response(_completed(payload), {"x-request-id": "receipt-123"}))
    provider = OpenAIVisionReviewProvider(_config(), transport=transport)
    verifier = Verifier()

    result = provider.execute(
        _contract(), _context(), tmp_path / "unused", "a" * 64, _authorization(), verifier
    )

    assert result.visual_review.task_id == "vision-task-1"
    assert result.visual_review.provider_id == provider.provider_id
    assert result.visual_review.reviewed_sources[0].sha256 == _context().items[0].source.sha256
    assert result.visual_review.authoritative_approval is False
    assert result.actual_cost is None and result.external_id is None
    assert result.metadata["receipt_id"] == "receipt-123"
    url, headers, request, timeout, max_response = transport.calls[0]
    assert url == "https://api.openai.com/v1/responses"
    assert headers["Authorization"] == "Bearer test-key-secret-value"
    assert request["store"] is False and request["tools"] == []
    assert request["input"][0]["content"][1]["type"] == "input_image"
    assert request["input"][0]["content"][1]["image_url"].startswith("data:image/png;base64,")
    assert request["text"]["format"]["strict"] is True
    assert timeout == 20 and max_response == provider.config.max_response_bytes
    assert verifier.calls == 1


def test_invalid_image_or_denied_durable_authorization_never_posts(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-secret-value")
    transport = FakeTransport(_response(_completed("{}")))
    provider = OpenAIVisionReviewProvider(_config(), transport=transport)
    bad_verifier = Verifier(allowed=False)
    with pytest.raises(ValidationError):
        provider.execute(
            _contract(), _context(), tmp_path / "unused", "a" * 64, _authorization(), bad_verifier
        )
    assert transport.calls == []

    bad = _context(b"not an image")
    with pytest.raises(ValidationError):
        provider.execute(
            _contract(bad), bad, tmp_path / "unused", "a" * 64, _authorization(), Verifier()
        )
    assert transport.calls == []


def test_refusal_or_malformed_structured_result_is_failed_without_leaking_secret(
    monkeypatch, tmp_path
):
    secret = "test-key-secret-value"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    body = {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "refusal", "refusal": secret}]}],
    }
    provider = OpenAIVisionReviewProvider(_config(), transport=FakeTransport(_response(body)))

    result = provider.execute(
        _contract(), _context(), tmp_path / "unused", "a" * 64, _authorization(), Verifier()
    )

    assert result.status.value == "FAILED"
    assert secret not in json.dumps(result.to_dict())

    echoed = json.dumps(
        {"conclusion": "POSSIBLE_ISSUE", "findings": [secret], "criteria": ["palette"]}
    )
    provider = OpenAIVisionReviewProvider(
        _config(), transport=FakeTransport(_response(_completed(echoed)))
    )
    result = provider.execute(
        _contract(), _context(), tmp_path / "unused", "a" * 64, _authorization(), Verifier()
    )
    assert result.status.value == "FAILED"
    assert secret not in json.dumps(result.to_dict())


def test_lost_response_is_uncertain_and_never_retried(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-secret-value")
    transport = FakeTransport(error=TimeoutError("response lost"))
    provider = OpenAIVisionReviewProvider(_config(), transport=transport)

    result = provider.execute(
        _contract(), _context(), tmp_path / "unused", "a" * 64, _authorization(), Verifier()
    )

    assert result.status.value == "UNCERTAIN"
    assert result.external_id is None
    assert len(transport.calls) == 1
