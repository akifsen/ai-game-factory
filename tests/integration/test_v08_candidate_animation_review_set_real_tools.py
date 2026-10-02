"""Real Blender + Godot integration for V0.8-8 multi-clip animation review set export."""

from __future__ import annotations

import hashlib
import json
import math
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
from gamefactory.core.domain.animation_clip import ANIMATION_CLIP_SCHEMA_VERSION
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
from gamefactory.workflows.v08_candidate_animation_review_set import (
    CandidateAnimationReviewSetSource,
    animation_review_set_current,
    export_rigged_character_animation_review_set,
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

_EXPECTED_REVIEW_SET_ROOT_FILES = frozenset(
    {
        "character.glb",
        "character.tscn",
        "candidate.json",
        "collider.json",
        "preview-manifest.json",
        "animation_clip_preview_player.gd",
        "animation_review_controller.gd",
        "animation_review_set_player.gd",
        "animation_review_set_controller.gd",
        "animation_review_set.tscn",
        "animation_review_set_preview.tscn",
        "animation_review_set_manifest.json",
        "project.godot",
    }
)


def _y_rotation_quaternion_xyzw(degrees: float) -> tuple[float, float, float, float]:
    half = math.radians(degrees) * 0.5
    return (0.0, math.sin(half), 0.0, math.cos(half))


def _authored_arm_reverse_02_clip_raw_bytes() -> bytes:
    identity = [0.0, 0.0, 0.0, 1.0]
    y_neg30 = list(_y_rotation_quaternion_xyzw(-30.0))
    doc = {
        "schema_version": ANIMATION_CLIP_SCHEMA_VERSION,
        "clip_id": "arm_reverse_02",
        "duration_seconds": 2.0,
        "loop": True,
        "tracks": [
            {
                "bone": "Spine",
                "keyframes": [
                    {"time": 0.0, "rotation_xyzw": identity},
                    {"time": 0.75, "rotation_xyzw": y_neg30},
                    {"time": 1.5, "rotation_xyzw": identity},
                ],
            },
        ],
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


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
        "asset_id": "humanoid_skin_animation_review_set_integration_01",
        "category": "character",
        "profile": "rigged_character",
        "profile_version": 1,
        "intent": "CLOSED candidate animation review set integration for canonical Blender export",
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
        "target_import_path": (
            "assets/generated/character/humanoid_skin_animation_review_set_integration_01/"
        ),
        "collider": {"policy": "capsule", "capsule": {"radius_m": 0.1, "height_m": 0.5}},
        "profile_document_hash": profile_document_hash(profile.document),
        "processed_glb_sha256": hashlib.sha256(glb.read_bytes()).hexdigest(),
    }
    return parse_asset_specification_v08_candidate(data)


def _copy_review_set_package(source: Path, destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    return destination


def _review_set_leaf_paths(review_dir: Path) -> list[Path]:
    paths: list[Path] = [review_dir / name for name in sorted(_EXPECTED_REVIEW_SET_ROOT_FILES)]
    for slot in ("000", "001"):
        for name in ("animation_clip.json", "animation_clip_manifest.json"):
            paths.append(review_dir / "clips" / slot / name)
    return paths


def _godot_harness_strip_main_scene(project_godot: Path) -> None:
    text = project_godot.read_text(encoding="utf-8")
    stripped = re.sub(r"^run/main_scene=.*\n", "", text, flags=re.MULTILINE)
    if stripped != text:
        project_godot.write_text(stripped, encoding="utf-8", newline="\n")


def _stage_review_set_inspect(package_dir: Path) -> None:
    inspect = _V08_PACKAGE.joinpath("animation_review_set_inspect.gd").read_bytes()
    (package_dir / "animation_review_set_inspect.gd").write_bytes(inspect)


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


def _run_godot_review_set_inspect(
    review_dir: Path, runner: ProcessRunner
) -> subprocess.CompletedProcess[str]:
    _stage_review_set_inspect(review_dir)
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
            "res://animation_review_set_inspect.gd",
        ],
        cwd=review_dir,
        capture_output=True,
        text=True,
        timeout=360,
    )


