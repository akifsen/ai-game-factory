"""Fail-closed checks keeping V0.7 assemblies outside the paid V0.6 DAG."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    CostLedgerRepository,
    ExecutionRepository,
    PaidRequestSnapshotRecord,
    PaidRequestSnapshotRepository,
    ProductionReadinessRecord,
    ProductionReadinessRepository,
    ProviderOperationIntent,
    ProviderOperationIntentRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.accounting.ledger import EntryType, LedgerEntry
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.models import (
    ApprovalRequest,
    ApprovalStatus,
    CostClass,
    Execution,
    Task,
    Workflow,
)
from gamefactory.workflows.asset_production import (
    AssetProductionHandlers,
    _guard_paid_assembly_defense,
    create_asset_production_workflow,
)


def _task(parameters: dict[str, Any], *, task_type: str = "asset_paid_generation") -> Task:
    return Task(
        id="task-paid",
        workflow_id="workflow-1",
        name="paid request",
        task_type=task_type,
        parameters=parameters,
    )


def _v06_parameters() -> dict[str, Any]:
    return {
        "graph_version": "0.6.0",
        "asset_id": "test_crate",
        "specification_hash": "a" * 64,
        "specification": {
            "schema_version": "0.5.0",
            "profile": "static_prop",
            "profile_version": 1,
        },
    }


def _assembly_spec() -> dict[str, Any]:
    return {
        "schema_version": "0.7.0",
        "source_kind": "local_operator_assembly",
        "profile": "vehicle_test",
        "profile_version": 1,
        "parts": [{"part_id": "hull", "parent": "root"}],
        "sockets": [{"socket_id": "muzzle", "parent_part": "hull"}],
    }


class _TaskRepo:
    def __init__(self, tasks: list[Task] | None = None, error: Exception | None = None) -> None:
        self.tasks = tasks or []
        self.error = error
        self.list_calls = 0

    def list_by_workflow(self, workflow_id: str) -> list[Task]:
        self.list_calls += 1
        if self.error is not None:
            raise self.error
        return self.tasks


class _PaidSideEffectSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        def fail_if_called(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            raise AssertionError(f"paid side effect reached: {name}")

        return fail_if_called


def _handler(repo: _TaskRepo) -> AssetProductionHandlers:
    handler = object.__new__(AssetProductionHandlers)
    handler._task_repo = repo
    handler.approvals = _PaidSideEffectSpy()
    handler._snapshot_repo = _PaidSideEffectSpy()
    handler._readiness_repo = _PaidSideEffectSpy()
    handler.intents = _PaidSideEffectSpy()
    handler.ledger = _PaidSideEffectSpy()
    handler.provider = _PaidSideEffectSpy()
    handler.artifacts = _PaidSideEffectSpy()
    handler.artifact_manager = _PaidSideEffectSpy()
    return handler


@pytest.mark.parametrize(
    "entry",
    ["paid_request_snapshot", "bind_paid_request_parameters", "paid_generate"],
)
def test_paid_entry_guards_crosscheck_prepare_before_paid_lookups(entry: str) -> None:
    workflow = Workflow(id="workflow-1", project_id="project-1", name="test")
    prepare = _task(
        {**_v06_parameters(), "specification": _assembly_spec()}, task_type="asset_prepare"
    )
    prepare.id = "task-prepare"
    paid = _task(_v06_parameters())
    repo = _TaskRepo([prepare, paid])
    handler = _handler(repo)

    with pytest.raises(ValidationError, match="Assembly specification"):
        if entry == "paid_request_snapshot":
            handler.paid_request_snapshot(workflow, paid, Execution("exec-1", paid.id, 1))
        elif entry == "bind_paid_request_parameters":
            handler.bind_paid_request_parameters(workflow, paid)
        else:
            handler.paid_generate(workflow, paid, Execution("exec-1", paid.id, 1))

    assert repo.list_calls == 1
    assert handler.approvals.calls == []
    assert handler._snapshot_repo.calls == []
    assert handler._readiness_repo.calls == []
    assert handler.intents.calls == []
    assert handler.ledger.calls == []
    assert handler.provider.calls == []


@pytest.mark.parametrize(
    ("entry", "location", "json_value"),
    [
        (entry, location, value)
        for entry in ("paid_request_snapshot", "bind_paid_request_parameters", "paid_generate")
        for location in ("paid", "prepare")
        for value in ([], None, 42)
    ],
)
def test_json_non_object_specifications_fail_before_paid_lookups(
    entry: str, location: str, json_value: Any
) -> None:
    workflow = Workflow(id="workflow-1", project_id="project-1", name="test")
    paid_params = _v06_parameters()
    prepare_params = _v06_parameters()
    if location == "paid":
        paid_params["specification"] = json.dumps(json_value)
    else:
        prepare_params["specification"] = json.dumps(json_value)
    prepare = _task(prepare_params, task_type="asset_prepare")
    prepare.id = "task-prepare"
    paid = _task(paid_params)
    repo = _TaskRepo([prepare, paid])
    handler = _handler(repo)

    with pytest.raises(ValidationError, match="cannot be inspected safely"):
        if entry == "paid_request_snapshot":
            handler.paid_request_snapshot(workflow, paid, Execution("exec-1", paid.id, 1))
        elif entry == "bind_paid_request_parameters":
            handler.bind_paid_request_parameters(workflow, paid)
        else:
            handler.paid_generate(workflow, paid, Execution("exec-1", paid.id, 1))

    assert handler.approvals.calls == []
    assert handler._snapshot_repo.calls == []
    assert handler._readiness_repo.calls == []
    assert handler.intents.calls == []
    assert handler.ledger.calls == []
    assert handler.provider.calls == []


@pytest.mark.parametrize(
    "entry",
    ["paid_request_snapshot", "bind_paid_request_parameters", "paid_generate"],
)
def test_overlapping_profile_aliases_inside_paid_spec_are_rejected(entry: str) -> None:
    workflow = Workflow(id="workflow-1", project_id="project-1", name="test")
    paid_params = _v06_parameters()
    paid_params["profile_id"] = "static_prop"
    paid_params["profile_version"] = 1
    paid_params["specification"]["profile"] = "enemy_npc"
    prepare = _task(_v06_parameters(), task_type="asset_prepare")
    prepare.id = "task-prepare"
    paid = _task(paid_params)
    handler = _handler(_TaskRepo([prepare, paid]))

    with pytest.raises(ValidationError, match="conflicting profile ids"):
        if entry == "paid_request_snapshot":
            handler.paid_request_snapshot(workflow, paid, Execution("exec-1", paid.id, 1))
        elif entry == "bind_paid_request_parameters":
            handler.bind_paid_request_parameters(workflow, paid)
        else:
            handler.paid_generate(workflow, paid, Execution("exec-1", paid.id, 1))

    assert handler.approvals.calls == []
    assert handler._snapshot_repo.calls == []
    assert handler._readiness_repo.calls == []
    assert handler.intents.calls == []
    assert handler.ledger.calls == []
    assert handler.provider.calls == []


@pytest.mark.parametrize(
    "entry",
    ["paid_request_snapshot", "bind_paid_request_parameters", "paid_generate"],
)
def test_exact_profile_binding_between_paid_and_prepare_tasks(entry: str) -> None:
    workflow = Workflow(id="workflow-1", project_id="project-1", name="test")
    paid_params = _v06_parameters()
    paid_params["profile_id"] = "enemy_npc"
    paid_params["profile_version"] = 1
    paid_params["specification"]["profile"] = "enemy_npc"
    prepare_params = _v06_parameters()
    prepare_params["profile_id"] = "static_prop"
    prepare_params["profile_version"] = 1
    prepare_params["specification"]["profile"] = "static_prop"
    prepare = _task(prepare_params, task_type="asset_prepare")
    prepare.id = "task-prepare"
    paid = _task(paid_params)
    handler = _handler(_TaskRepo([prepare, paid]))

    with pytest.raises(ValidationError, match="conflict with asset_prepare"):
        if entry == "paid_request_snapshot":
            handler.paid_request_snapshot(workflow, paid, Execution("exec-1", paid.id, 1))
        elif entry == "bind_paid_request_parameters":
            handler.bind_paid_request_parameters(workflow, paid)
        else:
            handler.paid_generate(workflow, paid, Execution("exec-1", paid.id, 1))

    assert handler.approvals.calls == []
    assert handler._snapshot_repo.calls == []
    assert handler._readiness_repo.calls == []
    assert handler.intents.calls == []
    assert handler.ledger.calls == []
    assert handler.provider.calls == []


def test_profile_qualified_legacy_alias_is_normalized() -> None:
    workflow = Workflow(id="workflow-1", project_id="project-1", name="test")
    prepare_params = _v06_parameters()
    prepare_params["profile_id"] = "static_prop"
    prepare_params["profile_version"] = 1
    paid_params = _v06_parameters()
    paid_params["profile_qualified"] = "static_prop@1"
    prepare = _task(prepare_params, task_type="asset_prepare")
    prepare.id = "task-prepare"
    paid = _task(paid_params)
    _guard_paid_assembly_defense(
        "paid_generate",
        task=paid,
        workflow=workflow,
        task_repo=_TaskRepo([prepare, paid]),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("bad_spec", ["[]", "null", "42"])
def test_non_object_json_and_cyclic_python_spec_fail_closed(bad_spec: str) -> None:
    with pytest.raises(ValidationError, match="cannot be inspected safely"):
        _guard_paid_assembly_defense(
            "create_asset_production_workflow",
            spec={"specification": bad_spec},
        )

    cyclic: dict[str, Any] = {}
    cyclic["specification"] = cyclic
    with pytest.raises(ValidationError, match="cannot be inspected safely"):
        _guard_paid_assembly_defense(
            "create_asset_production_workflow",
            spec=cyclic,
        )


def test_excessively_nested_python_spec_fails_within_bounded_walk() -> None:
    nested: dict[str, Any] = {"schema_version": "0.5.0", "profile": "static_prop"}
    for _ in range(12):
        nested = {"specification": nested}
    with pytest.raises(ValidationError, match="cannot be inspected safely"):
        _guard_paid_assembly_defense("create_asset_production_workflow", spec=nested)


def test_legacy_graph_constructor_rejects_local_assembly_before_path_or_db_work() -> None:
    with pytest.raises(ValidationError, match="forbidden from paid pipeline"):
        create_asset_production_workflow(
            "project-1",
            "missing-project-root",
            _assembly_spec(),  # type: ignore[arg-type]
            "missing-concept.png",
            "missing-provenance.json",
            provider_name="fake",
        )


def test_provider_generated_v07_single_mesh_fails_closed_at_legacy_binding() -> None:
    spec = {
        "schema_version": "0.7.0",
        "source_kind": "provider_generated",
        "profile": "crate_test",
        "profile_version": 1,
        "parts": None,
        "sockets": None,
    }
    with pytest.raises(
        ValidationError, match="V0.7 provider-generated paid binding is unsupported"
    ):
        create_asset_production_workflow(
            "project-1",
            "missing-project-root",
            spec,  # type: ignore[arg-type]
            "missing-concept.png",
            "missing-provenance.json",
            provider_name="fake",
        )


def test_conflicting_outer_metadata_cannot_hide_nested_assembly() -> None:
    task = _task(
        {
            **_v06_parameters(),
            "source_kind": "provider_generated",
            "specification": _assembly_spec(),
        }
    )
    with pytest.raises(ValidationError, match="Assembly specification"):
        _guard_paid_assembly_defense("paid_generate", task=task)


def test_malformed_constructor_specification_fails_with_domain_error() -> None:
    with pytest.raises(ValidationError, match="Specification cannot be inspected safely"):
        create_asset_production_workflow(
            "project-1",
            "missing-project-root",
            {"specification": "not valid JSON"},  # type: ignore[arg-type]
            "missing-concept.png",
            "missing-provenance.json",
            provider_name="fake",
        )


def test_bound_assembly_profile_is_rejected_even_if_dump_omits_v07_marker() -> None:
    profile = SimpleNamespace(
        profile_id="vehicle_test",
        schema_version="asset-profile-0.7.0",
        geometry_mode="assembly",
        assembly=object(),
    )

    class BoundSpec:
        def model_dump(self, *, mode: str) -> dict[str, str]:
            assert mode == "json"
            return {"schema_version": "0.6.0", "profile": "vehicle_test"}

        def bound_profile(self) -> SimpleNamespace:
            return profile

    with pytest.raises(ValidationError, match="Assembly specification"):
        _guard_paid_assembly_defense("create_asset_production_workflow", spec=BoundSpec())


def test_bound_profile_assembly_contract_is_checked_without_version_or_geometry_marker() -> None:
    profile = SimpleNamespace(
        profile_id="legacy_bound_profile",
        schema_version="asset-profile-0.5.0",
        geometry_mode="single_mesh",
        assembly=object(),
    )

    class BoundSpec:
        def model_dump(self, *, mode: str) -> dict[str, str]:
            assert mode == "json"
            return {"schema_version": "0.5.0", "profile": "legacy_bound_profile"}

        def bound_profile(self) -> SimpleNamespace:
            return profile

    with pytest.raises(ValidationError, match="Assembly specification"):
        _guard_paid_assembly_defense("create_asset_production_workflow", spec=BoundSpec())


def test_guard_fails_closed_when_prepare_cannot_be_loaded() -> None:
    workflow = Workflow(id="workflow-1", project_id="project-1", name="test")
    task = _task(_v06_parameters())
    with pytest.raises(
        ValidationError, match="Unable to verify the persisted prepare specification"
    ):
        _guard_paid_assembly_defense(
            "paid_generate",
            task=task,
            workflow=workflow,
            task_repo=_TaskRepo(error=RuntimeError("database unavailable")),  # type: ignore[arg-type]
        )


def test_guard_fails_closed_when_prepare_task_is_missing_or_malformed() -> None:
    workflow = Workflow(id="workflow-1", project_id="project-1", name="test")
    task = _task(_v06_parameters())
    with pytest.raises(ValidationError, match="exactly one asset_prepare"):
        _guard_paid_assembly_defense(
            "paid_generate",
            task=task,
            workflow=workflow,
            task_repo=_TaskRepo(),  # type: ignore[arg-type]
        )

    prepare = _task(
        {**_v06_parameters(), "specification": "not valid JSON"}, task_type="asset_prepare"
    )
    prepare.id = "task-prepare"
    with pytest.raises(ValidationError, match="specification cannot be inspected safely"):
        _guard_paid_assembly_defense(
            "paid_generate",
            task=task,
            workflow=workflow,
            task_repo=_TaskRepo([prepare, task]),  # type: ignore[arg-type]
        )


def test_v06_parameters_remain_eligible_for_paid_pipeline_guards() -> None:
    workflow = Workflow(id="workflow-1", project_id="project-1", name="test")
    prepare = _task(_v06_parameters(), task_type="asset_prepare")
    prepare.id = "task-prepare"
    paid = _task(_v06_parameters())
    _guard_paid_assembly_defense(
        "paid_generate",
        task=paid,
        workflow=workflow,
        task_repo=_TaskRepo([prepare, paid]),  # type: ignore[arg-type]
    )


def test_engine_guard_stops_before_policy_or_atomic_paid_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from tests.integration.test_accounting_v06 import _setup_accounting_env

    engine, db, workflow_id, fake, _ = _setup_accounting_env(tmp_path)
    workflow = WorkflowRepository(db).get(workflow_id)
    assert workflow is not None
    task_repo = TaskRepository(db)
    tasks = task_repo.list_by_workflow(workflow_id)
    prepare = next(task for task in tasks if task.task_type == "asset_prepare")
    paid = next(task for task in tasks if task.task_type == "asset_paid_generation")

    # Keep representative historical paid-gate/accounting rows in place while
    # attempting a replay whose prepare payload has been changed to assembly.
    approvals = ApprovalRepository(db)
    historical_approval = ApprovalRequest(
        id="approval-historical",
        workflow_id=workflow_id,
        task_id=paid.id,
        approval_type="paid_generation",
        status=ApprovalStatus.PENDING,
        reason="existing operator decision",
        cost_class=CostClass.PAID,
        operation_hash="historical-operation-hash",
    )
    approvals.save(historical_approval)
    snapshots = PaidRequestSnapshotRepository(db)
    snapshots.save(
        PaidRequestSnapshotRecord(
            id="snapshot-historical",
            workflow_id=workflow_id,
            task_id=paid.id,
            asset_id=paid.parameters["asset_id"],
            revision_number=paid.parameters["revision_number"],
            concept_version=1,
            schema_version="paid-request-0.6.0",
            snapshot_sha256="a" * 64,
            canonical_json="{}",
        )
    )
    readiness = ProductionReadinessRepository(db)
    readiness.save(
        ProductionReadinessRecord(
            id="readiness-historical",
            workflow_id=workflow_id,
            task_id=paid.id,
            snapshot_sha256="a" * 64,
            result="PASS",
            schema_version="production-readiness-0.6.0",
            report_json="{}",
            report_sha256="b" * 64,
        )
    )
    intents = ProviderOperationIntentRepository(db)
    intents.save(
        ProviderOperationIntent(
            id="intent-historical",
            workflow_id=workflow_id,
            task_id=paid.id,
            asset_id=paid.parameters["asset_id"],
            revision_number=paid.parameters["revision_number"],
            provider="fake",
            operation="image-to-3d",
            concept_hash="c" * 64,
            request_fingerprint="d" * 64,
            approval_id=historical_approval.id,
        )
    )
    ledger = CostLedgerRepository(db)
    ledger.append(
        [
            LedgerEntry(
                project_id=workflow.project_id,
                workflow_id=workflow_id,
                task_id=paid.id,
                entry_type=EntryType.RESERVE,
                amount=1.0,
                reason="historical reservation",
                source="test fixture",
                actor="test fixture",
            )
        ]
    )
    before_historical = (
        approvals.list_by_workflow(workflow_id),
        snapshots.list_by_workflow(workflow_id),
        readiness.list_by_workflow(workflow_id),
        intents.list_by_workflow(workflow_id),
        ledger.list_by_workflow(workflow_id),
    )

    prepare.parameters["specification"] = _assembly_spec()
    task_repo.update_parameters(prepare.id, prepare.parameters)

    policy_calls: list[str] = []
    claim_calls: list[str] = []

    def policy_spy(*args: Any, **kwargs: Any) -> Any:
        policy_calls.append("evaluate")
        raise AssertionError("paid policy evaluation must not run")

    def claim_spy(*args: Any, **kwargs: Any) -> Any:
        claim_calls.append("claim_execution")
        raise AssertionError("paid execution claim must not run")

    monkeypatch.setattr(engine.policy_engine, "evaluate", policy_spy)
    monkeypatch.setattr(engine.task_repo, "claim_execution", claim_spy)

    with pytest.raises(ValidationError, match="Assembly specification"):
        engine._execute_task(workflow, paid)

    assert policy_calls == []
    assert claim_calls == []
    assert ExecutionRepository(db).get_latest_attempt(paid.id) is None
    after_historical = (
        approvals.list_by_workflow(workflow_id),
        snapshots.list_by_workflow(workflow_id),
        readiness.list_by_workflow(workflow_id),
        intents.list_by_workflow(workflow_id),
        ledger.list_by_workflow(workflow_id),
    )
    assert after_historical == before_historical
    assert fake.invocation_count == 0
