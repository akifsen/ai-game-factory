"""Real Blender + Godot integration for V0.8-5 candidate animation preview export."""

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
from gamefactory.core.domain.models import AuditEvent, WorkflowStatus, generate_id
from gamefactory.core.domain.v08_candidate_contracts import (
    load_packaged_candidate_profile,
    parse_asset_specification_v08_candidate,
    profile_document_hash,
)
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner
from gamefactory.core.policies.policy_engine import PolicyEngine, PolicyRule
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.v08_candidate_animation_preview import (
    animation_preview_current,
    export_rigged_character_animation_preview,
)
from gamefactory.workflows.v08_candidate_currentness import assert_zero_provider_activity
from gamefactory.workflows.v08_candidate_evidence_readiness import (
    assert_candidate_workflow_engine_finalized,
)
from gamefactory.workflows.v08_candidate_preview import (
    candidate_preview_current,
    export_rigged_character_candidate_preview,
)
from gamefactory.workflows.v08_candidate_snapshot import CANDIDATE_TEST_ONLY_APPROVAL
from gamefactory.workflows.v08_candidate_workflow import (
    CandidateWorkflowHandlers,
    create_fresh_v08_candidate_workspace,
    create_v08_candidate_workflow,
    register_v08_candidate_handlers,
)
from tests.helpers.v08_candidate_workflow_failure_diagnostic import (
    assert_workflow_completed_or_diagnose,
)

BLENDER = os.environ.get(
    "GAMEFACTORY_TEST_BLENDER",
    r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe",
)
GODOT = os.environ.get(
    "GAMEFACTORY_TEST_GODOT",
    r"C:\Users\lenovo\devel\godot\Godot_v4.7.2-stable_win64.exe\Godot_v4.7.2-stable_win64_console.exe",
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
        "asset_id": "humanoid_skin_animation_integration_01",
        "category": "character",
        "profile": "rigged_character",
        "profile_version": 1,
        "intent": "CLOSED candidate animation preview integration for canonical Blender export",
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
        "target_import_path": "assets/generated/character/humanoid_skin_animation_integration_01/",
        "collider": {"policy": "capsule", "capsule": {"radius_m": 0.1, "height_m": 0.5}},
        "profile_document_hash": profile_document_hash(profile.document),
        "processed_glb_sha256": hashlib.sha256(glb.read_bytes()).hexdigest(),
    }
    return parse_asset_specification_v08_candidate(data)


def _inspect_script_path() -> Path:
    return Path(
        str(
            resources.files("gamefactory.resources.v08_candidate").joinpath(
                "animation_preview_inspect.gd"
            )
        )
    )


