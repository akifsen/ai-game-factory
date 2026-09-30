"""Real Blender integration tests for standalone V0.7 authored-assembly processing."""

from __future__ import annotations

import json
import math
import runpy
import struct
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import pytest

from gamefactory.adapters.assets.assembly_ingest import ingest_assembly_source
from gamefactory.adapters.assets.glb_validator import (
    _inspect,
    _node_matrix,
    _read_glb_bytes,
)
from gamefactory.adapters.assets.v07_geometry_validation import (
    _matrix_is_identity,
    _triangle_soup,
    triangle_soups_equivalent,
    validate_glb_v07,
    verify_source_to_processed_preservation,
)
from gamefactory.adapters.dcc.assembly_processor import (
    AssemblyProcessor,
    AssemblyProcessResult,
)
from gamefactory.adapters.dcc.blender import BlenderAdapter
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.core.domain.assembly_source import AssemblyIngestResult
from gamefactory.core.domain.asset_contracts import (
    AssetSpecificationV07,
    parse_asset_specification_v07,
)
from gamefactory.core.domain.asset_profiles import (
    AssetProfileV07,
    ProfileRegistry,
    builtin_registry,
    parse_profile_document_v07,
)
from gamefactory.core.domain.errors import ValidationError


def _blender_available() -> bool:
    detect = BlenderAdapter().detect_tool()
    return detect.available


def _create_assembly_profile() -> AssetProfileV07:
    base = builtin_registry().get("static_prop").document.model_dump(mode="json")
    assembly_conf = {
        "roles": ["hull", "turret", "barrel"],
        "required_roles": ["hull", "turret", "barrel"],
        "role_motion_constraints": {},
        "required_sockets": [
            {
                "socket_id": "muzzle",
                "parent_role": "barrel",
                "placement": "forward_end",
                "forward_end_fraction": 0.2,
                "rest_forward": [0, 0, -1],
            }
        ],
        "pivot_tolerance_m": 0.01,
        "basis_tolerance_deg": 1.0,
        "socket_position_tolerance_m": 0.01,
        "socket_angle_tolerance_deg": 1.0,
    }
    doc = {
        "schema_version": "asset-profile-0.7.0",
        "profile_id": "test_assembly_integration_profile",
        "version": 1,
        "categories": ["vehicle"],
        "review_views": ["front"],
        "framing": base["framing"],
        "dimension_rules": {"min_m": 0.1, "max_m": 10.0},
        "processing": {
            "lod0_required": True,
            "lod1_required": False,
            "allowed_lod_policies": ["lod0_only", "lod0_lod1"],
            "allowed_collider_policies": ["box"],
            "allowed_origin_policies": ["bottom_center", "center"],
            "default_origin_policy": "center",
            "dimension_tolerance_m": 0.02,
            "snap_grid_m": None,
            "rig_forbidden": True,
            "animation_forbidden": True,
            "max_materials": 4,
            "max_texture_dimension": 2048,
            "max_triangles_lod0": 10000,
        },
        "godot": base["godot"],
        "runtime": base["runtime"],
        "geometry_mode": "assembly",
        "accepted_source_kinds": ["local_operator_assembly"],
        "assembly": assembly_conf,
    }
    return AssetProfileV07(parse_profile_document_v07(doc))


