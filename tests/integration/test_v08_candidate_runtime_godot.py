"""Real Godot V0.8-3B candidate capsule runtime (nine views + physics)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from importlib import resources
from pathlib import Path

import pytest

from gamefactory.adapters.assets.v08_candidate_runtime_digest import (
    candidate_runtime_bound_payload_canonical,
    candidate_runtime_request_digest,
)
from gamefactory.adapters.assets.v08_candidate_runtime_pins import packaged_candidate_harness_path
from gamefactory.adapters.assets.v08_candidate_runtime_request import (
    build_bound_candidate_runtime_request,
)
from gamefactory.adapters.assets.v08_candidate_runtime_verify import (
    CandidateRuntimeObservationError,
    verify_candidate_runtime_observation,
)
from gamefactory.adapters.engines.v08_candidate_runtime_runner import (
    run_v08_candidate_capsule_runtime,
)
from gamefactory.adapters.fakes.humanoid_skin_fixture import build_humanoid_skinned_glb
from gamefactory.core.domain.v08_candidate_contracts import (
    load_packaged_candidate_profile,
    parse_asset_specification_v08_candidate,
)
from gamefactory.core.domain.v08_candidate_runtime_integers import JSON_SAFE_INTEGER_MAX
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner

GODOT = os.environ.get(
    "GAMEFACTORY_TEST_GODOT",
    r"C:\Users\lenovo\devel\godot\Godot_v4.7.2-stable_win64.exe\Godot_v4.7.2-stable_win64_console.exe",
)
BLENDER = os.environ.get(
    "GAMEFACTORY_TEST_BLENDER",
    r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe",
)
HAS_DISPLAY = bool(os.environ.get("DISPLAY")) if os.name != "nt" else True


def _linux_display_env_overrides() -> dict[str, str]:
    """Forward display-related env for non-headless Godot script runs (matches production runner)."""
    render_env: dict[str, str] = {}
    if sys.platform.startswith("linux"):
        display = os.environ.get("DISPLAY")
        if display:
            render_env["DISPLAY"] = display
        for name in ("XAUTHORITY", "LIBGL_ALWAYS_SOFTWARE"):
            if value := os.environ.get(name):
                render_env[name] = value
    return render_env


def _spec_for_glb(glb: Path):
    from gamefactory.core.domain.v08_candidate_contracts import (
        load_packaged_candidate_specification,
    )

    data = load_packaged_candidate_specification().model_dump(mode="json")
    data["processed_glb_sha256"] = hashlib.sha256(glb.read_bytes()).hexdigest()
    return parse_asset_specification_v08_candidate(data)


def _export_script_path() -> Path:
    return Path(
        str(
            resources.files("gamefactory.resources.blender").joinpath(
                "export_humanoid_12bone_fixture.py"
            )
        )
    )


def _run_blender_export(out: Path) -> None:
    cmd = [
        BLENDER,
        "--background",
        "--python",
        str(_export_script_path()),
        "--",
        str(out),
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert out.is_file()


@pytest.fixture
def fake_glb(tmp_path: Path) -> Path:
    path = tmp_path / "humanoid.glb"
    path.write_bytes(build_humanoid_skinned_glb("positive"))
    return path


@pytest.mark.skipif(not Path(GODOT).is_file() or not HAS_DISPLAY, reason="needs Godot and display")
def test_fake_glb_candidate_runtime_nine_views_and_digest(fake_glb: Path, tmp_path: Path) -> None:
    spec = _spec_for_glb(fake_glb)
    profile = load_packaged_candidate_profile()
    harness = Path(
        str(resources.files("gamefactory.resources.godot").joinpath("candidate_capsule_harness.gd"))
    )
    runner = ProcessRunner(sanitize_output=True)
    bound = build_bound_candidate_runtime_request(
        fake_glb,
        spec,
        profile,
        workflow_id="WF-V083B-FAKE",
        revision=1,
        execution_id="EXEC-V083B-FAKE",
        strict_attempt_number=1,
        godot_executable=Path(GODOT),
        runner=runner,
        harness_path=harness,
    )
    observation = run_v08_candidate_capsule_runtime(
        Path(GODOT),
        fake_glb,
        spec,
        profile,
        tmp_path / "out-fake",
        workflow_id="WF-V083B-FAKE",
        revision=1,
        execution_id="EXEC-V083B-FAKE",
        strict_attempt_number=1,
        runner=runner,
    )
    assert observation["status"] == "PASS"
    assert observation["request_digest"] == bound["request_digest"]
    assert observation["request_digest"] == candidate_runtime_request_digest(bound)
    assert len(observation["captures"]) == 9
    assert observation["physics_ray_hit"] is True
    assert observation["observed_glb_sha256"] == hashlib.sha256(fake_glb.read_bytes()).hexdigest()


@pytest.mark.skipif(
    not Path(GODOT).is_file() or not Path(BLENDER).is_file() or not HAS_DISPLAY,
    reason="needs Blender, Godot, and display",
)
def test_blender_glb_candidate_runtime(tmp_path: Path) -> None:
    from gamefactory.adapters.assets.v08_candidate_geometry import (
        envelope_size_from_aabb,
        visual_aabb_in_reference_root_frame,
    )
    from gamefactory.adapters.assets.v08_candidate_rules import decode_candidate_glb
    from gamefactory.core.domain.v08_candidate_contracts import profile_document_hash

    blender_out = tmp_path / "blender_humanoid.glb"
    _run_blender_export(blender_out)
    decoded = decode_candidate_glb(blender_out)
    mins, maxs = visual_aabb_in_reference_root_frame(decoded)
    width_m, height_m, depth_m = envelope_size_from_aabb(mins, maxs)
    profile = load_packaged_candidate_profile()
    data = {
        "schema_version": "0.8.0-candidate",
        "asset_id": "humanoid_skin_blender_runtime_01",
        "category": "character",
        "profile": "rigged_character",
        "profile_version": 1,
        "intent": "CLOSED candidate runtime acceptance for Blender export",
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
        "target_import_path": "assets/generated/character/humanoid_skin_blender_runtime_01/",
        "collider": {"policy": "capsule", "capsule": {"radius_m": 0.1, "height_m": 0.5}},
        "profile_document_hash": profile_document_hash(profile.document),
        "processed_glb_sha256": hashlib.sha256(blender_out.read_bytes()).hexdigest(),
    }
    spec = parse_asset_specification_v08_candidate(data)
    profile = load_packaged_candidate_profile()
    runner = ProcessRunner(sanitize_output=True)
    observation = run_v08_candidate_capsule_runtime(
        Path(GODOT),
        blender_out,
        spec,
        profile,
        tmp_path / "out-blender",
        workflow_id="WF-V083B-BLENDER",
        revision=1,
        execution_id="EXEC-V083B-BLENDER",
        strict_attempt_number=1,
        runner=runner,
    )
    assert observation["status"] == "PASS"
    assert len(observation["captures"]) == 9


def _stage_and_run_godot(
    godot: Path,
    glb: Path,
    bound: dict,
    tmp_path: Path,
    *,
    runner: ProcessRunner,
) -> dict:
    """Isolated Godot stage using the packaged harness and a caller-supplied bound request."""
    glb_bytes = glb.read_bytes()
    stage = tmp_path / f"godot_manual_{uuid.uuid4().hex}"
    stage.mkdir(parents=True)
    (stage / "asset.glb").write_bytes(glb_bytes)
    (stage / "Main.tscn").write_text(
        """[gd_scene load_steps=2 format=3]

