"""Focused unit tests for standalone V0.7 Godot assembly runtime verification."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.dcc.godot_assembly import (
    _MAX_PROCESSED_GLB_BYTES,
    OBSERVATION_SCHEMA_VERSION_V07,
    _bounded_processed_facts,
    _camera_up,
    _expected_socket_world,
    _load_harness_resource,
    _quaternion_matrix,
    _validate_observation,
    _validate_review_views,
    verify_godot_assembly,
)
from gamefactory.core.domain.asset_contracts import parse_asset_specification_v07
from gamefactory.core.domain.asset_profiles import (
    AssetProfileV07,
    ProfileRegistry,
    builtin_registry,
    parse_profile_document_v07,
)
from gamefactory.core.domain.camera_framing import view_axis_label, view_direction
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.execution.process_runner import CommandResult
from tests.integration.test_godot_assembly_real import (
    _build_tank_glb,
    _build_test_profile,
    _build_test_spec,
)


def _tank_profile(
    *, geometry_mode: str = "assembly", review_views: list[str] | None = None
) -> AssetProfileV07:
    base = builtin_registry().get("static_prop").document.model_dump(mode="json")
    if review_views is None:
        review_views = ["front", "three_quarter", "side"]
    doc: dict[str, Any] = {
        "schema_version": "asset-profile-0.7.0",
        "profile_id": "test_assembly_profile",
        "version": 1,
        "categories": ["vehicle"],
        "review_views": review_views,
        "framing": base["framing"],
        "dimension_rules": {"min_m": 0.5, "max_m": 10.0},
        "processing": {
            "lod0_required": True,
            "lod1_required": True,
            "allowed_lod_policies": ["lod0_lod1"],
            "allowed_collider_policies": ["box"],
            "allowed_origin_policies": ["center", "bottom_center"],
            "default_origin_policy": "center",
            "dimension_tolerance_m": 0.05,
            "snap_grid_m": None,
            "rig_forbidden": True,
            "animation_forbidden": True,
            "max_materials": 4,
            "max_texture_dimension": 2048,
            "max_triangles_lod0": 10000,
        },
        "godot": base["godot"],
        "runtime": base["runtime"],
        "geometry_mode": geometry_mode,
        "accepted_source_kinds": ["local_operator_assembly"]
        if geometry_mode == "assembly"
        else ["provider_generated"],
    }
    if geometry_mode == "assembly":
        doc["assembly"] = {
            "roles": ["hull", "turret", "barrel"],
            "required_roles": ["hull", "turret", "barrel"],
            "role_motion_constraints": {
                "turret": {"kind": "revolute", "axis": [0.0, 1.0, 0.0]},
                "barrel": {"kind": "revolute", "axis": [1.0, 0.0, 0.0]},
            },
            "required_sockets": [],
            "pivot_tolerance_m": 0.01,
            "basis_tolerance_deg": 1.0,
            "socket_position_tolerance_m": 0.01,
            "socket_angle_tolerance_deg": 1.0,
        }
    return AssetProfileV07(parse_profile_document_v07(doc))


def _tank_spec(profile: AssetProfileV07, *, include_parts: bool = True) -> Any:
    parts = []
    if include_parts:
        parts = [
            {
                "part_id": "hull",
                "role": "hull",
                "parent": "root",
                "pivot": {
                    "position_m": [0.0, -0.2, 0.0],
                    "basis": "identity",
                    "motion": {"kind": "fixed"},
                },
            },
            {
                "part_id": "turret",
                "role": "turret",
                "parent": "hull",
                "pivot": {
                    "position_m": [0.0, 0.4, 0.1],
                    "basis": "identity",
                    "motion": {"kind": "revolute", "axis": [0.0, 1.0, 0.0]},
                },
            },
            {
                "part_id": "barrel",
                "role": "barrel",
                "parent": "turret",
                "pivot": {
                    "position_m": [0.0, 0.2, -0.3],
                    "basis": "identity",
                    "motion": {"kind": "revolute", "axis": [1.0, 0.0, 0.0]},
                },
            },
        ]
    data: dict[str, Any] = {
        "schema_version": "0.7.0",
        "asset_id": "test_tank",
        "category": "vehicle",
        "profile": profile.profile_id,
        "profile_version": profile.version,
        "intent": "assembly unit test",
        "source_kind": "local_operator_assembly"
        if profile.geometry_mode == "assembly"
        else "provider_generated",
        "dimensions": {"width_m": 2.0, "height_m": 1.6, "depth_m": 3.0},
        "origin_policy": "center",
        "lod_policy": "lod0_lod1",
        "geometry_budget": {
            "max_triangles_lod0": 10000,
            "max_triangles_lod1": 5000,
            "lod_ratio": 0.5,
        },
        "material_budget": {"max_materials": 4},
        "texture_budget": {"max_dimension": 2048},
        "collider": {"policy": "box", "capsule": None},
    }
    if include_parts:
        data["parts"] = parts
    registry = ProfileRegistry(available=(), unsupported=(), available_v07=(profile,))
    return parse_asset_specification_v07(data, registry=registry)


def test_packaged_harness_resource_is_available() -> None:
    raw = _load_harness_resource()
    assert len(raw) > 1000
    assert b"SceneTree" in raw
    assert b"assembly-runtime-observation-0.7.0" in raw


def test_exact_bound_profile_is_required_before_processed_file_read(tmp_path: Path) -> None:
    profile = _tank_profile()
    spec = _tank_spec(profile)
    mismatched = _tank_profile(review_views=["front"])
    with pytest.raises(
        ValidationError, match="does not exactly match the specification-bound profile"
    ):
        verify_godot_assembly(spec, mismatched, tmp_path / "missing.glb", "0" * 64, "exec-profile")


def test_specification_source_kind_and_exact_types_are_required(tmp_path: Path) -> None:
    profile = _tank_profile()
    spec = _tank_spec(profile)
    wrong_source = spec.model_copy(update={"source_kind": "provider_generated"})
    with pytest.raises(ValidationError, match="source_kind 'local_operator_assembly'"):
        verify_godot_assembly(
            wrong_source, profile, tmp_path / "missing.glb", "0" * 64, "exec-source"
        )
    with pytest.raises(ValidationError, match="exact typed V0.7"):
        verify_godot_assembly(object(), profile, tmp_path / "missing.glb", "0" * 64, "exec-type")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("translation_offset", "yaw_degrees", "accepted"),
    [(0.001, 0.9, True), (0.011, 0.0, False), (0.0, 1.01, False)],
)
def test_part_source_tolerance_uses_bound_profile(
    tmp_path: Path, translation_offset: float, yaw_degrees: float, accepted: bool
) -> None:
    profile = _build_test_profile(review_views=["front"])
    spec = _build_test_spec(profile)
    glb_path = tmp_path / "pivot_tolerance.glb"
    _build_tank_glb(
        glb_path,
        turret_translation=[translation_offset, 0.4, 0.1],
        turret_yaw_deg=yaw_degrees,
    )
    if accepted:
        _bounded_processed_facts(glb_path.read_bytes(), spec, profile)
    else:
        reason = "local position" if translation_offset > 0.01 else "local basis"
        with pytest.raises(ValidationError, match=reason):
            _bounded_processed_facts(glb_path.read_bytes(), spec, profile)


def test_part_rotation_tolerance_is_quaternion_sign_invariant(tmp_path: Path) -> None:
    profile = _build_test_profile(review_views=["front"])
    yaw_degrees = 0.5
    half_yaw = math.radians(yaw_degrees) * 0.5
    positive_quaternion = [0.0, math.sin(half_yaw), 0.0, math.cos(half_yaw)]
    negative_quaternion = [-value for value in positive_quaternion]
    spec = _build_test_spec(profile, turret_basis=negative_quaternion)
    glb_path = tmp_path / "quaternion_sign.glb"
    _build_tank_glb(glb_path, turret_yaw_deg=yaw_degrees)
    _bounded_processed_facts(glb_path.read_bytes(), spec, profile)


@pytest.mark.parametrize(
    ("translation_offset", "rotation_degrees", "accepted"),
    [(0.009, 0.9, True), (0.011, 0.0, False), (0.0, 1.01, False)],
)
def test_socket_source_tolerance_uses_bound_profile(
    tmp_path: Path, translation_offset: float, rotation_degrees: float, accepted: bool
) -> None:
    profile = _build_test_profile(review_views=["front"])
    spec = _build_test_spec(profile)
    glb_path = tmp_path / "socket_tolerance.glb"
    _build_tank_glb(
        glb_path,
        muzzle_translation=[translation_offset, 0.0, -0.8],
        muzzle_rotation_deg=rotation_degrees,
    )
    if accepted:
        _bounded_processed_facts(glb_path.read_bytes(), spec, profile)
    else:
        reason = "local transform" if translation_offset > 0.01 else "local basis"
        with pytest.raises(ValidationError, match=reason):
            _bounded_processed_facts(glb_path.read_bytes(), spec, profile)


def test_processed_glb_over_50_mib_is_rejected_before_engine_dispatch(tmp_path: Path) -> None:
    profile = _tank_profile()
    spec = _tank_spec(profile)
    oversized = tmp_path / "oversized.glb"
    with oversized.open("wb") as stream:
        stream.seek(_MAX_PROCESSED_GLB_BYTES)
        stream.write(b"x")

    class RunnerSpy:
        called = False

        def run(self, cmd_req: Any) -> CommandResult:
            self.called = True
            raise AssertionError("oversized GLB reached Godot")

    runner = RunnerSpy()
    with pytest.raises(ValidationError, match="exceeds the 52428800-byte limit"):
        verify_godot_assembly(spec, profile, oversized, "0" * 64, "exec-oversize", runner=runner)
    assert runner.called is False


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("attempt_number", True, "attempt_number must be an integer"),
        ("revision", True, "revision must be an integer"),
        ("profile_version", True, "profile_version must be an integer"),
        ("revision", 2, "revision mismatch"),
        ("processed_glb_sha256", "0" * 64, "processed_glb_sha256 mismatch"),
        ("profile_id", "other", "profile_id mismatch"),
        ("profile_version", 2, "profile_version mismatch"),
        ("execution_id", "stale", "execution_id mismatch"),
    ],
)
def test_failed_observation_identity_is_strictly_bound(key: str, value: Any, message: str) -> None:
    from gamefactory.adapters.dcc.godot_assembly import _validate_observation

    profile = _tank_profile()
    spec = _tank_spec(profile)
    observation: dict[str, Any] = {
        "status": "FAIL",
        "errors": ["runtime finding"],
        "workflow_id": "assembly-verification",
        "execution_id": "exec-fail",
        "asset_id": spec.asset_id,
        "processed_glb_sha256": "a" * 64,
        "request_digest": "b" * 64,
        "revision": 1,
        "attempt_number": 1,
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "harness_sha256": "c" * 64,
        "profile_sha256": "d" * 64,
        "specification_sha256": "e" * 64,
    }
    observation[key] = value
    with pytest.raises(ValidationError, match=message):
        _validate_observation(
            observation,
            spec=spec,
            profile=profile,
            review_views=["front"],
            processed_hash="a" * 64,
            revision=1,
            workflow_id="assembly-verification",
            execution_id="exec-fail",
            attempt_number=1,
            request_digest="b" * 64,
            actual_facts={},
            harness_sha256="c" * 64,
            profile_sha256="d" * 64,
            specification_sha256="e" * 64,
        )


@pytest.mark.parametrize(
    ("views", "expected_err"),
    [
        ([], "non-empty"),
        (["unknown_angle"], "unknown review view identifier"),
        (["front", "front"], "duplicate review view identifier"),
        (["front", ""], "invalid review view identifier"),
        (["front", 123], "invalid review view identifier"),
    ],
)
def test_review_views_validation_rejects_invalid(views: Any, expected_err: str) -> None:
    with pytest.raises(ValidationError, match=expected_err):
        _validate_review_views(views)


def test_verify_assembly_rejects_non_assembly_profile(tmp_path: Path) -> None:
    single_prof = _tank_profile(geometry_mode="single_mesh", review_views=["front"])
    spec = _tank_spec(single_prof, include_parts=False)
    glb_path = tmp_path / "dummy.glb"
    _build_tank_glb(glb_path, asset_id="test_tank")
    sha = hashlib.sha256(glb_path.read_bytes()).hexdigest()

    with pytest.raises(ValidationError, match="only assembly geometry mode is supported"):
        verify_godot_assembly(
            spec=spec,
            profile=single_prof,
            processed_glb=glb_path,
            processed_glb_sha256=sha,
            execution_id="exec-01",
            output_dir=tmp_path / "out",
        )


def test_verify_assembly_rejects_hash_mismatch(tmp_path: Path) -> None:
    prof = _tank_profile()
    spec = _tank_spec(prof)
    glb_path = tmp_path / "dummy.glb"
    _build_tank_glb(glb_path, asset_id="test_tank")

    with pytest.raises(ValidationError, match="processed_glb SHA-256 mismatch"):
        verify_godot_assembly(
            spec=spec,
            profile=prof,
            processed_glb=glb_path,
            processed_glb_sha256="0000000000000000000000000000000000000000000000000000000000000000",
            execution_id="exec-01",
            output_dir=tmp_path / "out",
        )


def test_verify_assembly_clobber_guard_prevents_overwrite(tmp_path: Path) -> None:
    prof = _tank_profile()
    spec = _tank_spec(prof)
    glb_path = tmp_path / "dummy.glb"
    _build_tank_glb(glb_path, asset_id="test_tank")
    sha = hashlib.sha256(glb_path.read_bytes()).hexdigest()

    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True)
    # Stale observation already exists
    (out_dir / "runtime-observation.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValidationError, match="fresh, attempt-specific"):
        verify_godot_assembly(
            spec=spec,
            profile=prof,
            processed_glb=glb_path,
            processed_glb_sha256=sha,
            execution_id="exec-01",
            output_dir=out_dir,
        )


def test_verify_assembly_rejects_existing_empty_attempt_directory(tmp_path: Path) -> None:
    prof = _tank_profile()
    spec = _tank_spec(prof)
    glb_path = tmp_path / "dummy.glb"
    _build_tank_glb(glb_path, asset_id="test_tank")
    sha = hashlib.sha256(glb_path.read_bytes()).hexdigest()
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with pytest.raises(ValidationError, match="fresh, attempt-specific"):
        verify_godot_assembly(
            spec=spec,
            profile=prof,
            processed_glb=glb_path,
            processed_glb_sha256=sha,
            execution_id="exec-01",
            output_dir=out_dir,
        )


def test_verify_assembly_rejects_stale_observation_digest(tmp_path: Path) -> None:
    prof = _tank_profile()
    spec = _tank_spec(prof)
    glb_path = tmp_path / "dummy.glb"
    _build_tank_glb(glb_path, asset_id="test_tank")
    sha = hashlib.sha256(glb_path.read_bytes()).hexdigest()

    out_dir = tmp_path / "out"
    obs_path = out_dir / "runtime-observation.json"

    # Mock runner that writes observation with wrong request_digest
    class FakeRunner:
        def run(self, cmd_req: Any) -> CommandResult:
            if "--import" in cmd_req.args:
                return CommandResult(exit_code=0, stdout="", stderr="")
            # Staged harness run: create observation with mismatched digest
            obs = {
                "schema_version": OBSERVATION_SCHEMA_VERSION_V07,
                "workflow_id": "assembly-verification",
                "revision": 1,
                "asset_id": "test_tank",
                "execution_id": "exec-01",
                "attempt_number": 1,
                "processed_glb_sha256": sha,
                "request_digest": "stale_or_tampered_digest",
                "profile_id": prof.profile_id,
                "profile_version": prof.version,
                "harness_sha256": hashlib.sha256(_load_harness_resource()).hexdigest(),
                "profile_sha256": "0" * 64,
                "specification_sha256": "0" * 64,
                "status": "PASS",
                "errors": [],
            }
            obs_path.parent.mkdir(parents=True, exist_ok=True)
            obs_path.write_text(json.dumps(obs), encoding="utf-8")
            return CommandResult(exit_code=0, stdout="", stderr="")

    with pytest.raises(ValidationError, match="stale observation rejected"):
        verify_godot_assembly(
            spec=spec,
            profile=prof,
            processed_glb=glb_path,
            processed_glb_sha256=sha,
            execution_id="exec-01",
            output_dir=out_dir,
            godot_executable=glb_path,  # Any file that exists
            runner=FakeRunner(),
        )


def test_verify_assembly_rejects_corrupted_observation_json(tmp_path: Path) -> None:
    prof = _tank_profile()
    spec = _tank_spec(prof)
    glb_path = tmp_path / "dummy.glb"
    _build_tank_glb(glb_path, asset_id="test_tank")
    sha = hashlib.sha256(glb_path.read_bytes()).hexdigest()

    out_dir = tmp_path / "out"
    obs_path = out_dir / "runtime-observation.json"

    class CorruptRunner:
        def run(self, cmd_req: Any) -> CommandResult:
            if "--import" in cmd_req.args:
                return CommandResult(exit_code=0, stdout="", stderr="")
            obs_path.parent.mkdir(parents=True, exist_ok=True)
            obs_path.write_bytes(b"{not valid json:")
            return CommandResult(exit_code=0, stdout="", stderr="")

    with pytest.raises(ValidationError, match="not valid UTF-8 JSON"):
        verify_godot_assembly(
            spec=spec,
            profile=prof,
            processed_glb=glb_path,
            processed_glb_sha256=sha,
            execution_id="exec-01",
            output_dir=out_dir,
            godot_executable=glb_path,
            runner=CorruptRunner(),
        )


def test_verify_assembly_rejects_duplicate_keys_in_observation(tmp_path: Path) -> None:
    prof = _tank_profile()
    spec = _tank_spec(prof)
    glb_path = tmp_path / "dummy.glb"
    _build_tank_glb(glb_path, asset_id="test_tank")
    sha = hashlib.sha256(glb_path.read_bytes()).hexdigest()

    out_dir = tmp_path / "out"
    obs_path = out_dir / "runtime-observation.json"

    class DuplicateKeyRunner:
        def run(self, cmd_req: Any) -> CommandResult:
            if "--import" in cmd_req.args:
                return CommandResult(exit_code=0, stdout="", stderr="")
            obs_path.parent.mkdir(parents=True, exist_ok=True)
            obs_path.write_text('{"status": "PASS", "status": "FAIL"}', encoding="utf-8")
            return CommandResult(exit_code=0, stdout="", stderr="")

    with pytest.raises(ValidationError, match="duplicate JSON field: status"):
        verify_godot_assembly(
            spec=spec,
            profile=prof,
            processed_glb=glb_path,
            processed_glb_sha256=sha,
            execution_id="exec-01",
            output_dir=out_dir,
            godot_executable=glb_path,
            runner=DuplicateKeyRunner(),
        )


def test_verify_assembly_rejects_attempt_mismatch(tmp_path: Path) -> None:
    prof = _tank_profile()
    spec = _tank_spec(prof)
    glb_path = tmp_path / "dummy.glb"
    _build_tank_glb(glb_path, asset_id="test_tank")
    sha = hashlib.sha256(glb_path.read_bytes()).hexdigest()
    out_dir = tmp_path / "out"

    class WrongAttemptRunner:
        def run(self, cmd_req: Any) -> CommandResult:
            if "--import" in cmd_req.args:
                return CommandResult(exit_code=0, stdout="", stderr="")
            request_path = Path(cmd_req.args[cmd_req.args.index("--request") + 1])
            request = json.loads(request_path.read_text(encoding="utf-8"))
            observation = {
                "schema_version": OBSERVATION_SCHEMA_VERSION_V07,
                "workflow_id": request["workflow_id"],
                "revision": request["revision"],
                "asset_id": request["asset_id"],
                "execution_id": request["execution_id"],
                "attempt_number": request["attempt_number"] + 1,
                "processed_glb_sha256": request["processed_glb_sha256"],
                "request_digest": request["request_digest"],
                "status": "PASS",
                "errors": [],
            }
            Path(request["observation_path"]).write_text(json.dumps(observation), encoding="utf-8")
            return CommandResult(exit_code=0, stdout="", stderr="")

    with pytest.raises(ValidationError, match="attempt_number mismatch"):
        verify_godot_assembly(
            spec=spec,
            profile=prof,
            processed_glb=glb_path,
            processed_glb_sha256=sha,
            execution_id="exec-01",
            output_dir=out_dir,
            godot_executable=glb_path,
            runner=WrongAttemptRunner(),
        )


@pytest.mark.parametrize("tamper_position", [False, True])
def test_observation_gate_rejects_transform_tamper_or_failed_restoration(
    tamper_position: bool,
) -> None:
    profile = _tank_profile()
    spec = _tank_spec(profile)
    parts = {
        part.part_id: {
            "part_id": part.part_id,
            "parent": part.parent,
            "motion_kind": part.pivot.motion.kind,
            "local_position": list(part.pivot.position_m),
            "local_basis": [list(row) for row in _quaternion_matrix(part.pivot.basis)],
            "local_scale": [1.0, 1.0, 1.0],
            "gf_axis": list(part.pivot.motion.axis or []),
            "lod0_present": True,
            "lod1_present": True,
        }
        for part in spec.parts or []
    }
    motions = {
        part.part_id: {
            "part_id": part.part_id,
            "motion_applied": True,
            "pivot_world_ok": True,
            "axis_world_ok": True,
            "descendants_rigid_ok": True,
            "descendant_moved": True,
            "ancestors_siblings_unchanged": True,
            "restored_ok": False if part.part_id == "turret" else True,
        }
        for part in spec.parts or []
        if part.pivot.motion.kind != "fixed"
    }
    framing: dict[str, Any] = {}
    captures: dict[str, Any] = {}
    for view in profile.review_views:
        direction = view_direction(view)
        forward = (-direction[0], -direction[1], -direction[2])
        framing[view] = {
            "ok": True,
            "view_axis": view_axis_label(view),
            "camera_direction": forward,
            "camera_up": _camera_up(view, forward),
            "height_ratio": 0.65,
            "inside_viewport": True,
            "margin_ok": True,
        }
        captures[view] = {"sha256": "a" * 64}
    socket_facts: dict[str, Any] = {}
    socket_observations: list[dict[str, Any]] = []
    for socket in spec.sockets or []:
        world_position, world_basis = _expected_socket_world(spec, socket)
        basis = _quaternion_matrix(socket.rotation)
        world = [[*world_basis[row], world_position[row]] for row in range(3)] + [
            [0.0, 0.0, 0.0, 1.0]
        ]
        socket_facts[socket.socket_id] = {
            "position": tuple(socket.translation_m),
            "basis": basis,
            "world": world,
        }
        socket_observations.append(
            {
                "socket_id": socket.socket_id,
                "parent_part": socket.parent_part,
                "local_position": list(socket.translation_m),
                "local_basis": [list(row) for row in basis],
                "world_position": list(world_position),
                "world_basis": [list(row) for row in world_basis],
                "marker_created": True,
            }
        )
    observation = {
        "status": "PASS",
        "errors": [],
        "processed_glb_sha256": "b" * 64,
        "revision": 1,
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "workflow_id": "assembly-verification",
        "execution_id": "exec-unit",
        "attempt_number": 1,
        "asset_id": spec.asset_id,
        "request_digest": "c" * 64,
        "harness_sha256": "d" * 64,
        "profile_sha256": "e" * 64,
        "specification_sha256": "f" * 64,
        "semantic_root": {
            "name": "ROOT",
            "transform": {
                "origin": [0, 0, 0],
                "basis": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
            },
        },
        "restoration_verified": True,
        "parts_verified": list(parts.values()),
        "sockets_verified": socket_observations,
        "collider": {
            "shape": "box",
            "body_kind": profile.document.godot.body_kind,
            "bounds": {"position": [0, 0, 0], "size": [1, 1, 1]},
            "physics_ray_hit": True,
        },
        "articulation_results": list(motions.values()),
        "view_framing": framing,
        "captures": captures,
    }
    if tamper_position:
        observation["parts_verified"][1]["local_position"] = [999.0, 0.0, 0.0]

    expected_error = (
        "PART_turret local position differs from processed GLB"
        if tamper_position
        else "PART_turret articulation restored_ok failed"
    )
    with pytest.raises(ValidationError, match=expected_error):
        _validate_observation(
            observation,
            spec=spec,
            profile=profile,
            review_views=list(profile.review_views),
            processed_hash="b" * 64,
            revision=1,
            workflow_id="assembly-verification",
            execution_id="exec-unit",
            attempt_number=1,
            request_digest="c" * 64,
            harness_sha256="d" * 64,
            profile_sha256="e" * 64,
            specification_sha256="f" * 64,
            actual_facts={
                "parts": {
                    part.part_id: {
                        "position": tuple(part.pivot.position_m),
                        "basis": _quaternion_matrix(part.pivot.basis),
                        "scale": 1.0,
                        "motion": part.pivot.motion.kind,
                        "axis": list(part.pivot.motion.axis or []),
                        "lod1": True,
                    }
                    for part in spec.parts or []
                },
                "sockets": socket_facts,
                "collider_bounds": {"position": (0.0, 0.0, 0.0), "size": (1.0, 1.0, 1.0)},
            },
        )
