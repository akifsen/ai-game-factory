"""Integration tests for accounting CLI and migration-0007 historical reconciliation.

Covers:
- Schema migration 6 -> 7 carrying forward legacy execution costs into RESERVE entries.
- In-process accounting reconcile dry-run.
- Usage errors when --apply is provided without --actor or --reason.
- Applying reconciliation, verifying project net and audit trail while preserving execution row.
- Idempotency / refusal on second apply.
- Read-only ledger listing via CLI.
- UNCERTAIN intent requiring provider evidence and refusing reconciliation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MIGRATIONS, MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    AuditLogRepository,
    CostLedgerRepository,
)
from gamefactory.cli.main import EXIT_CONFIG_ERROR, EXIT_SUCCESS, main
from gamefactory.config.loader import ConfigLoader
from gamefactory.core.accounting.ledger import EntryType
from gamefactory.core.domain.models import utc_now_iso


def _run_cli(
    capsys: pytest.CaptureFixture[str],
    root: Path,
    *arguments: str,
) -> tuple[int, dict[str, Any]]:
    code = main([*arguments, "--project", str(root), "--json"])
    captured = capsys.readouterr()
    raw = captured.out.strip() or captured.err.strip()
    assert raw, f"CLI emitted no output; code={code}"
    return code, json.loads(raw)


def test_historical_overstatement_migration_and_cli_reconciliation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "game_project"
    factory_dir = root / ".gamefactory"
    state_dir = factory_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (factory_dir / "locks").mkdir(parents=True, exist_ok=True)
    project_id = "accounting-cli-test"
    (factory_dir / "factory.yml").write_text(
        f"schema_version: '0.1.0'\nproject:\n  id: {project_id}\n  name: Accounting CLI Test\n  version: '0.1.0'\nengine:\n  type: godot\n",
        encoding="utf-8",
    )
    db_path = root / ".gamefactory" / "state" / "factory.db"
    db = Database(db_path)

    # 1. Build a DB at schema version 6 only
    runner = MigrationRunner(db)
    with db.connect() as conn:
        runner.init_migration_table(conn)
        for version, name, func in MIGRATIONS[:6]:
            func(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, utc_now_iso()),
            )

        now = utc_now_iso()
        # Insert project
        conn.execute(
            """
            INSERT INTO projects (id, name, engine_type, root_path, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, "Accounting CLI Test", "godot", str(root), now, now),
        )

        # Insert workflow
        wf_id = "WF-HIST-001"
        conn.execute(
            """
            INSERT INTO workflows (id, project_id, name, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (wf_id, project_id, "Historical Overstatement Workflow", "COMPLETED", now, now),
        )

        # Insert paid task (task_type asset_paid_generation, cost_class PAID, cost 20)
        task_id = f"{wf_id}-PAID"
        conn.execute(
            """
            INSERT INTO tasks (
                id, workflow_id, name, task_type, cost_class, depends_on_json,
                status, parameters_json, max_retries, timeout_seconds, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                wf_id,
                "Historical Paid Generation",
                "asset_paid_generation",
                "PAID",
                "[]",
                "COMPLETED",
                json.dumps({"cost": 20.0, "provider": "meshy"}),
                1,
                60.0,
                now,
                now,
            ),
        )

        # Insert one COMPLETED execution with cost 20 and estimated_cost 20
        exec_id = f"EXEC-{task_id}-1"
        conn.execute(
            """
            INSERT INTO executions (
                id, task_id, attempt_number, status, started_at, completed_at,
                cost, estimated_cost, cost_unit, retryable
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                exec_id,
                task_id,
                1,
                "COMPLETED",
                now,
                now,
                20.0,
                20.0,
                "credits",
                1,
            ),
        )

        # Insert provider_operation_intents row status SUCCEEDED, actual_cost 15, external_task_id
        conn.execute(
            """
            INSERT INTO provider_operation_intents (
                id, workflow_id, task_id, asset_id, revision_number, provider, operation,
                concept_hash, request_fingerprint, approval_id, estimated_cost, actual_cost, cost_unit,
                external_task_id, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "INTENT-HIST-001",
                wf_id,
                task_id,
                "prop_energy_crate_01",
                1,
                "meshy",
                "text-to-3d",
                "concept-hash-001",
                "fingerprint-hist-001",
                "APP-HIST-001",
                20.0,
                15.0,
                "credits",
                "01a0e9c1-f324-75f4-ab7c-59ee3b6c541d",
                "SUCCEEDED",
                now,
                now,
            ),
        )

        row = conn.execute("SELECT SUM(cost) FROM executions").fetchone()
        old_sum = float(row[0])
        conn.commit()

    assert old_sum == 20.0

    # 2. Apply MigrationRunner(db).apply_all() -> schema at 7, ledger has exactly one RESERVE 20
    MigrationRunner(db).apply_all()
    with db.connect() as conn:
        applied = runner.get_applied_versions(conn)
        assert 7 in applied
        ledger_count = conn.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0]
        assert ledger_count == 1

    ledger_repo = CostLedgerRepository(db)
    entries = ledger_repo.list_by_workflow(wf_id)
    assert len(entries) == 1
    assert entries[0].entry_type == EntryType.RESERVE
    assert entries[0].amount == 20.0
    assert entries[0].source == "legacy_execution_cost"
    assert ledger_repo.project_net(project_id) == old_sum

    # 3. Dry run accounting reconcile --workflow WF --json
    code, dry_payload = _run_cli(
        capsys,
        root,
        "accounting",
        "reconcile",
        "--workflow",
        wf_id,
    )
    assert code == EXIT_SUCCESS
    assert dry_payload["spend_before"] == 20.0
    assert dry_payload["spend_after"] == 15.0
    assert dry_payload["net_change"] == -5.0

    op = dry_payload["operations"][0]
    assert op["reservation"] == 20.0
    assert op["recorded_cost"] == 20.0
    assert op["provider_actual"] == 15.0
    assert op["eligible"] is True
    assert op["net_change"] == -5.0

    proposed = op["proposed_entries"]
    assert len(proposed) == 2
    settle_entry = next(e for e in proposed if e["entry_type"] == "SETTLE")
    release_entry = next(e for e in proposed if e["entry_type"] == "RELEASE")
    assert settle_entry["amount"] == 15.0
    assert release_entry["amount"] == 20.0

    # Assert ledger row count is unchanged after dry run
    with db.connect() as conn:
        count_after_dry = conn.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0]
        assert count_after_dry == 1

    # 4. --apply without --actor or without --reason -> usage/config error exit code, no ledger change
    code_no_actor, err_no_actor = _run_cli(
        capsys,
        root,
        "accounting",
        "reconcile",
        "--workflow",
        wf_id,
        "--apply",
        "--reason",
        "historical overstatement",
    )
    assert code_no_actor == EXIT_CONFIG_ERROR
    assert "VALIDATION" in err_no_actor.get("error", "").upper()

    code_no_reason, err_no_reason = _run_cli(
        capsys,
        root,
        "accounting",
        "reconcile",
        "--workflow",
        wf_id,
        "--apply",
        "--actor",
        "tester",
    )
    assert code_no_reason == EXIT_CONFIG_ERROR
    assert "VALIDATION" in err_no_reason.get("error", "").upper()

    with db.connect() as conn:
        count_after_errors = conn.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0]
        assert count_after_errors == 1

    # 5. --apply --actor tester --reason "historical overstatement"
    code_apply, apply_payload = _run_cli(
        capsys,
        root,
        "accounting",
        "reconcile",
        "--workflow",
        wf_id,
        "--apply",
        "--actor",
        "tester",
        "--reason",
        "historical overstatement",
    )
    assert code_apply == EXIT_SUCCESS, f"apply_payload={apply_payload}"
    assert apply_payload["applied"] is True
    assert apply_payload["spend_after"] == 15.0

    # Project net is 15.0
    assert ledger_repo.project_net(project_id) == 15.0

    # An ACCOUNTING_RECONCILED audit event exists
    audit_events = AuditLogRepository(db).list_by_entity("Workflow", wf_id)
    assert any(e.action == "ACCOUNTING_RECONCILED" for e in audit_events)

    # Executions row still has cost 20 (not mutated)
    with db.connect() as conn:
        cost_row = conn.execute("SELECT cost FROM executions WHERE id = ?", (exec_id,)).fetchone()
        assert float(cost_row[0]) == 20.0
        ledger_count_applied = conn.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0]
        assert ledger_count_applied == 3

    # 6. A second apply -> documented refusal/no-op (assert no new entries)
    code_second, second_payload = _run_cli(
        capsys,
        root,
        "accounting",
        "reconcile",
        "--workflow",
        wf_id,
        "--apply",
        "--actor",
        "tester",
        "--reason",
        "historical overstatement",
    )
    assert code_second == EXIT_SUCCESS
    assert second_payload["applied"] is False
    assert second_payload["message"] == "Nothing to reconcile"
    with db.connect() as conn:
        ledger_count_second = conn.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0]
        assert ledger_count_second == 3

    # 7. accounting ledger --workflow WF --json lists the entries
    code_ledger, ledger_payload = _run_cli(
        capsys,
        root,
        "accounting",
        "ledger",
        "--workflow",
        wf_id,
    )
    assert code_ledger == EXIT_SUCCESS
    assert ledger_payload["workflow_id"] == wf_id
    assert ledger_payload["entry_count"] == 3
    entry_types = [e["entry_type"] for e in ledger_payload["entries"]]
    assert entry_types == ["RESERVE", "SETTLE", "RELEASE"]


