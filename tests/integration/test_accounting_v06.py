"""V0.6 workflow-level accounting integration tests with fake provider.

Asserts ledger entries, project net, provider invocation counts, and recovery
invariants for all 10 paid workflow cases.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from PIL import Image

from gamefactory.adapters.fakes.fake_provider import FakeAssetGenerationProvider
from gamefactory.adapters.fakes.readiness import PassingReadinessProbes
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    AssetRevisionRepository,
    AuditLogRepository,
    CostLedgerRepository,
    ExecutionRepository,
    ProjectRepository,
    ProviderOperationIntentRepository,
    TaskRepository,
)
from gamefactory.core.accounting.ledger import EntryType, LedgerEntry, plan_settlement
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.domain.asset_contracts import parse_asset_specification
from gamefactory.core.domain.errors import ToolExecutionError
from gamefactory.core.domain.models import (
    AuditEvent,
    Project,
    WorkflowStatus,
    generate_id,
)
from gamefactory.core.policies.policy_engine import PolicyEngine, PolicyRule
from gamefactory.workflows.accounting import CostAccounting
from gamefactory.workflows.asset_production import (
    AssetProductionHandlers,
    create_asset_production_workflow,
    register_asset_production_handlers,
)
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.handlers import TaskHandlerResult

SPEC_PATH = (
    Path(__file__).resolve().parents[2] / "src/gamefactory/resources/specs/prop_energy_crate_01.yml"
)


class ConfigurableCostFake(FakeAssetGenerationProvider):
    """Fake provider returning explicitly configured actual costs."""

    def __init__(
        self,
        actual_cost: float | None = 15.0,
        fail_with_cost: float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.target_actual_cost = actual_cost
        self.fail_with_cost = fail_with_cost

    def generate(self, request):
        if self.fail_with_cost is not None:
            response = super().generate(request)
            if self.intent_repo is not None:
                intent = self.intent_repo.get_by_task(str(request.parameters["task_id"]))
                if intent is not None:
                    intent.actual_cost = self.fail_with_cost
                    intent.status = "FAILED"
                    self.intent_repo.save(intent)
            raise ToolExecutionError(
                "Simulated terminal failure with billed provider cost",
                details={"actual_cost": self.fail_with_cost},
            )

        response = super().generate(request)
        if self.target_actual_cost is not None:
            response.cost = self.target_actual_cost
            response.details["actual_cost"] = self.target_actual_cost
            if self.intent_repo is not None:
                intent = self.intent_repo.get_by_task(str(request.parameters["task_id"]))
                if intent is not None:
                    intent.actual_cost = self.target_actual_cost
                    self.intent_repo.save(intent)
        return response


def _setup_accounting_env(
    tmp_path: Path,
    *,
    project_id: str = "asset-test",
    estimate: float = 20.0,
    budget: float = 100.0,
    provider: FakeAssetGenerationProvider | None = None,
) -> tuple[WorkflowEngine, Database, str, FakeAssetGenerationProvider, AssetProductionHandlers]:
    root = tmp_path / "project"
    root.mkdir(parents=True, exist_ok=True)
    db = Database(root / ".gamefactory/state/factory.db")
    MigrationRunner(db).apply_all()

    project = Project(
        id=project_id, name="Accounting Integration Test", engine_type="godot", root_path=str(root)
    )
    ProjectRepository(db).save(project)

    image = root / "concept.png"
    Image.new("RGB", (2, 2), (20, 70, 140)).save(image)
    provenance = root / "concept.json"
    provenance.write_text(
        json.dumps({"sha256": hashlib.sha256(image.read_bytes()).hexdigest()}), encoding="utf-8"
    )

    spec = parse_asset_specification(SPEC_PATH)
    revisions = AssetRevisionRepository(db)
    workflow, tasks = create_asset_production_workflow(
        project.id,
        root,
        spec,
        image,
        provenance,
        provider_name="fake",
        provider_estimate=estimate,
        revision_repository=revisions,
    )

    fake = provider or ConfigurableCostFake(
        actual_cost=15.0, intent_repo=ProviderOperationIntentRepository(db)
    )
    if fake.intent_repo is None:
        fake.intent_repo = ProviderOperationIntentRepository(db)

    engine = WorkflowEngine(
        root,
        db,
        policy_engine=PolicyEngine(
            PolicyRule(
                require_approval_for_paid=False,
                require_approval_for_process_execution=False,
                project_budget=budget,
            )
        ),
        asset_provider=fake,
    )

    handlers = AssetProductionHandlers(
        root,
        engine.art_repo,
        revisions,
        engine.app_repo,
        ProviderOperationIntentRepository(db),
        engine.evi_repo,
        engine.gate_repo,
        engine.exec_repo,
        engine.artifact_mgr,
        fake,
        readiness_probes=PassingReadinessProbes(),
    )

    # Stub downstream stages to complete smoothly
    def process_stub(wf, task, execution):
        raw = next(
            a
            for a in handlers.artifacts.list_by_workflow(wf.id)
            if a.artifact_type == "asset-raw-glb"
        )
        handlers.artifact_manager.verify_artifact_integrity(raw)
        path = handlers._path(task, f"processed-attempt-{execution.attempt_number}.glb")
        path.write_bytes((handlers.root / raw.relative_path).read_bytes())
        report = handlers._path(task, f"processing-attempt-{execution.attempt_number}.json")
        report.write_text(json.dumps({"status": "SUCCESS", "exit_code": 0}), encoding="utf-8")
        return TaskHandlerResult(
            1,
            "deterministic process stub",
            [
                handlers._register(wf, task, execution, "asset-processed-glb", path),
                handlers._register(wf, task, execution, "asset-processing-report", report),
            ],
        )

    def validate_stub(wf, task, execution):
        processed = next(
            a
            for a in handlers.artifacts.list_by_workflow(wf.id)
            if a.artifact_type == "asset-processed-glb"
        )
        handlers.artifact_manager.verify_artifact_integrity(processed)
        path = handlers._path(task, f"validation-attempt-{execution.attempt_number}.json")
        path.write_text(json.dumps({"status": "PASS", "passed": True}), encoding="utf-8")
        return TaskHandlerResult(
            1,
            "deterministic validation stub",
            [handlers._register(wf, task, execution, "asset-validation-report", path)],
        )

    def godot_stub(wf, task, execution):
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

    handlers.process = process_stub
    handlers.validate = validate_stub
    handlers.godot = godot_stub
    register_asset_production_handlers(engine.handler_registry, handlers)
    engine.register_workflow(workflow, tasks, allow_existing_empty_placeholder=True)
    return engine, db, workflow.id, fake, handlers


def _decision(engine: WorkflowEngine, db: Database, approval_id: str, *, approve: bool) -> None:
    repo = ApprovalRepository(db)
    approval = repo.get(approval_id)
    assert approval is not None
    task = TaskRepository(db).get(approval.task_id)
    workflow = engine.wf_repo.get(approval.workflow_id)
    assert task is not None and workflow is not None
    if approve:
        decided = ApprovalService.approve(
            approval,
            "test_actor",
            current_inputs=engine.approval_inputs(workflow, task),
        )
    else:
        decided = ApprovalService.reject(approval, "test_actor", "reject test")
    assert repo.decide_if_pending(
        decided,
        AuditEvent(
            id=generate_id("AUDIT"),
            entity_type="Approval",
            entity_id=decided.id,
            action="APPROVED" if approve else "REJECTED",
            actor="test_actor",
        ),
    )


def _approve_both_gates(engine: WorkflowEngine, db: Database, workflow_id: str) -> None:
    # 1. Concept review
    res1 = engine.run_workflow(workflow_id)
    assert res1.status == WorkflowStatus.BLOCKED
    assert res1.pending_approval_id
    _decision(engine, db, res1.pending_approval_id, approve=True)
    # 2. Paid generation review
    res2 = engine.run_workflow(workflow_id)
    assert res2.status == WorkflowStatus.BLOCKED
    assert res2.pending_approval_id
    _decision(engine, db, res2.pending_approval_id, approve=True)


# ---------------------------------------------------------------------------
# Test Cases 1 through 10
# ---------------------------------------------------------------------------


def test_1_reservation_20_greater_than_actual_15(tmp_path: Path) -> None:
    """1. reservation 20 > actual 15 → net 15, one SETTLE 15, RELEASE 20."""
    fake = ConfigurableCostFake(actual_cost=15.0)
    engine, db, wf_id, fake, _ = _setup_accounting_env(tmp_path, estimate=20.0, provider=fake)
    _approve_both_gates(engine, db, wf_id)

    res = engine.run_workflow(wf_id)
    assert res.status in (WorkflowStatus.RUNNING, WorkflowStatus.BLOCKED)
    assert fake.invocation_count == 1

    ledger_repo = CostLedgerRepository(db)
    task_id = f"{wf_id}-PAID-GENERATION"
    entries = ledger_repo.list_by_task(task_id)
    assert len(entries) == 3

    assert entries[0].entry_type == EntryType.RESERVE and entries[0].amount == 20.0
    assert entries[1].entry_type == EntryType.SETTLE and entries[1].amount == 15.0
    assert entries[2].entry_type == EntryType.RELEASE and entries[2].amount == 20.0

    assert ledger_repo.project_net("asset-test") == 15.0
    acct = ledger_repo.operation_account(task_id)
    assert acct.settled
    assert acct.net == 15.0
    assert acct.held == 0.0


def test_2_actual_25_greater_than_reservation_20(tmp_path: Path) -> None:
    """2. actual 25 > reservation 20 → net 25 (no silent cap)."""
    fake = ConfigurableCostFake(actual_cost=25.0)
    engine, db, wf_id, fake, _ = _setup_accounting_env(
        tmp_path, estimate=20.0, budget=200.0, provider=fake
    )
    _approve_both_gates(engine, db, wf_id)

    engine.run_workflow(wf_id)
    assert fake.invocation_count == 1

    ledger_repo = CostLedgerRepository(db)
    task_id = f"{wf_id}-PAID-GENERATION"
    entries = ledger_repo.list_by_task(task_id)
    assert len(entries) == 3

    assert entries[0].entry_type == EntryType.RESERVE and entries[0].amount == 20.0
    assert entries[1].entry_type == EntryType.SETTLE and entries[1].amount == 25.0
    assert entries[2].entry_type == EntryType.RELEASE and entries[2].amount == 20.0

    assert ledger_repo.project_net("asset-test") == 25.0
    acct = ledger_repo.operation_account(task_id)
    assert acct.settled
    assert acct.net == 25.0


def test_3_actual_equals_reservation(tmp_path: Path) -> None:
    """3. actual == reservation → net 20."""
    fake = ConfigurableCostFake(actual_cost=20.0)
    engine, db, wf_id, fake, _ = _setup_accounting_env(tmp_path, estimate=20.0, provider=fake)
    _approve_both_gates(engine, db, wf_id)

    engine.run_workflow(wf_id)
    assert fake.invocation_count == 1

    ledger_repo = CostLedgerRepository(db)
    task_id = f"{wf_id}-PAID-GENERATION"
    entries = ledger_repo.list_by_task(task_id)
    assert len(entries) == 3

    assert entries[0].entry_type == EntryType.RESERVE and entries[0].amount == 20.0
    assert entries[1].entry_type == EntryType.SETTLE and entries[1].amount == 20.0
    assert entries[2].entry_type == EntryType.RELEASE and entries[2].amount == 20.0

    assert ledger_repo.project_net("asset-test") == 20.0
    assert ledger_repo.operation_account(task_id).settled


def test_4_actual_unknown_preserves_reservation_no_settle(tmp_path: Path) -> None:
    """4. actual unknown (report_unknown_cost) → net 20 held, no SETTLE."""
    fake = FakeAssetGenerationProvider(report_unknown_cost=True)
    engine, db, wf_id, fake, _ = _setup_accounting_env(tmp_path, estimate=20.0, provider=fake)
    _approve_both_gates(engine, db, wf_id)

    engine.run_workflow(wf_id)
    assert fake.invocation_count == 1

    ledger_repo = CostLedgerRepository(db)
    task_id = f"{wf_id}-PAID-GENERATION"
    entries = ledger_repo.list_by_task(task_id)
    assert len(entries) == 1
    assert entries[0].entry_type == EntryType.RESERVE and entries[0].amount == 20.0

    acct = ledger_repo.operation_account(task_id)
    assert not acct.settled
    assert acct.held == 20.0
    assert ledger_repo.project_net("asset-test") == 20.0


def test_5_running_partial_actual_handling(tmp_path: Path) -> None:
    """5. running with partial actual lower than held → no release; partial higher → top-up RESERVE."""
    db = Database(tmp_path / "test5.db")
    MigrationRunner(db).apply_all()
    ledger_repo = CostLedgerRepository(db)
    audit_repo = AuditLogRepository(db)
    accounting = CostAccounting(ledger_repo, audit_repo)

    task_id = "TASK-PARTIAL-TEST"
    # Initial reservation 20.0
    entry = LedgerEntry(
        project_id="proj-5",
        workflow_id="wf-5",
        task_id=task_id,
        entry_type=EntryType.RESERVE,
        amount=20.0,
        cost_unit="credits",
        reason="Initial reservation",
        source="execution_claim",
        actor="WorkflowEngine",
    )
    ledger_repo.append([entry])
    assert ledger_repo.operation_account(task_id).held == 20.0

    # 1. Partial actual 10 <= held 20: no entries, never release while in flight
    res1 = accounting.handle_in_flight(task_id, 10.0)
    assert res1 == []
    assert ledger_repo.operation_account(task_id).held == 20.0
    assert ledger_repo.project_net("proj-5") == 20.0

    # 2. Partial actual 26 > held 20: appends top-up RESERVE of 6
    res2 = accounting.handle_in_flight(task_id, 26.0)
    assert len(res2) == 1
    assert res2[0].entry_type == EntryType.RESERVE
    assert res2[0].amount == 6.0
    assert res2[0].source == "provider_partial_actual"

    acct = ledger_repo.operation_account(task_id)
    assert acct.held == 26.0
    assert ledger_repo.project_net("proj-5") == 26.0

    # 3. Final settlement to 26: releases 26 and settles 26
    accounting.settle_terminal_success(task_id, 26.0)
    final_acct = ledger_repo.operation_account(task_id)
    assert final_acct.settled
    assert final_acct.net == 26.0
    assert ledger_repo.project_net("proj-5") == 26.0


def test_6_uncertain_submission_holds_reservation_reconcile_not_eligible(tmp_path: Path) -> None:
    """6. UNCERTAIN submission (simulate_uncertain_submission) → hold 20, no release; reconcile dry-run marks NOT eligible."""
    fake = FakeAssetGenerationProvider(simulate_uncertain_submission=True)
    engine, db, wf_id, fake, _ = _setup_accounting_env(tmp_path, estimate=20.0, provider=fake)
    _approve_both_gates(engine, db, wf_id)

    res = engine.run_workflow(wf_id)
    assert res.status == WorkflowStatus.BLOCKED
    assert fake.invocation_count == 0  # failed before submission id confirmed

    ledger_repo = CostLedgerRepository(db)
    task_id = f"{wf_id}-PAID-GENERATION"
    entries = ledger_repo.list_by_task(task_id)
    assert len(entries) == 1
    assert entries[0].entry_type == EntryType.RESERVE and entries[0].amount == 20.0
    assert ledger_repo.project_net("asset-test") == 20.0

    import argparse

    from gamefactory.cli.main import _accounting_reconcile

    args = argparse.Namespace(
        workflow=wf_id, task=None, apply=False, dry_run=True, actor=None, reason=None
    )
    payload, code, _ = _accounting_reconcile(db, args)
    assert code == 0
    op = next(o for o in payload["operations"] if o["task_id"] == task_id)
    assert not op["eligible"]
    assert "evidence is required" in op["reason"].lower()


def test_7_terminal_failed_with_cost_info_settles_to_actual(tmp_path: Path) -> None:
    """7. terminal FAILED with cost info → settle to actual."""
    fake = ConfigurableCostFake(fail_with_cost=8.0)
    engine, db, wf_id, fake, _ = _setup_accounting_env(tmp_path, estimate=20.0, provider=fake)
    _approve_both_gates(engine, db, wf_id)

    res = engine.run_workflow(wf_id)
    assert res.status == WorkflowStatus.FAILED

    ledger_repo = CostLedgerRepository(db)
    task_id = f"{wf_id}-PAID-GENERATION"
    entries = ledger_repo.list_by_task(task_id)
    assert len(entries) == 3

    assert entries[0].entry_type == EntryType.RESERVE and entries[0].amount == 20.0
    assert entries[1].entry_type == EntryType.SETTLE and entries[1].amount == 8.0
    assert entries[2].entry_type == EntryType.RELEASE and entries[2].amount == 20.0

    assert ledger_repo.project_net("asset-test") == 8.0
    acct = ledger_repo.operation_account(task_id)
    assert acct.settled
    assert acct.net == 8.0


def test_8_crash_recovery_query_only_settles_original_reservation(tmp_path: Path) -> None:
    """8. crash-recovery case:

    attempt 1 claims (reservation 20), external id persisted, then process crashes before
    terminal cost known; attempt 2 is query-only recovery where provider reports SUCCEEDED
    actual 15. Assert provider submissions == 1, intents == 1, net 15, reservation excess
    released, no double count, and SETTLE is linked to original intent.
    """
    fake = ConfigurableCostFake(actual_cost=15.0, simulate_crash=True)
    engine, db, wf_id, fake, _ = _setup_accounting_env(tmp_path, estimate=20.0, provider=fake)
    _approve_both_gates(engine, db, wf_id)

    # Attempt 1 crashes after provider accepts submission
    crashed = engine.run_workflow(wf_id)
    assert crashed.status == WorkflowStatus.BLOCKED
    assert fake.invocation_count == 1

    intents = ProviderOperationIntentRepository(db).list_by_workflow(wf_id)
    assert len(intents) == 1
    original_intent = intents[0]
    assert original_intent.status == "SUBMITTED"
    assert original_intent.external_task_id is not None

    ledger_repo = CostLedgerRepository(db)
    task_id = f"{wf_id}-PAID-GENERATION"
    assert ledger_repo.project_net("asset-test") == 20.0

    # Attempt 2: Turn off crash simulation, re-run workflow
    fake.simulate_crash = False
    recovered = engine.run_workflow(wf_id)
    assert recovered.status in (WorkflowStatus.RUNNING, WorkflowStatus.BLOCKED)

    # Invariants:
    # 1. No second provider submission
    assert fake.invocation_count == 1
    # 2. No second intent created
    intents_after = ProviderOperationIntentRepository(db).list_by_workflow(wf_id)
    assert len(intents_after) == 1
    # 3. Two executions exist: attempt 1 (failed/uncertain) and attempt 2 (completed)
    exec_repo = ExecutionRepository(db)
    attempts = exec_repo.list_by_task(task_id)
    assert len(attempts) == 2
    # 4. Total project net is 15 (excess 5 released, no double counting)
    assert ledger_repo.project_net("asset-test") == 15.0
    # 5. SETTLE entry is linked to original intent
    settle_entry = next(
        e for e in ledger_repo.list_by_task(task_id) if e.entry_type == EntryType.SETTLE
    )
    assert settle_entry.amount == 15.0
    assert settle_entry.intent_id == original_intent.id
    assert settle_entry.request_fingerprint == original_intent.request_fingerprint


def test_9_pre_submission_failure_releases_reservation(tmp_path: Path) -> None:
    """9. pre-submission failure (e.g. ValidationError raised before any intent) → reservation released, net 0."""
    engine, db, wf_id, fake, handlers = _setup_accounting_env(tmp_path, estimate=20.0)
    _approve_both_gates(engine, db, wf_id)

    # Pre-create raw attempt artifact so paid_generate raises RawArtifactInvalidError
    # before creating any provider operation intent.
    task_id = f"{wf_id}-PAID-GENERATION"
    task = engine.task_repo.get(task_id)
    assert task is not None
    raw_path = handlers._path(task, "raw-attempt-1.glb")
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(b"existing raw glb collision")

    failed = engine.run_workflow(wf_id)
    assert failed.status == WorkflowStatus.FAILED

    # No intent was created
    intents = ProviderOperationIntentRepository(db).list_by_workflow(wf_id)
    assert len(intents) == 0

    # Reservation was claimed and immediately released on failure, net is 0
    ledger_repo = CostLedgerRepository(db)
    assert ledger_repo.project_net("asset-test") == 0.0
    entries = ledger_repo.list_by_task(task_id)
    assert len(entries) == 2
    assert entries[0].entry_type == EntryType.RESERVE and entries[0].amount == 20.0
    assert entries[1].entry_type == EntryType.RELEASE and entries[1].amount == 20.0
    assert entries[1].source == "no_provider_submission"


def test_10_budget_gate_unblocked_after_settlement(tmp_path: Path) -> None:
    """10. budget: second workflow blocked by ledger-held reservation; after settlement frees budget it proceeds."""
    fake = ConfigurableCostFake(actual_cost=10.0)
    engine, db, wf1_id, fake, handlers = _setup_accounting_env(
        tmp_path, project_id="budget-test", estimate=20.0, budget=30.0, provider=fake
    )
    _approve_both_gates(engine, db, wf1_id)

    # WF1 claims reservation of 20.0 and crashes after provider submission (held 20)
    fake.simulate_crash = True
    crashed = engine.run_workflow(wf1_id)
    assert crashed.status == WorkflowStatus.BLOCKED
    ledger_repo = CostLedgerRepository(db)
    assert ledger_repo.project_net("budget-test") == 20.0
    initial_invocations = fake.invocation_count
    assert initial_invocations == 1

    # WF2 (estimate 15.0):
    project = ProjectRepository(db).get("budget-test")
    assert project is not None
    spec = parse_asset_specification(SPEC_PATH)
    image = engine.project_root / "concept.png"
    provenance = engine.project_root / "concept.json"
    wf2, tasks2 = create_asset_production_workflow(
        project.id,
        engine.project_root,
        spec,
        image,
        provenance,
        provider_name="fake",
        provider_estimate=15.0,
        revision_repository=handlers.revisions,
    )
    engine.register_workflow(wf2, tasks2, allow_existing_empty_placeholder=True)

    # run -> concept approval -> approve
    res_wf2_concept = engine.run_workflow(wf2.id)
    assert res_wf2_concept.status == WorkflowStatus.BLOCKED
    assert res_wf2_concept.pending_approval_id is not None
    concept_app = ApprovalRepository(db).get(res_wf2_concept.pending_approval_id)
    assert concept_app is not None and concept_app.approval_type == "concept_review"
    _decision(engine, db, res_wf2_concept.pending_approval_id, approve=True)

    # run -> result BLOCKED with error_code BUDGET_BLOCKED and no paid approval created, provider invocation_count unchanged
    res_wf2_budget = engine.run_workflow(wf2.id)
    assert res_wf2_budget.status == WorkflowStatus.BLOCKED
    assert res_wf2_budget.error_code == "BUDGET_BLOCKED"
    paid_approvals = [
        a
        for a in ApprovalRepository(db).list_by_workflow(wf2.id)
        if a.approval_type == "paid_generation"
    ]
    assert len(paid_approvals) == 0
    assert fake.invocation_count == initial_invocations

    # WF1 resumes -> query-only settle -> project net 10
    fake.simulate_crash = False
    recovered = engine.run_workflow(wf1_id)
    assert recovered.status in (WorkflowStatus.RUNNING, WorkflowStatus.BLOCKED)
    assert ledger_repo.project_net("budget-test") == 10.0

    # WF2 run -> now BLOCKED on a pending paid_generation approval (budget passes); approve
    res_wf2_gate = engine.run_workflow(wf2.id)
    assert res_wf2_gate.status == WorkflowStatus.BLOCKED
    assert res_wf2_gate.pending_approval_id is not None
    paid_app = ApprovalRepository(db).get(res_wf2_gate.pending_approval_id)
    assert paid_app is not None and paid_app.approval_type == "paid_generation"
    _decision(engine, db, res_wf2_gate.pending_approval_id, approve=True)

    # run -> the paid stage proceeds (fake invocation_count increments by exactly 1)
    res_wf2_proceed = engine.run_workflow(wf2.id)
    assert res_wf2_proceed.status in (WorkflowStatus.RUNNING, WorkflowStatus.BLOCKED)
    assert fake.invocation_count == initial_invocations + 1

    # Assert project net at the end equals 10 + WF2's settled actual
    task2_paid_id = f"{wf2.id}-PAID-GENERATION"
    wf2_acct = ledger_repo.operation_account(task2_paid_id)
    assert wf2_acct.settled
    assert ledger_repo.project_net("budget-test") == 10.0 + wf2_acct.net


def test_retry_claims_never_stack_reservations_on_one_paid_operation(tmp_path: Path) -> None:
    """Reviewer finding: a retry after settlement (or while a hold exists) adds no new hold."""
    from gamefactory.core.domain.models import Execution, TaskStatus

    engine, db, workflow_id, _, _ = _setup_accounting_env(tmp_path, estimate=20.0)
    ledger = CostLedgerRepository(db)
    tasks = TaskRepository(db)
    paid_task_id = f"{workflow_id}-PAID-GENERATION"

    def claim(attempt: int) -> None:
        tasks.update_status(paid_task_id, TaskStatus.PENDING)
        execution = Execution(
            id=f"EXEC-RETRY-{attempt}",
            task_id=paid_task_id,
            attempt_number=attempt,
            cost=20.0,
            estimated_cost=20.0,
            cost_unit="credits",
        )
        assert tasks.claim_execution(paid_task_id, execution, "asset-test", 100.0)

    claim(1)
    assert ledger.project_net("asset-test") == 20.0
    claim(2)  # unsettled hold already covers the request: no second hold
    assert ledger.project_net("asset-test") == 20.0

    account = ledger.operation_account(paid_task_id)
    ledger.append(plan_settlement(account, 15.0))
    assert ledger.project_net("asset-test") == 15.0
    claim(3)  # settled operation: a retry (e.g. failed provider query) reserves nothing
    assert ledger.project_net("asset-test") == 15.0


def test_uncertain_attempt_without_provider_contact_can_be_released(tmp_path: Path) -> None:
    """Reviewer finding: a crash before the intent claim must not leave a permanent hold."""
    import argparse

    from gamefactory.cli.main import _accounting_reconcile
    from gamefactory.core.domain.models import Execution, ExecutionStatus, TaskStatus

    engine, db, workflow_id, fake, _ = _setup_accounting_env(tmp_path, estimate=20.0)
    tasks = TaskRepository(db)
    paid_task_id = f"{workflow_id}-PAID-GENERATION"
    tasks.update_status(paid_task_id, TaskStatus.PENDING)
    execution = Execution(
        id="EXEC-CRASHED",
        task_id=paid_task_id,
        attempt_number=1,
        cost=20.0,
        estimated_cost=20.0,
        cost_unit="credits",
    )
    assert tasks.claim_execution(paid_task_id, execution, "asset-test", 100.0)
    execution.status = ExecutionStatus.UNCERTAIN
    ExecutionRepository(db).save(execution)
    ledger = CostLedgerRepository(db)
    assert ledger.project_net("asset-test") == 20.0

    args = argparse.Namespace(
        workflow=workflow_id, task=None, apply=True, actor="lead", reason="crash before claim"
    )
    payload, code, _ = _accounting_reconcile(db, args)

    assert code == 0, payload
    assert ledger.project_net("asset-test") == 0.0
    assert fake.invocation_count == 0
