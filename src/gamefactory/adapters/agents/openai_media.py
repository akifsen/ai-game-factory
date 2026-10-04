"""Optional official OpenAI image and speech providers over bounded HTTPS."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import ssl
import wave
from dataclasses import dataclass
from http.client import HTTPSConnection
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from PIL import Image, UnidentifiedImageError
from pydantic import ConfigDict, Field, field_validator

from gamefactory.adapters.agents.base import (
    ProviderReadiness,
    ProviderStatus,
    stable_config_fingerprint,
    validate_context,
    validate_provider_cost,
    verify_execution_authorization,
)
from gamefactory.adapters.agents.codex import _prepare_fresh_workspace, _stage_bytes
from gamefactory.core.domain.agent_contracts import (
    AgentKind,
    AgentOutcome,
    AgentResultProposal,
    AgentTaskContract,
    CostConstraints,
    EvidenceClaim,
    ProposedFile,
    StrictModel,
)
from gamefactory.core.domain.errors import ProviderUnavailable, ValidationError
from gamefactory.core.domain.provider_execution import (
    AuthorizationVerifier,
    ProviderAuthorization,
    ProviderExecutionStatus,
    ProviderOutputFile,
    ProviderRun,
)

_IMAGE_URL = "https://api.openai.com/v1/images/generations"
_AUDIO_URL = "https://api.openai.com/v1/audio/speech"
_MAX_PROMPT_BYTES = 64_000
_MAX_IMAGE_BYTES = 64 * 1024 * 1024
_MAX_AUDIO_BYTES = 64 * 1024 * 1024


class HttpResponse(StrictModel):
    status: int = Field(ge=100, le=599)
    headers: dict[str, str] = Field(default_factory=dict)
    body: bytes


class HttpTransport(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> HttpResponse: ...


class OpenAIMediaConfig(StrictModel):
    """Operator settings; model ids and credential environment-variable names are required."""

    model_id: str = Field(min_length=1, max_length=128)
    credential_env_name: str = Field(min_length=1, max_length=128)
    cost: CostConstraints
    timeout_seconds: float = Field(default=120, gt=0, le=600, allow_inf_nan=False)
    max_prompt_chars: int = Field(default=4000, ge=1, le=16_000)
    max_image_bytes: int = Field(default=_MAX_IMAGE_BYTES, ge=1024, le=_MAX_IMAGE_BYTES)
    max_audio_bytes: int = Field(default=_MAX_AUDIO_BYTES, ge=1024, le=_MAX_AUDIO_BYTES)
    max_image_pixels: int = Field(default=16_000_000, ge=1_000_000, le=32_000_000)
    max_audio_seconds: int = Field(default=300, ge=1, le=1800)

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    @field_validator("credential_env_name")
    @classmethod
    def secret_env_reference(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value) or not any(
            word in value.upper() for word in ("KEY", "TOKEN", "SECRET", "AUTH", "CREDENTIAL")
        ):
            raise ValueError(
                "credential_env_name must refer to a secret environment variable by name"
            )
        return value

    @field_validator("model_id")
    @classmethod
    def configured_model(cls, value: str) -> str:
        if not value or value.lower() in {"latest", "default", "auto"}:
            raise ValueError("an explicit model id is required; aliases are not accepted")
        return value


@dataclass(frozen=True)
class _Response:
    status: int
    headers: dict[str, str]
    body: bytes


class _StdlibHttpsTransport:
    """Single HTTPS POST with no redirects, retries, cookies or proxy forwarding."""

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.openai.com"
            or parsed.port not in {None, 443}
        ):
            raise ValidationError("OpenAI provider endpoint is fixed to official HTTPS API host")
        connection = HTTPSConnection(
            parsed.hostname, 443, timeout=timeout_seconds, context=ssl.create_default_context()
        )
        try:
            connection.request("POST", parsed.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(max_response_bytes + 1)
            if len(payload) > max_response_bytes:
                raise ValidationError("OpenAI response exceeds the configured byte limit")
            return HttpResponse(
                status=response.status,
                headers={key.lower(): value for key, value in response.getheaders()},
                body=payload,
            )
        finally:
            connection.close()


class _OutcomeUncertain(Exception):
    """A request may have reached the provider, but no result was received."""


class _OpenAIBase:
    capability: str
    tool_name: str
    kind: AgentKind

    def __init__(
        self,
        config: OpenAIMediaConfig,
        *,
        provider_id: str,
        transport: HttpTransport | None = None,
    ) -> None:
        if not provider_id.strip():
            raise ValidationError("OpenAI provider_id is required")
        self.config = config
        self.provider_id = provider_id
        self.capabilities = frozenset({self.capability})
        self.transport = transport or _StdlibHttpsTransport()

    @property
    def config_fingerprint(self) -> str:
        return stable_config_fingerprint(
            {
                "provider_id": self.provider_id,
                "provider_type": type(self).__name__,
                "model_id": self.config.model_id,
                "credential_env_name": self.config.credential_env_name,
                "cost": self.config.cost.model_dump(mode="json"),
                "timeout_seconds": self.config.timeout_seconds,
                "max_prompt_chars": self.config.max_prompt_chars,
                "max_image_bytes": self.config.max_image_bytes,
                "max_audio_bytes": self.config.max_audio_bytes,
                "max_image_pixels": self.config.max_image_pixels,
                "max_audio_seconds": self.config.max_audio_seconds,
                "endpoint": self.endpoint,
                "request_contract": "openai-media-1.0.0",
            }
        )

    def readiness(self) -> ProviderReadiness:
        if not os.environ.get(self.config.credential_env_name):
            return ProviderReadiness(
                self.provider_id,
                ProviderStatus.CREDENTIAL_MISSING,
                self.capabilities,
                reason="Configured credential environment variable is absent",
                missing_credentials=(self.config.credential_env_name,),
            )
        return ProviderReadiness(
            self.provider_id,
            ProviderStatus.NOT_VERIFIED,
            self.capabilities,
            reason="Credential name is present; no network probe or paid generation was performed",
            live_execution_verified=False,
        )

    @property
    def is_configured(self) -> bool:
        """Credential-name presence is checked locally; no API request is made."""
        return self.readiness().status in {ProviderStatus.AVAILABLE, ProviderStatus.NOT_VERIFIED}

    def _authorize(
        self,
        contract: AgentTaskContract,
        fingerprint: str,
        authorization: ProviderAuthorization,
        verifier: AuthorizationVerifier,
    ) -> str:
        if (
            contract.selected_agent_id is not None
            and contract.selected_agent_id != self.provider_id
        ):
            raise ValidationError(
                "Selected agent identity does not match the registered OpenAI provider"
            )
        if contract.kind != self.kind:
            raise ValidationError("Task kind does not match configured OpenAI media provider")
        if contract.max_output_files < 1 or contract.max_output_bytes < 1:
            raise ValidationError("Task contract does not permit a media output")
        expected_template = "openai.image" if self.kind == AgentKind.IMAGE else "openai.speech"
        if (
            contract.prompt_template.template_id != expected_template
            or contract.prompt_template.version != "1.0.0"
        ):
            raise ValidationError(
                "OpenAI media provider requires its fixed, versioned task template"
            )
        if (
            self.tool_name not in contract.allowed_tools
            or self.tool_name not in contract.tool_constraints.allowed_tools
            or self.tool_name in contract.forbidden_tools
            or contract.tool_constraints.max_tool_calls < 1
            or not contract.tool_constraints.network_allowed
        ):
            raise ValidationError("OpenAI provider requires one explicitly allowed network call")
        required = {item.name for item in contract.required_capabilities if item.required}
        if not required.issubset(self.capabilities):
            raise ValidationError("OpenAI provider lacks required task capabilities")
        if self.config.cost.cost_class == "LOCAL":
            raise ValidationError(
                "A hosted OpenAI provider cannot be represented as local-cost execution"
            )
        validate_provider_cost(self.config.cost, contract)
        verify_execution_authorization(
            provider_id=self.provider_id,
            contract=contract,
            request_fingerprint=fingerprint,
            authorization=authorization,
            verifier=verifier,
        )
        secret = os.environ.get(self.config.credential_env_name)
        if not secret:
            raise ProviderUnavailable(
                "Configured OpenAI credential is absent", provider=self.provider_id
            )
        return secret

    @property
    def endpoint(self) -> str:
        raise NotImplementedError

    def _post(
        self, endpoint: str, secret: str, payload: dict[str, Any], max_bytes: int, timeout: float
    ) -> HttpResponse:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = self.transport.post(
            endpoint,
            headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
            body=body,
            timeout_seconds=min(self.config.timeout_seconds, timeout),
            max_response_bytes=max_bytes,
        )
        if response.status < 200 or response.status >= 300:
            # Do not surface provider response bodies or request headers; they may contain secrets.
            if response.status >= 500:
                raise _OutcomeUncertain("provider returned a server error after request submission")
            raise ValidationError(f"OpenAI provider returned HTTP status {response.status}")
        return response


class OpenAIImageProvider(_OpenAIBase):
    """Image generation provider using the documented Images generation API."""

    capability = "image.generate"
    tool_name = "openai.images.generate"
    kind = AgentKind.IMAGE

    def __init__(
        self,
        config: OpenAIMediaConfig,
        *,
        provider_id: str = "provider.openai.image",
        transport: HttpTransport | None = None,
    ) -> None:
        super().__init__(config, provider_id=provider_id, transport=transport)

    @property
    def endpoint(self) -> str:
        return _IMAGE_URL

    def execute(
        self,
        contract: AgentTaskContract,
        context: Any,
        workspace: Path,
        request_fingerprint: str,
        authorization: ProviderAuthorization,
        authorization_verifier: AuthorizationVerifier,
    ) -> ProviderRun:
        validate_context(contract, context)
        output_path = _single_output_path(contract, ".png")
        for item in context.items:
            # The exact existing output target is supplied for its approved
            # before-hash only. It is not silently treated as model input.
            if item.source.path == output_path:
                continue
            try:
                item.content.decode("utf-8")
            except UnicodeDecodeError as err:
                raise ValidationError(
                    "Image generation accepts text-only context; binary references are unsupported"
                ) from err
        prompt = _task_prompt(
            contract, context, self.config.max_prompt_chars, excluded_path=output_path
        )
        if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
            raise ValidationError("Image generation prompt exceeds the 64 KB limit")
        run_root = _prepare_fresh_workspace(workspace)
        secret = self._authorize(
            contract, request_fingerprint, authorization, authorization_verifier
        )
        try:
            response = self._post(
                self.endpoint,
                secret,
                {
                    "model": self.config.model_id,
                    "prompt": prompt,
                    "n": 1,
                    "output_format": "png",
                },
                min(
                    90_000_000,
                    min(self.config.max_image_bytes, contract.max_output_bytes) * 4 // 3
                    + 1_000_000,
                ),
                contract.tool_constraints.timeout_seconds,
            )
        except (_OutcomeUncertain, OSError, TimeoutError):
            return _uncertain_run(
                self.provider_id, self.config.model_id, "image_generation_outcome_unknown"
            )
        try:
            envelope = json.loads(response.body)
            data = envelope["data"]
            if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
                raise ValueError("expected exactly one image")
            content = base64.b64decode(data[0]["b64_json"], validate=True)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as err:
            raise ValidationError(
                "OpenAI image response did not contain one valid base64 image"
            ) from err
        if not content or len(content) > self.config.max_image_bytes:
            raise ValidationError("OpenAI image response is empty or exceeds its byte limit")
        if len(content) > contract.max_output_bytes:
            return _output_limit_run(
                self.provider_id, self.config.model_id, "image_output_exceeds_task_limit"
            )
        width, height = _validate_image(content, self.config.max_image_pixels)
        if secret.encode("utf-8") in content:
            raise ValidationError("Generated media appears to contain credential material")
        staged = _stage_bytes(run_root, "outputs/image-000.png", content)
        proposal = _proposal(
            contract,
            self.provider_id,
            f"Generated a PNG image ({width}x{height}) with configured OpenAI model",
            output_path,
            content,
        )
        return ProviderRun(
            schema_version="provider-run-1.0.0",
            status=ProviderExecutionStatus.COMPLETED,
            proposal=proposal,
            files=(
                ProviderOutputFile(
                    path=output_path,
                    content_base64=base64.b64encode(content).decode("ascii"),
                    sha256=hashlib.sha256(content).hexdigest(),
                    media_type="image/png",
                    expected_before_sha256=_expected_before_hash(contract, output_path),
                    staged_path=staged.relative_to(workspace.resolve()).as_posix(),
                ),
            ),
            actual_cost=None,
            external_id=None,
            metadata={
                "provider_id": self.provider_id,
                "model_id": self.config.model_id,
                "operation": "images.generations",
                "width": width,
                "height": height,
                "receipt_id": _request_id(response.headers),
                "prompt_template": "openai-image-1.0.0",
            },
        )


class OpenAIAudioSpeechProvider(_OpenAIBase):
    """Speech synthesis provider; it advertises audio.speech only (not music/SFX)."""

    capability = "audio.speech"
    tool_name = "openai.audio.speech"
    kind = AgentKind.AUDIO

    def __init__(
        self,
        config: OpenAIMediaConfig,
        *,
        voice: str,
        provider_id: str = "provider.openai.audio-speech",
        transport: HttpTransport | None = None,
    ) -> None:
        if not voice.strip() or len(voice) > 128:
            raise ValidationError("Speech voice must be explicitly configured")
        self.voice = voice
        super().__init__(config, provider_id=provider_id, transport=transport)

    @property
    def endpoint(self) -> str:
        return _AUDIO_URL

    @property
    def config_fingerprint(self) -> str:
        return stable_config_fingerprint(
            {
                "provider_id": self.provider_id,
                "provider_type": type(self).__name__,
                "model_id": self.config.model_id,
                "voice": self.voice,
                "credential_env_name": self.config.credential_env_name,
                "cost": self.config.cost.model_dump(mode="json"),
                "timeout_seconds": self.config.timeout_seconds,
                "endpoint": self.endpoint,
                "response_format": "wav",
                "request_contract": "openai-media-1.0.0",
                "max_prompt_chars": self.config.max_prompt_chars,
                "max_image_bytes": self.config.max_image_bytes,
                "max_audio_bytes": self.config.max_audio_bytes,
                "max_image_pixels": self.config.max_image_pixels,
                "max_audio_seconds": self.config.max_audio_seconds,
            }
        )

    def execute(
        self,
        contract: AgentTaskContract,
        context: Any,
        workspace: Path,
        request_fingerprint: str,
        authorization: ProviderAuthorization,
        authorization_verifier: AuthorizationVerifier,
    ) -> ProviderRun:
        validate_context(contract, context)
        text = contract.objective
        if len(text) > 4096:
            raise ValidationError("Speech text exceeds the provider's bounded input length")
        output_path = _single_output_path(contract, ".wav")
        run_root = _prepare_fresh_workspace(workspace)
        secret = self._authorize(
            contract, request_fingerprint, authorization, authorization_verifier
        )
        try:
            response = self._post(
                self.endpoint,
                secret,
                {
                    "model": self.config.model_id,
                    "input": text,
                    "voice": self.voice,
                    "response_format": "wav",
                },
                self.config.max_audio_bytes,
                contract.tool_constraints.timeout_seconds,
            )
        except (_OutcomeUncertain, OSError, TimeoutError):
            return _uncertain_run(
                self.provider_id, self.config.model_id, "speech_generation_outcome_unknown"
            )
        content = response.body
        if not content or len(content) > self.config.max_audio_bytes:
            raise ValidationError("OpenAI speech response is empty or exceeds its byte limit")
        if len(content) > contract.max_output_bytes:
            return _output_limit_run(
                self.provider_id, self.config.model_id, "speech_output_exceeds_task_limit"
            )
        duration, channels, rate = _validate_wav(content, self.config.max_audio_seconds)
        if secret.encode("utf-8") in content:
            raise ValidationError("Generated audio appears to contain credential material")
        staged = _stage_bytes(run_root, "outputs/speech-000.wav", content)
        proposal = _proposal(
            contract,
            self.provider_id,
            f"Generated WAV speech ({duration:.2f}s, {channels} channels, {rate} Hz)",
            output_path,
            content,
        )
        return ProviderRun(
            schema_version="provider-run-1.0.0",
            status=ProviderExecutionStatus.COMPLETED,
            proposal=proposal,
            files=(
                ProviderOutputFile(
                    path=output_path,
                    content_base64=base64.b64encode(content).decode("ascii"),
                    sha256=hashlib.sha256(content).hexdigest(),
                    media_type="audio/wav",
                    expected_before_sha256=_expected_before_hash(contract, output_path),
                    staged_path=staged.relative_to(workspace.resolve()).as_posix(),
                ),
            ),
            actual_cost=None,
            external_id=None,
            metadata={
                "provider_id": self.provider_id,
                "model_id": self.config.model_id,
                "voice": self.voice,
                "operation": "audio.speech",
                "duration_seconds": duration,
                "channels": channels,
                "sample_rate_hz": rate,
                "receipt_id": _request_id(response.headers),
                "prompt_template": "openai-speech-1.0.0",
            },
        )


def _task_prompt(
    contract: AgentTaskContract, context: Any, max_chars: int, *, excluded_path: str | None = None
) -> str:
    chunks = [contract.objective]
    if contract.acceptance_criteria:
        chunks.append("Acceptance: " + "; ".join(contract.acceptance_criteria))
    for item in context.items:
        if item.source.path == excluded_path:
            continue
        try:
            text = item.content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        chunks.append(f"[{item.source.path} — {item.source.purpose}]\n{text}")
    prompt = "\n\n".join(chunks)
    if len(prompt) > max_chars:
        raise ValidationError("Media prompt exceeds configured character limit")
    return prompt


def _uncertain_run(provider_id: str, model_id: str, reason: str) -> ProviderRun:
    return ProviderRun(
        schema_version="provider-run-1.0.0",
        status=ProviderExecutionStatus.UNCERTAIN,
        proposal=None,
        external_id=None,
        metadata={
            "provider_id": provider_id,
            "model_id": model_id,
            "outcome": "UNCERTAIN",
            "reason": reason,
            "retry_automatically": False,
        },
    )


def _output_limit_run(provider_id: str, model_id: str, reason: str) -> ProviderRun:
    return ProviderRun(
        schema_version="provider-run-1.0.0",
        status=ProviderExecutionStatus.FAILED,
        proposal=None,
        external_id=None,
        metadata={
            "provider_id": provider_id,
            "model_id": model_id,
            "outcome": "FAILED",
            "reason": reason,
            "retry_automatically": False,
        },
    )


def _single_output_path(contract: AgentTaskContract, suffix: str) -> str:
    if len(contract.allowed_output_paths) != 1:
        raise ValidationError("OpenAI media requests require exactly one explicit output path")
    path = contract.allowed_output_paths[0]
    if not path.lower().endswith(suffix):
        raise ValidationError("Output path suffix does not match provider media type")
    return path


def _proposal(
    contract: AgentTaskContract, provider_id: str, summary: str, output_path: str, content: bytes
) -> AgentResultProposal:
    required = tuple(
        EvidenceClaim(
            evidence_type=kind,
            description="Provider returned validated media bytes; Factory verification remains pending",
        )
        for kind in contract.required_evidence_types
    )
    before = _expected_before_hash(contract, output_path)
    encoded = base64.b64encode(content).decode("ascii")
    proposed_file = ProposedFile(
        path=output_path,
        operation="UPDATE" if before is not None else "CREATE",
        content_base64=encoded,
        before_sha256=before,
        output_sha256=hashlib.sha256(content).hexdigest(),
    )
    return AgentResultProposal(
        schema_version="1.0.0",
        task_id=contract.task_id,
        agent_id=provider_id,
        outcome=AgentOutcome.PROPOSED,
        summary=summary,
        evidence=required,
        proposed_files=(proposed_file,),
        requested_capabilities=tuple(
            item.name for item in contract.required_capabilities if item.required
        ),
        requested_tools=(
            "openai.images.generate" if contract.kind == AgentKind.IMAGE else "openai.audio.speech",
        ),
    )


def _expected_before_hash(contract: AgentTaskContract, output_path: str) -> str | None:
    """Return the exact registered source hash used by both proposal and output envelope."""
    return next((source.sha256 for source in contract.sources if source.path == output_path), None)


def _validate_image(content: bytes, max_pixels: int) -> tuple[int, int]:
    try:
        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > max_pixels:
                raise ValidationError(
                    "Generated image dimensions exceed the configured decoded pixel limit"
                )
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            if image.format != "PNG":
                raise ValidationError("Generated output_format=png returned non-PNG media")
            return image.size
    except (UnidentifiedImageError, OSError, ValueError) as err:
        if isinstance(err, ValidationError):
            raise
        raise ValidationError("Generated image is corrupt or unsupported") from err


def _validate_wav(content: bytes, max_seconds: int) -> tuple[float, int, int]:
    try:
        with wave.open(io.BytesIO(content), "rb") as stream:
            channels = stream.getnchannels()
            rate = stream.getframerate()
            frames = stream.getnframes()
            width = stream.getsampwidth()
            duration = frames / rate if rate else 0
            if channels not in {1, 2} or rate < 8000 or rate > 192000 or width not in {1, 2, 3, 4}:
                raise ValidationError(
                    "WAV channel, sample-rate, or sample-width is outside safe bounds"
                )
            if frames <= 0 or duration <= 0 or duration > max_seconds:
                raise ValidationError("WAV duration is empty or exceeds the configured limit")
            pcm = stream.readframes(frames)
            if len(pcm) != frames * channels * width:
                raise ValidationError("WAV frame data is truncated")
            return duration, channels, rate
    except (wave.Error, EOFError) as err:
        raise ValidationError("Generated WAV is corrupt or unsupported") from err


def _request_id(headers: dict[str, str]) -> str | None:
    value = headers.get("x-request-id")
    return (
        value[:128]
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value)
        else None
    )
