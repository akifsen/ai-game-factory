"""Human-in-the-loop approval service.

Governs approval creation, inspection, and explicit human decisions:
- APPROVED
- REJECTED
- CHANGES_REQUESTED

Enforces immutable input binding: approvals bind a cryptographic fingerprint
of operation inputs. If inputs change before execution, the approval is invalid.
"""

import hashlib
import json
from typing import Any

from gamefactory.core.domain.errors import ApprovalRequired, ValidationError
from gamefactory.core.domain.models import (
    ApprovalRequest,
    ApprovalStatus,
    CostClass,
    generate_id,
    utc_now_iso,
)


def compute_operation_hash(task_id: str, approval_type: str, inputs: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 fingerprint for operation inputs.

    Inputs are normalized and sorted. Secrets are not included in fingerprints.
    """
    normalized_json = json.dumps(
        {"task_id": task_id, "approval_type": approval_type, "inputs": inputs},
        sort_keys=True,
        allow_nan=False,
    )
    return hashlib.sha256(normalized_json.encode("utf-8")).hexdigest()


class ApprovalService:
    """Manages creation, evaluation, and decision recording for human approvals."""

    @staticmethod
    def create_request(
        workflow_id: str,
        task_id: str,
        approval_type: str,
        reason: str,
        cost_class: CostClass = CostClass.LOCAL,
        operation_inputs: dict[str, Any] | None = None,
        artifact_ids: list[str] | None = None,
    ) -> ApprovalRequest:
        """Create a new pending approval request binding operation inputs."""
        inputs = operation_inputs or {}
        try:
            json.dumps(inputs, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Approval inputs must be finite JSON values") from exc
        op_hash = compute_operation_hash(task_id, approval_type, inputs)

        return ApprovalRequest(
            id=generate_id("APP"),
            workflow_id=workflow_id,
            task_id=task_id,
            approval_type=approval_type,
            status=ApprovalStatus.PENDING,
            reason=reason,
            cost_class=cost_class,
            operation_hash=op_hash,
            actor=None,
            comment=None,
            requested_at=utc_now_iso(),
            decided_at=None,
            artifact_ids=artifact_ids or [],
        )

    @staticmethod
    def approve(
        request: ApprovalRequest,
        actor: str,
        comment: str | None = None,
        current_inputs: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        """Record human approval decision.

        Verifies that current operation inputs match the fingerprint created
        when the request was opened.
        """
        if request.status != ApprovalStatus.PENDING:
            raise ValidationError(
                f"Cannot approve request '{request.id}': current status is {request.status.value}, expected PENDING",
                details={"approval_id": request.id, "current_status": request.status.value},
            )

        if current_inputs is not None:
            current_hash = compute_operation_hash(
                request.task_id, request.approval_type, current_inputs
            )
            if current_hash != request.operation_hash:
                raise ApprovalRequired(
                    f"Operation inputs for '{request.task_id}' have changed since approval was requested. A new approval is required.",
                    approval_id=request.id,
                    details={
                        "task_id": request.task_id,
                        "expected_hash": request.operation_hash,
                        "current_hash": current_hash,
                    },
                )

        request.status = ApprovalStatus.APPROVED
        request.actor = actor.strip() or "operator"
        request.comment = comment.strip() if comment else None
        request.decided_at = utc_now_iso()
        return request

    @staticmethod
    def reject(
        request: ApprovalRequest,
        actor: str,
        comment: str | None = None,
    ) -> ApprovalRequest:
        """Record human rejection decision."""
        if request.status != ApprovalStatus.PENDING:
            raise ValidationError(
                f"Cannot reject request '{request.id}': current status is {request.status.value}, expected PENDING",
                details={"approval_id": request.id, "current_status": request.status.value},
            )

        request.status = ApprovalStatus.REJECTED
        request.actor = actor.strip() or "operator"
        request.comment = comment.strip() if comment else None
        request.decided_at = utc_now_iso()
        return request

    @staticmethod
    def request_changes(
        request: ApprovalRequest,
        actor: str,
        comment: str | None = None,
    ) -> ApprovalRequest:
        """Record human changes requested decision."""
        if request.status != ApprovalStatus.PENDING:
            raise ValidationError(
                f"Cannot request changes for request '{request.id}': current status is {request.status.value}, expected PENDING",
                details={"approval_id": request.id, "current_status": request.status.value},
            )

        request.status = ApprovalStatus.CHANGES_REQUESTED
        request.actor = actor.strip() or "operator"
        request.comment = comment.strip() if comment else None
        request.decided_at = utc_now_iso()
        return request
