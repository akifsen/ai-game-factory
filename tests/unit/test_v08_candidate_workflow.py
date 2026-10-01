"""Unit tests for V0.8-3C candidate workflow currentness and engine gates."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import uuid
from dataclasses import replace
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import pytest
from PIL import Image

from gamefactory.adapters.assets.internal_rig_canonical import reviewed_text_sha256
from gamefactory.adapters.assets.internal_rig_evidence import export_rig_evidence_bundle
from gamefactory.adapters.assets.v08_candidate_runtime_digest import (
    candidate_runtime_bound_payload_canonical,
)
from gamefactory.adapters.assets.v08_candidate_runtime_request import (
    build_bound_candidate_runtime_request,
)
from gamefactory.adapters.engines.godot_image import decode_png
from gamefactory.adapters.engines.v08_candidate_runtime_runner import guard_candidate_process_runner
from gamefactory.adapters.fakes.humanoid_skin_fixture import build_humanoid_skinned_glb
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    CostLedgerRepository,
    ExecutionRepository,
    PaidRequestSnapshotRepository,
    ProviderInvocationRepository,
    ProviderOperationIntentRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.camera_framing import (
    BoundsAABB,
    framing_geometry,
    framing_metrics_from_projected_rect,
    view_axis_label,
)
from gamefactory.core.domain.errors import ArtifactError, ToolExecutionError, ValidationError
from gamefactory.core.domain.models import (
    ApprovalStatus,
    Artifact,
    AuditEvent,
    Execution,
    ExecutionStatus,
    Task,
    TaskStatus,
    Workflow,
    WorkflowStatus,
    generate_id,
    utc_now_iso,
)
from gamefactory.core.domain.v08_candidate_contracts import (
    load_packaged_candidate_specification,
    parse_asset_specification_v08_candidate,
)
from gamefactory.core.domain.v08_candidate_runtime_contract import (
    load_packaged_candidate_runtime_contract,
)
from gamefactory.core.execution.process_runner import CommandRequest, CommandResult, ProcessRunner
from gamefactory.core.policies.policy_engine import PolicyEngine, PolicyRule
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.v08_candidate_currentness import (
    CandidateCurrentnessError,
    artifact_bound_to_execution,
    assert_latest_attempt_completed,
)
from gamefactory.workflows.v08_candidate_snapshot import CANDIDATE_TEST_ONLY_APPROVAL
from gamefactory.workflows.v08_candidate_workflow import (
    CandidateWorkflowHandlers,
    candidate_workflow_readiness,
    create_fresh_v08_candidate_workspace,
    create_v08_candidate_workflow,
    register_v08_candidate_handlers,
    strict_godot_version_line,
)
from gamefactory.workflows.v08_candidate_workspace import (
    V08CandidateWorkspace,
    reject_unmanaged_candidate_database,
)


class _FakeRunner(ProcessRunner):
    def run(self, request: CommandRequest) -> CommandResult:
        if "--version" in request.args:
            return CommandResult(
                exit_code=0, stdout="4.7.2.stable.official.unit\n", stderr="", timed_out=False
            )
        return CommandResult(exit_code=0, stdout="", stderr="", timed_out=False)


def _spec_for_glb(glb: Path):
    data = load_packaged_candidate_specification().model_dump(mode="json")
    data["asset_id"] = "humanoid_skin_unit_01"
    data["processed_glb_sha256"] = hashlib.sha256(glb.read_bytes()).hexdigest()
    return parse_asset_specification_v08_candidate(data)


def _write_nonblank_png(path: Path, *, width: int = 1280, height: int = 720) -> None:
    image = Image.new("RGB", (width, height), (40, 44, 52))
    for x in range(0, width, 64):
        for y in range(0, height, 64):
            image.putpixel((x, y), ((x + y) % 255, 80, 120))
    image.save(path, format="PNG")


def _view_framing_from_bound(bound: dict) -> dict:
    contract = load_packaged_candidate_runtime_contract()
    mins = bound["rest_aabb"]["min"]
    maxs = bound["rest_aabb"]["max"]
    bounds = BoundsAABB(
        min_x=mins[0],
        min_y=mins[1],
        min_z=mins[2],
        max_x=maxs[0],
        max_y=maxs[1],
        max_z=maxs[2],
    )
    viewport = (contract.viewport.width, contract.viewport.height)
    framing_policy = contract.framing
    out: dict = {}
    for view in bound["nine_view_set"]:
        geom = framing_geometry(
            bounds,
            view,
            fov_degrees=framing_policy.fov_degrees,
            target_screen_fraction=framing_policy.target_screen_fraction,
            viewport=viewport,
        )
        vp_w, vp_h = viewport
        rect_h = geom.vertical_fraction * vp_h
        rect_w = geom.horizontal_fraction * vp_w
        rect_x = (vp_w - rect_w) * 0.5
        rect_y = (vp_h - rect_h) * 0.5
        metrics = framing_metrics_from_projected_rect(
            x=rect_x,
            y=rect_y,
            width=rect_w,
            height=rect_h,
            viewport_width=float(vp_w),
            viewport_height=float(vp_h),
            margin_fraction=framing_policy.margin_fraction,
        )
        out[view] = {
            "ok": True,
            "reason": "",
            "height_ratio": metrics.height_ratio,
            "fill_ratio": metrics.fill_ratio,
            "horizontally_centered": metrics.horizontally_centered,
            "view_axis": view_axis_label(view),
            "camera_distance": geom.distance,
            "projected_rect_pixels": {
                "x": rect_x,
                "y": rect_y,
                "width": rect_w,
                "height": rect_h,
            },
            "center_offset": metrics.center_offset,
        }
    return out


def _observation_from_bound(bound: dict, harness_raw: str) -> dict:
    return {
        "schema_version": "candidate-runtime-observation-0.8.0",
        "workflow_id": bound["workflow_id"],
        "revision": bound["revision"],
        "execution_id": bound["execution_id"],
        "strict_attempt_number": bound["strict_attempt_number"],
        "asset_id": bound["asset_id"],
        "processed_glb_sha256": bound["processed_glb_sha256"],
        "observed_glb_sha256": bound["processed_glb_sha256"],
        "request_digest": bound["request_digest"],
        "rest_aabb_canonical_sha256": bound["rest_aabb_canonical_sha256"],
        "godot_version": bound["godot_version"],
        "harness_sha256": bound["harness_sha256"],
        "harness_sha256_raw": harness_raw,
        "status": "PASS",
        "candidate_state": "CLOSED",
        "public_status": "UNSUPPORTED",
        "production_eligible": False,
        "visual_mesh_name": "SM_HumanoidSkin",
        "runtime_body_kind": "static_body",
        "collision_shape_class": "CapsuleShape3D",
        "physics_ray_hit": True,
        "rest_mesh_bounds": bound["rest_aabb"],
        "capsule": {
            "shape_class": "CapsuleShape3D",
            "observed_radius_m": bound["capsule"]["radius_m"],
            "observed_height_m": bound["capsule"]["height_m"],
            "center_m": bound["capsule_center_m"],
        },
        "view_framing": _view_framing_from_bound(bound),
        "captures": [],
        "errors": [],
    }


def _populate_captures(capture_dir: Path, bound: dict, observation: dict) -> None:
    contract = load_packaged_candidate_runtime_contract()
    width = contract.viewport.width
    height = contract.viewport.height
    capture_dir.mkdir(parents=True, exist_ok=True)
    captures = []
    for view in bound["nine_view_set"]:
        png_path = capture_dir / f"{view}.png"
        _write_nonblank_png(png_path, width=width, height=height)
        raw = png_path.read_bytes()
        digest = decode_png(raw, width, height).sha256
        captures.append(
            {
                "view": view,
                "png_sha256": digest,
                "png_width": width,
                "png_height": height,
                "execution_id": bound["execution_id"],
                "revision": bound["revision"],
                "strict_attempt_number": bound["strict_attempt_number"],
                "request_digest": bound["request_digest"],
            }
        )
    observation["captures"] = captures


def _fake_capsule_runtime(
    godot: Path,
    glb_path: Path,
    spec: Any,
    profile: Any,
    output_dir: Path,
    *,
    workflow_id: str,
    revision: int,
    execution_id: str,
    strict_attempt_number: int,
    runner: ProcessRunner | None = None,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    del timeout_seconds
    output_dir.mkdir(parents=True, exist_ok=True)
    stage = output_dir / f"godot_candidate_stage_{uuid.uuid4().hex}"
    stage.mkdir()
    harness = Path(
        str(resources.files("gamefactory.resources.godot").joinpath("candidate_capsule_harness.gd"))
    )
    proc = runner or _FakeRunner()
    bound = build_bound_candidate_runtime_request(
        glb_path,
        spec,
        profile,
        workflow_id=workflow_id,
        revision=revision,
        execution_id=execution_id,
        strict_attempt_number=strict_attempt_number,
        godot_executable=godot,
        runner=proc,
        harness_path=harness,
    )
    bound["bound_payload_canonical"] = candidate_runtime_bound_payload_canonical(bound)
    request_path = stage / "request.json"
    observation_path = stage / "observation.json"
    _, harness_raw = reviewed_text_sha256(harness)
    observation = _observation_from_bound(bound, harness_raw)
    _populate_captures(stage / "captures", bound, observation)
    request_path.write_text(json.dumps(bound, sort_keys=True) + "\n", encoding="utf-8")
    observation_path.write_text(json.dumps(observation, sort_keys=True) + "\n", encoding="utf-8")
    import_log = stage / "godot-import.log"
    render_log = stage / "godot-render.log"
    import_log.write_text("import ok\n", encoding="utf-8")
    render_log.write_text("render ok\n", encoding="utf-8")
    provenance = {
        "stage_dir": str(stage),
        "request_path": str(request_path),
        "import_log": str(import_log),
        "render_log": str(render_log),
        "import_exit_code": 0,
        "process_exit_code": 0,
        "import_timed_out": False,
        "process_timed_out": False,
        "bound_request": dict(bound),
    }
    (stage / "provenance.json").write_text(
        json.dumps(provenance, sort_keys=True) + "\n", encoding="utf-8"
    )
    return observation


def _fake_rig_export(glb_path: Path, output_dir: Path, **kwargs: Any) -> Path:
    from unittest.mock import patch

    from test_internal_rig_evidence_export import _mock_oracle

    from gamefactory.adapters.assets.internal_rig_canonical import (
        reviewed_text_sha256,
        sha256_bytes,
    )

    if output_dir.exists():
        import shutil

        shutil.rmtree(output_dir)
    contract_path = Path(
        str(
            resources.files("gamefactory.resources.internal_skin").joinpath(
                "humanoid_12bone_contract.json"
            )
        )
    )
    harness = Path(
        str(resources.files("gamefactory.resources.godot").joinpath("skin_deformation_harness.gd"))
    )
    contract_sha256 = sha256_bytes(contract_path.read_bytes())
    harness_sha256, _ = reviewed_text_sha256(harness)

    def _run_oracle(
        _godot: Path,
        glb: Path,
        _contract: object,
        oracle_out: Path,
        **oracle_kw: Any,
    ) -> dict[str, Any]:
        del oracle_kw
        return _mock_oracle(
            glb,
            oracle_out,
            contract_sha256=contract_sha256,
            harness_sha256=harness_sha256,
        )

    with patch(
        "gamefactory.adapters.assets.internal_rig_evidence.run_skin_deformation_oracle",
        side_effect=_run_oracle,
    ):
        return export_rig_evidence_bundle(
            glb_path,
            output_dir,
            bundle_id=kwargs.get("bundle_id", "unit"),
            godot_executable=Path("godot"),
            runner=kwargs.get("runner"),
        )


def _handlers(root: Path, db: Database) -> CandidateWorkflowHandlers:
    return CandidateWorkflowHandlers(
        root,
        ArtifactRepository(db),
        AssetRevisionRepository(db),
        ApprovalRepository(db),
        ExecutionRepository(db),
        TaskRepository(db),
        ArtifactManager(root),
        godot_path="godot",
        runner=_FakeRunner(),
        capsule_runtime=_fake_capsule_runtime,
        rig_export=_fake_rig_export,
        c2_export_callback=None,
        godot_version_probe=lambda _g, _r: "4.stable.unit",
    )


def _engine(root: Path, db: Database, handlers: CandidateWorkflowHandlers) -> WorkflowEngine:
    engine = WorkflowEngine(
        root,
        db,
        policy_engine=PolicyEngine(
            PolicyRule(require_approval_for_paid=True, require_approval_for_process_execution=False)
        ),
        asset_provider=None,
    )
    register_v08_candidate_handlers(engine.handler_registry, handlers)
    return engine


def _approve(engine: WorkflowEngine, db: Database, approval_id: str) -> None:
    repo = ApprovalRepository(db)
    approval = repo.get(approval_id)
    assert approval is not None
    task = TaskRepository(db).get(approval.task_id)
    workflow = engine.wf_repo.get(approval.workflow_id)
    assert task is not None and workflow is not None
    decided = ApprovalService.approve(
        approval,
        "unit-test-actor",
        current_inputs=engine.approval_inputs(workflow, task),
    )
    assert repo.decide_if_pending(
        decided,
        AuditEvent(
            id=generate_id("AUDIT"),
            entity_type="Approval",
            entity_id=decided.id,
            action="APPROVED",
            actor="unit-test-actor",
            previous_state="PENDING",
            new_state="APPROVED",
        ),
    )


def _run_to_test_only_gate(
    workspace: V08CandidateWorkspace,
    handlers: CandidateWorkflowHandlers | None = None,
) -> tuple[WorkflowEngine, str, CandidateWorkflowHandlers]:
    root = workspace.root
    db = workspace.db
    glb = root / "source.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    spec = _spec_for_glb(glb)
    workflow, tasks = create_v08_candidate_workflow(workspace, glb, spec)
    handlers = handlers or _handlers(root, db)
    engine = _engine(root, db, handlers)
    engine.register_workflow(workflow, tasks, allow_existing_empty_placeholder=True)
    result = engine.run_workflow(workflow.id)
    for _ in range(24):
        if result.status == WorkflowStatus.BLOCKED and result.pending_approval_id:
            approval = ApprovalRepository(db).get(result.pending_approval_id)
            if approval is not None and approval.approval_type == CANDIDATE_TEST_ONLY_APPROVAL:
                break
        if result.status in (WorkflowStatus.COMPLETED, WorkflowStatus.FAILED):
            break
        result = engine.run_workflow(workflow.id)
    assert result.pending_approval_id is not None
    approval = ApprovalRepository(db).get(result.pending_approval_id)
    assert approval is not None
    assert approval.approval_type == CANDIDATE_TEST_ONLY_APPROVAL
    return engine, workflow.id, handlers


def test_strict_godot_version_probe_rejects_nonzero_exit() -> None:
    class BadRunner:
        def run(self, request: CommandRequest):
            del request
            return CommandResult(exit_code=1, stdout="", stderr="fail", timed_out=False)

    with pytest.raises(ToolExecutionError):
        strict_godot_version_line(Path("godot"), BadRunner())  # type: ignore[arg-type]


def test_failed_latest_attempt_blocks_currentness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    root, db = workspace.root, workspace.db
    glb = root / "source.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    spec = _spec_for_glb(glb)
    workflow, tasks = create_v08_candidate_workflow(workspace, glb, spec)
    engine = _engine(root, db, _handlers(root, db))
    engine.register_workflow(workflow, tasks, allow_existing_empty_placeholder=True)
    capture = next(t for t in tasks if t.task_type == "v08_candidate_capsule_capture")
    exec_repo = ExecutionRepository(db)
    exec_repo.save(
        Execution(
            id="EXEC-FAIL",
            task_id=capture.id,
            attempt_number=1,
            status=ExecutionStatus.FAILED,
            completed_at=utc_now_iso(),
        )
    )
    with pytest.raises(CandidateCurrentnessError):
        assert_latest_attempt_completed(exec_repo, capture, purpose="unit")


def test_c2_hook_missing_blocks_final_completion(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    engine, workflow_id, _handlers_ref = _run_to_test_only_gate(workspace)
    blocked = engine.run_workflow(workflow_id)
    assert blocked.pending_approval_id
    _approve(engine, workspace.db, blocked.pending_approval_id)
    final = engine.run_workflow(workflow_id)
    assert final.status != WorkflowStatus.COMPLETED
    evidence = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    assert evidence.status == TaskStatus.FAILED


def test_zero_provider_invocations_for_candidate_workflow(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _, workflow_id, _ = _run_to_test_only_gate(workspace)
    _assert_zero_provider_ledger_intents_snapshots(workspace.db, workflow_id)


def test_test_only_receipt_not_promotion_eligible(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    engine, workflow_id, _ = _run_to_test_only_gate(workspace)
    blocked = engine.run_workflow(workflow_id)
    _approve(engine, workspace.db, blocked.pending_approval_id or "")
    engine.run_workflow(workflow_id)
    review = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_test_only_review"
    )
    assert review.status == TaskStatus.COMPLETED
    arts = [
        a
        for a in ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-test-only-receipt"
    ]
    assert arts
    receipt = json.loads((workspace.root / arts[-1].relative_path).read_text(encoding="utf-8"))
    assert receipt["promotion_eligible"] is False
    assert receipt["production_eligible"] is False


def test_rejects_production_factory_database(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path / "fresh")
    root = tmp_path / "legacy"
    root.mkdir()
    db_path = root / ".gamefactory/state/factory.db"
    db_path.parent.mkdir(parents=True)
    db = Database(db_path)
    MigrationRunner(db).apply_all()
    with pytest.raises(ValidationError, match="factory-managed"):
        reject_unmanaged_candidate_database(db, workspace=workspace, project_root=workspace.root)


def test_rejects_unmanaged_renamed_production_like_database(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path / "fresh")
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign_db = foreign_root / ".gamefactory/state/candidate-factory.db"
    foreign_db.parent.mkdir(parents=True)
    db = Database(foreign_db)
    MigrationRunner(db).apply_all()
    with pytest.raises(ValidationError, match="factory-managed"):
        reject_unmanaged_candidate_database(db, workspace=workspace, project_root=workspace.root)


def test_v08_candidate_workspace_rejects_direct_construction(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    with pytest.raises(ValidationError, match="create_fresh"):
        V08CandidateWorkspace(
            project_id="x",
            root=workspace.root,
            db=workspace.db,
            managed_workspace_id="not-registered",
        )


def test_readiness_rejects_tampered_rig_wrapper(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    wrapper = next(a for a in arts if a.artifact_type == "candidate-rig-attempt-wrapper")
    path = workspace.root / wrapper.relative_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["execution_id"] = "EXEC-FOREIGN"
    doc["processed_glb_sha256"] = "0" * 64
    path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    from dataclasses import replace

    new_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    ArtifactRepository(workspace.db).save(
        replace(wrapper, content_hash=new_hash, file_size=path.stat().st_size)
    )
    with pytest.raises(CandidateCurrentnessError):
        candidate_workflow_readiness(handlers, workflow_id)


def test_artifact_binding_rejects_attempt_substring_only() -> None:
    art = type(
        "A",
        (),
        {
            "relative_path": ".gamefactory/assets/x/r001/attempt-1/static-report.json",
        },
    )()
    execution = Execution(
        id="EXEC-REAL",
        task_id="T",
        attempt_number=1,
        status=ExecutionStatus.COMPLETED,
    )
    assert artifact_bound_to_execution(art, execution) is False


def test_export_rig_evidence_forwards_runner_version_failure(tmp_path: Path) -> None:
    glb = tmp_path / "skin.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    out = tmp_path / "bundle"

    class BadRunner:
        def run(self, request: CommandRequest):
            if "--version" in request.args:
                return CommandResult(exit_code=1, stdout="", stderr="bad", timed_out=False)
            return CommandResult(exit_code=0, stdout="", stderr="", timed_out=False)

    with pytest.raises(ToolExecutionError):
        export_rig_evidence_bundle(
            glb,
            out,
            godot_executable=Path("godot"),
            runner=guard_candidate_process_runner(BadRunner()),  # type: ignore[arg-type]
        )


def test_guarded_runner_rejects_valid_version_stdout_with_exit_failure() -> None:
    class MisleadingRunner:
        def run(self, request: CommandRequest):
            if "--version" in request.args:
                return CommandResult(
                    exit_code=1,
                    stdout="4.7.2.stable.official.unit\n",
                    stderr="",
                    timed_out=False,
                )
            return CommandResult(exit_code=0, stdout="", stderr="", timed_out=False)

    with pytest.raises(ToolExecutionError):
        strict_godot_version_line(
            Path("godot"),
            guard_candidate_process_runner(MisleadingRunner()),  # type: ignore[arg-type]
        )


def _inject_blocking_attempt(
    db: Database,
    workflow_id: str,
    task_type: str,
    status: ExecutionStatus,
) -> Execution:
    task = next(
        t for t in TaskRepository(db).list_by_workflow(workflow_id) if t.task_type == task_type
    )
    repo = ExecutionRepository(db)
    latest = repo.get_latest_attempt(task.id)
    attempt = (latest.attempt_number + 1) if latest else 1
    blocked = Execution(
        id=generate_id("EXEC-BLOCK"),
        task_id=task.id,
        attempt_number=attempt,
        status=status,
        completed_at=utc_now_iso(),
    )
    repo.save(blocked)
    return blocked


def _review_task(db: Database, workflow_id: str) -> Task:
    return next(
        t
        for t in TaskRepository(db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_test_only_review"
    )


def _workflow(db: Database, workflow_id: str) -> Workflow:
    workflow = WorkflowRepository(db).get(workflow_id)
    assert workflow is not None
    return workflow


def _rehash_registered_artifact(db: Database, root: Path, art: Artifact) -> None:
    path = root / art.relative_path
    ArtifactRepository(db).save(
        replace(
            art,
            content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
            file_size=path.stat().st_size,
        )
    )


def _assert_no_test_only_receipt(db: Database, workflow_id: str) -> None:
    receipts = [
        a
        for a in ArtifactRepository(db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-test-only-receipt"
    ]
    assert not receipts


def _assert_no_c2_export_artifacts(db: Database, workflow_id: str) -> None:
    markers = [
        a
        for a in ArtifactRepository(db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-c2-export-marker"
    ]
    assert not markers


def _assert_zero_provider_ledger_intents_snapshots(db: Database, workflow_id: str) -> None:
    assert ProviderInvocationRepository(db).count(workflow_id) == 0
    assert not ProviderOperationIntentRepository(db).list_by_workflow(workflow_id)
    assert not CostLedgerRepository(db).list_by_workflow(workflow_id)
    assert not PaidRequestSnapshotRepository(db).list_by_workflow(workflow_id)


def _assert_latest_attempt_error(exc: BaseException, blocked: Execution, task: Task) -> None:
    message = str(exc)
    assert blocked.id in message
    assert blocked.status.value in message
    assert task.name in message or "latest attempt" in message


def _run_to_receipt(
    workspace: V08CandidateWorkspace, workflow_id: str, engine: WorkflowEngine
) -> None:
    blocked = engine.run_workflow(workflow_id)
    assert blocked.pending_approval_id
    _approve(engine, workspace.db, blocked.pending_approval_id)
    engine.run_workflow(workflow_id)
    review = _review_task(workspace.db, workflow_id)
    assert review.status == TaskStatus.COMPLETED
    receipts = [
        a
        for a in ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-test-only-receipt"
    ]
    assert receipts


@pytest.mark.parametrize(
    ("task_type", "status"),
    [
        ("v08_candidate_static_validate", ExecutionStatus.UNCERTAIN),
        ("v08_candidate_identity_process", ExecutionStatus.FAILED),
    ],
)
def test_blocking_latest_matrix_blocks_readiness(
    tmp_path: Path, task_type: str, status: ExecutionStatus
) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    task = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == task_type
    )
    blocked = _inject_blocking_attempt(workspace.db, workflow_id, task_type, status)
    with pytest.raises(CandidateCurrentnessError) as exc_info:
        candidate_workflow_readiness(handlers, workflow_id)
    _assert_latest_attempt_error(exc_info.value, blocked, task)
    _assert_no_test_only_receipt(workspace.db, workflow_id)


_GATE_SURFACE = Literal["readiness", "review_context", "approved_dispatch", "evidence"]


@pytest.mark.parametrize(
    ("gate_surface", "task_type", "status"),
    [
        (surface, task_type, status)
        for surface in ("readiness", "review_context", "approved_dispatch", "evidence")
        for task_type in ("v08_candidate_capsule_capture", "v08_candidate_rig_oracle")
        for status in (
            ExecutionStatus.RUNNING,
            ExecutionStatus.UNCERTAIN,
            ExecutionStatus.FAILED,
        )
    ],
)
def test_capture_oracle_blocking_gate_matrix(
    tmp_path: Path,
    gate_surface: _GATE_SURFACE,
    task_type: str,
    status: ExecutionStatus,
) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    task = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == task_type
    )
    workflow = _workflow(workspace.db, workflow_id)
    review = _review_task(workspace.db, workflow_id)

    if gate_surface == "evidence":
        _run_to_receipt(workspace, workflow_id, engine)
        blocked = _inject_blocking_attempt(workspace.db, workflow_id, task_type, status)
        final = engine.run_workflow(workflow_id)
        assert final.status != WorkflowStatus.COMPLETED
        evidence = next(
            t
            for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
            if t.task_type == "v08_candidate_evidence"
        )
        assert evidence.status == TaskStatus.FAILED
        _assert_no_c2_export_artifacts(workspace.db, workflow_id)
        _assert_zero_provider_ledger_intents_snapshots(workspace.db, workflow_id)
        return

    if gate_surface == "approved_dispatch":
        pending = next(
            a
            for a in ApprovalRepository(workspace.db).list_by_workflow(workflow_id)
            if a.approval_type == CANDIDATE_TEST_ONLY_APPROVAL
            and a.status == ApprovalStatus.PENDING
        )
        _approve(engine, workspace.db, pending.id)
        blocked = _inject_blocking_attempt(workspace.db, workflow_id, task_type, status)
        _assert_no_test_only_receipt(workspace.db, workflow_id)
        with pytest.raises(ArtifactError) as exc_info:
            engine.run_workflow(workflow_id)
        _assert_latest_attempt_error(exc_info.value, blocked, task)
        assert _review_task(workspace.db, workflow_id).status != TaskStatus.COMPLETED
        _assert_no_test_only_receipt(workspace.db, workflow_id)
        return

    blocked = _inject_blocking_attempt(workspace.db, workflow_id, task_type, status)
    _assert_no_test_only_receipt(workspace.db, workflow_id)

    if gate_surface == "readiness":
        with pytest.raises(CandidateCurrentnessError) as exc_info:
            candidate_workflow_readiness(handlers, workflow_id)
        _assert_latest_attempt_error(exc_info.value, blocked, task)
    elif gate_surface == "review_context":
        with pytest.raises(ArtifactError) as exc_info:
            handlers.test_only_review_context(workflow, review)
        _assert_latest_attempt_error(exc_info.value, blocked, task)
    else:
        raise AssertionError(f"unexpected gate surface {gate_surface}")


def test_source_glb_byte_mutation_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    glb = workspace.root / "source.glb"
    glb.write_bytes(glb.read_bytes() + b"TAMPER")
    with pytest.raises(CandidateCurrentnessError):
        candidate_workflow_readiness(handlers, workflow_id)


def test_rehashed_static_report_pass_status_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    processed = next(a for a in arts if a.artifact_type == "candidate-processed-glb")
    proc_path = workspace.root / processed.relative_path
    corrupted = proc_path.read_bytes()[:-4]
    proc_path.write_bytes(corrupted)
    from dataclasses import replace

    ArtifactRepository(workspace.db).save(
        replace(processed, content_hash=hashlib.sha256(corrupted).hexdigest())
    )
    with pytest.raises(CandidateCurrentnessError):
        candidate_workflow_readiness(handlers, workflow_id)


def test_raw_processed_mismatch_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    raw = next(a for a in arts if a.artifact_type == "candidate-raw-glb")
    raw_path = workspace.root / raw.relative_path
    raw_path.write_bytes(raw_path.read_bytes() + b"TAMPER")
    with pytest.raises((CandidateCurrentnessError, ArtifactError)):
        candidate_workflow_readiness(handlers, workflow_id)


def test_stale_approval_blocks_after_snapshot_mutation(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    engine, workflow_id, _handlers_ref = _run_to_test_only_gate(workspace)
    blocked = engine.run_workflow(workflow_id)
    assert blocked.pending_approval_id
    _approve(engine, workspace.db, blocked.pending_approval_id)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    processed = next(a for a in arts if a.artifact_type == "candidate-processed-glb")
    proc_path = workspace.root / processed.relative_path
    proc_path.write_bytes(proc_path.read_bytes() + b"TAMPER")
    final = engine.run_workflow(workflow_id)
    review = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_test_only_review"
    )
    assert review.status == TaskStatus.BLOCKED
    assert final.status == WorkflowStatus.BLOCKED
    receipts = [
        a
        for a in ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-test-only-receipt"
    ]
    assert not receipts


def test_invalid_receipt_promotion_flag_blocks_evidence(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    engine, workflow_id, _ = _run_to_test_only_gate(workspace)
    blocked = engine.run_workflow(workflow_id)
    _approve(engine, workspace.db, blocked.pending_approval_id or "")
    engine.run_workflow(workflow_id)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    receipt = next(a for a in arts if a.artifact_type == "candidate-test-only-receipt")
    path = workspace.root / receipt.relative_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["promotion_eligible"] = True
    path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    from dataclasses import replace

    ArtifactRepository(workspace.db).save(
        replace(receipt, content_hash=hashlib.sha256(path.read_bytes()).hexdigest())
    )
    final = engine.run_workflow(workflow_id)
    assert final.status != WorkflowStatus.COMPLETED


def test_c2_export_callback_mutation_blocks_evidence(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    callback_calls = 0

    def _mutating_callback(_request: Any) -> Path:
        nonlocal callback_calls
        callback_calls += 1
        arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
        processed = next(a for a in arts if a.artifact_type == "candidate-processed-glb")
        proc_path = workspace.root / processed.relative_path
        proc_path.write_bytes(proc_path.read_bytes() + b"TAMPER")
        marker = workspace.root / "c2-marker.json"
        marker.write_text("{}", encoding="utf-8")
        return marker

    handlers.c2_export_callback = _mutating_callback
    blocked = engine.run_workflow(workflow_id)
    _approve(engine, workspace.db, blocked.pending_approval_id or "")
    engine.run_workflow(workflow_id)
    evidence = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    assert evidence.status == TaskStatus.FAILED
    assert callback_calls == 1
    latest = ExecutionRepository(workspace.db).get_latest_attempt(evidence.id)
    assert latest is not None
    assert latest.error_message is not None
    assert "snapshot changed during C2 export hook" in latest.error_message


def test_guarded_runner_records_provenance_commands() -> None:
    runner = guard_candidate_process_runner(_FakeRunner())
    strict_godot_version_line(Path("godot"), runner)
    assert runner.command_records
    assert "--version" in runner.command_records[0].args


def test_rehashed_retained_profile_extra_field_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    profile_art = next(a for a in arts if a.artifact_type == "candidate-profile-document")
    path = workspace.root / profile_art.relative_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["unexpected_field"] = "tamper"
    path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    from dataclasses import replace

    ArtifactRepository(workspace.db).save(
        replace(profile_art, content_hash=hashlib.sha256(path.read_bytes()).hexdigest())
    )
    with pytest.raises(CandidateCurrentnessError, match="retained profile document"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_foreign_receipt_approval_id_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    blocked = engine.run_workflow(workflow_id)
    _approve(engine, workspace.db, blocked.pending_approval_id or "")
    engine.run_workflow(workflow_id)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    receipt = next(a for a in arts if a.artifact_type == "candidate-test-only-receipt")
    path = workspace.root / receipt.relative_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["approval_id"] = "APP-FOREIGN"
    doc["fingerprint"] = "f" * 64
    doc["approval_operation_hash"] = "f" * 64
    path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    from dataclasses import replace

    ArtifactRepository(workspace.db).save(
        replace(receipt, content_hash=hashlib.sha256(path.read_bytes()).hexdigest())
    )
    with pytest.raises(CandidateCurrentnessError, match="approval"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_provenance_nonzero_exit_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    prov = next(a for a in arts if a.artifact_type == "candidate-runtime-provenance")
    path = workspace.root / prov.relative_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["import_exit_code"] = 1
    path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    from dataclasses import replace

    ArtifactRepository(workspace.db).save(
        replace(prov, content_hash=hashlib.sha256(path.read_bytes()).hexdigest())
    )
    with pytest.raises(CandidateCurrentnessError, match="non-zero exits"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_runtime_observation_wrong_workflow_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    obs_art = next(a for a in arts if a.artifact_type == "candidate-runtime-observation")
    path = workspace.root / obs_art.relative_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["workflow_id"] = "WF-FOREIGN"
    path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    from dataclasses import replace

    ArtifactRepository(workspace.db).save(
        replace(obs_art, content_hash=hashlib.sha256(path.read_bytes()).hexdigest())
    )
    with pytest.raises(CandidateCurrentnessError, match="workflow_id mismatch"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_c2_trivial_existing_path_callback_still_blocks_evidence(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    callback_calls = 0

    def _noop_callback(_request: Any) -> Path:
        nonlocal callback_calls
        callback_calls += 1
        marker = workspace.root / "existing-path.json"
        marker.write_text("{}", encoding="utf-8")
        return marker

    handlers.c2_export_callback = _noop_callback
    blocked = engine.run_workflow(workflow_id)
    _approve(engine, workspace.db, blocked.pending_approval_id or "")
    engine.run_workflow(workflow_id)
    final = engine.run_workflow(workflow_id)
    assert final.status != WorkflowStatus.COMPLETED
    evidence = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    assert evidence.status == TaskStatus.FAILED
    assert callback_calls == 1
    latest = ExecutionRepository(workspace.db).get_latest_attempt(evidence.id)
    assert latest is not None
    assert latest.error_message is not None
    assert "C2 trusted cold verification" in latest.error_message
    markers = [
        a
        for a in ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-c2-export-marker"
    ]
    assert not markers


def test_asset_revision_row_tamper_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    tasks = TaskRepository(workspace.db).list_by_workflow(workflow_id)
    prepare = next(t for t in tasks if t.task_type == "v08_candidate_prepare")
    asset_id = prepare.parameters["asset_id"]
    number = int(prepare.parameters["revision_number"])
    with workspace.db.transaction() as conn:
        conn.execute(
            "UPDATE asset_revisions SET spec_hash = ? WHERE asset_id = ? AND revision_number = ?;",
            ("0" * 64, asset_id, number),
        )
    with pytest.raises(CandidateCurrentnessError, match="spec_hash"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_runtime_registered_capture_count_short_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    capture = next(a for a in arts if a.artifact_type == "candidate-runtime-capture")
    with workspace.db.transaction() as conn:
        conn.execute("DELETE FROM artifacts WHERE id = ?;", (capture.id,))
    with pytest.raises(CandidateCurrentnessError, match="expected nine registered runtime capture"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_runtime_duplicate_registered_capture_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    capture = next(a for a in arts if a.artifact_type == "candidate-runtime-capture")
    dup_path = workspace.root / capture.relative_path.replace(".png", "-dup.png")
    dup_path.write_bytes((workspace.root / capture.relative_path).read_bytes())
    dup = replace(
        capture,
        id=generate_id("ART-DUP"),
        relative_path=dup_path.relative_to(workspace.root).as_posix(),
        content_hash=hashlib.sha256(dup_path.read_bytes()).hexdigest(),
        file_size=dup_path.stat().st_size,
    )
    ArtifactRepository(workspace.db).save(dup)
    with pytest.raises(CandidateCurrentnessError, match="expected nine registered runtime capture"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_runtime_foreign_bound_capture_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    capture = next(a for a in arts if a.artifact_type == "candidate-runtime-capture")
    rel_parts = capture.relative_path.replace("\\", "/").split("/")
    foreign_parts = [
        "capsule-runtime-EXEC-FOREIGN" if part.startswith("capsule-runtime-") else part
        for part in rel_parts
    ]
    foreign_path = Path("/".join(foreign_parts))
    full = workspace.root / foreign_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes((workspace.root / capture.relative_path).read_bytes())
    foreign = replace(
        capture,
        id=generate_id("ART-FOREIGN-CAP"),
        relative_path=foreign_path.as_posix(),
        content_hash=hashlib.sha256(full.read_bytes()).hexdigest(),
        file_size=full.stat().st_size,
    )
    with workspace.db.transaction() as conn:
        conn.execute("DELETE FROM artifacts WHERE id = ?;", (capture.id,))
    ArtifactRepository(workspace.db).save(foreign)
    with pytest.raises(CandidateCurrentnessError, match="expected nine registered runtime capture"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_runtime_request_foreign_execution_path_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    request = next(a for a in arts if a.artifact_type == "candidate-runtime-request")
    rel_parts = request.relative_path.replace("\\", "/").split("/")
    foreign_rel = "/".join(
        "capsule-runtime-EXEC-FOREIGN-RUNTIME" if part.startswith("capsule-runtime-") else part
        for part in rel_parts
    )
    foreign_full = workspace.root / foreign_rel
    foreign_full.parent.mkdir(parents=True, exist_ok=True)
    foreign_full.write_bytes((workspace.root / request.relative_path).read_bytes())
    with workspace.db.transaction() as conn:
        conn.execute("DELETE FROM artifacts WHERE id = ?;", (request.id,))
    ArtifactRepository(workspace.db).save(
        replace(
            request,
            id=generate_id("ART-FOREIGN-REQ"),
            relative_path=foreign_rel,
            content_hash=hashlib.sha256(foreign_full.read_bytes()).hexdigest(),
            file_size=foreign_full.stat().st_size,
        )
    )
    with pytest.raises(
        CandidateCurrentnessError,
        match=r"expected exactly one candidate-runtime-request",
    ):
        candidate_workflow_readiness(handlers, workflow_id)


def test_runtime_request_revision_mismatch_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    request = next(a for a in arts if a.artifact_type == "candidate-runtime-request")
    path = workspace.root / request.relative_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["revision"] = int(doc["revision"]) + 17
    path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    _rehash_registered_artifact(workspace.db, workspace.root, request)
    with pytest.raises(CandidateCurrentnessError, match="runtime request revision mismatch"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_runtime_request_observation_rehashed_wrong_revision_pair_blocks_readiness(
    tmp_path: Path,
) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    request = next(a for a in arts if a.artifact_type == "candidate-runtime-request")
    observation = next(a for a in arts if a.artifact_type == "candidate-runtime-observation")
    wrong_revision = 9999
    for art in (request, observation):
        path = workspace.root / art.relative_path
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["revision"] = wrong_revision
        path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
        _rehash_registered_artifact(workspace.db, workspace.root, art)
    with pytest.raises(CandidateCurrentnessError, match="runtime request revision mismatch"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_retained_spec_rehashed_wrong_asset_id_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    spec_art = next(a for a in arts if a.artifact_type == "candidate-specification")
    path = workspace.root / spec_art.relative_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["asset_id"] = "humanoid_skin_unit_02"
    path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    _rehash_registered_artifact(workspace.db, workspace.root, spec_art)
    with pytest.raises(CandidateCurrentnessError, match="retained specification"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_asset_revision_profile_id_tamper_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    tasks = TaskRepository(workspace.db).list_by_workflow(workflow_id)
    prepare = next(t for t in tasks if t.task_type == "v08_candidate_prepare")
    asset_id = prepare.parameters["asset_id"]
    number = int(prepare.parameters["revision_number"])
    with workspace.db.transaction() as conn:
        conn.execute(
            "UPDATE asset_revisions SET profile_id = ? WHERE asset_id = ? AND revision_number = ?;",
            ("profile-foreign", asset_id, number),
        )
    with pytest.raises(CandidateCurrentnessError, match="profile_id"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_asset_revision_raw_hash_tamper_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    tasks = TaskRepository(workspace.db).list_by_workflow(workflow_id)
    prepare = next(t for t in tasks if t.task_type == "v08_candidate_prepare")
    asset_id = prepare.parameters["asset_id"]
    number = int(prepare.parameters["revision_number"])
    with workspace.db.transaction() as conn:
        conn.execute(
            "UPDATE asset_revisions SET raw_glb_hash = ? WHERE asset_id = ? AND revision_number = ?;",
            ("1" * 64, asset_id, number),
        )
    with pytest.raises(CandidateCurrentnessError, match="raw_glb_hash"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_prepare_task_params_source_hash_drift_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    prepare = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_prepare"
    )
    params = dict(prepare.parameters)
    params["source_glb_hash"] = "2" * 64
    with workspace.db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET parameters_json = ? WHERE id = ?;",
            (json.dumps(params), prepare.id),
        )
    with pytest.raises(CandidateCurrentnessError, match="authoritative source_glb"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_persisted_approval_rejected_after_receipt_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    _run_to_receipt(workspace, workflow_id, engine)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    receipt = json.loads(
        (
            workspace.root
            / next(
                a for a in arts if a.artifact_type == "candidate-test-only-receipt"
            ).relative_path
        ).read_text(encoding="utf-8")
    )
    approval = ApprovalRepository(workspace.db).get(receipt["approval_id"])
    assert approval is not None
    ApprovalRepository(workspace.db).save(
        replace(approval, status=ApprovalStatus.REJECTED, decided_at=utc_now_iso())
    )
    with pytest.raises(CandidateCurrentnessError, match="persisted approval is not APPROVED"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_persisted_approval_stale_operation_hash_after_receipt_blocks_readiness(
    tmp_path: Path,
) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    _run_to_receipt(workspace, workflow_id, engine)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    receipt = json.loads(
        (
            workspace.root
            / next(
                a for a in arts if a.artifact_type == "candidate-test-only-receipt"
            ).relative_path
        ).read_text(encoding="utf-8")
    )
    approval = ApprovalRepository(workspace.db).get(receipt["approval_id"])
    assert approval is not None
    with workspace.db.transaction() as conn:
        conn.execute(
            "UPDATE approvals SET operation_hash = ? WHERE id = ?;",
            ("0" * 64, approval.id),
        )
    with pytest.raises(CandidateCurrentnessError, match="approval_operation_hash|operation_hash"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_persisted_approval_foreign_task_after_receipt_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    _run_to_receipt(workspace, workflow_id, engine)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    receipt = json.loads(
        (
            workspace.root
            / next(
                a for a in arts if a.artifact_type == "candidate-test-only-receipt"
            ).relative_path
        ).read_text(encoding="utf-8")
    )
    approval = ApprovalRepository(workspace.db).get(receipt["approval_id"])
    assert approval is not None
    review_task = _review_task(workspace.db, workflow_id)
    foreign_task = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.id != review_task.id
    )
    with workspace.db.transaction() as conn:
        conn.execute(
            "UPDATE approvals SET task_id = ? WHERE id = ?;",
            (foreign_task.id, approval.id),
        )
    with pytest.raises(CandidateCurrentnessError, match="persisted approval"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_post_receipt_capture_rerun_blocks_readiness_and_evidence(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    _run_to_receipt(workspace, workflow_id, engine)
    blocked = _inject_blocking_attempt(
        workspace.db, workflow_id, "v08_candidate_capsule_capture", ExecutionStatus.FAILED
    )
    capture_task = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_capsule_capture"
    )
    with pytest.raises(CandidateCurrentnessError) as exc_info:
        candidate_workflow_readiness(handlers, workflow_id)
    _assert_latest_attempt_error(exc_info.value, blocked, capture_task)
    final = engine.run_workflow(workflow_id)
    assert final.status != WorkflowStatus.COMPLETED
    evidence = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    assert evidence.status == TaskStatus.FAILED
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)


def test_provenance_process_exit_nonzero_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    prov = next(a for a in arts if a.artifact_type == "candidate-runtime-provenance")
    path = workspace.root / prov.relative_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["process_exit_code"] = 2
    path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    _rehash_registered_artifact(workspace.db, workspace.root, prov)
    with pytest.raises(CandidateCurrentnessError, match="non-zero exits"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_provenance_render_log_engine_error_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    prov = next(a for a in arts if a.artifact_type == "candidate-runtime-provenance")
    path = workspace.root / prov.relative_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    log_path = Path(doc["render_log"])
    if not log_path.is_absolute():
        log_path = workspace.root / log_path
    log_path.write_text("ERROR: Failed to load scene\n", encoding="utf-8")
    render_art = next(a for a in arts if a.artifact_type == "candidate-runtime-render-log")
    _rehash_registered_artifact(workspace.db, workspace.root, render_art)
    with pytest.raises(
        CandidateCurrentnessError,
        match="engine error diagnostics|registration hash drifted",
    ):
        candidate_workflow_readiness(handlers, workflow_id)


def test_provenance_missing_import_log_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    prov = next(a for a in arts if a.artifact_type == "candidate-runtime-provenance")
    path = workspace.root / prov.relative_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    del doc["import_log"]
    path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    _rehash_registered_artifact(workspace.db, workspace.root, prov)
    with pytest.raises(CandidateCurrentnessError, match="missing import_log"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_provenance_bound_request_wrong_execution_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    prov = next(a for a in arts if a.artifact_type == "candidate-runtime-provenance")
    path = workspace.root / prov.relative_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    bound = dict(doc["bound_request"])
    bound["execution_id"] = "EXEC-PROVENANCE-FOREIGN"
    doc["bound_request"] = bound
    path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    _rehash_registered_artifact(workspace.db, workspace.root, prov)
    with pytest.raises(
        CandidateCurrentnessError,
        match="bound_request does not match registered runtime request",
    ):
        candidate_workflow_readiness(handlers, workflow_id)


def test_guarded_runner_rejects_version_timeout_with_valid_stdout() -> None:
    class TimeoutRunner:
        def run(self, request: CommandRequest):
            if "--version" in request.args:
                return CommandResult(
                    exit_code=0,
                    stdout="4.7.2.stable.official.unit\n",
                    stderr="",
                    timed_out=True,
                )
            return CommandResult(exit_code=0, stdout="", stderr="", timed_out=False)

    with pytest.raises(ToolExecutionError, match="timed out"):
        strict_godot_version_line(
            Path("godot"),
            guard_candidate_process_runner(TimeoutRunner()),  # type: ignore[arg-type]
        )


def test_c2_callback_injects_blocking_execution_blocks_evidence(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    callback_calls = 0

    def _blocking_callback(_request: Any) -> Path:
        nonlocal callback_calls
        callback_calls += 1
        evidence_task = next(
            t
            for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
            if t.task_type == "v08_candidate_evidence"
        )
        ExecutionRepository(workspace.db).save(
            Execution(
                id=generate_id("EXEC-C2-BLOCK"),
                task_id=evidence_task.id,
                attempt_number=99,
                status=ExecutionStatus.RUNNING,
            )
        )
        marker = workspace.root / "c2-blocking-marker.json"
        marker.write_text("{}", encoding="utf-8")
        return marker

    handlers.c2_export_callback = _blocking_callback
    blocked = engine.run_workflow(workflow_id)
    _approve(engine, workspace.db, blocked.pending_approval_id or "")
    engine.run_workflow(workflow_id)
    final = engine.run_workflow(workflow_id)
    assert final.status != WorkflowStatus.COMPLETED
    evidence = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    assert evidence.status == TaskStatus.FAILED
    assert callback_calls == 1
    failed_exec = next(
        e
        for e in ExecutionRepository(workspace.db).list_by_task(evidence.id)
        if e.status == ExecutionStatus.FAILED
    )
    assert failed_exec.error_message is not None
    assert "blocking status during C2 export hook" in failed_exec.error_message
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)


def test_c2_evidence_failure_is_c2_unavailable_not_missing_artifact(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    marker = workspace.root / "present.json"
    marker.write_text("{}", encoding="utf-8")
    handlers.c2_export_callback = lambda _req: marker
    blocked = engine.run_workflow(workflow_id)
    _approve(engine, workspace.db, blocked.pending_approval_id or "")
    engine.run_workflow(workflow_id)
    final = engine.run_workflow(workflow_id)
    assert final.status != WorkflowStatus.COMPLETED
    evidence = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    assert evidence.status == TaskStatus.FAILED
    latest = ExecutionRepository(workspace.db).get_latest_attempt(evidence.id)
    assert latest is not None
    assert latest.error_message is not None
    assert "missing path" not in (latest.error_message or "").casefold()
    assert "C2 trusted cold verification" in latest.error_message
    assert "C1 blocks evidence task completion" in latest.error_message
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)
    receipts = [
        a
        for a in ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-test-only-receipt"
    ]
    assert receipts


def test_genuine_post_review_readiness_passes_then_evidence_fails_c2_unavailable(
    tmp_path: Path,
) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    _run_to_receipt(workspace, workflow_id, engine)
    snapshot = candidate_workflow_readiness(handlers, workflow_id)
    assert snapshot.fingerprint()
    final = engine.run_workflow(workflow_id)
    assert final.status != WorkflowStatus.COMPLETED
    evidence = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    assert evidence.status == TaskStatus.FAILED
    latest = ExecutionRepository(workspace.db).get_latest_attempt(evidence.id)
    assert latest is not None
    assert latest.error_message is not None
    assert "C2 trusted cold verification" in latest.error_message
    assert "operation_hash is stale" not in (latest.error_message or "")


def test_runtime_split_stage_observation_reregistration_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    request = next(a for a in arts if a.artifact_type == "candidate-runtime-request")
    observation = next(a for a in arts if a.artifact_type == "candidate-runtime-observation")
    request_stage = (workspace.root / request.relative_path).parent
    foreign_stage = request_stage.parent / "godot_candidate_stage_foreign"
    foreign_stage.mkdir()
    (foreign_stage / "captures").mkdir()
    foreign_obs = foreign_stage / "observation.json"
    foreign_obs.write_bytes((workspace.root / observation.relative_path).read_bytes())
    for cap in [a for a in arts if a.artifact_type == "candidate-runtime-capture"]:
        view = Path(cap.relative_path).name
        dest = foreign_stage / "captures" / view
        dest.write_bytes((workspace.root / cap.relative_path).read_bytes())
        foreign_rel = dest.relative_to(workspace.root).as_posix()
        with workspace.db.transaction() as conn:
            conn.execute(
                "UPDATE artifacts SET relative_path = ?, file_size = ? WHERE id = ?;",
                (foreign_rel, dest.stat().st_size, cap.id),
            )
    foreign_obs_rel = foreign_obs.relative_to(workspace.root).as_posix()
    with workspace.db.transaction() as conn:
        conn.execute(
            "UPDATE artifacts SET relative_path = ?, file_size = ? WHERE id = ?;",
            (foreign_obs_rel, foreign_obs.stat().st_size, observation.id),
        )
    with pytest.raises(
        CandidateCurrentnessError,
        match="runtime observation must be observation.json directly under runtime stage",
    ):
        candidate_workflow_readiness(handlers, workflow_id)


def test_runtime_child_directory_import_log_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    request = next(a for a in arts if a.artifact_type == "candidate-runtime-request")
    stage = (workspace.root / request.relative_path).parent
    child = stage / "foreign-child"
    child.mkdir()
    child_log = child / "godot-import.log"
    child_log.write_text((stage / "godot-import.log").read_text(encoding="utf-8"), encoding="utf-8")
    import_art = next(a for a in arts if a.artifact_type == "candidate-runtime-import-log")
    child_rel = child_log.relative_to(workspace.root).as_posix()
    with workspace.db.transaction() as conn:
        conn.execute(
            "UPDATE artifacts SET relative_path = ?, content_hash = ?, file_size = ? WHERE id = ?;",
            (
                child_rel,
                hashlib.sha256(child_log.read_bytes()).hexdigest(),
                child_log.stat().st_size,
                import_art.id,
            ),
        )
    prov = next(a for a in arts if a.artifact_type == "candidate-runtime-provenance")
    prov_path = workspace.root / prov.relative_path
    doc = json.loads(prov_path.read_text(encoding="utf-8"))
    doc["import_log"] = str(child_log)
    prov_path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    _rehash_registered_artifact(workspace.db, workspace.root, prov)
    with pytest.raises(
        CandidateCurrentnessError,
        match="import_log must be godot-import.log directly under runtime stage",
    ):
        candidate_workflow_readiness(handlers, workflow_id)


def test_runtime_duplicate_registered_provenance_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    prov = next(a for a in arts if a.artifact_type == "candidate-runtime-provenance")
    dup = replace(prov, id=generate_id("ART-DUP-PROV"))
    ArtifactRepository(workspace.db).save(dup)
    with pytest.raises(
        CandidateCurrentnessError,
        match="expected exactly one registered candidate-runtime-provenance",
    ):
        candidate_workflow_readiness(handlers, workflow_id)


@pytest.mark.parametrize(
    "timeout_patch",
    [
        {"import_timed_out": True},
        {"process_timed_out": True},
        {"import_timed_out": 0},
        {"process_timed_out": "false"},
    ],
)
def test_provenance_timeout_provenance_must_be_exactly_false(
    tmp_path: Path, timeout_patch: dict[str, object]
) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    prov = next(a for a in arts if a.artifact_type == "candidate-runtime-provenance")
    path = workspace.root / prov.relative_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc.update(timeout_patch)
    path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    _rehash_registered_artifact(workspace.db, workspace.root, prov)
    with pytest.raises(CandidateCurrentnessError, match="must be exactly false"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_provenance_missing_import_timed_out_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    prov = next(a for a in arts if a.artifact_type == "candidate-runtime-provenance")
    path = workspace.root / prov.relative_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    del doc["import_timed_out"]
    path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    _rehash_registered_artifact(workspace.db, workspace.root, prov)
    with pytest.raises(CandidateCurrentnessError, match="import_timed_out must be exactly false"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_snapshot_fingerprint_includes_pre_review_capture_path_binding(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    before = candidate_workflow_readiness(handlers, workflow_id)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    capture = next(a for a in arts if a.artifact_type == "candidate-runtime-capture")
    request = next(a for a in arts if a.artifact_type == "candidate-runtime-request")
    stage = (workspace.root / request.relative_path).parent
    foreign_stage = stage.parent / "godot_candidate_stage_foreign"
    foreign_stage.mkdir()
    (foreign_stage / "captures").mkdir()
    dest = foreign_stage / "captures" / Path(capture.relative_path).name
    dest.write_bytes((workspace.root / capture.relative_path).read_bytes())
    foreign_rel = dest.relative_to(workspace.root).as_posix()
    with workspace.db.transaction() as conn:
        conn.execute(
            "UPDATE artifacts SET relative_path = ?, file_size = ? WHERE id = ?;",
            (foreign_rel, dest.stat().st_size, capture.id),
        )
    with pytest.raises(
        CandidateCurrentnessError,
        match="runtime capture",
    ):
        candidate_workflow_readiness(handlers, workflow_id)
    assert "pre_review_artifact_bindings" in before.payload
    assert before.payload["authoritative_source_glb_relative_path"]


def test_provenance_import_log_outside_stage_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    prov = next(a for a in arts if a.artifact_type == "candidate-runtime-provenance")
    path = workspace.root / prov.relative_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    foreign_log = workspace.root / "foreign-godot-import.log"
    foreign_log.write_text("import ok\n", encoding="utf-8")
    doc["import_log"] = str(foreign_log)
    path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    _rehash_registered_artifact(workspace.db, workspace.root, prov)
    with pytest.raises(CandidateCurrentnessError, match="does not match registered capture log"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_provenance_bound_request_foreign_profile_hash_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    prov = next(a for a in arts if a.artifact_type == "candidate-runtime-provenance")
    path = workspace.root / prov.relative_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    bound = dict(doc["bound_request"])
    bound["profile_document_hash"] = "f" * 64
    doc["bound_request"] = bound
    path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    _rehash_registered_artifact(workspace.db, workspace.root, prov)
    with pytest.raises(
        CandidateCurrentnessError,
        match="bound_request does not match registered runtime request",
    ):
        candidate_workflow_readiness(handlers, workflow_id)


def test_provenance_bool_false_import_exit_blocks_readiness(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    arts = ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
    prov = next(a for a in arts if a.artifact_type == "candidate-runtime-provenance")
    path = workspace.root / prov.relative_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["import_exit_code"] = False
    path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    _rehash_registered_artifact(workspace.db, workspace.root, prov)
    with pytest.raises(CandidateCurrentnessError, match="exit codes invalid"):
        candidate_workflow_readiness(handlers, workflow_id)


def test_guarded_runner_rejects_bool_false_exit_code_on_version_probe() -> None:
    class BoolExitRunner:
        def run(self, request: CommandRequest):
            if "--version" in request.args:
                return CommandResult(
                    exit_code=False,  # type: ignore[arg-type]
                    stdout="4.7.2.stable.official.unit\n",
                    stderr="",
                    timed_out=False,
                )
            return CommandResult(exit_code=0, stdout="", stderr="", timed_out=False)

    with pytest.raises(ToolExecutionError, match="non-integer exit code"):
        strict_godot_version_line(
            Path("godot"),
            guard_candidate_process_runner(BoolExitRunner()),  # type: ignore[arg-type]
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction parent guard")
def test_fresh_workspace_rejects_junction_parent(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    junction_parent = tmp_path / "junction_parent"
    try:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction_parent), str(target)],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("junction creation unavailable without privilege")
    with pytest.raises(ValidationError, match="symlink or junction"):
        create_fresh_v08_candidate_workspace(junction_parent)
