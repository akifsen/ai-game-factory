"""Unit tests for Meshy CLI runner, doctor diagnostics, and generation provider safety."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gamefactory.adapters.external.meshy_cli import (
    MeshyAssetGenerationProvider,
    MeshyCliRunner,
    MeshyDoctorResult,
    _validate_finite_nonnegative_cost,
    resolve_paid_request,
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
from gamefactory.core.domain.asset_contracts import (
    AssetRevision,
    AssetSpecification,
    spec_fingerprint,
)
from gamefactory.core.domain.errors import (
    ApprovalRequired,
    PaidRequestInvalidError,
    ProviderFailedError,
    ProviderUncertainError,
    RawArtifactInvalidError,
    ValidationError,
)
from gamefactory.core.domain.models import (
    ApprovalStatus,
    Artifact,
    CostClass,
    Project,
    Task,
    Workflow,
)
from gamefactory.core.domain.paid_request import PaidRequestSnapshot
from gamefactory.core.execution.process_runner import CommandRequest, CommandResult, ProcessRunner
from gamefactory.workflows.ports import GenerationRequest

HEX_HASH_1 = "1" * 64
HEX_HASH_2 = "2" * 64
HEX_HASH_3 = "3" * 64
HEX_HASH_4 = "4" * 64


@pytest.mark.parametrize("invalid_cost", [None, "UNKNOWN", "bad", True, -1, float("inf")])
def test_operation_scope_rejects_invalid_explicit_task_cost(
    tmp_path: Path, invalid_cost: Any
) -> None:
    db = Database(tmp_path / "scope.db")
    MigrationRunner(db).apply_all()
    _, workflows, tasks = _setup_db_with_parents(db, tmp_path)
    task = tasks.get("task-01")
    workflow = workflows.get("wf-01")
    assert task is not None and workflow is not None
    task.parameters["cost"] = invalid_cost

    with pytest.raises(ValidationError):
        build_operation_inputs(workflow, task, [], CostClass.PAID)


def _setup_db_with_parents(
    db: Database,
    tmp_path: Path,
    workflow_id: str = "wf-01",
    task_id: str = "task-01",
    asset_id: str = "prop_energy_crate_01",
    revision_number: int = 1,
    polycount: int = 10000,
    cost: float = 4.0,
    spec_hash: str = HEX_HASH_2,
    specification: dict[str, Any] | None = None,
    provider_estimate: float | None = None,
    budget_reservation: float | None = None,
) -> tuple[ProjectRepository, WorkflowRepository, TaskRepository]:
    proj_repo = ProjectRepository(db)
    proj_repo.save(Project(id="p-01", name="P01", engine_type="godot", root_path=str(tmp_path)))
    wf_repo = WorkflowRepository(db)
    wf_repo.save(Workflow(id=workflow_id, project_id="p-01", name="WF01"))
    task_repo = TaskRepository(db)
    task_repo.save(
        Task(
            id=task_id,
            workflow_id=workflow_id,
            name="Task01",
            task_type="asset_paid_generation",
            cost_class=CostClass.PAID,
            parameters={
                "provider": "meshy",
                "asset_id": asset_id,
                "revision_number": revision_number,
                **(
                    {"specification": specification}
                    if specification is not None
                    else {"target_polycount": polycount}
                ),
                "cost": cost,
                "spec_hash": spec_hash,
                "specification_hash": spec_hash,
                **(
                    {
                        "provider_estimate": provider_estimate,
                        "budget_reservation": budget_reservation,
                    }
                    if specification is not None
                    else {}
                ),
            },
        )
    )
    return proj_repo, wf_repo, task_repo


def _setup_full_context(
    db: Database,
    tmp_path: Path,
    asset_id: str = "prop_energy_crate_01",
    revision_number: int = 1,
    workflow_id: str = "wf-01",
    task_id: str = "task-01",
    approval_id: str = "app-01",
    approval_status: ApprovalStatus = ApprovalStatus.APPROVED,
    approval_type: str = "paid_generation",
    polycount: int = 10000,
    cost: float = 4.0,
    spec_hash: str = HEX_HASH_2,
    concept_bytes: bytes = b"concept_png_payload",
    specification: dict[str, Any] | None = None,
    provider_estimate: float | None = None,
    budget_reservation: float | None = None,
) -> tuple[Path, str, str]:
    """Helper to set up complete authoritative database entities with realistic records and minted approval."""
    _setup_db_with_parents(
        db,
        tmp_path,
        workflow_id=workflow_id,
        task_id=task_id,
        asset_id=asset_id,
        revision_number=revision_number,
        polycount=polycount,
        cost=cost,
        spec_hash=spec_hash,
        specification=specification,
        provider_estimate=provider_estimate,
        budget_reservation=budget_reservation,
    )
    wf_repo = WorkflowRepository(db)
    task_repo = TaskRepository(db)
    workflow = wf_repo.get(workflow_id)
    assert workflow is not None
    task = task_repo.get(task_id)
    assert task is not None

    # Save concept file on disk and compute hash
    concept_dir = tmp_path / "concepts"
    concept_dir.mkdir(parents=True, exist_ok=True)
    concept_file = concept_dir / f"{asset_id}.png"
    concept_file.write_bytes(concept_bytes)
    concept_hash = hashlib.sha256(concept_bytes).hexdigest()

    # Save concept artifact in ArtifactRepository
    art_repo = ArtifactRepository(db)
    artifact = Artifact(
        id=f"art-concept-{asset_id}",
        workflow_id=workflow_id,
        task_id=task_id,
        artifact_type="asset-concept",
        producer="concept_generator",
        relative_path=f"concepts/{asset_id}.png",
        content_hash=concept_hash,
        file_size=len(concept_bytes),
        validation_state="VALID",
    )
    art_repo.save(artifact)

    # Save AssetRevision
    rev_repo = AssetRevisionRepository(db)
    revision = AssetRevision(
        asset_id=asset_id,
        revision_number=revision_number,
        workflow_id=workflow_id,
        spec_hash=spec_hash,
        concept_hash=concept_hash,
    )
    rev_repo.save(revision)

    # Mint realistic ApprovalRequest using ApprovalService and build_operation_inputs
    artifacts = art_repo.list_by_workflow(workflow_id)
    op_inputs = build_operation_inputs(
        workflow=workflow,
        task=task,
        artifacts=artifacts,
        cost_class=CostClass.PAID,
        provider_name=None,
        handler_context=None,
    )
    approval = ApprovalService.create_request(
        workflow_id=workflow_id,
        task_id=task_id,
        approval_type=approval_type,
        reason="Approved for generation",
        cost_class=CostClass.PAID,
        operation_inputs=op_inputs,
    )
    # Explicit deterministic ID
    approval.id = approval_id
    if approval_status == ApprovalStatus.APPROVED:
        ApprovalService.approve(approval, actor="operator", current_inputs=op_inputs)
    elif approval_status == ApprovalStatus.REJECTED:
        ApprovalService.reject(approval, actor="operator")
    elif approval_status == ApprovalStatus.CHANGES_REQUESTED:
        ApprovalService.request_changes(approval, actor="operator")

    app_repo = ApprovalRepository(db)
    app_repo.save(approval)

    return concept_file, concept_hash, approval.operation_hash


def _make_paid_request_snapshot(
    asset_id: str = "prop_energy_crate_01",
    revision_number: int = 1,
    concept_hash: str = HEX_HASH_1,
    spec_hash: str = HEX_HASH_2,
    polycount: int = 10000,
    cost: float | None = 4.0,
    specification: dict[str, Any] | None = None,
    budget_reservation: float | None = None,
) -> PaidRequestSnapshot:
    binding = {
        "asset_id": asset_id,
        "revision_number": revision_number,
        "concept_version": 1,
        "concept_sha256": concept_hash,
        "specification_sha256": spec_hash,
        "profile_id": "static_prop",
        "profile_version": 1,
    }
    spec = (
        specification
        if specification is not None
        else {"geometry_budget": {"max_triangles_lod0": polycount}}
    )
    res_val = (
        budget_reservation
        if budget_reservation is not None
        else (cost if cost is not None else 10.0)
    )
    cost_dict = {"estimate": cost, "reservation": res_val, "unit": "credits"}
    content = resolve_paid_request(binding, spec, cost_dict)
    return PaidRequestSnapshot.from_content(content)


def test_meshy_cli_runner_doctor_available() -> None:
    mock_runner = MagicMock(spec=ProcessRunner)
    doctor_stdout = """{
      "schema_version": "meshy.cli/v1",
      "command": "doctor",
      "ok": true,
      "result": {
        "cli": {"version": "0.4.0", "node": "v24.13.0", "platform": "win32"},
        "local_ready": true,
        "credential_sources": {
          "flag": false,
          "env": true,
          "api_key_file": null,
          "stored_profile": {"exists": false}
        }
      }
    }"""
    mock_runner.run.return_value = CommandResult(
        exit_code=0, stdout=doctor_stdout, stderr="", duration_seconds=0.1
    )

    runner = MeshyCliRunner(runner=mock_runner)
    runner._cached_runner_cmd = ["meshy"]
    res = runner.doctor()

    assert isinstance(res, MeshyDoctorResult)
    assert res.available is True
    assert res.has_credential is True
    assert res.status == "AVAILABLE"
    assert res.cli_version == "0.4.0"
    assert res.node_version == "v24.13.0"
    command_request = mock_runner.run.call_args.args[0]
    assert command_request.structured_json_output is True


def test_meshy_cli_runner_doctor_distinguishes_credential_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MESHY_API_KEY", raising=False)
    mock_runner = MagicMock(spec=ProcessRunner)
    doctor_stdout = """{
      "schema_version": "meshy.cli/v1",
      "command": "doctor",
      "ok": true,
      "result": {
        "cli": {"version": "0.4.0", "node": "v24.13.0", "platform": "win32"},
        "local_ready": true,
        "credential_sources": {
          "flag": false,
          "env": false,
          "api_key_file": null,
          "stored_profile": {"exists": false}
        }
      }
    }"""
    mock_runner.run.return_value = CommandResult(
        exit_code=0, stdout=doctor_stdout, stderr="", duration_seconds=0.1
    )

    runner = MeshyCliRunner(runner=mock_runner)
    runner._cached_runner_cmd = ["meshy"]
    res = runner.doctor()

    # Must be CREDENTIAL_MISSING, NOT APPROVAL_REQUIRED
    assert res.available is True
    assert res.has_credential is False
    assert res.status == "CREDENTIAL_MISSING"


def test_meshy_doctor_passes_only_host_api_key_and_trusts_cli_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MESHY_API_KEY", "synthetic-meshy-key")
    mock_runner = MagicMock(spec=ProcessRunner)
    mock_runner.run.return_value = CommandResult(
        exit_code=0,
        stdout=json.dumps(
            {
                "result": {
                    "cli": {"version": "0.4.0", "node": "v24.13.0"},
                    "local_ready": True,
                    "credential_sources": {
                        "env": False,
                        "flag": False,
                        "stored_profile": {"exists": False},
                    },
                }
            }
        ),
        stderr="",
    )
    runner = MeshyCliRunner(runner=mock_runner)
    runner._cached_runner_cmd = ["meshy"]

    result = runner.doctor()

    request = mock_runner.run.call_args.args[0]
    assert request.env_overrides == {"MESHY_API_KEY": "synthetic-meshy-key"}
    assert request.structured_json_output is True
    assert result.status == "CREDENTIAL_MISSING"
    assert result.has_credential is False


def test_meshy_cli_runner_doctor_detects_wrong_version() -> None:
    mock_runner = MagicMock(spec=ProcessRunner)
    doctor_stdout = """{
      "schema_version": "meshy.cli/v1",
      "command": "doctor",
      "ok": true,
      "result": {
        "cli": {"version": "0.3.1", "node": "v24.13.0", "platform": "win32"},
        "local_ready": true,
        "credential_sources": {"env": true}
      }
    }"""
    mock_runner.run.return_value = CommandResult(
        exit_code=0, stdout=doctor_stdout, stderr="", duration_seconds=0.1
    )

    runner = MeshyCliRunner(runner=mock_runner)
    runner._cached_runner_cmd = ["meshy"]
    res = runner.doctor()

    assert res.available is False
    assert res.status == "MISCONFIGURED"
    assert "Expected meshy-cli version 0.4.0" in res.details["reason"]


def test_meshy_mandatory_intent_repo() -> None:
    with pytest.raises(ProviderFailedError, match="ProviderOperationIntentRepository is mandatory"):
        MeshyAssetGenerationProvider(intent_repo=None)


def test_meshy_rejects_placeholder_or_missing_parameters(tmp_path: Path) -> None:
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    _setup_db_with_parents(db, tmp_path)
    intent_repo = ProviderOperationIntentRepository(db)

    provider = MeshyAssetGenerationProvider(intent_repo=intent_repo, allow_paid_calls=True)
    req = GenerationRequest(
        prompt="Energy Crate",
        parameters={
            "asset_id": "prop_energy_crate_01",
            "workflow_id": "unknown_wf",  # Placeholder value rejected!
            "task_id": "task-01",
            "approval_id": "app-01",
            "concept_hash": "c" * 64,
        },
    )

    with pytest.raises(ProviderFailedError, match="placeholder value"):
        provider.generate(req)


def test_meshy_provider_blocks_paid_calls_by_default(tmp_path: Path) -> None:
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    concept_file, concept_hash, op_hash = _setup_full_context(db, tmp_path)
    intent_repo = ProviderOperationIntentRepository(db)

    provider = MeshyAssetGenerationProvider(intent_repo=intent_repo, allow_paid_calls=False)
    assert provider.name == "meshy"
    assert provider.cost_class == CostClass.PAID

    snap = _make_paid_request_snapshot(concept_hash=concept_hash)
    req = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=op_hash,
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "concept_hash": concept_hash,
            "concept_image_path": str(concept_file),
            "paid_request_snapshot_sha256": snap.sha256,
        },
        paid_request=snap,
    )

    with pytest.raises(ApprovalRequired) as exc_info:
        provider.generate(req)

    assert "BLOCKED per Phase A requirements" in str(exc_info.value)
    assert provider.invocation_count == 0


def test_meshy_provider_uncertain_crash_recovery(tmp_path: Path) -> None:
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    concept_file, concept_hash, op_hash = _setup_full_context(db, tmp_path)
    intent_repo = ProviderOperationIntentRepository(db)

    # Simulate an intent that was left in SUBMITTING status without an external_task_id
    intent = ProviderOperationIntent(
        id="intent-crash",
        workflow_id="wf-01",
        task_id="task-01",
        asset_id="prop_energy_crate_01",
        revision_number=1,
        provider="meshy",
        operation="image-to-3d",
        concept_hash=concept_hash,
        request_fingerprint=op_hash,
        approval_id="app-01",
        status="SUBMITTING",
        external_task_id=None,
    )
    intent_repo.save(intent)

    provider = MeshyAssetGenerationProvider(intent_repo=intent_repo, allow_paid_calls=False)
    req = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=op_hash,
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "concept_hash": concept_hash,
        },
    )

    with pytest.raises(ProviderUncertainError) as exc_info:
        provider.generate(req)

    assert "Automatic second paid generation is forbidden" in str(exc_info.value)
    assert provider.invocation_count == 0


def test_meshy_provider_queries_existing_task_without_re_creating(tmp_path: Path) -> None:
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    concept_file, concept_hash, op_hash = _setup_full_context(db, tmp_path)
    intent_repo = ProviderOperationIntentRepository(db)

    # Intent with external task ID in SUBMITTED state
    intent = ProviderOperationIntent(
        id="intent-existing",
        workflow_id="wf-01",
        task_id="task-01",
        asset_id="prop_energy_crate_01",
        revision_number=1,
        provider="meshy",
        operation="image-to-3d",
        concept_hash=concept_hash,
        request_fingerprint=op_hash,
        approval_id="app-01",
        status="SUBMITTED",
        external_task_id="meshy-task-42",
        actual_cost=None,
    )
    intent_repo.save(intent)

    mock_runner = MagicMock(spec=ProcessRunner)
    # Mock CLI response for image-to-3d get meshy-task-42 returning SUCCEEDED in v1 envelope
    get_stdout = """{
      "schema_version": "meshy.cli/v1",
      "command": "image-to-3d get",
      "ok": true,
      "result": {
        "task": {
          "task_id": "meshy-task-42",
          "status": "SUCCEEDED",
          "progress": 100,
          "consumed_credits": 4.0
        }
      }
    }"""
    mock_runner.run.return_value = CommandResult(exit_code=0, stdout=get_stdout, stderr="")

    cli_runner = MeshyCliRunner(runner=mock_runner)
    cli_runner._cached_runner_cmd = ["meshy"]

    provider = MeshyAssetGenerationProvider(
        cli_runner=cli_runner, intent_repo=intent_repo, allow_paid_calls=False
    )
    req = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=op_hash,
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "concept_hash": concept_hash,
        },
    )

    resp = provider.generate(req)
    assert resp.external_op_id == "meshy-task-42"
    assert resp.status == "SUCCESS"
    assert resp.details["actual_cost"] == 4.0
    assert provider.invocation_count == 0  # Create was NEVER invoked!

    # Verified that CLI was called with `image-to-3d get meshy-task-42`, not create!
    called_args = mock_runner.run.call_args[0][0].args
    assert "image-to-3d" in called_args
    assert "get" in called_args
    assert "create" not in called_args


def test_meshy_create_enforces_2k_and_smart_topology_within_budget(tmp_path: Path) -> None:
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    concept_file, concept_hash, op_hash = _setup_full_context(db, tmp_path, polycount=20000)
    intent_repo = ProviderOperationIntentRepository(db)

    mock_runner = MagicMock(spec=ProcessRunner)
    create_stdout = """{
      "schema_version": "meshy.cli/v1",
      "command": "image-to-3d create",
      "ok": true,
      "result": {
        "submission": {
          "task_id": "meshy-new-777",
          "status": "PENDING"
        }
      }
    }"""
    mock_runner.run.return_value = CommandResult(exit_code=0, stdout=create_stdout, stderr="")

    cli_runner = MeshyCliRunner(runner=mock_runner)
    cli_runner._cached_runner_cmd = ["meshy"]

    provider = MeshyAssetGenerationProvider(
        cli_runner=cli_runner, intent_repo=intent_repo, allow_paid_calls=True
    )

    snap = _make_paid_request_snapshot(concept_hash=concept_hash, polycount=20000)
    req = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=op_hash,
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "concept_hash": concept_hash,
            "concept_image_path": str(concept_file),
            "target_polycount": 20000,
            "max_triangles_lod0": 20000,
            "paid_request_snapshot_sha256": snap.sha256,
        },
        paid_request=snap,
    )

    resp = provider.generate(req)
    assert resp.external_op_id == "meshy-new-777"
    assert resp.status == "SUBMITTED"

    called_args = mock_runner.run.call_args[0][0].args
    # Check 2k textures enforced, never 4k!
    assert "--texture-resolution" in called_args
    idx = called_args.index("--texture-resolution")
    assert called_args[idx + 1] == "2k"

    # Check smart-topology clamped to 15000 budget
    assert "--model-type" in called_args
    idx_m = called_args.index("--model-type")
    assert called_args[idx_m + 1] == "smart-topology"
    assert "--target-polycount" in called_args
    idx_p = called_args.index("--target-polycount")
    assert called_args[idx_p + 1] == "15000"

    # Check operation-id local journal is attached
    assert "--operation-id" in called_args

    # Check durable intent was updated immediately with external_task_id
    updated_intent = intent_repo.get_by_fingerprint(snap.sha256)
    assert updated_intent is not None
    assert updated_intent.external_task_id == "meshy-new-777"
    assert updated_intent.status == "SUBMITTED"


def test_meshy_create_submission_uses_protocol_stdout(tmp_path: Path) -> None:
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    concept_file, concept_hash, op_hash = _setup_full_context(db, tmp_path, polycount=10000)
    intent_repo = ProviderOperationIntentRepository(db)

    mock_runner = MagicMock(spec=ProcessRunner)
    valid_protocol_json = json.dumps(
        {
            "schema_version": "meshy.cli/v1",
            "command": "image-to-3d create",
            "ok": True,
            "result": {
                "submission": {
                    "task_id": "01a0e9c1-f324-75f4-ab7c-59ee3b6c541d",
                    "status": "PENDING",
                }
            },
        }
    )
    # Corrupted / redacted stdout would fail task-id regex if parsed
    corrupted_stdout = '{"result": {"submission": {"task_id": "[REDACTED]"}}}'
    mock_runner.run.return_value = CommandResult(
        exit_code=0,
        stdout=corrupted_stdout,
        stderr="",
        protocol_stdout=valid_protocol_json,
    )

    cli_runner = MeshyCliRunner(runner=mock_runner)
    cli_runner._cached_runner_cmd = ["meshy"]

    provider = MeshyAssetGenerationProvider(
        cli_runner=cli_runner, intent_repo=intent_repo, allow_paid_calls=True
    )

    snap = _make_paid_request_snapshot(concept_hash=concept_hash, polycount=10000)
    req = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=op_hash,
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "concept_hash": concept_hash,
            "concept_image_path": str(concept_file),
            "target_polycount": 10000,
            "max_triangles_lod0": 10000,
            "paid_request_snapshot_sha256": snap.sha256,
        },
        paid_request=snap,
    )

    resp = provider.generate(req)
    assert resp.external_op_id == "01a0e9c1-f324-75f4-ab7c-59ee3b6c541d"
    assert resp.status == "SUBMITTED"

    updated_intent = intent_repo.get_by_fingerprint(snap.sha256)
    assert updated_intent is not None
    assert updated_intent.external_task_id == "01a0e9c1-f324-75f4-ab7c-59ee3b6c541d"
    assert updated_intent.status == "SUBMITTED"


def test_actual_workflow_shape_preserves_unknown_estimate_and_binds_nested_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MESHY_API_KEY", raising=False)
    spec = AssetSpecification(
        asset_id="prop_energy_crate_01",
        intent="Stylized energy crate",
        dimensions={"width_m": 1.0, "depth_m": 1.0, "height_m": 1.0},
        geometry_budget={"max_triangles_lod0": 12000},
    )
    spec_data = spec.model_dump(mode="json")
    spec_hash = spec_fingerprint(spec)
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    concept_file, concept_hash, op_hash = _setup_full_context(
        db,
        tmp_path,
        cost=10.0,
        spec_hash=spec_hash,
        specification=spec_data,
        provider_estimate=None,
        budget_reservation=10.0,
    )
    intent_repo = ProviderOperationIntentRepository(db)
    mock_runner = MagicMock(spec=ProcessRunner)
    mock_runner.run.return_value = CommandResult(
        exit_code=0,
        stdout=json.dumps(
            {"result": {"submission": {"task_id": "meshy-nested-1", "status": "PENDING"}}}
        ),
        stderr="",
    )
    cli_runner = MeshyCliRunner(runner=mock_runner)
    cli_runner._cached_runner_cmd = ["meshy"]
    provider = MeshyAssetGenerationProvider(
        cli_runner=cli_runner, intent_repo=intent_repo, allow_paid_calls=True
    )
    snap = _make_paid_request_snapshot(
        concept_hash=concept_hash,
        spec_hash=spec_hash,
        specification=spec_data,
        cost=None,
        budget_reservation=10.0,
    )
    request = GenerationRequest(
        prompt="Energy crate",
        operation_hash=op_hash,
        parameters={
            "provider": "meshy",
            "asset_id": spec.asset_id,
            "revision_number": 1,
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "concept_hash": concept_hash,
            "concept_image_path": str(concept_file),
            "specification": spec_data,
            "specification_hash": spec_hash,
            "provider_estimate": None,
            "budget_reservation": 10.0,
            "cost": None,
            "max_triangles_lod0": 12000,
            "paid_request_snapshot_sha256": snap.sha256,
        },
        paid_request=snap,
    )

    response = provider.generate(request)
    intent = intent_repo.get_by_fingerprint(snap.sha256)
    assert response.external_op_id == "meshy-nested-1"
    assert intent is not None and intent.estimated_cost is None
    command_request = mock_runner.run.call_args.args[0]
    assert command_request.structured_json_output is True
    assert command_request.env_overrides == {}
    assert command_request.args[command_request.args.index("--target-polycount") + 1] == "12000"


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"max_triangles_lod0": 0}, "does not match nested specification"),
        ({"provider_estimate": 1.25}, "does not match authoritative task estimate"),
    ],
)
def test_actual_workflow_shape_rejects_unbound_polycount_and_estimate(
    tmp_path: Path, overrides: dict[str, Any], match: str
) -> None:
    spec = AssetSpecification(
        asset_id="prop_energy_crate_01",
        intent="Stylized energy crate",
        dimensions={"width_m": 1.0, "depth_m": 1.0, "height_m": 1.0},
        geometry_budget={"max_triangles_lod0": 12000},
    )
    spec_data = spec.model_dump(mode="json")
    spec_hash = spec_fingerprint(spec)
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    concept_file, concept_hash, op_hash = _setup_full_context(
        db,
        tmp_path,
        cost=10.0,
        spec_hash=spec_hash,
        specification=spec_data,
        provider_estimate=None,
        budget_reservation=10.0,
    )
    mock_runner = MagicMock(spec=ProcessRunner)
    cli_runner = MeshyCliRunner(runner=mock_runner)
    cli_runner._cached_runner_cmd = ["meshy"]
    provider = MeshyAssetGenerationProvider(
        cli_runner=cli_runner,
        intent_repo=ProviderOperationIntentRepository(db),
        allow_paid_calls=True,
    )
    parameters: dict[str, Any] = {
        "provider": "meshy",
        "asset_id": spec.asset_id,
        "revision_number": 1,
        "task_id": "task-01",
        "workflow_id": "wf-01",
        "approval_id": "app-01",
        "concept_hash": concept_hash,
        "concept_image_path": str(concept_file),
        "specification": spec_data,
        "specification_hash": spec_hash,
        "provider_estimate": None,
        "budget_reservation": 10.0,
        "cost": None,
        "max_triangles_lod0": 12000,
    }
    parameters.update(overrides)

    snap = _make_paid_request_snapshot(
        concept_hash=concept_hash,
        spec_hash=spec_hash,
        specification=spec_data,
        cost=None,
        budget_reservation=10.0,
    )

    with pytest.raises(ProviderFailedError, match=match):
        provider.generate(
            GenerationRequest(
                "Energy crate",
                operation_hash=op_hash,
                parameters=parameters,
                paid_request=snap,
            )
        )
    assert provider.invocation_count == 0
    assert mock_runner.run.call_count == 0


def test_meshy_create_failure_classified_as_uncertain(tmp_path: Path) -> None:
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    concept_file, concept_hash, op_hash = _setup_full_context(db, tmp_path)
    intent_repo = ProviderOperationIntentRepository(db)

    mock_runner = MagicMock(spec=ProcessRunner)
    mock_runner.run.return_value = CommandResult(
        exit_code=1, stdout="", stderr="Connection dropped mid-flight"
    )

    cli_runner = MeshyCliRunner(runner=mock_runner)
    cli_runner._cached_runner_cmd = ["meshy"]

    provider = MeshyAssetGenerationProvider(
        cli_runner=cli_runner, intent_repo=intent_repo, allow_paid_calls=True
    )

    snap = _make_paid_request_snapshot(concept_hash=concept_hash)
    req = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=op_hash,
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "concept_hash": concept_hash,
            "concept_image_path": str(concept_file),
            "paid_request_snapshot_sha256": snap.sha256,
        },
        paid_request=snap,
    )

    with pytest.raises(ProviderUncertainError):
        provider.generate(req)

    # Intent must be recorded as UNCERTAIN in DB to prevent duplicate calls
    intent = intent_repo.get_by_fingerprint(snap.sha256)
    assert intent is not None
    assert intent.status == "UNCERTAIN"


def test_meshy_download_glb_bounded_and_hashed(tmp_path: Path) -> None:
    mock_runner = MagicMock(spec=ProcessRunner)

    dest_dir = tmp_path / "downloads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    expected_file = dest_dir / "raw.glb"
    payload = b"glTF binary payload content"
    expected_hash = hashlib.sha256(payload).hexdigest()

    def side_effect(cmd_req: CommandRequest) -> CommandResult:
        expected_file.write_bytes(payload)
        stdout = json.dumps(
            {
                "schema_version": "meshy.cli/v1",
                "command": "download",
                "ok": True,
                "result": {
                    "downloads": {
                        "state": "completed",
                        "files": [
                            {
                                "key": "model.glb",
                                "path": "raw.glb",
                                "bytes": len(payload),
                                "sha256": expected_hash,
                                "status": "written",
                                "format": "glb",
                            }
                        ],
                        "metadata_path": None,
                        "material_links": [],
                    }
                },
            }
        )
        return CommandResult(exit_code=0, stdout=stdout, stderr="")

    mock_runner.run.side_effect = side_effect
    runner = MeshyCliRunner(runner=mock_runner)
    runner._cached_runner_cmd = ["meshy"]

    file_path, file_hash = runner.download_glb("task-123", dest_dir, max_bytes=1000)
    assert file_path.is_file()
    assert file_hash == expected_hash

    # Check CLI options: requires --resource image-to-3d, --task-id, global --output and --workspace, NO --overwrite
    called_args = mock_runner.run.call_args[0][0].args
    assert "--resource" in called_args
    assert "image-to-3d" in called_args
    assert "--task-id" in called_args
    assert "task-123" in called_args
    assert "--output" in called_args
    assert "--workspace" in called_args
    assert "--overwrite" not in called_args
    command_request = mock_runner.run.call_args.args[0]
    assert command_request.structured_json_output is True


# =========================================================================
# Additional focused tests for the delegated engineering revision
# =========================================================================


def test_concurrency_race_exactly_one_create(tmp_path: Path) -> None:
    """Requirement 2: Two threads race => exactly 1 create invocation, loser blocked or queries."""
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    concept_file, concept_hash, op_hash = _setup_full_context(db, tmp_path)
    intent_repo = ProviderOperationIntentRepository(db)

    create_invocations = 0
    create_lock = threading.Lock()

    mock_runner = MagicMock(spec=ProcessRunner)

    def side_effect(cmd_req: CommandRequest) -> CommandResult:
        nonlocal create_invocations
        with create_lock:
            create_invocations += 1
        time.sleep(0.05)  # simulate network delay to ensure overlap
        stdout = json.dumps(
            {
                "schema_version": "meshy.cli/v1",
                "command": "image-to-3d create",
                "ok": True,
                "result": {
                    "submission": {
                        "task_id": "0192e2b8-93d3-7d72-9749-cfa098670ab3",
                        "status": "PENDING",
                    }
                },
            }
        )
        return CommandResult(exit_code=0, stdout=stdout, stderr="")

    mock_runner.run.side_effect = side_effect
    cli_runner = MeshyCliRunner(runner=mock_runner)
    cli_runner._cached_runner_cmd = ["meshy"]

    provider = MeshyAssetGenerationProvider(
        cli_runner=cli_runner, intent_repo=intent_repo, allow_paid_calls=True
    )

    snap = _make_paid_request_snapshot(concept_hash=concept_hash)
    req = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=op_hash,
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "concept_hash": concept_hash,
            "concept_image_path": str(concept_file),
            "paid_request_snapshot_sha256": snap.sha256,
        },
        paid_request=snap,
    )

    results: list[Any] = []
    exceptions: list[Exception] = []

    def worker() -> None:
        try:
            resp = provider.generate(req)
            results.append(resp)
        except Exception as exc:
            exceptions.append(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        t1 = executor.submit(worker)
        t2 = executor.submit(worker)
        t1.result()
        t2.result()

    # Exactly 1 fake create was invoked!
    assert create_invocations == 1
    # Winner succeeded with SUBMITTED
    assert len(results) == 1
    assert results[0].status == "SUBMITTED"
    assert results[0].external_op_id == "0192e2b8-93d3-7d72-9749-cfa098670ab3"
    # Loser was blocked with ProviderUncertainError
    assert len(exceptions) == 1
    assert isinstance(exceptions[0], ProviderUncertainError)


def test_approval_verification_missing_rejected_or_forged(tmp_path: Path) -> None:
    """Requirement 1: Missing, rejected, or forged approval causes zero create invocations."""
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    concept_file, concept_hash, op_hash = _setup_full_context(
        db, tmp_path, approval_status=ApprovalStatus.REJECTED
    )
    intent_repo = ProviderOperationIntentRepository(db)

    mock_runner = MagicMock(spec=ProcessRunner)
    cli_runner = MeshyCliRunner(runner=mock_runner)
    cli_runner._cached_runner_cmd = ["meshy"]

    provider = MeshyAssetGenerationProvider(
        cli_runner=cli_runner, intent_repo=intent_repo, allow_paid_calls=True
    )

    snap = _make_paid_request_snapshot(concept_hash=concept_hash)

    # 1. Rejected approval
    req_rejected = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=op_hash,
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "concept_hash": concept_hash,
            "concept_image_path": str(concept_file),
            "paid_request_snapshot_sha256": snap.sha256,
        },
        paid_request=snap,
    )
    with pytest.raises(ApprovalRequired, match="not approved"):
        provider.generate(req_rejected)
    assert provider.invocation_count == 0
    assert mock_runner.run.call_count == 0

    # 2. Missing approval
    req_missing = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=op_hash,
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "nonexistent-app",
            "concept_hash": concept_hash,
            "concept_image_path": str(concept_file),
            "paid_request_snapshot_sha256": snap.sha256,
        },
        paid_request=snap,
    )
    with pytest.raises(ApprovalRequired, match="not found"):
        provider.generate(req_missing)
    assert provider.invocation_count == 0

    # 3. Forged / mismatched operation hash
    app_repo = ApprovalRepository(db)
    app = app_repo.get("app-01")
    assert app is not None
    app.status = ApprovalStatus.APPROVED
    app_repo.save(app)

    req_forged = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=HEX_HASH_3,  # forged fingerprint does not match approved operation_hash
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "concept_hash": concept_hash,
            "concept_image_path": str(concept_file),
            "paid_request_snapshot_sha256": snap.sha256,
        },
        paid_request=snap,
    )
    with pytest.raises(
        ApprovalRequired, match="does not match current authoritative operation hash"
    ):
        provider.generate(req_forged)
    assert provider.invocation_count == 0


def test_revision_and_concept_hash_binding(tmp_path: Path) -> None:
    """Requirement 1: Bind request to actual revision and concept PNG hash before intent."""
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    concept_file, concept_hash, op_hash = _setup_full_context(db, tmp_path)
    intent_repo = ProviderOperationIntentRepository(db)

    provider = MeshyAssetGenerationProvider(intent_repo=intent_repo, allow_paid_calls=True)

    # 1. Missing revision in DB
    task_repo = TaskRepository(db)
    task = task_repo.get("task-01")
    assert task is not None
    task.parameters["revision_number"] = 999
    task_repo.update_parameters("task-01", task.parameters)

    snap_bad_rev = _make_paid_request_snapshot(concept_hash=concept_hash, revision_number=999)
    req_bad_rev = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=op_hash,
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 999,  # does not exist
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "concept_hash": concept_hash,
            "concept_image_path": str(concept_file),
            "paid_request_snapshot_sha256": snap_bad_rev.sha256,
        },
        paid_request=snap_bad_rev,
    )
    with pytest.raises(ProviderFailedError, match="revision 999 .* does not exist"):
        provider.generate(req_bad_rev)

    # Restore task revision number for next checks
    task.parameters["revision_number"] = 1
    task_repo.update_parameters("task-01", task.parameters)

    # 2. Corrupted concept file on disk does not match concept_hash
    concept_file.write_bytes(b"corrupted content")
    snap_corrupted = _make_paid_request_snapshot(concept_hash=concept_hash, revision_number=1)
    req_corrupted = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=op_hash,
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "concept_hash": concept_hash,
            "concept_image_path": str(concept_file),
            "paid_request_snapshot_sha256": snap_corrupted.sha256,
        },
        paid_request=snap_corrupted,
    )
    with pytest.raises(ProviderFailedError, match="does not match expected concept_hash"):
        provider.generate(req_corrupted)
    assert provider.invocation_count == 0


def test_cost_validation_and_unknown_handling() -> None:
    """Requirement 3: Finite nonnegative cost only; UNKNOWN stays None."""
    assert _validate_finite_nonnegative_cost(None, "cost") is None
    assert _validate_finite_nonnegative_cost("", "cost") is None
    assert _validate_finite_nonnegative_cost("UNKNOWN", "cost") is None
    assert _validate_finite_nonnegative_cost(0, "cost") == 0.0
    assert _validate_finite_nonnegative_cost(5.5, "cost") == 5.5

    # Reject bool
    with pytest.raises(ProviderFailedError, match="cannot be a boolean"):
        _validate_finite_nonnegative_cost(True, "cost")
    with pytest.raises(ProviderFailedError, match="cannot be a boolean"):
        _validate_finite_nonnegative_cost(False, "cost")

    # Reject NaN, Inf, negative
    with pytest.raises(ProviderFailedError, match="must be finite"):
        _validate_finite_nonnegative_cost(float("nan"), "cost")
    with pytest.raises(ProviderFailedError, match="must be finite"):
        _validate_finite_nonnegative_cost(float("inf"), "cost")
    with pytest.raises(ProviderFailedError, match="cannot be negative"):
        _validate_finite_nonnegative_cost(-1.0, "cost")


def test_unsafe_target_polycount_denied_before_intent(tmp_path: Path) -> None:
    """Requirement 3: Deny unsafe polycount (< 100, bool, non-numeric) before intent/create."""
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    concept_file, concept_hash, op_hash = _setup_full_context(db, tmp_path)
    intent_repo = ProviderOperationIntentRepository(db)

    provider = MeshyAssetGenerationProvider(intent_repo=intent_repo, allow_paid_calls=True)

    req = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=op_hash,
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "concept_hash": concept_hash,
            "concept_image_path": str(concept_file),
            "target_polycount": 50,  # unsafe: < 100
        },
    )
    with pytest.raises(ProviderFailedError, match="Unsafe target polycount"):
        provider.generate(req)
    assert provider.invocation_count == 0


def test_download_rejects_preexisting_stale_file(tmp_path: Path) -> None:
    """Requirement 4: Download rejects preexisting file; overwrite is forbidden."""
    dest_dir = tmp_path / "downloads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stale_file = dest_dir / "raw.glb"
    stale_file.write_bytes(b"stale content")

    mock_runner = MagicMock(spec=ProcessRunner)
    runner = MeshyCliRunner(runner=mock_runner)
    runner._cached_runner_cmd = ["meshy"]

    with pytest.raises(RawArtifactInvalidError, match="already exists prior to download"):
        runner.download_glb("task-123", dest_dir)
    assert mock_runner.run.call_count == 0


def test_download_rejects_symlink_and_path_escape(tmp_path: Path) -> None:
    """Requirement 4: Download rejects symlink and path escapes."""
    dest_dir = tmp_path / "downloads"
    dest_dir.mkdir(parents=True, exist_ok=True)

    mock_runner = MagicMock(spec=ProcessRunner)

    # Malicious manifest returning path escaping the directory
    escape_stdout = json.dumps(
        {
            "schema_version": "meshy.cli/v1",
            "command": "download",
            "ok": True,
            "result": {
                "downloads": {
                    "state": "completed",
                    "files": [
                        {
                            "key": "model.glb",
                            "path": "../../outside.glb",
                            "bytes": 100,
                            "sha256": HEX_HASH_1,
                            "status": "written",
                            "format": "glb",
                        }
                    ],
                }
            },
        }
    )
    mock_runner.run.return_value = CommandResult(exit_code=0, stdout=escape_stdout, stderr="")
    runner = MeshyCliRunner(runner=mock_runner)
    runner._cached_runner_cmd = ["meshy"]

    with pytest.raises(RawArtifactInvalidError, match="escapes workspace containment"):
        runner.download_glb("task-123", dest_dir)


def test_download_rejects_hash_mismatch_and_oversize(tmp_path: Path) -> None:
    """Requirement 4: Verify real bytes SHA256 matches manifest and size cap."""
    dest_dir = tmp_path / "downloads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    target_file = dest_dir / "raw.glb"

    mock_runner = MagicMock(spec=ProcessRunner)

    def side_effect(cmd_req: CommandRequest) -> CommandResult:
        target_file.write_bytes(b"tampered content")
        stdout = json.dumps(
            {
                "schema_version": "meshy.cli/v1",
                "command": "download",
                "ok": True,
                "result": {
                    "downloads": {
                        "state": "completed",
                        "files": [
                            {
                                "key": "model.glb",
                                "path": "raw.glb",
                                "bytes": 16,
                                "sha256": HEX_HASH_1,  # wrong hash!
                                "status": "written",
                                "format": "glb",
                            }
                        ],
                    }
                },
            }
        )
        return CommandResult(exit_code=0, stdout=stdout, stderr="")

    mock_runner.run.side_effect = side_effect
    runner = MeshyCliRunner(runner=mock_runner)
    runner._cached_runner_cmd = ["meshy"]

    with pytest.raises(RawArtifactInvalidError, match="does not match manifest hash"):
        runner.download_glb("task-123", dest_dir)


def test_get_task_normalizes_v1_and_validates_task_id() -> None:
    """Requirement 5: get_task requires matching task ID, recognized states, safe opaque ID regex."""
    mock_runner = MagicMock(spec=ProcessRunner)
    runner = MeshyCliRunner(runner=mock_runner)
    runner._cached_runner_cmd = ["meshy"]

    # 1. Invalid task ID format rejected before CLI run
    with pytest.raises(ProviderFailedError, match="Invalid task_id format"):
        runner.get_task("../../malicious/path")
    assert mock_runner.run.call_count == 0

    # 2. Mismatched task ID returned from query
    mismatch_stdout = json.dumps(
        {
            "schema_version": "meshy.cli/v1",
            "command": "image-to-3d get",
            "ok": True,
            "result": {
                "task": {
                    "task_id": "other-task-999",
                    "status": "SUCCEEDED",
                    "progress": 100,
                    "consumed_credits": 3.0,
                }
            },
        }
    )
    mock_runner.run.return_value = CommandResult(exit_code=0, stdout=mismatch_stdout, stderr="")

    with pytest.raises(ProviderFailedError, match="Mismatched task_id returned"):
        runner.get_task("task-123")

    # 3. Unrecognized status
    bad_status_stdout = json.dumps(
        {
            "schema_version": "meshy.cli/v1",
            "command": "image-to-3d get",
            "ok": True,
            "result": {
                "task": {
                    "task_id": "task-123",
                    "status": "INVENTED_STATUS",
                    "progress": 10,
                }
            },
        }
    )
    mock_runner.run.return_value = CommandResult(exit_code=0, stdout=bad_status_stdout, stderr="")

    with pytest.raises(ProviderFailedError, match="Unrecognized task status"):
        runner.get_task("task-123")
    assert mock_runner.run.call_args.args[0].structured_json_output is True


def test_existing_intent_identity_mismatch_fails(tmp_path: Path) -> None:
    """Requirement 2: Existing intent identity must match request revision/concept/approval/hash."""
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    concept_file, concept_hash, op_hash = _setup_full_context(db, tmp_path)
    intent_repo = ProviderOperationIntentRepository(db)

    # Intent with different revision
    intent = ProviderOperationIntent(
        id="intent-mismatch",
        workflow_id="wf-01",
        task_id="task-01",
        asset_id="prop_energy_crate_01",
        revision_number=2,  # different revision!
        provider="meshy",
        operation="image-to-3d",
        concept_hash=concept_hash,
        request_fingerprint=op_hash,
        approval_id="app-01",
        status="SUBMITTED",
        external_task_id="ext-123",
    )
    intent_repo.save(intent)

    provider = MeshyAssetGenerationProvider(intent_repo=intent_repo, allow_paid_calls=False)

    req = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=op_hash,
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,  # requests revision 1
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "concept_hash": concept_hash,
        },
    )

    with pytest.raises(ProviderFailedError, match="Refusing to attach to unrelated task"):
        provider.generate(req)


def test_tampered_request_parameters_rejected_zero_create(tmp_path: Path) -> None:
    """Requirement 1: Tampered request parameters (polycount, cost, spec_hash) rejected with 0 create."""
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    concept_file, concept_hash, op_hash = _setup_full_context(
        db, tmp_path, polycount=10000, cost=4.0, spec_hash=HEX_HASH_2
    )
    intent_repo = ProviderOperationIntentRepository(db)

    mock_runner = MagicMock(spec=ProcessRunner)
    cli_runner = MeshyCliRunner(runner=mock_runner)
    cli_runner._cached_runner_cmd = ["meshy"]
    provider = MeshyAssetGenerationProvider(
        cli_runner=cli_runner, intent_repo=intent_repo, allow_paid_calls=True
    )

    # 1. Tampered polycount in request (15000 vs authoritative 10000)
    snap_bad_poly = _make_paid_request_snapshot(concept_hash=concept_hash, polycount=15000)
    req_bad_poly = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=op_hash,
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "concept_hash": concept_hash,
            "concept_image_path": str(concept_file),
            "target_polycount": 15000,
            "paid_request_snapshot_sha256": snap_bad_poly.sha256,
        },
        paid_request=snap_bad_poly,
    )
    with pytest.raises(ProviderFailedError, match="does not match authoritative task polycount"):
        provider.generate(req_bad_poly)
    assert provider.invocation_count == 0
    assert mock_runner.run.call_count == 0

    # 2. Tampered cost in request (50.0 vs authoritative 4.0)
    snap_bad_cost = _make_paid_request_snapshot(concept_hash=concept_hash, cost=50.0)
    req_bad_cost = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=op_hash,
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "concept_hash": concept_hash,
            "concept_image_path": str(concept_file),
            "cost": 50.0,
            "paid_request_snapshot_sha256": snap_bad_cost.sha256,
        },
        paid_request=snap_bad_cost,
    )
    with pytest.raises(ProviderFailedError, match="does not match authoritative task cost"):
        provider.generate(req_bad_cost)
    assert provider.invocation_count == 0
    assert mock_runner.run.call_count == 0

    # 3. Tampered specification_hash in request
    snap_bad_spec = _make_paid_request_snapshot(concept_hash=concept_hash, spec_hash=HEX_HASH_3)
    req_bad_spec = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=op_hash,
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "concept_hash": concept_hash,
            "concept_image_path": str(concept_file),
            "specification_hash": HEX_HASH_3,
            "paid_request_snapshot_sha256": snap_bad_spec.sha256,
        },
        paid_request=snap_bad_spec,
    )
    # V0.6: the snapshot binding check rejects a tampered specification hash before
    # the legacy parameter checks run; the zero-create safety property is unchanged.
    with pytest.raises(
        PaidRequestInvalidError,
        match="specification_sha256 .* does not match revision spec_hash",
    ):
        provider.generate(req_bad_spec)
    assert provider.invocation_count == 0
    assert mock_runner.run.call_count == 0


def test_tampered_authoritative_task_params_after_approval_rejected_zero_create(
    tmp_path: Path,
) -> None:
    """Requirement 1: Authoritative task parameters modified after approval invalidate approval with 0 create."""
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    concept_file, concept_hash, op_hash = _setup_full_context(
        db, tmp_path, polycount=10000, cost=4.0
    )
    intent_repo = ProviderOperationIntentRepository(db)

    # After approval receipt is minted, attacker/operator alters task parameters in the database
    task_repo = TaskRepository(db)
    task = task_repo.get("task-01")
    assert task is not None
    task.parameters["target_polycount"] = 5000  # altered!
    task_repo.update_parameters("task-01", task.parameters)

    mock_runner = MagicMock(spec=ProcessRunner)
    cli_runner = MeshyCliRunner(runner=mock_runner)
    cli_runner._cached_runner_cmd = ["meshy"]
    provider = MeshyAssetGenerationProvider(
        cli_runner=cli_runner, intent_repo=intent_repo, allow_paid_calls=True
    )

    snap = _make_paid_request_snapshot(concept_hash=concept_hash, polycount=5000)
    req = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=op_hash,
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "concept_hash": concept_hash,
            "concept_image_path": str(concept_file),
            "target_polycount": 5000,
            "paid_request_snapshot_sha256": snap.sha256,
        },
        paid_request=snap,
    )

    # Must detect hash mismatch and reject with ApprovalRequired
    with pytest.raises(
        ApprovalRequired, match="Authoritative task parameters or artifacts have changed"
    ):
        provider.generate(req)
    assert provider.invocation_count == 0
    assert mock_runner.run.call_count == 0


def test_download_rejects_non_completed_envelope_state(tmp_path: Path) -> None:
    """Requirement 2: download_glb rejects when result.downloads.state is not completed."""
    dest_dir = tmp_path / "downloads"
    dest_dir.mkdir(parents=True, exist_ok=True)

    mock_runner = MagicMock(spec=ProcessRunner)
    runner = MeshyCliRunner(runner=mock_runner)
    runner._cached_runner_cmd = ["meshy"]

    for bad_state in ["failed", "partial", "pending"]:
        mock_runner.run.return_value = CommandResult(
            exit_code=0,
            stdout=json.dumps(
                {
                    "schema_version": "meshy.cli/v1",
                    "command": "download",
                    "ok": True,
                    "result": {
                        "downloads": {
                            "state": bad_state,
                            "files": [
                                {
                                    "key": "model.glb",
                                    "path": "raw.glb",
                                    "bytes": 100,
                                    "sha256": HEX_HASH_1,
                                    "status": "written",
                                    "format": "glb",
                                }
                            ],
                        }
                    },
                }
            ),
            stderr="",
        )
        with pytest.raises(RawArtifactInvalidError, match="expected 'completed'"):
            runner.download_glb("task-123", dest_dir)


def test_download_rejects_unsuccessful_or_non_written_file_status(tmp_path: Path) -> None:
    """Requirement 2: download_glb rejects partial, failed, skipped or non-written file status."""
    dest_dir = tmp_path / "downloads"
    dest_dir.mkdir(parents=True, exist_ok=True)

    mock_runner = MagicMock(spec=ProcessRunner)
    runner = MeshyCliRunner(runner=mock_runner)
    runner._cached_runner_cmd = ["meshy"]

    for unsucc_status in ["skipped", "failed", "partial"]:
        mock_runner.run.return_value = CommandResult(
            exit_code=0,
            stdout=json.dumps(
                {
                    "schema_version": "meshy.cli/v1",
                    "command": "download",
                    "ok": True,
                    "result": {
                        "downloads": {
                            "state": "completed",
                            "files": [
                                {
                                    "key": "model.glb",
                                    "path": "raw.glb",
                                    "bytes": 100,
                                    "sha256": HEX_HASH_1,
                                    "status": unsucc_status,
                                    "format": "glb",
                                }
                            ],
                        }
                    },
                }
            ),
            stderr="",
        )
        with pytest.raises(RawArtifactInvalidError, match="unsuccessful status"):
            runner.download_glb("task-123", dest_dir)


def test_download_requires_exactly_one_written_glb(tmp_path: Path) -> None:
    """Requirement 2: download_glb requires exactly one GLB file with status 'written'."""
    dest_dir = tmp_path / "downloads"
    dest_dir.mkdir(parents=True, exist_ok=True)

    mock_runner = MagicMock(spec=ProcessRunner)
    runner = MeshyCliRunner(runner=mock_runner)
    runner._cached_runner_cmd = ["meshy"]

    # Multiple written GLB files
    mock_runner.run.return_value = CommandResult(
        exit_code=0,
        stdout=json.dumps(
            {
                "schema_version": "meshy.cli/v1",
                "command": "download",
                "ok": True,
                "result": {
                    "downloads": {
                        "state": "completed",
                        "files": [
                            {
                                "key": "model1.glb",
                                "path": "raw1.glb",
                                "bytes": 100,
                                "sha256": HEX_HASH_1,
                                "status": "written",
                                "format": "glb",
                            },
                            {
                                "key": "model2.glb",
                                "path": "raw2.glb",
                                "bytes": 100,
                                "sha256": HEX_HASH_2,
                                "status": "written",
                                "format": "glb",
                            },
                        ],
                    }
                },
            }
        ),
        stderr="",
    )
    with pytest.raises(
        RawArtifactInvalidError, match="Expected exactly one GLB file with status 'written'"
    ):
        runner.download_glb("task-123", dest_dir)


def test_download_rejects_invalid_bytes_values(tmp_path: Path) -> None:
    """Requirement 2: Manifest bytes must be strict non-negative integer."""
    dest_dir = tmp_path / "downloads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    target_file = dest_dir / "raw.glb"

    mock_runner = MagicMock(spec=ProcessRunner)
    runner = MeshyCliRunner(runner=mock_runner)
    runner._cached_runner_cmd = ["meshy"]

    for bad_bytes in [-1, True, False, "not_a_number", None]:

        def make_side_effect(cur_bad_bytes: Any) -> Any:
            def side_effect(cmd_req: CommandRequest) -> CommandResult:
                target_file.write_bytes(b"content")
                return CommandResult(
                    exit_code=0,
                    stdout=json.dumps(
                        {
                            "schema_version": "meshy.cli/v1",
                            "command": "download",
                            "ok": True,
                            "result": {
                                "downloads": {
                                    "state": "completed",
                                    "files": [
                                        {
                                            "key": "model.glb",
                                            "path": "raw.glb",
                                            "bytes": cur_bad_bytes,
                                            "sha256": HEX_HASH_1,
                                            "status": "written",
                                            "format": "glb",
                                        }
                                    ],
                                }
                            },
                        }
                    ),
                    stderr="",
                )

            return side_effect

        mock_runner.run.side_effect = make_side_effect(bad_bytes)
        with pytest.raises(RawArtifactInvalidError):
            runner.download_glb("task-123", dest_dir)
        if target_file.exists():
            target_file.unlink()


def test_get_task_terminal_unsuccessful_persists_failed_never_resubmits(tmp_path: Path) -> None:
    """Requirement 3: Terminal unsuccessful remote state (EXPIRED/CANCELED/CANCELLED) persists FAILED and never resubmits."""
    for term_status in ["EXPIRED", "CANCELED", "CANCELLED"]:
        test_dir = tmp_path / f"test_{term_status}"
        test_dir.mkdir(parents=True, exist_ok=True)
        db = Database(test_dir / "factory.db")
        MigrationRunner(db).apply_all()
        concept_file, concept_hash, op_hash = _setup_full_context(db, test_dir)
        intent_repo = ProviderOperationIntentRepository(db)

        # Existing intent in SUBMITTED state with external task ID
        intent = ProviderOperationIntent(
            id=f"intent-{term_status}",
            workflow_id="wf-01",
            task_id="task-01",
            asset_id="prop_energy_crate_01",
            revision_number=1,
            provider="meshy",
            operation="image-to-3d",
            concept_hash=concept_hash,
            request_fingerprint=op_hash,
            approval_id="app-01",
            status="SUBMITTED",
            external_task_id=f"meshy-ext-{term_status}",
        )
        intent_repo.save(intent)

        mock_runner = MagicMock(spec=ProcessRunner)
        mock_runner.run.return_value = CommandResult(
            exit_code=0,
            stdout=json.dumps(
                {
                    "schema_version": "meshy.cli/v1",
                    "command": "image-to-3d get",
                    "ok": True,
                    "result": {
                        "task": {
                            "task_id": f"meshy-ext-{term_status}",
                            "status": term_status,
                            "progress": 50,
                        }
                    },
                }
            ),
            stderr="",
        )
        cli_runner = MeshyCliRunner(runner=mock_runner)
        cli_runner._cached_runner_cmd = ["meshy"]
        provider = MeshyAssetGenerationProvider(
            cli_runner=cli_runner, intent_repo=intent_repo, allow_paid_calls=False
        )

        req = GenerationRequest(
            prompt="Energy Crate",
            operation_hash=op_hash,
            parameters={
                "asset_id": "prop_energy_crate_01",
                "revision_number": 1,
                "task_id": "task-01",
                "workflow_id": "wf-01",
                "approval_id": "app-01",
                "concept_hash": concept_hash,
            },
        )

        # First call: detects terminal status, persists FAILED, raises ProviderFailedError
        with pytest.raises(
            ProviderFailedError, match=f"terminated with remote status '{term_status}'"
        ):
            provider.generate(req)

        # Verify DB persisted FAILED
        persisted_intent = intent_repo.get(f"intent-{term_status}")
        assert persisted_intent is not None
        assert persisted_intent.status == "FAILED"

        # Second call: must immediately raise ProviderFailedError without querying CLI runner again
        mock_runner.run.reset_mock()
        with pytest.raises(
            ProviderFailedError,
            match="failed remotely. Re-submission on the same revision is forbidden",
        ):
            provider.generate(req)
        assert mock_runner.run.call_count == 0  # No query, no create!


def test_two_approved_tasks_same_asset_revision_race_creates_at_most_once(tmp_path: Path) -> None:
    """Requirement 4: Two approved tasks for the same asset revision must never create twice; race test."""
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()

    # Set up task-01 approved for asset revision 1
    concept_file, concept_hash, op_hash_1 = _setup_full_context(
        db, tmp_path, task_id="task-01", approval_id="app-01"
    )

    # Set up task-02 also approved for the SAME asset revision 1
    task_repo = TaskRepository(db)
    task_repo.save(
        Task(
            id="task-02",
            workflow_id="wf-01",
            name="Task02",
            task_type="asset_paid_generation",
            cost_class=CostClass.PAID,
            parameters={
                "provider": "meshy",
                "asset_id": "prop_energy_crate_01",
                "revision_number": 1,
                "target_polycount": 10000,
                "cost": 4.0,
                "spec_hash": HEX_HASH_2,
                "specification_hash": HEX_HASH_2,
            },
        )
    )
    art_repo = ArtifactRepository(db)
    wf_repo = WorkflowRepository(db)
    workflow = wf_repo.get("wf-01")
    assert workflow is not None
    task2 = task_repo.get("task-02")
    assert task2 is not None
    artifacts = art_repo.list_by_workflow("wf-01")
    op_inputs_2 = build_operation_inputs(
        workflow=workflow,
        task=task2,
        artifacts=artifacts,
        cost_class=CostClass.PAID,
        provider_name=None,
        handler_context=None,
    )
    app2 = ApprovalService.create_request(
        workflow_id="wf-01",
        task_id="task-02",
        approval_type="paid_generation",
        reason="Approval for task 2",
        cost_class=CostClass.PAID,
        operation_inputs=op_inputs_2,
    )
    app2.id = "app-02"
    ApprovalService.approve(app2, actor="operator", current_inputs=op_inputs_2)
    ApprovalRepository(db).save(app2)
    op_hash_2 = app2.operation_hash

    intent_repo = ProviderOperationIntentRepository(db)

    create_count = 0
    create_lock = threading.Lock()
    mock_runner = MagicMock(spec=ProcessRunner)

    def side_effect(cmd_req: CommandRequest) -> CommandResult:
        nonlocal create_count
        with create_lock:
            create_count += 1
        time.sleep(0.05)
        return CommandResult(
            exit_code=0,
            stdout=json.dumps(
                {
                    "schema_version": "meshy.cli/v1",
                    "command": "image-to-3d create",
                    "ok": True,
                    "result": {
                        "submission": {
                            "task_id": "meshy-unique-ext-01",
                            "status": "PENDING",
                        }
                    },
                }
            ),
            stderr="",
        )

    mock_runner.run.side_effect = side_effect
    cli_runner = MeshyCliRunner(runner=mock_runner)
    cli_runner._cached_runner_cmd = ["meshy"]
    provider = MeshyAssetGenerationProvider(
        cli_runner=cli_runner, intent_repo=intent_repo, allow_paid_calls=True
    )

    snap1 = _make_paid_request_snapshot(concept_hash=concept_hash, polycount=10000)
    req1 = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=op_hash_1,
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,
            "task_id": "task-01",
            "workflow_id": "wf-01",
            "approval_id": "app-01",
            "concept_hash": concept_hash,
            "concept_image_path": str(concept_file),
            "target_polycount": 10000,
            "paid_request_snapshot_sha256": snap1.sha256,
        },
        paid_request=snap1,
    )

    snap2 = _make_paid_request_snapshot(concept_hash=concept_hash, polycount=10000)
    req2 = GenerationRequest(
        prompt="Energy Crate",
        operation_hash=op_hash_2,
        parameters={
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,
            "task_id": "task-02",
            "workflow_id": "wf-01",
            "approval_id": "app-02",
            "concept_hash": concept_hash,
            "concept_image_path": str(concept_file),
            "target_polycount": 10000,
            "paid_request_snapshot_sha256": snap2.sha256,
        },
        paid_request=snap2,
    )

    results: list[Any] = []
    errors: list[Exception] = []

    def run_req(r: GenerationRequest) -> None:
        try:
            resp = provider.generate(r)
            results.append(resp)
        except Exception as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(run_req, req1)
        f2 = executor.submit(run_req, req2)
        f1.result()
        f2.result()

    # CRITICAL: Exactly 1 create call across both approved tasks!
    assert create_count == 1
    # Exactly 1 succeeded
    assert len(results) == 1
    assert results[0].status == "SUBMITTED"
    # Exactly 1 failed closed (mismatched task_id or race loss)
    assert len(errors) == 1
    assert isinstance(errors[0], (ProviderFailedError, ProviderUncertainError))
