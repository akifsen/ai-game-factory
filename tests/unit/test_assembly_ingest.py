"""Unit and bounded integration tests for assembly source ingest and standalone verification."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import struct
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.assets import assembly_ingest as assembly_ingest_module
from gamefactory.adapters.assets.assembly_ingest import (
    ingest_assembly_source,
    inspect_untrusted_assembly_package,
    preflight_assembly_glb,
    verify_retained_assembly,
)
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.core.domain.assembly_source import (
    MAX_ASSEMBLY_SOURCE_BYTES,
    MAX_PROVENANCE_SIDECAR_BYTES,
)
from gamefactory.core.domain.asset_contracts import (
    AssetSpecificationV07,
    parse_asset_specification_v07,
)
from gamefactory.core.domain.asset_profiles import (
    AssetProfileV07,
    ProfileRegistry,
    parse_profile_document_v07,
)
from gamefactory.core.domain.errors import ValidationError


def _profile(
    *,
    geometry_mode: str = "assembly",
    collider: str = "box",
    required_sockets: bool = True,
    pivot_tolerance_m: float = 0.01,
    basis_tolerance_deg: float = 1.0,
    socket_position_tolerance_m: float = 0.01,
    socket_angle_tolerance_deg: float = 1.0,
) -> AssetProfileV07:
    from gamefactory.core.domain.asset_profiles import builtin_registry

    base = builtin_registry().get("static_prop").document.model_dump(mode="json")
    assembly = {
        "roles": ["hull", "turret", "barrel"],
        "required_roles": ["hull", "turret", "barrel"],
        "role_motion_constraints": {},
        "required_sockets": (
            [
                {
                    "socket_id": "muzzle",
                    "parent_role": "barrel",
                    "placement": "forward_end",
                    "forward_end_fraction": 0.2,
                    "rest_forward": [0, 0, -1],
                }
            ]
            if required_sockets
            else []
        ),
        "pivot_tolerance_m": pivot_tolerance_m,
        "basis_tolerance_deg": basis_tolerance_deg,
        "socket_position_tolerance_m": socket_position_tolerance_m,
        "socket_angle_tolerance_deg": socket_angle_tolerance_deg,
    }
    data = {
        "schema_version": "asset-profile-0.7.0",
        "profile_id": "test_assembly_ingest_profile",
        "version": 1,
        "categories": ["vehicle"],
        "review_views": ["front"],
        "framing": base["framing"],
        "dimension_rules": {"min_m": 0.1, "max_m": 10.0},
        "processing": {
            "lod0_required": True,
            "lod1_required": False,
            "allowed_lod_policies": ["lod0_only", "lod0_lod1"],
            "allowed_collider_policies": [collider],
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
        "geometry_mode": geometry_mode,
        "accepted_source_kinds": [
            "local_operator_assembly" if geometry_mode == "assembly" else "provider_generated"
        ],
        "assembly": assembly if geometry_mode == "assembly" else None,
    }
    return AssetProfileV07(parse_profile_document_v07(data))


def _spec(
    profile: AssetProfileV07 | None = None,
    *,
    source_kind: str = "local_operator_assembly",
    sockets: bool = True,
    parts: list[dict[str, Any]] | None = None,
) -> AssetSpecificationV07:
    prof = profile or _profile()
    declared_parts = (
        parts
        if parts is not None
        else [
            {
                "part_id": "hull",
                "role": "hull",
                "parent": "root",
                "pivot": {
                    "position_m": [0, 0, 0],
                    "basis": "identity",
                    "motion": {"kind": "fixed"},
                },
            },
            {
                "part_id": "turret",
                "role": "turret",
                "parent": "hull",
                "pivot": {
                    "position_m": [0, 0, 0],
                    "basis": "identity",
                    "motion": {"kind": "fixed"},
                },
            },
            {
                "part_id": "barrel",
                "role": "barrel",
                "parent": "turret",
                "pivot": {
                    "position_m": [0, 0, 0],
                    "basis": "identity",
                    "motion": {"kind": "fixed"},
                },
            },
        ]
    )
    data: dict[str, Any] = {
        "schema_version": "0.7.0",
        "asset_id": "test_assembly_tank",
        "category": "vehicle",
        "profile": prof.profile_id,
        "profile_version": prof.version,
        "intent": "Ingest test assembly tank",
        "source_kind": source_kind,
        "dimensions": {"width_m": 2.0, "height_m": 1.5, "depth_m": 3.0},
        "origin_policy": "center",
        "lod_policy": "lod0_only",
        "geometry_budget": {
            "max_triangles_lod0": 10000,
            "max_triangles_lod1": 5000,
            "lod_ratio": 0.5,
        },
        "material_budget": {"max_materials": 4},
        "texture_budget": {"max_dimension": 2048},
        "collider": {"policy": "box"},
    }
    if source_kind == "local_operator_assembly":
        data["parts"] = declared_parts
        data["sockets"] = (
            [
                {
                    "socket_id": "muzzle",
                    "parent_part": "barrel",
                    "translation_m": [0, 0, -0.49],
                    "rotation": "identity",
                    "placement": "forward_end",
                }
            ]
            if sockets
            else None
        )
    registry = ProfileRegistry(available=(), unsupported=(), available_v07=(prof,))
    return parse_asset_specification_v07(data, registry=registry)


def _read_glb(raw: bytes) -> tuple[dict[str, Any], bytes]:
    json_len = struct.unpack_from("<I", raw, 12)[0]
    document = json.loads(raw[20 : 20 + json_len].decode().rstrip())
    binary_offset = 20 + json_len + 8
    return document, raw[binary_offset:]


def _write_glb(path: Path, document: dict[str, Any], binary: bytes) -> None:
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


def _generate_valid_assembly_glb(
    path: Path,
    *,
    source_front: str = "-Z",
    root_translation: list[float] | None = None,
    root_rotation: list[float] | None = None,
) -> Path:
    """Generate a strictly valid source GLB adhering to ADR 0013/0014/0016 contract:

    - Exactly one identity ROOT node in active scene
    - root PART direct child of ROOT
    - Declared LOD0 direct identity mesh children and socket leaves only
    - Top PART rotated 180° around +Y if source_front == '+Z'
    """
    create_box_glb(
        width_m=2,
        depth_m=3,
        height_m=1.5,
        mesh_name="base",
        origin="center",
        include_collider=False,
        output_path=path,
    )
    doc, binary = _read_glb(path.read_bytes())
    doc["meshes"][0]["name"] = "SM_test_assembly_tank_hull_LOD0"

    top_trans = root_translation if root_translation is not None else [0, 0, 0]
    top_rot = (
        root_rotation
        if root_rotation is not None
        else ([0.0, 1.0, 0.0, 0.0] if source_front == "+Z" else [0.0, 0.0, 0.0, 1.0])
    )

    doc["nodes"] = [
        {
            "name": "ROOT",
            "children": [1],
            "translation": [0, 0, 0],
            "rotation": [0, 0, 0, 1],
            "scale": [1, 1, 1],
        },
        {
            "name": "PART_hull",
            "children": [2, 3],
            "translation": top_trans,
            "rotation": top_rot,
            "scale": [1, 1, 1],
            "extras": {"gf_motion": "fixed"},
        },
        {
            "name": "SM_test_assembly_tank_hull_LOD0",
            "mesh": 0,
            "translation": [0, 0, 0],
            "rotation": [0, 0, 0, 1],
            "scale": [1, 1, 1],
        },
        {
            "name": "PART_turret",
            "children": [4, 5],
            "translation": [0, 0, 0],
            "rotation": [0, 0, 0, 1],
            "scale": [1, 1, 1],
            "extras": {"gf_motion": "fixed"},
        },
        {
            "name": "SM_test_assembly_tank_turret_LOD0",
            "mesh": 0,
            "translation": [0, 0, 0],
            "rotation": [0, 0, 0, 1],
            "scale": [1, 1, 1],
        },
        {
            "name": "PART_barrel",
            "children": [6, 7],
            "translation": [0, 0, 0],
            "rotation": [0, 0, 0, 1],
            "scale": [1, 1, 1],
            "extras": {"gf_motion": "fixed"},
        },
        {
            "name": "SM_test_assembly_tank_barrel_LOD0",
            "mesh": 0,
            "translation": [0, 0, 0],
            "rotation": [0, 0, 0, 1],
            "scale": [1, 1, 1],
        },
        {
            "name": "SOCKET_muzzle",
            "translation": [0, 0, -0.49],
            "rotation": [0, 0, 0, 1],
            "scale": [1, 1, 1],
        },
    ]
    doc["scenes"] = [{"nodes": [0]}]
    doc["scene"] = 0
    _write_glb(path, doc, binary)
    return path


def test_positive_generated_small_self_contained_glb_ingest_and_verify(tmp_path: Path) -> None:
    managed_root = tmp_path / "managed_root"
    managed_root.mkdir()
    source_glb = _generate_valid_assembly_glb(tmp_path / "tank.glb")
    spec = _spec()

    result = ingest_assembly_source(
        source_glb_path=source_glb,
        managed_root=managed_root,
        relative_package_dir="sources/assemblies/tank_001",
        spec=spec,
        authoring_tool_name="Blender",
        authoring_tool_version="4.2.1",
        source_front="-Z",
        actor="operator_dave",
        reason="Authored tank baseline",
        expected_source_sha256=None,
    )

    assert result.package_dir.is_dir()
    assert result.retained_glb_path.is_file()
    assert result.retained_provenance_path.is_file()
    assert result.retained_glb_byte_size > 0
    assert len(result.retained_glb_sha256) == 64
    assert result.provenance.paid is False
    assert result.provenance.source_front == "-Z"
    assert set(result.provenance.part_map.keys()) == {"hull", "turret", "barrel"}
    assert set(result.provenance.socket_map.keys()) == {"muzzle"}

    # Authenticated standalone verify succeeds with mandatory pinned digest
    verified = verify_retained_assembly(
        package_dir="sources/assemblies/tank_001",
        spec=spec,
        managed_root=managed_root,
        expected_provenance_sha256=result.retained_provenance_sha256,
    )
    assert verified.retained_glb_sha256 == result.retained_glb_sha256
    assert verified.provenance.spec_fingerprint == result.spec_fingerprint


def test_oversized_canonical_provenance_is_rejected_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root = tmp_path / "managed_root"
    managed_root.mkdir()
    source_glb = _generate_valid_assembly_glb(tmp_path / "tank.glb")
    spec = _spec()
    monkeypatch.setattr(
        assembly_ingest_module.AssemblySourceProvenance,
        "to_canonical_bytes",
        lambda _self: b"x" * (MAX_PROVENANCE_SIDECAR_BYTES + 1),
    )

    with pytest.raises(ValidationError, match="Canonical provenance sidecar exceeds"):
        ingest_assembly_source(
            source_glb_path=source_glb,
            managed_root=managed_root,
            relative_package_dir="sources/assemblies/oversized_provenance",
            spec=spec,
            authoring_tool_name="Blender",
            authoring_tool_version="4.2.1",
            source_front="-Z",
            actor="operator_dave",
            reason="bounded sidecar regression",
        )

    assert not (managed_root / "sources" / "assemblies" / "oversized_provenance").exists()
    assert not list(managed_root.rglob(".tmp_assembly_*"))


def test_completion_marker_is_published_after_package_data_staging_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root = tmp_path / "managed_root"
    managed_root.mkdir()
    source_glb = _generate_valid_assembly_glb(tmp_path / "tank.glb")
    target_dir = managed_root / "sources" / "assemblies" / "marker_last"
    real_link = os.link
    final_marker_observed = False

    def observe_link(src: Any, dst: Any) -> None:
        nonlocal final_marker_observed
        dst_path = Path(dst)
        if dst_path.name == "publication_marker.json" and dst_path.parent == target_dir:
            final_marker_observed = True
            staging_dirs = list(target_dir.parent.glob(".tmp_assembly_marker_last_*"))
            assert len(staging_dirs) == 1
            assert not (staging_dirs[0] / "source.glb").exists()
            assert not (staging_dirs[0] / "source_provenance.json").exists()
            assert (staging_dirs[0] / "publication_marker.json").is_file()
        real_link(src, dst)

    monkeypatch.setattr(os, "link", observe_link)
    result = ingest_assembly_source(
        source_glb_path=source_glb,
        managed_root=managed_root,
        relative_package_dir="sources/assemblies/marker_last",
        spec=_spec(),
        authoring_tool_name="Blender",
        authoring_tool_version="4.2.1",
        source_front="-Z",
        actor="operator_dave",
        reason="marker-last regression",
    )

    assert final_marker_observed
    assert (result.package_dir / "publication_marker.json").is_file()
    assert not list(target_dir.parent.glob(".tmp_assembly_marker_last_*"))


@pytest.mark.parametrize(
    ("field_path", "invalid_value"),
    [
        (("scenes", 0, "nodes", 0), False),
        (("nodes", 1, "children", 0), True),
    ],
)
def test_source_preflight_rejects_boolean_glb_indices(
    tmp_path: Path, field_path: tuple[str | int, ...], invalid_value: bool
) -> None:
    glb = _generate_valid_assembly_glb(tmp_path / "bool_index.glb")
    document, binary = _read_glb(glb.read_bytes())
    value: Any = document
    for key in field_path[:-1]:
        value = value[key]
    value[field_path[-1]] = invalid_value
    _write_glb(glb, document, binary)

    with pytest.raises(ValidationError, match="must be an integer"):
        preflight_assembly_glb(glb.read_bytes(), _spec())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document: document.update(scenes=None),
        lambda document: document.update(nodes=None),
        lambda document: document.update(meshes=None),
        lambda document: document.update(accessors=None),
        lambda document: document.update(bufferViews=None),
        lambda document: document["nodes"][1].update(children=None),
    ],
    ids=("scenes", "nodes", "meshes", "accessors", "bufferViews", "children"),
)
def test_source_preflight_rejects_malformed_collections_as_validation_errors(
    tmp_path: Path, mutation: Any
) -> None:
    glb = _generate_valid_assembly_glb(tmp_path / "malformed_collection.glb")
    document, binary = _read_glb(glb.read_bytes())
    mutation(document)
    _write_glb(glb, document, binary)

    with pytest.raises(ValidationError):
        preflight_assembly_glb(glb.read_bytes(), _spec())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("translation", ["invalid", 0, 0]),
        ("rotation", [0, 0, 0, 0]),
        ("rotation", [0, 0, 0, 2]),
        ("scale", [True, 1, 1]),
        ("scale", [-1, -1, 1]),
    ],
)
def test_source_preflight_rejects_malformed_raw_node_transforms(
    tmp_path: Path, field: str, value: list[Any]
) -> None:
    glb = _generate_valid_assembly_glb(tmp_path / "malformed_transform.glb")
    document, binary = _read_glb(glb.read_bytes())
    part = next(node for node in document["nodes"] if node["name"] == "PART_hull")
    part[field] = value
    _write_glb(glb, document, binary)

    with pytest.raises(ValidationError):
        preflight_assembly_glb(glb.read_bytes(), _spec())


def test_source_preflight_rejects_fixed_motion_axis_and_invalid_motion_axis(
    tmp_path: Path,
) -> None:
    glb = _generate_valid_assembly_glb(tmp_path / "invalid_axis.glb")
    document, binary = _read_glb(glb.read_bytes())
    turret = next(node for node in document["nodes"] if node["name"] == "PART_turret")
    turret.setdefault("extras", {})["gf_axis"] = [0, 1, 0]
    _write_glb(glb, document, binary)
    with pytest.raises(ValidationError, match="unexpected 'gf_axis'"):
        preflight_assembly_glb(glb.read_bytes(), _spec())

    turret["extras"]["gf_motion"] = "revolute"
    turret["extras"]["gf_axis"] = ["invalid", 1, 0]
    _write_glb(glb, document, binary)
    spec = _spec()
    turret_spec = spec.parts[1]
    motion = turret_spec.pivot.motion.model_copy(update={"kind": "revolute", "axis": [0, 1, 0]})
    pivot = turret_spec.pivot.model_copy(update={"motion": motion})
    turret_spec = turret_spec.model_copy(update={"pivot": pivot})
    spec = spec.model_copy(update={"parts": [spec.parts[0], turret_spec, *spec.parts[2:]]})
    with pytest.raises(ValidationError, match="gf_axis.*numeric"):
        preflight_assembly_glb(glb.read_bytes(), spec)


def test_source_preflight_rejects_negative_raw_scale_even_when_basis_matches(
    tmp_path: Path,
) -> None:
    glb = _generate_valid_assembly_glb(tmp_path / "negative_scale_rotated_spec.glb")
    document, binary = _read_glb(glb.read_bytes())
    root_part = next(node for node in document["nodes"] if node["name"] == "PART_hull")
    root_part["scale"] = [-1, -1, 1]
    _write_glb(glb, document, binary)

    spec = _spec()
    root = spec.parts[0]
    pivot = root.pivot.model_copy(update={"basis": [0, 0, 1, 0]})
    rotated_root = root.model_copy(update={"pivot": pivot})
    rotated_spec = spec.model_copy(update={"parts": [rotated_root, *spec.parts[1:]]})
    with pytest.raises(ValidationError, match="scale components must all be positive"):
        preflight_assembly_glb(glb.read_bytes(), rotated_spec)


def test_source_preflight_rejects_non_affine_node_matrix(tmp_path: Path) -> None:
    glb = _generate_valid_assembly_glb(tmp_path / "non_affine_matrix.glb")
    document, binary = _read_glb(glb.read_bytes())
    part = next(node for node in document["nodes"] if node["name"] == "PART_hull")
    for field in ("translation", "rotation", "scale"):
        part.pop(field, None)
    part["matrix"] = [1, 0, 0, 0.1, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    _write_glb(glb, document, binary)

    with pytest.raises(ValidationError, match="matrix must be affine"):
        preflight_assembly_glb(glb.read_bytes(), _spec())


def test_reject_wrong_expected_hash(tmp_path: Path) -> None:
    managed_root = tmp_path / "managed_root"
    managed_root.mkdir()
    source_glb = _generate_valid_assembly_glb(tmp_path / "tank.glb")
    spec = _spec()

    with pytest.raises(ValidationError, match="Source SHA-256 mismatch"):
        ingest_assembly_source(
            source_glb_path=source_glb,
            managed_root=managed_root,
            relative_package_dir="sources/assemblies/tank_hash_mismatch",
            spec=spec,
            authoring_tool_name="Blender",
            authoring_tool_version="4.2.1",
            source_front="-Z",
            actor="operator_dave",
            reason="test",
            expected_source_sha256="0" * 64,
        )


def test_reject_wrong_expected_size(tmp_path: Path) -> None:
    managed_root = tmp_path / "managed_root"
    managed_root.mkdir()
    source_glb = _generate_valid_assembly_glb(tmp_path / "tank.glb")
    spec = _spec()

    with pytest.raises(ValidationError, match="Source byte size mismatch"):
        ingest_assembly_source(
            source_glb_path=source_glb,
            managed_root=managed_root,
            relative_package_dir="sources/assemblies/tank_size_mismatch",
            spec=spec,
            authoring_tool_name="Blender",
            authoring_tool_version="4.2.1",
            source_front="-Z",
            actor="operator_dave",
            reason="test",
            expected_source_byte_size=9999999,
        )


def test_reject_wrong_maps_missing_part_or_socket_in_glb(tmp_path: Path) -> None:
    source_glb = tmp_path / "mutated_maps.glb"
    _generate_valid_assembly_glb(source_glb)
    doc, binary = _read_glb(source_glb.read_bytes())
    # Rename PART_turret to other name
    for node in doc["nodes"]:
        if node.get("name") == "PART_turret":
            node["name"] = "PART_wrong_name"
            break
    _write_glb(source_glb, doc, binary)
    spec = _spec()

    with pytest.raises(ValidationError, match="Required part node 'PART_turret' is missing"):
        preflight_assembly_glb(source_glb.read_bytes(), spec)


def test_reject_wrong_parent_in_glb(tmp_path: Path) -> None:
    source_glb = tmp_path / "mutated_parent.glb"
    _generate_valid_assembly_glb(source_glb)
    doc, binary = _read_glb(source_glb.read_bytes())
    # Find PART_hull and PART_turret and PART_barrel
    hull_idx = next(i for i, n in enumerate(doc["nodes"]) if n.get("name") == "PART_hull")
    turret_idx = next(i for i, n in enumerate(doc["nodes"]) if n.get("name") == "PART_turret")
    barrel_idx = next(i for i, n in enumerate(doc["nodes"]) if n.get("name") == "PART_barrel")

    # Re-parent barrel under hull instead of turret
    doc["nodes"][turret_idx]["children"].remove(barrel_idx)
    doc["nodes"][hull_idx]["children"].append(barrel_idx)
    _write_glb(source_glb, doc, binary)
    spec = _spec()

    with pytest.raises(ValidationError, match="does not match declared parent"):
        preflight_assembly_glb(source_glb.read_bytes(), spec)


def test_reject_wrong_source_kind_in_spec(tmp_path: Path) -> None:
    managed_root = tmp_path / "managed_root"
    managed_root.mkdir()
    source_glb = _generate_valid_assembly_glb(tmp_path / "tank.glb")
    profile_sm = _profile(geometry_mode="single_mesh", required_sockets=False)
    spec_sm = _spec(profile_sm, source_kind="provider_generated", sockets=False)

    with pytest.raises(ValidationError, match="must be 'local_operator_assembly'"):
        ingest_assembly_source(
            source_glb_path=source_glb,
            managed_root=managed_root,
            relative_package_dir="sources/assemblies/tank_sm",
            spec=spec_sm,
            authoring_tool_name="Blender",
            authoring_tool_version="4.2.1",
            source_front="-Z",
            actor="operator_dave",
            reason="test",
        )


def test_reject_wrong_front(tmp_path: Path) -> None:
    managed_root = tmp_path / "managed_root"
    managed_root.mkdir()
    source_glb = _generate_valid_assembly_glb(tmp_path / "tank.glb")
    spec = _spec()

    with pytest.raises(ValidationError, match="source_front must be '-Z' or '\\+Z'"):
        ingest_assembly_source(
            source_glb_path=source_glb,
            managed_root=managed_root,
            relative_package_dir="sources/assemblies/tank_front",
            spec=spec,
            authoring_tool_name="Blender",
            authoring_tool_version="4.2.1",
            source_front="+X",  # type: ignore[arg-type]
            actor="operator_dave",
            reason="test",
        )


def test_positive_and_negative_source_front_normalization(tmp_path: Path) -> None:
    # 1. Authored GLB with declared source_front: "+Z" and matching 180° rotation on top PART passes
    source_glb_pos = _generate_valid_assembly_glb(tmp_path / "tank_pos.glb", source_front="+Z")
    spec = _spec()
    preflight_assembly_glb(source_glb_pos.read_bytes(), spec, source_front="+Z")

    # 2. Authored GLB with declared source_front: "+Z" but raw top PART has identity (not rotated) fails
    source_glb_neg = _generate_valid_assembly_glb(tmp_path / "tank_neg.glb", source_front="-Z")
    with pytest.raises(ValidationError, match="does not match declared source_front '\\+Z'"):
        preflight_assembly_glb(source_glb_neg.read_bytes(), spec, source_front="+Z")


def test_reject_external_resources(tmp_path: Path) -> None:
    source_glb = tmp_path / "external_buf.glb"
    _generate_valid_assembly_glb(source_glb)
    doc, binary = _read_glb(source_glb.read_bytes())
    doc["buffers"][0]["uri"] = "external_mesh.bin"
    _write_glb(source_glb, doc, binary)
    spec = _spec()

    with pytest.raises(ValidationError, match="only one embedded GLB buffer is supported"):
        preflight_assembly_glb(source_glb.read_bytes(), spec)

    # External image URI
    img_glb = tmp_path / "external_img.glb"
    _generate_valid_assembly_glb(img_glb)
    doc, binary = _read_glb(img_glb.read_bytes())
    doc["images"] = [{"uri": "http://evil.com/texture.png"}]
    _write_glb(img_glb, doc, binary)
    with pytest.raises(ValidationError, match="external image URI is forbidden"):
        preflight_assembly_glb(img_glb.read_bytes(), spec)


def test_reject_oversize_glb(tmp_path: Path) -> None:
    oversize_bytes = b"glTF" + b"\0" * (MAX_ASSEMBLY_SOURCE_BYTES + 10)
    spec = _spec()
    with pytest.raises(ValidationError, match="exceeds limit"):
        preflight_assembly_glb(oversize_bytes, spec)


@pytest.mark.parametrize(
    "corrupt_maker, expected_err",
    [
        (lambda b: b"BAD_MAGIC" + b[9:], "Invalid GLB magic header"),
        (lambda b: struct.pack("<4sII", b"glTF", 1, len(b)) + b[12:], "Unsupported glTF version"),
        (lambda b: struct.pack("<4sII", b"glTF", 2, len(b) - 10) + b[12:], "declared length"),
        (lambda b: b[:15], "too small"),
    ],
)
def test_reject_parser_failures(tmp_path: Path, corrupt_maker: Any, expected_err: str) -> None:
    source_glb = tmp_path / "corrupt.glb"
    _generate_valid_assembly_glb(source_glb)
    corrupt_bytes = corrupt_maker(source_glb.read_bytes())
    spec = _spec()
    with pytest.raises(ValidationError, match=expected_err):
        preflight_assembly_glb(corrupt_bytes, spec)


# --- Item 1 Negative Tests: glb_validator._inspect reuse and geometry safety ---


def test_reject_no_geometry(tmp_path: Path) -> None:
    source_glb = tmp_path / "no_geom.glb"
    _generate_valid_assembly_glb(source_glb)
    doc, binary = _read_glb(source_glb.read_bytes())
    doc["meshes"] = []
    for node in doc["nodes"]:
        node.pop("mesh", None)
    _write_glb(source_glb, doc, binary)
    spec = _spec()

    with pytest.raises(ValidationError, match="contains no nonempty processable geometry"):
        preflight_assembly_glb(source_glb.read_bytes(), spec)


def test_reject_skins_and_animations(tmp_path: Path) -> None:
    source_glb = tmp_path / "skin.glb"
    _generate_valid_assembly_glb(source_glb)
    doc, binary = _read_glb(source_glb.read_bytes())
    doc["skins"] = [{"joints": [0]}]
    _write_glb(source_glb, doc, binary)
    spec = _spec()

    with pytest.raises(ValidationError, match="skins/rigging are forbidden"):
        preflight_assembly_glb(source_glb.read_bytes(), spec)

    # Animations
    anim_glb = tmp_path / "anim.glb"
    _generate_valid_assembly_glb(anim_glb)
    doc, binary = _read_glb(anim_glb.read_bytes())
    doc["animations"] = [{"name": "idle", "channels": [], "samplers": []}]
    _write_glb(anim_glb, doc, binary)
    with pytest.raises(ValidationError, match="animations are forbidden"):
        preflight_assembly_glb(anim_glb.read_bytes(), spec)


def test_reject_excessive_accessor_count(tmp_path: Path) -> None:
    source_glb = tmp_path / "bad_accessor.glb"
    _generate_valid_assembly_glb(source_glb)
    doc, binary = _read_glb(source_glb.read_bytes())
    # Modify an accessor count to 999_999_999
    doc["accessors"][0]["count"] = 999_999_999
    _write_glb(source_glb, doc, binary)
    spec = _spec()

    with pytest.raises(ValidationError, match="accessor count is invalid or exceeds safety limit"):
        preflight_assembly_glb(source_glb.read_bytes(), spec)


def test_reject_node_cycle_and_excessive_depth(tmp_path: Path) -> None:
    # 1. Cycle: turrret's child is hull
    source_glb = tmp_path / "cycle.glb"
    _generate_valid_assembly_glb(source_glb)
    doc, binary = _read_glb(source_glb.read_bytes())
    hull_idx = next(i for i, n in enumerate(doc["nodes"]) if n.get("name") == "PART_hull")
    turret_idx = next(i for i, n in enumerate(doc["nodes"]) if n.get("name") == "PART_turret")
    doc["nodes"][turret_idx]["children"].append(hull_idx)
    _write_glb(source_glb, doc, binary)
    spec = _spec()

    with pytest.raises(ValidationError, match="(multiply-parented|hierarchy cycle)"):
        preflight_assembly_glb(source_glb.read_bytes(), spec)


def test_reject_corrupt_embedded_image(tmp_path: Path) -> None:
    source_glb = tmp_path / "corrupt_img.glb"
    _generate_valid_assembly_glb(source_glb)
    doc, binary = _read_glb(source_glb.read_bytes())

    # Add a bufferView with corrupt image bytes
    corrupt_bytes = b"NOT_A_VALID_PNG_OR_JPEG_IMAGE_BYTES"
    orig_bin_len = len(binary)
    padded_bin = binary + corrupt_bytes + b"\0" * ((-len(corrupt_bytes)) % 4)
    doc["bufferViews"].append(
        {"buffer": 0, "byteOffset": orig_bin_len, "byteLength": len(corrupt_bytes)}
    )
    doc["buffers"][0]["byteLength"] = len(padded_bin)
    doc["images"] = [{"bufferView": len(doc["bufferViews"]) - 1, "mimeType": "image/png"}]
    _write_glb(source_glb, doc, padded_bin)
    spec = _spec()

    with pytest.raises(ValidationError, match="embedded image is missing, corrupt, or unsupported"):
        preflight_assembly_glb(source_glb.read_bytes(), spec)


# --- Item 2 Negative Tests: Authenticated Verification & Pinning ---


def test_authenticated_verification_tamper_and_mandatory_pin(tmp_path: Path) -> None:
    managed_root = tmp_path / "managed_root"
    managed_root.mkdir()
    source_glb = _generate_valid_assembly_glb(tmp_path / "auth_test.glb")
    spec = _spec()

    result = ingest_assembly_source(
        source_glb_path=source_glb,
        managed_root=managed_root,
        relative_package_dir="sources/assemblies/auth_001",
        spec=spec,
        authoring_tool_name="Blender",
        authoring_tool_version="4.2.1",
        source_front="-Z",
        actor="operator_dave",
        reason="Initial baseline",
    )

    # 1. Missing expected_provenance_sha256 raises TypeError
    with pytest.raises(TypeError):
        verify_retained_assembly(  # type: ignore[call-arg]
            package_dir="sources/assemblies/auth_001",
            spec=spec,
            managed_root=managed_root,
        )

    # 2. Malformed pin
    with pytest.raises(ValidationError, match="must be a valid 64-character hex SHA-256"):
        verify_retained_assembly(
            package_dir="sources/assemblies/auth_001",
            spec=spec,
            managed_root=managed_root,
            expected_provenance_sha256="not_a_valid_sha256",
        )

    # 3. Tamper sidecar metadata with old pinned digest
    prov_dict = json.loads(result.retained_provenance_path.read_text("utf-8"))
    prov_dict["actor"] = "attacker_mallory"
    prov_dict["reason"] = "tampered reason"
    tampered_bytes = json.dumps(prov_dict).encode("utf-8")
    result.retained_provenance_path.write_bytes(tampered_bytes)

    # 3a. Authenticated verify rejects due to pinned digest mismatch
    with pytest.raises(ValidationError, match="does not match pinned"):
        verify_retained_assembly(
            package_dir="sources/assemblies/auth_001",
            spec=spec,
            managed_root=managed_root,
            expected_provenance_sha256=result.retained_provenance_sha256,
        )

    # 3b. Untrusted inspect also rejects if marker was not updated (marker gate catches tamper)
    with pytest.raises(ValidationError, match="does not match provenance digest"):
        inspect_untrusted_assembly_package(
            package_dir="sources/assemblies/auth_001",
            spec=spec,
            managed_root=managed_root,
        )

    # 3c. Even if attacker coordinates rewrite of publication marker to match tampered sidecar:
    # verify STILL rejects because pinned digest is immutable!
    import hashlib

    marker_path = result.package_dir / "publication_marker.json"
    marker_dict = json.loads(marker_path.read_text("utf-8"))
    marker_dict["provenance_sha256"] = hashlib.sha256(tampered_bytes).hexdigest()
    marker_dict["provenance_byte_size"] = len(tampered_bytes)
    marker_path.write_text(json.dumps(marker_dict), "utf-8")

    with pytest.raises(ValidationError, match="does not match pinned"):
        verify_retained_assembly(
            package_dir="sources/assemblies/auth_001",
            spec=spec,
            managed_root=managed_root,
            expected_provenance_sha256=result.retained_provenance_sha256,
        )

    # 4. Untrusted inspection function is unauthenticated: once internally consistent, it inspects without pin
    untrusted = inspect_untrusted_assembly_package(
        package_dir="sources/assemblies/auth_001",
        spec=spec,
        managed_root=managed_root,
    )
    assert untrusted.provenance.actor == "attacker_mallory"


# --- Item 3 Negative & Concurrency Tests: Exclusive Ownership & No Overwrite ---


def test_exclusive_ownership_prevents_overwriting_preexisting_directory(tmp_path: Path) -> None:
    managed_root = tmp_path / "managed_root"
    managed_root.mkdir()
    source_glb = _generate_valid_assembly_glb(tmp_path / "tank.glb")
    spec = _spec()

    # Pre-create empty directory
    target_dir = managed_root / "sources" / "assemblies" / "pre_existing_empty"
    target_dir.mkdir(parents=True, exist_ok=False)
    assert target_dir.is_dir()

    with pytest.raises(ValidationError, match="already exists"):
        ingest_assembly_source(
            source_glb_path=source_glb,
            managed_root=managed_root,
            relative_package_dir="sources/assemblies/pre_existing_empty",
            spec=spec,
            authoring_tool_name="Blender",
            authoring_tool_version="4.2.1",
            source_front="-Z",
            actor="operator_dave",
            reason="test",
        )

    # Ensure pre-existing empty directory was NOT deleted or overwritten
    assert target_dir.is_dir()
    assert len(list(target_dir.iterdir())) == 0


def test_race_injection_at_publication_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root = tmp_path / "managed_root"
    managed_root.mkdir()
    source_glb = _generate_valid_assembly_glb(tmp_path / "tank.glb")
    spec = _spec()

    target_pkg = managed_root / "sources" / "assemblies" / "race_target"

    # Inject directory creation concurrently right before safe_package_dir.mkdir
    real_mkdir = Path.mkdir

    def injected_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == target_pkg and not self.exists():
            # Simulate concurrent actor creating the directory right before our claim
            real_mkdir(self, parents=False, exist_ok=False)
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", injected_mkdir)

    with pytest.raises(ValidationError, match="already exists or was claimed concurrently"):
        ingest_assembly_source(
            source_glb_path=source_glb,
            managed_root=managed_root,
            relative_package_dir="sources/assemblies/race_target",
            spec=spec,
            authoring_tool_name="Blender",
            authoring_tool_version="4.2.1",
            source_front="-Z",
            actor="operator_dave",
            reason="race injection test",
        )

    # Ensure concurrent directory remains untouched and no staging garbage left behind
    assert target_pkg.is_dir()
    parent_dir = target_pkg.parent
    stray_staging = [p for p in parent_dir.iterdir() if p.name.startswith(".tmp_assembly")]
    assert len(stray_staging) == 0


def test_concurrent_publication_race_exclusive_claim(tmp_path: Path) -> None:
    managed_root = tmp_path / "managed_root"
    managed_root.mkdir()
    (managed_root / "sources" / "assemblies").mkdir(parents=True, exist_ok=True)
    source_glb = _generate_valid_assembly_glb(tmp_path / "tank.glb")
    spec = _spec()

    def _attempt_ingest(worker_id: int) -> tuple[int, bool, str]:
        try:
            res = ingest_assembly_source(
                source_glb_path=source_glb,
                managed_root=managed_root,
                relative_package_dir="sources/assemblies/competing_package",
                spec=spec,
                authoring_tool_name="Blender",
                authoring_tool_version="4.2.1",
                source_front="-Z",
                actor=f"worker_{worker_id}",
                reason="race test",
            )
            return (worker_id, True, res.retained_glb_sha256)
        except ValidationError as exc:
            return (worker_id, False, str(exc))

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(_attempt_ingest, i) for i in range(4)]
        results = [f.result() for f in futures]

    successes = [r for r in results if r[1]]
    failures = [r for r in results if not r[1]]

    # Exactly one thread claims the package directory and succeeds; all others fail cleanly
    assert len(successes) == 1
    assert len(failures) == 3
    for f in failures:
        assert "already exists or was claimed concurrently" in f[2]


# --- Item 4 Tests: Pre-Resolution Lexical Component Checks ---


def test_reject_symlink_components_before_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root = tmp_path / "managed_root"
    managed_root.mkdir()
    source_glb = _generate_valid_assembly_glb(tmp_path / "real_tank.glb")
    spec = _spec()

    # Ingest baseline package
    result = ingest_assembly_source(
        source_glb_path=source_glb,
        managed_root=managed_root,
        relative_package_dir="sources/assemblies/valid_pkg",
        spec=spec,
        authoring_tool_name="Blender",
        authoring_tool_version="4.2.1",
        source_front="-Z",
        actor="operator_dave",
        reason="test",
    )

    # Monkeypatch to simulate a symlink component along the lexical path
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda self: self.name == "symlink_component" or real_is_symlink(self),
    )

    # 1. Source GLB with symlink component
    with pytest.raises(ValidationError, match="contains a symlink"):
        ingest_assembly_source(
            source_glb_path=tmp_path / "symlink_component" / "tank.glb",
            managed_root=managed_root,
            relative_package_dir="sources/assemblies/test_link",
            spec=spec,
            authoring_tool_name="Blender",
            authoring_tool_version="4.2.1",
            source_front="-Z",
            actor="operator_dave",
            reason="test",
        )

    # 2. Package dir with symlink component under managed_root
    with pytest.raises(ValidationError, match="contains a symlink"):
        verify_retained_assembly(
            package_dir="sources/symlink_component/valid_pkg",
            spec=spec,
            managed_root=managed_root,
            expected_provenance_sha256=result.retained_provenance_sha256,
        )

    # 3. Package dir with symlink component without managed_root
    with pytest.raises(ValidationError, match="contains a symlink"):
        verify_retained_assembly(
            package_dir=tmp_path / "symlink_component" / "valid_pkg",
            spec=spec,
            managed_root=None,
            expected_provenance_sha256=result.retained_provenance_sha256,
        )


# --- Item 7 Tests: Architecture GLB Contract & Hierarchy Alignment ---


def test_reject_missing_or_invalid_root_node(tmp_path: Path) -> None:
    # 1. Missing ROOT node: root node has wrong name
    source_glb = tmp_path / "no_root.glb"
    _generate_valid_assembly_glb(source_glb)
    doc, binary = _read_glb(source_glb.read_bytes())
    doc["nodes"][0]["name"] = "WRAPPER_ROOT"
    _write_glb(source_glb, doc, binary)
    spec = _spec()

    with pytest.raises(ValidationError, match="must be named 'ROOT'"):
        preflight_assembly_glb(source_glb.read_bytes(), spec)

    # 2. Non-identity ROOT node
    bad_root_glb = tmp_path / "bad_root.glb"
    _generate_valid_assembly_glb(bad_root_glb)
    doc, binary = _read_glb(bad_root_glb.read_bytes())
    doc["nodes"][0]["translation"] = [1.0, 0.0, 0.0]
    _write_glb(bad_root_glb, doc, binary)

    with pytest.raises(ValidationError, match="ROOT node transform must be identity"):
        preflight_assembly_glb(bad_root_glb.read_bytes(), spec)


def test_reject_arbitrary_wrappers_and_extra_nodes(tmp_path: Path) -> None:
    source_glb = tmp_path / "wrapper.glb"
    _generate_valid_assembly_glb(source_glb)
    doc, binary = _read_glb(source_glb.read_bytes())

    # Add an unauthorized wrapper between ROOT and PART_hull
    wrapper_idx = len(doc["nodes"])
    doc["nodes"].append({"name": "ARM_wrapper", "children": [1]})
    doc["nodes"][0]["children"] = [wrapper_idx]
    _write_glb(source_glb, doc, binary)
    spec = _spec()

    with pytest.raises(
        ValidationError, match="(ROOT node direct child must be 'PART_hull'|unauthorized node)"
    ):
        preflight_assembly_glb(source_glb.read_bytes(), spec)


def test_reject_processor_scope_lod1_or_colliders_in_source_glb(tmp_path: Path) -> None:
    source_glb = tmp_path / "processor_nodes.glb"
    _generate_valid_assembly_glb(source_glb)
    doc, binary = _read_glb(source_glb.read_bytes())

    # Add a LOD1 mesh child under PART_hull
    lod1_idx = len(doc["nodes"])
    doc["nodes"].append(
        {"name": "SM_test_assembly_tank_hull_LOD1", "mesh": 0, "translation": [0, 0, 0]}
    )
    hull_idx = next(i for i, n in enumerate(doc["nodes"]) if n.get("name") == "PART_hull")
    doc["nodes"][hull_idx]["children"].append(lod1_idx)
    _write_glb(source_glb, doc, binary)
    spec = _spec()

    with pytest.raises(
        ValidationError, match="(must have exactly 1 direct LOD0 mesh child|Unexpected child node)"
    ):
        preflight_assembly_glb(source_glb.read_bytes(), spec)


def test_reject_socket_with_children_or_mesh(tmp_path: Path) -> None:
    # 1. Socket with child node
    source_glb = tmp_path / "socket_child.glb"
    _generate_valid_assembly_glb(source_glb)
    doc, binary = _read_glb(source_glb.read_bytes())

    child_idx = len(doc["nodes"])
    doc["nodes"].append({"name": "sub_point"})
    sock_idx = next(i for i, n in enumerate(doc["nodes"]) if n.get("name") == "SOCKET_muzzle")
    doc["nodes"][sock_idx]["children"] = [child_idx]
    _write_glb(source_glb, doc, binary)
    spec = _spec()

    with pytest.raises(ValidationError, match="Socket node 'SOCKET_muzzle' must be a leaf"):
        preflight_assembly_glb(source_glb.read_bytes(), spec)

    # 2. Socket with mesh
    source_glb_mesh = tmp_path / "socket_mesh.glb"
    _generate_valid_assembly_glb(source_glb_mesh)
    doc_m, binary_m = _read_glb(source_glb_mesh.read_bytes())
    sock_idx_m = next(i for i, n in enumerate(doc_m["nodes"]) if n.get("name") == "SOCKET_muzzle")
    doc_m["nodes"][sock_idx_m]["mesh"] = 0
    _write_glb(source_glb_mesh, doc_m, binary_m)

    with pytest.raises(ValidationError, match="Socket node 'SOCKET_muzzle' cannot contain a mesh"):
        preflight_assembly_glb(source_glb_mesh.read_bytes(), spec)


def test_root_transform_parent_frame_order_and_normalization_fixtures(tmp_path: Path) -> None:
    """Noncommuting nonidentity root quaternion and nonzero root translation fixtures.

    Proves correct parent-frame order (raw = Ry180 * canonical) and rejects wrong parent-frame order,
    double rotation, and moved root under +/-Z source_front.
    """
    # 90 degrees rotation about X axis: q = [sqrt(2)/2, 0, 0, sqrt(2)/2]
    # Rx(90) = [[1, 0, 0], [0, 0, -1], [0, 1, 0]]
    # Ry180 * Rx(90) has quaternion [0, sqrt(2)/2, -sqrt(2)/2, 0]
    # In contrast, child-frame Rx(90) * Ry180 has quaternion [0, sqrt(2)/2, sqrt(2)/2, 0]
    # Nonzero root translation: [0.5, 1.2, -0.8]
    # Ry180 * [0.5, 1.2, -0.8] = [-0.5, 1.2, 0.8]
    q_can = [0.7071067811865475, 0.0, 0.0, 0.7071067811865475]
    pos_can = [0.5, 1.2, -0.8]

    custom_parts = [
        {
            "part_id": "hull",
            "role": "hull",
            "parent": "root",
            "pivot": {"position_m": pos_can, "basis": q_can, "motion": {"kind": "fixed"}},
        },
        {
            "part_id": "turret",
            "role": "turret",
            "parent": "hull",
            "pivot": {"position_m": [0, 0, 0], "basis": "identity", "motion": {"kind": "fixed"}},
        },
        {
            "part_id": "barrel",
            "role": "barrel",
            "parent": "turret",
            "pivot": {"position_m": [0, 0, 0], "basis": "identity", "motion": {"kind": "fixed"}},
        },
    ]
    spec = _spec(parts=custom_parts)

    # --- 1. Valid +Z source_front fixture ---
    raw_trans_pos_z = [-pos_can[0], pos_can[1], -pos_can[2]]  # [-0.5, 1.2, 0.8]
    raw_rot_pos_z = [0.0, 0.7071067811865475, -0.7071067811865475, 0.0]  # Ry180 * Rx(90)
    glb_pos = _generate_valid_assembly_glb(
        tmp_path / "root_pos_z.glb",
        source_front="+Z",
        root_translation=raw_trans_pos_z,
        root_rotation=raw_rot_pos_z,
    )
    preflight_assembly_glb(glb_pos.read_bytes(), spec, source_front="+Z")

    # --- 2. Negative test: Wrong parent-frame order (child-frame Rx(90) * Ry180) ---
    wrong_order_rot = [0.0, 0.7071067811865475, 0.7071067811865475, 0.0]
    glb_wrong_order = _generate_valid_assembly_glb(
        tmp_path / "root_wrong_order.glb",
        source_front="+Z",
        root_translation=raw_trans_pos_z,
        root_rotation=wrong_order_rot,
    )
    with pytest.raises(ValidationError, match="Root part 'PART_hull' rotation does not match"):
        preflight_assembly_glb(glb_wrong_order.read_bytes(), spec, source_front="+Z")

    # --- 3. Negative test: Double rotation (applying Ry180 twice yields unrotated canonical rotation) ---
    glb_double_rot = _generate_valid_assembly_glb(
        tmp_path / "root_double_rot.glb",
        source_front="+Z",
        root_translation=raw_trans_pos_z,
        root_rotation=q_can,
    )
    with pytest.raises(ValidationError, match="Root part 'PART_hull' rotation does not match"):
        preflight_assembly_glb(glb_double_rot.read_bytes(), spec, source_front="+Z")

    # --- 4. Negative test: Moved root (translation not rotated or zeroed) ---
    glb_moved_root = _generate_valid_assembly_glb(
        tmp_path / "root_moved.glb",
        source_front="+Z",
        root_translation=pos_can,  # Not rotated by Ry180!
        root_rotation=raw_rot_pos_z,
    )
    with pytest.raises(
        ValidationError,
        match="Root part 'PART_hull' translation .* does not match declared pivot position",
    ):
        preflight_assembly_glb(glb_moved_root.read_bytes(), spec, source_front="+Z")

    glb_zero_root = _generate_valid_assembly_glb(
        tmp_path / "root_zero.glb",
        source_front="+Z",
        root_translation=[0, 0, 0],  # Moved root to 0
        root_rotation=raw_rot_pos_z,
    )
    with pytest.raises(
        ValidationError,
        match="Root part 'PART_hull' translation .* does not match declared pivot position",
    ):
        preflight_assembly_glb(glb_zero_root.read_bytes(), spec, source_front="+Z")

    # --- 5. Valid -Z source_front fixture ---
    glb_neg_z = _generate_valid_assembly_glb(
        tmp_path / "root_neg_z.glb",
        source_front="-Z",
        root_translation=pos_can,
        root_rotation=q_can,
    )
    preflight_assembly_glb(glb_neg_z.read_bytes(), spec, source_front="-Z")

    # --- 6. Negative test: Spurious Ry180 applied when source_front == -Z ---
    glb_neg_z_spurious = _generate_valid_assembly_glb(
        tmp_path / "root_neg_z_spurious.glb",
        source_front="-Z",
        root_translation=pos_can,
        root_rotation=raw_rot_pos_z,
    )
    with pytest.raises(ValidationError, match="Root part 'PART_hull' rotation does not match"):
        preflight_assembly_glb(glb_neg_z_spurious.read_bytes(), spec, source_front="-Z")


def test_descendant_pivot_tolerances_from_bound_profile(tmp_path: Path) -> None:
    """Descendant pivot position and basis comparisons use bound profile tolerances in degrees/meters."""
    # Profile A: tight tolerances
    prof_tight = _profile(pivot_tolerance_m=0.01, basis_tolerance_deg=1.0)
    spec_tight = _spec(prof_tight)

    # Profile B: relaxed tolerances
    prof_relaxed = _profile(pivot_tolerance_m=0.05, basis_tolerance_deg=5.0)
    spec_relaxed = _spec(prof_relaxed)

    # Base GLB
    glb_path = tmp_path / "tolerance_test.glb"
    _generate_valid_assembly_glb(glb_path)
    doc, binary = _read_glb(glb_path.read_bytes())

    # 1. Perturb turret position by 0.03m (violates tight 0.01m, satisfies relaxed 0.05m)
    turret_idx = next(i for i, n in enumerate(doc["nodes"]) if n.get("name") == "PART_turret")
    doc["nodes"][turret_idx]["translation"] = [0, 0.03, 0]
    _write_glb(glb_path, doc, binary)

    # Tight fails
    with pytest.raises(
        ValidationError, match="translation .* difference .* exceeds tolerance 0.01m"
    ):
        preflight_assembly_glb(glb_path.read_bytes(), spec_tight)

    # Relaxed passes
    preflight_assembly_glb(glb_path.read_bytes(), spec_relaxed)

    # Restore translation
    doc["nodes"][turret_idx]["translation"] = [0, 0, 0]

    # 2. Perturb turret rotation by ~3 degrees about Y (violates tight 1.0 deg, satisfies relaxed 5.0 deg)
    q_3deg = [0.0, 0.02617695, 0.0, 0.9996573]
    doc["nodes"][turret_idx]["rotation"] = q_3deg
    _write_glb(glb_path, doc, binary)

    # Tight fails
    with pytest.raises(
        ValidationError,
        match="rotation does not match declared pivot basis: difference .* exceeds tolerance 1.0°",
    ):
        preflight_assembly_glb(glb_path.read_bytes(), spec_tight)

    # Relaxed passes
    preflight_assembly_glb(glb_path.read_bytes(), spec_relaxed)


def test_part_scale_orthogonality_reflection_and_shear_rejection(tmp_path: Path) -> None:
    """Scale validation rejects negative scale, reflection (det < 0), and shear via orthogonality/determinant."""
    glb_path = tmp_path / "scale_test.glb"
    _generate_valid_assembly_glb(glb_path)
    doc, binary = _read_glb(glb_path.read_bytes())
    turret_idx = next(i for i, n in enumerate(doc["nodes"]) if n.get("name") == "PART_turret")
    spec = _spec()

    # 1. Positive uniform scale (e.g. 2.0) passes
    doc["nodes"][turret_idx]["scale"] = [2.0, 2.0, 2.0]
    _write_glb(glb_path, doc, binary)
    preflight_assembly_glb(glb_path.read_bytes(), spec)

    # 2. Negative scale TRS (reflection along X)
    doc["nodes"][turret_idx]["scale"] = [-1.0, 1.0, 1.0]
    _write_glb(glb_path, doc, binary)
    with pytest.raises(ValidationError, match="scale components must all be positive"):
        preflight_assembly_glb(glb_path.read_bytes(), spec)

    # 3. Negative uniform scale TRS
    doc["nodes"][turret_idx]["scale"] = [-1.0, -1.0, -1.0]
    _write_glb(glb_path, doc, binary)
    with pytest.raises(ValidationError, match="scale components must all be positive"):
        preflight_assembly_glb(glb_path.read_bytes(), spec)

    # 4. Reflection via explicit 4x4 matrix
    doc["nodes"][turret_idx].pop("scale", None)
    doc["nodes"][turret_idx].pop("rotation", None)
    doc["nodes"][turret_idx].pop("translation", None)
    doc["nodes"][turret_idx]["matrix"] = [
        -1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    ]
    _write_glb(glb_path, doc, binary)
    with pytest.raises(ValidationError, match="non-positive determinant"):
        preflight_assembly_glb(glb_path.read_bytes(), spec)

    # 5. Shear via explicit 4x4 matrix (non-orthogonal column axes)
    doc["nodes"][turret_idx]["matrix"] = [
        1.0,
        0.5,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    ]
    _write_glb(glb_path, doc, binary)
    with pytest.raises(ValidationError, match="(shear detected|uniform|determinant)"):
        preflight_assembly_glb(glb_path.read_bytes(), spec)


def test_socket_structural_contract_and_motion_extras(tmp_path: Path) -> None:
    """Socket local position/basis and PART motion extras must match as structural contract."""
    glb_path = tmp_path / "contract_test.glb"
    _generate_valid_assembly_glb(glb_path)
    doc, binary = _read_glb(glb_path.read_bytes())
    spec = _spec()

    # 1. Socket translation mismatch beyond tolerance
    muzzle_idx = next(i for i, n in enumerate(doc["nodes"]) if n.get("name") == "SOCKET_muzzle")
    doc["nodes"][muzzle_idx]["translation"] = [0, 0, -0.40]  # declared is -0.49
    _write_glb(glb_path, doc, binary)
    with pytest.raises(
        ValidationError,
        match="Socket 'SOCKET_muzzle' translation .* difference .* exceeds tolerance",
    ):
        preflight_assembly_glb(glb_path.read_bytes(), spec)

    # Restore socket translation
    doc["nodes"][muzzle_idx]["translation"] = [0, 0, -0.49]

    # 2. Socket rotation mismatch beyond tolerance (rotated 90 deg around Y)
    doc["nodes"][muzzle_idx]["rotation"] = [0.0, 0.70710678, 0.0, 0.70710678]
    _write_glb(glb_path, doc, binary)
    with pytest.raises(ValidationError, match="Socket 'SOCKET_muzzle' rotation does not match"):
        preflight_assembly_glb(glb_path.read_bytes(), spec)

    # Restore socket rotation
    doc["nodes"][muzzle_idx]["rotation"] = [0, 0, 0, 1]

    # 3. Missing motion extras on PART node
    hull_idx = next(i for i, n in enumerate(doc["nodes"]) if n.get("name") == "PART_hull")
    doc["nodes"][hull_idx].pop("extras", None)
    _write_glb(glb_path, doc, binary)
    with pytest.raises(ValidationError, match="must declare motion extras"):
        preflight_assembly_glb(glb_path.read_bytes(), spec)

    # 4. Motion kind mismatch on PART node
    doc["nodes"][hull_idx]["extras"] = {"gf_motion": "revolute", "gf_axis": [0, 1, 0]}
    _write_glb(glb_path, doc, binary)
    with pytest.raises(ValidationError, match="does not match declared motion kind 'fixed'"):
        preflight_assembly_glb(glb_path.read_bytes(), spec)

    # 5. Revolute motion part with matching and mismatching gf_axis
    revolute_parts = [
        {
            "part_id": "hull",
            "role": "hull",
            "parent": "root",
            "pivot": {"position_m": [0, 0, 0], "basis": "identity", "motion": {"kind": "fixed"}},
        },
        {
            "part_id": "turret",
            "role": "turret",
            "parent": "hull",
            "pivot": {
                "position_m": [0, 0, 0],
                "basis": "identity",
                "motion": {"kind": "revolute", "axis": [0, 1, 0]},
            },
        },
        {
            "part_id": "barrel",
            "role": "barrel",
            "parent": "turret",
            "pivot": {"position_m": [0, 0, 0], "basis": "identity", "motion": {"kind": "fixed"}},
        },
    ]
    revolute_spec = _spec(parts=revolute_parts)

    turret_idx = next(i for i, n in enumerate(doc["nodes"]) if n.get("name") == "PART_turret")
    doc["nodes"][hull_idx]["extras"] = {"gf_motion": "fixed"}
    doc["nodes"][turret_idx]["extras"] = {"gf_motion": "revolute", "gf_axis": [0, 1, 0]}
    _write_glb(glb_path, doc, binary)
    # Passes when matching
    preflight_assembly_glb(glb_path.read_bytes(), revolute_spec)

    # Fails when axis does not match declared axis
    doc["nodes"][turret_idx]["extras"] = {"gf_motion": "revolute", "gf_axis": [1, 0, 0]}
    _write_glb(glb_path, doc, binary)
    with pytest.raises(
        ValidationError, match="motion extras 'gf_axis' .* does not match declared axis"
    ):
        preflight_assembly_glb(glb_path.read_bytes(), revolute_spec)


def test_publication_completion_marker_reader_gates(tmp_path: Path) -> None:
    """Reader gates reject packages with missing, malformed, or tampered completion markers."""
    managed_root = tmp_path / "managed_root"
    managed_root.mkdir()
    source_glb = _generate_valid_assembly_glb(tmp_path / "marker_tank.glb")
    spec = _spec()

    result = ingest_assembly_source(
        source_glb_path=source_glb,
        managed_root=managed_root,
        relative_package_dir="sources/assemblies/marker_test_pkg",
        spec=spec,
        authoring_tool_name="Blender",
        authoring_tool_version="4.2.1",
        source_front="-Z",
        actor="operator_dave",
        reason="baseline",
    )

    marker_file = result.package_dir / "publication_marker.json"
    assert marker_file.is_file()
    marker_content = json.loads(marker_file.read_text("utf-8"))
    assert marker_content["source_artifact_sha256"] == result.retained_glb_sha256
    assert marker_content["provenance_sha256"] == result.retained_provenance_sha256
    assert marker_content["spec_fingerprint"] == result.spec_fingerprint

    # 1. Missing marker: delete marker
    backup_marker = marker_file.read_bytes()
    marker_file.unlink()

    with pytest.raises(ValidationError, match="publication-completion marker .* is missing"):
        verify_retained_assembly(
            package_dir="sources/assemblies/marker_test_pkg",
            spec=spec,
            managed_root=managed_root,
            expected_provenance_sha256=result.retained_provenance_sha256,
        )

    with pytest.raises(ValidationError, match="publication-completion marker .* is missing"):
        inspect_untrusted_assembly_package(
            package_dir="sources/assemblies/marker_test_pkg",
            spec=spec,
            managed_root=managed_root,
        )

    # 2. Malformed marker
    marker_file.write_text("NOT_VALID_JSON{", encoding="utf-8")
    with pytest.raises(ValidationError, match="malformed or invalid"):
        verify_retained_assembly(
            package_dir="sources/assemblies/marker_test_pkg",
            spec=spec,
            managed_root=managed_root,
            expected_provenance_sha256=result.retained_provenance_sha256,
        )

    # 3. Tampered spec fingerprint in marker
    tampered_fp_marker = dict(marker_content, spec_fingerprint="0" * 64)
    marker_file.write_text(json.dumps(tampered_fp_marker), encoding="utf-8")
    with pytest.raises(ValidationError, match="spec_fingerprint .* does not match expected"):
        verify_retained_assembly(
            package_dir="sources/assemblies/marker_test_pkg",
            spec=spec,
            managed_root=managed_root,
            expected_provenance_sha256=result.retained_provenance_sha256,
        )

    # 4. Tampered GLB digest in marker
    tampered_glb_marker = dict(marker_content, source_artifact_sha256="0" * 64)
    marker_file.write_text(json.dumps(tampered_glb_marker), encoding="utf-8")
    with pytest.raises(
        ValidationError, match="source_artifact_sha256 .* does not match GLB digest"
    ):
        verify_retained_assembly(
            package_dir="sources/assemblies/marker_test_pkg",
            spec=spec,
            managed_root=managed_root,
            expected_provenance_sha256=result.retained_provenance_sha256,
        )

    # Restore valid marker
    marker_file.write_bytes(backup_marker)
    verified = verify_retained_assembly(
        package_dir="sources/assemblies/marker_test_pkg",
        spec=spec,
        managed_root=managed_root,
        expected_provenance_sha256=result.retained_provenance_sha256,
    )
    assert verified.retained_glb_sha256 == result.retained_glb_sha256


def test_concurrent_reader_or_crash_before_marker_rejected(tmp_path: Path) -> None:
    """A package with moved source and provenance but crashed before marker is rejected as incomplete."""
    managed_root = tmp_path / "managed_root"
    pkg_dir = managed_root / "sources" / "assemblies" / "incomplete_pkg"
    pkg_dir.mkdir(parents=True)

    source_glb = _generate_valid_assembly_glb(tmp_path / "tmp.glb")
    spec = _spec()

    # Put GLB and provenance in pkg_dir, but NO publication_marker.json
    (pkg_dir / "source.glb").write_bytes(source_glb.read_bytes())
    from gamefactory.core.domain.assembly_source import create_assembly_provenance

    glb_bytes = (pkg_dir / "source.glb").read_bytes()
    prov = create_assembly_provenance(
        source_artifact_sha256=hashlib.sha256(glb_bytes).hexdigest(),
        source_artifact_byte_size=len(glb_bytes),
        authoring_tool_name="Blender",
        authoring_tool_version="4.2.1",
        source_front="-Z",
        spec=spec,
        actor="operator_dave",
        reason="test",
    )
    (pkg_dir / "source_provenance.json").write_bytes(prov.to_canonical_bytes())

    # Concurrent reader or verifier running before marker exists MUST reject
    with pytest.raises(ValidationError, match="publication-completion marker .* is missing"):
        verify_retained_assembly(
            package_dir="sources/assemblies/incomplete_pkg",
            spec=spec,
            managed_root=managed_root,
            expected_provenance_sha256=prov.provenance_hash(),
        )

    with pytest.raises(ValidationError, match="publication-completion marker .* is missing"):
        inspect_untrusted_assembly_package(
            package_dir="sources/assemblies/incomplete_pkg",
            spec=spec,
            managed_root=managed_root,
        )


def test_foreign_file_preserved_and_exact_owned_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a failure occurs, only exact owned files are removed; foreign files and finaldir are preserved."""
    managed_root = tmp_path / "managed_root"
    managed_root.mkdir()
    source_glb = _generate_valid_assembly_glb(tmp_path / "tank.glb")
    spec = _spec()

    target_dir = managed_root / "sources" / "assemblies" / "injected_failure_pkg"
    foreign_file_name = "user_important_notes.txt"

    # Hook the atomic no-replace link to inject foreign file and crash at marker publication.
    real_link = os.link

    def crashing_link(src: Any, dst: Any) -> None:
        src_path = Path(src)
        dst_path = Path(dst)
        # At marker publication, simulate a competing writer and a failed link.
        if "publication_marker" in dst_path.name or "publication_marker" in src_path.name:
            (dst_path.parent / foreign_file_name).write_text("do not delete me!", encoding="utf-8")
            raise OSError("Simulated disk error or crash during marker publication")
        real_link(src, dst)

    monkeypatch.setattr(os, "link", crashing_link)

    with pytest.raises(ValidationError, match="no-replace publication.*failed"):
        ingest_assembly_source(
            source_glb_path=source_glb,
            managed_root=managed_root,
            relative_package_dir="sources/assemblies/injected_failure_pkg",
            spec=spec,
            authoring_tool_name="Blender",
            authoring_tool_version="4.2.1",
            source_front="-Z",
            actor="operator_dave",
            reason="test",
        )

    # Verify:
    # 1. Foreign file is preserved!
    foreign_file_path = target_dir / foreign_file_name
    assert foreign_file_path.is_file(), "Foreign file must NOT be deleted by cleanup!"
    assert foreign_file_path.read_text(encoding="utf-8") == "do not delete me!"

    # 2. Target directory is preserved (rmdir failed because foreign file is inside; NOT rmtree'd)
    assert target_dir.is_dir(), "Directory containing foreign file must NOT be deleted!"

    # 3. Owned files were cleaned up
    assert not (target_dir / "source.glb").exists(), "Owned GLB must be cleaned up on failure"
    assert not (target_dir / "source_provenance.json").exists(), (
        "Owned provenance must be cleaned up on failure"
    )