def _create_assembly_spec(
    profile: AssetProfileV07,
    *,
    source_front: Literal["-Z", "+Z"] = "-Z",
    root_basis: Any = "identity",
    root_position: list[float] | None = None,
    dimensions: tuple[float, float, float] = (1.0, 1.4, 1.2),
    socket_rotation: Any = "identity",
) -> AssetSpecificationV07:
    parts = [
        {
            "part_id": "hull",
            "role": "hull",
            "parent": "root",
            "pivot": {
                "position_m": root_position or [0, -0.2, 0],
                "basis": root_basis,
                "motion": {"kind": "fixed"},
            },
        },
        {
            "part_id": "turret",
            "role": "turret",
            "parent": "hull",
            "pivot": {
                "position_m": [0, 0.2, 0.1],
                "basis": "identity",
                "motion": {"kind": "revolute", "axis": [0, 1, 0]},
            },
        },
        {
            "part_id": "barrel",
            "role": "barrel",
            "parent": "turret",
            "pivot": {
                "position_m": [0, 0.2, -0.2],
                "basis": "identity",
                "motion": {"kind": "revolute", "axis": [1, 0, 0]},
            },
        },
    ]
    sockets = [
        {
            "socket_id": "muzzle",
            "parent_part": "barrel",
            "translation_m": [0, 0, -0.49],
            "rotation": socket_rotation,
            "placement": "forward_end",
        }
    ]
    data: dict[str, Any] = {
        "schema_version": "0.7.0",
        "asset_id": "test_blender_tank",
        "category": "vehicle",
        "profile": profile.profile_id,
        "profile_version": profile.version,
        "intent": "blender assembly integration test",
        "source_kind": "local_operator_assembly",
        "dimensions": {
            "width_m": dimensions[0],
            "height_m": dimensions[1],
            "depth_m": dimensions[2],
        },
        "origin_policy": "center",
        "lod_policy": "lod0_lod1",
        "geometry_budget": {
            "max_triangles_lod0": 10000,
            "max_triangles_lod1": 5000,
            "lod_ratio": 0.5,
        },
        "material_budget": {"max_materials": 4},
        "texture_budget": {"max_dimension": 2048},
        "collider": {"policy": "box"},
        "parts": parts,
        "sockets": sockets,
    }
    registry = ProfileRegistry(available=(), unsupported=(), available_v07=(profile,))
    return parse_asset_specification_v07(data, registry=registry)


def _read_glb(raw: bytes) -> tuple[dict[str, Any], bytes]:
    json_len = struct.unpack_from("<I", raw, 12)[0]
    doc = json.loads(raw[20 : 20 + json_len].decode("utf-8").rstrip())
    return doc, raw[20 + json_len + 8 :]


def _write_glb(path: Path, doc: dict[str, Any], binary: bytes) -> None:
    enc = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    enc += b" " * ((-len(enc)) % 4)
    bin_pad = binary + b"\0" * ((-len(binary)) % 4)
    total = 12 + 8 + len(enc) + 8 + len(bin_pad)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(enc), 0x4E4F534A)
        + enc
        + struct.pack("<II", len(bin_pad), 0x004E4942)
        + bin_pad
    )


