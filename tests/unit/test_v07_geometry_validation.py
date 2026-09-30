from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.assets.glb_validator import validate_glb
from gamefactory.adapters.assets.v07_geometry_validation import (
    VerifiedSourceNormalization,
    selected_v07_groups,
)
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.core.domain.asset_contracts import parse_asset_specification_v07
from gamefactory.core.domain.asset_profiles import (
    AssetProfileV07,
    ProfileRegistry,
    parse_profile_document_v07,
)


def _profile(
    *, geometry_mode: str, collider: str, required_sockets: bool = False
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
        "pivot_tolerance_m": 0.01,
        "basis_tolerance_deg": 1.0,
        "socket_position_tolerance_m": 0.01,
        "socket_angle_tolerance_deg": 1.0,
    }
    data = {
        "schema_version": "asset-profile-0.7.0",
        "profile_id": "test_geometry",
        "version": 1,
        "categories": ["vehicle" if geometry_mode == "assembly" else "character"],
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


def _spec(profile: AssetProfileV07, *, sockets: bool = True, capsule: bool = False):
    assembly = profile.geometry_mode == "assembly"
    parts = []
    if assembly:
        parts = [
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
    data: dict[str, Any] = {
        "schema_version": "0.7.0",
        "asset_id": "test_vehicle" if assembly else "test_character",
        "category": "vehicle" if assembly else "character",
        "profile": profile.profile_id,
        "profile_version": profile.version,
        "intent": "geometry test",
        "source_kind": "local_operator_assembly" if assembly else "provider_generated",
        "dimensions": {"width_m": 1.0, "height_m": 1.0, "depth_m": 1.0},
        "origin_policy": "center",
        "lod_policy": "lod0_only",
        "geometry_budget": {
            "max_triangles_lod0": 10000,
            "max_triangles_lod1": 5000,
            "lod_ratio": 0.5,
        },
        "material_budget": {"max_materials": 4},
        "texture_budget": {"max_dimension": 2048},
        "collider": {
            "policy": "capsule" if capsule else "box",
            "capsule": {"radius_m": 0.25, "height_m": 0.9} if capsule else None,
        },
    }
    if assembly:
        data["parts"] = parts
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
    registry = ProfileRegistry(available=(), unsupported=(), available_v07=(profile,))
    return parse_asset_specification_v07(data, registry=registry)


def _articulate_spec(spec) -> None:
    pivots = {part.part_id: part.pivot for part in spec.parts}
    object.__setattr__(pivots["hull"], "position_m", [0, -0.2, 0])
    object.__setattr__(pivots["turret"], "position_m", [0, 0.2, 0.1])
    object.__setattr__(pivots["turret"].motion, "kind", "revolute")
    object.__setattr__(pivots["turret"].motion, "axis", [0, 1, 0])
    object.__setattr__(pivots["barrel"], "position_m", [0, 0.2, -0.2])
    object.__setattr__(pivots["barrel"].motion, "kind", "revolute")
    object.__setattr__(pivots["barrel"].motion, "axis", [1, 0, 0])
    object.__setattr__(spec.sockets[0], "translation_m", [0, 0, -0.49])
    object.__setattr__(spec.dimensions, "height_m", 1.4)
    object.__setattr__(spec.dimensions, "depth_m", 1.2)


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


def _assembly_glb(path: Path, *, articulated: bool = False) -> None:
    create_box_glb(
        width_m=1,
        depth_m=1,
        height_m=1,
        mesh_name="base",
        origin="center",
        include_collider=True,
        collider_name="COL_test_vehicle",
        output_path=path,
    )
    doc, binary = _read_glb(path.read_bytes())
    doc["meshes"][0]["name"] = "shared_geometry"
    if articulated:
        doc["meshes"][1]["primitives"][0]["attributes"]["POSITION"] = len(doc["accessors"])
        offset = len(binary)
        points = [
            (-0.5, -0.7, -0.6),
            (0.5, -0.7, -0.6),
            (0.5, -0.7, 0.6),
            (-0.5, -0.7, 0.6),
            (-0.5, 0.7, -0.6),
            (0.5, 0.7, -0.6),
            (0.5, 0.7, 0.6),
            (-0.5, 0.7, 0.6),
        ]
        position_data = b"".join(struct.pack("<fff", *point) for point in points)
        new_view = len(doc["bufferViews"])
        doc["bufferViews"].append(
            {"buffer": 0, "byteOffset": offset, "byteLength": len(position_data), "target": 34962}
        )
        new_accessor = dict(doc["accessors"][0])
        new_accessor.update(
            {
                "bufferView": new_view,
                "byteOffset": 0,
                "min": [-0.5, -0.7, -0.6],
                "max": [0.5, 0.7, 0.6],
            }
        )
        doc["accessors"].append(new_accessor)
        binary += position_data
        binary += b"\0" * ((-len(binary)) % 4)
        doc["buffers"][0]["byteLength"] = len(binary)
    doc["nodes"] = [
        {"name": "ROOT", "children": [1, 8]},
        {
            "name": "PART_hull",
            "translation": [0, -0.2 if articulated else 0, 0],
            "extras": {"gf_motion": "fixed"},
            "children": [2, 3],
        },
        {"name": "SM_test_vehicle_hull_LOD0", "mesh": 0},
        {
            "name": "PART_turret",
            "translation": [0, 0.2, 0.1] if articulated else [0, 0, 0],
            "extras": {
                "gf_motion": "revolute" if articulated else "fixed",
                **({"gf_axis": [0, 1, 0]} if articulated else {}),
            },
            "children": [4, 5],
        },
        {"name": "SM_test_vehicle_turret_LOD0", "mesh": 0},
        {
            "name": "PART_barrel",
            "translation": [0, 0.2, -0.2] if articulated else [0, 0, 0],
            "extras": {
                "gf_motion": "revolute" if articulated else "fixed",
                **({"gf_axis": [1, 0, 0]} if articulated else {}),
            },
            "children": [6, 7],
        },
        {"name": "SM_test_vehicle_barrel_LOD0", "mesh": 0},
        {"name": "SOCKET_muzzle", "translation": [0, 0, -0.49]},
        {"name": "COL_test_vehicle", "mesh": 1},
    ]
    doc["scenes"] = [{"nodes": [0]}]
    _write_glb(path, doc, binary)


def _raw_assembly_source(path: Path) -> None:
    """Write source-stage ROOT + PART + LOD0 nodes without the processed collider."""
    _assembly_glb(path, articulated=True)
    document, binary = _read_glb(path.read_bytes())
    document["nodes"][0]["children"] = [1]
    document["nodes"].pop()
    document["meshes"].pop()
    _write_glb(path, document, binary)


def _split_processed_vertices(
    path: Path, *, mesh_index: int = 0, change_topology: bool = False
) -> None:
    from gamefactory.adapters.assets.glb_validator import _accessor

    document, binary = _read_glb(path.read_bytes())
    primitive = document["meshes"][mesh_index]["primitives"][0]
    positions = _accessor(document, binary, primitive["attributes"]["POSITION"], "VEC3")
    indices = [int(row[0]) for row in _accessor(document, binary, primitive["indices"], "SCALAR")]
    split_positions = [tuple(float(value) for value in positions[index]) for index in indices]
    if change_topology:
        # Change one triangle while retaining all original extrema and triangle count.
        split_positions[0] = split_positions[1]
    for _ in range((-len(binary)) % 4):
        binary += b"\0"
    position_offset = len(binary)
    position_bytes = b"".join(struct.pack("<fff", *point) for point in split_positions)
    binary += position_bytes
    position_view = len(document["bufferViews"])
    document["bufferViews"].append(
        {
            "buffer": 0,
            "byteOffset": position_offset,
            "byteLength": len(position_bytes),
            "target": 34962,
        }
    )
    position_accessor = len(document["accessors"])
    mins = [min(point[axis] for point in split_positions) for axis in range(3)]
    maxs = [max(point[axis] for point in split_positions) for axis in range(3)]
    position_definition = dict(document["accessors"][primitive["attributes"]["POSITION"]])
    position_definition.update(
        {
            "bufferView": position_view,
            "byteOffset": 0,
            "count": len(split_positions),
            "min": mins,
            "max": maxs,
        }
    )
    document["accessors"].append(position_definition)
    for _ in range((-len(binary)) % 4):
        binary += b"\0"
    index_offset = len(binary)
    new_indices = list(range(len(split_positions)))
    index_bytes = struct.pack(f"<{len(new_indices)}H", *new_indices)
    binary += index_bytes
    index_view = len(document["bufferViews"])
    document["bufferViews"].append(
        {"buffer": 0, "byteOffset": index_offset, "byteLength": len(index_bytes), "target": 34963}
    )
    index_accessor = len(document["accessors"])
    index_definition = dict(document["accessors"][primitive["indices"]])
    index_definition.update(
        {
            "bufferView": index_view,
            "byteOffset": 0,
            "count": len(new_indices),
            "min": [0],
            "max": [len(new_indices) - 1],
        }
    )
    document["accessors"].append(index_definition)
    primitive["attributes"] = {"POSITION": position_accessor}
    primitive["indices"] = index_accessor
    document["buffers"][0]["byteLength"] = len(binary)
    _write_glb(path, document, binary)


def _replace_collider_triangles(path: Path, mode: str) -> None:
    from gamefactory.adapters.assets.glb_validator import _accessor

    document, binary = _read_glb(path.read_bytes())
    primitive = document["meshes"][1]["primitives"][0]
    old_accessor = primitive["indices"]
    indices = [int(row[0]) for row in _accessor(document, binary, old_accessor, "SCALAR")]
    if mode == "duplicate":
        indices[3:6] = indices[0:3]
    elif mode == "overlap":
        indices[3:6] = [0, 1, 3]
    else:
        raise ValueError(mode)
    binary += b"\0" * ((-len(binary)) % 4)
    offset = len(binary)
    index_data = struct.pack(f"<{len(indices)}H", *indices)
    binary += index_data
    view_index = len(document["bufferViews"])
    document["bufferViews"].append(
        {"buffer": 0, "byteOffset": offset, "byteLength": len(index_data), "target": 34963}
    )
    accessor_index = len(document["accessors"])
    index_definition = dict(document["accessors"][old_accessor])
    index_definition.update({"bufferView": view_index, "byteOffset": 0, "count": len(indices)})
    document["accessors"].append(index_definition)
    primitive["indices"] = accessor_index
    document["buffers"][0]["byteLength"] = len(binary)
    _write_glb(path, document, binary)


def _rules(result) -> dict[str, str]:
    return {finding.rule_id: finding.severity.value for finding in result.findings}


def _observation(path: Path) -> VerifiedSourceNormalization:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    return VerifiedSourceNormalization(
        digest,
        digest,
        "-Z",
        False,
        (0, 0, 0, 1),
        raw,
    )


def test_assembly_composition_comes_from_typed_capabilities_and_positive_fixture(
    tmp_path: Path,
) -> None:
    profile = _profile(geometry_mode="assembly", collider="box", required_sockets=True)
    spec = _spec(profile)
    _articulate_spec(spec)
    path = tmp_path / "assembly.glb"
    _assembly_glb(path, articulated=True)
    assert selected_v07_groups(spec, profile) == (
        "core",
        "parts",
        "orientation_source_front",
        "pivot",
        "sockets",
        "collider_box",
    )
    result = validate_glb(path, spec)
    rules = _rules(result)
    assert rules["orientation.source_front"] == "FAIL"
    assert all(
        value == "PASS" for key, value in rules.items() if key != "orientation.source_front"
    ), rules

    # A hash-bound immutable observation is required; source_front is never guessed.
    from gamefactory.adapters.assets.v07_geometry_validation import validate_glb_v07

    observed = validate_glb_v07(path, spec, source_observation=_observation(path))
    assert observed.status.value == "FAIL", observed.to_dict()


def test_v07_groups_execute_from_one_immutable_context_in_canonical_order(
    tmp_path: Path,
) -> None:
    from gamefactory.adapters.assets.v07_geometry_validation import V07RuleGroup
    from gamefactory.adapters.assets.v07_validation_context import build_v07_validation_context
    from gamefactory.adapters.assets.v07_validation_rules import (
        V07RuleExecutionContext,
        evaluate_ordered_v07_rules,
        require_implemented_v07_groups,
    )

    profile = _profile(geometry_mode="assembly", collider="box", required_sockets=False)
    spec = _spec(profile, sockets=False)
    path = tmp_path / "groups.glb"
    _assembly_glb(path)
    decoded = build_v07_validation_context(path, spec, profile, None, 50 * 1024 * 1024)
    assert decoded.parse_error is None
    with pytest.raises(TypeError):
        decoded.document["asset"] = {}  # type: ignore[index]
    original_intent = decoded.specification.intent
    object.__setattr__(spec, "intent", "mutated after snapshot")
    assert decoded.specification.intent == original_intent

    execution = V07RuleExecutionContext(decoded, 50 * 1024 * 1024)
    core = evaluate_ordered_v07_rules(execution, (V07RuleGroup.CORE,))
    assembly = evaluate_ordered_v07_rules(execution, (V07RuleGroup.ASSEMBLY,))
    assert {item.rule_id for item in core} == {
        "glb.parse",
        "geometry.nonempty",
        "nodes.unique",
        "materials.budget",
        "textures.dimension",
        "geometry.lod0_budget",
        "geometry.lod1_budget",
    }
    assert "root.structure" in {item.rule_id for item in assembly}
    assert "materials.budget" not in {item.rule_id for item in assembly}
    with pytest.raises(ValueError, match="non-empty"):
        require_implemented_v07_groups(())
    with pytest.raises(ValueError, match="duplicate"):
        require_implemented_v07_groups((V07RuleGroup.CORE, V07RuleGroup.CORE))
    with pytest.raises(ValueError, match="closed V07RuleGroup"):
        require_implemented_v07_groups(("core",))
    with pytest.raises(ValueError, match="canonical order"):
        require_implemented_v07_groups((V07RuleGroup.ASSEMBLY, V07RuleGroup.CORE))


def test_v07_missing_or_oversized_artifact_fails_every_selected_group(tmp_path: Path) -> None:
    profile = _profile(geometry_mode="assembly", collider="box", required_sockets=False)
    spec = _spec(profile, sockets=False)
    missing = validate_glb(tmp_path / "missing.glb", spec)
    missing_rules = _rules(missing)
    assert missing_rules["glb.parse"] == "FAIL"
    for group in selected_v07_groups(spec, profile):
        assert missing_rules[f"input.{group}"] == "FAIL", missing_rules

    path = tmp_path / "large.glb"
    _assembly_glb(path)
    from gamefactory.adapters.assets.v07_geometry_validation import validate_glb_v07

    oversized = validate_glb_v07(path, spec, max_file_size_bytes=64)
    oversized_rules = _rules(oversized)
    assert oversized_rules["glb.parse"] == "FAIL"
    assert oversized_rules["input.core"] == "FAIL"


@pytest.mark.parametrize(
    ("case", "expected_rule"),
    [
        ("duplicate-name", "nodes.unique"),
        ("cycle", "glb.parse"),
        ("depth", "glb.parse"),
        ("external-image", "glb.parse"),
        ("skin", "glb.parse"),
        ("animation", "glb.parse"),
        ("extension", "glb.parse"),
    ],
)
def test_v07_keeps_bounded_parser_rejections_and_duplicate_name_rule(
    tmp_path: Path, case: str, expected_rule: str
) -> None:
    profile = _profile(geometry_mode="assembly", collider="box", required_sockets=False)
    spec = _spec(profile, sockets=False)
    path = tmp_path / f"{case}.glb"
    _assembly_glb(path)
    document, binary = _read_glb(path.read_bytes())
    if case == "duplicate-name":
        document["nodes"][3]["name"] = document["nodes"][1]["name"]
    elif case == "cycle":
        document["nodes"][5]["children"].append(1)
    elif case == "depth":
        parent = 7
        for index in range(130):
            child = len(document["nodes"])
            document["nodes"].append({"name": f"deep_{index}", "children": []})
            document["nodes"][parent].setdefault("children", []).append(child)
            parent = child
    elif case == "external-image":
        document["images"] = [{"uri": "https://example.invalid/texture.png"}]
    elif case == "skin":
        document["skins"] = [{}]
    elif case == "animation":
        document["animations"] = [{}]
    elif case == "extension":
        document["extensionsUsed"] = ["KHR_draco_mesh_compression"]
    _write_glb(path, document, binary)
    rules = _rules(validate_glb(path, spec))
    assert rules[expected_rule] == "FAIL", rules


@pytest.mark.parametrize(
    ("mutate", "expected_rule"),
    [
        (lambda doc: doc["nodes"][1].update(children=[2]), "part.parent.turret"),
        (lambda doc: doc["nodes"][1].update(name="PART_other"), "part.missing"),
        (lambda doc: doc["nodes"][3].update(scale=[2, 1, 1]), "part.scale.turret"),
        (lambda doc: doc["nodes"][7].update(translation=[0, 0, 0]), "socket.position.muzzle"),
        (lambda doc: doc["nodes"][7].update(rotation=[0, 1, 0, 0]), "socket.orientation.muzzle"),
        (lambda doc: doc["nodes"][4].update(name="NOT_A_PART_MESH"), "part.mesh_set"),
    ],
)
def test_assembly_rejects_isolated_node_mutations(
    tmp_path: Path, mutate, expected_rule: str
) -> None:
    profile = _profile(geometry_mode="assembly", collider="box", required_sockets=True)
    spec = _spec(profile)
    path = tmp_path / "mutated.glb"
    _assembly_glb(path)
    doc, binary = _read_glb(path.read_bytes())
    mutate(doc)
    _write_glb(path, doc, binary)
    rules = _rules(validate_glb(path, spec))
    assert rules[expected_rule] == "FAIL", rules


def test_capsule_is_spec_data_and_glb_contains_no_capsule_mesh(tmp_path: Path) -> None:
    profile = _profile(geometry_mode="single_mesh", collider="capsule")
    spec = _spec(profile, capsule=True)
    path = tmp_path / "character.glb"
    create_box_glb(
        width_m=1,
        depth_m=1,
        height_m=1,
        mesh_name="SM_test_character",
        origin="center",
        output_path=path,
    )
    result = validate_glb(path, spec)
    assert result.status.value == "PASS", result.to_dict()

    doc, binary = _read_glb(path.read_bytes())
    doc["nodes"].append({"name": "COL_test_character", "mesh": 0})
    doc["scenes"][0]["nodes"].append(1)
    _write_glb(path, doc, binary)
    rules = _rules(validate_glb(path, spec))
    assert rules["collider.capsule.shape"] == "FAIL"


def test_declared_geometry_dimension_mismatch_fails_without_resizing(tmp_path: Path) -> None:
    profile = _profile(geometry_mode="assembly", collider="box", required_sockets=False)
    spec = _spec(profile)
    object.__setattr__(spec.dimensions, "width_m", 2.0)
    path = tmp_path / "assembly.glb"
    _assembly_glb(path)
    rules = _rules(validate_glb(path, spec))
    assert rules["dimensions.bounds"] == "FAIL"
    assert rules["geometry.lod0_budget"] == "PASS"


@pytest.mark.parametrize(
    ("mutate", "expected_rule"),
    [
        (
            lambda document: document["nodes"][3].update(translation=[0, 0.2, 0]),
            "pivot.position.turret",
        ),
        (
            lambda document: document["nodes"][3].update(
                rotation=[0, 0.7071067811865476, 0, 0.7071067811865476]
            ),
            "pivot.orientation.turret",
        ),
        (lambda document: document["nodes"][3].update(scale=[-1, -1, -1]), "part.scale.turret"),
        (lambda document: document["nodes"][3].update(scale=[-1, -1, 1]), "part.scale.turret"),
        (
            lambda document: document["nodes"][3]["extras"].update(gf_axis=[1, 0, 0]),
            "pivot.axis.turret",
        ),
        (
            lambda document: (
                document["nodes"][0].update(children=[1]),
                document["nodes"][1].update(children=[2, 3, 8]),
            ),
            "collider.parent",
        ),
        (
            lambda document: document["nodes"][8].update(rotation=[0, 0.3826834, 0, 0.9238795]),
            "collider.parent",
        ),
        (
            lambda document: document["nodes"][7].update(name="SOCKET_other"),
            "socket.missing.muzzle",
        ),
        (lambda document: document["nodes"][7].update(mesh=0), "socket.structure.muzzle"),
    ],
)
def test_articulation_and_collider_findings_are_independent(
    tmp_path: Path, mutate, expected_rule: str
) -> None:
    profile = _profile(geometry_mode="assembly", collider="box", required_sockets=True)
    spec = _spec(profile)
    _articulate_spec(spec)
    path = tmp_path / "asset.glb"
    _assembly_glb(path, articulated=True)
    document, binary = _read_glb(path.read_bytes())
    mutate(document)
    _write_glb(path, document, binary)
    rules = _rules(validate_glb(path, spec))
    assert rules[expected_rule] == "FAIL", rules


def test_assembly_requires_the_active_scene_to_start_at_its_only_root(tmp_path: Path) -> None:
    from dataclasses import replace

    from gamefactory.adapters.assets.v07_geometry_validation import V07RuleGroup
    from gamefactory.adapters.assets.v07_validation_context import build_v07_validation_context
    from gamefactory.adapters.assets.v07_validation_rules import (
        V07RuleExecutionContext,
        evaluate_ordered_v07_rules,
    )

    profile = _profile(geometry_mode="assembly", collider="box", required_sockets=False)
    spec = _spec(profile, sockets=False)
    path = tmp_path / "inactive-root.glb"
    _assembly_glb(path)
    decoded = build_v07_validation_context(path, spec, profile, None, 50 * 1024 * 1024)
    invalid_active_root = replace(decoded, active_scene_roots=(1,))
    findings = evaluate_ordered_v07_rules(
        V07RuleExecutionContext(invalid_active_root, 50 * 1024 * 1024),
        (V07RuleGroup.ASSEMBLY,),
    )
    assert next(item for item in findings if item.rule_id == "scene.assembly_root").passed is False


@pytest.mark.parametrize("mode", ["duplicate", "overlap"])
def test_box_collider_rejects_duplicate_or_overlapping_face_triangles(
    tmp_path: Path, mode: str
) -> None:
    profile = _profile(geometry_mode="assembly", collider="box", required_sockets=False)
    spec = _spec(profile, sockets=False)
    path = tmp_path / f"bad-box-{mode}.glb"
    _assembly_glb(path)
    _replace_collider_triangles(path, mode)
    rules = _rules(validate_glb(path, spec))
    assert rules["collider.box"] == "FAIL", rules


def test_collapsed_pivot_is_separate_from_position_finding(tmp_path: Path) -> None:
    profile = _profile(geometry_mode="assembly", collider="box", required_sockets=False)
    spec = _spec(profile, sockets=False)
    object.__setattr__(spec.parts[1].pivot, "position_m", [0, 0.2, 0])
    path = tmp_path / "collapsed.glb"
    _assembly_glb(path)
    rules = _rules(validate_glb(path, spec))
    assert rules["pivot.collapsed.turret"] == "FAIL"
    assert rules["pivot.position.turret"] == "FAIL"


def test_per_part_lod_bounds_are_checked_from_actual_vertices(tmp_path: Path) -> None:
    profile = _profile(geometry_mode="assembly", collider="box", required_sockets=False)
    spec = _spec(profile, sockets=False)
    path = tmp_path / "lod.glb"
    _assembly_glb(path)
    document, binary = _read_glb(path.read_bytes())
    document["nodes"].append(
        {"name": "SM_test_vehicle_turret_LOD1", "mesh": 0, "scale": [0.5, 0.5, 0.5]}
    )
    document["nodes"][3]["children"].append(9)
    _write_glb(path, document, binary)
    rules = _rules(validate_glb(path, spec))
    assert rules["lod.bounds.turret"] == "FAIL", rules


def test_socket_parent_and_forward_end_reject_wrong_attachment(tmp_path: Path) -> None:
    profile = _profile(geometry_mode="assembly", collider="box", required_sockets=True)
    spec = _spec(profile)
    path = tmp_path / "socket.glb"
    _assembly_glb(path)
    document, binary = _read_glb(path.read_bytes())
    document["nodes"][5]["children"].remove(7)
    document["nodes"][1]["children"].append(7)
    document["nodes"][7]["translation"][2] = -2.0
    _write_glb(path, document, binary)
    rules = _rules(validate_glb(path, spec))
    assert rules["socket.parent.muzzle"] == "FAIL"
    assert rules["socket.position.muzzle"] == "FAIL"


def test_source_normalization_observation_must_match_processed_hash_and_rotation(
    tmp_path: Path,
) -> None:
    from gamefactory.adapters.assets.v07_geometry_validation import validate_glb_v07

    profile = _profile(geometry_mode="assembly", collider="box", required_sockets=False)
    spec = _spec(profile, sockets=False)
    path = tmp_path / "asset.glb"
    _assembly_glb(path)
    valid = _observation(path)
    invalid = VerifiedSourceNormalization(
        valid.source_sha256,
        valid.processed_sha256,
        "+Z",
        True,
        (0, 0, 0, 1),
        valid.source_glb_bytes,
    )
    rules = _rules(validate_glb_v07(path, spec, source_observation=invalid))
    assert rules["orientation.source_front"] == "FAIL"


def test_source_front_positive_fixture_proves_single_rotation_and_deep_transform(
    tmp_path: Path,
) -> None:
    from gamefactory.adapters.assets.v07_geometry_validation import (
        validate_glb_v07,
        verify_source_to_processed_preservation,
    )

    profile = _profile(geometry_mode="assembly", collider="box", required_sockets=False)
    spec = _spec(profile)
    _articulate_spec(spec)
    object.__setattr__(spec.parts[0].pivot, "basis", (0, 1, 0, 0))
    source_path = tmp_path / "retained-source.glb"
    _raw_assembly_source(source_path)
    source_bytes = source_path.read_bytes()

    def observe(path: Path) -> VerifiedSourceNormalization:
        processed_bytes = path.read_bytes()
        return VerifiedSourceNormalization(
            hashlib.sha256(source_bytes).hexdigest(),
            hashlib.sha256(processed_bytes).hexdigest(),
            "+Z",
            True,
            (0, 1, 0, 0),
            source_bytes,
        )

    processed_path = tmp_path / "normalized.glb"
    _assembly_glb(processed_path, articulated=True)
    processed, binary = _read_glb(processed_path.read_bytes())
    processed["nodes"][1]["rotation"] = [0, -1, 0, 0]
    _write_glb(processed_path, processed, binary)
    passed = _rules(
        validate_glb_v07(processed_path, spec, source_observation=observe(processed_path))
    )
    assert passed["orientation.source_front"] == "PASS", passed
    assert passed["pivot.orientation.hull"] == "PASS", passed
    preservation = verify_source_to_processed_preservation(
        observe(processed_path), processed_path, spec
    )
    assert preservation.passed, preservation

    # Ry(180) applied twice is identity, so the retained source comparison must reject it.
    double_path = tmp_path / "double-normalized.glb"
    _assembly_glb(double_path, articulated=True)
    doubled = _rules(validate_glb_v07(double_path, spec, source_observation=observe(double_path)))
    assert doubled["orientation.source_front"] == "FAIL", doubled

    changed_path = tmp_path / "deep-transform-changed.glb"
    _assembly_glb(changed_path, articulated=True)
    changed, binary = _read_glb(changed_path.read_bytes())
    changed["nodes"][3]["translation"][1] += 0.05
    _write_glb(changed_path, changed, binary)
    changed_rules = _rules(
        validate_glb_v07(changed_path, spec, source_observation=observe(changed_path))
    )
    assert changed_rules["orientation.source_front"] == "FAIL", changed_rules

    socket_path = tmp_path / "changed-socket-local.glb"
    _assembly_glb(socket_path, articulated=True)
    socket_doc, binary = _read_glb(socket_path.read_bytes())
    socket_doc["nodes"][7]["translation"][0] = 0.1
    _write_glb(socket_path, socket_doc, binary)
    socket_result = verify_source_to_processed_preservation(observe(socket_path), socket_path, spec)
    assert not socket_result.passed, socket_result

    motion_path = tmp_path / "changed-motion-extras.glb"
    _assembly_glb(motion_path, articulated=True)
    motion_doc, binary = _read_glb(motion_path.read_bytes())
    motion_doc["nodes"][3]["extras"]["gf_axis"] = [0, 0, 1]
    _write_glb(motion_path, motion_doc, binary)
    motion_result = verify_source_to_processed_preservation(observe(motion_path), motion_path, spec)
    assert not motion_result.passed, motion_result


def test_source_preservation_accepts_split_vertices_but_rejects_same_bounds_wrong_topology(
    tmp_path: Path,
) -> None:
    from gamefactory.adapters.assets.v07_geometry_validation import (
        verify_source_to_processed_preservation,
    )

    profile = _profile(geometry_mode="assembly", collider="box", required_sockets=False)
    spec = _spec(profile)
    _articulate_spec(spec)
    source_path = tmp_path / "source.glb"
    _raw_assembly_source(source_path)
    source_bytes = source_path.read_bytes()

    def observation(path: Path) -> VerifiedSourceNormalization:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        return VerifiedSourceNormalization(
            hashlib.sha256(source_bytes).hexdigest(),
            digest,
            "-Z",
            False,
            (0, 0, 0, 1),
            source_bytes,
        )

    valid_path = tmp_path / "vertex-split.glb"
    _assembly_glb(valid_path, articulated=True)
    _split_processed_vertices(valid_path)
    valid = verify_source_to_processed_preservation(observation(valid_path), valid_path, spec)
    assert valid.passed, valid

    changed_path = tmp_path / "same-aabb-wrong-triangle.glb"
    _assembly_glb(changed_path, articulated=True)
    _split_processed_vertices(changed_path, change_topology=True)
    changed_doc, changed_binary = _read_glb(changed_path.read_bytes())
    positions = changed_doc["accessors"][-2]
    changed_rows = __import__(
        "gamefactory.adapters.assets.glb_validator", fromlist=["_accessor"]
    )._accessor(
        changed_doc,
        changed_binary,
        len(changed_doc["accessors"]) - 2,
        "VEC3",
    )
    assert tuple(min(row[a] for row in changed_rows) for a in range(3)) == tuple(positions["min"])
    assert tuple(max(row[a] for row in changed_rows) for a in range(3)) == tuple(positions["max"])
    result = verify_source_to_processed_preservation(observation(changed_path), changed_path, spec)
    assert not result.passed, result


def test_triangle_soup_comparison_tolerates_float32_boundary_drift() -> None:
    from gamefactory.adapters.assets.v07_geometry_validation import triangle_soups_equivalent

    source = (((0.12345649, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),)
    processed = (((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.12345651, 0.0, 0.0)),)
    assert triangle_soups_equivalent(source, processed)


def test_triangle_soup_tolerance_matching_preserves_alternate_candidates() -> None:
    from gamefactory.adapters.assets.v07_geometry_validation import triangle_soups_equivalent

    def triangle(offset: float) -> tuple[tuple[float, float, float], ...]:
        return ((offset, 0.0, 0.0), (offset + 0.1, 0.0, 0.0), (offset, 0.1, 0.0))

    source = (triangle(0.0), triangle(1.5e-5))
    processed = (triangle(-0.8e-5), triangle(0.6e-5))
    assert triangle_soups_equivalent(source, processed, tolerance=1e-5)
    assert triangle_soups_equivalent(
        tuple(reversed(source)), tuple(reversed(processed)), tolerance=1e-5
    )

    # Both source triangles can only use the first processed candidate, so no
    # perfect one-to-one geometric match exists.
    no_perfect_match = (triangle(0.6e-5), triangle(2.6e-5))
    assert not triangle_soups_equivalent(source, no_perfect_match, tolerance=1e-5)


def test_triangle_soup_exact_high_multiplicity_uses_multiset_fast_path() -> None:
    from gamefactory.adapters.assets.v07_geometry_validation import triangle_soups_equivalent

    triangle = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    repeated = (triangle,) * 400
    assert triangle_soups_equivalent(repeated, tuple(reversed(repeated)))


def test_source_preservation_rejects_extra_source_mesh_and_processed_root_wrapper(
    tmp_path: Path,
) -> None:
    from gamefactory.adapters.assets.v07_geometry_validation import (
        verify_source_to_processed_preservation,
    )

    profile = _profile(geometry_mode="assembly", collider="box", required_sockets=False)
    spec = _spec(profile)
    _articulate_spec(spec)
    valid_source = tmp_path / "source.glb"
    _raw_assembly_source(valid_source)
    valid_source_bytes = valid_source.read_bytes()

    extra_source = tmp_path / "extra-source-mesh.glb"
    _raw_assembly_source(extra_source)
    document, binary = _read_glb(extra_source.read_bytes())
    document["nodes"].append({"name": "SM_foreign_extra", "mesh": 0})
    document["nodes"][0]["children"].append(len(document["nodes"]) - 1)
    _write_glb(extra_source, document, binary)
    extra_bytes = extra_source.read_bytes()

    valid_processed = tmp_path / "processed.glb"
    _assembly_glb(valid_processed, articulated=True)
    valid_processed_bytes = valid_processed.read_bytes()

    wrapped_processed = tmp_path / "wrapped-processed.glb"
    _assembly_glb(wrapped_processed, articulated=True)
    document, binary = _read_glb(wrapped_processed.read_bytes())
    wrapper_index = len(document["nodes"])
    document["nodes"].append({"name": "WRAPPER", "children": [0]})
    document["scenes"] = [{"nodes": [wrapper_index]}]
    _write_glb(wrapped_processed, document, binary)
    processed_bytes = wrapped_processed.read_bytes()

    observation = VerifiedSourceNormalization(
        hashlib.sha256(valid_source_bytes).hexdigest(),
        hashlib.sha256(processed_bytes).hexdigest(),
        "-Z",
        False,
        (0, 0, 0, 1),
        valid_source_bytes,
    )
    wrapped = verify_source_to_processed_preservation(observation, wrapped_processed, spec)
    assert not wrapped.passed, wrapped

    observation_with_extra = VerifiedSourceNormalization(
        hashlib.sha256(extra_bytes).hexdigest(),
        hashlib.sha256(valid_processed_bytes).hexdigest(),
        "-Z",
        False,
        (0, 0, 0, 1),
        extra_bytes,
    )
    extra = verify_source_to_processed_preservation(observation_with_extra, valid_processed, spec)
    assert not extra.passed, extra


def test_single_mesh_actual_bounds_and_capsule_fit_fail_independently(tmp_path: Path) -> None:
    profile = _profile(geometry_mode="single_mesh", collider="capsule")
    spec = _spec(profile, capsule=True)
    path = tmp_path / "character.glb"
    create_box_glb(
        width_m=1,
        depth_m=1,
        height_m=1,
        mesh_name="SM_test_character",
        origin="center",
        output_path=path,
    )
    object.__setattr__(spec.dimensions, "height_m", 0.8)
    rules = _rules(validate_glb(path, spec))
    assert rules["dimensions.bounds"] == "FAIL"
    assert rules["collider.capsule.fit"] == "FAIL"


def test_single_mesh_box_capability_uses_canonical_group_order_and_executes(tmp_path: Path) -> None:
    profile = _profile(geometry_mode="single_mesh", collider="box")
    spec = _spec(profile)
    path = tmp_path / "single-box.glb"
    create_box_glb(
        width_m=1,
        depth_m=1,
        height_m=1,
        mesh_name="SM_test_character",
        origin="center",
        include_collider=True,
        collider_name="COL_test_character",
        output_path=path,
    )
    assert selected_v07_groups(spec, profile) == (
        "core",
        "single_mesh",
        "collider_box",
    )
    result = validate_glb(path, spec)
    rules = _rules(result)
    assert rules["collider.parent"] == "PASS", rules
    assert rules["collider.box"] == "PASS", rules
    _split_processed_vertices(path, mesh_index=1)
    split_rules = _rules(validate_glb(path, spec))
    assert split_rules["collider.box"] == "PASS", split_rules
    assert all(value == "PASS" for value in rules.values()), {
        key: value for key, value in rules.items() if value != "PASS"
    }