def test_cleanup_preserves_foreign_replacement_at_owned_final_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root = tmp_path / "managed_root"
    managed_root.mkdir()
    source_glb = _generate_valid_assembly_glb(tmp_path / "tank.glb")
    target_dir = managed_root / "sources" / "assemblies" / "replaced_final"
    foreign_bytes = b"foreign replacement at formerly-owned path"
    real_link = os.link

    def link_then_fail(src: Any, dst: Any) -> None:
        dst_path = Path(dst)
        if dst_path == target_dir / "source.glb":
            real_link(src, dst)
            dst_path.unlink()
            dst_path.write_bytes(foreign_bytes)
            return
        if dst_path == target_dir / "source_provenance.json":
            raise OSError("injected failure after foreign replacement")
        real_link(src, dst)

    monkeypatch.setattr(os, "link", link_then_fail)
    with pytest.raises(ValidationError, match="no-replace publication.*failed"):
        ingest_assembly_source(
            source_glb_path=source_glb,
            managed_root=managed_root,
            relative_package_dir="sources/assemblies/replaced_final",
            spec=_spec(),
            authoring_tool_name="Blender",
            authoring_tool_version="4.2.1",
            source_front="-Z",
            actor="operator_dave",
            reason="cleanup ownership regression",
        )

    assert (target_dir / "source.glb").read_bytes() == foreign_bytes
    assert not (target_dir / "source_provenance.json").exists()


