"""Regression coverage for workflow approval, persistence, and paid safety boundaries."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

from gamefactory.adapters.fakes.fake_provider import FakeAssetGenerationProvider
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AuditLogRepository,
    ExecutionRepository,
    ProjectRepository,
    TaskRepository,
)
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.domain.errors import BudgetExceeded, ReconciliationRequired, ValidationError
from gamefactory.core.domain.models import (
    ApprovalStatus,
    AuditEvent,
    CostClass,
    Execution,
    Project,
    Task,
    TaskStatus,
    Workflow,
    WorkflowStatus,
)
from gamefactory.core.policies.policy_engine import PolicyEngine, PolicyRule
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.handlers import TaskHandlerRegistry, TaskHandlerResult
from gamefactory.workflows.ports import GenerationRequest, GenerationResponse


def setup(
    tmp_path: Path,
    *,
    provider=None,
    budget: float = 500.0,
    handler_registry=None,
    require_repo_write: bool = False,
):
    db = Database(tmp_path / ".gamefactory" / "state" / "factory.db")
    MigrationRunner(db).apply_all()
    ProjectRepository(db).save(Project("P", "Test", "godot", str(tmp_path)))
    engine = WorkflowEngine(
        tmp_path,
        db,
        policy_engine=PolicyEngine(
            PolicyRule(project_budget=budget, require_approval_for_repo_write=require_repo_write)
        ),
        asset_provider=provider,
        handler_registry=handler_registry,
    )
    return db, engine


def test_budget_lost_between_evaluation_and_claim_persists_blocked_state(
    tmp_path: Path, monkeypatch
) -> None:
    _, engine = setup(tmp_path)
    workflow = Workflow("WF-BUDGET-RACE", "P", "budget race")
    task = Task("T-BUDGET-RACE", workflow.id, "inspect", "inspect_project")
    engine.register_workflow(workflow, [task])

    def reject_atomic_claim(*_args):
        raise BudgetExceeded("Another workflow reserved the remaining budget")

    monkeypatch.setattr(engine.task_repo, "claim_execution", reject_atomic_claim)
    result = engine.run_workflow(workflow.id)
    assert result.status == WorkflowStatus.BLOCKED
    assert "reserved" in result.error_message
    assert engine.wf_repo.get(workflow.id).status == WorkflowStatus.BLOCKED
    assert engine.task_repo.get(task.id).status == TaskStatus.BLOCKED
    assert engine.exec_repo.list_by_task(task.id) == []
    assert engine.invocation_repo.count(workflow.id) == 0
    assert any(
        event.new_state == TaskStatus.BLOCKED.value
        for event in engine.audit_repo.list_by_entity("Task", task.id)
    )


def paid_workflow(wf_id: str, task_id: str, *, cost: float = 5.0):
    wf = Workflow(wf_id, "P", wf_id)
    prep = Task(f"{task_id}-prep", wf_id, "prepare", "inspect_project")
    paid = Task(
        task_id,
        wf_id,
        "paid",
        "paid_generation",
        cost_class=CostClass.PAID,
        depends_on=[prep.id],
        parameters={"prompt": task_id, "cost": cost},
    )
    return wf, [prep, paid]


def approve_paid(engine: WorkflowEngine, wf_id: str, task_id: str) -> None:
    app = engine.app_repo.list_by_workflow(wf_id)[0]
    task = engine.task_repo.get(task_id)
    assert task is not None
    workflow = engine.wf_repo.get(wf_id)
    assert workflow is not None
    ApprovalService.approve(app, "test", current_inputs=engine.approval_inputs(workflow, task))
    engine.app_repo.save(app)


def test_paid_unknown_outcome_is_never_retried_and_invocation_is_durable(tmp_path: Path) -> None:
    provider = FakeAssetGenerationProvider(simulate_crash=True)
    db, engine = setup(tmp_path, provider=provider)
    wf, tasks = paid_workflow("WF-UNCERTAIN", "T-UNCERTAIN")
    engine.register_workflow(wf, tasks)
    assert engine.run_workflow(wf.id).status == WorkflowStatus.BLOCKED
    approve_paid(engine, wf.id, tasks[1].id)
    assert engine.run_workflow(wf.id).status == WorkflowStatus.BLOCKED
    assert engine.invocation_repo.count(wf.id) == 1
    with pytest.raises(ReconciliationRequired):
        engine.run_workflow(wf.id)
    assert engine.invocation_repo.count(wf.id) == 1
    assert ExecutionRepository(db).get_latest_attempt(tasks[1].id).external_op_id == "FAKE-OP-0001"


def test_claim_rolls_back_task_state_if_attempt_insert_fails(tmp_path: Path) -> None:
    db, engine = setup(tmp_path)
    wf, tasks = paid_workflow("WF-CLAIM", "T-CLAIM")
    engine.register_workflow(wf, tasks)
    with pytest.raises(sqlite3.IntegrityError):
        engine.task_repo.claim_execution(
            tasks[0].id,
            Execution("EXEC-BAD", "MISSING-TASK", 1),
            wf.project_id,
            500.0,
        )
    assert engine.task_repo.get(tasks[0].id).status == TaskStatus.PENDING
    assert ExecutionRepository(db).get("EXEC-BAD") is None


def test_approved_input_mutation_requires_new_approval_and_never_calls_provider(
    tmp_path: Path,
) -> None:
    provider = FakeAssetGenerationProvider()
    db, engine = setup(tmp_path, provider=provider)
    wf, tasks = paid_workflow("WF-MUTATE", "T-MUTATE")
    engine.register_workflow(wf, tasks)
    engine.run_workflow(wf.id)
    approve_paid(engine, wf.id, tasks[1].id)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET parameters_json=? WHERE id=?",
            ('{"prompt":"changed","cost":5}', tasks[1].id),
        )
    assert engine.run_workflow(wf.id).status == WorkflowStatus.BLOCKED
    assert provider.invocation_count == 0
    assert len(ApprovalRepository(db).list_pending(wf.id)) == 1


def test_tampering_while_approval_is_blocked_prevents_completion(tmp_path: Path) -> None:
    provider = FakeAssetGenerationProvider()
    _, engine = setup(tmp_path, provider=provider)
    wf, tasks = paid_workflow("WF-TAMPER", "T-TAMPER")
    engine.register_workflow(wf, tasks)
    engine.run_workflow(wf.id)
    approve_paid(engine, wf.id, tasks[1].id)
    artifact = ArtifactRepository(engine.db).list_by_workflow(wf.id)[0]
    (tmp_path / artifact.relative_path).write_text("tampered", encoding="utf-8")
    result = engine.run_workflow(wf.id)
    assert result.status == WorkflowStatus.BLOCKED
    assert provider.invocation_count == 0


def test_retries_cannot_mutate_another_workflows_task(tmp_path: Path) -> None:
    _, engine = setup(tmp_path)
    wf_a, tasks_a = paid_workflow("WF-A", "T-A")
    wf_b, tasks_b = paid_workflow("WF-B", "T-B")
    engine.register_workflow(wf_a, tasks_a)
    engine.register_workflow(wf_b, tasks_b)
    failed = Task("T-FAIL", "WF-C", "fail", "simulated_failure", parameters={"fail_attempts": 1})
    # Use a separate one-task workflow to obtain a failed task state.
    wf_c = Workflow("WF-C", "P", "C")
    engine.register_workflow(wf_c, [failed])
    engine.run_workflow(wf_c.id)
    with pytest.raises(ValidationError, match="does not belong"):
        engine.retry_task(wf_a.id, failed.id)
    assert TaskRepository(engine.db).get(failed.id).status == TaskStatus.FAILED


def test_approval_compare_and_set_records_one_decision_and_audit(tmp_path: Path) -> None:
    db, engine = setup(tmp_path)
    wf, tasks = paid_workflow("WF-CAS", "T-CAS")
    engine.register_workflow(wf, tasks)
    engine.run_workflow(wf.id)
    approval = engine.app_repo.list_by_workflow(wf.id)[0]
    stale_pending = deepcopy(approval)
    approved = ApprovalService.approve(approval, "operator")
    assert engine.app_repo.decide_if_pending(
        approved, AuditEvent("A-OK", "Approval", approved.id, "APPROVED", "operator")
    )
    rejected = ApprovalService.reject(stale_pending, "operator", "late race")
    assert not engine.app_repo.decide_if_pending(
        rejected, AuditEvent("A-LATE", "Approval", rejected.id, "REJECTED", "operator")
    )
    assert engine.app_repo.get(approval.id).status == ApprovalStatus.APPROVED
    assert len(AuditLogRepository(db).list_by_entity("Approval", approval.id)) == 1


def test_rejected_approval_is_terminal_until_operator_changes_plan(tmp_path: Path) -> None:
    db, engine = setup(tmp_path, provider=FakeAssetGenerationProvider())
    wf, tasks = paid_workflow("WF-REJECT", "T-REJECT")
    engine.register_workflow(wf, tasks)
    engine.run_workflow(wf.id)
    approval = engine.app_repo.list_by_workflow(wf.id)[0]
    ApprovalService.reject(approval, "operator", "do not spend")
    engine.app_repo.save(approval)
    result = engine.run_workflow(wf.id)
    assert result.status == WorkflowStatus.FAILED
    assert engine.task_repo.get(tasks[1].id).status == TaskStatus.FAILED
    assert not engine.app_repo.list_pending(wf.id)
    assert engine.invocation_repo.count(wf.id) == 0


class FailedResponseProvider(FakeAssetGenerationProvider):
    def generate(self, request):
        self.invocation_count += 1
        return GenerationResponse("provider-op", "FAILED", output_path=None, cost=0.0)


class CountingMeteredProvider:
    name = "counting-metered"
    cost_class = CostClass.METERED

    def __init__(self, *, fail_after_acceptance: bool = False) -> None:
        self.invocations = 0
        self.fail_after_acceptance = fail_after_acceptance

    def is_configured(self) -> bool:
        return True

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        self.invocations += 1
        if self.fail_after_acceptance:
            raise RuntimeError("provider accepted request but response was lost")
        output = Path(str(request.parameters["output_path"]))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"counting-provider-output")
        return GenerationResponse(
            f"OP-{self.invocations}", "SUCCESS", str(output), 2, "provider_credits"
        )


def test_provider_cost_class_cannot_be_downgraded_and_ledger_is_generic(tmp_path: Path) -> None:
    provider = CountingMeteredProvider()
    db, engine = setup(tmp_path, provider=provider)
    wf, tasks = paid_workflow("WF-METERED", "T-METERED", cost=2)
    tasks[1].cost_class = CostClass.LOCAL
    engine.register_workflow(wf, tasks)
    assert engine.run_workflow(wf.id).status == WorkflowStatus.BLOCKED
    assert provider.invocations == 0
    approve_paid(engine, wf.id, tasks[1].id)
    assert engine.run_workflow(wf.id).status == WorkflowStatus.COMPLETED
    assert provider.invocations == 1
    assert engine.invocation_repo.count(wf.id) == 1
    restarted = WorkflowEngine(tmp_path, db, asset_provider=provider)
    assert restarted.run_workflow(wf.id).status == WorkflowStatus.COMPLETED
    assert provider.invocations == 1
    assert restarted.invocation_repo.count(wf.id) == 1


def test_metered_accepted_call_with_unknown_result_is_not_replayed(tmp_path: Path) -> None:
    provider = CountingMeteredProvider(fail_after_acceptance=True)
    db, engine = setup(tmp_path, provider=provider)
    wf, tasks = paid_workflow("WF-METERED-UNCERTAIN", "T-METERED-UNCERTAIN")
    tasks[1].cost_class = CostClass.LOCAL
    engine.register_workflow(wf, tasks)
    engine.run_workflow(wf.id)
    approve_paid(engine, wf.id, tasks[1].id)
    assert engine.run_workflow(wf.id).status == WorkflowStatus.BLOCKED
    assert provider.invocations == 1
    restarted_provider = CountingMeteredProvider(fail_after_acceptance=True)
    restarted = WorkflowEngine(tmp_path, db, asset_provider=restarted_provider)
    with pytest.raises(ReconciliationRequired):
        restarted.run_workflow(wf.id)
    assert restarted_provider.invocations == 0
    assert restarted.invocation_repo.count(wf.id) == 1


@pytest.mark.parametrize("provider", [None, FailedResponseProvider()])
def test_missing_or_failed_provider_result_never_creates_artifact(tmp_path: Path, provider) -> None:
    # No provider is tested on a paid operation only after explicit approval.
    db, engine = setup(tmp_path, provider=provider)
    wf, tasks = paid_workflow("WF-RESULT", "T-RESULT")
    engine.register_workflow(wf, tasks)
    engine.run_workflow(wf.id)
    approve_paid(engine, wf.id, tasks[1].id)
    result = engine.run_workflow(wf.id)
    assert result.status == WorkflowStatus.BLOCKED
    assert not [
        a for a in ArtifactRepository(db).list_by_workflow(wf.id) if a.artifact_type == "model_3d"
    ]


def test_project_budget_is_reserved_across_workflows(tmp_path: Path) -> None:
    provider = FakeAssetGenerationProvider()
    db, engine = setup(tmp_path, provider=provider, budget=6.0)
    first, first_tasks = paid_workflow("WF-BUDGET-1", "T-BUDGET-1", cost=5.0)
    second, second_tasks = paid_workflow("WF-BUDGET-2", "T-BUDGET-2", cost=2.0)
    engine.register_workflow(first, first_tasks)
    engine.register_workflow(second, second_tasks)
    engine.run_workflow(first.id)
    approve_paid(engine, first.id, first_tasks[1].id)
    assert engine.run_workflow(first.id).status == WorkflowStatus.COMPLETED
    assert engine.run_workflow(second.id).status == WorkflowStatus.BLOCKED
    assert engine.task_repo.get(second_tasks[1].id).status == TaskStatus.BLOCKED
    transitions = AuditLogRepository(db).list_by_entity("Task", second_tasks[1].id)
    assert any(
        event.previous_state == "PENDING" and event.new_state == "BLOCKED" for event in transitions
    )
    assert engine.invocation_repo.count() == 1


def test_concurrent_workflow_claims_cannot_overreserve_project_budget(tmp_path: Path) -> None:
    db, engine = setup(tmp_path, budget=5.0)
    workflows = [Workflow("WF-RACE-A", "P", "A"), Workflow("WF-RACE-B", "P", "B")]
    tasks = [
        Task(
            "T-RACE-A",
            workflows[0].id,
            "paid",
            "paid_generation",
            cost_class=CostClass.PAID,
            parameters={"cost": 5},
        ),
        Task(
            "T-RACE-B",
            workflows[1].id,
            "paid",
            "paid_generation",
            cost_class=CostClass.PAID,
            parameters={"cost": 5},
        ),
    ]
    for wf, task in zip(workflows, tasks, strict=True):
        engine.register_workflow(wf, [task])

    def claim(index: int) -> str:
        execution = Execution(f"EXEC-RACE-{index}", tasks[index].id, 1, cost=5, estimated_cost=5)
        try:
            return (
                "claimed"
                if engine.task_repo.claim_execution(tasks[index].id, execution, "P", 5)
                else "lost"
            )
        except BudgetExceeded:
            return "budget"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (0, 1)))
    assert results.count("claimed") == 1
    assert results.count("budget") == 1
    events_a = AuditLogRepository(db).list_by_entity("Task", tasks[0].id)
    events_b = AuditLogRepository(db).list_by_entity("Task", tasks[1].id)
    start_events = [event for event in events_a + events_b if event.new_state == "RUNNING"]
    assert len(start_events) == 1
    assert "execution_id" in start_events[0].details
    attempts = sum(len(ExecutionRepository(db).list_by_task(task.id)) for task in tasks)
    assert attempts == 1


def test_repository_write_policy_pauses_before_concept_artifact_creation(tmp_path: Path) -> None:
    db, engine = setup(tmp_path, require_repo_write=True)
    wf = Workflow("WF-REPO-POLICY", "P", "repo write")
    inspect = Task("T-REPO-INSPECT", wf.id, "inspect", "inspect_project")
    concept = Task("T-REPO-CONCEPT", wf.id, "concept", "generate_concept", depends_on=[inspect.id])
    engine.register_workflow(wf, [inspect, concept])
    result = engine.run_workflow(wf.id)
    assert result.status == WorkflowStatus.BLOCKED
    approval = engine.app_repo.list_by_workflow(wf.id)[0]
    assert approval.approval_type == "repository_write"
    assert not [
        a
        for a in ArtifactRepository(db).list_by_workflow(wf.id)
        if a.artifact_type == "concept_spec"
    ]


def test_artifacts_from_two_workflows_do_not_overwrite_each_other(tmp_path: Path) -> None:
    _, engine = setup(tmp_path)
    workflows = [Workflow("WF-FILE-A", "P", "A"), Workflow("WF-FILE-B", "P", "B")]
    for workflow in workflows:
        engine.register_workflow(
            workflow, [Task(f"{workflow.id}-T", workflow.id, "inspect", "inspect_project")]
        )
        assert engine.run_workflow(workflow.id).status == WorkflowStatus.COMPLETED
    artifacts = [ArtifactRepository(engine.db).list_by_workflow(wf.id)[0] for wf in workflows]
    assert artifacts[0].relative_path != artifacts[1].relative_path
    assert all(engine.artifact_mgr.verify_artifact_integrity(artifact) for artifact in artifacts)


def test_registered_handler_result_is_versioned_and_engine_verified(tmp_path: Path) -> None:
    registry = TaskHandlerRegistry()
    engine_ref = {}

    def handler(workflow, task, execution):
        engine = engine_ref["engine"]
        artifact = engine.artifact_mgr.create_text_artifact(
            workflow.id,
            task.id,
            "report",
            "TestHandler",
            f".gamefactory/artifacts/{workflow.id}/{task.id}/result.txt",
            "verified output",
        )
        engine.art_repo.save(artifact)
        return TaskHandlerResult(schema_version=1, summary="done", artifact_ids=[artifact.id])

    registry.register("custom_report", handler)
    _, engine = setup(tmp_path, handler_registry=registry)
    engine_ref["engine"] = engine
    wf = Workflow("WF-HANDLER", "P", "custom")
    task = Task("T-HANDLER", wf.id, "report", "custom_report")
    engine.register_workflow(wf, [task])
    assert engine.run_workflow(wf.id).status == WorkflowStatus.COMPLETED
    assert engine.evi_repo.list_by_task(task.id)[0].evidence_type == "handler:custom_report"


@pytest.mark.parametrize(
    "handler_result",
    [
        {"schema_version": 1, "summary": "claimed", "artifact_ids": []},
        TaskHandlerResult(schema_version=99, summary="unsupported", artifact_ids=["ART-1"]),
        TaskHandlerResult(schema_version=1, summary="", artifact_ids=["ART-1"]),
    ],
)
def test_malformed_handler_results_fail_closed(tmp_path: Path, handler_result) -> None:
    registry = TaskHandlerRegistry()
    registry.register("bad_handler", lambda *_: handler_result)
    _, engine = setup(tmp_path, handler_registry=registry)
    wf = Workflow("WF-BAD-HANDLER", "P", "bad")
    task = Task("T-BAD-HANDLER", wf.id, "bad", "bad_handler")
    engine.register_workflow(wf, [task])
    assert engine.run_workflow(wf.id).status == WorkflowStatus.FAILED
    assert engine.task_repo.get(task.id).status == TaskStatus.FAILED
