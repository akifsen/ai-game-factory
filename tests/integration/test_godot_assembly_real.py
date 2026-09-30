"""Real Godot integration tests for standalone V0.7 assembly runtime verification.

Executes actual Godot 4.7.2 engine processes to verify:
1. Positive acceptance: script-built rotated nonzero tank (hull -> turret -> barrel -> muzzle + COL box + LOD0/LOD1)
2. Negative fixtures:
   - part parent mismatch
   - pivot orientation / basis mismatch
   - motion axis mismatch in GLB extras
   - socket position / orientation mismatch
   - collider missing or ray-hit failure
   - restoration failure
   - capture angle unknown (fails before load)
   - hash / tamper detection
   - PNG decode validation
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.dcc.godot_assembly import verify_godot_assembly
from gamefactory.adapters.engines.godot_image import ImageValidationError, decode_png
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.core.domain.asset_contracts import parse_asset_specification_v07
from gamefactory.core.domain.asset_profiles import (
    AssetProfileV07,
    ProfileRegistry,
    builtin_registry,
    parse_profile_document_v07,
)
from gamefactory.core.domain.camera_framing import PLACED_VIEWS
from gamefactory.core.domain.errors import ToolExecutionError, ValidationError
from gamefactory.core.execution.process_runner import ProcessRunner


def _read_glb(raw: bytes) -> tuple[dict, bytes]:
    json_len = struct.unpack_from("<I", raw, 12)[0]
    document = json.loads(raw[20 : 20 + json_len].decode().rstrip())
    binary_offset = 20 + json_len + 8
    return document, raw[binary_offset:]


def _write_glb(path: Path, document: dict, binary: bytes) -> None:
    encoded = json.dumps(document, separators=(",", ":")).encode()
    encoded += b" " * ((-len(encoded)) % 4)
    binary += b"\0" * ((-len(binary)) % 4)
    total = 12 + 8 + len(encoded) + 8 + len(binary)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(encoded), 0x4E4F534A)
        + encoded
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )


def _build_tank_glb(
    path: Path,
    *,
    asset_id: str = "tank",
    turret_yaw_deg: float = 0.0,
    turret_scale: list[float] | None = None,
    turret_matrix: list[float] | None = None,
    turret_translation: list[float] | None = None,
    barrel_pitch_deg: float = 0.0,
    barrel_motion_kind: str = "revolute",
    turret_parent: str = "hull",
    barrel_parent: str = "turret",
    turret_motion_axis: list[float] | None = None,
    barrel_motion_axis: list[float] | None = None,
    muzzle_translation: list[float] | None = None,
    muzzle_rotation_deg: float = 0.0,
    omit_collider: bool = False,
    extra_root_wrapper: bool = False,
) -> None:
    if turret_motion_axis is None:
        turret_motion_axis = [0.0, 1.0, 0.0]
    if barrel_motion_axis is None:
        barrel_motion_axis = [1.0, 0.0, 0.0]
    if muzzle_translation is None:
        muzzle_translation = [0.0, 0.0, -0.8]
    if turret_translation is None:
        turret_translation = [0.0, 0.4, 0.1]

    create_box_glb(
        width_m=2.0,
        height_m=1.6,
        depth_m=3.0,
        mesh_name="shared_geometry",
        origin="center",
        include_collider=not omit_collider,
        collider_name=f"COL_{asset_id}",
        output_path=path,
    )
    doc, binary = _read_glb(path.read_bytes())

    # Add LOD1 mesh definition reusing binary box vertices
    doc["meshes"].append(dict(doc["meshes"][0]))
    doc["meshes"][0]["name"] = "mesh_lod0"
    if not omit_collider:
        doc["meshes"][1]["name"] = "mesh_col"
        doc["meshes"][2]["name"] = "mesh_lod1"
    else:
        doc["meshes"][1]["name"] = "mesh_lod1"

    lod0_mesh_idx = 0
    lod1_mesh_idx = 2 if not omit_collider else 1
    col_mesh_idx = 1

    # Rotations
    half_yaw = math.radians(turret_yaw_deg) * 0.5
    qy = math.sin(half_yaw)
    qw = math.cos(half_yaw)
    turret_rotation = [0.0, qy, 0.0, qw] if turret_yaw_deg != 0.0 else None

    half_pitch = math.radians(barrel_pitch_deg) * 0.5
    qx = math.sin(half_pitch)
    pw = math.cos(half_pitch)
    barrel_rotation = [qx, 0.0, 0.0, pw] if barrel_pitch_deg != 0.0 else None

    hull_children = [2, 3]
    turret_children = [5, 6]
    barrel_children = [8, 9, 10]
    root_children = [1]
    if not omit_collider:
        root_children.append(11)

    if turret_parent == "hull":
        hull_children.append(4)
    elif turret_parent == "root":
        root_children.append(4)

    if barrel_parent == "turret":
        turret_children.append(7)
    elif barrel_parent == "hull":
        hull_children.append(7)

    turret_dict: dict[str, Any] = {
        "name": "PART_turret",
        "translation": turret_translation,
        "extras": {
            "gf_motion": "revolute",
            "gf_axis": turret_motion_axis,
        },
        "children": turret_children,
    }
    if turret_matrix is not None:
        turret_dict["matrix"] = turret_matrix
    else:
        if turret_rotation:
            turret_dict["rotation"] = turret_rotation
        if turret_scale is not None:
            turret_dict["scale"] = turret_scale

    barrel_dict: dict[str, Any] = {
        "name": "PART_barrel",
        "translation": [0.0, 0.2, -0.3],
        "extras": {
            "gf_motion": barrel_motion_kind,
            "gf_axis": barrel_motion_axis,
        },
        "children": barrel_children,
    }
    if barrel_rotation:
        barrel_dict["rotation"] = barrel_rotation

    socket_node: dict[str, Any] = {"name": "SOCKET_muzzle", "translation": muzzle_translation}
    if muzzle_rotation_deg:
        half_socket = math.radians(muzzle_rotation_deg) * 0.5
        socket_node["rotation"] = [math.sin(half_socket), 0.0, 0.0, math.cos(half_socket)]

    nodes: list[dict[str, Any]] = [
        {"name": "ROOT", "children": root_children},
        {
            "name": "PART_hull",
            "translation": [0.0, -0.2, 0.0],
            "extras": {"gf_motion": "fixed"},
            "children": hull_children,
        },
        {"name": f"SM_{asset_id}_hull_LOD0", "mesh": lod0_mesh_idx},
        {"name": f"SM_{asset_id}_hull_LOD1", "mesh": lod1_mesh_idx},
        turret_dict,
        {"name": f"SM_{asset_id}_turret_LOD0", "mesh": lod0_mesh_idx},
        {"name": f"SM_{asset_id}_turret_LOD1", "mesh": lod1_mesh_idx},
        barrel_dict,
        {"name": f"SM_{asset_id}_barrel_LOD0", "mesh": lod0_mesh_idx},
        {"name": f"SM_{asset_id}_barrel_LOD1", "mesh": lod1_mesh_idx},
        socket_node,
    ]
    if not omit_collider:
        nodes.append({"name": f"COL_{asset_id}", "mesh": col_mesh_idx})

    if extra_root_wrapper:
        # Wrap ROOT inside another unexpected node
        nodes.append({"name": "UNEXPECTED_WRAPPER", "children": [0]})
        doc["scenes"] = [{"nodes": [len(nodes) - 1]}]
    else:
        doc["scenes"] = [{"nodes": [0]}]

    doc["nodes"] = nodes
    _write_glb(path, doc, binary)


def _build_test_profile(
    *,
    review_views: list[str] | None = None,
    require_ray_hit: bool = True,
    barrel_motion_kind: str = "revolute",
) -> AssetProfileV07:
    base = builtin_registry().get("static_prop").document.model_dump(mode="json")
    if review_views is None:
        review_views = ["front", "three_quarter", "side"]
    doc: dict[str, Any] = {
        "schema_version": "asset-profile-0.7.0",
        "profile_id": "real_tank_vehicle",
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
        "godot": {
            "body_kind": "static_body",
            "require_ray_hit": require_ray_hit,
            "require_area": False,
        },
        "runtime": base["runtime"],
        "geometry_mode": "assembly",
        "accepted_source_kinds": ["local_operator_assembly"],
        "assembly": {
            "roles": ["hull", "turret", "barrel"],
            "required_roles": ["hull", "turret", "barrel"],
            "role_motion_constraints": {
                "turret": {"kind": "revolute", "axis": [0.0, 1.0, 0.0]},
                "barrel": {"kind": barrel_motion_kind, "axis": [1.0, 0.0, 0.0]},
            },
            "required_sockets": [
                {
                    "socket_id": "muzzle",
                    "parent_role": "barrel",
                    "placement": "forward_end",
                    "forward_end_fraction": 0.2,
                    "rest_forward": [0.0, 0.0, -1.0],
                }
            ],
            "pivot_tolerance_m": 0.01,
            "basis_tolerance_deg": 1.0,
            "socket_position_tolerance_m": 0.01,
            "socket_angle_tolerance_deg": 1.0,
        },
    }
    return AssetProfileV07(parse_profile_document_v07(doc))


def _build_test_spec(
    profile: AssetProfileV07,
    *,
    turret_basis: list[float] | str = "identity",
    barrel_basis: list[float] | str = "identity",
    muzzle_translation: list[float] | None = None,
    muzzle_rotation: list[float] | str = "identity",
) -> Any:
    if muzzle_translation is None:
        muzzle_translation = [0.0, 0.0, -0.8]

    data: dict[str, Any] = {
        "schema_version": "0.7.0",
        "asset_id": "tank",
        "category": "vehicle",
        "profile": profile.profile_id,
        "profile_version": profile.version,
        "intent": "real tank assembly verification",
        "source_kind": "local_operator_assembly",
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
        "parts": [
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
                    "basis": turret_basis,
                    "motion": {"kind": "revolute", "axis": [0.0, 1.0, 0.0]},
                },
            },
            {
                "part_id": "barrel",
                "role": "barrel",
                "parent": "turret",
                "pivot": {
                    "position_m": [0.0, 0.2, -0.3],
                    "basis": barrel_basis,
                    "motion": {
                        "kind": profile.assembly.role_motion_constraints["barrel"].kind,
                        "axis": [1.0, 0.0, 0.0],
                    },
                },
            },
        ],
        "sockets": [
            {
                "socket_id": "muzzle",
                "parent_part": "barrel",
                "translation_m": muzzle_translation,
                "rotation": muzzle_rotation,
                "placement": "forward_end",
            }
        ],
    }
    registry = ProfileRegistry(available=(), unsupported=(), available_v07=(profile,))
    return parse_asset_specification_v07(data, registry=registry)


@pytest.fixture
def godot_exe() -> str:
    env_val = os.environ.get("GAMEFACTORY_TEST_GODOT")
    if not env_val:
        pytest.skip("Set GAMEFACTORY_TEST_GODOT to run real Godot integration tests")
    p = Path(env_val).expanduser().resolve()
    if not p.is_file():
        pytest.fail(f"Configured Godot executable does not exist: {p}")
    return str(p)


@pytest.mark.real_godot
def test_real_godot_positive_tank_acceptance(tmp_path: Path, godot_exe: str) -> None:
    """Proves positive acceptance of a script-built rotated nonzero tank assembly."""
    glb_path = tmp_path / "tank.glb"
    out_dir = tmp_path / "output"

    # Rotated nonzero tank: turret yaw 15 deg, barrel pitch -10 deg
    yaw_deg = 15.0
    half_yaw = math.radians(yaw_deg) * 0.5
    turret_basis = [0.0, math.sin(half_yaw), 0.0, math.cos(half_yaw)]

    pitch_deg = -10.0
    half_pitch = math.radians(pitch_deg) * 0.5
    barrel_basis = [math.sin(half_pitch), 0.0, 0.0, math.cos(half_pitch)]

    _build_tank_glb(
        glb_path,
        turret_yaw_deg=yaw_deg + 0.5,
        turret_scale=[2.0, 2.0, 2.0],
        turret_translation=[0.001, 0.4, 0.1],
        barrel_pitch_deg=pitch_deg,
        muzzle_translation=[0.001, 0.0, -0.8],
        muzzle_rotation_deg=0.5,
    )
    sha256 = hashlib.sha256(glb_path.read_bytes()).hexdigest()

    all_review_views = [
        "front",
        "rear",
        "left",
        "right",
        "side",
        "three_quarter",
        "three_quarter_front",
        "three_quarter_rear",
        "top",
    ]
    assert set(all_review_views) == PLACED_VIEWS
    profile = _build_test_profile(review_views=all_review_views)
    spec = _build_test_spec(
        profile,
        turret_basis=turret_basis,
        barrel_basis=barrel_basis,
    )

    result = verify_godot_assembly(
        spec=spec,
        profile=profile,
        processed_glb=glb_path,
        processed_glb_sha256=sha256,
        execution_id="exec-tank-real-01",
        attempt_number=1,
        output_dir=out_dir,
        godot_executable=godot_exe,
    )

    assert result.status == "PASS"
    assert result.findings == []
    assert result.request_digest != ""
    assert result.processed_glb_sha256 == sha256

    obs = result.observation
    assert obs["status"] == "PASS"
    assert obs["schema_version"] == "assembly-runtime-observation-0.7.0"
    assert obs["semantic_root"]["name"] == "ROOT"
    assert obs["restoration_verified"] is True
    assert obs["collider"]["physics_ray_hit"] is True
    assert next(item for item in obs["parts_verified"] if item["part_id"] == "turret")[
        "local_scale"
    ] == pytest.approx([2.0, 2.0, 2.0])
    assert obs["sockets_verified"][0]["world_position"] != pytest.approx([0.0, 0.0, -1.0])

    # Check articulation verification of each moving part
    articulation = {item["part_id"]: item for item in obs["articulation_results"]}
    assert "turret" in articulation
    assert articulation["turret"]["pivot_world_ok"] is True
    assert articulation["turret"]["descendants_rigid_ok"] is True
    assert articulation["turret"]["ancestors_siblings_unchanged"] is True
    assert articulation["turret"]["restored_ok"] is True

    assert "barrel" in articulation
    assert articulation["barrel"]["pivot_world_ok"] is True
    assert articulation["barrel"]["descendants_rigid_ok"] is True
    assert articulation["barrel"]["ancestors_siblings_unchanged"] is True
    assert articulation["barrel"]["restored_ok"] is True

    # Sockets verified and Marker3D created
    sockets = {item["socket_id"]: item for item in obs["sockets_verified"]}
    assert "muzzle" in sockets
    assert sockets["muzzle"]["marker_created"] is True

    # Review captures rendered and decoded
    assert set(result.captures.keys()) == set(all_review_views)
    for _view, png_bytes in result.captures.items():
        decoded = decode_png(png_bytes, 1280, 720)
        assert decoded.width == 1280
        assert decoded.height == 720
        assert decoded.mode in ("RGB", "RGBA")


@pytest.mark.real_godot
def test_real_godot_prismatic_articulation(tmp_path: Path, godot_exe: str) -> None:
    glb_path = tmp_path / "tank_prismatic.glb"
    _build_tank_glb(glb_path, barrel_motion_kind="prismatic")
    sha256 = hashlib.sha256(glb_path.read_bytes()).hexdigest()
    profile = _build_test_profile(review_views=["front"], barrel_motion_kind="prismatic")
    spec = _build_test_spec(profile)

    result = verify_godot_assembly(
        spec=spec,
        profile=profile,
        processed_glb=glb_path,
        processed_glb_sha256=sha256,
        execution_id="exec-prismatic-real",
        output_dir=tmp_path / "output_prismatic",
        godot_executable=godot_exe,
    )
    barrel = next(
        item for item in result.observation["articulation_results"] if item["part_id"] == "barrel"
    )
    assert barrel["motion_kind"] == "prismatic"
    assert barrel["motion_applied"] is True
    assert barrel["axis_world_ok"] is True
    assert barrel["descendant_moved"] is True
    assert barrel["restored_ok"] is True


@pytest.mark.real_godot
def test_real_godot_negative_part_parent_mismatch(tmp_path: Path, godot_exe: str) -> None:
    """Reject when part parent hierarchy does not match canonical spec."""
    glb_path = tmp_path / "tank_bad_parent.glb"
    out_dir = tmp_path / "output_bad_parent"

    # Turret parented to ROOT instead of hull
    _build_tank_glb(glb_path, turret_parent="root")
    sha256 = hashlib.sha256(glb_path.read_bytes()).hexdigest()

    profile = _build_test_profile()
    spec = _build_test_spec(profile)

    with pytest.raises(ValidationError, match="PART_turret parent differs"):
        verify_godot_assembly(
            spec=spec,
            profile=profile,
            processed_glb=glb_path,
            processed_glb_sha256=sha256,
            execution_id="exec-parent-fail",
            output_dir=out_dir,
            godot_executable=godot_exe,
        )


@pytest.mark.real_godot
def test_real_godot_rejects_unexpected_semantic_root_wrapper(
    tmp_path: Path, godot_exe: str
) -> None:
    glb_path = tmp_path / "wrapped.glb"
    _build_tank_glb(glb_path, extra_root_wrapper=True)
    sha256 = hashlib.sha256(glb_path.read_bytes()).hexdigest()
    profile = _build_test_profile(review_views=["front"])
    spec = _build_test_spec(profile)

    with pytest.raises(ValidationError, match="active scene must have exact ROOT identity"):
        verify_godot_assembly(
            spec=spec,
            profile=profile,
            processed_glb=glb_path,
            processed_glb_sha256=sha256,
            execution_id="exec-wrapper-fail",
            output_dir=tmp_path / "output_wrapper",
            godot_executable=godot_exe,
        )


@pytest.mark.real_godot
def test_real_godot_rejects_missing_rendered_capture(tmp_path: Path, godot_exe: str) -> None:
    glb_path = tmp_path / "tank.glb"
    _build_tank_glb(glb_path)
    sha256 = hashlib.sha256(glb_path.read_bytes()).hexdigest()
    profile = _build_test_profile(review_views=["front"])
    spec = _build_test_spec(profile)
    out_dir = tmp_path / "output_missing_capture"

    class DeleteCaptureRunner:
        def __init__(self) -> None:
            self.delegate = ProcessRunner()

        def run(self, request: Any) -> Any:
            result = self.delegate.run(request)
            if "--script" in request.args:
                (out_dir / "front.png").unlink(missing_ok=True)
            return result

    with pytest.raises(ToolExecutionError, match="Expected review capture missing"):
        verify_godot_assembly(
            spec=spec,
            profile=profile,
            processed_glb=glb_path,
            processed_glb_sha256=sha256,
            execution_id="exec-capture-missing",
            output_dir=out_dir,
            godot_executable=godot_exe,
            runner=DeleteCaptureRunner(),
        )


@pytest.mark.real_godot
def test_real_godot_negative_pivot_orientation_mismatch(tmp_path: Path, godot_exe: str) -> None:
    """Reject when part basis rotation differs from declared spec."""
    glb_path = tmp_path / "tank_bad_basis.glb"
    out_dir = tmp_path / "output_bad_basis"

    # Turret rotated 45 deg in GLB, but spec declares identity
    _build_tank_glb(glb_path, turret_yaw_deg=45.0)
    sha256 = hashlib.sha256(glb_path.read_bytes()).hexdigest()

    profile = _build_test_profile()
    spec = _build_test_spec(profile, turret_basis="identity")

    with pytest.raises(ValidationError, match="PART_turret local basis differs"):
        verify_godot_assembly(
            spec=spec,
            profile=profile,
            processed_glb=glb_path,
            processed_glb_sha256=sha256,
            execution_id="exec-basis-fail",
            output_dir=out_dir,
            godot_executable=godot_exe,
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"turret_scale": [-1.0, 1.0, 1.0]}, "reflection or singular basis"),
        (
            {
                "turret_matrix": [
                    1,
                    0,
                    0,
                    0,
                    0.5,
                    0.8660254037844386,
                    0,
                    0,
                    0,
                    0,
                    1,
                    0,
                    0,
                    0.4,
                    0.1,
                    1,
                ]
            },
            "contains shear",
        ),
    ],
)
def test_real_processed_glb_rejects_reflection_and_shear(
    tmp_path: Path, kwargs: dict[str, Any], message: str
) -> None:
    glb_path = tmp_path / "tank_invalid_basis.glb"
    _build_tank_glb(glb_path, **kwargs)
    profile = _build_test_profile(review_views=["front"])
    spec = _build_test_spec(profile)
    with pytest.raises(ValidationError, match=message):
        verify_godot_assembly(
            spec=spec,
            profile=profile,
            processed_glb=glb_path,
            processed_glb_sha256=hashlib.sha256(glb_path.read_bytes()).hexdigest(),
            execution_id="exec-invalid-basis",
            godot_executable=glb_path,
        )


@pytest.mark.real_godot
def test_real_godot_negative_motion_axis_mismatch(tmp_path: Path, godot_exe: str) -> None:
    """Reject when gf_axis in GLB extras differs from declared specification."""
    glb_path = tmp_path / "tank_bad_axis.glb"
    out_dir = tmp_path / "output_bad_axis"

    # Turret motion axis in extras set to [1, 0, 0] instead of declared [0, 1, 0]
    _build_tank_glb(glb_path, turret_motion_axis=[1.0, 0.0, 0.0])
    sha256 = hashlib.sha256(glb_path.read_bytes()).hexdigest()

    profile = _build_test_profile()
    spec = _build_test_spec(profile)

    with pytest.raises(ValidationError, match="PART_turret gf_axis differs"):
        verify_godot_assembly(
            spec=spec,
            profile=profile,
            processed_glb=glb_path,
            processed_glb_sha256=sha256,
            execution_id="exec-axis-fail",
            output_dir=out_dir,
            godot_executable=godot_exe,
        )


@pytest.mark.real_godot
def test_real_godot_negative_socket_position_mismatch(tmp_path: Path, godot_exe: str) -> None:
    """Reject when socket position differs from declared spec."""
    glb_path = tmp_path / "tank_bad_socket.glb"
    out_dir = tmp_path / "output_bad_socket"

    # Sockets translated to [0.0, 0.0, -0.2] in GLB vs declared [0.0, 0.0, -0.8]
    _build_tank_glb(glb_path, muzzle_translation=[0.0, 0.0, -0.2])
    sha256 = hashlib.sha256(glb_path.read_bytes()).hexdigest()

    profile = _build_test_profile()
    spec = _build_test_spec(profile, muzzle_translation=[0.0, 0.0, -0.8])

    with pytest.raises(ValidationError, match="SOCKET_muzzle local transform differs"):
        verify_godot_assembly(
            spec=spec,
            profile=profile,
            processed_glb=glb_path,
            processed_glb_sha256=sha256,
            execution_id="exec-socket-fail",
            output_dir=out_dir,
            godot_executable=godot_exe,
        )


@pytest.mark.real_godot
def test_real_godot_negative_collider_missing(tmp_path: Path, godot_exe: str) -> None:
    """Reject when assembly root COL_ box proxy is missing."""
    glb_path = tmp_path / "tank_no_col.glb"
    out_dir = tmp_path / "output_no_col"

    _build_tank_glb(glb_path, omit_collider=True)
    sha256 = hashlib.sha256(glb_path.read_bytes()).hexdigest()

    profile = _build_test_profile()
    spec = _build_test_spec(profile)

    with pytest.raises(ValidationError, match="requires direct non-empty COL_tank"):
        verify_godot_assembly(
            spec=spec,
            profile=profile,
            processed_glb=glb_path,
            processed_glb_sha256=sha256,
            execution_id="exec-col-fail",
            output_dir=out_dir,
            godot_executable=godot_exe,
        )


@pytest.mark.real_godot
def test_real_godot_negative_unknown_capture_angle(tmp_path: Path, godot_exe: str) -> None:
    """Reject unknown review views before load."""
    glb_path = tmp_path / "tank.glb"
    out_dir = tmp_path / "output_unknown_angle"

    _build_tank_glb(glb_path)
    sha256 = hashlib.sha256(glb_path.read_bytes()).hexdigest()

    # Exercise adapter guard even if a corrupted in-memory profile bypasses parsing.
    profile = _build_test_profile()
    profile.document.review_views = ["front", "unknown_angle"]
    spec = _build_test_spec(profile)

    with pytest.raises(ValidationError, match="unknown review view identifier: 'unknown_angle'"):
        verify_godot_assembly(
            spec=spec,
            profile=profile,
            processed_glb=glb_path,
            processed_glb_sha256=sha256,
            execution_id="exec-bad-view",
            output_dir=out_dir,
            godot_executable=godot_exe,
        )
    assert not out_dir.exists(), "invalid review views must fail before creating staging/output"


@pytest.mark.real_godot
def test_real_godot_negative_hash_tamper_attempt(tmp_path: Path, godot_exe: str) -> None:
    """Reject when processed GLB has been modified or hash was falsified."""
    glb_path = tmp_path / "tank.glb"
    out_dir = tmp_path / "output_tamper"

    _build_tank_glb(glb_path)
    real_sha = hashlib.sha256(glb_path.read_bytes()).hexdigest()
    tampered_sha = real_sha[:-4] + "0000"

    profile = _build_test_profile()
    spec = _build_test_spec(profile)

    with pytest.raises(ValidationError, match="processed_glb SHA-256 mismatch"):
        verify_godot_assembly(
            spec=spec,
            profile=profile,
            processed_glb=glb_path,
            processed_glb_sha256=tampered_sha,
            execution_id="exec-tamper",
            output_dir=out_dir,
            godot_executable=godot_exe,
        )


@pytest.mark.real_godot
def test_real_godot_negative_corrupted_png_decode() -> None:
    """Validate that corrupt PNG bytes fail decode_png without concealing error."""
    with pytest.raises(ImageValidationError):
        decode_png(b"not-a-valid-png-stream", 1280, 720)

    # Valid PNG signature but corrupted body
    with pytest.raises(ImageValidationError):
        decode_png(b"\x89PNG\r\n\x1a\ncorrupted_payload", 1280, 720)
