"""Fake deterministic provider adapters for automated tests and demo workflows.

Explicitly named and isolated to ensure fake adapters never masquerade as
real production integrations.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.adapters.persistence.repositories import (
    ProviderOperationIntent,
    ProviderOperationIntentRepository,
)
from gamefactory.core.domain.errors import (
    ProviderUncertainError,
    ToolExecutionError,
)
from gamefactory.core.domain.models import CostClass, generate_id
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
        simulate_uncertain_submission: bool = False,
        raw_malformed: bool = False,
        intent_repo: ProviderOperationIntentRepository | None = None,
        report_unknown_cost: bool = False,
    ) -> None:
        self._name = name
        self._cost_class = cost_class
        self.fail_times = fail_times
        self.fail_message = fail_message
        self.simulate_crash = simulate_crash
        self.simulate_uncertain_submission = simulate_uncertain_submission
        self.raw_malformed = raw_malformed
        self.intent_repo = intent_repo
        self.report_unknown_cost = report_unknown_cost
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

    def _write_output(self, request: GenerationRequest) -> Path | None:
        """Model a deterministic remote download for an already accepted fake task."""
        output_path = request.parameters.get("output_path")
        if not output_path:
            return None
        output_file = Path(str(output_path))
        output_file.parent.mkdir(parents=True, exist_ok=True)
        if output_file.exists():
            return output_file
        if self.raw_malformed:
            output_file.write_bytes(b"MALFORMED_CORRUPT_GLB_BYTES")
        else:
            spec = request.parameters.get("specification", {})
            dimensions = spec.get("dimensions", {}) if isinstance(spec, dict) else {}
            create_box_glb(
                width_m=float(request.parameters.get("width_m", dimensions.get("width_m", 1.2))),
                depth_m=float(request.parameters.get("depth_m", dimensions.get("depth_m", 1.0))),
                height_m=float(request.parameters.get("height_m", dimensions.get("height_m", 1.0))),
                mesh_name=str(request.parameters.get("asset_id", "SM_Prop_EnergyCrate_A")),
                include_lod1=False,
                include_collider=False,
                num_materials=1,
                include_texture=True,
                output_path=output_file,
            )
        return output_file

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Execute deterministic generation with invocation counting and failure simulation."""
        params = request.parameters
        request_fingerprint = request.operation_hash or generate_id("FP")
        task_id = str(params.get("task_id", ""))
        workflow_id = str(params.get("workflow_id", ""))
        asset_id = str(params.get("asset_id", "asset_under_test"))
        revision_number = int(params.get("revision_number", 1))
        approval_id = str(params.get("approval_id", "app-001"))
        concept_hash = str(params.get("concept_hash", "concept-hash-placeholder"))

        # 1. Check for existing durable intent / resume by fingerprint or task
        existing_intent: ProviderOperationIntent | None = None
        if self.intent_repo is not None:
            existing_intent = self.intent_repo.get_by_fingerprint(request_fingerprint)
            if existing_intent is None and task_id:
                existing_intent = self.intent_repo.get_by_task(task_id)

        if existing_intent is not None:
            if (
                existing_intent.status in {"SUCCEEDED", "SUBMITTED"}
                and existing_intent.external_task_id
            ):
                # Query/resume existing known task without re-running generation!
                downloaded = self._write_output(request)
                actual_cost = existing_intent.actual_cost
                if actual_cost is None and not self.report_unknown_cost:
                    actual_cost = (
                        5.0 if self._cost_class in (CostClass.PAID, CostClass.EXPENSIVE) else 0.0
                    )
                existing_intent.status = "SUCCEEDED"
                existing_intent.actual_cost = actual_cost
                if self.intent_repo is not None:
                    self.intent_repo.save(existing_intent)
                return GenerationResponse(
                    external_op_id=existing_intent.external_task_id,
                    status="SUCCESS",
                    output_path=str(downloaded) if downloaded else None,
                    cost=actual_cost if actual_cost is not None else 0.0,
                    cost_unit="fake_credits",
                    details={
                        "resumed": True,
                        "task_id": existing_intent.external_task_id,
                        "actual_cost": actual_cost,
                    },
                )
            if (
                existing_intent.status in ("SUBMITTING", "UNCERTAIN")
                and not existing_intent.external_task_id
            ):
                # Crash occurred after submission intent but before task ID confirmed: UNCERTAIN
                raise ProviderUncertainError(
                    f"Generation for '{asset_id}' was initiated but accepted task ID is unknown. "
                    "Automatic second paid generation is forbidden; reconciliation required.",
                    task_id=task_id,
                    execution_id=existing_intent.id,
                    provider=self._name,
                    details={"intent_id": existing_intent.id, "status": existing_intent.status},
                )

        # 2. Record pre-submission durable intent if intent_repo is provided
        intent: ProviderOperationIntent | None = None
        if self.intent_repo is not None:
            intent = ProviderOperationIntent(
                id=generate_id("INTENT"),
                workflow_id=workflow_id,
                task_id=task_id,
                asset_id=asset_id,
                revision_number=revision_number,
                provider=self._name,
                operation="image-to-3d",
                concept_hash=concept_hash,
                request_fingerprint=request_fingerprint,
                approval_id=approval_id,
                estimated_cost=5.0,
                status="SUBMITTING",
            )
            self.intent_repo.save(intent)

        # 3. Simulate uncertain crash before task ID is accepted
        if self.simulate_uncertain_submission:
            if self.intent_repo is not None and intent is not None:
                intent.status = "UNCERTAIN"
                self.intent_repo.save(intent)
            raise ProviderUncertainError(
                "Simulated crash before provider returned task ID",
                task_id=task_id,
                execution_id=intent.id if intent else "unknown",
                provider=self._name,
            )

        self.invocation_count += 1
        self.recorded_requests.append(request)
        op_id = (
            "FAKE-OP-"
            + hashlib.sha256(
                f"{workflow_id}:{task_id}:{revision_number}:{request_fingerprint}".encode()
            ).hexdigest()[:16]
            if self.intent_repo is not None
            else f"FAKE-OP-{self.invocation_count:04d}"
        )
        self.generated_op_ids.append(op_id)

        # Update intent with accepted external task ID
        if self.intent_repo is not None and intent is not None:
            intent.external_task_id = op_id
            intent.status = "SUBMITTED"
            self.intent_repo.save(intent)

        if self.fail_times > 0:
            self.fail_times -= 1
            if self.intent_repo is not None and intent is not None:
                intent.status = "FAILED"
                self.intent_repo.save(intent)
            raise ToolExecutionError(
                self.fail_message,
                details={"external_op_id": op_id, "invocation": self.invocation_count},
            )

        if self.simulate_crash:
            # Simulate crash where request was sent to provider but local runner died
            raise ProviderUncertainError(
                f"Simulated response loss after fake provider accepted {op_id}",
                task_id=task_id,
                execution_id=intent.id if intent else "unknown",
                provider=self._name,
                details={"process_state_uncertain": True, "external_op_id": op_id},
            )

        output_file = self._write_output(request)

        cost_val = 0.0
        if self._cost_class in (CostClass.PAID, CostClass.EXPENSIVE):
            cost_val = 5.0

        if self.intent_repo is not None and intent is not None:
            intent.status = "SUCCEEDED"
            intent.actual_cost = cost_val if not self.report_unknown_cost else None
            self.intent_repo.save(intent)

        return GenerationResponse(
            external_op_id=op_id,
            status="SUCCESS",
            output_path=str(output_file) if output_file else None,
            cost=cost_val if not self.report_unknown_cost else 0.0,
            cost_unit="fake_credits",
            details={
                "prompt": request.prompt,
                "triangles": 12,
                "cost_reported": "UNKNOWN" if self.report_unknown_cost else cost_val,
                "actual_cost": None if self.report_unknown_cost else cost_val,
            },
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
