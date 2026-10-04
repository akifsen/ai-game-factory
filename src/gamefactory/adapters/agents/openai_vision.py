"""Optional advisory OpenAI vision review over bounded HTTPS Responses API."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
from http.client import HTTPException
from pathlib import Path
from typing import Any, Literal

from PIL import Image, UnidentifiedImageError
from pydantic import ConfigDict, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from gamefactory.adapters.agents.base import (
    ProviderReadiness,
    ProviderStatus,
    stable_config_fingerprint,
    validate_context,
    validate_provider_cost,
    verify_execution_authorization,
)
from gamefactory.adapters.agents.openai_media import (
    HttpResponse,
    HttpTransport,
    _StdlibHttpsTransport,
)
from gamefactory.core.domain.agent_contracts import (
    AgentKind,
    AgentTaskContract,
    CostConstraints,
    StrictModel,
)
from gamefactory.core.domain.agent_contracts import (
    VisualReview as AgentVisualReview,
)
from gamefactory.core.domain.errors import ProviderUnavailable, ValidationError
from gamefactory.core.domain.provider_execution import (
    AuthorizationVerifier,
    ProviderAuthorization,
    ProviderExecutionStatus,
    ProviderRun,
)
from gamefactory.core.execution.redaction import redactor

_ENDPOINT = "https://api.openai.com/v1/responses"
_PROMPT_PATH = Path(__file__).parents[2] / "resources" / "agents" / "openai_vision_v1.md"
_TEMPLATE_ID = "openai.vision"
_TEMPLATE_VERSION = "1.0.0"
_MAX_TOTAL_IMAGE_BYTES = 16 * 1024 * 1024


class OpenAIVisionConfig(StrictModel):
    model_id: str = Field(min_length=1, max_length=128)
    credential_env_name: str = Field(min_length=1, max_length=128)
    cost: CostConstraints
    timeout_seconds: float = Field(default=120, gt=0, le=600, allow_inf_nan=False)
    max_images: int = Field(default=8, ge=1, le=16)
    max_image_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=_MAX_TOTAL_IMAGE_BYTES)
    max_image_pixels: int = Field(default=16_000_000, ge=1_000_000, le=32_000_000)
    max_context_chars: int = Field(default=8_000, ge=1, le=32_000)
    max_response_bytes: int = Field(default=128_000, ge=1_024, le=1_000_000)
    max_output_tokens: int = Field(default=1_200, ge=64, le=4_000)

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    @field_validator("credential_env_name")
    @classmethod
    def validate_env_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value) or not any(
            word in value.upper() for word in ("KEY", "TOKEN", "SECRET", "AUTH", "CREDENTIAL")
        ):
            raise ValueError("credential_env_name must name an environment secret")
        return value

    @field_validator("model_id")
    @classmethod
    def explicit_model(cls, value: str) -> str:
        if value.lower() in {"latest", "default", "auto"}:
            raise ValueError("vision provider requires an explicit configured model id")
        return value


class _VisionPayload(StrictModel):
    conclusion: Literal["LIKELY_MATCH", "POSSIBLE_ISSUE", "INCONCLUSIVE"]
    findings: tuple[str, ...] = Field(min_length=1, max_length=64)
    criteria: tuple[str, ...] = Field(max_length=64)

    @field_validator("findings", "criteria")
    @classmethod
    def bounded_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            not value.strip() or len(value) > 2_000 for value in values
        ):
            raise ValueError("vision review strings must be unique, non-empty, and bounded")
        return values


class OpenAIVisionReviewProvider:
    """Reviews only explicitly selected, hash-verified PNG/JPEG context images."""

    capability = "vision.review"
    tool_name = "openai.responses.vision"

    def __init__(
        self,
        config: OpenAIVisionConfig,
        *,
        provider_id: str = "provider.openai.vision-review",
        transport: HttpTransport | None = None,
    ) -> None:
        if not provider_id.strip():
            raise ValidationError("OpenAI vision provider_id is required")
        self.config = config
        self.provider_id = provider_id
        self.capabilities = frozenset({self.capability})
        self.transport = transport or _StdlibHttpsTransport()

    @property
    def config_fingerprint(self) -> str:
        prompt_bytes = _read_prompt_asset()
        return stable_config_fingerprint(
            {
                "provider_id": self.provider_id,
                "provider_type": type(self).__name__,
                "model_id": self.config.model_id,
                "credential_env_name": self.config.credential_env_name,
                "cost": self.config.cost.model_dump(mode="json"),
                "endpoint": _ENDPOINT,
                "timeout_seconds": self.config.timeout_seconds,
                "max_images": self.config.max_images,
                "max_image_bytes": self.config.max_image_bytes,
                "max_image_pixels": self.config.max_image_pixels,
                "max_context_chars": self.config.max_context_chars,
                "max_response_bytes": self.config.max_response_bytes,
                "max_output_tokens": self.config.max_output_tokens,
                "prompt_template": f"{_TEMPLATE_ID}/{_TEMPLATE_VERSION}",
                "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
                "request_contract": "openai.responses.vision-1.0.0",
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
            reason="Credential name is present; no network probe or paid review was performed",
            live_execution_verified=False,
        )

    @property
    def is_configured(self) -> bool:
        return self.readiness().status in {ProviderStatus.AVAILABLE, ProviderStatus.NOT_VERIFIED}

    def execute(
        self,
        contract: AgentTaskContract,
        context: Any,
        workspace: Path,
        request_fingerprint: str,
        authorization: ProviderAuthorization,
        authorization_verifier: AuthorizationVerifier,
    ) -> ProviderRun:
        del workspace  # Vision review is read-only and creates no workspace outputs.
        if contract.kind != AgentKind.VISION:
            raise ValidationError("OpenAI vision provider supports vision tasks only")
        if contract.selected_agent_id != self.provider_id:
            raise ValidationError(
                "Selected agent identity does not match the registered vision provider"
            )
        if (
            contract.prompt_template.template_id != _TEMPLATE_ID
            or contract.prompt_template.version != _TEMPLATE_VERSION
        ):
            raise ValidationError(
                "OpenAI vision provider requires its fixed openai.vision 1.0.0 template"
            )
        if (
            contract.allowed_output_paths
            or contract.max_output_files != 0
            or contract.max_output_bytes != 0
        ):
            raise ValidationError(
                "Vision review is read-only and does not accept output file scopes"
            )
        required = {item.name for item in contract.required_capabilities if item.required}
        if (
            self.tool_name not in contract.allowed_tools
            or self.tool_name not in contract.tool_constraints.allowed_tools
            or self.tool_name in contract.forbidden_tools
            or contract.tool_constraints.max_tool_calls < 1
            or not contract.tool_constraints.network_allowed
            or contract.tool_constraints.repository_write_allowed
            or not required.issubset(self.capabilities)
            or self.capability not in required
        ):
            raise ValidationError(
                "Vision review requires one approved network call and no repository writes"
            )
        validate_provider_cost(self.config.cost, contract)
        if self.config.cost.cost_class == "LOCAL":
            raise ValidationError("Hosted OpenAI vision cannot be classified as LOCAL cost")
        if contract.cost_constraints.fallback_max_amount != 0:
            raise ValidationError("Automatic provider fallback is unsupported")
        validate_context(contract, context)
        secret = os.environ.get(self.config.credential_env_name)
        if not secret:
            raise ProviderUnavailable(
                "Configured OpenAI credential is absent", provider=self.provider_id
            )
        prompt_asset = _read_prompt_asset()
        prompt_hash = hashlib.sha256(prompt_asset).hexdigest()
        prompt, image_parts, reviewed_sources = self._prepare_input(
            contract, context, prompt_asset, secret
        )
        if redactor.redact_text(prompt) != prompt:
            raise ValidationError("Vision prompt contains secret-like material")
        schema = {
            "type": "object",
            "properties": {
                "conclusion": {
                    "type": "string",
                    "enum": ["LIKELY_MATCH", "POSSIBLE_ISSUE", "INCONCLUSIVE"],
                },
                "findings": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "minItems": 1,
                    "maxItems": 64,
                },
                "criteria": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "minItems": 0,
                    "maxItems": 64,
                },
            },
            "required": ["conclusion", "findings", "criteria"],
            "additionalProperties": False,
        }
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}, *image_parts]
        payload = {
            "model": self.config.model_id,
            "store": False,
            "tools": [],
            "input": [{"role": "user", "content": content}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "visual_review",
                    "strict": True,
                    "schema": schema,
                }
            },
            "max_output_tokens": self.config.max_output_tokens,
        }
        verify_execution_authorization(
            provider_id=self.provider_id,
            contract=contract,
            request_fingerprint=request_fingerprint,
            authorization=authorization,
            verifier=authorization_verifier,
        )
        try:
            response = self.transport.post(
                _ENDPOINT,
                headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
                body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                timeout_seconds=min(
                    self.config.timeout_seconds, contract.tool_constraints.timeout_seconds
                ),
                max_response_bytes=self.config.max_response_bytes,
            )
        except (OSError, TimeoutError, ConnectionError, HTTPException):
            return self._uncertain("vision_request_outcome_unknown")
        if response.status >= 500:
            return self._uncertain("vision_response_outcome_unknown")
        if response.status < 200 or response.status >= 300:
            return self._failed(f"vision_http_{response.status}")
        if len(response.body) > self.config.max_response_bytes:
            return self._failed("vision_response_exceeded_configured_limit")
        try:
            result = _parse_completed_response(response)
            if secret in "\n".join((*result.findings, *result.criteria)):
                return self._failed("vision_response_contained_credential_material")
            reviewed = AgentVisualReview(
                schema_version="agent-visual-review-1.0.0",
                task_id=contract.task_id,
                provider_id=self.provider_id,
                reviewed_sources=reviewed_sources,
                conclusion=result.conclusion,
                findings=result.findings,
                criteria=result.criteria,
                authoritative_approval=False,
            )
        except (
            ValueError,
            KeyError,
            TypeError,
            json.JSONDecodeError,
            ValidationError,
            PydanticValidationError,
        ):
            return self._failed("vision_response_invalid_or_refused")
        return ProviderRun(
            schema_version="provider-run-1.0.0",
            status=ProviderExecutionStatus.COMPLETED,
            proposal=None,
            visual_review=reviewed,
            actual_cost=None,
            external_id=None,
            metadata={
                "provider_id": self.provider_id,
                "model_id": self.config.model_id,
                "operation": "responses.vision",
                "prompt_template": f"{_TEMPLATE_ID}/{_TEMPLATE_VERSION}",
                "prompt_sha256": prompt_hash,
                "receipt_id": _receipt_id(response.headers),
            },
        )

    def _prepare_input(
        self, contract: AgentTaskContract, context: Any, prompt_asset: bytes, secret: str
    ) -> tuple[str, list[dict[str, Any]], tuple[Any, ...]]:
        images: list[dict[str, Any]] = []
        sources = []
        text_parts = [prompt_asset.decode("utf-8"), f"Task: {contract.objective}"]
        text_parts.extend(f"Criterion: {criterion}" for criterion in contract.acceptance_criteria)
        image_bytes_total = 0
        context_chars = sum(map(len, text_parts))
        if context_chars > self.config.max_context_chars:
            raise ValidationError("Vision text context exceeds its configured character limit")
        for item in context.items:
            source = item.source
            raw = item.content
            if len(raw) != source.size_bytes or hashlib.sha256(raw).hexdigest() != source.sha256:
                raise ValidationError(
                    "Vision context content does not match its declared source size and hash"
                )
            if secret.encode("utf-8") in raw:
                raise ValidationError("Vision context contains configured credential material")
            sources.append(source)
            suffix = Path(source.path).suffix.lower()
            if suffix in {".png", ".jpg", ".jpeg"}:
                if len(raw) > self.config.max_image_bytes:
                    raise ValidationError("Selected vision image exceeds its byte limit")
                media_type, _dimensions = _decode_image(raw, suffix, self.config.max_image_pixels)
                image_bytes_total += len(raw)
                if (
                    image_bytes_total > _MAX_TOTAL_IMAGE_BYTES
                    or len(images) >= self.config.max_images
                ):
                    raise ValidationError(
                        "Selected vision images exceed the count or aggregate byte limit"
                    )
                data_url = f"data:{media_type};base64,{base64.b64encode(raw).decode('ascii')}"
                images.append({"type": "input_image", "image_url": data_url, "detail": "high"})
            else:
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as err:
                    raise ValidationError(
                        "Vision context supports only PNG/JPEG images and UTF-8 text"
                    ) from err
                if redactor.redact_text(text) != text:
                    raise ValidationError("Vision text context contains secret-like material")
                text_parts.append(
                    f"Selected context ({source.path}; purpose: {source.purpose}):\n{text}"
                )
                context_chars += len(text) + len(source.path) + len(source.purpose)
            if context_chars > self.config.max_context_chars:
                raise ValidationError("Vision text context exceeds its configured character limit")
        if not images:
            raise ValidationError(
                "Vision review requires at least one explicitly selected PNG or JPEG image"
            )
        return "\n\n".join(text_parts), images, tuple(sources)

    def _uncertain(self, reason: str) -> ProviderRun:
        return ProviderRun(
            schema_version="provider-run-1.0.0",
            status=ProviderExecutionStatus.UNCERTAIN,
            proposal=None,
            external_id=None,
            metadata={"provider_id": self.provider_id, "reason": reason},
        )

    def _failed(self, reason: str) -> ProviderRun:
        return ProviderRun(
            schema_version="provider-run-1.0.0",
            status=ProviderExecutionStatus.FAILED,
            proposal=None,
            external_id=None,
            metadata={"provider_id": self.provider_id, "reason": reason},
        )


def _decode_image(raw: bytes, suffix: str, max_pixels: int) -> tuple[str, tuple[int, int]]:
    expected = "PNG" if suffix == ".png" else "JPEG"
    try:
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > max_pixels:
                raise ValidationError("Selected vision image exceeds the decoded pixel limit")
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            if image.format != expected:
                raise ValidationError(
                    "Selected vision image extension does not match decoded image format"
                )
        return ("image/png" if expected == "PNG" else "image/jpeg"), (width, height)
    except (UnidentifiedImageError, OSError, ValueError) as err:
        if isinstance(err, ValidationError):
            raise
        raise ValidationError("Selected vision image is corrupt or unsupported") from err


def _read_prompt_asset() -> bytes:
    try:
        raw = _PROMPT_PATH.read_bytes()
    except OSError as err:
        raise ValidationError("Versioned OpenAI vision prompt asset is unavailable") from err
    if not raw or len(raw) > 16_000:
        raise ValidationError("Versioned OpenAI vision prompt asset is empty or oversized")
    return raw


def _parse_completed_response(response: HttpResponse) -> _VisionPayload:
    if len(response.body) > 1_000_000:
        raise ValidationError("Vision response exceeds the configured bound")
    envelope = json.loads(response.body)
    if not isinstance(envelope, dict) or envelope.get("status") != "completed":
        raise ValidationError("Vision response did not complete")
    output = envelope.get("output")
    if not isinstance(output, list) or len(output) > 8:
        raise ValidationError("Vision response output envelope is malformed")
    text_parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            raise ValidationError("Vision response included an unexpected tool or output item")
        contents = item.get("content")
        if not isinstance(contents, list) or len(contents) > 8:
            raise ValidationError("Vision response message content is malformed")
        for content in contents:
            if (
                not isinstance(content, dict)
                or content.get("type") != "output_text"
                or not isinstance(content.get("text"), str)
            ):
                raise ValidationError(
                    "Vision response was refused or included an unexpected content item"
                )
            text_parts.append(content["text"])
    if len(text_parts) != 1 or len(text_parts[0].encode("utf-8")) > 32_000:
        raise ValidationError(
            "Vision response must contain exactly one bounded structured text result"
        )
    return _VisionPayload.model_validate_json(text_parts[0])


def _receipt_id(headers: dict[str, str]) -> str | None:
    value = headers.get("x-request-id")
    return (
        value[:128]
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value)
        else None
    )
