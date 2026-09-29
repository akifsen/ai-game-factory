"""Integration tests for Slice D evidence-driven recovery CLI and invariants.

Covers:
1. Exact historical pre-launch Godot failure reclassification and engine retry.
2. Launched process refusal.
3. RUNTIME_VALIDATION_FAILED refusal.
4. ASSET_VALIDATION_FAILED refusal.
5. Paid task with UNCERTAIN intent and no external id refusal.
6. Paid SUCCEEDED intent + downstream Godot pre-launch failure with real harness.
7. Argparse rejection of --force and usage error for --apply without actor/reason.
8. Inspect is strictly read-only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    AuditLogRepository,
    ExecutionRepository,
    ProviderOperationIntent,
    ProviderOperationIntentRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.cli.exit_codes import EXIT_CONFIG_ERROR, EXIT_SUCCESS, EXIT_WORKFLOW_FAILURE
from gamefactory.cli.main import main
from gamefactory.core.domain.errors import ToolUnavailableError, ValidationError
from gamefactory.core.domain.models import (
    AuditEvent,
    CostClass,
    Execution,
    ExecutionStatus,
    Task,
    TaskStatus,
    Workflow,
    WorkflowStatus,
    generate_id,
    utc_now_iso,
)
from gamefactory.core.policies.policy_engine import PolicyEngine, PolicyRule
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.handlers import (
    RegisteredTaskHandler,
    TaskHandlerMetadata,
    TaskHandlerResult,
)
from tests.integration.test_accounting_v06 import _decision, _setup_accounting_env


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


def _dump_table(db: Database, table: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        cursor = conn.execute(f"SELECT * FROM {table} ORDER BY 1 ASC")
        return [dict(row) for row in cursor.fetchall()]


def _setup_project(tmp_path: Path, project_id: str = "recovery-test") -> tuple[Path, Database]:
    root = tmp_path / "project"
    factory_dir = root / ".gamefactory"
    state_dir = factory_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (factory_dir / "locks").mkdir(parents=True, exist_ok=True)
    (factory_dir / "factory.yml").write_text(
        f"schema_version: '0.1.0'\nproject:\n  id: {project_id}\n  name: Recovery Test\n  version: '0.1.0'\nengine:\n  type: godot\n",
        encoding="utf-8",
    )
    db = Database(state_dir / "factory.db")
    MigrationRunner(db).apply_all()
    now = utc_now_iso()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO projects (id, name, engine_type, root_path, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, "Recovery Test", "godot", str(root), now, now),
        )
    return root, db


def test_historical_godot_failure_reclassification_and_engine_retry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, db = _setup_project(tmp_path, "hist-recovery-test")
    now = utc_now_iso()

    wf_id = "WF-HIST-001"
    task_id = "WF-HIST-001-GODOT"
    exec_id = "EXEC-41c22667"

    wf = Workflow(
        id=wf_id,
        project_id="hist-recovery-test",
        name="Historical Godot Workflow",
        status=WorkflowStatus.FAILED,
        created_at=now,
        updated_at=now,
    )
    WorkflowRepository(db).save(wf)

    task = Task(
        id=task_id,
        workflow_id=wf_id,
        name="Asset Godot Stage",
        task_type="asset_godot",
        cost_class=CostClass.LOCAL,
        status=TaskStatus.FAILED,
        max_retries=1,
        created_at=now,
        updated_at=now,
    )
    TaskRepository(db).save(task)

    execution = Execution(
        id=exec_id,
        task_id=task_id,
        attempt_number=1,
        status=ExecutionStatus.FAILED,
        pid=None,
        exit_code=None,
        stdout=None,
        stderr=None,
        retryable=False,
        error_message="Godot executable is required for asset runtime verification",
        started_at=now,
        completed_at=now,
    )
    ExecutionRepository(db).save(execution)

    audit_repo = AuditLogRepository(db)
    audit_repo.append(
        AuditEvent(
            id=generate_id("AUDIT"),
            entity_type="Task",
            entity_id=task_id,
            action="FAILED",
            actor="WorkflowEngine",
            timestamp=now,
            details={
                "attempt": 1,
                "error": "Godot executable is required for asset runtime verification",
                "error_code": "ENGINE_IMPORT_FAILED",
            },
        )
    )

    # 1. Inspect shows PRE_EXECUTION_TOOL_CONFIGURATION
    code, inspect_payload = _run_cli(capsys, root, "recovery", "inspect", wf_id)
    assert code == EXIT_SUCCESS
    assert len(inspect_payload["tasks"]) == 1
    t_info = inspect_payload["tasks"][0]
    assert t_info["classification"] == "PRE_EXECUTION_TOOL_CONFIGURATION"
    assert t_info["eligible_for_reclassification"] is True
    assert any("recovery reclassify" in a for a in t_info["safe_actions"])

    # 2. Dry run is eligible and mutates nothing
    exec_dump_before = _dump_table(db, "executions")
    audit_dump_before = _dump_table(db, "audit_events")

    code, dry_payload = _run_cli(capsys, root, "recovery", "reclassify", "--execution", exec_id)
    assert code == EXIT_SUCCESS
    assert dry_payload["eligible"] is True
    assert dry_payload["dry_run"] is True
    assert dry_payload["mutated"] is False

    assert _dump_table(db, "executions") == exec_dump_before
    assert _dump_table(db, "audit_events") == audit_dump_before

    # 3. Apply with --actor / --reason -> retryable 1 and RECOVERY_RECLASSIFIED audit event
    code, apply_payload = _run_cli(
        capsys,
        root,
        "recovery",
        "reclassify",
        "--execution",
        exec_id,
        "--apply",
        "--actor",
        "lead_engineer",
        "--reason",
        "Godot path configured on runner",
    )
    assert code == EXIT_SUCCESS
    assert apply_payload["eligible"] is True
    assert apply_payload["applied"] is True
    assert apply_payload["mutated"] is True

    updated_exec = ExecutionRepository(db).get(exec_id)
    assert updated_exec is not None
    assert updated_exec.retryable is True

    audit_events = AuditLogRepository(db).list_by_entity("Execution", exec_id)
    reclass_events = [e for e in audit_events if e.action == "RECOVERY_RECLASSIFIED"]
    assert len(reclass_events) == 1
    event = reclass_events[0]
    assert event.actor == "lead_engineer"
    assert event.details["category"] == "PRE_EXECUTION_TOOL_CONFIGURATION"
    assert event.details["evidence"]["error_code"] == "ENGINE_IMPORT_FAILED"
    assert event.details["reason"] == "Godot path configured on runner"
    assert event.details["before"] == 0
    assert event.details["after"] == 1

    # 4. A second apply refuses as not applicable (already retryable)
    code, second_payload = _run_cli(
        capsys,
        root,
        "recovery",
        "reclassify",
        "--execution",
        exec_id,
        "--apply",
        "--actor",
        "lead_engineer",
        "--reason",
        "retry reclassify",
    )
    assert code == EXIT_WORKFLOW_FAILURE
    assert second_payload["eligible"] is False
    assert second_payload["applied"] is False
    assert any("already retryable" in r for r in second_payload["reasons"])

    # 5. Engine permits retry_task for that Godot task
    engine = WorkflowEngine(root, db, policy_engine=PolicyEngine(PolicyRule()))

    def godot_stub(w, t, e):
        art_file = root / "godot_out.txt"
        art_file.write_text("ok", encoding="utf-8")
        art = engine.artifact_mgr.register_file_artifact(
            w.id, t.id, "asset-godot-render-log", t.task_type, "godot_out.txt"
        )
        engine.art_repo.save(art)
        return TaskHandlerResult(1, "godot stub ok", [art.id])

    engine.handler_registry.register("asset_godot", godot_stub)
    res = engine.retry_task(wf_id, task_id)
    assert res.status in (WorkflowStatus.COMPLETED, WorkflowStatus.RUNNING)
    re_task = TaskRepository(db).get(task_id)
    assert re_task is not None
    assert re_task.status == TaskStatus.COMPLETED


def test_launched_process_refuses_reclassification(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, db = _setup_project(tmp_path, "process-launched-test")

    wf_id = "WF-PROC-001"
    task_id = "WF-PROC-001-TASK"
    exec_id = "EXEC-PROC-001"

    WorkflowRepository(db).save(
        Workflow(
            id=wf_id,
            project_id="process-launched-test",
            name="Proc Test",
            status=WorkflowStatus.FAILED,
        )
    )
    TaskRepository(db).save(
        Task(
            id=task_id,
            workflow_id=wf_id,
            name="Proc Task",
            task_type="asset_godot",
            status=TaskStatus.FAILED,
        )
    )
    ExecutionRepository(db).save(
        Execution(
            id=exec_id,
            task_id=task_id,
            attempt_number=1,
            status=ExecutionStatus.FAILED,
            pid=1234,
            exit_code=1,
            stderr="Segmentation fault (core dumped)",
            error_message="Godot executable is required for asset runtime verification",
        )
    )

    dump_before = _dump_table(db, "executions")

    code, payload = _run_cli(
        capsys,
        root,
        "recovery",
        "reclassify",
        "--execution",
        exec_id,
        "--apply",
        "--actor",
        "operator",
        "--reason",
        "try repair",
    )
    assert code == EXIT_WORKFLOW_FAILURE
    assert payload["eligible"] is False
    assert payload["mutated"] is False
    assert any("launched process" in r for r in payload["reasons"])
    assert _dump_table(db, "executions") == dump_before


def test_runtime_validation_failed_refuses_reclassification(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, db = _setup_project(tmp_path, "runtime-val-test")

    wf_id = "WF-RVAL-001"
    task_id = "WF-RVAL-001-TASK"
    exec_id = "EXEC-RVAL-001"

    WorkflowRepository(db).save(
        Workflow(
            id=wf_id,
            project_id="runtime-val-test",
            name="RVal Test",
            status=WorkflowStatus.FAILED,
        )
    )
    TaskRepository(db).save(
        Task(
            id=task_id,
            workflow_id=wf_id,
            name="RVal Task",
            task_type="asset_godot",
            status=TaskStatus.FAILED,
        )
    )
    ExecutionRepository(db).save(
        Execution(
            id=exec_id,
            task_id=task_id,
            attempt_number=1,
            status=ExecutionStatus.FAILED,
            error_message="Independent Godot observation failed mesh, collision, or error checks",
        )
    )
    AuditLogRepository(db).append(
        AuditEvent(
            id=generate_id("AUDIT"),
            entity_type="Task",
            entity_id=task_id,
            action="FAILED",
            actor="WorkflowEngine",
            details={"attempt": 1, "error_code": "RUNTIME_VALIDATION_FAILED"},
        )
    )

    code, payload = _run_cli(
        capsys,
        root,
        "recovery",
        "reclassify",
        "--execution",
        exec_id,
        "--apply",
        "--actor",
        "operator",
        "--reason",
        "reclassify runtime validation",
    )
    assert code == EXIT_WORKFLOW_FAILURE
    assert payload["eligible"] is False
    assert any("deterministic failure by configuration policy" in r for r in payload["reasons"])


def test_asset_validation_failed_refuses_reclassification(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, db = _setup_project(tmp_path, "asset-val-test")

    wf_id = "WF-AVAL-001"
    task_id = "WF-AVAL-001-TASK"
    exec_id = "EXEC-AVAL-001"

    WorkflowRepository(db).save(
        Workflow(
            id=wf_id,
            project_id="asset-val-test",
            name="AVal Test",
            status=WorkflowStatus.FAILED,
        )
    )
    TaskRepository(db).save(
        Task(
            id=task_id,
            workflow_id=wf_id,
            name="AVal Task",
            task_type="asset_process",
            status=TaskStatus.FAILED,
        )
    )
    ExecutionRepository(db).save(
        Execution(
            id=exec_id,
            task_id=task_id,
            attempt_number=1,
            status=ExecutionStatus.FAILED,
            error_message="Asset validation failed geometry check",
        )
    )
    AuditLogRepository(db).append(
        AuditEvent(
            id=generate_id("AUDIT"),
            entity_type="Task",
            entity_id=task_id,
            action="FAILED",
            actor="WorkflowEngine",
            details={"attempt": 1, "error_code": "ASSET_VALIDATION_FAILED"},
        )
    )

    code, payload = _run_cli(
        capsys,
        root,
        "recovery",
        "reclassify",
        "--execution",
        exec_id,
        "--apply",
        "--actor",
        "operator",
        "--reason",
        "reclassify asset validation",
    )
    assert code == EXIT_WORKFLOW_FAILURE
    assert payload["eligible"] is False
    assert any("deterministic failure by configuration policy" in r for r in payload["reasons"])


def test_paid_task_with_uncertain_intent_and_no_external_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, db = _setup_project(tmp_path, "paid-uncertain-test")

    wf_id = "WF-PAID-001"
    task_id = "WF-PAID-001-GEN"
    exec_id = "EXEC-PAID-001"

    WorkflowRepository(db).save(
        Workflow(
            id=wf_id,
            project_id="paid-uncertain-test",
            name="Paid Test",
            status=WorkflowStatus.BLOCKED,
        )
    )
    TaskRepository(db).save(
        Task(
            id=task_id,
            workflow_id=wf_id,
            name="Paid Task",
            task_type="asset_paid_generation",
            cost_class=CostClass.PAID,
            status=TaskStatus.BLOCKED,
            max_retries=1,
        )
    )
    ExecutionRepository(db).save(
        Execution(
            id=exec_id,
            task_id=task_id,
            attempt_number=1,
            status=ExecutionStatus.UNCERTAIN,
            error_message="Connection reset during POST",
        )
    )
    intent_repo = ProviderOperationIntentRepository(db)
    intent = ProviderOperationIntent(
        id="INTENT-PAID-001",
        workflow_id=wf_id,
        task_id=task_id,
        asset_id="crate",
        revision_number=1,
        provider="meshy",
        operation="image-to-3d",
        concept_hash="concept-123",
        request_fingerprint="fp-123",
        approval_id="APP-001",
        status="UNCERTAIN",
        external_task_id=None,
    )
    intent_repo.save(intent)

    intents_before = _dump_table(db, "provider_operation_intents")

    # 1. Inspect shows PAID_SUBMISSION_UNCERTAIN
    code, inspect_payload = _run_cli(capsys, root, "recovery", "inspect", wf_id)
    assert code == EXIT_SUCCESS
    assert inspect_payload["tasks"][0]["classification"] == "PAID_SUBMISSION_UNCERTAIN"
    assert inspect_payload["tasks"][0]["eligible_for_reclassification"] is False

    # 2. Reclassify apply refuses
    code, payload = _run_cli(
        capsys,
        root,
        "recovery",
        "reclassify",
        "--execution",
        exec_id,
        "--apply",
        "--actor",
        "operator",
        "--reason",
        "attempt override",
    )
    assert code == EXIT_WORKFLOW_FAILURE
    assert payload["eligible"] is False
    assert any(
        "reclassification could allow a second provider submission" in r for r in payload["reasons"]
    )

    # 3. Engine retry_task refuses
    engine = WorkflowEngine(root, db, policy_engine=PolicyEngine(PolicyRule()))
    with pytest.raises(ValidationError):
        engine.retry_task(wf_id, task_id)

    # 4. provider_operation_intents unchanged
    assert _dump_table(db, "provider_operation_intents") == intents_before


def test_paid_succeeded_intent_with_downstream_godot_pre_launch_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, db, wf_id, fake, handlers = _setup_accounting_env(tmp_path)

    godot_called = 0

    def godot_stub(wf, task, execution):
        nonlocal godot_called
        godot_called += 1
        if godot_called == 1:
            raise ToolUnavailableError(
                "Godot executable is required for asset runtime verification",
                tool="godot",
                reason="executable_missing",
                task_id=task.id,
            )
        # Second call succeeds
        processed = next(
            a
            for a in handlers.artifacts.list_by_workflow(wf.id)
            if a.artifact_type == "asset-processed-glb"
        )
        handlers.artifact_manager.verify_artifact_integrity(processed)
        path = handlers._path(task, f"runtime-{execution.id}.json")
        path.write_text(
            json.dumps({"status": "PASS", "execution_id": execution.id}), encoding="utf-8"
        )
        ids = [handlers._register(wf, task, execution, "asset-runtime-observation", path)]
        for angle in ("front", "three_quarter", "side"):
            capture = handlers._path(task, f"{execution.id}-{angle}.png")
            Image.new("RGB", (2, 2), (20, 70, 140)).save(capture)
            ids.append(handlers._register(wf, task, execution, "asset-runtime-capture", capture))
        return TaskHandlerResult(1, "deterministic runtime stub", ids)

    orig_meta = engine.handler_registry.metadata("asset_godot")
    engine.handler_registry._handlers["asset_godot"] = RegisteredTaskHandler(
        godot_stub, orig_meta or TaskHandlerMetadata()
    )

    # Run workflow and approve required gates until godot task failure
    res = engine.run_workflow(wf_id)
    while res.status == WorkflowStatus.BLOCKED and res.pending_approval_id:
        _decision(engine, db, res.pending_approval_id, approve=True)
        res = engine.run_workflow(wf_id)

    assert res.status == WorkflowStatus.FAILED
    assert fake.invocation_count == 1

    # Verify paid intent succeeded
    intent_repo = ProviderOperationIntentRepository(db)
    tasks = TaskRepository(db).list_by_workflow(wf_id)
    paid_task = next(t for t in tasks if t.task_type == "asset_paid_generation")
    paid_intent = intent_repo.get_by_task(paid_task.id)
    assert paid_intent is not None
    assert paid_intent.status == "SUCCEEDED"

    godot_task = next(t for t in tasks if t.task_type == "asset_godot")
    assert godot_task.status == TaskStatus.FAILED

    # Inspect shows Godot task PRE_EXECUTION_TOOL_CONFIGURATION and already retryable
    code, inspect_payload = _run_cli(capsys, tmp_path / "project", "recovery", "inspect", wf_id)
    assert code == EXIT_SUCCESS
    g_info = next(t for t in inspect_payload["tasks"] if t["task_type"] == "asset_godot")
    assert g_info["classification"] == "PRE_EXECUTION_TOOL_CONFIGURATION"
    assert g_info["latest_execution"]["retryable"] is True
    assert g_info["eligible_for_reclassification"] is False  # Already retryable!
    assert any("gamefactory retry" in act for act in g_info["safe_actions"])

    # Retry completes Godot stage while fake provider invocation count stays 1
    retry_res = engine.retry_task(wf_id, godot_task.id)
    assert retry_res.status in (
        WorkflowStatus.RUNNING,
        WorkflowStatus.COMPLETED,
        WorkflowStatus.BLOCKED,
    )
    assert fake.invocation_count == 1
    re_godot = TaskRepository(db).get(godot_task.id)
    assert re_godot is not None
    assert re_godot.status == TaskStatus.COMPLETED


def test_force_and_apply_usage_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, db = _setup_project(tmp_path, "usage-err-test")

    # 1. --force rejected by argparse
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "recovery",
                "reclassify",
                "--execution",
                "EXEC-TEST",
                "--force",
                "--project",
                str(root),
                "--json",
            ]
        )
    assert exc_info.value.code != 0
    capsys.readouterr()  # Flush captured output

    # 2. --apply without --actor
    code, payload = _run_cli(
        capsys,
        root,
        "recovery",
        "reclassify",
        "--execution",
        "EXEC-TEST",
        "--apply",
        "--reason",
        "missing actor",
    )
    assert code == EXIT_CONFIG_ERROR
    assert "error" in payload

    # 3. --apply without --reason
    code, payload = _run_cli(
        capsys,
        root,
        "recovery",
        "reclassify",
        "--execution",
        "EXEC-TEST",
        "--apply",
        "--actor",
        "operator",
    )
    assert code == EXIT_CONFIG_ERROR
    assert "error" in payload


def test_inspect_is_strictly_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, db = _setup_project(tmp_path, "read-only-test")
    wf_id = "WF-RO-001"
    task_id = "WF-RO-001-TASK"
    exec_id = "EXEC-RO-001"

    WorkflowRepository(db).save(
        Workflow(
            id=wf_id,
            project_id="read-only-test",
            name="RO Workflow",
            status=WorkflowStatus.FAILED,
        )
    )
    TaskRepository(db).save(
        Task(
            id=task_id,
            workflow_id=wf_id,
            name="RO Task",
            task_type="asset_godot",
            status=TaskStatus.FAILED,
        )
    )
    ExecutionRepository(db).save(
        Execution(
            id=exec_id,
            task_id=task_id,
            attempt_number=1,
            status=ExecutionStatus.FAILED,
            error_message="Godot executable is required for asset runtime verification",
        )
    )

    exec_before = _dump_table(db, "executions")
    audit_before = _dump_table(db, "audit_events")
    intents_before = _dump_table(db, "provider_operation_intents")

    code, _ = _run_cli(capsys, root, "recovery", "inspect", wf_id)
    assert code == EXIT_SUCCESS

    assert _dump_table(db, "executions") == exec_before
    assert _dump_table(db, "audit_events") == audit_before
    assert _dump_table(db, "provider_operation_intents") == intents_before