def _create_source_assembly_glb(
    path: Path,
    spec: AssetSpecificationV07,
    *,
    source_front: Literal["-Z", "+Z"] = "-Z",
) -> None:
    create_box_glb(
        width_m=1.0,
        depth_m=1.0,
        height_m=1.0,
        mesh_name="shared_geometry",
        origin="center",
        include_collider=False,
        output_path=path,
    )
    doc, binary = _read_glb(path.read_bytes())

    # Raw root PART rotation: if source_front == "+Z", raw root PART rotation is Ry(-180Y) from canonical
    assert spec.parts
    root_pivot = spec.parts[0].pivot
    canonical_basis = (
        list(root_pivot.basis) if root_pivot.basis != "identity" else [0.0, 0.0, 0.0, 1.0]
    )
    canonical_position = list(root_pivot.position_m)
    if source_front == "+Z":
        # Raw source parent-frame root transform is inverse(Ry(pi)) * canonical.
        hull_rot = [
            canonical_basis[2],
            canonical_basis[3],
            -canonical_basis[0],
            -canonical_basis[1],
        ]
        hull_translation = [-canonical_position[0], canonical_position[1], -canonical_position[2]]
    else:
        hull_rot = canonical_basis
        hull_translation = canonical_position

    assert spec.sockets
    socket = spec.sockets[0]
    socket_rotation = list(socket.rotation) if socket.rotation != "identity" else None
    nodes: list[dict[str, Any]] = [
        {"name": "ROOT", "children": [1]},
        {
            "name": "PART_hull",
            "translation": hull_translation,
            **({"rotation": hull_rot} if hull_rot else {}),
            "extras": {"gf_motion": "fixed"},
            "children": [2, 3],
        },
        {"name": f"SM_{spec.asset_id}_hull_LOD0", "mesh": 0},
        {
            "name": "PART_turret",
            "translation": [0, 0.2, 0.1],
            "extras": {"gf_motion": "revolute", "gf_axis": [0, 1, 0]},
            "children": [4, 5],
        },
        {"name": f"SM_{spec.asset_id}_turret_LOD0", "mesh": 0},
        {
            "name": "PART_barrel",
            "translation": [0, 0.2, -0.2],
            "extras": {"gf_motion": "revolute", "gf_axis": [1, 0, 0]},
            "children": [6, 7],
        },
        {"name": f"SM_{spec.asset_id}_barrel_LOD0", "mesh": 0},
        {
            "name": "SOCKET_muzzle",
            "translation": list(socket.translation_m),
            **({"rotation": socket_rotation} if socket_rotation else {}),
        },
    ]
    doc["nodes"] = nodes
    doc["scenes"] = [{"nodes": [0]}]
    _write_glb(path, doc, binary)


def _plus_z_fixture_spec(
    tmp_path: Path,
    profile: AssetProfileV07,
    canonical_basis: Sequence[float],
    *,
    socket_rotation: Any = "identity",
) -> AssetSpecificationV07:
    """Derive center-origin dimensions/translation from the actual authored fixture before ingest."""
    draft = _create_assembly_spec(
        profile,
        source_front="+Z",
        root_basis=list(canonical_basis),
        root_position=[0.0, 0.0, 0.0],
        socket_rotation=socket_rotation,
    )
    source_path = tmp_path / "plus_z_dimensions_draft.glb"
    _create_source_assembly_glb(source_path, draft, source_front="+Z")
    document, binary = _read_glb_bytes(source_path.read_bytes(), 50 * 1024 * 1024)
    _, raw_world_points, _, _, _, _ = _inspect(document, binary)
    # The draft stores inverse(Ry(180)) * canonical at PART_hull. Apply the one
    # allowed parent-frame Ry to measure canonical rest bounds at zero translation.
    canonical_points = [(-point[0], point[1], -point[2]) for point in raw_world_points]
    bounds_min = tuple(min(point[axis] for point in canonical_points) for axis in range(3))
    bounds_max = tuple(max(point[axis] for point in canonical_points) for axis in range(3))
    center = tuple((bounds_min[axis] + bounds_max[axis]) / 2 for axis in range(3))
    dimensions = (
        bounds_max[0] - bounds_min[0],
        bounds_max[1] - bounds_min[1],
        bounds_max[2] - bounds_min[2],
    )
    return _create_assembly_spec(
        profile,
        source_front="+Z",
        root_basis=list(canonical_basis),
        root_position=[-value for value in center],
        dimensions=dimensions,
        socket_rotation=socket_rotation,
    )


def _setup_ingested_package(
    tmp_path: Path,
    spec: AssetSpecificationV07,
    *,
    source_front: Literal["-Z", "+Z"] = "-Z",
) -> AssemblyIngestResult:
    source_raw_path = tmp_path / "raw_source.glb"
    _create_source_assembly_glb(source_raw_path, spec, source_front=source_front)

    return ingest_assembly_source(
        source_glb_path=source_raw_path,
        managed_root=tmp_path / "managed_repo",
        relative_package_dir=f"pkg_{source_front.replace('+', 'pos_').replace('-', 'neg_')}",
        spec=spec,
        authoring_tool_name="Blender",
        authoring_tool_version="5.2.1",
        source_front=source_front,
        actor="integration_tester",
        reason="Blender assembly integration tests",
    )


