"""Real Blender + Godot integration for V0.8-7 animation review export."""

from __future__ import annotations

import hashlib
import json
import os
import re
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
from gamefactory.adapters.engines.godot_staging import sha256_file
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
from gamefactory.workflows.v08_candidate_animation_clip_preview import (
    _CLIP_PACKAGE_FILES,
    acceptance_arm_wave_clip_raw_bytes,
    animation_clip_preview_current,
    export_rigged_character_animation_clip_preview,
)
from gamefactory.workflows.v08_candidate_animation_review import (
    animation_review_current,
    export_rigged_character_animation_review,
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

_V08_PACKAGE = resources.files("gamefactory.resources.v08_candidate")

_EXPECTED_REVIEW_FILES = frozenset(
    {
        "character.glb",
        "character.tscn",
        "candidate.json",
        "collider.json",
        "preview-manifest.json",
        "animation_clip_preview.tscn",
        "animation_clip_preview_player.gd",
        "animation_clip.json",
        "animation_clip_manifest.json",
        "project.godot",
        "animation_review.tscn",
        "animation_review_controller.gd",
        "animation_review_manifest.json",
    }
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
        "asset_id": "humanoid_skin_animation_review_integration_01",
        "category": "character",
        "profile": "rigged_character",
        "profile_version": 1,
        "intent": "CLOSED candidate animation review integration for canonical Blender export",
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
        "target_import_path": "assets/generated/character/humanoid_skin_animation_review_integration_01/",
        "collider": {"policy": "capsule", "capsule": {"radius_m": 0.1, "height_m": 0.5}},
        "profile_document_hash": profile_document_hash(profile.document),
        "processed_glb_sha256": hashlib.sha256(glb.read_bytes()).hexdigest(),
    }
    return parse_asset_specification_v08_candidate(data)


def _copy_review_package(source: Path, destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    return destination


def _godot_harness_strip_main_scene(project_godot: Path) -> None:
    text = project_godot.read_text(encoding="utf-8")
    stripped = text.replace('run/main_scene="res://animation_clip_preview.tscn"\n', "")
    if stripped != text:
        project_godot.write_text(stripped, encoding="utf-8", newline="\n")


def _upstream_animation_package_sha256(package_dir: Path) -> str:
    digests = {
        name: hashlib.sha256((package_dir / name).read_bytes()).hexdigest()
        for name in sorted(_CLIP_PACKAGE_FILES)
    }
    canonical = json.dumps(digests, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _coherent_drift_review_embedded_v086_to_b(review_dir: Path) -> None:
    clip_path = review_dir / "animation_clip.json"
    doc = json.loads(clip_path.read_text(encoding="utf-8"))
    doc["duration_seconds"] = 2.0
    clip_path.write_text(json.dumps(doc), encoding="utf-8")
    clip_manifest_path = review_dir / "animation_clip_manifest.json"
    clip_manifest = json.loads(clip_manifest_path.read_text(encoding="utf-8"))
    clip_manifest["file_digests"]["animation_clip.json"] = sha256_file(clip_path)
    clip_manifest["clip_sha256"] = sha256_file(clip_path)
    clip_manifest_path.write_text(json.dumps(clip_manifest), encoding="utf-8")
    review_manifest_path = review_dir / "animation_review_manifest.json"
    review_manifest = json.loads(review_manifest_path.read_text(encoding="utf-8"))
    file_digests = dict(review_manifest["file_digests"])
    for name in sorted(_CLIP_PACKAGE_FILES):
        file_digests[name] = sha256_file(review_dir / name)
    review_manifest["file_digests"] = file_digests
    review_manifest["clip_sha256"] = clip_manifest["clip_sha256"]
    review_manifest["upstream_animation_manifest_sha256"] = sha256_file(clip_manifest_path)
    review_manifest["upstream_animation_package_sha256"] = _upstream_animation_package_sha256(
        review_dir
    )
    review_manifest_path.write_text(json.dumps(review_manifest), encoding="utf-8")


def _stage_review_inspect(package_dir: Path) -> None:
    inspect = _V08_PACKAGE.joinpath("animation_review_inspect.gd").read_bytes()
    (package_dir / "animation_review_inspect.gd").write_bytes(inspect)


def _assert_no_script_errors(combined_output: str) -> None:
    if "SCRIPT ERROR" in combined_output or "ParseError" in combined_output:
        pytest.fail(combined_output[-2500:])
    for line in combined_output.splitlines():
        if re.search(r"\bERROR:\s", line):
            pytest.fail(combined_output[-2500:])


def _run_godot_import(review_dir: Path, runner: ProcessRunner) -> None:
    import_result = runner.run(
        CommandRequest(
            args=[str(GODOT), "--headless", "--path", str(review_dir), "--import", "--quit"],
            cwd=review_dir,
            timeout_seconds=180.0,
        )
    )
    import_combined = (import_result.stdout or "") + (import_result.stderr or "")
    assert import_result.exit_code == 0, import_combined[-1500:]
    _assert_no_script_errors(import_combined)


def _run_godot_review_inspect(
    review_dir: Path, runner: ProcessRunner
) -> subprocess.CompletedProcess[str]:
    _stage_review_inspect(review_dir)
    _run_godot_import(review_dir, runner)
    return subprocess.run(
        [
            str(GODOT),
            "--rendering-method",
            "gl_compatibility",
            "--audio-driver",
            "Dummy",
            "--path",
            str(review_dir),
            "--script",
            "res://animation_review_inspect.gd",
        ],
        cwd=review_dir,
        capture_output=True,
        text=True,
        timeout=240,
    )


@pytest.mark.skipif(not Path(BLENDER).is_file(), reason="Blender executable not available")
@pytest.mark.skipif(not Path(GODOT).is_file(), reason="Godot executable not available")
@pytest.mark.real_godot
def test_real_workflow_exports_animation_review_and_godot_launches_scene(
    tmp_path: Path,
) -> None:
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
        "integration-animation-review-actor",
        current_inputs=engine.approval_inputs(wf, task),
    )
    ApprovalRepository(db).decide_if_pending(
        decided,
        AuditEvent(
            id=generate_id("AUDIT"),
            entity_type="Approval",
            entity_id=decided.id,
            action="APPROVED",
            actor="integration-animation-review-actor",
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
    clip_path = tmp_path / "arm_wave_01.json"
    clip_path.write_bytes(acceptance_arm_wave_clip_raw_bytes())
    clip_preview_dir = tmp_path / "candidate-clip-preview-out"
    export_rigged_character_animation_clip_preview(
        handlers, workflow.id, preview_dir, clip_path, clip_preview_dir
    )
    v086_digests = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in clip_preview_dir.iterdir()
    }
    review_dir = tmp_path / "candidate-animation-review-out"
    review_result = export_rigged_character_animation_review(
        handlers,
        workflow.id,
        preview_dir,
        clip_path,
        clip_preview_dir,
        review_dir,
    )
    assert review_result.production_eligible is False
    assert review_result.promotion_eligible is False
    for name, digest in v086_digests.items():
        assert hashlib.sha256((review_dir / name).read_bytes()).hexdigest() == digest
    assert candidate_preview_current(handlers, workflow.id, preview_dir) is True
    assert (
        animation_clip_preview_current(
            handlers, workflow.id, preview_dir, clip_path, clip_preview_dir
        )
        is True
    )
    assert (
        animation_review_current(
            handlers, workflow.id, preview_dir, clip_path, clip_preview_dir, review_dir
        )
        is True
    )

    review_byte_snapshot = {
        name: (review_dir / name).read_bytes() for name in _EXPECTED_REVIEW_FILES
    }
    clip_preview_byte_snapshot = {
        path.name: path.read_bytes() for path in clip_preview_dir.iterdir()
    }

    drift_review = _copy_review_package(review_dir, tmp_path / "review-coherent-v086-drift")
    _coherent_drift_review_embedded_v086_to_b(drift_review)
    assert (
        animation_clip_preview_current(
            handlers, workflow.id, preview_dir, clip_path, clip_preview_dir
        )
        is True
    )
    assert (
        animation_review_current(
            handlers, workflow.id, preview_dir, clip_path, clip_preview_dir, drift_review
        )
        is False
    )

    runner = ProcessRunner(sanitize_output=True)
    launch_copy = _copy_review_package(review_dir, tmp_path / "review-godot-launch-consumer")
    assert (
        b'run/main_scene="res://animation_clip_preview.tscn"'
        in (launch_copy / "project.godot").read_bytes()
    )
    _run_godot_import(launch_copy, runner)
    launch = subprocess.run(
        [
            str(GODOT),
            "--rendering-method",
            "gl_compatibility",
            "--audio-driver",
            "Dummy",
            "--path",
            str(launch_copy),
            "--scene",
            "res://animation_review.tscn",
            "--quit-after",
            "48",
        ],
        cwd=launch_copy,
        capture_output=True,
        text=True,
        timeout=180,
    )
    launch_combined = launch.stdout + launch.stderr
    assert launch.returncode == 0, launch_combined[-2000:]
    _assert_no_script_errors(launch_combined)

    inspect_copy = _copy_review_package(review_dir, tmp_path / "review-godot-inspect-consumer")
    _godot_harness_strip_main_scene(inspect_copy / "project.godot")
    inspect_result = _run_godot_review_inspect(inspect_copy, runner)
    combined = inspect_result.stdout + inspect_result.stderr
    assert inspect_result.returncode == 0, combined[-3000:]
    _assert_no_script_errors(combined)
    assert "PASS: animation_review_interactive" in inspect_result.stdout

    assert {p.name for p in review_dir.iterdir()} == _EXPECTED_REVIEW_FILES
    for name, payload in review_byte_snapshot.items():
        assert (review_dir / name).read_bytes() == payload
    for name, payload in clip_preview_byte_snapshot.items():
        assert (clip_preview_dir / name).read_bytes() == payload
    assert (
        animation_review_current(
            handlers, workflow.id, preview_dir, clip_path, clip_preview_dir, review_dir
        )
        is True
    )
