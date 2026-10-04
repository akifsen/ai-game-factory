"""Explicit local composition helpers for concrete agent providers."""

from __future__ import annotations

import re

from gamefactory.adapters.agents.base import AgentProvider
from gamefactory.adapters.agents.base import ProviderRegistry as _ProviderRegistry
from gamefactory.core.domain.agent_contracts import AgentDefinition
from gamefactory.core.domain.errors import ValidationError


class ExplicitProviderRegistry(_ProviderRegistry):
    """In-memory provider registry bound to each adapter's effective config digest.

    Registrations are supplied by trusted application composition. This module
    never scans packages, imports model-selected code, or probes live services.
    """

    def __init__(self) -> None:
        super().__init__()
        self._fingerprints: dict[str, str] = {}

    def register(
        self, provider: AgentProvider, definition: AgentDefinition, *, enabled: bool = True
    ) -> None:
        fingerprint = getattr(provider, "config_fingerprint", None)
        configured = getattr(provider, "is_configured", None)
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValidationError(
                "Provider must expose a stable lowercase configuration fingerprint"
            )
        if not isinstance(configured, bool):
            raise ValidationError("Provider must expose truthful static is_configured readiness")
        super().register(provider, definition, enabled=enabled)
        self._fingerprints[provider.provider_id] = fingerprint

    def config_fingerprint(self, provider_id: str) -> str | None:
        return self._fingerprints.get(provider_id)


__all__ = ["ExplicitProviderRegistry"]
