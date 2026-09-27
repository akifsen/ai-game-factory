"""V0.2 must preserve V0.1 project contracts and already approved work."""

from pathlib import Path

import pytest

from gamefactory.adapters.fakes.fake_provider import FakeAssetGenerationProvider
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    TaskRepository,
)
from gamefactory.config.loader import ConfigLoader
from gamefactory.core.approvals.approval_service import ApprovalService, compute_operation_hash
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.models import TaskStatus, WorkflowStatus
from gamefactory.core.policies.policy_engine import PolicyEngine, PolicyRule
from gamefactory.workflows.definitions import create_demo_workflow
from gamefactory.workflows.engine import WorkflowEngine


def _legacy_project(tmp_path: Path) -> tuple[Database, str]:
    root, config, existed = ConfigLoader.init_project(tmp_path, project_name="Legacy Game")
    assert root == tmp_path
    assert existed is False
    assert config.schema_version == "0.1.0"
    assert config.policies.require_approval_for_repo_write is False
    assert config.policies.require_approval_for_process_execution is False
    assert config.policies.paid_operations_require_approval is True
    assert config.policies.destructive_operations_require_approval is True
    assert config.engine.type == "godot"
    return Database(root / ".gamefactory" / "state" / "factory.db"), config.project.id


def test_v01_approved_workflow_resumes_with_original_input_hash(tmp_path: Path) -> None:
    db, project_id = _legacy_project(tmp_path)
    provider = FakeAssetGenerationProvider()
    policy = PolicyEngine(PolicyRule(require_approval_for_paid=True))
    first_engine = WorkflowEngine(tmp_path, db, policy_engine=policy, asset_provider=provider)
    workflow, tasks = create_demo_workflow(project_id)
    first_engine.register_workflow(workflow, tasks)

    first_result = first_engine.run_workflow(workflow.id)
    assert first_result.status == WorkflowStatus.BLOCKED
    assert provider.invocation_count == 0
    approval_repo = ApprovalRepository(db)
    approvals = approval_repo.list_pending(workflow.id)
    assert len(approvals) == 1
    approval = approvals[0]
    paid_task = TaskRepository(db).get(f"{workflow.id}-T4")
    assert paid_task is not None

    # This is the exact V0.1 scope shape; ordinary built-in tasks never add a
    # handler_context field to a previously approved operation fingerprint.
    artifacts = ArtifactRepository(db).list_by_workflow(workflow.id)
    legacy_inputs = {
        "parameters": paid_task.parameters,
        "scope": {
            "workflow_id": workflow.id,
            "task_type": paid_task.task_type,
            "cost_class": paid_task.cost_class.value,
            "provider": provider.name,
            "artifacts": sorted((item.id, item.content_hash) for item in artifacts),
            "estimated_cost": float(paid_task.parameters.get("cost", 0.0)),
        },
    }
    assert "handler_context" not in legacy_inputs["scope"]
    assert (
        compute_operation_hash(paid_task.id, approval.approval_type, legacy_inputs)
        == approval.operation_hash
    )
    ApprovalService.approve(approval, "legacy-operator", current_inputs=legacy_inputs)
    approval_repo.save(approval)
    TaskRepository(db).update_status(paid_task.id, TaskStatus.PENDING)

    # A fresh engine models approve -> resume in another CLI process.
    resumed_engine = WorkflowEngine(tmp_path, db, policy_engine=policy, asset_provider=provider)
    resumed = resumed_engine.run_workflow(workflow.id)
    assert resumed.status == WorkflowStatus.COMPLETED
    assert provider.invocation_count == 1
    assert approval_repo.list_pending(workflow.id) == []
    assert approval_repo.get(approval.id).operation_hash == approval.operation_hash
    assert resumed_engine.wf_repo.get(workflow.id).status == WorkflowStatus.COMPLETED


def test_update_parameters_rejects_secrets_without_overwriting_previous_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, project_id = _legacy_project(tmp_path)
    engine = WorkflowEngine(tmp_path, db, asset_provider=FakeAssetGenerationProvider())
    workflow, tasks = create_demo_workflow(project_id)
    engine.register_workflow(workflow, tasks)
    repository = TaskRepository(db)
    task_id = f"{workflow.id}-T4"

    safe_parameters = {"prompt": "A small robot", "cost": 2.0, "cost_unit": "fake_credits"}
    repository.update_parameters(task_id, safe_parameters)
    assert repository.get(task_id).parameters == safe_parameters

    monkeypatch.setenv("COMPAT_SECRET_TOKEN", "known-credential-987")
    for unsafe in (
        {"nested": {"api_key": "unlisted-value"}},
        {"prompt": "known-credential-987"},
    ):
        with pytest.raises(ValidationError, match="sensitive"):
            repository.update_parameters(task_id, unsafe)
        assert repository.get(task_id).parameters == safe_parameters

    with pytest.raises(ValidationError, match="not found"):
        repository.update_parameters("missing-task", safe_parameters)
