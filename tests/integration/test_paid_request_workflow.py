"""V0.6 paid request snapshot, readiness gate, and dispatch drift protection.

Every test uses the fake provider. No real provider is contacted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import test_accounting_v06 as harness
from test_accounting_v06 import _decision, _setup_accounting_env

from gamefactory.adapters.fakes import fake_provider
from gamefactory.adapters.fakes.readiness import PassingReadinessProbes
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    CostLedgerRepository,
    PaidRequestSnapshotRepository,
    ProductionReadinessRepository,
    ProviderOperationIntentRepository,
    TaskRepository,
)
from gamefactory.core.domain.errors import PaidRequestIncompatibleError
from gamefactory.core.domain.models import Task, WorkflowStatus
from gamefactory.core.domain.paid_request import PaidRequestSnapshot
from gamefactory.workflows.production_readiness import ReadinessCheck, ReadinessContext

V06_ORDER = [
    "PREPARE",
    "CONCEPT-REVIEW",
    "PAID-REQUEST",
    "READINESS",
    "PAID-GENERATION",
    "PROCESS",
    "VALIDATE",
    "GODOT",
    "FINAL-REVIEW",
    "EVIDENCE",
]


class FailingGodotProbes(PassingReadinessProbes):
    """Readiness double reporting a missing Godot executable."""

    def evaluate(self, context: ReadinessContext) -> list[ReadinessCheck]:
        checks = super().evaluate(context)
        return [
            ReadinessCheck(
                name="godot_available",
                category="tools",
                status="FAIL",
                critical=True,
                detail="Godot executable is not configured",
                observed={"path": None},
            )
            if check.name == "godot_available"
            else check
            for check in checks
        ]


def _run_to_paid_gate(engine: Any, db: Any, workflow_id: str) -> str:
    concept = engine.run_workflow(workflow_id)
    assert concept.status == WorkflowStatus.BLOCKED
    assert concept.pending_approval_id
    _decision(engine, db, concept.pending_approval_id, approve=True)
    paid = engine.run_workflow(workflow_id)
    assert paid.status == WorkflowStatus.BLOCKED, paid.error_message
    approval = ApprovalRepository(db).get(paid.pending_approval_id or "")
    assert approval is not None and approval.approval_type == "paid_generation"
    return approval.id


def _paid_task(db: Any, workflow_id: str) -> Task:
    task = TaskRepository(db).get(f"{workflow_id}-PAID-GENERATION")
    assert task is not None
    return task


def _intents(db: Any, workflow_id: str) -> list[Any]:
    return ProviderOperationIntentRepository(db).list_by_workflow(workflow_id)


def test_v06_graph_order_and_only_paid_task_reserves_cost(tmp_path: Path) -> None:
    engine, db, workflow_id, _, _ = _setup_accounting_env(tmp_path)
    tasks = {task.id: task for task in TaskRepository(db).list_by_workflow(workflow_id)}
    ids = [f"{workflow_id}-{suffix}" for suffix in V06_ORDER]
    assert set(tasks) == set(ids)
    for previous, current in zip(ids, ids[1:], strict=False):
        assert tasks[current].depends_on == [previous]
    assert all(
        task.parameters.get("graph_version") == "0.6.0"
        for task in tasks.values()
        if task.parameters
    )
    reserving = [task_id for task_id, task in tasks.items() if "cost" in task.parameters]
    assert reserving == [f"{workflow_id}-PAID-GENERATION"]


def test_legacy_graph_still_runs_through_legacy_paid_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = harness.create_asset_production_workflow

    def legacy_graph(*args: Any, **kwargs: Any) -> Any:
        workflow, tasks = original(*args, **kwargs)
        removed = {f"{workflow.id}-PAID-REQUEST", f"{workflow.id}-READINESS"}
        legacy = [task for task in tasks if task.id not in removed]
        for task in legacy:
            task.parameters = {k: v for k, v in task.parameters.items() if k != "graph_version"}
            if task.id.endswith("-PAID-GENERATION"):
                task.depends_on = [f"{workflow.id}-CONCEPT-REVIEW"]
        return workflow, legacy

    monkeypatch.setattr(harness, "create_asset_production_workflow", legacy_graph)
    engine, db, workflow_id, fake, _ = _setup_accounting_env(tmp_path)
    assert len(TaskRepository(db).list_by_workflow(workflow_id)) == 8

    approval_id = _run_to_paid_gate(engine, db, workflow_id)
    _decision(engine, db, approval_id, approve=True)
    result = engine.run_workflow(workflow_id)

    assert result.status == WorkflowStatus.BLOCKED
    pending = ApprovalRepository(db).get(result.pending_approval_id or "")
    assert pending is not None and pending.approval_type == "final_visual_review"
    assert fake.invocation_count == 1
    assert fake.submitted_requests == []  # legacy path carries no snapshot
    assert PaidRequestSnapshotRepository(db).list_by_workflow(workflow_id) == []


def test_paid_approval_binds_snapshot_hash_and_readiness(tmp_path: Path) -> None:
    engine, db, workflow_id, _, _ = _setup_accounting_env(tmp_path)
    approval_id = _run_to_paid_gate(engine, db, workflow_id)

    approval = ApprovalRepository(db).get(approval_id)
    snapshot = PaidRequestSnapshotRepository(db).get_active_for_workflow(workflow_id)
    readiness = ProductionReadinessRepository(db).get_active_for_workflow(workflow_id)
    assert approval is not None and snapshot is not None and readiness is not None
    artifact = ArtifactRepository(db).get(snapshot.artifact_id or "")
    assert artifact is not None

    assert approval.paid_request_snapshot_hash == snapshot.snapshot_sha256
    assert artifact.content_hash == snapshot.snapshot_sha256
    assert readiness.result == "PASS"
    params = _paid_task(db, workflow_id).parameters
    assert params["paid_request_snapshot_sha256"] == snapshot.snapshot_sha256
    assert (
        PaidRequestSnapshot.from_content(params["paid_request_snapshot"]).sha256
        == snapshot.snapshot_sha256
    )
    assert params["production_readiness_report_sha256"] == readiness.report_sha256


def test_flagship_adapter_default_drift_cannot_change_the_approved_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, db, workflow_id, fake, _ = _setup_accounting_env(tmp_path)
    approval_id = _run_to_paid_gate(engine, db, workflow_id)
    approved = PaidRequestSnapshot.from_content(
        _paid_task(db, workflow_id).parameters["paid_request_snapshot"]
    )
    _decision(engine, db, approval_id, approve=True)

    # Adapter code/defaults change between human approval and dispatch.
    monkeypatch.setitem(fake_provider.FAKE_IMAGE_TO_3D_DEFAULTS, "texture_resolution", "1k")
    monkeypatch.setitem(fake_provider.FAKE_IMAGE_TO_3D_DEFAULTS, "enable_pbr", False)
    monkeypatch.setitem(fake_provider.FAKE_IMAGE_TO_3D_DEFAULTS, "model_type", "lowpoly")

    engine.run_workflow(workflow_id)

    assert fake.invocation_count == 1
    assert fake.submitted_requests == [approved.content["request"]]
    assert fake.submitted_requests[0]["texture_resolution"] == "2k"
    assert fake.submitted_requests[0]["enable_pbr"] is True
    assert fake.submitted_requests[0]["model_type"] == "smart-topology"
    intents = _intents(db, workflow_id)
    assert len(intents) == 1
    assert intents[0].request_fingerprint == approved.sha256
    assert intents[0].paid_request_snapshot_hash == approved.sha256


def test_incompatible_adapter_refuses_before_any_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, db, workflow_id, fake, _ = _setup_accounting_env(tmp_path)
    approval_id = _run_to_paid_gate(engine, db, workflow_id)
    _decision(engine, db, approval_id, approve=True)

    def reject(snapshot_content: dict[str, Any]) -> None:
        raise PaidRequestIncompatibleError(
            "adapter contract 1 is no longer supported", provider="fake"
        )

    monkeypatch.setattr(fake, "check_paid_request", reject)
    result = engine.run_workflow(workflow_id)

    assert result.status == WorkflowStatus.FAILED
    assert result.error_code == "PAID_REQUEST_INCOMPATIBLE"
    assert fake.invocation_count == 0
    assert fake.submitted_requests == []
    assert _intents(db, workflow_id) == []
    assert CostLedgerRepository(db).project_net("asset-test") == 0.0


def test_tampered_snapshot_artifact_blocks_dispatch(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, handlers = _setup_accounting_env(tmp_path)
    approval_id = _run_to_paid_gate(engine, db, workflow_id)
    _decision(engine, db, approval_id, approve=True)

    snapshot = PaidRequestSnapshotRepository(db).get_active_for_workflow(workflow_id)
    assert snapshot is not None
    artifact = ArtifactRepository(db).get(snapshot.artifact_id or "")
    assert artifact is not None
    path = handlers.root / artifact.relative_path
    path.write_text(path.read_text(encoding="utf-8").replace('"2k"', '"4k"'), encoding="utf-8")

    result = engine.run_workflow(workflow_id)

    assert result.status != WorkflowStatus.COMPLETED
    assert fake.invocation_count == 0
    assert _intents(db, workflow_id) == []


def test_missing_godot_fails_readiness_before_any_spend(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, handlers = _setup_accounting_env(tmp_path)
    handlers.readiness_probes = FailingGodotProbes()

    concept = engine.run_workflow(workflow_id)
    _decision(engine, db, concept.pending_approval_id or "", approve=True)
    result = engine.run_workflow(workflow_id)

    assert result.status == WorkflowStatus.FAILED
    assert result.error_code == "PRODUCTION_READINESS_FAILED"
    approvals = ApprovalRepository(db).list_by_workflow(workflow_id)
    assert not [a for a in approvals if a.approval_type == "paid_generation"]
    assert fake.invocation_count == 0
    assert _intents(db, workflow_id) == []
    report = ProductionReadinessRepository(db).get_active_for_workflow(workflow_id)
    assert report is not None and report.result == "FAIL"
    assert CostLedgerRepository(db).project_net("asset-test") == 0.0

    # Operator fixes the configuration; the readiness task is retryable.
    handlers.readiness_probes = PassingReadinessProbes()
    retried = engine.retry_task(workflow_id, f"{workflow_id}-READINESS")
    assert retried.status == WorkflowStatus.BLOCKED
    pending = ApprovalRepository(db).get(retried.pending_approval_id or "")
    assert pending is not None and pending.approval_type == "paid_generation"
    assert fake.invocation_count == 0


def test_exactly_one_submission_per_approved_snapshot(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup_accounting_env(tmp_path)
    approval_id = _run_to_paid_gate(engine, db, workflow_id)
    _decision(engine, db, approval_id, approve=True)
    engine.run_workflow(workflow_id)
    engine.run_workflow(workflow_id)
    engine.run_workflow(workflow_id)

    assert fake.invocation_count == 1
    intents = _intents(db, workflow_id)
    assert len(intents) == 1
    snapshot = PaidRequestSnapshotRepository(db).get_active_for_workflow(workflow_id)
    assert snapshot is not None
    assert intents[0].request_fingerprint == snapshot.snapshot_sha256


def test_material_request_change_changes_snapshot_and_approval_hash(tmp_path: Path) -> None:
    first_engine, first_db, first_id, _, _ = _setup_accounting_env(tmp_path / "a")
    first = ApprovalRepository(first_db).get(_run_to_paid_gate(first_engine, first_db, first_id))

    second_engine, second_db, second_id, _, _ = _setup_accounting_env(tmp_path / "b", estimate=25.0)
    second = ApprovalRepository(second_db).get(
        _run_to_paid_gate(second_engine, second_db, second_id)
    )

    assert first is not None and second is not None
    assert first.paid_request_snapshot_hash != second.paid_request_snapshot_hash
    assert first.operation_hash != second.operation_hash
