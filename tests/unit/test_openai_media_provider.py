from __future__ import annotations

import base64
import hashlib
import io
import json
import wave

import pytest
from PIL import Image
from pydantic import ValidationError

from gamefactory.adapters.agents.openai_media import (
    HttpResponse,
    OpenAIAudioSpeechProvider,
    OpenAIImageProvider,
    OpenAIMediaConfig,
    _validate_image,
    _validate_wav,
)
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
from gamefactory.core.domain.errors import ValidationError as FactoryValidationError
from gamefactory.core.domain.provider_execution import ProviderAuthorization


class RecordingTransport:
    def __init__(self, response: HttpResponse):
        self.response = response
        self.requests = []

    def post(self, url, *, headers, body, timeout_seconds, max_response_bytes):
        self.requests.append((url, headers, json.loads(body), timeout_seconds, max_response_bytes))
        return self.response


def config():
    return OpenAIMediaConfig(
        model_id="configured-image-model",
        credential_env_name="OPENAI_API_KEY",
        cost=CostConstraints(max_amount=1, currency="USD", unit="request", cost_class="PAID"),
    )


def test_image_media_is_fully_decoded_and_dimensions_bounded():
    image = Image.new("RGB", (3, 2), "blue")
    output = io.BytesIO()
    image.save(output, format="PNG")
    assert _validate_image(output.getvalue(), 6) == (3, 2)
    with pytest.raises(FactoryValidationError):
        _validate_image(b"\x89PNG\r\n\x1a\n" + b"not-an-image", 100)
    with pytest.raises(FactoryValidationError):
        _validate_image(output.getvalue(), 5)


def test_wav_parser_rejects_header_only_or_truncated_audio():
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 10)
    assert _validate_wav(output.getvalue(), 1)[1:] == (1, 16000)
    with pytest.raises(FactoryValidationError):
        _validate_wav(output.getvalue()[:44], 1)


class AllowVerifiedIntent:
    def verify(self, authorization, provider_id, request_fingerprint, operation_hash):
        return (
            authorization.approval_id == "approval-1"
            and provider_id in {"provider.openai.image", "provider.openai.audio-speech"}
            and request_fingerprint == authorization.request_fingerprint
            and operation_hash == authorization.operation_hash
        )


def image_contract(**overrides):
    values = {
        "schema_version": "1.0.0",
        "task_id": "task-1",
        "task_type": "image.generate",
        "kind": AgentKind.IMAGE,
        "objective": "Make a blue square",
        "project_id": "project-1",
        "prompt_template": PromptTemplateRef(template_id="openai.image", version="1.0.0"),
        "required_capabilities": (CapabilityRequirement(name="image.generate"),),
        "allowed_tools": ("openai.images.generate",),
        "tool_constraints": ToolConstraints(
            allowed_tools=("openai.images.generate",),
            max_tool_calls=1,
            timeout_seconds=10,
            network_allowed=True,
        ),
        "cost_constraints": CostConstraints(
            max_amount=1, currency="USD", unit="request", cost_class="PAID"
        ),
        "allowed_output_paths": ("assets/generated.png",),
    }
    values.update(overrides)
    return AgentTaskContract(**values)


def authorization():
    return ProviderAuthorization(
        approval_id="approval-1",
        operation_hash="b" * 64,
        request_fingerprint="a" * 64,
        max_cost=1,
        currency="USD",
        unit="request",
        cost_class="PAID",
    )


def image_bytes():
    image = Image.new("RGB", (3, 2), "blue")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def speech_contract(**overrides):
    values = {
        "schema_version": "1.0.0",
        "task_id": "task-2",
        "task_type": "audio.speech",
        "kind": AgentKind.AUDIO,
        "objective": "A short test phrase",
        "project_id": "project-1",
        "prompt_template": PromptTemplateRef(template_id="openai.speech", version="1.0.0"),
        "required_capabilities": (CapabilityRequirement(name="audio.speech"),),
        "allowed_tools": ("openai.audio.speech",),
        "tool_constraints": ToolConstraints(
            allowed_tools=("openai.audio.speech",),
            max_tool_calls=1,
            timeout_seconds=10,
            network_allowed=True,
        ),
        "cost_constraints": CostConstraints(
            max_amount=1, currency="USD", unit="request", cost_class="PAID"
        ),
        "allowed_output_paths": ("audio/voice.wav",),
    }
    values.update(overrides)
    return AgentTaskContract(**values)


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 160)
    return output.getvalue()


