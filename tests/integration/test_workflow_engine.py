"""Integration tests for WorkflowEngine: DAG execution, approval blocking, resume, and gate completion."""

from pathlib import Path

from gamefactory.adapters.fakes.fake_provider import (
    FakeAssetGenerationProvider,
)
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
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
from gamefactory.workflows.definitions import create_demo_workflow
from gamefactory.workflows.engine import WorkflowEngine


class TestWorkflowEngine:
    def setup_method(self) -> None:
        pass

    def test_demo_workflow_lifecycle_with_approval_gate(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".gamefactory" / "state" / "factory.db"
        db = Database(db_path)
        MigrationRunner(db).apply_all()

        # Save project
        proj_repo = ProjectRepository(db)
        proj = Project(
            id="proj-demo", name="Demo Game", engine_type="godot", root_path=str(tmp_path)
        )
        proj_repo.save(proj)

        policy = PolicyEngine(PolicyRule(require_approval_for_paid=True))
        provider = FakeAssetGenerationProvider()
        engine = WorkflowEngine(tmp_path, db, policy_engine=policy, asset_provider=provider)

        # 1. Create and register demo workflow
        wf, tasks = create_demo_workflow("proj-demo")
        engine.register_workflow(wf, tasks)

        # 2. First execution: runs T1, T2, T3, then BLOCKS on T4 (paid generation)
        res1 = engine.run_workflow(wf.id)
        assert res1.status == WorkflowStatus.BLOCKED
        assert res1.pending_approval_id is not None
        assert f"{wf.id}-T4" in res1.blocked_tasks

        # Provider must NOT have been called yet! (Paid safety invariant)
        assert provider.invocation_count == 0

        # Check approval in database
        app_repo = ApprovalRepository(db)
        pending_apps = app_repo.list_pending(wf.id)
        assert len(pending_apps) == 1
        approval = pending_apps[0]
        assert approval.status == ApprovalStatus.PENDING
        assert approval.cost_class == CostClass.PAID

        # 3. Simulate human approving the request
        task4 = engine.task_repo.get(f"{wf.id}-T4")
        assert task4 is not None
        current_workflow = engine.wf_repo.get(wf.id)
        assert current_workflow is not None
        ApprovalService.approve(
            approval,
            actor="LeadArtist",
            comment="Approved high poly budget",
            current_inputs=engine.approval_inputs(current_workflow, task4),
        )
        app_repo.save(approval)

        # 4. Resume workflow in a separate invocation
        # Unblock task status
        engine.task_repo.update_status(task4.id, TaskStatus.PENDING)
        res2 = engine.run_workflow(wf.id)

        # 5. Workflow must complete successfully
        assert res2.status == WorkflowStatus.COMPLETED
        assert len(res2.completed_tasks) == 6

        # Provider must have been called exactly once
        assert provider.invocation_count == 1

        # Check artifacts and quality gates
        art_repo = ArtifactRepository(db)
        artifacts = art_repo.list_by_workflow(wf.id)
        assert len(artifacts) >= 3
        for art in artifacts:
            assert art.validation_state in ("VALID", "VERIFIED")

        # 6. Running resume again is idempotent and causes no extra provider calls
        res3 = engine.run_workflow(wf.id)
        assert res3.status == WorkflowStatus.COMPLETED
        assert provider.invocation_count == 1
