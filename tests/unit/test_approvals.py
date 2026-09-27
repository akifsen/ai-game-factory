"""Unit tests for ApprovalService."""

import pytest

from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.domain.errors import ApprovalRequired, ValidationError
from gamefactory.core.domain.models import ApprovalStatus, CostClass


class TestApprovalService:
    def test_create_and_approve_with_matching_inputs(self) -> None:
        inputs = {"prompt": "heavy mechanized enemy", "max_triangles": 15000}
        req = ApprovalService.create_request(
            workflow_id="WF-001",
            task_id="TASK-PAID",
            approval_type="paid_generation",
            reason="Paid 3D generation",
            cost_class=CostClass.PAID,
            operation_inputs=inputs,
        )
        assert req.status == ApprovalStatus.PENDING
        assert req.operation_hash != ""

        # Approve with same inputs
        approved = ApprovalService.approve(
            req,
            actor="LeadArtist",
            comment="Approved high poly budget",
            current_inputs=inputs,
        )
        assert approved.status == ApprovalStatus.APPROVED
        assert approved.actor == "LeadArtist"
        assert approved.comment == "Approved high poly budget"
        assert approved.decided_at is not None

    def test_approve_with_tampered_inputs_fails(self) -> None:
        original_inputs = {"prompt": "heavy mechanized enemy", "max_triangles": 15000}
        req = ApprovalService.create_request(
            workflow_id="WF-001",
            task_id="TASK-PAID",
            approval_type="paid_generation",
            reason="Paid 3D generation",
            cost_class=CostClass.PAID,
            operation_inputs=original_inputs,
        )

        tampered_inputs = {"prompt": "different prompt entirely", "max_triangles": 15000}
        with pytest.raises(ApprovalRequired, match="inputs.*have changed"):
            ApprovalService.approve(
                req,
                actor="LeadArtist",
                current_inputs=tampered_inputs,
            )

    def test_reject_and_request_changes(self) -> None:
        req = ApprovalService.create_request(
            workflow_id="WF-002",
            task_id="TASK-002",
            approval_type="design_review",
            reason="Game vision check",
        )
        rejected = ApprovalService.reject(req, actor="Director", comment="Not matching art bible")
        assert rejected.status == ApprovalStatus.REJECTED
        assert rejected.comment == "Not matching art bible"

        # Cannot re-decide already decided request
        with pytest.raises(ValidationError, match="expected PENDING"):
            ApprovalService.approve(rejected, actor="AnotherActor")
