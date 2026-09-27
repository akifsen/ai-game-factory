"""Persistence redaction regressions for provider output and operator supplied records."""

import json
from pathlib import Path

import pytest

from gamefactory.adapters.fakes.fake_provider import FakeAssetGenerationProvider
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    AuditLogRepository,
    EvidenceRepository,
    ExecutionRepository,
    ProjectRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.domain.errors import ValidationError, WorkflowError
from gamefactory.core.domain.models import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    CostClass,
    Evidence,
    Execution,
    ExecutionStatus,
    Project,
    Task,
    TaskStatus,
    Workflow,
    WorkflowStatus,
)
from gamefactory.workflows.engine import WorkflowEngine


def _database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    ProjectRepository(db).save(
        Project(id="P-REDACT", name="Redaction", engine_type="godot", root_path=str(tmp_path))
    )
    return db


def _create_task(db: Database, workflow_id: str = "WF-REDACT") -> tuple[Workflow, Task]:
    workflow = Workflow(
        id=workflow_id,
        project_id="P-REDACT",
        name="Persistence redaction",
        status=WorkflowStatus.PENDING,
    )
    task = Task(
        id=f"{workflow_id}-T1",
        workflow_id=workflow_id,
        name="Provider call",
        task_type="paid_generation",
        cost_class=CostClass.FREE_EXTERNAL,
    )
    WorkflowRepository(db).save_with_tasks(workflow, [task])
    return workflow, task


def _database_text_values(db: Database) -> list[str]:
    values: list[str] = []
    with db.connect() as conn:
        table_names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in table_names:
            rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
            for row in rows:
                values.extend(str(value) for value in row if value is not None)
    return values


def test_engine_provider_error_is_redacted_from_db_and_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "FACTORY_DB_SECRET_51f03d!q"
    monkeypatch.setenv("AI_GAME_FACTORY_TEST_TOKEN", secret)
    db = _database(tmp_path)
    workflow, task = _create_task(db)
    provider = FakeAssetGenerationProvider(
        cost_class=CostClass.FREE_EXTERNAL,
        fail_times=1,
        fail_message=f"provider failed while using {secret}",
    )
    engine = WorkflowEngine(tmp_path, db, asset_provider=provider)
    result = engine.run_workflow(workflow.id)

    assert result.failed_tasks == [task.id]
    assert secret not in str(result.error_message)
    assert secret not in "\n".join(_database_text_values(db))

    with db.connect() as conn:
        execution = conn.execute("SELECT * FROM executions WHERE task_id=?", (task.id,)).fetchone()
        event = conn.execute(
            "SELECT * FROM audit_events WHERE entity_id=? AND action='FAILED'", (task.id,)
        ).fetchone()
        assert execution is not None and event is not None
        assert secret not in str(dict(execution))
        assert secret not in str(dict(event))
        details = json.loads(event["details_json"])
        assert details["execution_id"] == execution["id"]
        assert event["previous_state"] == TaskStatus.RUNNING.value
        assert event["new_state"] == TaskStatus.FAILED.value

    for database_file in (db.db_path, Path(f"{db.db_path}-wal")):
        if database_file.exists():
            assert secret.encode("utf-8") not in database_file.read_bytes()


@pytest.mark.parametrize("repository_kind", ["task", "workflow"])
@pytest.mark.parametrize("parameter_name", ["prompt", "output_path"])
def test_task_parameters_with_sensitive_inputs_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repository_kind: str,
    parameter_name: str,
) -> None:
    secret = "RAW_CREDENTIAL_51f03d"
    monkeypatch.setenv("GAME_FACTORY_SECRET_TOKEN", secret)
    db = _database(tmp_path)
    workflow = Workflow(id="WF-INPUT", project_id="P-REDACT", name="Input guard")
    task = Task(
        id="T-INPUT",
        workflow_id=workflow.id,
        name="Input",
        task_type="inspect_project",
        parameters={parameter_name: secret},
    )
    with pytest.raises(ValidationError, match="sensitive field or known credential"):
        if repository_kind == "task":
            TaskRepository(db).save(task)
        else:
            WorkflowRepository(db).save_with_tasks(workflow, [task])
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM workflows").fetchone()[0] == 0


def test_sensitive_task_parameter_keys_are_rejected_without_modifying_metadata(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path)
    task = Task(
        id="T-KEY",
        workflow_id="WF-KEY",
        name="Input",
        task_type="inspect_project",
        parameters={"api_token": "anything"},
    )
    with pytest.raises(ValidationError):
        TaskRepository(db).save(task)