def test_remote_adapters_require_verified_workflow_authorization_before_call(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-fixture")
    transport = RecordingTransport(HttpResponse(status=200, body=b'{"data":[]}'))
    provider = OpenAIImageProvider(config(), transport=transport)
    with pytest.raises(FactoryValidationError):
        provider.execute(
            image_contract(), BoundedContext((), 0), tmp_path / "fresh", "a" * 64, None, None
        )
    assert transport.requests == []


def test_model_id_and_secret_reference_are_explicit():
    with pytest.raises(ValidationError):
        OpenAIMediaConfig(
            model_id="latest", credential_env_name="OPENAI_API_KEY", cost=CostConstraints()
        )
    with pytest.raises(ValidationError):
        OpenAIMediaConfig(
            model_id="model-x", credential_env_name="KEY-INVALID", cost=CostConstraints()
        )


def test_speech_provider_advertises_speech_only():
    provider = OpenAIAudioSpeechProvider(config(), voice="configured-voice")
    assert provider.capabilities == frozenset({"audio.speech"})


def test_authorized_image_request_binds_output_and_contract_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-fixture")
    png = image_bytes()
    payload = json.dumps({"data": [{"b64_json": base64.b64encode(png).decode()}]}).encode()
    transport = RecordingTransport(
        HttpResponse(status=200, body=payload, headers={"x-request-id": "receipt-42"})
    )
    provider = OpenAIImageProvider(config(), transport=transport)
    run = provider.execute(
        image_contract(),
        BoundedContext((), 0),
        tmp_path / "image-work",
        "a" * 64,
        authorization(),
        AllowVerifiedIntent(),
    )
    assert run.files[0].content_bytes() == png
    assert run.files[0].sha256 == hashlib.sha256(png).hexdigest()
    assert run.external_id is None  # A request receipt is not a queryable generation id.
    assert run.metadata["receipt_id"] == "receipt-42"
    assert transport.requests[0][0] == "https://api.openai.com/v1/images/generations"
    assert transport.requests[0][2]["n"] == 1
    assert transport.requests[0][2]["output_format"] == "png"
    assert transport.requests[0][3] == 10


def test_invalid_image_scope_and_http_failure_never_retry(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-fixture")
    transport = RecordingTransport(HttpResponse(status=503, body=b"secret body"))
    provider = OpenAIImageProvider(config(), transport=transport)
    with pytest.raises(FactoryValidationError):
        provider.execute(
            image_contract(allowed_output_paths=("assets/output.wav",)),
            BoundedContext((), 0),
            tmp_path / "invalid-scope",
            "a" * 64,
            authorization(),
            AllowVerifiedIntent(),
        )
    assert transport.requests == []
    run = provider.execute(
        image_contract(),
        BoundedContext((), 0),
        tmp_path / "http-error",
        "a" * 64,
        authorization(),
        AllowVerifiedIntent(),
    )
    assert len(transport.requests) == 1
    assert run.status.value == "UNCERTAIN"
    assert run.metadata["retry_automatically"] is False


def test_lost_http_response_is_uncertain_without_automatic_retry(monkeypatch, tmp_path):
    class LostResponse:
        def __init__(self):
            self.calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            raise TimeoutError("transport timeout")

    monkeypatch.setenv("OPENAI_API_KEY", "secret-fixture")
    transport = LostResponse()
    provider = OpenAIImageProvider(config(), transport=transport)
    run = provider.execute(
        image_contract(),
        BoundedContext((), 0),
        tmp_path / "lost-response",
        "a" * 64,
        authorization(),
        AllowVerifiedIntent(),
    )
    assert run.status.value == "UNCERTAIN"
    assert transport.calls == 1


def test_authorized_speech_returns_validated_wav_and_expected_api_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-fixture")
    wav = wav_bytes()
    transport = RecordingTransport(
        HttpResponse(status=200, body=wav, headers={"x-request-id": "receipt-audio"})
    )
    provider = OpenAIAudioSpeechProvider(config(), voice="operator-voice", transport=transport)
    run = provider.execute(
        speech_contract(),
        BoundedContext((), 0),
        tmp_path / "speech-work",
        "a" * 64,
        authorization(),
        AllowVerifiedIntent(),
    )
    assert run.files[0].content_bytes() == wav
    assert run.files[0].media_type == "audio/wav"
    assert transport.requests[0][0] == "https://api.openai.com/v1/audio/speech"
    assert transport.requests[0][2] == {
        "model": "configured-image-model",
        "input": "A short test phrase",
        "voice": "operator-voice",
        "response_format": "wav",
    }
    assert run.external_id is None


def test_authorized_image_update_binds_same_before_hash_and_preserves_original(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-fixture")
    original = image_bytes()
    output_path = "assets/generated.png"
    existing = tmp_path / "project" / output_path
    existing.parent.mkdir(parents=True)
    existing.write_bytes(original)
    source = SourceReference(
        path=output_path,
        sha256=hashlib.sha256(original).hexdigest(),
        size_bytes=len(original),
        purpose="existing output baseline for update",
    )
    context = BoundedContext((ContextItem(source, original),), len(original))
    generated = image_bytes()
    response = HttpResponse(
        status=200,
        body=json.dumps(
            {
                "data": [{"b64_json": base64.b64encode(generated).decode()}],
            }
        ).encode(),
    )
    provider = OpenAIImageProvider(config(), transport=RecordingTransport(response))

    run = provider.execute(
        image_contract(sources=(source,)),
        context,
        tmp_path / "image-update-work",
        "a" * 64,
        authorization(),
        AllowVerifiedIntent(),
    )

    assert run.proposal.proposed_files[0].operation == "UPDATE"
    assert run.proposal.proposed_files[0].before_sha256 == source.sha256
    assert run.files[0].expected_before_sha256 == source.sha256
    assert run.files[0].sha256 == run.proposal.proposed_files[0].output_sha256
    assert existing.read_bytes() == original


def test_authorized_speech_update_binds_same_before_hash_and_preserves_original(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-fixture")
    original = wav_bytes()
    output_path = "audio/voice.wav"
    existing = tmp_path / "project" / output_path
    existing.parent.mkdir(parents=True)
    existing.write_bytes(original)
    source = SourceReference(
        path=output_path,
        sha256=hashlib.sha256(original).hexdigest(),
        size_bytes=len(original),
        purpose="existing output baseline for update",
    )
    context = BoundedContext((ContextItem(source, original),), len(original))
    generated = wav_bytes()
    provider = OpenAIAudioSpeechProvider(
        config(),
        voice="operator-voice",
        transport=RecordingTransport(HttpResponse(status=200, body=generated)),
    )

    run = provider.execute(
        speech_contract(sources=(source,)),
        context,
        tmp_path / "speech-update-work",
        "a" * 64,
        authorization(),
        AllowVerifiedIntent(),
    )

    assert run.proposal.proposed_files[0].operation == "UPDATE"
    assert run.proposal.proposed_files[0].before_sha256 == source.sha256
    assert run.files[0].expected_before_sha256 == source.sha256
    assert run.files[0].sha256 == run.proposal.proposed_files[0].output_sha256
    assert existing.read_bytes() == original


def test_mismatched_media_update_context_is_rejected_before_http_post(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-fixture")
    content = image_bytes()
    path = "assets/generated.png"
    actual = SourceReference(
        path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        purpose="existing output baseline",
    )
    mismatched = SourceReference(
        path=path, sha256="0" * 64, size_bytes=len(content), purpose="different declared baseline"
    )
    context = BoundedContext((ContextItem(actual, content),), len(content))
    image_transport = RecordingTransport(HttpResponse(status=200, body=b""))
    image_provider = OpenAIImageProvider(config(), transport=image_transport)
    with pytest.raises(FactoryValidationError):
        image_provider.execute(
            image_contract(sources=(mismatched,)),
            context,
            tmp_path / "bad-image-update",
            "a" * 64,
            authorization(),
            AllowVerifiedIntent(),
        )
    assert image_transport.requests == []

    wav = wav_bytes()
    audio_path = "audio/voice.wav"
    actual_audio = SourceReference(
        path=audio_path,
        sha256=hashlib.sha256(wav).hexdigest(),
        size_bytes=len(wav),
        purpose="existing output baseline",
    )
    mismatched_audio = SourceReference(
        path=audio_path, sha256="1" * 64, size_bytes=len(wav), purpose="different declared baseline"
    )
    audio_context = BoundedContext((ContextItem(actual_audio, wav),), len(wav))
    audio_transport = RecordingTransport(HttpResponse(status=200, body=b""))
    audio_provider = OpenAIAudioSpeechProvider(
        config(), voice="operator-voice", transport=audio_transport
    )
    with pytest.raises(FactoryValidationError):
        audio_provider.execute(
            speech_contract(sources=(mismatched_audio,)),
            audio_context,
            tmp_path / "bad-audio-update",
            "a" * 64,
            authorization(),
            AllowVerifiedIntent(),
        )
    assert audio_transport.requests == []
