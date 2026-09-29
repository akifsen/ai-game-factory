"""Regression tests for `accounting reconcile` release safety and apply-time consistency.

Covers:
- A paid task type that never journals a durable intent (legacy ``paid_generation``) is
  never released just because no intent exists, even when it really was charged.
- An asset paid generation that crashed before provider contact can still be released.
- Ambiguous submissions (SUBMITTING/UNCERTAIN intent, RUNNING attempt, external
  operation id, recorded invocation) are refused.
- The apply transaction recomputes the project balance, so a concurrent spend on
  another workflow is reflected in the audit totals.
- A plan that changed between preview and apply is refused with a state-changed error.

Interleavings are driven deterministically by hooking ``Database.transaction`` (the
apply path's write transaction), never by sleeping.
"""

from __future__ import annotations

import argparse
import contextlib
import json
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import AuditLogRepository, CostLedgerRepository
from gamefactory.cli.main import EXIT_APPROVAL_BLOCKED, EXIT_SUCCESS, _accounting_reconcile, main
from gamefactory.core.domain.errors import FactoryError
from gamefactory.core.domain.models import utc_now_iso

PROJECT_ID = "reconcile-safety"
WF_ID = "WF-RECON-001"
TASK_ID = f"{WF_ID}-PAID"


def _project(tmp_path: Path) -> tuple[Path, Database]:
    root = tmp_path / "game"
    factory_dir = root / ".gamefactory"
    (factory_dir / "state").mkdir(parents=True)
    (factory_dir / "locks").mkdir(parents=True)
    (factory_dir / "factory.yml").write_text(
        f"schema_version: '0.1.0'\nproject:\n  id: {PROJECT_ID}\n  name: Reconcile Safety\n"
        "  version: '0.1.0'\nengine:\n  type: godot\n",
        encoding="utf-8",
    )
    db = Database(factory_dir / "state" / "factory.db")
    MigrationRunner(db).apply_all()
    now = utc_now_iso()
    with contextlib.closing(db.connect()) as conn:
        conn.execute(
            "INSERT INTO projects (id, name, engine_type, root_path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (PROJECT_ID, "Reconcile Safety", "godot", str(root), now, now),
        )
        conn.commit()
    return root, db


def _workflow(db: Database, wf_id: str, task_id: str, task_type: str, task_status: str) -> None:
    now = utc_now_iso()
    with contextlib.closing(db.connect()) as conn:
        conn.execute(
            "INSERT INTO workflows (id, project_id, name, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (wf_id, PROJECT_ID, wf_id, "BLOCKED", now, now),
        )
        conn.execute(
            """
            INSERT INTO tasks (
                id, workflow_id, name, task_type, cost_class, depends_on_json,
                status, parameters_json, max_retries, timeout_seconds, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'PAID', '[]', ?, '{}', 1, 60.0, ?, ?)
            """,
            (task_id, wf_id, task_id, task_type, task_status, now, now),
        )
        conn.commit()


def _execution(
    db: Database, task_id: str, status: str, *, attempt: int = 1, external_op_id: str | None = None
) -> None:
    now = utc_now_iso()
    with contextlib.closing(db.connect()) as conn:
        conn.execute(
            """
            INSERT INTO executions (
                id, task_id, attempt_number, status, started_at, completed_at, external_op_id,
                cost, estimated_cost, cost_unit, retryable
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 20.0, 20.0, 'credits', 0)
            """,
            (f"EXEC-{task_id}-{attempt}", task_id, attempt, status, now, now, external_op_id),
        )
        conn.commit()


def _intent(
    db: Database,
    wf_id: str,
    task_id: str,
    status: str,
    *,
    actual_cost: float | None = None,
    external_task_id: str | None = None,
) -> None:
    now = utc_now_iso()
    with contextlib.closing(db.connect()) as conn:
        conn.execute(
            """
            INSERT INTO provider_operation_intents (
                id, workflow_id, task_id, asset_id, revision_number, provider, operation,
                concept_hash, request_fingerprint, approval_id, estimated_cost, actual_cost,
                cost_unit, external_task_id, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'pickup_energy_cell_01', 2, 'meshy', 'image-to-3d',
                      'concept-hash', ?, 'APP-1', 20.0, ?, 'credits', ?, ?, ?, ?)
            """,
            (
                f"INTENT-{task_id}",
                wf_id,
                task_id,
                f"fp-{task_id}",
                actual_cost,
                external_task_id,
                status,
                now,
                now,
            ),
        )
        conn.commit()


