"""Centralized policy engine for AI Game Factory.

Evaluates permissions, cost safety, and approval requirements for:
- local read operations
- repository write operations
- process executions
- free external provider operations
- paid external operations
- destructive operations
"""

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from gamefactory.core.domain.errors import BudgetExceeded, PolicyViolation
from gamefactory.core.domain.models import CostClass


class OperationType(StrEnum):
    LOCAL_READ = "LOCAL_READ"
    REPOSITORY_WRITE = "REPOSITORY_WRITE"
    PROCESS_EXECUTION = "PROCESS_EXECUTION"
    FREE_EXTERNAL = "FREE_EXTERNAL"
    PAID_OPERATION = "PAID_OPERATION"
    DESTRUCTIVE = "DESTRUCTIVE"


@dataclass
class PolicyRule:
    require_approval_for_paid: bool = True
    require_approval_for_destructive: bool = True
    require_approval_for_repo_write: bool = False
    max_operation_cost: float = 100.0
    project_budget: float = 500.0

    def __post_init__(self) -> None:
        for name in ("max_operation_cost", "project_budget"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise PolicyViolation(
                    f"{name} must be a finite non-negative amount in the project currency"
                )


@dataclass
class PolicyEvaluationResult:
    allowed: bool
    requires_approval: bool
    approval_type: str | None = None
    reason: str = ""


class PolicyEngine:
    """Central evaluator for all operation requests in the Factory."""

    def __init__(self, rule: PolicyRule | None = None) -> None:
        self.rule = rule or PolicyRule()

    def evaluate(
        self,
        op_type: OperationType,
        cost_class: CostClass = CostClass.LOCAL,
        estimated_cost: float = 0.0,
        current_spent: float = 0.0,
        has_approval: bool = False,
        context: dict[str, Any] | None = None,
    ) -> PolicyEvaluationResult:
        """Evaluate an operation against active policies."""
        for name, value in (("estimated_cost", estimated_cost), ("current_spent", current_spent)):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise PolicyViolation(
                    f"{name} must be a finite non-negative amount in the project currency"
                )
        # 1. Budget enforcement
        if estimated_cost > 0.0:
            if estimated_cost > self.rule.max_operation_cost:
                raise BudgetExceeded(
                    f"Estimated cost {estimated_cost:.2f} cost units exceeds max per-operation limit {self.rule.max_operation_cost:.2f} cost units",
                    details={
                        "estimated_cost": estimated_cost,
                        "limit": self.rule.max_operation_cost,
                    },
                )
            if current_spent + estimated_cost > self.rule.project_budget:
                raise BudgetExceeded(
                    f"Operation would exceed total project budget: current spent {current_spent:.2f} + {estimated_cost:.2f} > {self.rule.project_budget:.2f} cost units",
                    details={
                        "current_spent": current_spent,
                        "estimated_cost": estimated_cost,
                        "budget": self.rule.project_budget,
                    },
                )

        # 2. Destructive operations (ALWAYS require explicit approval)
        if op_type == OperationType.DESTRUCTIVE:
            if not self.rule.require_approval_for_destructive or has_approval:
                return PolicyEvaluationResult(
                    allowed=True, requires_approval=False, reason="Destructive operation approved"
                )
            return PolicyEvaluationResult(
                allowed=False,
                requires_approval=True,
                approval_type="destructive_operation",
                reason="Destructive repository operation requires explicit human approval",
            )

        # 3. Paid operations (Require approval unless policy disabled or approval granted)
        if op_type == OperationType.PAID_OPERATION or cost_class in (
            CostClass.METERED,
            CostClass.PAID,
            CostClass.EXPENSIVE,
        ):
            if not self.rule.require_approval_for_paid or has_approval:
                return PolicyEvaluationResult(
                    allowed=True, requires_approval=False, reason="Paid operation approved"
                )
            return PolicyEvaluationResult(
                allowed=False,
                requires_approval=True,
                approval_type="paid_generation",
                reason="Paid external operation requires human approval per cost safety policy",
            )

        # 4. Repository write operations
        if op_type == OperationType.REPOSITORY_WRITE:
            if self.rule.require_approval_for_repo_write and not has_approval:
                return PolicyEvaluationResult(
                    allowed=False,
                    requires_approval=True,
                    approval_type="repository_write",
                    reason="Repository modification requires approval",
                )
            return PolicyEvaluationResult(
                allowed=True, requires_approval=False, reason="Repository write permitted"
            )

        # 5. Process execution and local reads
        if op_type in (
            OperationType.LOCAL_READ,
            OperationType.PROCESS_EXECUTION,
            OperationType.FREE_EXTERNAL,
        ):
            return PolicyEvaluationResult(
                allowed=True, requires_approval=False, reason="Operation permitted by default"
            )

        # Unknown / default reject
        raise PolicyViolation(f"Unrecognized operation type '{op_type}'")
