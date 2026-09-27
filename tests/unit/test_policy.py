"""Unit tests for the centralized PolicyEngine policy matrix."""

import pytest

from gamefactory.core.domain.errors import BudgetExceeded, PolicyViolation
from gamefactory.core.domain.models import CostClass
from gamefactory.core.policies.policy_engine import (
    OperationType,
    PolicyEngine,
    PolicyRule,
)


class TestPolicyEngine:
    def setup_method(self) -> None:
        self.engine = PolicyEngine(
            PolicyRule(
                require_approval_for_paid=True,
                require_approval_for_destructive=True,
                require_approval_for_repo_write=False,
                max_operation_cost=50.0,
                project_budget=200.0,
            )
        )

    def test_local_read_allowed_without_approval(self) -> None:
        res = self.engine.evaluate(OperationType.LOCAL_READ)
        assert res.allowed is True
        assert res.requires_approval is False

    def test_process_execution_allowed_without_approval(self) -> None:
        res = self.engine.evaluate(OperationType.PROCESS_EXECUTION)
        assert res.allowed is True
        assert res.requires_approval is False

    def test_free_external_allowed_without_approval(self) -> None:
        res = self.engine.evaluate(OperationType.FREE_EXTERNAL, cost_class=CostClass.FREE_EXTERNAL)
        assert res.allowed is True
        assert res.requires_approval is False

    def test_repo_write_without_approval_when_rule_disabled(self) -> None:
        res = self.engine.evaluate(OperationType.REPOSITORY_WRITE)
        assert res.allowed is True
        assert res.requires_approval is False

    def test_repo_write_requires_approval_when_configured(self) -> None:
        engine = PolicyEngine(PolicyRule(require_approval_for_repo_write=True))
        res = engine.evaluate(OperationType.REPOSITORY_WRITE, has_approval=False)
        assert res.allowed is False
        assert res.requires_approval is True
        assert res.approval_type == "repository_write"

        res_approved = engine.evaluate(OperationType.REPOSITORY_WRITE, has_approval=True)
        assert res_approved.allowed is True
        assert res_approved.requires_approval is False

    def test_paid_operation_blocked_without_approval(self) -> None:
        res = self.engine.evaluate(
            OperationType.PAID_OPERATION,
            cost_class=CostClass.PAID,
            estimated_cost=10.0,
            has_approval=False,
        )
        assert res.allowed is False
        assert res.requires_approval is True
        assert res.approval_type == "paid_generation"

    def test_paid_operation_allowed_with_approval(self) -> None:
        res = self.engine.evaluate(
            OperationType.PAID_OPERATION,
            cost_class=CostClass.PAID,
            estimated_cost=10.0,
            has_approval=True,
        )
        assert res.allowed is True
        assert res.requires_approval is False

    def test_destructive_operation_blocked_without_approval(self) -> None:
        res = self.engine.evaluate(OperationType.DESTRUCTIVE, has_approval=False)
        assert res.allowed is False
        assert res.requires_approval is True
        assert res.approval_type == "destructive_operation"

    def test_destructive_operation_allowed_with_approval(self) -> None:
        res = self.engine.evaluate(OperationType.DESTRUCTIVE, has_approval=True)
        assert res.allowed is True
        assert res.requires_approval is False

    def test_budget_exceeded_per_operation(self) -> None:
        with pytest.raises(BudgetExceeded, match="exceeds max per-operation limit"):
            self.engine.evaluate(
                OperationType.PAID_OPERATION,
                cost_class=CostClass.PAID,
                estimated_cost=60.0,  # Max is 50.0
            )

    def test_budget_exceeded_total_project(self) -> None:
        with pytest.raises(BudgetExceeded, match="exceed total project budget"):
            self.engine.evaluate(
                OperationType.PAID_OPERATION,
                cost_class=CostClass.PAID,
                estimated_cost=30.0,
                current_spent=180.0,  # 180 + 30 = 210 > 200 budget
            )

    @pytest.mark.parametrize("cost_class", [CostClass.METERED, CostClass.PAID, CostClass.EXPENSIVE])
    def test_external_cost_classes_require_approval_even_for_free_operation(
        self, cost_class: CostClass
    ) -> None:
        result = self.engine.evaluate(
            OperationType.FREE_EXTERNAL,
            cost_class=cost_class,
            estimated_cost=1.0,
        )
        assert result.requires_approval is True
        assert result.allowed is False

    @pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf"), float("-inf")])
    def test_invalid_estimated_cost_is_rejected(self, value: float) -> None:
        with pytest.raises(PolicyViolation):
            self.engine.evaluate(OperationType.LOCAL_READ, estimated_cost=value)

    @pytest.mark.parametrize("field", ["max_operation_cost", "project_budget"])
    @pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf")])
    def test_invalid_budget_configuration_is_rejected(self, field: str, value: float) -> None:
        with pytest.raises(PolicyViolation):
            PolicyRule(**{field: value})

    @pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf")])
    def test_invalid_current_spend_is_rejected(self, value: float) -> None:
        with pytest.raises(PolicyViolation):
            self.engine.evaluate(OperationType.LOCAL_READ, current_spent=value)