def test_blender_assembly_process_end_to_end_source_front_minus_z(tmp_path: Path) -> None:
    """Test real Blender background processing with source_front == '-Z'."""
    if not _blender_available():
        pytest.skip("Blender executable not available on host")

    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile, source_front="-Z", root_basis="identity")

    ingest_result = _setup_ingested_package(tmp_path, spec, source_front="-Z")
    source_raw_bytes = ingest_result.retained_glb_path.read_bytes()

    out_dir = tmp_path / "output_workspace"
    out_dir.mkdir(parents=True, exist_ok=True)
    processed_glb = out_dir / "processed_tank.glb"
    report_file = out_dir / "tank_report.json"

    processor = AssemblyProcessor()
    result = processor.process_assembly(
        package=ingest_result,
        spec=spec,
        expected_provenance_sha256=ingest_result.retained_provenance_sha256,
        processed_glb_path=processed_glb,
        report_path=report_file,
        timeout_seconds=45.0,
    )

    assert isinstance(result, AssemblyProcessResult)
    assert result.status == "SUCCESS"
    assert result.exit_code == 0
    assert result.source_front == "-Z"
    assert not result.verified_normalization.normalization_applied
    assert result.verified_normalization.root_rotation_xyzw == (0.0, 0.0, 0.0, 1.0)
    assert processed_glb.is_file() and processed_glb.stat().st_size > 0
    assert report_file.is_file()

    # Invariant: source GLB must be completely unchanged
    assert ingest_result.retained_glb_path.read_bytes() == source_raw_bytes

    # Inspect exported GLB directly
    doc, binary = _read_glb_bytes(processed_glb.read_bytes(), 50 * 1024 * 1024)
    nodes = doc.get("nodes", [])
    node_by_name = {str(n.get("name", "")): (i, n) for i, n in enumerate(nodes)}

    # 1. Exact ROOT identity
    assert "ROOT" in node_by_name
    root_idx, root_node = node_by_name["ROOT"]
    assert _matrix_is_identity(_node_matrix(root_node))

    # 2. Descendants and root child topology
    assert "PART_hull" in node_by_name
    hull_idx, hull_node = node_by_name["PART_hull"]
    assert hull_idx in root_node.get("children", [])
    assert hull_node.get("extras", {}).get("gf_motion") == "fixed"

    assert "PART_turret" in node_by_name
    turret_idx, turret_node = node_by_name["PART_turret"]
    assert turret_idx in hull_node.get("children", [])
    assert turret_node.get("extras", {}).get("gf_motion") == "revolute"
    assert turret_node.get("extras", {}).get("gf_axis") == [0, 1, 0]

    assert "PART_barrel" in node_by_name
    barrel_idx, barrel_node = node_by_name["PART_barrel"]
    assert barrel_idx in turret_node.get("children", [])
    assert barrel_node.get("extras", {}).get("gf_motion") == "revolute"
    assert barrel_node.get("extras", {}).get("gf_axis") == [1, 0, 0]

    assert "SOCKET_muzzle" in node_by_name
    muzzle_idx, muzzle_node = node_by_name["SOCKET_muzzle"]
    assert muzzle_idx in barrel_node.get("children", [])

    # 3. Direct identity named LOD0 meshes
    for pid in ("hull", "turret", "barrel"):
        lod0_name = f"SM_{spec.asset_id}_{pid}_LOD0"
        assert lod0_name in node_by_name
        l0_idx, l0_node = node_by_name[lod0_name]
        p_idx = node_by_name[f"PART_{pid}"][0]
        assert l0_idx in nodes[p_idx].get("children", [])
        assert _matrix_is_identity(_node_matrix(l0_node))

        # Check per-part LOD1 generated under same PART
        lod1_name = f"SM_{spec.asset_id}_{pid}_LOD1"
        assert lod1_name in node_by_name
        l1_idx, l1_node = node_by_name[lod1_name]
        assert l1_idx in nodes[p_idx].get("children", [])
        assert _matrix_is_identity(_node_matrix(l1_node))

    # 4. Root box collider COL_{asset_id}
    col_name = f"COL_{spec.asset_id}"
    assert col_name in node_by_name
    col_idx, col_node = node_by_name[col_name]
    assert col_idx in root_node.get("children", [])
    assert _matrix_is_identity(_node_matrix(col_node))

    # 5. Outside preservation verification
    pres = verify_source_to_processed_preservation(
        result.verified_normalization, processed_glb, spec
    )
    assert pres.passed, [f.message for f in pres.findings if not f.passed]

    # 6. Report schema 0.7 structure
    report = result.report_data
    assert report.get("schema_version") == "0.7.0"
    assert report.get("status") == "SUCCESS"
    assert report.get("source_glb_sha256") == result.source_glb_sha256
    assert report.get("output_glb_sha256") == result.processed_glb_sha256
    assert report.get("provenance_sha256") == result.provenance_sha256
    assert "blender_version" in report
    assert "assembly_bounds" in report
    assert "per_part_metrics" in report


