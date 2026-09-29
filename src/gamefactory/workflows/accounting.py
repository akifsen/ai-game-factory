"""Workflow cost accounting service.

Coordinates append-only cost ledger mutations, settlement planning, and audit trail
recording for paid provider operations.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from gamefactory.core.accounting.ledger import (
    EntryType,
    LedgerEntry,
    plan_settlement,
)
from gamefactory.core.domain.models import AuditEvent, generate_id, utc_now_iso

if TYPE_CHECKING:
    from gamefactory.adapters.persistence.repositories import (
        AuditLogRepository,
        CostLedgerRepository,
        ProviderOperationIntent,
    )


class CostAccounting:
    """Service mediating durable financial ledger updates for workflow tasks."""

    def __init__(
        self,
        ledger_repo: CostLedgerRepository,
        audit_repo: AuditLogRepository,
    ) -> None:
        self.ledger_repo = ledger_repo
        self.audit_repo = audit_repo

    def settle_terminal_success(
        self,
        task_id: str,
        actual_cost: float | None,
        *,
        execution_id: str | None = None,
        intent: ProviderOperationIntent | None = None,
        cost_unit: str = "credits",
    ) -> list[LedgerEntry]:
        """Settle a completed paid operation to verified actual provider cost."""
        if actual_cost is None:
            # Unknown actual cost: reservation stays held without settlement
            return []
        actual = float(actual_cost)
        if not math.isfinite(actual) or actual < 0:
            return []

        account = self.ledger_repo.operation_account(task_id)
        if account.settled:
            return []

        intent_id = intent.id if intent else None
        fingerprint = intent.request_fingerprint if intent else None
        unit = getattr(intent, "cost_unit", None) or cost_unit or account.cost_unit

        entries = plan_settlement(
            account,
            actual,
            source="provider_terminal",
            actor="WorkflowEngine",
            execution_id=execution_id,
            intent_id=intent_id,
            request_fingerprint=fingerprint,
            cost_unit=unit,
        )

        audit = AuditEvent(
            id=generate_id("AUDIT"),
            entity_type="Task",
            entity_id=task_id,
            action="COST_SETTLED",
            actor="WorkflowEngine",
            timestamp=utc_now_iso(),
            details={
                "reservation_held": account.held,
                "held": account.held,
                "actual": actual,
                "released": account.held if account.held > 0 else 0.0,
                "net": actual,
                "execution_id": execution_id,
                "intent_id": intent_id,
                "request_fingerprint": fingerprint,
            },
        )
        return self.ledger_repo.append(entries, audit_event=audit)

    def handle_in_flight(
        self,
        task_id: str,
        partial_actual: float | None,
        *,
        execution_id: str | None = None,
        intent: ProviderOperationIntent | None = None,
        cost_unit: str = "credits",
    ) -> list[LedgerEntry]:
        """Handle incremental cost reported while a provider task is still in-flight.

        Top up reservation if partial actual exceeds held reservation; never release
        while in-flight.
        """
        if partial_actual is None:
            return []
        partial = float(partial_actual)
        if not math.isfinite(partial) or partial < 0:
            return []

        account = self.ledger_repo.operation_account(task_id)
        if account.settled:
            return []

        if partial > account.held:
            delta = partial - account.held
            intent_id = intent.id if intent else None
            fingerprint = intent.request_fingerprint if intent else None
            unit = getattr(intent, "cost_unit", None) or cost_unit or account.cost_unit
            entry = LedgerEntry(
                id=generate_id("LEDGER"),
                project_id=account.project_id,
                workflow_id=account.workflow_id,
                task_id=task_id,
                execution_id=execution_id,
                intent_id=intent_id,
                request_fingerprint=fingerprint,
                entry_type=EntryType.RESERVE,
                amount=delta,
                cost_unit=unit,
                reason=f"Top-up reservation for partial actual cost {partial} exceeding held {account.held}",
                source="provider_partial_actual",
                actor="WorkflowEngine",
                created_at=utc_now_iso(),
            )
            return self.ledger_repo.append([entry])
        return []

    def handle_failure(
        self,
        task_id: str,
        *,
        execution_id: str | None = None,
        intent: ProviderOperationIntent | None = None,
        cost_unit: str = "credits",
        submission_uncertain: bool = False,
    ) -> list[LedgerEntry]:
        """Resolve cost accounting when a task execution terminates in failure.

        - If the provider outcome is uncertain: keep the reservation held, even when
          no durable intent exists (the provider may have accepted and charged).
        - If no provider intent exists: release held reservation (source='no_provider_submission').
        - If intent exists and FAILED with known actual: settle to actual.
        - Otherwise (UNCERTAIN, in flight, unknown actual): keep reservation held.
        """
        account = self.ledger_repo.operation_account(task_id)
        if account.settled or submission_uncertain:
            return []

        if intent is None:
            # No provider intent was ever persisted (pre-submission failure)
            if account.held > 0:
                unit = cost_unit or account.cost_unit
                entry = LedgerEntry(
                    id=generate_id("LEDGER"),
                    project_id=account.project_id,
                    workflow_id=account.workflow_id,
                    task_id=task_id,
                    execution_id=execution_id,
                    intent_id=None,
                    request_fingerprint=None,
                    entry_type=EntryType.RELEASE,
                    amount=account.held,
                    cost_unit=unit,
                    reason="Release reservation because no provider submission occurred",
                    source="no_provider_submission",
                    actor="WorkflowEngine",
                    created_at=utc_now_iso(),
                )
                audit = AuditEvent(
                    id=generate_id("AUDIT"),
                    entity_type="Task",
                    entity_id=task_id,
                    action="COST_RELEASED",
                    actor="WorkflowEngine",
                    timestamp=utc_now_iso(),
                    details={
                        "held": account.held,
                        "released": account.held,
                        "net": 0.0,
                        "source": "no_provider_submission",
                        "execution_id": execution_id,
                    },
                )
                return self.ledger_repo.append([entry], audit_event=audit)
            return []

        # Intent exists
        if intent.status == "FAILED" and intent.actual_cost is not None:
            return self.settle_terminal_success(
                task_id,
                intent.actual_cost,
                execution_id=execution_id,
                intent=intent,
                cost_unit=intent.cost_unit,
            )

        # UNCERTAIN / SUBMITTING without external id -> hold
        # FAILED with unknown actual -> hold
        return []

    def settle_builtin_paid(
        self,
        task_id: str,
        cost: float,
        *,
        execution_id: str | None = None,
        cost_unit: str = "credits",
    ) -> list[LedgerEntry]:
        """Settle a completed builtin paid_generation task."""
        account = self.ledger_repo.operation_account(task_id)
        if account.settled:
            return []
        actual = float(cost)
        if not math.isfinite(actual) or actual < 0:
            return []

        entries = plan_settlement(
            account,
            actual,
            source="provider_terminal",
            actor="WorkflowEngine",
            execution_id=execution_id,
            cost_unit=cost_unit or account.cost_unit,
        )
        audit = AuditEvent(
            id=generate_id("AUDIT"),
            entity_type="Task",
            entity_id=task_id,
            action="COST_SETTLED",
            actor="WorkflowEngine",
            timestamp=utc_now_iso(),
            details={
                "reservation_held": account.held,
                "held": account.held,
                "actual": actual,
                "released": account.held if account.held > 0 else 0.0,
                "net": actual,
                "execution_id": execution_id,
            },
        )
        return self.ledger_repo.append(entries, audit_event=audit)
