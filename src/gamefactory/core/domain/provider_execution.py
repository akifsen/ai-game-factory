"""Strict provider-run envelopes; outputs remain staged and non-authoritative."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, field_validator, model_validator

from gamefactory.core.domain.agent_contracts import (
    AgentResultProposal,
    StrictModel,
)
from gamefactory.core.domain.agent_contracts import (
    VisualReview as AgentVisualReview,
)
from gamefactory.core.domain.errors import ValidationError

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_OUTPUT_FILE_BYTES = 64 * 1024 * 1024
_MAX_PROVIDER_RUN_BYTES = 100 * 1024 * 1024


class ProviderExecutionStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


class ProviderAuthorization(StrictModel):
    """Hash- and ceiling-bound workflow authorization; never a boolean opt-in."""

    approval_id: str = Field(min_length=1, max_length=128)
    operation_hash: str
    request_fingerprint: str
    max_cost: float = Field(ge=0, allow_inf_nan=False)
    currency: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,31}$")
    unit: str = Field(min_length=1, max_length=32)
    cost_class: Literal["LOCAL", "FREE_EXTERNAL", "METERED", "PAID", "EXPENSIVE"]

    @field_validator("operation_hash", "request_fingerprint")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("authorization hashes must be 64 lowercase hexadecimal characters")
        return value


@runtime_checkable
class AuthorizationVerifier(Protocol):
    """Workflow-owned port that checks persisted intent and human approval."""

    def verify(
        self,
        authorization: ProviderAuthorization,
        provider_id: str,
        request_fingerprint: str,
        operation_hash: str,
    ) -> bool: ...


class ProviderOutputFile(StrictModel):
    """A content-addressed output staged by an adapter inside its owned workspace."""

    path: str = Field(min_length=1, max_length=1024)
    content_base64: str = Field(min_length=1, max_length=90_000_000)
    sha256: str
    media_type: str = Field(min_length=1, max_length=128)
    expected_before_sha256: str | None = None
    staged_path: str | None = Field(default=None, max_length=1024)

    @field_validator("sha256", "expected_before_sha256")
    @classmethod
    def validate_hash(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("file hash must be 64 lowercase hexadecimal characters")
        return value

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if (
            "\\" in value
            or value.startswith("/")
            or ":" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("provider output path must be normalized and project-relative")
        return value

    @field_validator("staged_path")
    @classmethod
    def validate_staged_path(cls, value: str | None) -> str | None:
        if value is not None and (
            "\\" in value
            or value.startswith("/")
            or ":" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("staged_path must be normalized and workspace-relative")
        return value

    @model_validator(mode="after")
    def validate_content(self) -> ProviderOutputFile:
        try:
            content = base64.b64decode(self.content_base64, validate=True)
        except (ValueError, TypeError) as err:
            raise ValueError("content_base64 must be valid base64") from err
        if len(content) > _MAX_OUTPUT_FILE_BYTES:
            raise ValueError("provider output file exceeds the 64 MiB limit")
        if hashlib.sha256(content).hexdigest() != self.sha256:
            raise ValueError("provider output hash does not match content")
        return self

    def content_bytes(self) -> bytes:
        return base64.b64decode(self.content_base64, validate=True)

    def bytes(self) -> bytes:
        """Compatibility helper for artifact sinks that consume raw output bytes."""
        return self.content_bytes()


class ProviderRun(StrictModel):
    """Validated provider response. No field grants approval or workflow completion."""

    schema_version: Literal["provider-run-1.0.0"]
    status: ProviderExecutionStatus
    proposal: AgentResultProposal | None
    visual_review: AgentVisualReview | None = None
    files: tuple[ProviderOutputFile, ...] = ()
    actual_cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cost_currency: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{1,31}$")
    cost_unit: str | None = Field(default=None, max_length=32)
    external_id: str | None = Field(default=None, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    @model_validator(mode="after")
    def validate_status_cost_and_files(self) -> ProviderRun:
        paths = [file.path for file in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("provider output paths must be unique")
        total = sum(len(file.content_base64) * 3 // 4 for file in self.files)
        if total > _MAX_PROVIDER_RUN_BYTES:
            raise ValueError("provider run output exceeds the 100 MiB aggregate limit")
        if self.proposal is not None and self.visual_review is not None:
            raise ValueError("provider run must contain either a proposal or visual review")
        if (
            self.status == ProviderExecutionStatus.COMPLETED
            and self.proposal is None
            and self.visual_review is None
        ):
            raise ValueError(
                "completed provider run requires a structured proposal or visual review"
            )
        if self.status != ProviderExecutionStatus.COMPLETED and self.files:
            raise ValueError("failed or uncertain provider runs cannot publish staged files")
        if self.actual_cost is None and (
            self.cost_currency is not None or self.cost_unit is not None
        ):
            raise ValueError("unknown actual cost cannot claim currency or unit")
        if self.actual_cost is not None and (self.cost_currency is None or self.cost_unit is None):
            raise ValueError("known actual cost requires currency and unit")
        if self.actual_cost is not None and not math.isfinite(self.actual_cost):
            raise ValueError("actual_cost must be finite")
        try:
            encoded_metadata = json.dumps(
                self.metadata, allow_nan=False, separators=(",", ":")
            ).encode()
        except (TypeError, ValueError) as err:
            raise ValueError("provider metadata must be finite JSON") from err
        if len(encoded_metadata) > 64_000:
            raise ValueError("provider metadata exceeds 64 KB")
        return self

    @classmethod
    def from_value(cls, value: dict[str, Any] | str | bytes) -> ProviderRun:
        """Validate untrusted JSON or a mapping against the complete run schema."""
        if isinstance(value, (str, bytes)):
            raw = value.encode("utf-8") if isinstance(value, str) else value
            if len(raw) > _MAX_PROVIDER_RUN_BYTES:
                raise ValidationError("Provider run JSON exceeds the 100 MiB limit")
            return cls.model_validate_json(raw)
        try:
            raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as err:
            raise ValidationError("Provider run mapping must contain finite JSON values") from err
        if len(raw) > _MAX_PROVIDER_RUN_BYTES:
            raise ValidationError("Provider run JSON exceeds the 100 MiB limit")
        return cls.model_validate_json(raw)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