def test_blender_assembly_process_end_to_end_source_front_plus_z(tmp_path: Path) -> None:
    """Test real Blender background processing with source_front == '+Z' and Ry(180) normalization."""
    if not _blender_available():
        pytest.skip("Blender executable not available on host")

    profile = _create_assembly_profile()
    # Three noncommuting authored rotations and a nonzero root translation exercise
    # left-multiplication by the one allowed Ry(180) source normalization.
    hx, hy, hz = (math.radians(value) / 2 for value in (30.0, 20.0, 10.0))
    qx = (math.sin(hx), 0.0, 0.0, math.cos(hx))
    qy = (0.0, math.sin(hy), 0.0, math.cos(hy))
    qz = (0.0, 0.0, math.sin(hz), math.cos(hz))

    def multiply(left: Sequence[float], right: Sequence[float]) -> list[float]:
        x1, y1, z1, w1 = left
        x2, y2, z2, w2 = right
        return [
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        ]

    canonical_basis = multiply(multiply(qx, qy), qz)
    spec = _plus_z_fixture_spec(tmp_path, profile, canonical_basis)
    assert spec.parts
    expected_root_position = tuple(spec.parts[0].pivot.position_m)
    assert max(abs(value) for value in expected_root_position) > 0.05

    ingest_result = _setup_ingested_package(tmp_path, spec, source_front="+Z")
    source_raw_bytes = ingest_result.retained_glb_path.read_bytes()

    out_dir = tmp_path / "output_workspace_plus_z"
    out_dir.mkdir(parents=True, exist_ok=True)
    processed_glb = out_dir / "processed_tank_plus_z.glb"
    report_file = out_dir / "tank_report_plus_z.json"

    processor = AssemblyProcessor()
    result = processor.process_assembly(
        package=ingest_result,
        spec=spec,
        expected_provenance_sha256=ingest_result.retained_provenance_sha256,
        processed_glb_path=processed_glb,
        report_path=report_file,
        timeout_seconds=45.0,
    )

    assert isinstance(result, AssemblyProcessResult)
    assert result.status == "SUCCESS"
    assert result.exit_code == 0
    assert result.source_front == "+Z"
    assert result.verified_normalization.normalization_applied
    assert result.verified_normalization.root_rotation_xyzw == (0.0, 1.0, 0.0, 0.0)

    # Invariant: source GLB must be completely unchanged
    assert ingest_result.retained_glb_path.read_bytes() == source_raw_bytes

    # Inspect exported GLB
    doc, binary = _read_glb_bytes(processed_glb.read_bytes(), 50 * 1024 * 1024)
    nodes = doc.get("nodes", [])
    node_by_name = {str(n.get("name", "")): (i, n) for i, n in enumerate(nodes)}

    # ROOT is strictly identity
    root_idx, root_node = node_by_name["ROOT"]
    assert _matrix_is_identity(_node_matrix(root_node))

    # Normalized root part PART_hull has canonical rotation
    hull_idx, hull_node = node_by_name["PART_hull"]
    assert hull_idx in root_node.get("children", [])
    hull_mat = _node_matrix(hull_node)
    x, y, z, w = canonical_basis
    expected_rotation = (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )
    for row in range(3):
        for column in range(3):
            assert abs(hull_mat[row][column] - expected_rotation[row][column]) <= 1e-4
    for axis, coordinate in enumerate(expected_root_position):
        assert abs(hull_mat[axis][3] - coordinate) <= 1e-4

    # Deeper children local transforms must remain UNCHANGED
    turret_idx, turret_node = node_by_name["PART_turret"]
    assert turret_idx in hull_node.get("children", [])
    turret_mat = _node_matrix(turret_node)
    # Turret translation was [0, 0.2, 0.1]
    assert abs(turret_mat[0][3] - 0.0) <= 1e-4
    assert abs(turret_mat[1][3] - 0.2) <= 1e-4
    assert abs(turret_mat[2][3] - 0.1) <= 1e-4
    assert turret_node.get("extras") == {
        "gf_motion": "revolute",
        "gf_axis": [0, 1, 0],
    }
    assert turret_node.get("rotation") is None

    barrel_idx, barrel_node = node_by_name["PART_barrel"]
    assert barrel_idx in turret_node.get("children", [])
    barrel_mat = _node_matrix(barrel_node)
    assert tuple(barrel_mat[row][3] for row in range(3)) == pytest.approx(
        (0.0, 0.2, -0.2), abs=1e-4
    )
    assert barrel_node.get("extras") == {
        "gf_motion": "revolute",
        "gf_axis": [1, 0, 0],
    }
    socket_idx, socket_node = node_by_name["SOCKET_muzzle"]
    assert socket_idx in barrel_node.get("children", [])
    assert tuple(socket_node.get("translation", ())) == pytest.approx((0.0, 0.0, -0.49), abs=1e-4)
    assert not socket_node.get("rotation")

    # Outside preservation check must prove the normalization and deep transform preservation
    pres = verify_source_to_processed_preservation(
        result.verified_normalization, processed_glb, spec
    )
    assert pres.passed, [f.message for f in pres.findings if not f.passed]