def _ledger(db: Database, wf_id: str, task_id: str, entry_type: str, amount: float) -> None:
    with contextlib.closing(db.connect()) as conn:
        conn.execute(
            """
            INSERT INTO cost_ledger (
                id, project_id, workflow_id, task_id, entry_type, amount, cost_unit,
                reason, source, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'credits', 'seed', 'test', 'test', ?)
            """,
            (
                f"LEDGER-{task_id}-{entry_type}-{amount}",
                PROJECT_ID,
                wf_id,
                task_id,
                entry_type,
                amount,
                utc_now_iso(),
            ),
        )
        conn.commit()


def _ledger_count(db: Database) -> int:
    with contextlib.closing(db.connect()) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0])


def _args(apply: bool, **extra: Any) -> argparse.Namespace:
    return argparse.Namespace(
        workflow=WF_ID,
        task=None,
        apply=apply,
        actor="lead" if apply else None,
        reason="reconcile test" if apply else None,
        **extra,
    )


def _cli(capsys: pytest.CaptureFixture[str], root: Path, *arguments: str) -> tuple[int, Any]:
    code = main(["accounting", "reconcile", *arguments, "--project", str(root), "--json"])
    captured = capsys.readouterr()
    return code, json.loads(captured.out.strip() or captured.err.strip())


@contextlib.contextmanager
def _before_apply_transaction(
    monkeypatch: pytest.MonkeyPatch, interleave: Callable[[], None]
) -> Generator[None, None, None]:
    """Run ``interleave`` once, after the preview and before the apply write transaction."""
    original = Database.transaction
    fired: list[bool] = []

    @contextlib.contextmanager
    def hooked(self: Database) -> Generator[Any, None, None]:
        if not fired:
            fired.append(True)
            interleave()
        with original(self) as conn:
            yield conn

    monkeypatch.setattr(Database, "transaction", hooked)
    yield
    monkeypatch.setattr(Database, "transaction", original)
    assert fired, "the apply transaction never ran"


# --- Finding 1: RELEASE only where the intent-before-contact invariant holds ----------


@pytest.mark.parametrize(
    ("execution_status", "task_status"),
    [("COMPLETED", "COMPLETED"), ("FAILED", "FAILED"), ("UNCERTAIN", "BLOCKED")],
)
def test_charged_legacy_paid_task_without_intent_is_never_released(
    tmp_path: Path, execution_status: str, task_status: str
) -> None:
    """A historical ``paid_generation`` that really was charged keeps its hold."""
    _, db = _project(tmp_path)
    _workflow(db, WF_ID, TASK_ID, "paid_generation", task_status)
    _execution(db, TASK_ID, execution_status)
    _ledger(db, WF_ID, TASK_ID, "RESERVE", 20.0)

    payload, code, human = _accounting_reconcile(db, _args(apply=False))
    assert code == EXIT_SUCCESS
    op = payload["operations"][0]
    assert op["eligible"] is False
    assert op["manual_evidence_required"] is True
    assert op["proposed_entries"] == []
    assert "manual provider evidence required" in op["reason"]
    assert "manual evidence required" in (human or "")

    payload, code, _ = _accounting_reconcile(db, _args(apply=True))
    assert code == EXIT_SUCCESS
    assert payload["applied"] is False
    assert CostLedgerRepository(db).project_net(PROJECT_ID) == 20.0
    assert _ledger_count(db) == 1


