"""Fake deterministic provider adapters for automated tests and demo workflows.

Explicitly named and isolated to ensure fake adapters never masquerade as
real production integrations.
"""

from pathlib import Path
from typing import Any

from gamefactory.core.domain.errors import ToolExecutionError
from gamefactory.core.domain.models import CostClass
from gamefactory.workflows.ports import (
    AssetGenerationProvider,
    GenerationRequest,
    GenerationResponse,
)


class FakeAssetGenerationProvider(AssetGenerationProvider):
    """Deterministic fake asset generator with call counting and controllable failures."""

    def __init__(
        self,
        name: str = "fake_asset_gen",
        cost_class: CostClass = CostClass.PAID,
        fail_times: int = 0,
        fail_message: str = "Simulated transient provider timeout",
        simulate_crash: bool = False,
    ) -> None:
        self._name = name
        self._cost_class = cost_class
        self.fail_times = fail_times
        self.fail_message = fail_message
        self.simulate_crash = simulate_crash
        self.invocation_count = 0
        self.recorded_requests: list[GenerationRequest] = []
        self.generated_op_ids: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def cost_class(self) -> CostClass:
        return self._cost_class

    def is_configured(self) -> bool:
        return True

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Execute deterministic generation with invocation counting and failure simulation."""
        self.invocation_count += 1
        self.recorded_requests.append(request)
        op_id = f"FAKE-OP-{self.invocation_count:04d}"
        self.generated_op_ids.append(op_id)

        if self.fail_times > 0:
            self.fail_times -= 1
            raise ToolExecutionError(
                self.fail_message,
                details={"external_op_id": op_id, "invocation": self.invocation_count},
            )

        if self.simulate_crash:
            # Simulate crash where request was sent to provider but local runner died
            raise RuntimeError(f"CRASH_AFTER_SUBMISSION:{op_id}")

        output_path = request.parameters.get("output_path")
        if output_path:
            output_file = Path(str(output_path))
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_bytes(b"FAKE-GLB\x00deterministic-test-artifact")

        return GenerationResponse(
            external_op_id=op_id,
            status="SUCCESS",
            output_path=str(output_path) if output_path else None,
            cost=5.0 if self._cost_class in (CostClass.PAID, CostClass.EXPENSIVE) else 0.0,
            cost_unit="fake_credits",
            details={"prompt": request.prompt, "triangles": 12000},
        )


class FakeAgentProvider:
    """Deterministic agent worker mock for code and design tasks."""

    def __init__(self, name: str = "FakeEngineer") -> None:
        self.name = name
        self.invocation_count = 0

    def execute_task(self, task_type: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.invocation_count += 1
        return {
            "agent": self.name,
            "status": "COMPLETED",
            "summary": f"Completed {task_type} deterministically",
            "parameters": parameters,
        }