def test_evidence_approval_and_audit_payloads_are_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "providerToken-RAW-91!"
    monkeypatch.setenv("AI_GAME_FACTORY_TEST_TOKEN", secret)
    db = _database(tmp_path)
    workflow, task = _create_task(db)
    execution = Execution(
        id="EXEC-REDACT",
        task_id=task.id,
        attempt_number=1,
        status=ExecutionStatus.RUNNING,
    )
    ExecutionRepository(db).save(execution)
    ExecutionRepository(db).save(
        Execution(
            id="EXEC-REDACT",
            task_id=task.id,
            attempt_number=1,
            status=ExecutionStatus.FAILED,
            error_message=f"failure {secret}",
            stdout=f"stdout {secret}",
            stderr=f"stderr {secret}",
        )
    )
    EvidenceRepository(db).save(
        Evidence(
            id="EVI-REDACT",
            task_id=task.id,
            execution_id=execution.id,
            evidence_type="provider_result",
            summary=f"summary {secret}",
            raw_data={
                "error": secret,
                "api_token": secret,
                "operation_hash": "stable-hash-123",
                "output_path": "C:/project/out.glb?variant=unchanged-query",
                "query": "variant=unchanged-query",
                "provider_url": f"https://provider.invalid/item?token={secret}",
            },
        )
    )
    approval = ApprovalRequest(
        id="APP-REDACT",
        workflow_id=workflow.id,
        task_id=task.id,
        approval_type="provider_call",
        reason=f"reason {secret}",
        actor=f"operator {secret}",
        comment=f"comment {secret}",
    )
    ApprovalRepository(db).save(approval)
    decided = ApprovalRequest(
        id=approval.id,
        workflow_id=workflow.id,
        task_id=task.id,
        approval_type="provider_call",
        status=ApprovalStatus.APPROVED,
        reason=approval.reason,
        actor=f"approver {secret}",
        comment=f"decision {secret}",
    )
    assert ApprovalRepository(db).decide_if_pending(
        decided,
        AuditEvent(
            id="AUD-APPROVAL",
            entity_type="Approval",
            entity_id=approval.id,
            action="APPROVED",
            actor=f"reviewer {secret}",
            details={"error": secret, "task_id": task.id},
        ),
    )
    AuditLogRepository(db).append(
        AuditEvent(
            id="AUD-RAW",
            entity_type="Task",
            entity_id=task.id,
            action="PROVIDER_ERROR",
            actor=f"worker {secret}",
            details={
                "error": secret,
                "task_id": task.id,
                "operation_hash": "stable-hash-123",
                "output_path": "C:/project/out.glb?variant=unchanged-query",
                "query": "variant=unchanged-query",
                "provider_url": f"https://provider.invalid/item?token={secret}",
            },
        )
    )

    assert secret not in "\n".join(_database_text_values(db))
    with db.connect() as conn:
        stored_evidence = conn.execute("SELECT * FROM evidences").fetchone()
        stored_approval = conn.execute("SELECT * FROM approvals").fetchone()
        audit_rows = conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        assert "stable-hash-123" in stored_evidence["raw_data_json"]
        assert "C:/project/out.glb?variant=unchanged-query" in stored_evidence["raw_data_json"]
        assert "variant=unchanged-query" in stored_evidence["raw_data_json"]
        assert "provider_url" in stored_evidence["raw_data_json"]
        assert "[REDACTED]" in stored_evidence["raw_data_json"]
        assert "[REDACTED]" in stored_approval["reason"]
        approval_event = next(row for row in audit_rows if row["id"] == "AUD-APPROVAL")
        assert "[REDACTED]" in approval_event["actor"]
        raw_event = next(row for row in audit_rows if row["id"] == "AUD-RAW")
        raw_details = json.loads(raw_event["details_json"])
        assert raw_details["task_id"] == task.id
        assert raw_details["operation_hash"] == "stable-hash-123"
        assert raw_details["output_path"] == "C:/project/out.glb?variant=unchanged-query"
        assert raw_details["query"] == "variant=unchanged-query"
        assert "[REDACTED]" in raw_details["provider_url"]


def test_finalize_task_rejects_invalid_state_transition(tmp_path: Path) -> None:
    db = _database(tmp_path)
    workflow, task = _create_task(db)
    task_repo = TaskRepository(db)
    execution_repo = ExecutionRepository(db)
    running_execution = Execution(
        id="EXEC-INVALID-TRANSITION",
        task_id=task.id,
        attempt_number=1,
        status=ExecutionStatus.RUNNING,
    )
    assert task_repo.claim_execution(task.id, running_execution, workflow.project_id, 500.0)
    task.status = TaskStatus.PENDING  # RUNNING -> PENDING is forbidden by the state machine.
    running_execution.status = ExecutionStatus.COMPLETED
    with pytest.raises(WorkflowError, match="Invalid task transition"):
        execution_repo.finalize_task(task, running_execution)
