"""Unit tests for cost ledger models, settlement planning, immutability triggers, and repository."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import CostLedgerRepository
from gamefactory.core.accounting.ledger import (
    EntryType,
    LedgerEntry,
    OperationAccount,
    plan_settlement,
    signed_amount,
)
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.models import AuditEvent, generate_id, utc_now_iso


def test_ledger_entry_validations() -> None:
    entry = LedgerEntry(
        project_id="proj-1",
        workflow_id="wf-1",
        task_id="task-1",
        entry_type=EntryType.RESERVE,
        amount=25.0,
        cost_unit="credits",
        reason="Initial reservation",
        source="test",
        actor="tester",
    )
    assert entry.amount == 25.0
    assert entry.entry_type == EntryType.RESERVE
    assert signed_amount(entry) == 25.0

    # Negative amount on RESERVE raises ValidationError
    with pytest.raises(ValidationError, match="amount must be non-negative"):
        LedgerEntry(entry_type=EntryType.RESERVE, amount=-5.0)

    # Negative amount on SETTLE raises ValidationError
    with pytest.raises(ValidationError, match="amount must be non-negative"):
        LedgerEntry(entry_type=EntryType.SETTLE, amount=-1.0)

    # Negative amount on RELEASE raises ValidationError
    with pytest.raises(ValidationError, match="amount must be non-negative"):
        LedgerEntry(entry_type=EntryType.RELEASE, amount=-1.0)

    # Signed amount for RELEASE is negative
    rel = LedgerEntry(entry_type=EntryType.RELEASE, amount=10.0)
    assert signed_amount(rel) == -10.0

    # ADJUSTMENT can be signed
    adj_pos = LedgerEntry(entry_type=EntryType.ADJUSTMENT, amount=15.0)
    assert signed_amount(adj_pos) == 15.0
    adj_neg = LedgerEntry(entry_type=EntryType.ADJUSTMENT, amount=-15.0)
    assert signed_amount(adj_neg) == -15.0


def test_operation_account_and_plan_settlement() -> None:
    acct = OperationAccount(
        task_id="task-100",
        project_id="p-1",
        workflow_id="wf-1",
        reserved_total=20.0,
        released_total=0.0,
    )
    assert acct.held == 20.0
    assert not acct.settled
    assert acct.net == 20.0

    # Plan settlement to actual 15
    entries = plan_settlement(acct, 15.0)
    assert len(entries) == 2
    assert entries[0].entry_type == EntryType.SETTLE
    assert entries[0].amount == 15.0
    assert entries[1].entry_type == EntryType.RELEASE
    assert entries[1].amount == 20.0

    # Net contribution of planned settlement
    net_contrib = sum(signed_amount(e) for e in entries)
    assert net_contrib == 15.0 - 20.0  # -5.0
    assert acct.net + net_contrib == 15.0

    # Omit zero-amount release when held is 0
    zero_held_acct = OperationAccount(
        task_id="task-101",
        project_id="p-1",
        workflow_id="wf-1",
        reserved_total=0.0,
        released_total=0.0,
    )
    entries_zero = plan_settlement(zero_held_acct, 10.0)
    assert len(entries_zero) == 1
    assert entries_zero[0].entry_type == EntryType.SETTLE
    assert entries_zero[0].amount == 10.0

    # SETTLE even if actual is 0.0 to mark settled
    entries_free = plan_settlement(acct, 0.0)
    assert len(entries_free) == 2
    assert entries_free[0].entry_type == EntryType.SETTLE
    assert entries_free[0].amount == 0.0
    assert entries_free[1].entry_type == EntryType.RELEASE
    assert entries_free[1].amount == 20.0

    # Refuse to plan if already settled
    settled_acct = OperationAccount(task_id="task-102", settled=True)
    with pytest.raises(ValidationError, match="already settled"):
        plan_settlement(settled_acct, 10.0)


def test_cost_ledger_immutability_triggers(tmp_path: Path) -> None:
    """Requirement 12: Ledger immutability: direct UPDATE/DELETE raises sqlite3 error."""
    db_file = tmp_path / "test.db"
    db = Database(db_file)
    MigrationRunner(db).apply_all()
    repo = CostLedgerRepository(db)

    entry = LedgerEntry(
        id="LEDGER-IMMUTABLE-01",
        project_id="p-1",
        workflow_id="wf-1",
        task_id="task-1",
        entry_type=EntryType.RESERVE,
        amount=50.0,
        cost_unit="credits",
        reason="Immutability probe",
        source="test",
        actor="test_runner",
    )
    repo.append([entry])
    assert repo.project_net("p-1") == 50.0

    conn = db.connect()
    try:
        # Direct UPDATE must raise sqlite3 DatabaseError/IntegrityError via BEFORE UPDATE trigger
        with pytest.raises(
            (sqlite3.DatabaseError, sqlite3.IntegrityError), match="updates are forbidden"
        ):
            conn.execute("UPDATE cost_ledger SET amount = 100.0 WHERE id = 'LEDGER-IMMUTABLE-01'")

        # Direct DELETE must raise sqlite3 DatabaseError/IntegrityError via BEFORE DELETE trigger
        with pytest.raises(
            (sqlite3.DatabaseError, sqlite3.IntegrityError), match="deletes are forbidden"
        ):
            conn.execute("DELETE FROM cost_ledger WHERE id = 'LEDGER-IMMUTABLE-01'")
    finally:
        conn.close()

    # Verify amount and entry survived unmutated
    entries = repo.list_by_workflow("wf-1")
    assert len(entries) == 1
    assert entries[0].amount == 50.0


def test_cost_ledger_repository_operations(tmp_path: Path) -> None:
    db = Database(tmp_path / "repo.db")
    MigrationRunner(db).apply_all()
    repo = CostLedgerRepository(db)

    # Initially zero
    assert repo.project_net("proj-x") == 0.0
    acct = repo.operation_account("task-xyz")
    assert acct.held == 0.0
    assert not acct.settled

    # Append reserve
    r1 = LedgerEntry(
        project_id="proj-x",
        workflow_id="wf-x",
        task_id="task-xyz",
        entry_type=EntryType.RESERVE,
        amount=20.0,
        reason="Reserve 20",
        source="claim",
        actor="engine",
    )
    audit = AuditEvent(
        id=generate_id("AUDIT"),
        entity_type="Task",
        entity_id="task-xyz",
        action="RESERVED",
        actor="engine",
        timestamp=utc_now_iso(),
    )
    repo.append([r1], audit_event=audit)

    assert repo.project_net("proj-x") == 20.0
    acct = repo.operation_account("task-xyz")
    assert acct.held == 20.0
    assert not acct.settled

    # Settle to 15
    plan = plan_settlement(acct, 15.0)
    repo.append(plan)

    assert repo.project_net("proj-x") == 15.0
    acct_settled = repo.operation_account("task-xyz")
    assert acct_settled.settled
    assert acct_settled.held == 0.0
    assert acct_settled.net == 15.0


def test_uncertain_failure_without_intent_keeps_reservation_held(tmp_path: Path) -> None:
    """A provider outcome that is uncertain must never release its hold, even with no intent."""
    from gamefactory.adapters.persistence.repositories import AuditLogRepository
    from gamefactory.workflows.accounting import CostAccounting

    db = Database(tmp_path / "uncertain.db")
    MigrationRunner(db).apply_all()
    repo = CostLedgerRepository(db)
    accounting = CostAccounting(repo, AuditLogRepository(db))
    repo.append(
        [
            LedgerEntry(
                project_id="proj-u",
                workflow_id="wf-u",
                task_id="task-u",
                entry_type=EntryType.RESERVE,
                amount=20.0,
                reason="Reserve 20",
                source="execution_claim",
                actor="engine",
            )
        ]
    )

    assert accounting.handle_failure("task-u", intent=None, submission_uncertain=True) == []
    assert repo.project_net("proj-u") == 20.0

    released = accounting.handle_failure("task-u", intent=None, submission_uncertain=False)
    assert [entry.entry_type for entry in released] == [EntryType.RELEASE]
    assert repo.project_net("proj-u") == 0.0
