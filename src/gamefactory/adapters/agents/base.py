"""Provider ports, readiness states and explicit local registration."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from gamefactory.core.domain.agent_contracts import AgentDefinition, AgentTaskContract
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.provider_execution import (
    AuthorizationVerifier,
    ProviderAuthorization,
    ProviderRun,
)


class ProviderStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    MISCONFIGURED = "MISCONFIGURED"
    CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
    NOT_VERIFIED = "NOT_VERIFIED"


@dataclass(frozen=True)
class ProviderReadiness:
    provider_id: str
    status: ProviderStatus
    capabilities: frozenset[str]
    reason: str | None = None
    executable_path: str | None = None
    missing_credentials: tuple[str, ...] = ()
    live_execution_verified: bool = False


@runtime_checkable
class AgentProvider(Protocol):
    provider_id: str
    capabilities: frozenset[str]
    is_configured: bool

    def readiness(self) -> ProviderReadiness: ...

    def execute(
        self,
        contract: AgentTaskContract,
        context: Any,
        workspace: Path,
        request_fingerprint: str,
        authorization: ProviderAuthorization,
        authorization_verifier: AuthorizationVerifier,
    ) -> ProviderRun: ...


@runtime_checkable
class ImageGenerationProvider(AgentProvider, Protocol):
    """Capability-oriented image generation port."""


@runtime_checkable
class VisionReviewProvider(AgentProvider, Protocol):
    """Advisory vision review port; implementations cannot mark gates passed."""


@runtime_checkable
class AudioGenerationProvider(AgentProvider, Protocol):
    """Audio generation port; speech and music/SFX remain distinct capabilities."""


@dataclass(frozen=True)
class RegisteredProvider:
    provider: AgentProvider
    definition: AgentDefinition
    enabled: bool = True


class ProviderRegistry:
    """In-process trusted registrations only; never imports plugins dynamically."""

    def __init__(self) -> None:
        self._providers: dict[str, RegisteredProvider] = {}

    def register(
        self,
        provider: AgentProvider,
        definition: AgentDefinition,
        *,
        enabled: bool = True,
    ) -> None:
        if provider.provider_id != definition.agent_id:
            raise ValidationError("Provider identity must match its registered agent definition")
        if provider.provider_id in self._providers:
            raise ValidationError(
                "Provider id is already registered", details={"provider_id": provider.provider_id}
            )
        advertised = set(provider.capabilities)
        if not advertised or not advertised.issubset(definition.capabilities):
            raise ValidationError(
                "Provider capabilities must be declared by its registered agent definition"
            )
        self._providers[provider.provider_id] = RegisteredProvider(provider, definition, enabled)

    def get(self, provider_id: str) -> RegisteredProvider | None:
        return self._providers.get(provider_id)

    def capabilities(self) -> frozenset[str]:
        return frozenset(
            capability
            for entry in self._providers.values()
            if entry.enabled
            and entry.provider.readiness().status
            in {ProviderStatus.AVAILABLE, ProviderStatus.NOT_VERIFIED}
            for capability in entry.provider.capabilities
        )

    def readiness(self) -> tuple[ProviderReadiness, ...]:
        rows = []
        for provider_id in sorted(self._providers):
            entry = self._providers[provider_id]
            result = entry.provider.readiness()
            if not entry.enabled and result.status == ProviderStatus.AVAILABLE:
                result = ProviderReadiness(
                    provider_id,
                    ProviderStatus.UNAVAILABLE,
                    result.capabilities,
                    "Provider is disabled",
                )
            rows.append(result)
        return tuple(rows)

    def route(self, contract: AgentTaskContract) -> tuple[RegisteredProvider, ...]:
        required = {item.name for item in contract.required_capabilities if item.required}
        available = self.capabilities()
        if not required.issubset(available):
            raise ValidationError(
                "Required provider capabilities are unavailable",
                details={"missing": sorted(required - available)},
            )
        return tuple(
            self._providers[key]
            for key in sorted(self._providers)
            if self._providers[key].enabled
            and required.issubset(self._providers[key].provider.capabilities)
            and self._providers[key].provider.readiness().status
            in {ProviderStatus.AVAILABLE, ProviderStatus.NOT_VERIFIED}
        )


def verify_execution_authorization(
    *,
    provider_id: str,
    contract: AgentTaskContract,
    request_fingerprint: str,
    authorization: ProviderAuthorization | None,
    verifier: AuthorizationVerifier | None,
) -> None:
    """Fail closed unless workflow composition confirms the durable approved intent."""
    if authorization is None or verifier is None or not isinstance(verifier, AuthorizationVerifier):
        raise ValidationError("Provider execution requires a workflow authorization verifier")
    if authorization.request_fingerprint != request_fingerprint:
        raise ValidationError("Provider authorization is bound to a different request fingerprint")
    ceiling = contract.cost_constraints
    ranks = {"LOCAL": 0, "FREE_EXTERNAL": 1, "METERED": 2, "PAID": 3, "EXPENSIVE": 4}
    if (
        authorization.currency != ceiling.currency
        or authorization.unit != ceiling.unit
        or ranks[authorization.cost_class] > ranks[ceiling.cost_class]
        or authorization.max_cost > ceiling.max_amount
    ):
        raise ValidationError("Provider authorization exceeds or mismatches the task cost boundary")
    if ceiling.fallback_max_amount != 0:
        raise ValidationError(
            "Automatic provider fallback is unsupported; split fallback into a separately approved task"
        )
    if not verifier.verify(
        authorization, provider_id, request_fingerprint, authorization.operation_hash
    ):
        raise ValidationError(
            "Persisted workflow approval does not authorize this provider operation"
        )


def validate_provider_cost(provider_cost: Any, contract: AgentTaskContract) -> None:
    ranks = {"LOCAL": 0, "FREE_EXTERNAL": 1, "METERED": 2, "PAID": 3, "EXPENSIVE": 4}
    ceiling = contract.cost_constraints
    if (
        provider_cost.currency != ceiling.currency
        or provider_cost.unit != ceiling.unit
        or ranks[provider_cost.cost_class] > ranks[ceiling.cost_class]
        or provider_cost.max_amount > ceiling.max_amount
        or provider_cost.approval_required
        and not ceiling.approval_required
        or provider_cost.fallback_max_amount != 0
    ):
        raise ValidationError("Provider cost configuration exceeds the task authorization ceiling")


def validate_context(contract: AgentTaskContract, context: Any) -> None:
    items = getattr(context, "items", None)
    if not isinstance(items, tuple):
        raise ValidationError("Provider context must be a bounded ContextBuilder result")
    if sum(len(item.content) for item in items) > contract.context_max_bytes:
        raise ValidationError("Provider context exceeds the task contract byte limit")
    declared = {source.path: source for source in contract.sources}
    if set(declared) != {item.source.path for item in items}:
        raise ValidationError("Provider context paths do not match the task contract")
    for item in items:
        source = declared[item.source.path]
        if (
            item.source.sha256 != source.sha256
            or item.source.size_bytes != source.size_bytes
            or len(item.content) != source.size_bytes
            or hashlib.sha256(item.content).hexdigest() != source.sha256
        ):
            raise ValidationError(
                "Provider context content does not match the contract source hash"
            )


def stable_config_fingerprint(config: Any, *, executable: Path | None = None) -> str:
    """Bind approvals to non-secret provider settings and installed executable bytes."""
    if isinstance(config, dict):
        safe_config = dict(config)
    elif hasattr(config, "model_dump"):
        safe_config = config.model_dump(mode="json")
    elif hasattr(config, "__dataclass_fields__"):
        from dataclasses import asdict

        safe_config = asdict(config)
    else:
        raise ValidationError("Provider configuration cannot be fingerprinted")
    if executable is not None:
        path = Path(os.path.abspath(executable))
        current = path
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        while True:
            try:
                info = current.lstat()
            except FileNotFoundError:
                info = None
            except OSError as err:
                raise ValidationError(
                    "Configured provider executable cannot be inspected safely"
                ) from err
            if info is not None and (
                stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & reparse_flag
            ):
                raise ValidationError(
                    "Configured provider executable cannot cross links or junctions"
                )
            if current.parent == current:
                break
            current = current.parent
        digest = hashlib.sha256()
        try:
            descriptor = os.open(
                path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > 512 * 1024 * 1024:
                    raise ValidationError(
                        "Configured provider executable is not a bounded regular file"
                    )
                total = 0
                while chunk := stream.read(min(1024 * 1024, 512 * 1024 * 1024 - total + 1)):
                    total += len(chunk)
                    if total > 512 * 1024 * 1024:
                        raise ValidationError(
                            "Configured provider executable exceeds the fingerprint size limit"
                        )
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise ValidationError(
                        "Configured provider executable changed while being fingerprinted"
                    )
        except OSError as err:
            raise ValidationError("Configured provider executable cannot be fingerprinted") from err
        safe_config["_executable"] = {"path": str(path), "sha256": digest.hexdigest()}
    payload = json.dumps(
        safe_config, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