def test_blender_rotated_hierarchy_lod_bounds_pass_full_v07_validation(
    tmp_path: Path,
) -> None:
    """Exercise rotated parent-frame bounds and a semantically aligned socket end to end."""
    if not _blender_available():
        pytest.skip("Blender executable not available on host")

    profile = _create_assembly_profile()
    half_angle = math.radians(30.0) / 2
    root_rotation = (math.sin(half_angle), 0.0, 0.0, math.cos(half_angle))
    inverse_root_rotation = (-root_rotation[0], 0.0, 0.0, root_rotation[3])
    spec = _plus_z_fixture_spec(
        tmp_path,
        profile,
        root_rotation,
        socket_rotation=inverse_root_rotation,
    )
    ingest_result = _setup_ingested_package(tmp_path, spec, source_front="+Z")
    output = tmp_path / "rotated-output"
    output.mkdir()
    processed = output / "processed.glb"
    report = output / "report.json"

    result = AssemblyProcessor().process_assembly(
        package=ingest_result,
        spec=spec,
        expected_provenance_sha256=ingest_result.retained_provenance_sha256,
        processed_glb_path=processed,
        report_path=report,
        timeout_seconds=60.0,
    )
    validation = validate_glb_v07(
        processed,
        spec,
        source_observation=result.verified_normalization,
    )
    assert validation.passed, [
        (finding.rule_id, finding.message, finding.actual)
        for finding in validation.findings
        if finding.severity.value == "FAIL"
    ]


