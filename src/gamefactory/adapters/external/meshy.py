"""Meshy provider boundary representation for V0.1.

Declares PAID cost classification, configuration presence checks, and boundary
contracts without triggering real paid generation calls in V0.1.
"""

import os

from gamefactory.adapters.external.base import (
    AssetGenerationProvider,
    GenerationRequest,
    GenerationResponse,
)
from gamefactory.core.domain.errors import ProviderUnavailable
from gamefactory.core.domain.models import CostClass


class MeshyProvider(AssetGenerationProvider):
    """Provider boundary for Meshy 3D generation service."""

    @property
    def name(self) -> str:
        return "meshy"

    @property
    def cost_class(self) -> CostClass:
        return CostClass.PAID

    def is_configured(self) -> bool:
        """Check if MESHY_API_KEY environment variable is defined without exposing it."""
        key = os.environ.get("MESHY_API_KEY")
        return bool(key and len(key.strip()) > 0)

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Reject real paid generation in V0.1 milestone."""
        raise ProviderUnavailable(
            "Meshy production API calls are not permitted in Factory Core V0.1. Use fake test providers.",
            provider="meshy",
        )
