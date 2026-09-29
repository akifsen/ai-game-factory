"""Unit tests for paid request adapters, drift isolation, incompatibility checks, and provider execution."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import gamefactory.adapters.external.meshy_cli as meshy_module
from gamefactory.adapters.external.meshy_cli import (
    MeshyAssetGenerationProvider,
    MeshyCliRunner,
    build_create_args,
)
from gamefactory.adapters.external.meshy_cli import (
    check_paid_request as meshy_check_paid_request,
)
from gamefactory.adapters.external.meshy_cli import (
    resolve_paid_request as meshy_resolve_paid_request,
)
from gamefactory.adapters.fakes.fake_provider import (
    FakeAssetGenerationProvider,
)
from gamefactory.adapters.fakes.fake_provider import (
    check_paid_request as fake_check_paid_request,
)
from gamefactory.adapters.fakes.fake_provider import (
    resolve_paid_request as fake_resolve_paid_request,
)
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    ProjectRepository,
    ProviderOperationIntent,
    ProviderOperationIntentRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.approvals.operation_scope import build_operation_inputs
from gamefactory.core.domain.asset_contracts import AssetRevision
from gamefactory.core.domain.errors import (
    PaidRequestIncompatibleError,
    PaidRequestRequiredError,
)
from gamefactory.core.domain.models import (
    Artifact,
    CostClass,
    Project,
    Task,
    Workflow,
)
from gamefactory.core.domain.paid_request import (
    PaidRequestSnapshot,
)
from gamefactory.core.execution.process_runner import CommandResult, ProcessRunner
from gamefactory.workflows.ports import GenerationRequest


def _sample_binding(
    asset_id: str = "prop_crate_01",
    revision_number: int = 1,
    concept_sha256: str = "a" * 64,
    specification_sha256: str = "b" * 64,
) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "revision_number": revision_number,
        "concept_version": 1,
        "concept_sha256": concept_sha256,
        "specification_sha256": specification_sha256,
        "profile_id": "static_prop",
        "profile_version": 1,
    }


def _sample_spec(max_triangles_lod0: int = 10000) -> dict[str, Any]:
    return {
        "geometry_budget": {
            "max_triangles_lod0": max_triangles_lod0,
        }
    }


def _sample_cost(estimate: float = 4.0, reservation: float = 4.0) -> dict[str, Any]:
    return {
        "estimate": estimate,
        "reservation": reservation,
        "unit": "credits",
    }


def test_meshy_build_create_args_exact_flags() -> None:
    binding = _sample_binding()
    spec = _sample_spec(10000)
    cost = _sample_cost()
    snapshot_content = meshy_resolve_paid_request(binding, spec, cost)

    args = build_create_args(snapshot_content)
    expected = [
        "--model-type",
        "smart-topology",
        "--target-polycount",
        "10000",
        "--should-texture",
        "true",
        "--enable-pbr",
        "true",
        "--texture-resolution",
        "2k",
        "--target-formats",
        "glb",
    ]
    assert args == expected


def test_meshy_build_create_args_drift_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    binding = _sample_binding()
    spec = _sample_spec(12000)
    cost = _sample_cost()
    snapshot_content = meshy_resolve_paid_request(binding, spec, cost)
    snapshot = PaidRequestSnapshot.from_content(snapshot_content)

    # Now mutate runtime defaults and polycount limit in meshy_module
    monkeypatch.setitem(meshy_module.MESHY_IMAGE_TO_3D_DEFAULTS, "texture_resolution", "1k")
    monkeypatch.setitem(meshy_module.MESHY_IMAGE_TO_3D_DEFAULTS, "model_type", "lowpoly")
    monkeypatch.setattr(meshy_module, "MESHY_MAX_TARGET_POLYCOUNT", 5000)

    # build_create_args MUST obey the approved snapshot, completely isolated from runtime defaults
    args = build_create_args(snapshot.content)
    assert "--texture-resolution" in args
    idx_res = args.index("--texture-resolution")
    assert args[idx_res + 1] == "2k"

    assert "--model-type" in args
    idx_model = args.index("--model-type")
    assert args[idx_model + 1] == "smart-topology"

    assert "--target-polycount" in args
    idx_poly = args.index("--target-polycount")
    assert args[idx_poly + 1] == "12000"


@pytest.mark.parametrize(
    "mutator, expected_err",
    [
        (lambda c: c.update({"schema": "paid-request-0.5.0"}), PaidRequestIncompatibleError),
        (lambda c: c.update({"provider": "not-meshy"}), PaidRequestIncompatibleError),
        (lambda c: c.update({"operation": "text-to-3d"}), PaidRequestIncompatibleError),
        (lambda c: c["adapter"].update({"id": "other-adapter"}), PaidRequestIncompatibleError),
        (lambda c: c["adapter"].update({"contract_version": 2}), PaidRequestIncompatibleError),
        (lambda c: c["request"].update({"texture_resolution": "4k"}), PaidRequestIncompatibleError),
        (lambda c: c["request"].update({"model_type": "remesh"}), PaidRequestIncompatibleError),
        (lambda c: c["request"].update({"target_formats": ["obj"]}), PaidRequestIncompatibleError),
        (lambda c: c["request"].update({"target_polycount": 50}), PaidRequestIncompatibleError),
        (lambda c: c["request"].update({"target_polycount": 25000}), PaidRequestIncompatibleError),
        (lambda c: c["request"].update({"remove_lighting": True}), PaidRequestIncompatibleError),
        (lambda c: c["request"].update({"rig": True}), PaidRequestIncompatibleError),
        (lambda c: c["request"].update({"animate": True}), PaidRequestIncompatibleError),
        (lambda c: c["request"].update({"variants": 2}), PaidRequestIncompatibleError),
    ],
)
def test_meshy_check_paid_request_incompatibilities(
    mutator: Any, expected_err: type[Exception]
) -> None:
    binding = _sample_binding()
    spec = _sample_spec(10000)
    cost = _sample_cost()
    snapshot_content = meshy_resolve_paid_request(binding, spec, cost)
    mutator(snapshot_content)

    with pytest.raises(expected_err):
        meshy_check_paid_request(snapshot_content)


def _setup_full_meshy_env(
    tmp_path: Path,
    polycount: int = 10000,
    cost: float = 4.0,
) -> tuple[Database, Path, str, str, PaidRequestSnapshot]:
    db = Database(tmp_path / "meshy_adapter_test.db")
    MigrationRunner(db).apply_all()

    proj_repo = ProjectRepository(db)
    proj_repo.save(Project(id="p-01", name="P01", engine_type="godot", root_path=str(tmp_path)))
    wf_repo = WorkflowRepository(db)
    wf_repo.save(Workflow(id="wf-01", project_id="p-01", name="WF01"))

    spec_dict = _sample_spec(polycount)
    spec_bytes = str(spec_dict).encode("utf-8")
    spec_hash = hashlib.sha256(spec_bytes).hexdigest()

    task_repo = TaskRepository(db)
    task_repo.save(
        Task(
            id="task-01",
            workflow_id="wf-01",
            name="Task01",
            task_type="asset_paid_generation",
            cost_class=CostClass.PAID,
            parameters={
                "provider": "meshy",
                "asset_id": "prop_crate_01",
                "revision_number": 1,
                "target_polycount": polycount,
                "cost": cost,
                "spec_hash": spec_hash,
                "specification_hash": spec_hash,
            },
        )
    )

    concept_dir = tmp_path / "concepts"
    concept_dir.mkdir(parents=True, exist_ok=True)
    concept_file = concept_dir / "prop_crate_01.png"
    concept_bytes = b"sample_concept_png"
    concept_file.write_bytes(concept_bytes)
    concept_hash = hashlib.sha256(concept_bytes).hexdigest()

    art_repo = ArtifactRepository(db)
    art_repo.save(
        Artifact(
            id="art-concept-01",
            workflow_id="wf-01",
            task_id="task-01",
            artifact_type="asset-concept",
            producer="concept_generator",
            relative_path="concepts/prop_crate_01.png",
            content_hash=concept_hash,
            file_size=len(concept_bytes),
            validation_state="VALID",
        )
    )

    rev_repo = AssetRevisionRepository(db)
    rev_repo.save(
        AssetRevision(
            asset_id="prop_crate_01",
            revision_number=1,
            workflow_id="wf-01",
            spec_hash=spec_hash,
            concept_hash=concept_hash,
        )
    )

    workflow = wf_repo.get("wf-01")
    task = task_repo.get("task-01")
    assert workflow is not None and task is not None
    artifacts = art_repo.list_by_workflow("wf-01")
    op_inputs = build_operation_inputs(
        workflow=workflow,
        task=task,
        artifacts=artifacts,
        cost_class=CostClass.PAID,
    )
    approval = ApprovalService.create_request(
        workflow_id="wf-01",
        task_id="task-01",
        approval_type="paid_generation",
        reason="Approved for generation",
        cost_class=CostClass.PAID,
        operation_inputs=op_inputs,
    )
    approval.id = "app-01"
    ApprovalService.approve(approval, actor="operator", current_inputs=op_inputs)
    ApprovalRepository(db).save(approval)

    binding = _sample_binding(
        asset_id="prop_crate_01",
        revision_number=1,
        concept_sha256=concept_hash,
        specification_sha256=spec_hash,
    )
    cost_dict = _sample_cost(cost, cost)
    snap_content = meshy_resolve_paid_request(binding, spec_dict, cost_dict)
    snapshot = PaidRequestSnapshot.from_content(snap_content)

    return db, concept_file, concept_hash, approval.operation_hash, snapshot


def test_meshy_generate_incompatible_snapshot_aborts_without_runner_or_intent(
    tmp_path: Path,
) -> None:
    db, concept_file, concept_hash, op_hash, snapshot = _setup_full_meshy_env(tmp_path)

    # Alter snapshot content to be incompatible
    bad_content = copy.deepcopy(snapshot.content)
    bad_content["request"]["texture_resolution"] = "4k"
    bad_snapshot = PaidRequestSnapshot.from_content(bad_content)

    mock_runner = MagicMock(spec=ProcessRunner)
    runner = MeshyCliRunner(runner=mock_runner)
    runner._cached_runner_cmd = ["meshy"]
    intent_repo = ProviderOperationIntentRepository(db)
    provider = MeshyAssetGenerationProvider(
        cli_runner=runner, intent_repo=intent_repo, allow_paid_calls=True
    )

    req = GenerationRequest(
        prompt="A sci-fi crate",
        operation_hash=op_hash,
        parameters={
            "task_id": "task-01",
            "asset_id": "prop_crate_01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "revision_number": 1,
            "concept_image_path": str(concept_file),
            "concept_hash": concept_hash,
            "target_polycount": 10000,
            "cost": 4.0,
            "paid_request_snapshot_sha256": bad_snapshot.sha256,
        },
        paid_request=bad_snapshot,
    )

    with pytest.raises(PaidRequestIncompatibleError):
        provider.generate(req)

    # Zero process executions made
    assert mock_runner.run.call_count == 0

    # Zero intents registered
    assert intent_repo.get_by_task("task-01") is None


def test_meshy_generate_valid_snapshot_execution(tmp_path: Path) -> None:
    db, concept_file, concept_hash, op_hash, snapshot = _setup_full_meshy_env(tmp_path)

    mock_runner = MagicMock(spec=ProcessRunner)
    create_stdout = (
        '{"schema_version": "meshy.cli/v1", "command": "image-to-3d create", "ok": true, '
        '"result": {"submission": {"task_id": "meshy-op-123", "status": "PENDING"}}}'
    )
    mock_runner.run.return_value = CommandResult(
        exit_code=0, stdout=create_stdout, stderr="", duration_seconds=0.1
    )

    runner = MeshyCliRunner(runner=mock_runner)
    runner._cached_runner_cmd = ["meshy"]
    intent_repo = ProviderOperationIntentRepository(db)
    provider = MeshyAssetGenerationProvider(
        cli_runner=runner, intent_repo=intent_repo, allow_paid_calls=True
    )

    req = GenerationRequest(
        prompt="A sci-fi crate",
        operation_hash=op_hash,
        parameters={
            "task_id": "task-01",
            "asset_id": "prop_crate_01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "revision_number": 1,
            "concept_image_path": str(concept_file),
            "concept_hash": concept_hash,
            "target_polycount": 10000,
            "cost": 4.0,
            "paid_request_snapshot_sha256": snapshot.sha256,
        },
        paid_request=snapshot,
    )

    res = provider.generate(req)
    assert res.status == "SUBMITTED"
    assert res.external_op_id == "meshy-op-123"

    # Verify create command arguments came directly from snapshot
    create_call = mock_runner.run.call_args_list[0]
    create_cmd = create_call.args[0].args
    assert "--model-type" in create_cmd
    assert "--target-polycount" in create_cmd
    poly_idx = create_cmd.index("--target-polycount")
    assert create_cmd[poly_idx + 1] == "10000"

    # Verify intent saved has snapshot hash as fingerprint and paid_request_snapshot_hash
    intent = intent_repo.get_by_task("task-01")
    assert intent is not None
    assert intent.request_fingerprint == snapshot.sha256
    assert intent.paid_request_snapshot_hash == snapshot.sha256


def test_meshy_generate_missing_snapshot_raises_required_error(tmp_path: Path) -> None:
    db, concept_file, concept_hash, op_hash, _ = _setup_full_meshy_env(tmp_path)

    mock_runner = MagicMock(spec=ProcessRunner)
    runner = MeshyCliRunner(runner=mock_runner)
    runner._cached_runner_cmd = ["meshy"]
    intent_repo = ProviderOperationIntentRepository(db)
    provider = MeshyAssetGenerationProvider(
        cli_runner=runner, intent_repo=intent_repo, allow_paid_calls=True
    )

    req = GenerationRequest(
        prompt="A sci-fi crate",
        operation_hash=op_hash,
        parameters={
            "task_id": "task-01",
            "asset_id": "prop_crate_01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "revision_number": 1,
            "concept_image_path": str(concept_file),
            "concept_hash": concept_hash,
            "target_polycount": 10000,
            "cost": 4.0,
        },
        paid_request=None,
    )

    with pytest.raises(PaidRequestRequiredError) as exc_info:
        provider.generate(req)
    assert exc_info.value.code == "PAID_REQUEST_REQUIRED"

    assert mock_runner.run.call_count == 0
    assert intent_repo.get_by_task("task-01") is None


def test_meshy_generate_existing_intent_query_without_snapshot_succeeds(tmp_path: Path) -> None:
    db, concept_file, concept_hash, op_hash, _ = _setup_full_meshy_env(tmp_path)

    # Seed an existing intent for recovery
    intent_repo = ProviderOperationIntentRepository(db)
    intent = ProviderOperationIntent(
        id="intent-legacy-01",
        workflow_id="wf-01",
        task_id="task-01",
        asset_id="prop_crate_01",
        revision_number=1,
        provider="meshy",
        operation="image-to-3d",
        concept_hash=concept_hash,
        request_fingerprint=op_hash,
        approval_id="app-01",
        estimated_cost=4.0,
        actual_cost=None,
        cost_unit="credits",
        external_task_id="ext-legacy-123",
        status="PENDING",
        created_at="2026-09-29T10:00:00Z",
        updated_at="2026-09-29T10:00:00Z",
        paid_request_snapshot_hash=None,
    )
    intent_repo.save(intent)

    mock_runner = MagicMock(spec=ProcessRunner)
    # Check command response for existing intent (via get_task)
    check_stdout = (
        '{"schema_version": "meshy.cli/v1", "result": {"task": {'
        '"task_id": "ext-legacy-123", "status": "SUCCEEDED", "progress": 100, "consumed_credits": 4.0}}}'
    )
    mock_runner.run.return_value = CommandResult(
        exit_code=0, stdout=check_stdout, stderr="", duration_seconds=0.1
    )

    runner = MeshyCliRunner(runner=mock_runner)
    runner._cached_runner_cmd = ["meshy"]
    provider = MeshyAssetGenerationProvider(
        cli_runner=runner, intent_repo=intent_repo, allow_paid_calls=True
    )

    req = GenerationRequest(
        prompt="A sci-fi crate",
        operation_hash=op_hash,
        parameters={
            "task_id": "task-01",
            "asset_id": "prop_crate_01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "revision_number": 1,
            "concept_image_path": str(concept_file),
            "concept_hash": concept_hash,
            "target_polycount": 10000,
            "cost": 4.0,
        },
        paid_request=None,
    )

    res = provider.generate(req)
    assert res.status == "SUCCESS"
    assert res.external_op_id == "ext-legacy-123"


def test_fake_provider_paid_request_adapter() -> None:
    binding = _sample_binding()
    spec = _sample_spec(8000)
    cost = _sample_cost(5.0, 5.0)

    resolved = fake_resolve_paid_request(binding, spec, cost)
    assert resolved["provider"] == "fake"
    assert resolved["adapter"]["id"] == "fake-image-to-3d"
    assert resolved["request"]["target_polycount"] == 8000

    fake_check_paid_request(resolved)

    snapshot = PaidRequestSnapshot.from_content(resolved)

    fake_prov = FakeAssetGenerationProvider()
    assert fake_prov.submitted_requests == []

    req = GenerationRequest(
        prompt="Sci-fi crate",
        parameters={
            "task_id": "task-fake-01",
            "asset_id": "prop_crate_01",
            "workflow_id": "wf-fake",
            "approval_id": "app-fake",
            "revision_number": 1,
        },
        paid_request=snapshot,
    )

    res = fake_prov.generate(req)
    assert res.status == "SUCCESS"
    assert len(fake_prov.submitted_requests) == 1
    assert fake_prov.submitted_requests[0]["target_polycount"] == 8000

    # Also check legacy path with paid_request=None works
    req_legacy = GenerationRequest(
        prompt="Sci-fi crate 2",
        parameters={
            "task_id": "task-fake-02",
            "asset_id": "prop_crate_02",
            "workflow_id": "wf-fake",
            "approval_id": "app-fake",
            "revision_number": 1,
        },
        paid_request=None,
    )
    res_legacy = fake_prov.generate(req_legacy)
    assert res_legacy.status == "SUCCESS"
    # submitted_requests was not updated on legacy request
    assert len(fake_prov.submitted_requests) == 1