def test_asset_generation_crashed_before_provider_contact_is_released(tmp_path: Path) -> None:
    _, db = _project(tmp_path)
    _workflow(db, WF_ID, TASK_ID, "asset_paid_generation", "BLOCKED")
    _execution(db, TASK_ID, "UNCERTAIN")
    _ledger(db, WF_ID, TASK_ID, "RESERVE", 20.0)

    preview, _, _ = _accounting_reconcile(db, _args(apply=False))
    op = preview["operations"][0]
    assert op["eligible"] is True
    assert op["manual_evidence_required"] is False
    assert [(e["entry_type"], e["amount"]) for e in op["proposed_entries"]] == [("RELEASE", 20.0)]

    payload, code, _ = _accounting_reconcile(db, _args(apply=True))
    assert code == EXIT_SUCCESS and payload["applied"] is True
    assert (payload["spend_before"], payload["spend_after"], payload["net_change"]) == (
        20.0,
        0.0,
        -20.0,
    )
    assert CostLedgerRepository(db).project_net(PROJECT_ID) == 0.0


@pytest.mark.parametrize(
    "scenario",
    [
        "intent_submitting",
        "intent_uncertain",
        "intent_submitted_with_remote_id",
        "attempt_running",
        "attempt_external_op_id",
        "recorded_invocation",
        "attempt_completed",
    ],
)
def test_ambiguous_asset_submission_is_refused(tmp_path: Path, scenario: str) -> None:
    _, db = _project(tmp_path)
    _workflow(db, WF_ID, TASK_ID, "asset_paid_generation", "BLOCKED")
    _ledger(db, WF_ID, TASK_ID, "RESERVE", 20.0)
    if scenario == "intent_submitting":
        _execution(db, TASK_ID, "UNCERTAIN")
        _intent(db, WF_ID, TASK_ID, "SUBMITTING")
    elif scenario == "intent_uncertain":
        _execution(db, TASK_ID, "UNCERTAIN")
        _intent(db, WF_ID, TASK_ID, "UNCERTAIN")
    elif scenario == "intent_submitted_with_remote_id":
        _execution(db, TASK_ID, "FAILED")
        _intent(db, WF_ID, TASK_ID, "SUBMITTED", external_task_id="remote-1")
    elif scenario == "attempt_running":
        _execution(db, TASK_ID, "RUNNING")
    elif scenario == "attempt_external_op_id":
        _execution(db, TASK_ID, "FAILED", external_op_id="remote-2")
    elif scenario == "recorded_invocation":
        _execution(db, TASK_ID, "FAILED")
        with contextlib.closing(db.connect()) as conn:
            conn.execute(
                "INSERT INTO provider_invocations (id, workflow_id, task_id, provider, "
                "operation_hash, invoked_at) VALUES ('CALL-1', ?, ?, 'meshy', 'h', ?)",
                (WF_ID, TASK_ID, utc_now_iso()),
            )
            conn.commit()
    elif scenario == "attempt_completed":
        _execution(db, TASK_ID, "COMPLETED")

    payload, _, _ = _accounting_reconcile(db, _args(apply=False))
    op = payload["operations"][0]
    assert op["eligible"] is False
    assert op["manual_evidence_required"] is True
    assert "provider evidence" in op["reason"]

    payload, code, _ = _accounting_reconcile(db, _args(apply=True))
    assert code == EXIT_SUCCESS and payload["applied"] is False
    assert CostLedgerRepository(db).project_net(PROJECT_ID) == 20.0


# --- Finding 2: apply-time consistency -------------------------------------------------


def _settleable_pickup(db: Database) -> None:
    """The pickup r002 shape: terminal SUCCEEDED intent, verified actual 15, hold 20."""
    _workflow(db, WF_ID, TASK_ID, "asset_paid_generation", "COMPLETED")
    _execution(db, TASK_ID, "COMPLETED", external_op_id="remote-pickup")
    _intent(db, WF_ID, TASK_ID, "SUCCEEDED", actual_cost=15.0, external_task_id="remote-pickup")
    _ledger(db, WF_ID, TASK_ID, "RESERVE", 20.0)