[ext_resource type="PackedScene" path="res://asset.glb" id="1"]

[node name="Root" type="Node3D"]

[node name="Asset" parent="." instance=ExtResource("1")]
""",
        encoding="utf-8",
    )
    harness = packaged_candidate_harness_path()
    shutil.copyfile(harness, stage / "candidate_capsule_harness.gd")
    capture_dir = stage / "captures"
    capture_dir.mkdir()
    observation_path = stage / "observation.json"
    stage_request = {
        **bound,
        "glb": "res://Main.tscn",
        "capture_dir": str(capture_dir),
        "observation_path": str(observation_path),
    }
    request_path = stage / "request.json"
    request_path.write_text(json.dumps(stage_request, sort_keys=True), encoding="utf-8")
    (stage / "project.godot").write_text(
        "\n".join(
            [
                "config_version=5",
                "",
                "[application]",
                'config/name="V08 Candidate Capsule Runtime"',
                "",
                "[display]",
                "window/size/viewport_width=1280",
                "window/size/viewport_height=720",
                "",
                "[rendering]",
                'renderer/rendering_method="gl_compatibility"',
            ]
        ),
        encoding="utf-8",
    )
    display_env = _linux_display_env_overrides()
    import_result = runner.run(
        CommandRequest(
            args=[str(godot), "--headless", "--path", str(stage), "--import", "--quit"],
            cwd=stage,
            timeout_seconds=180.0,
            env_overrides=display_env,
        )
    )
    assert import_result.exit_code == 0, import_result.stderr[-1500:]
    script_result = runner.run(
        CommandRequest(
            args=[
                str(godot),
                "--path",
                str(stage),
                "--script",
                "res://candidate_capsule_harness.gd",
                "--",
                "--request",
                str(request_path),
            ],
            cwd=stage,
            timeout_seconds=180.0,
            env_overrides=display_env,
        )
    )
    assert observation_path.is_file(), script_result.stderr[-1500:]
    return json.loads(observation_path.read_text(encoding="utf-8"))


@pytest.mark.skipif(not Path(GODOT).is_file() or not HAS_DISPLAY, reason="needs Godot and display")
def test_godot_emits_json_safe_integer_counters_at_max(fake_glb: Path, tmp_path: Path) -> None:
    spec = _spec_for_glb(fake_glb)
    profile = load_packaged_candidate_profile()
    runner = ProcessRunner(sanitize_output=True)
    observation = run_v08_candidate_capsule_runtime(
        Path(GODOT),
        fake_glb,
        spec,
        profile,
        tmp_path / "out-nearmax",
        workflow_id="WF-V083B-NEARMAX",
        revision=JSON_SAFE_INTEGER_MAX,
        execution_id="EXEC-V083B-NEARMAX",
        strict_attempt_number=JSON_SAFE_INTEGER_MAX,
        runner=runner,
    )
    assert observation["status"] == "PASS"
    assert observation["revision"] == JSON_SAFE_INTEGER_MAX
    assert observation["strict_attempt_number"] == JSON_SAFE_INTEGER_MAX
    assert observation["captures"][0]["revision"] == JSON_SAFE_INTEGER_MAX
    assert type(observation["revision"]) is int


@pytest.mark.skipif(not Path(GODOT).is_file() or not HAS_DISPLAY, reason="needs Godot and display")
def test_godot_wrong_capsule_center_fails_ray_and_verify_rejects(
    fake_glb: Path, tmp_path: Path
) -> None:
    spec = _spec_for_glb(fake_glb)
    profile = load_packaged_candidate_profile()
    harness = packaged_candidate_harness_path()
    runner = ProcessRunner(sanitize_output=True)
    bound = build_bound_candidate_runtime_request(
        fake_glb,
        spec,
        profile,
        workflow_id="WF-V083B-WRONG-CENTER",
        revision=1,
        execution_id="EXEC-V083B-WRONG-CENTER",
        strict_attempt_number=1,
        godot_executable=Path(GODOT),
        runner=runner,
        harness_path=harness,
    )
    wrong_center = list(bound["capsule_center_m"])
    wrong_center[0] += 1.5
    wrong_bound = dict(bound)
    wrong_bound["capsule_center_m"] = wrong_center
    wrong_bound["bound_payload_canonical"] = candidate_runtime_bound_payload_canonical(wrong_bound)
    wrong_bound["request_digest"] = candidate_runtime_request_digest(wrong_bound)
    observation = _stage_and_run_godot(Path(GODOT), fake_glb, wrong_bound, tmp_path, runner=runner)
    assert observation["status"] == "FAIL"
    assert observation["physics_ray_hit"] is False
    assert any("physics ray" in err for err in observation.get("errors", []))
    center_obs = observation["capsule"]["center_m"]
    assert abs(center_obs[0] - wrong_center[0]) < 1e-4
    with pytest.raises(CandidateRuntimeObservationError, match="status"):
        verify_candidate_runtime_observation(
            observation,
            bound,
            spec=spec,
            profile=profile,
            capture_dir=tmp_path / "empty-captures",
            glb_path=fake_glb,
            glb_bytes=fake_glb.read_bytes(),
            harness_path=harness,
            process_exit_code=1,
        )