def test_reconciliation_uncertain_intent_not_eligible(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An UNCERTAIN intent shows eligible false mentioning provider evidence, apply creates nothing."""
    root = tmp_path / "game_uncertain"
    _, cfg, _ = ConfigLoader.init_project(root, project_name="Uncertain Accounting Test")
    project_id = cfg.project.id
    db_path = root / ".gamefactory" / "state" / "factory.db"
    db = Database(db_path)
    MigrationRunner(db).apply_all()

    now = utc_now_iso()
    wf_id = "WF-UNCERTAIN-001"
    task_id = f"{wf_id}-PAID"
    exec_id = f"EXEC-{task_id}-1"

    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO workflows (id, project_id, name, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (wf_id, project_id, "Uncertain Intent Workflow", "BLOCKED", now, now),
        )
        conn.execute(
            """
            INSERT INTO tasks (
                id, workflow_id, name, task_type, cost_class, depends_on_json,
                status, parameters_json, max_retries, timeout_seconds, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                wf_id,
                "Uncertain Task",
                "asset_paid_generation",
                "PAID",
                "[]",
                "BLOCKED",
                json.dumps({"cost": 25.0, "provider": "meshy"}),
                1,
                60.0,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO executions (
                id, task_id, attempt_number, status, started_at, completed_at,
                cost, estimated_cost, cost_unit, retryable
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                exec_id,
                task_id,
                1,
                "UNCERTAIN",
                now,
                None,
                0.0,
                25.0,
                "credits",
                1,
            ),
        )
        conn.execute(
            """
            INSERT INTO provider_operation_intents (
                id, workflow_id, task_id, asset_id, revision_number, provider, operation,
                concept_hash, request_fingerprint, approval_id, estimated_cost, actual_cost, cost_unit,
                external_task_id, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "INTENT-UNCERTAIN-001",
                wf_id,
                task_id,
                "prop_energy_crate_01",
                1,
                "meshy",
                "text-to-3d",
                "concept-hash-unc-001",
                "fingerprint-unc-001",
                "APP-UNC-001",
                25.0,
                None,
                "credits",
                None,
                "UNCERTAIN",
                now,
                now,
            ),
        )
        # Seed initial reservation into ledger
        conn.execute(
            """
            INSERT INTO cost_ledger (
                id, project_id, workflow_id, task_id, execution_id, intent_id,
                request_fingerprint, entry_type, amount, cost_unit, reason,
                source, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "LEDGER-UNC-001",
                project_id,
                wf_id,
                task_id,
                exec_id,
                "INTENT-UNCERTAIN-001",
                "fingerprint-unc-001",
                "RESERVE",
                25.0,
                "credits",
                "Initial reservation",
                "execution_claim",
                "WorkflowEngine",
                now,
            ),
        )
        conn.commit()

    # Reconcile shows eligible false with reason mentioning provider evidence
    code, payload = _run_cli(capsys, root, "accounting", "reconcile", "--workflow", wf_id)
    assert code == EXIT_SUCCESS
    op = payload["operations"][0]
    assert op["eligible"] is False
    assert "provider evidence" in op["reason"].lower()

    # Apply creates nothing
    code_apply, apply_payload = _run_cli(
        capsys,
        root,
        "accounting",
        "reconcile",
        "--workflow",
        wf_id,
        "--apply",
        "--actor",
        "tester",
        "--reason",
        "attempt reconcile",
    )
    assert code_apply == EXIT_SUCCESS
    assert apply_payload["applied"] is False

    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM cost_ledger WHERE workflow_id = ?", (wf_id,)
        ).fetchone()[0]
        assert count == 1
