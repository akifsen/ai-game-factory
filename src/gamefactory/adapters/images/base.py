"""Base interfaces and contracts for concept image generation providers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from gamefactory.core.domain.models import CostClass


@dataclass(frozen=True)
class ConceptProvenance:
    """Immutable provenance record binding concept image to prompt, sidecar metadata, and spec."""

    provider: str
    model: str
    prompt: str
    prompt_version: str
    asset_spec_hash: str
    generation_timestamp: str  # actual sidecar timestamp or "UNKNOWN", never fabricated
    imported_at: str  # distinct timestamp of when the asset was ingested
    artifact_hash: str
    dimensions: dict[str, int]
    cost_classification: str
    source_type: str = "imported"  # "imported" or "local_generation"
    sidecar_path: str | None = None
    sidecar_hash: str | None = None
    provenance_type: str = "UNKNOWN"
    paid: bool = False
    source_script_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConceptGenerationRequest:
    prompt: str
    asset_spec_hash: str
    output_path: Path
    dimensions: tuple[int, int] = (1024, 1024)
    model: str = "default"
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConceptGenerationResponse:
    artifact_path: Path
    provenance: ConceptProvenance
    cost: float = 0.0
    cost_unit: str = "credits"
    details: dict[str, Any] = field(default_factory=dict)


class ImageGenerationProvider(Protocol):
    """Protocol for external or local concept image generation providers."""

    @property
    def name(self) -> str: ...

    @property
    def cost_class(self) -> CostClass: ...

    def is_configured(self) -> bool: ...

    def generate(self, request: ConceptGenerationRequest) -> ConceptGenerationResponse: ...