def _coherent_drift_review_set_slot_a_duration(review_dir: Path) -> None:
    nested = review_dir / "clips/000/animation_clip.json"
    doc = json.loads(nested.read_bytes())
    doc["duration_seconds"] = 2.0
    nested.write_text(json.dumps(doc), encoding="utf-8")
    nested_manifest = review_dir / "clips/000/animation_clip_manifest.json"
    nested_manifest_doc = json.loads(nested_manifest.read_bytes())
    nested_manifest_doc["file_digests"]["animation_clip.json"] = sha256_file(nested)
    nested_manifest_doc["clip_sha256"] = sha256_file(nested)
    nested_manifest.write_text(json.dumps(nested_manifest_doc), encoding="utf-8")
    root_manifest_path = review_dir / "animation_review_set_manifest.json"
    root_manifest = json.loads(root_manifest_path.read_bytes())
    ordered = list(root_manifest["ordered_clips"])
    ordered[0] = dict(ordered[0])
    ordered[0]["raw_clip_sha256"] = sha256_file(nested)
    ordered[0]["raw_original_manifest_sha256"] = sha256_file(nested_manifest)
    root_manifest["ordered_clips"] = ordered
    clip_digests = {
        path: sha256_file(review_dir / path)
        for path in (
            "clips/000/animation_clip.json",
            "clips/000/animation_clip_manifest.json",
            "clips/001/animation_clip.json",
            "clips/001/animation_clip_manifest.json",
        )
    }
    root_manifest["nested_clips_payload_digest"] = hashlib.sha256(
        json.dumps(clip_digests, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    ).hexdigest()
    root_manifest_path.write_bytes(
        json.dumps(root_manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    )


@pytest.mark.skipif(not Path(BLENDER).is_file(), reason="Blender executable not available")
@pytest.mark.skipif(not Path(GODOT).is_file(), reason="Godot executable not available")
@pytest.mark.real_godot
def test_real_workflow_exports_animation_review_set_and_godot_validates(
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
        "integration-animation-review-set-actor",
        current_inputs=engine.approval_inputs(wf, task),
    )
    ApprovalRepository(db).decide_if_pending(
        decided,
        AuditEvent(
            id=generate_id("AUDIT"),
            entity_type="Approval",
            entity_id=decided.id,
            action="APPROVED",
            actor="integration-animation-review-set-actor",
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
    clip_a_path = tmp_path / "arm_wave_01.json"
    clip_b_path = tmp_path / "arm_reverse_02.json"
    clip_a_path.write_bytes(acceptance_arm_wave_clip_raw_bytes())
    clip_b_path.write_bytes(_authored_arm_reverse_02_clip_raw_bytes())
    caller_clip_a_before = clip_a_path.read_bytes()
    caller_clip_b_before = clip_b_path.read_bytes()

    clip_preview_a = tmp_path / "candidate-clip-preview-a"
    clip_preview_b = tmp_path / "candidate-clip-preview-b"
    export_rigged_character_animation_clip_preview(
        handlers, workflow.id, preview_dir, clip_a_path, clip_preview_a
    )
    export_rigged_character_animation_clip_preview(
        handlers, workflow.id, preview_dir, clip_b_path, clip_preview_b
    )
    v086_a_before = {
        name: (clip_preview_a / name).read_bytes() for name in sorted(_CLIP_PACKAGE_FILES)
    }
    v086_b_before = {
        name: (clip_preview_b / name).read_bytes() for name in sorted(_CLIP_PACKAGE_FILES)
    }

    sources = (
        CandidateAnimationReviewSetSource(clip_preview_a, clip_a_path),
        CandidateAnimationReviewSetSource(clip_preview_b, clip_b_path),
    )
    review_set_dir = tmp_path / "candidate-animation-review-set-out"
    review_result = export_rigged_character_animation_review_set(
        handlers,
        workflow.id,
        preview_dir,
        sources,
        review_set_dir,
    )
    assert review_result.production_eligible is False
    assert review_result.promotion_eligible is False
    assert review_result.ordered_clip_ids == ("arm_wave_01", "arm_reverse_02")

    assert {p.name for p in review_set_dir.iterdir()} == _EXPECTED_REVIEW_SET_ROOT_FILES | {"clips"}
    assert len(_review_set_leaf_paths(review_set_dir)) == 17
    for slot, clip_preview, clip_path in (
        ("000", clip_preview_a, clip_a_path),
        ("001", clip_preview_b, clip_b_path),
    ):
        slot_dir = review_set_dir / "clips" / slot
        assert (slot_dir / "animation_clip.json").read_bytes() == (
            clip_preview / "animation_clip.json"
        ).read_bytes()
        assert (slot_dir / "animation_clip.json").read_bytes() == clip_path.read_bytes()
        assert (slot_dir / "animation_clip_manifest.json").read_bytes() == (
            clip_preview / "animation_clip_manifest.json"
        ).read_bytes()

    manifest = json.loads(
        (review_set_dir / "animation_review_set_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.get("production_eligible") is False
    assert manifest.get("promotion_eligible") is False
    ordered = manifest["ordered_clips"]
    assert [entry["clip_id"] for entry in ordered] == ["arm_wave_01", "arm_reverse_02"]
    for index, (clip_path, clip_preview) in enumerate(
        ((clip_a_path, clip_preview_a), (clip_b_path, clip_preview_b))
    ):
        entry = ordered[index]
        assert entry["raw_clip_sha256"] == hashlib.sha256(clip_path.read_bytes()).hexdigest()
        assert entry["raw_original_manifest_sha256"] == sha256_file(
            clip_preview / "animation_clip_manifest.json"
        )
        assert entry["raw_clip_sha256"] == sha256_file(
            review_set_dir / f"clips/{index:03d}/animation_clip.json"
        )
        assert entry["raw_original_manifest_sha256"] == sha256_file(
            review_set_dir / f"clips/{index:03d}/animation_clip_manifest.json"
        )

    for name, payload in v086_a_before.items():
        assert (clip_preview_a / name).read_bytes() == payload
    for name, payload in v086_b_before.items():
        assert (clip_preview_b / name).read_bytes() == payload
    assert clip_a_path.read_bytes() == caller_clip_a_before
    assert clip_b_path.read_bytes() == caller_clip_b_before

    assert candidate_preview_current(handlers, workflow.id, preview_dir) is True
    assert (
        animation_clip_preview_current(
            handlers, workflow.id, preview_dir, clip_a_path, clip_preview_a
        )
        is True
    )
    assert (
        animation_clip_preview_current(
            handlers, workflow.id, preview_dir, clip_b_path, clip_preview_b
        )
        is True
    )
    assert (
        animation_review_set_current(handlers, workflow.id, preview_dir, sources, review_set_dir)
        is True
    )

    review_set_byte_snapshot = {
        path.relative_to(review_set_dir).as_posix(): path.read_bytes()
        for path in _review_set_leaf_paths(review_set_dir)
    }

    drift_review_set = _copy_review_set_package(
        review_set_dir, tmp_path / "review-set-coherent-nested-drift"
    )
    _coherent_drift_review_set_slot_a_duration(drift_review_set)
    assert (
        animation_clip_preview_current(
            handlers, workflow.id, preview_dir, clip_a_path, clip_preview_a
        )
        is True
    )
    assert (
        animation_review_set_current(handlers, workflow.id, preview_dir, sources, drift_review_set)
        is False
    )

    runner = ProcessRunner(sanitize_output=True)
    launch_copy = _copy_review_set_package(
        review_set_dir, tmp_path / "review-set-godot-launch-consumer"
    )
    assert (
        b'run/main_scene="res://animation_review_set.tscn"'
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
            "res://animation_review_set.tscn",
            "--quit-after",
            "96",
        ],
        cwd=launch_copy,
        capture_output=True,
        text=True,
        timeout=240,
    )
    launch_combined = launch.stdout + launch.stderr
    assert launch.returncode == 0, launch_combined[-2000:]
    _assert_no_script_errors(launch_combined)

    inspect_copy = _copy_review_set_package(
        review_set_dir, tmp_path / "review-set-godot-inspect-consumer"
    )
    _godot_harness_strip_main_scene(inspect_copy / "project.godot")
    assert "run/main_scene=" not in (inspect_copy / "project.godot").read_text(encoding="utf-8")
    inspect_result = _run_godot_review_set_inspect(inspect_copy, runner)
    combined = inspect_result.stdout + inspect_result.stderr
    assert inspect_result.returncode == 0, combined[-3000:]
    _assert_no_script_errors(combined)
    assert "PASS: animation_review_set_interactive" in inspect_result.stdout

    assert not (review_set_dir / ".godot").exists()
    for rel, payload in review_set_byte_snapshot.items():
        assert (review_set_dir / rel).read_bytes() == payload
    for name, payload in v086_a_before.items():
        assert (clip_preview_a / name).read_bytes() == payload
    for name, payload in v086_b_before.items():
        assert (clip_preview_b / name).read_bytes() == payload
    assert clip_a_path.read_bytes() == caller_clip_a_before
    assert clip_b_path.read_bytes() == caller_clip_b_before
    assert (
        animation_review_set_current(handlers, workflow.id, preview_dir, sources, review_set_dir)
        is True
    )
