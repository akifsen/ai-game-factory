"""Pure shared helper for building operation inputs for approval fingerprints."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.models import Artifact, CostClass, Task, Workflow


def build_operation_inputs(
    workflow: Workflow,
    task: Task,
    artifacts: Sequence[Artifact],
    cost_class: CostClass | str,
    provider_name: str | None = None,
    handler_context: Any = None,
) -> dict[str, Any]:
    """Build the deterministic operation inputs dictionary for approval fingerprints and dispatch.

    Preserves the exact current engine.approval_inputs JSON shape and serialization.
    """
    cost_class_val = cost_class.value if isinstance(cost_class, CostClass) else str(cost_class)
    raw_cost = task.parameters.get("cost")
    if "cost" not in task.parameters:
        estimated_cost = 0.0
    elif isinstance(raw_cost, bool):
        raise ValidationError("Task.parameters.cost cannot be a boolean")
    elif raw_cost is None:
        raise ValidationError("Task.parameters.cost must be numeric when explicitly provided")
    else:
        try:
            estimated_cost = float(raw_cost)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Task.parameters.cost must be numeric") from exc
        if not math.isfinite(estimated_cost) or estimated_cost < 0:
            raise ValidationError("Task.parameters.cost must be finite and non-negative")

    scope: dict[str, Any] = {
        "workflow_id": workflow.id,
        "task_type": task.task_type,
        "cost_class": cost_class_val,
        "provider": provider_name,
        "artifacts": sorted((item.id, item.content_hash) for item in artifacts),
        "estimated_cost": estimated_cost,
    }
    if handler_context is not None:
        scope["handler_context"] = handler_context

    return {
        "parameters": task.parameters,
        "scope": scope,
    }