def _copy_animation_package(source: Path, destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    return destination


def _godot_harness_project_without_main_loop(project_godot: Path) -> None:
    text = project_godot.read_text(encoding="utf-8")
    stripped = text.replace('run/main_scene="res://animation_preview.tscn"\n', "")
    if stripped != text:
        project_godot.write_text(stripped, encoding="utf-8", newline="\n")


def _run_godot_animation_inspect(preview_dir: Path, runner: ProcessRunner):
    shutil.copy2(_inspect_script_path(), preview_dir / "animation_preview_inspect.gd")
    import_result = runner.run(
        CommandRequest(
            args=[str(GODOT), "--headless", "--path", str(preview_dir), "--import", "--quit"],
            cwd=preview_dir,
            timeout_seconds=180.0,
        )
    )
    assert import_result.exit_code == 0, import_result.stderr[-1500:]
    cmd = [
        str(GODOT),
        "--rendering-method",
        "gl_compatibility",
        "--audio-driver",
        "Dummy",
        "--path",
        str(preview_dir),
        "--script",
        "res://animation_preview_inspect.gd",
    ]
    completed = subprocess.run(
        cmd,
        cwd=preview_dir,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return completed


@pytest.mark.skipif(not Path(BLENDER).is_file(), reason="Blender executable not available")
@pytest.mark.skipif(not Path(GODOT).is_file(), reason="Godot executable not available")
@pytest.mark.real_godot
def test_real_workflow_exports_animation_preview_and_godot_plays_clip(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    root, db = workspace.root, workspace.db
    glb = root / "canonical.glb"
    _run_blender_export(glb)
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
    task = TaskRepository(db).get(approval.task_id)
    wf = engine.wf_repo.get(workflow.id)
    assert task is not None and wf is not None
    decided = ApprovalService.approve(
        approval,
        "integration-animation-preview-actor",
        current_inputs=engine.approval_inputs(wf, task),
    )
    ApprovalRepository(db).decide_if_pending(
        decided,
        AuditEvent(
            id=generate_id("AUDIT"),
            entity_type="Approval",
            entity_id=decided.id,
            action="APPROVED",
            actor="integration-animation-preview-actor",
            previous_state="PENDING",
            new_state="APPROVED",
        ),
    )
    after = engine.run_workflow(workflow.id)
    for _ in range(8):
        if after.status == WorkflowStatus.COMPLETED:
            break
        after = engine.run_workflow(workflow.id)
    assert_workflow_completed_or_diagnose(db, root, workflow.id, after)
    assert_zero_provider_activity(db, workflow.id)
    assert ProviderInvocationRepository(db).count(workflow.id) == 0
    assert_candidate_workflow_engine_finalized(handlers, workflow.id)

    preview_dir = tmp_path / "candidate-preview-out"
    export_rigged_character_candidate_preview(handlers, workflow.id, preview_dir)
    animation_dir = tmp_path / "candidate-animation-out"
    export_result = export_rigged_character_animation_preview(
        handlers, workflow.id, preview_dir, animation_dir
    )
    assert export_result.production_eligible is False
    assert (animation_dir / "character.glb").read_bytes() == glb.read_bytes()
    assert candidate_preview_current(handlers, workflow.id, preview_dir) is True
    assert animation_preview_current(handlers, workflow.id, preview_dir, animation_dir) is True

    runner = ProcessRunner(sanitize_output=True)
    godot_copy = _copy_animation_package(animation_dir, tmp_path / "candidate-animation-godot-copy")
    _godot_harness_project_without_main_loop(godot_copy / "project.godot")
    script_result = _run_godot_animation_inspect(godot_copy, runner)
    assert script_result.returncode == 0, script_result.stdout + script_result.stderr
    assert "PASS" in (script_result.stdout or "")

    for bone in ("Spine", "RightUpperArm", "NotInContract"):
        negative_copy = _copy_animation_package(
            animation_dir, tmp_path / f"candidate-animation-negative-{bone}"
        )
        _godot_harness_project_without_main_loop(negative_copy / "project.godot")
        spec_path = negative_copy / "animation_spec.json"
        spec_doc = json.loads(spec_path.read_text(encoding="utf-8"))
        spec_doc["bone_name"] = bone
        spec_path.write_text(json.dumps(spec_doc), encoding="utf-8")
        negative_result = _run_godot_animation_inspect(negative_copy, runner)
        assert negative_result.returncode != 0
        assert "FAIL" in negative_result.stdout + negative_result.stderr

    wrong_track = _copy_animation_package(animation_dir, tmp_path / "wrong-track")
    _godot_harness_project_without_main_loop(wrong_track / "project.godot")
    player_path = wrong_track / "animation_preview_player.gd"
    player_text = player_path.read_text(encoding="utf-8")
    tampered = player_text.replace(
        'NodePath(str(skel_path) + ":" + bone_name)',
        'NodePath(str(skel_path) + ":Spine")',
    )
    assert tampered != player_text
    player_path.write_text(tampered, encoding="utf-8")
    negative_result = _run_godot_animation_inspect(wrong_track, runner)
    assert negative_result.returncode != 0
    assert "canonical track targets wrong skeleton/bone" in negative_result.stdout
    assert candidate_preview_current(handlers, workflow.id, preview_dir) is True
    assert animation_preview_current(handlers, workflow.id, preview_dir, animation_dir) is True