def test_staging_cleanup_preserves_modified_foreign_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root = tmp_path / "managed_root"
    managed_root.mkdir()
    source_glb = _generate_valid_assembly_glb(tmp_path / "tank.glb")
    target_dir = managed_root / "sources" / "assemblies" / "replaced_staging"
    foreign_bytes = b"foreign staging content"
    real_link = os.link
    stage_was_modified = False

    def link_then_modify_stage(src: Any, dst: Any) -> None:
        nonlocal stage_was_modified
        src_path, dst_path = Path(src), Path(dst)
        if dst_path == target_dir / "source.glb":
            real_link(src, dst)
            staged_provenance = src_path.with_name("source_provenance.json")
            staged_provenance.write_bytes(foreign_bytes)
            stage_was_modified = True
            return
        real_link(src, dst)

    monkeypatch.setattr(os, "link", link_then_modify_stage)
    with pytest.raises(ValidationError, match="Staged provenance sidecar changed"):
        ingest_assembly_source(
            source_glb_path=source_glb,
            managed_root=managed_root,
            relative_package_dir="sources/assemblies/replaced_staging",
            spec=_spec(),
            authoring_tool_name="Blender",
            authoring_tool_version="4.2.1",
            source_front="-Z",
            actor="operator_dave",
            reason="staging ownership regression",
        )

    assert stage_was_modified
    staging_dirs = list(target_dir.parent.glob(".tmp_assembly_replaced_staging_*"))
    assert len(staging_dirs) == 1
    assert (staging_dirs[0] / "source_provenance.json").read_bytes() == foreign_bytes
    assert not target_dir.exists()


