"""SQLite-backed V0.4 asset DAG invariants; all generation is deterministic fake work."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from gamefactory.adapters.fakes.fake_provider import FakeAssetGenerationProvider
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    ExecutionRepository,
    ProjectRepository,
    ProviderOperationIntentRepository,
    TaskRepository,
)
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.domain.asset_contracts import parse_asset_specification
from gamefactory.core.domain.errors import ArtifactError, ToolExecutionError
from gamefactory.core.domain.models import AuditEvent, Project, WorkflowStatus, generate_id
from gamefactory.core.policies.policy_engine import PolicyEngine, PolicyRule
from gamefactory.workflows.asset_production import (
    AssetProductionHandlers,
    create_asset_production_workflow,
    register_asset_production_handlers,
)
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.handlers import TaskHandlerResult

SPEC = (
    Path(__file__).resolve().parents[2] / "src/gamefactory/resources/specs/prop_energy_crate_01.yml"
)


class HigherActualCostFake(FakeAssetGenerationProvider):
    """Fake-only invoice larger than the approved estimate for accounting tests."""

    def generate(self, request):
        response = super().generate(request)
        response.cost = 15.0
        response.details["actual_cost"] = 15.0
        if self.intent_repo is not None:
            intent = self.intent_repo.get_by_task(str(request.parameters["task_id"]))
            if intent is not None:
                intent.actual_cost = 15.0
                self.intent_repo.save(intent)
        return response


class MissingRawHigherCostFake(HigherActualCostFake):
    def _write_output(self, request):
        return None


class DurableBillingTerminalFake(FakeAssetGenerationProvider):
    def generate(self, request):
        try:
            return super().generate(request)
        except ToolExecutionError:
            assert self.intent_repo is not None
            intent = self.intent_repo.get_by_task(str(request.parameters["task_id"]))
            assert intent is not None
            intent.actual_cost = 15.0
            self.intent_repo.save(intent)
            raise


def _setup(
    tmp_path: Path,
    *,
    estimate: float | None = 5.0,
    reservation: float | None = None,
    budget: float = 500.0,
    provider: FakeAssetGenerationProvider | None = None,
    stub_downstream: bool = False,
    fail_process_once: bool = False,
    validate_real: bool = False,
    processed_mutation: str | None = None,
    fail_godot_once: bool = False,
) -> tuple[WorkflowEngine, Database, str, FakeAssetGenerationProvider, AssetProductionHandlers]:
    root = tmp_path / "project"
    root.mkdir()
    db = Database(root / ".gamefactory/state/factory.db")
    MigrationRunner(db).apply_all()
    project = Project(
        id="asset-test", name="Asset integration test", engine_type="godot", root_path=str(root)
    )
    ProjectRepository(db).save(project)
    image = root / "concept.png"
    Image.new("RGB", (2, 2), (20, 70, 140)).save(image)
    provenance = root / "concept.json"
    provenance.write_text(
        json.dumps({"sha256": hashlib.sha256(image.read_bytes()).hexdigest()}), encoding="utf-8"
    )
    spec = parse_asset_specification(SPEC)
    revisions = AssetRevisionRepository(db)
    workflow, tasks = create_asset_production_workflow(
        project.id,
        root,
        spec,
        image,
        provenance,
        provider_name="fake",
        provider_estimate=estimate,
        budget_reservation=reservation,
        revision_repository=revisions,
    )
    fake = provider or FakeAssetGenerationProvider(
        intent_repo=ProviderOperationIntentRepository(db)
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
    )
    if stub_downstream:
        _install_downstream_stubs(
            handlers,
            fail_process_once=fail_process_once,
            validate_real=validate_real,
            processed_mutation=processed_mutation,
            fail_godot_once=fail_godot_once,
        )
    register_asset_production_handlers(engine.handler_registry, handlers)
    engine.register_workflow(workflow, tasks, allow_existing_empty_placeholder=True)
    return engine, db, workflow.id, fake, handlers


def _install_downstream_stubs(
    handlers: AssetProductionHandlers,
    *,
    fail_process_once: bool,
    validate_real: bool,
    processed_mutation: str | None,
    fail_godot_once: bool,
) -> None:
    """Use deterministic outputs after the real paid handler; preserve artifact verification."""
    state = {"process_calls": 0, "godot_calls": 0}

    def process(workflow, task, execution):
        state["process_calls"] += 1
        raw = next(
            a
            for a in handlers.artifacts.list_by_workflow(workflow.id)
            if a.artifact_type == "asset-raw-glb"
        )
        handlers.artifact_manager.verify_artifact_integrity(raw)
        if fail_process_once and state["process_calls"] == 1:
            raise ToolExecutionError("simulated Blender failure before any processed artifact")
        path = handlers._path(task, f"processed-attempt-{execution.attempt_number}.glb")
        if processed_mutation is not None:
            spec = parse_asset_specification(task.parameters["specification"])
            create_box_glb(
                width_m=12.0 if processed_mutation == "scale_x10" else spec.dimensions.width_m,
                depth_m=10.0 if processed_mutation == "scale_x10" else spec.dimensions.depth_m,
                height_m=10.0 if processed_mutation == "scale_x10" else spec.dimensions.height_m,
                mesh_name=f"SM_{spec.asset_id}",
                collider_name=f"COL_{spec.asset_id}",
                include_lod1=True,
                include_collider=processed_mutation != "remove_collider",
                include_texture=True,
                output_path=path,
            )
        else:
            path.write_bytes((handlers.root / raw.relative_path).read_bytes())
        report = handlers._path(task, f"processing-attempt-{execution.attempt_number}.json")
        report.write_text(json.dumps({"status": "SUCCESS", "exit_code": 0}), encoding="utf-8")
        return TaskHandlerResult(
            1,
            "deterministic process stub",
            [
                handlers._register(workflow, task, execution, "asset-processed-glb", path),
                handlers._register(workflow, task, execution, "asset-processing-report", report),
            ],
        )

    def validate(workflow, task, execution):
        processed = next(
            a
            for a in handlers.artifacts.list_by_workflow(workflow.id)
            if a.artifact_type == "asset-processed-glb"
        )
        handlers.artifact_manager.verify_artifact_integrity(processed)
        path = handlers._path(task, f"validation-attempt-{execution.attempt_number}.json")
        path.write_text(json.dumps({"status": "PASS", "passed": True}), encoding="utf-8")
        return TaskHandlerResult(
            1,
            "deterministic validation stub",
            [handlers._register(workflow, task, execution, "asset-validation-report", path)],
        )

    def godot(workflow, task, execution):
        state["godot_calls"] += 1
        if fail_godot_once and state["godot_calls"] == 1:
            raise ToolExecutionError("simulated Godot stage failure before observation")
        processed = next(
            a
            for a in handlers.artifacts.list_by_workflow(workflow.id)
            if a.artifact_type == "asset-processed-glb"
        )
        handlers.artifact_manager.verify_artifact_integrity(processed)
        path = handlers._path(task, f"runtime-{execution.id}.json")
        path.write_text(
            json.dumps({"status": "PASS", "execution_id": execution.id}), encoding="utf-8"
        )
        ids = [handlers._register(workflow, task, execution, "asset-runtime-observation", path)]
        for angle in ("front", "three_quarter", "side"):
            capture = handlers._path(task, f"{execution.id}-{angle}.png")
            Image.new("RGB", (2, 2), (20, 70, 140)).save(capture)
            ids.append(
                handlers._register(workflow, task, execution, "asset-runtime-capture", capture)
            )
        return TaskHandlerResult(1, "deterministic runtime stub", ids)

    handlers.process = process
    if not validate_real:
        handlers.validate = validate
    handlers.godot = godot


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
            "integration-test-actor",
            current_inputs=engine.approval_inputs(workflow, task),
        )
    else:
        decided = ApprovalService.reject(approval, "integration-test-actor", "reject test")
    assert repo.decide_if_pending(
        decided,
        AuditEvent(
            id=generate_id("AUDIT"),
            entity_type="Approval",
            entity_id=decided.id,
            action="APPROVED" if approve else "REJECTED",
            actor="integration-test-actor",
            previous_state="PENDING",
            new_state="APPROVED" if approve else "REJECTED",
        ),
    )


def _at_paid_gate(engine: WorkflowEngine, db: Database, workflow_id: str) -> str:
    concept = engine.run_workflow(workflow_id)
    assert concept.status == WorkflowStatus.BLOCKED
    approval = ApprovalRepository(db).get(concept.pending_approval_id or "")
    assert approval is not None and approval.approval_type == "concept_review"
    _decision(engine, db, approval.id, approve=True)
    paid = engine.run_workflow(workflow_id)
    assert paid.status == WorkflowStatus.BLOCKED
    approval = ApprovalRepository(db).get(paid.pending_approval_id or "")
    assert approval is not None and approval.approval_type == "paid_generation"
    return approval.id


def test_mandatory_concept_and_paid_gates_when_generic_policy_disabled(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup(tmp_path)
    _at_paid_gate(engine, db, workflow_id)
    assert fake.invocation_count == 0
    types = {t.task_type: t.status.value for t in TaskRepository(db).list_by_workflow(workflow_id)}
    assert types["asset_prepare"] == "COMPLETED"
    assert types["asset_concept_review"] == "COMPLETED"
    assert types["asset_paid_generation"] == "BLOCKED"


def test_rejected_concept_never_invokes_provider_or_regenerates(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup(tmp_path)
    pending = engine.run_workflow(workflow_id)
    assert pending.status == WorkflowStatus.BLOCKED
    _decision(engine, db, pending.pending_approval_id or "", approve=False)
    assert engine.run_workflow(workflow_id).status == WorkflowStatus.FAILED
    assert engine.run_workflow(workflow_id).status == WorkflowStatus.FAILED
    assert fake.invocation_count == 0
    assert not ProviderOperationIntentRepository(db).get_by_task(f"{workflow_id}-PAID-GENERATION")


def test_project_budget_blocks_before_provider_call(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup(tmp_path, estimate=10.0, budget=9.0)
    first = engine.run_workflow(workflow_id)
    assert first.status == WorkflowStatus.BLOCKED
    _decision(engine, db, first.pending_approval_id or "", approve=True)
    blocked = engine.run_workflow(workflow_id)
    assert blocked.status == WorkflowStatus.BLOCKED
    assert "budget" in (blocked.error_message or "").lower()
    assert fake.invocation_count == 0
    assert (
        ProviderOperationIntentRepository(db).get_by_task(f"{workflow_id}-PAID-GENERATION") is None
    )


def test_reported_actual_above_reservation_is_counted_once(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup(
        tmp_path,
        estimate=10.0,
        budget=20.0,
        provider=HigherActualCostFake(),
        stub_downstream=True,
    )
    paid_id = _at_paid_gate(engine, db, workflow_id)
    _decision(engine, db, paid_id, approve=True)
    pending = engine.run_workflow(workflow_id)
    assert pending.status == WorkflowStatus.BLOCKED
    assert fake.invocation_count == 1
    paid_attempts = ExecutionRepository(db).list_by_task(f"{workflow_id}-PAID-GENERATION")
    assert len(paid_attempts) == 1
    assert sum(attempt.cost for attempt in paid_attempts) == 15.0


@pytest.mark.parametrize("provider_kind", ["malformed", "missing"])
def test_actual_billing_survives_raw_failure_before_blender(
    tmp_path: Path,
    provider_kind: str,
) -> None:
    provider = (
        HigherActualCostFake(raw_malformed=True)
        if provider_kind == "malformed"
        else MissingRawHigherCostFake()
    )
    engine, db, workflow_id, fake, _ = _setup(
        tmp_path,
        estimate=10.0,
        budget=20.0,
        provider=provider,
        stub_downstream=True,
    )
    paid_id = _at_paid_gate(engine, db, workflow_id)
    _decision(engine, db, paid_id, approve=True)
    result = engine.run_workflow(workflow_id)
    assert result.status == WorkflowStatus.FAILED
    assert fake.invocation_count == 1
    attempts = ExecutionRepository(db).list_by_task(f"{workflow_id}-PAID-GENERATION")
    assert len(attempts) == 1 and attempts[0].cost == 15.0
    assert ExecutionRepository(db).list_by_task(f"{workflow_id}-PROCESS") == []
    assert not any(
        a.approval_type == "final_visual_review"
        for a in ApprovalRepository(db).list_by_workflow(workflow_id)
    )


def test_durable_actual_billing_survives_terminal_provider_exception(tmp_path: Path) -> None:
    provider = DurableBillingTerminalFake(fail_times=1)
    engine, db, workflow_id, fake, _ = _setup(
        tmp_path,
        estimate=10.0,
        budget=20.0,
        provider=provider,
        stub_downstream=True,
    )
    paid_id = _at_paid_gate(engine, db, workflow_id)
    _decision(engine, db, paid_id, approve=True)
    result = engine.run_workflow(workflow_id)
    assert result.status == WorkflowStatus.FAILED
    assert fake.invocation_count == 1
    intent = ProviderOperationIntentRepository(db).get_by_task(f"{workflow_id}-PAID-GENERATION")
    assert intent is not None and intent.actual_cost == 15.0
    attempts = ExecutionRepository(db).list_by_task(f"{workflow_id}-PAID-GENERATION")
    assert len(attempts) == 1 and attempts[0].cost == 15.0
    assert ExecutionRepository(db).list_by_task(f"{workflow_id}-PROCESS") == []
    assert not any(
        a.approval_type == "final_visual_review"
        for a in ApprovalRepository(db).list_by_workflow(workflow_id)
    )


def test_unknown_cost_without_reservation_blocks_before_provider_call(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup(tmp_path, estimate=None)
    approval_id = _at_paid_gate(engine, db, workflow_id)
    _decision(engine, db, approval_id, approve=True)
    result = engine.run_workflow(workflow_id)
    assert result.status in (WorkflowStatus.FAILED, WorkflowStatus.BLOCKED)
    assert fake.invocation_count == 0
    assert (
        ProviderOperationIntentRepository(db).get_by_task(f"{workflow_id}-PAID-GENERATION") is None
    )


def test_fake_generation_once_and_completed_paid_stage_resume_once(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup(tmp_path)
    approval_id = _at_paid_gate(engine, db, workflow_id)
    _decision(engine, db, approval_id, approve=True)
    result = engine.run_workflow(workflow_id)
    # Without Blender on this CI path, the completed paid stage remains durable.
    assert result.status in (WorkflowStatus.FAILED, WorkflowStatus.BLOCKED)
    assert fake.invocation_count == 1
    assert any(
        a.artifact_type == "asset-raw-glb"
        for a in ArtifactRepository(db).list_by_workflow(workflow_id)
    )
    engine.run_workflow(workflow_id)
    assert fake.invocation_count == 1
    attempts = ExecutionRepository(db).list_by_task(f"{workflow_id}-PAID-GENERATION")
    assert len(attempts) == 1 and attempts[0].status.value == "COMPLETED"


def test_final_review_is_mandatory_and_repeated_resume_does_not_regenerate(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup(tmp_path, stub_downstream=True)
    approval_id = _at_paid_gate(engine, db, workflow_id)
    _decision(engine, db, approval_id, approve=True)
    pending = engine.run_workflow(workflow_id)
    assert pending.status == WorkflowStatus.BLOCKED
    final = ApprovalRepository(db).get(pending.pending_approval_id or "")
    assert final is not None and final.approval_type == "final_visual_review"
    assert fake.invocation_count == 1
    assert engine.run_workflow(workflow_id).status == WorkflowStatus.BLOCKED
    assert fake.invocation_count == 1
    _decision(engine, db, final.id, approve=True)
    assert engine.run_workflow(workflow_id).status == WorkflowStatus.COMPLETED
    assert engine.run_workflow(workflow_id).status == WorkflowStatus.COMPLETED
    assert fake.invocation_count == 1


def test_final_rejection_is_terminal_and_new_revision_opens_new_concept_gate(
    tmp_path: Path,
) -> None:
    engine, db, workflow_id, fake, _ = _setup(tmp_path, stub_downstream=True)
    paid_id = _at_paid_gate(engine, db, workflow_id)
    _decision(engine, db, paid_id, approve=True)
    pending = engine.run_workflow(workflow_id)
    final = ApprovalRepository(db).get(pending.pending_approval_id or "")
    assert final is not None and final.approval_type == "final_visual_review"
    _decision(engine, db, final.id, approve=False)
    assert engine.run_workflow(workflow_id).status == WorkflowStatus.FAILED
    assert engine.run_workflow(workflow_id).status == WorkflowStatus.FAILED
    assert fake.invocation_count == 1
    intents = ProviderOperationIntentRepository(db)
    first_intent = intents.get_by_task(f"{workflow_id}-PAID-GENERATION")
    assert first_intent is not None and first_intent.external_task_id

    root = engine.project_root
    new_workflow, tasks = create_asset_production_workflow(
        "asset-test",
        root,
        parse_asset_specification(SPEC),
        root / "concept.png",
        root / "concept.json",
        provider_name="fake",
        provider_estimate=5.0,
        revision_repository=AssetRevisionRepository(db),
    )
    assert tasks[0].parameters["revision_number"] == 2
    engine.register_workflow(new_workflow, tasks, allow_existing_empty_placeholder=True)
    second_fake = FakeAssetGenerationProvider(intent_repo=intents)
    resumed_engine = WorkflowEngine(
        root,
        db,
        policy_engine=PolicyEngine(PolicyRule(require_approval_for_paid=False)),
        asset_provider=second_fake,
    )
    second_handlers = AssetProductionHandlers(
        root,
        resumed_engine.art_repo,
        AssetRevisionRepository(db),
        resumed_engine.app_repo,
        intents,
        resumed_engine.evi_repo,
        resumed_engine.gate_repo,
        resumed_engine.exec_repo,
        resumed_engine.artifact_mgr,
        second_fake,
    )
    _install_downstream_stubs(
        second_handlers,
        fail_process_once=False,
        validate_real=False,
        processed_mutation=None,
        fail_godot_once=False,
    )
    register_asset_production_handlers(resumed_engine.handler_registry, second_handlers)
    next_gate = resumed_engine.run_workflow(new_workflow.id)
    next_approval = ApprovalRepository(db).get(next_gate.pending_approval_id or "")
    assert next_approval is not None and next_approval.approval_type == "concept_review"
    assert next_approval.workflow_id == new_workflow.id
    _decision(resumed_engine, db, next_approval.id, approve=True)
    next_paid = resumed_engine.run_workflow(new_workflow.id)
    paid_approval = ApprovalRepository(db).get(next_paid.pending_approval_id or "")
    assert paid_approval is not None and paid_approval.approval_type == "paid_generation"
    _decision(resumed_engine, db, paid_approval.id, approve=True)
    second_pending = resumed_engine.run_workflow(new_workflow.id)
    assert second_pending.status == WorkflowStatus.BLOCKED, second_pending.error_message
    second_intent = intents.get_by_task(f"{new_workflow.id}-PAID-GENERATION")
    assert second_intent is not None and second_intent.external_task_id
    assert second_intent.external_task_id != first_intent.external_task_id
    assert second_fake.invocation_count == 1
    assert (
        intents.get_by_task(f"{workflow_id}-PAID-GENERATION").external_task_id
        == first_intent.external_task_id
    )
    assert fake.invocation_count == 1


def test_paid_rejection_never_invokes_provider(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup(tmp_path)
    paid_id = _at_paid_gate(engine, db, workflow_id)
    _decision(engine, db, paid_id, approve=False)
    assert engine.run_workflow(workflow_id).status == WorkflowStatus.FAILED
    assert fake.invocation_count == 0


def test_terminal_provider_failure_stops_before_blender_and_final_review(tmp_path: Path) -> None:
    fake = FakeAssetGenerationProvider(fail_times=1)
    engine, db, workflow_id, fake, _ = _setup(tmp_path, provider=fake, stub_downstream=True)
    paid_id = _at_paid_gate(engine, db, workflow_id)
    _decision(engine, db, paid_id, approve=True)
    failed = engine.run_workflow(workflow_id)
    assert failed.status == WorkflowStatus.FAILED
    assert fake.invocation_count == 1
    assert ExecutionRepository(db).list_by_task(f"{workflow_id}-PROCESS") == []
    assert not any(
        a.artifact_type in {"asset-processed-glb", "asset-processing-report"}
        for a in ArtifactRepository(db).list_by_workflow(workflow_id)
    )
    assert not any(
        a.approval_type == "final_visual_review"
        for a in ApprovalRepository(db).list_by_workflow(workflow_id)
    )
    assert engine.run_workflow(workflow_id).status == WorkflowStatus.FAILED
    assert fake.invocation_count == 1


def test_raw_tamper_stops_process_retry_without_second_generation(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup(
        tmp_path,
        stub_downstream=True,
        fail_process_once=True,
    )
    paid_id = _at_paid_gate(engine, db, workflow_id)
    _decision(engine, db, paid_id, approve=True)
    assert engine.run_workflow(workflow_id).status == WorkflowStatus.FAILED
    raw = next(
        a
        for a in ArtifactRepository(db).list_by_workflow(workflow_id)
        if a.artifact_type == "asset-raw-glb"
    )
    (engine.project_root / raw.relative_path).write_bytes(b"tampered raw glb")
    result = engine.retry_task(workflow_id, f"{workflow_id}-PROCESS")
    assert result.status != WorkflowStatus.COMPLETED
    assert fake.invocation_count == 1
    assert not any(
        a.artifact_type == "asset-processed-glb"
        for a in ArtifactRepository(db).list_by_workflow(workflow_id)
    )


def test_processed_tamper_stops_final_review_without_second_generation(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup(tmp_path, stub_downstream=True)
    paid_id = _at_paid_gate(engine, db, workflow_id)
    _decision(engine, db, paid_id, approve=True)
    pending = engine.run_workflow(workflow_id)
    assert pending.status == WorkflowStatus.BLOCKED
    processed = next(
        a
        for a in ArtifactRepository(db).list_by_workflow(workflow_id)
        if a.artifact_type == "asset-processed-glb"
    )
    (engine.project_root / processed.relative_path).write_bytes(b"tampered processed glb")
    final = ApprovalRepository(db).get(pending.pending_approval_id or "")
    assert final is not None
    with pytest.raises(ArtifactError):
        _decision(engine, db, final.id, approve=True)
    assert fake.invocation_count == 1


def test_malformed_fake_glb_fails_real_validator_before_final_gate(tmp_path: Path) -> None:
    fake = FakeAssetGenerationProvider(raw_malformed=True)
    engine, db, workflow_id, fake, _ = _setup(
        tmp_path,
        provider=fake,
        stub_downstream=True,
        validate_real=True,
    )
    paid_id = _at_paid_gate(engine, db, workflow_id)
    _decision(engine, db, paid_id, approve=True)
    result = engine.run_workflow(workflow_id)
    assert result.status == WorkflowStatus.FAILED
    assert result.error_code == "RAW_ARTIFACT_INVALID"
    assert fake.invocation_count == 1
    assert not any(
        a.approval_type == "final_visual_review"
        for a in ApprovalRepository(db).list_by_workflow(workflow_id)
    )


def test_blender_failure_retry_never_submits_second_provider_task(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup(
        tmp_path,
        stub_downstream=True,
        fail_process_once=True,
    )
    paid_id = _at_paid_gate(engine, db, workflow_id)
    _decision(engine, db, paid_id, approve=True)
    assert engine.run_workflow(workflow_id).status == WorkflowStatus.FAILED
    assert fake.invocation_count == 1
    resumed = engine.retry_task(workflow_id, f"{workflow_id}-PROCESS")
    assert resumed.status == WorkflowStatus.BLOCKED
    final = ApprovalRepository(db).get(resumed.pending_approval_id or "")
    assert final is not None and final.approval_type == "final_visual_review"
    assert fake.invocation_count == 1


@pytest.mark.parametrize("mutation", ["remove_collider", "scale_x10"])
def test_real_validate_handler_blocks_bad_geometry_before_runtime_and_final(
    tmp_path: Path,
    mutation: str,
) -> None:
    engine, db, workflow_id, fake, _ = _setup(
        tmp_path,
        stub_downstream=True,
        validate_real=True,
        processed_mutation=mutation,
    )
    paid_id = _at_paid_gate(engine, db, workflow_id)
    _decision(engine, db, paid_id, approve=True)
    result = engine.run_workflow(workflow_id)
    assert result.status == WorkflowStatus.FAILED
    assert fake.invocation_count == 1
    assert not any(
        a.artifact_type == "asset-runtime-observation"
        for a in ArtifactRepository(db).list_by_workflow(workflow_id)
    )
    assert not any(
        a.approval_type == "final_visual_review"
        for a in ApprovalRepository(db).list_by_workflow(workflow_id)
    )


def test_godot_failure_explicit_retry_preserves_single_provider_generation(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup(
        tmp_path,
        stub_downstream=True,
        fail_godot_once=True,
    )
    paid_id = _at_paid_gate(engine, db, workflow_id)
    _decision(engine, db, paid_id, approve=True)
    first = engine.run_workflow(workflow_id)
    assert first.status == WorkflowStatus.FAILED
    assert fake.invocation_count == 1
    assert not any(
        a.approval_type == "final_visual_review"
        for a in ApprovalRepository(db).list_by_workflow(workflow_id)
    )
    resumed = engine.retry_task(workflow_id, f"{workflow_id}-GODOT")
    assert resumed.status == WorkflowStatus.BLOCKED
    final = ApprovalRepository(db).get(resumed.pending_approval_id or "")
    assert final is not None and final.approval_type == "final_visual_review"
    assert fake.invocation_count == 1


def test_known_id_crash_recovery_queries_once_without_second_budget_liability(
    tmp_path: Path,
) -> None:
    fake = FakeAssetGenerationProvider(simulate_crash=True)
    engine, db, workflow_id, fake, _ = _setup(
        tmp_path,
        provider=fake,
        estimate=10.0,
        budget=10.0,
        stub_downstream=True,
    )
    paid_id = _at_paid_gate(engine, db, workflow_id)
    _decision(engine, db, paid_id, approve=True)
    crashed = engine.run_workflow(workflow_id)
    assert crashed.status == WorkflowStatus.BLOCKED
    assert fake.invocation_count == 1
    intent = ProviderOperationIntentRepository(db).get_by_task(f"{workflow_id}-PAID-GENERATION")
    assert intent is not None and intent.external_task_id
    fake.simulate_crash = False
    recovered = engine.run_workflow(workflow_id)
    assert recovered.status == WorkflowStatus.BLOCKED
    final = ApprovalRepository(db).get(recovered.pending_approval_id or "")
    assert final is not None and final.approval_type == "final_visual_review", (
        recovered.error_message
    )
    assert fake.invocation_count == 1
    attempts = ExecutionRepository(db).list_by_task(f"{workflow_id}-PAID-GENERATION")
    assert len(attempts) == 2
    assert sum(attempt.cost for attempt in attempts) <= 10.0
