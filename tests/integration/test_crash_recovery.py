"""Integration tests for failure scenarios, retry attempts preservation, and crash reconciliation."""

from pathlib import Path

import pytest

from gamefactory.adapters.fakes.fake_provider import (
    FakeAssetGenerationProvider,
)
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ExecutionRepository,
    ProjectRepository,
)
from gamefactory.core.domain.errors import ReconciliationRequired
from gamefactory.core.domain.models import (
    CostClass,
    Execution,
    ExecutionStatus,
    Project,
    Task,
    TaskStatus,
    Workflow,
    WorkflowStatus,
)
from gamefactory.workflows.definitions import create_failure_workflow
from gamefactory.workflows.engine import WorkflowEngine


class TestCrashRecoveryAndFailures:
    def test_deterministic_failure_and_retry_history(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".gamefactory" / "state" / "factory.db"
        db = Database(db_path)
        MigrationRunner(db).apply_all()

        proj_repo = ProjectRepository(db)
        proj_repo.save(
            Project(id="proj-fail", name="Fail Test", engine_type="godot", root_path=str(tmp_path))
        )

        engine = WorkflowEngine(tmp_path, db)

        wf, tasks = create_failure_workflow("proj-fail")
        engine.register_workflow(wf, tasks)

        # 1. First execution: T1 succeeds, T2 fails, T3 does NOT run
        res1 = engine.run_workflow(wf.id)
        assert res1.status == WorkflowStatus.FAILED
        assert f"{wf.id}-T2" in res1.failed_tasks

        t1 = engine.task_repo.get(f"{wf.id}-T1")
        t2 = engine.task_repo.get(f"{wf.id}-T2")
        t3 = engine.task_repo.get(f"{wf.id}-T3")
        assert t1 is not None and t1.status == TaskStatus.COMPLETED
        assert t2 is not None and t2.status == TaskStatus.FAILED
        assert t3 is not None and t3.status == TaskStatus.PENDING  # Downstream did not run

        # Inspect failure in execution repository
        exec_repo = ExecutionRepository(db)
        attempts_before = exec_repo.list_by_task(t2.id)
        assert len(attempts_before) == 1
        assert attempts_before[0].status == ExecutionStatus.FAILED
        assert "Simulated upstream transient failure" in (attempts_before[0].error_message or "")

        # 2. Retry Task 2
        res2 = engine.retry_task(wf.id, t2.id)
        assert res2.status == WorkflowStatus.COMPLETED
        assert f"{wf.id}-T2" in res2.completed_tasks
        assert f"{wf.id}-T3" in res2.completed_tasks

        # Verify historical attempts: both attempt 1 and attempt 2 preserved
        attempts_after = exec_repo.list_by_task(t2.id)
        assert len(attempts_after) == 2
        assert attempts_after[0].attempt_number == 1
        assert attempts_after[0].status == ExecutionStatus.FAILED
        assert attempts_after[1].attempt_number == 2
        assert attempts_after[1].status == ExecutionStatus.COMPLETED

    def test_crash_during_paid_operation_blocks_for_reconciliation(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".gamefactory" / "state" / "factory.db"
        db = Database(db_path)
        MigrationRunner(db).apply_all()

        proj_repo = ProjectRepository(db)
        proj_repo.save(
            Project(
                id="proj-crash", name="Crash Test", engine_type="godot", root_path=str(tmp_path)
            )
        )

        provider = FakeAssetGenerationProvider(cost_class=CostClass.PAID)
        engine = WorkflowEngine(tmp_path, db, asset_provider=provider)

        # Set up a workflow with a paid task
        wf = Workflow(
            id="WF-CRASH", project_id="proj-crash", name="Crash WF", status=WorkflowStatus.RUNNING
        )
        paid_task = Task(
            id="T-PAID-CRASH",
            workflow_id="WF-CRASH",
            name="Paid Task",
            task_type="paid_generation",
            cost_class=CostClass.PAID,
            status=TaskStatus.RUNNING,
            parameters={"prompt": "Titan", "cost": 10.0},
        )
        engine.register_workflow(wf, [paid_task])

        # Simulate that an execution attempt was submitted to external provider,
        # but the local process crashed while status was RUNNING
        exec_repo = ExecutionRepository(db)
        exec_repo.save(
            Execution(
                id="EXEC-CRASH-001",
                task_id=paid_task.id,
                attempt_number=1,
                status=ExecutionStatus.RUNNING,  # Process died before completed
                external_op_id="MESHY-OP-UNKNOWN-STATUS",
                cost=10.0,
            )
        )

        # Resuming workflow must NOT re-invoke the provider!
        # It MUST raise ReconciliationRequired and block the task!
        with pytest.raises(ReconciliationRequired, match="reconciliation required"):
            engine.run_workflow(wf.id)

        # Provider invocation count must be 0 (no blind reinvocation!)
        assert provider.invocation_count == 0

        # Task and Workflow must be BLOCKED
        updated_task = engine.task_repo.get(paid_task.id)
        assert updated_task is not None
        assert updated_task.status == TaskStatus.BLOCKED