@pytest.mark.parametrize(
    "destination_name",
    ("source.glb", "source_provenance.json", "publication_marker.json"),
)
def test_no_replace_publication_race_preserves_foreign_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, destination_name: str
) -> None:
    managed_root = tmp_path / "managed_root"
    managed_root.mkdir()
    source_glb = _generate_valid_assembly_glb(tmp_path / "tank.glb")
    target_dir = managed_root / "sources" / "assemblies" / "race_destination"
    foreign_bytes = f"foreign bytes at {destination_name}".encode()
    real_link = os.link
    injected = False

    def create_foreign_then_link(src: Any, dst: Any) -> None:
        nonlocal injected
        dst_path = Path(dst)
        if dst_path == target_dir / destination_name and not injected:
            dst_path.write_bytes(foreign_bytes)
            injected = True
        real_link(src, dst)

    monkeypatch.setattr(os, "link", create_foreign_then_link)
    with pytest.raises(ValidationError, match="Publication destination already exists"):
        ingest_assembly_source(
            source_glb_path=source_glb,
            managed_root=managed_root,
            relative_package_dir="sources/assemblies/race_destination",
            spec=_spec(),
            authoring_tool_name="Blender",
            authoring_tool_version="4.2.1",
            source_front="-Z",
            actor="operator_dave",
            reason="atomic no-replace race regression",
        )

    assert injected
    assert (target_dir / destination_name).read_bytes() == foreign_bytes
    with pytest.raises(ValidationError):
        verify_retained_assembly(
            target_dir,
            _spec(),
            expected_provenance_sha256="0" * 64,
        )


