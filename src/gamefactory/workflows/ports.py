"""Provider capability contracts owned by the workflow layer."""

from dataclasses import dataclass, field
from typing import Any, Protocol

from gamefactory.core.domain.models import CostClass
from gamefactory.core.domain.paid_request import PaidRequestSnapshot


@dataclass
class GenerationRequest:
    prompt: str
    target_format: str = "glb"
    parameters: dict[str, Any] = field(default_factory=dict)
    operation_hash: str = ""
    paid_request: PaidRequestSnapshot | None = None


@dataclass
class GenerationResponse:
    external_op_id: str
    status: str
    output_path: str | None = None
    cost: float = 0.0
    cost_unit: str = "provider_units"
    details: dict[str, Any] = field(default_factory=dict)


class AssetGenerationProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def cost_class(self) -> CostClass: ...

    def is_configured(self) -> bool: ...

    def generate(self, request: GenerationRequest) -> GenerationResponse: ...


class PaidRequestAdapter(Protocol):
    def resolve_paid_request(
        self, binding: dict[str, Any], specification: dict[str, Any], cost: dict[str, Any]
    ) -> dict[str, Any]: ...

    def check_paid_request(self, snapshot_content: dict[str, Any]) -> None: ...
