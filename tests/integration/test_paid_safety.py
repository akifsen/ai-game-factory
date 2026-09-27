"""Integration tests for paid operation safety invariants (Sections 73 & 108).

Proves that:
1. Absent approval -> provider invocation count is strictly 0.
2. Granted approval -> provider invocation count is exactly 1.
3. Subsequent restart/resume -> provider invocation count remains 1.
"""

from pathlib import Path

from gamefactory.adapters.fakes.fake_provider import (
    FakeAssetGenerationProvider,
)
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ProjectRepository,
)
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.domain.models import (
    ApprovalStatus,
    CostClass,
    Project,
    TaskStatus,
    WorkflowStatus,
)
from gamefactory.core.policies.policy_engine import (
    PolicyEngine,
    PolicyRule,
)
from gamefactory.workflows.definitions import create_paid_safety_workflow
from gamefactory.workflows.engine import WorkflowEngine


class TestPaidOperationSafety:
    def test_paid_operation_safety_invariants(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".gamefactory" / "state" / "factory.db"
        db = Database(db_path)
        MigrationRunner(db).apply_all()

        proj_repo = ProjectRepository(db)
        proj_repo.save(
            Project(id="proj-paid", name="Paid Test", engine_type="godot", root_path=str(tmp_path))
        )

        policy = PolicyEngine(PolicyRule(require_approval_for_paid=True))
        provider = FakeAssetGenerationProvider(cost_class=CostClass.PAID)
        engine = WorkflowEngine(tmp_path, db, policy_engine=policy, asset_provider=provider)

        wf, tasks = create_paid_safety_workflow("proj-paid")
        engine.register_workflow(wf, tasks)

        # Step 1: Execute workflow. It must pause at Task 2 (PAID)
        res1 = engine.run_workflow(wf.id)
        assert res1.status == WorkflowStatus.BLOCKED
        assert provider.invocation_count == 0  # Invariant: 0 calls without approval

        app_repo = ApprovalRepository(db)
        pending = app_repo.list_pending(wf.id)
        assert len(pending) == 1
        approval = pending[0]
        assert approval.status == ApprovalStatus.PENDING

        # Step 2: Human grants approval explicitly
        paid_task = engine.task_repo.get(f"{wf.id}-T2")
        assert paid_task is not None
        current_workflow = engine.wf_repo.get(wf.id)
        assert current_workflow is not None
        ApprovalService.approve(
            approval,
            actor="ArtDirector",
            comment="Approved credit expenditure",
            current_inputs=engine.approval_inputs(current_workflow, paid_task),
        )
        app_repo.save(approval)

        # Step 3: Resume workflow
        engine.task_repo.update_status(paid_task.id, TaskStatus.PENDING)
        res2 = engine.run_workflow(wf.id)
        assert res2.status == WorkflowStatus.COMPLETED
        assert provider.invocation_count == 1  # Invariant: exactly 1 call after approval

        # Step 4: Simulate restart/recovery (new engine instance)
        engine2 = WorkflowEngine(tmp_path, db, policy_engine=policy, asset_provider=provider)
        res3 = engine2.run_workflow(wf.id)
        assert res3.status == WorkflowStatus.COMPLETED
        assert provider.invocation_count == 1  # Invariant: still exactly 1 call after restart