def test_post_marker_staging_cleanup_failure_keeps_successful_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root = tmp_path / "managed_root"
    managed_root.mkdir()
    source_glb = _generate_valid_assembly_glb(tmp_path / "tank.glb")
    real_unlink = Path.unlink

    def fail_marker_stage_cleanup(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.name == "publication_marker.json" and path.parent.name.startswith(
            ".tmp_assembly_post_marker_cleanup_"
        ):
            raise OSError("injected post-publication staging cleanup failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_marker_stage_cleanup)
    result = ingest_assembly_source(
        source_glb_path=source_glb,
        managed_root=managed_root,
        relative_package_dir="sources/assemblies/post_marker_cleanup",
        spec=_spec(),
        authoring_tool_name="Blender",
        authoring_tool_version="4.2.1",
        source_front="-Z",
        actor="operator_dave",
        reason="post marker cleanup failure regression",
    )

    verified = verify_retained_assembly(
        result.package_dir,
        _spec(),
        expected_provenance_sha256=result.retained_provenance_sha256,
    )
    assert verified.retained_glb_sha256 == result.retained_glb_sha256
    assert (result.package_dir / "publication_marker.json").is_file()
    staging_dirs = list(result.package_dir.parent.glob(".tmp_assembly_post_marker_cleanup_*"))
    assert len(staging_dirs) == 1
    assert (staging_dirs[0] / "publication_marker.json").is_file()


def test_existing_empty_target_untouched_on_failure(tmp_path: Path) -> None:
    """Pre-existing empty directory is untouched and not deleted on ingest failure."""
    managed_root = tmp_path / "managed_root"
    managed_root.mkdir()
    target_dir = managed_root / "sources" / "assemblies" / "pre_existing_empty_pkg"
    target_dir.mkdir(parents=True)
    assert target_dir.is_dir()
    assert list(target_dir.iterdir()) == []

    source_glb = _generate_valid_assembly_glb(tmp_path / "tank.glb")
    spec = _spec()

    with pytest.raises(ValidationError, match="already exists"):
        ingest_assembly_source(
            source_glb_path=source_glb,
            managed_root=managed_root,
            relative_package_dir="sources/assemblies/pre_existing_empty_pkg",
            spec=spec,
            authoring_tool_name="Blender",
            authoring_tool_version="4.2.1",
            source_front="-Z",
            actor="operator_dave",
            reason="test",
        )

    # Empty target directory remains untouched and existing
    assert target_dir.is_dir()
    assert list(target_dir.iterdir()) == []


def test_inspect_untrusted_assembly_package_pre_resolution_symlink_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """inspect_untrusted_assembly_package performs pre-resolution lexical checks without managed_root."""
    _generate_valid_assembly_glb(tmp_path / "tank.glb")
    spec = _spec()

    # Simulate symlink on lexical component
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda self: self.name == "symlink_dir" or real_is_symlink(self),
    )

    with pytest.raises(ValidationError, match="contains a symlink"):
        inspect_untrusted_assembly_package(
            package_dir=tmp_path / "symlink_dir" / "pkg",
            spec=spec,
            managed_root=None,
        )
