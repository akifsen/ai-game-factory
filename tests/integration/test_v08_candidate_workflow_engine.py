"""Real-tool WorkflowEngine integration for V0.8-3C candidate TEST_ONLY review."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest

from gamefactory.adapters.assets.v08_candidate_evidence import (
    bind_production_candidate_evidence_exporter,
)
from gamefactory.adapters.assets.v08_candidate_geometry import (
    envelope_size_from_aabb,
    visual_aabb_in_reference_root_frame,
)
from gamefactory.adapters.assets.v08_candidate_rules import decode_candidate_glb
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    ExecutionRepository,
    ProviderInvocationRepository,
    TaskRepository,
)
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.models import (
    AuditEvent,
    TaskStatus,
    WorkflowStatus,
    generate_id,
)
from gamefactory.core.domain.v08_candidate_contracts import (
    load_packaged_candidate_profile,
    parse_asset_specification_v08_candidate,
    profile_document_hash,
)
from gamefactory.core.execution.process_runner import ProcessRunner
from gamefactory.core.policies.policy_engine import PolicyEngine, PolicyRule
from gamefactory.workflows.engine import WorkflowEngine, WorkflowExecutionResult
from gamefactory.workflows.v08_candidate_currentness import assert_zero_provider_activity
from gamefactory.workflows.v08_candidate_evidence_readiness import (
    assert_candidate_evidence_complete,
    assert_candidate_workflow_engine_finalized,
)
from gamefactory.workflows.v08_candidate_snapshot import CANDIDATE_TEST_ONLY_APPROVAL
from gamefactory.workflows.v08_candidate_workflow import (
    CandidateWorkflowHandlers,
    create_fresh_v08_candidate_workspace,
    create_v08_candidate_workflow,
    register_v08_candidate_handlers,
)

BLENDER = os.environ.get(
    "GAMEFACTORY_TEST_BLENDER",
    r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe",
)
GODOT = os.environ.get(
    "GAMEFACTORY_TEST_GODOT",
    r"C:\Users\lenovo\devel\godot\Godot_v4.7.2-stable_win64.exe\Godot_v4.7.2-stable_win64_console.exe",
)


def _ci_candidate_real_evidence_dir(project_root: Path) -> Path | None:
    explicit = os.environ.get("GAMEFACTORY_CI_CANDIDATE_REAL_EVIDENCE_DIR")
    if explicit:
        return Path(explicit)
    if os.environ.get("CI"):
        return project_root / ".verification" / "ci-candidate-real"
    return None


def _sanitize_workflow_failure_message(message: str | None, *, limit: int = 400) -> str | None:
    if message is None:
        return None
    trimmed = message.strip()
    if len(trimmed) > limit:
        return trimmed[: limit - 3] + "..."
    return trimmed


def _workflow_failure_diagnostics(
    db: object,
    workflow_id: str,
    result: WorkflowExecutionResult,
) -> dict[str, object]:
    tasks = TaskRepository(db).list_by_workflow(workflow_id)
    exec_repo = ExecutionRepository(db)
    failed_summaries: list[dict[str, object]] = []
    for task in tasks:
        if task.status != TaskStatus.FAILED:
            continue
        attempts = exec_repo.list_by_task(task.id)
        latest = attempts[-1] if attempts else None
        failed_summaries.append(
            {
                "task_id": task.id,
                "task_type": task.task_type,
                "task_status": task.status.value,
                "latest_execution_status": latest.status.value if latest else None,
                "error_message": _sanitize_workflow_failure_message(
                    latest.error_message if latest else None
                ),
            }
        )
    return {
        "workflow_id": workflow_id,
        "workflow_status": result.status.value,
        "workflow_error_message": _sanitize_workflow_failure_message(result.error_message),
        "workflow_error_code": result.error_code,
        "failed_task_count": len(failed_summaries),
        "failed_tasks": failed_summaries,
    }


def _maybe_write_ci_workflow_failure_report(
    project_root: Path,
    payload: dict[str, object],
) -> None:
    dest_root = _ci_candidate_real_evidence_dir(project_root)
    if dest_root is None:
        return
    dest_root.mkdir(parents=True, exist_ok=True)
    report_path = dest_root / "workflow-failure-diagnostic.json"
    report_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def assert_workflow_completed_or_diagnose(
    db: object,
    project_root: Path,
    workflow_id: str,
    result: WorkflowExecutionResult,
) -> None:
    if result.status == WorkflowStatus.COMPLETED:
        return
    diagnostics = _workflow_failure_diagnostics(db, workflow_id, result)
    _maybe_write_ci_workflow_failure_report(project_root, diagnostics)
    raise AssertionError(
        "candidate workflow did not reach COMPLETED after TEST_ONLY approval: "
        f"status={result.status.value} "
        f"error_message={result.error_message!r} "
        f"error_code={result.error_code!r} "
        f"failed_tasks={diagnostics['failed_tasks']}"
    )


def copy_ci_candidate_evidence_if_requested(
    project_root: Path,
    db: object,
    workflow_id: str,
) -> None:
    """Opt-in hook: copy selected publication artifacts when CI sets the destination env."""
    dest_raw = os.environ.get("GAMEFACTORY_CI_CANDIDATE_EVIDENCE_DIR")
    if not dest_raw:
        return
    dest = Path(dest_raw)
    dest.mkdir(parents=True, exist_ok=True)
    arts = ArtifactRepository(db).list_by_workflow(workflow_id)
    by_type = {artifact.artifact_type: artifact for artifact in arts}
    manifest = by_type.get("candidate-c2-evidence-manifest")
    result = by_type.get("candidate-c2-evidence-result")
    marker = by_type.get("candidate-c2-export-marker")
    if manifest is None or result is None or marker is None:
        raise AssertionError("CI candidate evidence hook missing publication artifacts")
    manifest_path = project_root / manifest.relative_path
    result_path = project_root / result.relative_path
    marker_path = project_root / marker.relative_path
    shutil.copy2(manifest_path, dest / "manifest.json")
    shutil.copy2(result_path, dest / "result.json")
    shutil.copy2(marker_path, dest / "export-marker.json")
    result_doc = json.loads(result_path.read_text(encoding="utf-8"))
    trusted = result_doc.get("trusted_cold_result")
    if trusted is None:
        raise AssertionError("CI candidate evidence hook missing trusted_cold_result")
    (dest / "trusted-cold-outcome.json").write_text(
        json.dumps(trusted, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _export_script_path() -> Path:
    return Path(
        str(
            resources.files("gamefactory.resources.blender").joinpath(
                "export_humanoid_12bone_fixture.py"
            )
        )
    )


def _run_blender_export(out: Path) -> None:
    cmd = [BLENDER, "--background", "--python", str(_export_script_path()), "--", str(out)]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert out.is_file()


def _spec_for_glb(glb: Path):
    decoded = decode_candidate_glb(glb)
    mins, maxs = visual_aabb_in_reference_root_frame(decoded)
    width_m, height_m, depth_m = envelope_size_from_aabb(mins, maxs)
    profile = load_packaged_candidate_profile()
    data = {
        "schema_version": "0.8.0-candidate",
        "asset_id": "humanoid_skin_workflow_01",
        "category": "character",
        "profile": "rigged_character",
        "profile_version": 1,
        "intent": "CLOSED candidate workflow integration for canonical Blender export",
        "source_kind": "local_verified_rig",
        "dimensions": {"width_m": width_m, "depth_m": depth_m, "height_m": height_m},
        "orientation": {"up": "+Y", "front": "-Z"},
        "origin_contract": "humanoid_reference_root",
        "rig_contract_id": "humanoid_12bone_v1",
        "visual_mesh_name": "SM_HumanoidSkin",
        "geometry_budget": {
            "max_triangles_lod0": 20000,
            "max_triangles_lod1": 10000,
            "lod_ratio": 0.5,
        },
        "material_budget": {"max_materials": 4},
        "texture_budget": {"max_dimension": 2048},
        "lod_policy": "lod0_only",
        "target_engine": "godot",
        "target_import_path": "assets/generated/character/humanoid_skin_workflow_01/",
        "collider": {"policy": "capsule", "capsule": {"radius_m": 0.1, "height_m": 0.5}},
        "profile_document_hash": profile_document_hash(profile.document),
        "processed_glb_sha256": hashlib.sha256(glb.read_bytes()).hexdigest(),
    }
    return parse_asset_specification_v08_candidate(data)


@pytest.mark.skipif(not Path(BLENDER).is_file(), reason="Blender executable not available")
@pytest.mark.skipif(not Path(GODOT).is_file(), reason="Godot executable not available")
@pytest.mark.real_godot
def test_candidate_workflow_reaches_test_only_review_and_receipt(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    root, db = workspace.root, workspace.db
    glb = root / "canonical.glb"
    _run_blender_export(glb)
    source_sha_before = hashlib.sha256(glb.read_bytes()).hexdigest()
    spec = _spec_for_glb(glb)
    workflow, tasks = create_v08_candidate_workflow(workspace, glb, spec)
    handlers = CandidateWorkflowHandlers(
        root,
        ArtifactRepository(db),
        AssetRevisionRepository(db),
        ApprovalRepository(db),
        ExecutionRepository(db),
        TaskRepository(db),
        ArtifactManager(root),
        godot_path=GODOT,
        runner=ProcessRunner(sanitize_output=True),
    )
    bind_production_candidate_evidence_exporter(handlers)
    engine = WorkflowEngine(
        root,
        db,
        policy_engine=PolicyEngine(
            PolicyRule(require_approval_for_paid=True, require_approval_for_process_execution=False)
        ),
        asset_provider=None,
    )
    register_v08_candidate_handlers(engine.handler_registry, handlers)
    engine.register_workflow(workflow, tasks, allow_existing_empty_placeholder=True)

    result = engine.run_workflow(workflow.id)
    for _ in range(40):
        if result.status == WorkflowStatus.BLOCKED and result.pending_approval_id:
            approval = ApprovalRepository(db).get(result.pending_approval_id)
            if approval and approval.approval_type == CANDIDATE_TEST_ONLY_APPROVAL:
                break
        if result.status in (WorkflowStatus.COMPLETED, WorkflowStatus.FAILED):
            break
        result = engine.run_workflow(workflow.id)

    assert result.pending_approval_id is not None
    approval = ApprovalRepository(db).get(result.pending_approval_id)
    assert approval is not None
    assert approval.approval_type == CANDIDATE_TEST_ONLY_APPROVAL
    assert ProviderInvocationRepository(db).count(workflow.id) == 0
    assert_zero_provider_activity(db, workflow.id)

    task = TaskRepository(db).get(approval.task_id)
    wf = engine.wf_repo.get(workflow.id)
    assert task is not None and wf is not None
    decided = ApprovalService.approve(
        approval,
        "integration-test-actor",
        current_inputs=engine.approval_inputs(wf, task),
    )
    ApprovalRepository(db).decide_if_pending(
        decided,
        AuditEvent(
            id=generate_id("AUDIT"),
            entity_type="Approval",
            entity_id=decided.id,
            action="APPROVED",
            actor="integration-test-actor",
            previous_state="PENDING",
            new_state="APPROVED",
        ),
    )
    after = engine.run_workflow(workflow.id)
    for _ in range(6):
        if after.status == WorkflowStatus.COMPLETED:
            break
        after = engine.run_workflow(workflow.id)
    assert_workflow_completed_or_diagnose(db, root, workflow.id, after)
    assert hashlib.sha256(glb.read_bytes()).hexdigest() == source_sha_before
    assert_zero_provider_activity(db, workflow.id)
    oracle_task = next(
        t
        for t in TaskRepository(db).list_by_workflow(workflow.id)
        if t.task_type == "v08_candidate_rig_oracle"
    )
    oracle_attempts = ExecutionRepository(db).list_by_task(oracle_task.id)
    assert len(oracle_attempts) == 1
    assert oracle_attempts[0].status.value == "COMPLETED"
    readiness = assert_candidate_evidence_complete(handlers, workflow.id)
    assert readiness.candidate_evidence_complete is True
    assert readiness.production_eligible is False
    assert readiness.promotion_eligible is False
    finalized = assert_candidate_workflow_engine_finalized(handlers, workflow.id)
    assert finalized.candidate_evidence_complete is True
    assert ProviderInvocationRepository(db).count(workflow.id) == 0
    markers = [
        a
        for a in ArtifactRepository(db).list_by_workflow(workflow.id)
        if a.artifact_type == "candidate-c2-export-marker"
    ]
    assert len(markers) == 1
    review = next(
        t
        for t in TaskRepository(db).list_by_workflow(workflow.id)
        if t.task_type == "v08_candidate_test_only_review"
    )
    assert review.status == TaskStatus.COMPLETED
    receipt_arts = [
        a
        for a in ArtifactRepository(db).list_by_workflow(workflow.id)
        if a.artifact_type == "candidate-test-only-receipt"
    ]
    assert receipt_arts
    receipt = json.loads((root / receipt_arts[-1].relative_path).read_text(encoding="utf-8"))
    assert receipt["promotion_eligible"] is False
    assert receipt["production_eligible"] is False
    assert receipt["receipt_scope"] == "candidate_test_only"
    copy_ci_candidate_evidence_if_requested(root, db, workflow.id)