def test_concurrent_spend_is_reflected_in_audit_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, db = _project(tmp_path)
    _settleable_pickup(db)
    _workflow(db, "WF-OTHER", "WF-OTHER-PAID", "asset_paid_generation", "RUNNING")

    def other_workflow_reserves() -> None:
        _ledger(db, "WF-OTHER", "WF-OTHER-PAID", "RESERVE", 7.0)

    with _before_apply_transaction(monkeypatch, other_workflow_reserves):
        payload, code, _ = _accounting_reconcile(db, _args(apply=True))

    assert code == EXIT_SUCCESS and payload["applied"] is True
    # Balance read inside the apply transaction: 20 (hold) + 7 (concurrent) = 27.
    assert payload["spend_before"] == 27.0
    assert payload["spend_after"] == 22.0
    assert payload["net_change"] == -5.0
    assert [(e["entry_type"], e["amount"]) for e in payload["applied_entries"]] == [
        ("SETTLE", 15.0),
        ("RELEASE", 20.0),
    ]
    audit = [
        e
        for e in AuditLogRepository(db).list_by_entity("Workflow", WF_ID)
        if e.action == "ACCOUNTING_RECONCILED"
    ]
    assert len(audit) == 1
    details = audit[0].details
    assert (details["spend_before"], details["spend_after"], details["net_change"]) == (
        27.0,
        22.0,
        -5.0,
    )
    assert CostLedgerRepository(db).project_net(PROJECT_ID) == 22.0

    # Second apply is an idempotent no-op.
    again, code, _ = _accounting_reconcile(db, _args(apply=True))
    assert code == EXIT_SUCCESS
    assert again["applied"] is False and again["message"] == "Nothing to reconcile"
    assert _ledger_count(db) == 4


@pytest.mark.parametrize("change", ["actual_cost", "already_settled", "hold_changed"])
def test_plan_change_between_preview_and_apply_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    _, db = _project(tmp_path)
    _settleable_pickup(db)

    def mutate() -> None:
        if change == "actual_cost":
            with contextlib.closing(db.connect()) as conn:
                conn.execute(
                    "UPDATE provider_operation_intents SET actual_cost = 18.0 WHERE task_id = ?",
                    (TASK_ID,),
                )
                conn.commit()
        elif change == "already_settled":
            _ledger(db, WF_ID, TASK_ID, "SETTLE", 15.0)
            _ledger(db, WF_ID, TASK_ID, "RELEASE", 20.0)
        else:
            _ledger(db, WF_ID, TASK_ID, "RELEASE", 5.0)

    rows_before_apply = _ledger_count(db)
    with _before_apply_transaction(monkeypatch, mutate), pytest.raises(FactoryError) as err:
        _accounting_reconcile(db, _args(apply=True))
    assert err.value.code == "RECONCILIATION_STATE_CHANGED"

    written_by_mutation = {"actual_cost": 0, "already_settled": 2, "hold_changed": 1}[change]
    assert _ledger_count(db) == rows_before_apply + written_by_mutation
    assert not [
        e
        for e in AuditLogRepository(db).list_by_entity("Workflow", WF_ID)
        if e.action == "ACCOUNTING_RECONCILED"
    ]


def test_apply_with_stale_plan_hash_from_dry_run_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, db = _project(tmp_path)
    _settleable_pickup(db)

    code, dry = _cli(capsys, root, "--workflow", WF_ID)
    assert code == EXIT_SUCCESS
    reviewed_hash = dry["plan_hash"]
    assert dry["net_change"] == -5.0

    with contextlib.closing(db.connect()) as conn:
        conn.execute(
            "UPDATE provider_operation_intents SET actual_cost = 12.0 WHERE task_id = ?",
            (TASK_ID,),
        )
        conn.commit()

    common = ["--workflow", WF_ID, "--apply", "--actor", "lead", "--reason", "r"]
    code, err = _cli(capsys, root, *common, "--plan-hash", reviewed_hash)
    assert code == EXIT_APPROVAL_BLOCKED
    assert err["error"] == "RECONCILIATION_STATE_CHANGED"
    assert _ledger_count(db) == 1

    # A fresh dry-run gives the new plan; applying that exact plan succeeds.
    _, fresh = _cli(capsys, root, "--workflow", WF_ID)
    assert fresh["plan_hash"] != reviewed_hash
    code, applied = _cli(capsys, root, *common, "--plan-hash", fresh["plan_hash"])
    assert code == EXIT_SUCCESS and applied["applied"] is True
    assert applied["net_change"] == -8.0

    # Re-running the same apply command is an idempotent no-op.
    code, again = _cli(capsys, root, *common, "--plan-hash", fresh["plan_hash"])
    assert code == EXIT_SUCCESS and again["applied"] is False
    assert _ledger_count(db) == 3