def test_lod1_fallback_is_rejected_when_original_exceeds_budget() -> None:
    script = runpy.run_path(str(AssemblyProcessor.get_script_path()))
    require_budget = script["_require_lod1_fallback_budget"]
    with pytest.raises(RuntimeError, match=r"preserving LOD0 geometry \(12 triangles\).*budget 10"):
        require_budget("hull", 12, 10)


def test_blender_assembly_process_rejects_spec_changed_after_source_pin(tmp_path: Path) -> None:
    """A changed specification must be rejected by provenance authentication before DCC."""
    if not _blender_available():
        pytest.skip("Blender executable not available on host")

    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile)

    # The retained provenance binds the original dimensions and cannot authenticate a changed spec.
    ingest_result = _setup_ingested_package(tmp_path, spec)
    object.__setattr__(spec.dimensions, "width_m", 3.0)

    out_dir = tmp_path / "dim_mismatch_dir"
    out_dir.mkdir(parents=True, exist_ok=True)
    processed_glb = out_dir / "out.glb"
    report_file = out_dir / "report.json"

    processor = AssemblyProcessor()
    with pytest.raises(ValidationError, match="spec_fingerprint"):
        processor.process_assembly(
            package=ingest_result,
            spec=spec,
            expected_provenance_sha256=ingest_result.retained_provenance_sha256,
            processed_glb_path=processed_glb,
            report_path=report_file,
            timeout_seconds=30.0,
        )

    # Output file must not have been created
    assert not processed_glb.is_file()


def test_blender_assembly_semantics_repeat_across_fresh_runs(tmp_path: Path) -> None:
    if not _blender_available():
        pytest.skip("Blender executable not available on host")
    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile)
    ingest_result = _setup_ingested_package(tmp_path, spec)
    processor = AssemblyProcessor()
    outputs: list[tuple[dict[str, Any], bytes]] = []
    for run_index in range(2):
        out_dir = tmp_path / f"semantic_run_{run_index}"
        out_dir.mkdir()
        result = processor.process_assembly(
            package=ingest_result,
            spec=spec,
            expected_provenance_sha256=ingest_result.retained_provenance_sha256,
            processed_glb_path=out_dir / "processed.glb",
            report_path=out_dir / "report.json",
            timeout_seconds=45.0,
        )
        assert result.status == "SUCCESS"
        outputs.append(_read_glb_bytes(result.processed_glb_path.read_bytes(), 50 * 1024 * 1024))

    left, left_binary = outputs[0]
    right, right_binary = outputs[1]
    left_nodes = {node["name"]: node for node in left["nodes"]}
    right_nodes = {node["name"]: node for node in right["nodes"]}
    assert set(left_nodes) == set(right_nodes)

    def parent_names(document: dict[str, Any]) -> dict[str, str | None]:
        names = {index: node["name"] for index, node in enumerate(document["nodes"])}
        return {
            document["nodes"][child]["name"]: names[parent]
            for parent, node in enumerate(document["nodes"])
            for child in node.get("children", [])
        }

    assert parent_names(left) == parent_names(right)
    for name in left_nodes:
        left_node, right_node = left_nodes[name], right_nodes[name]
        left_matrix, right_matrix = _node_matrix(left_node), _node_matrix(right_node)
        for row in range(4):
            for column in range(4):
                assert abs(left_matrix[row][column] - right_matrix[row][column]) <= 1e-5
        assert left_node.get("extras", {}) == right_node.get("extras", {})
        if "mesh" in left_node:
            left_mesh_name = left["meshes"][left_node["mesh"]]["name"]
            right_mesh_name = right["meshes"][right_node["mesh"]]["name"]
            assert left_mesh_name == right_mesh_name
            assert triangle_soups_equivalent(
                _triangle_soup(left, left_binary, left_mesh_name),
                _triangle_soup(right, right_binary, right_mesh_name),
            )
