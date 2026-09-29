"""Pure domain module for append-only cost ledger accounting.

Provides primitives for managing committed budget, reservations, settlements,
releases, and adjustments across workflow executions.
No database or I/O dependencies are permitted in this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.models import generate_id, utc_now_iso


class EntryType(StrEnum):
    """Categorization of cost ledger entries."""

    RESERVE = "RESERVE"
    SETTLE = "SETTLE"
    RELEASE = "RELEASE"
    ADJUSTMENT = "ADJUSTMENT"


@dataclass
class LedgerEntry:
    """An immutable financial ledger entry recording a reservation or charge."""

    id: str = field(default_factory=lambda: generate_id("LEDGER"))
    project_id: str = ""
    workflow_id: str = ""
    task_id: str = ""
    execution_id: str | None = None
    intent_id: str | None = None
    request_fingerprint: str | None = None
    entry_type: EntryType = EntryType.RESERVE
    amount: float = 0.0
    cost_unit: str = "credits"
    reason: str = ""
    source: str = ""
    actor: str = ""
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if isinstance(self.entry_type, str):
            self.entry_type = EntryType(self.entry_type)
        self.amount = float(self.amount)
        if not math.isfinite(self.amount):
            raise ValidationError("Ledger entry amount must be finite")
        if self.entry_type in (EntryType.RESERVE, EntryType.SETTLE, EntryType.RELEASE):
            if self.amount < 0:
                raise ValidationError(
                    f"{self.entry_type.value} amount must be non-negative; got {self.amount}"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "workflow_id": self.workflow_id,
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "intent_id": self.intent_id,
            "request_fingerprint": self.request_fingerprint,
            "entry_type": self.entry_type.value,
            "amount": self.amount,
            "cost_unit": self.cost_unit,
            "reason": self.reason,
            "source": self.source,
            "actor": self.actor,
            "created_at": self.created_at,
        }


def signed_amount(entry: LedgerEntry) -> float:
    """Compute the signed contribution of a ledger entry to project spend.

    - RESERVE: +amount (provisional liability)
    - SETTLE: +amount (final committed liability)
    - RELEASE: -amount (unwinding of provisional liability)
    - ADJUSTMENT: stored amount as signed
    """
    etype = EntryType(entry.entry_type) if isinstance(entry.entry_type, str) else entry.entry_type
    if etype == EntryType.RELEASE:
        return -float(entry.amount)
    return float(entry.amount)


@dataclass
class OperationAccount:
    """Aggregated accounting balance for a single paid operation (task)."""

    task_id: str
    reserved_total: float = 0.0
    released_total: float = 0.0
    settled_total: float = 0.0
    adjustments: float = 0.0
    settled: bool = False
    project_id: str = ""
    workflow_id: str = ""
    cost_unit: str = "credits"

    @property
    def held(self) -> float:
        """Currently active reservation held against the budget prior to settlement."""
        return self.reserved_total - self.released_total

    @property
    def net(self) -> float:
        """Net committed spend for this operation."""
        return self.reserved_total - self.released_total + self.settled_total + self.adjustments

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project_id": self.project_id,
            "workflow_id": self.workflow_id,
            "reserved_total": self.reserved_total,
            "released_total": self.released_total,
            "settled_total": self.settled_total,
            "adjustments": self.adjustments,
            "held": self.held,
            "settled": self.settled,
            "net": self.net,
            "cost_unit": self.cost_unit,
        }


def plan_settlement(
    account: OperationAccount,
    actual: float,
    *,
    source: str = "provider_terminal",
    actor: str = "WorkflowEngine",
    reason: str | None = None,
    execution_id: str | None = None,
    intent_id: str | None = None,
    request_fingerprint: str | None = None,
    cost_unit: str | None = None,
) -> list[LedgerEntry]:
    """Plan ledger entries to settle a paid operation to its verified actual cost.

    Creates a SETTLE entry for the actual amount (even if 0) to finalize the operation,
    and a RELEASE entry for the held reservation (omitted if held amount is zero).
    Refuses if the operation is already settled.
    """
    if account.settled:
        raise ValidationError(f"Operation '{account.task_id}' is already settled")
    actual_float = float(actual)
    if not math.isfinite(actual_float) or actual_float < 0:
        raise ValidationError("Settlement actual amount must be a finite non-negative number")

    unit = cost_unit or account.cost_unit or "credits"
    entries: list[LedgerEntry] = []

    # 1. SETTLE entry (authoritative terminal cost)
    entries.append(
        LedgerEntry(
            id=generate_id("LEDGER"),
            project_id=account.project_id,
            workflow_id=account.workflow_id,
            task_id=account.task_id,
            execution_id=execution_id,
            intent_id=intent_id,
            request_fingerprint=request_fingerprint,
            entry_type=EntryType.SETTLE,
            amount=actual_float,
            cost_unit=unit,
            reason=reason
            or f"Settlement of operation {account.task_id} to actual cost {actual_float}",
            source=source,
            actor=actor,
            created_at=utc_now_iso(),
        )
    )

    # 2. RELEASE held (omit zero-amount RELEASE)
    held_amount = account.held
    if held_amount > 0:
        entries.append(
            LedgerEntry(
                id=generate_id("LEDGER"),
                project_id=account.project_id,
                workflow_id=account.workflow_id,
                task_id=account.task_id,
                execution_id=execution_id,
                intent_id=intent_id,
                request_fingerprint=request_fingerprint,
                entry_type=EntryType.RELEASE,
                amount=held_amount,
                cost_unit=unit,
                reason=f"Release of held reservation ({held_amount}) upon settlement",
                source=source,
                actor=actor,
                created_at=utc_now_iso(),
            )
        )

    return entries
