"""Real local Blender/Godot acceptance for the private candidate vehicle@1 profile."""

from __future__ import annotations

import hashlib
import json
import struct
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml

from gamefactory.adapters.assets.assembly_ingest import ingest_assembly_source
from gamefactory.adapters.assets.v07_geometry_validation import (
    validate_glb_v07,
    verify_source_to_processed_preservation,
)
from gamefactory.adapters.dcc.assembly_processor import AssemblyProcessor
from gamefactory.adapters.dcc.blender import BlenderAdapter
from gamefactory.adapters.dcc.godot_assembly import verify_godot_assembly
from gamefactory.adapters.engines.godot import GodotAdapter
from gamefactory.adapters.engines.godot_image import decode_png
from gamefactory.core.domain.asset_contracts import (
    AssetSpecificationV07,
    parse_asset_specification_v07,
)
from gamefactory.core.domain.asset_profiles import (
    UNSUPPORTED_PROFILE_IDS,
    AssetProfileV07,
    ProfileRegistry,
    parse_profile_document_v07,
)

PROFILE_RESOURCE = "resources/profiles/vehicle.yml"
SPEC_RESOURCE = "resources/specs/armored_vehicle_test.yml"
ALL_REVIEW_VIEWS = (
    "front",
    "rear",
    "left",
    "right",
    "side",
    "three_quarter",
    "three_quarter_front",
    "three_quarter_rear",
    "top",
)


def _load_profile_spec() -> tuple[AssetProfileV07, AssetSpecificationV07]:
    profile_path = files("gamefactory").joinpath(PROFILE_RESOURCE)
    profile = AssetProfileV07(parse_profile_document_v07(profile_path.read_text(encoding="utf-8")))
    unsupported = tuple(item for item in UNSUPPORTED_PROFILE_IDS if item != profile.profile_id)
    registry = ProfileRegistry(available=(), unsupported=unsupported, available_v07=(profile,))
    spec_data = yaml.safe_load(
        files("gamefactory").joinpath(SPEC_RESOURCE).read_text(encoding="utf-8")
    )
    spec = parse_asset_specification_v07(spec_data, registry=registry)
    return profile, spec


def _append_buffer(data: bytes, target: int, binary: bytearray, views: list[dict[str, Any]]) -> int:
    binary.extend(b"\0" * ((-len(binary)) % 4))
    offset = len(binary)
    binary.extend(data)
    views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(data), "target": target})
    return len(views) - 1


def _write_box_assembly(path: Path, asset_id: str) -> None:
    """Write distinct hull/turret/barrel geometry with real nonzero parent pivots."""
    vertices_template = (
        (-0.5, -0.5, -0.5),
        (0.5, -0.5, -0.5),
        (0.5, -0.5, 0.5),
        (-0.5, -0.5, 0.5),
        (-0.5, 0.5, -0.5),
        (0.5, 0.5, -0.5),
        (0.5, 0.5, 0.5),
        (-0.5, 0.5, 0.5),
    )
    triangle_indices = (
        0,
        1,
        2,
        0,
        2,
        3,
        4,
        6,
        5,
        4,
        7,
        6,
        0,
        4,
        5,
        0,
        5,
        1,
        2,
        6,
        7,
        2,
        7,
        3,
        0,
        3,
        7,
        0,
        7,
        4,
        1,
        5,
        6,
        1,
        6,
        2,
    )
    binary = bytearray()
    views: list[dict[str, Any]] = []
    accessors: list[dict[str, Any]] = []
    meshes: list[dict[str, Any]] = []
    for name, (width, height, depth) in (
        ("hull", (2.8, 0.9, 3.4)),
        ("turret", (1.8, 0.55, 1.65)),
        ("barrel", (0.36, 0.35, 2.8)),
    ):
        vertices = [(x * width, y * height, z * depth) for x, y, z in vertices_template]
        position_bytes = b"".join(struct.pack("<fff", *vertex) for vertex in vertices)
        position_view = _append_buffer(position_bytes, 34962, binary, views)
        position_accessor = len(accessors)
        accessors.append(
            {
                "bufferView": position_view,
                "componentType": 5126,
                "count": len(vertices),
                "type": "VEC3",
                "min": [min(point[axis] for point in vertices) for axis in range(3)],
                "max": [max(point[axis] for point in vertices) for axis in range(3)],
            }
        )
        index_bytes = struct.pack(f"<{len(triangle_indices)}H", *triangle_indices)
        index_view = _append_buffer(index_bytes, 34963, binary, views)
        index_accessor = len(accessors)
        accessors.append(
            {
                "bufferView": index_view,
                "componentType": 5123,
                "count": len(triangle_indices),
                "type": "SCALAR",
                "min": [min(triangle_indices)],
                "max": [max(triangle_indices)],
            }
        )
        meshes.append(
            {
                "name": f"{asset_id}_{name}_mesh",
                "primitives": [
                    {
                        "attributes": {"POSITION": position_accessor},
                        "indices": index_accessor,
                        "material": 0,
                    }
                ],
            }
        )

    document = {
        "asset": {"version": "2.0", "generator": "vehicle-profile-test-fixture"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [
            {"name": "ROOT", "children": [1]},
            {
                "name": "PART_hull",
                "translation": [0.0, 0.45, 0.21],
                "extras": {"gf_motion": "fixed"},
                "children": [2, 3],
            },
            {"name": f"SM_{asset_id}_hull_LOD0", "mesh": 0},
            {
                "name": "PART_turret",
                "translation": [0.0, 0.45, 0.1],
                "extras": {"gf_motion": "revolute", "gf_axis": [0.0, 1.0, 0.0]},
                "children": [4, 5],
            },
            {"name": f"SM_{asset_id}_turret_LOD0", "mesh": 1},
            {
                "name": "PART_barrel",
                "translation": [0.0, 0.0, -0.82],
                "extras": {"gf_motion": "revolute", "gf_axis": [1.0, 0.0, 0.0]},
                "children": [6, 7],
            },
            {"name": f"SM_{asset_id}_barrel_LOD0", "mesh": 2},
            {"name": "SOCKET_muzzle", "translation": [0.0, 0.0, -1.4]},
        ],
        "meshes": meshes,
        "materials": [
            {
                "name": "M_ArmoredVehicle",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.18, 0.24, 0.3, 1.0],
                    "metallicFactor": 0.25,
                    "roughnessFactor": 0.7,
                },
            }
        ],
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": len(binary)}],
    }
    encoded = json.dumps(document, separators=(",", ":")).encode("utf-8")
    encoded += b" " * ((-len(encoded)) % 4)
    binary.extend(b"\0" * ((-len(binary)) % 4))
    total = 12 + 8 + len(encoded) + 8 + len(binary)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(encoded), 0x4E4F534A)
        + encoded
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )


def _read_glb(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    json_length = struct.unpack_from("<I", raw, 12)[0]
    document = json.loads(raw[20 : 20 + json_length].decode("utf-8").rstrip())
    binary_offset = 20 + json_length
    binary_length = struct.unpack_from("<I", raw, binary_offset)[0]
    return document, raw[binary_offset + 8 : binary_offset + 8 + binary_length]


def _write_glb(path: Path, document: dict[str, Any], binary: bytes) -> None:
    encoded = json.dumps(document, separators=(",", ":")).encode("utf-8")
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


def _blender_executable() -> str | None:
    result = BlenderAdapter().detect_tool()
    return result.executable_path if result.available else None


@pytest.mark.real_godot
def test_vehicle_candidate_real_blender_and_godot_acceptance_with_semantic_negatives(
    tmp_path: Path,
) -> None:
    blender = _blender_executable()
    godot = GodotAdapter().find_candidate_executable()
    if blender is None:
        pytest.skip("Blender is not installed on this host")
    if godot is None:
        pytest.skip("Godot is not installed on this host")

    profile, spec = _load_profile_spec()
    source_file = tmp_path / "armored_vehicle_source.glb"
    _write_box_assembly(source_file, spec.asset_id)
    managed_root = tmp_path / "managed"
    package = ingest_assembly_source(
        source_glb_path=source_file,
        managed_root=managed_root,
        relative_package_dir="source/armored_vehicle_test/r001",
        spec=spec,
        authoring_tool_name="Blender",
        authoring_tool_version="5.2.1",
        source_front="-Z",
        actor="isolated-integration-fixture",
        reason="Private candidate profile end-to-end fixture",
    )
    source_before = package.retained_glb_path.read_bytes()

    process_dir = tmp_path / "processed"
    process_dir.mkdir()
    processed = process_dir / "armored_vehicle_processed.glb"
    process_report = process_dir / "process-report.json"
    process_result = AssemblyProcessor().process_assembly(
        package=package,
        spec=spec,
        expected_provenance_sha256=package.retained_provenance_sha256,
        processed_glb_path=processed,
        report_path=process_report,
        timeout_seconds=90.0,
    )
    assert process_result.status == "SUCCESS"
    assert processed.is_file() and processed.stat().st_size > 0
    assert package.retained_glb_path.read_bytes() == source_before

    preservation = verify_source_to_processed_preservation(
        process_result.verified_normalization, processed, spec
    )
    assert preservation.passed, [finding.message for finding in preservation.findings]
    validation = validate_glb_v07(
        processed,
        spec,
        source_observation=process_result.verified_normalization,
    )
    assert validation.passed, [
        (finding.rule_id, finding.message, finding.actual)
        for finding in validation.findings
        if finding.severity.value == "FAIL"
    ]

    actual_processed_hash = hashlib.sha256(processed.read_bytes()).hexdigest()
    runtime = verify_godot_assembly(
        spec,
        profile,
        processed,
        actual_processed_hash,
        "vehicle-candidate-runtime-1",
        attempt_number=1,
        workflow_id="vehicle-candidate-workflow-test-only",
        revision=1,
        output_dir=tmp_path / "godot-runtime",
        godot_executable=godot,
    )
    assert runtime.status == "PASS"
    assert runtime.observation["restoration_verified"] is True
    assert set(runtime.captures) == set(ALL_REVIEW_VIEWS)
    for view, png in runtime.captures.items():
        decoded = decode_png(png, 1280, 720)
        assert (decoded.width, decoded.height) == (1280, 720), view

    articulation = {item["part_id"]: item for item in runtime.observation["articulation_results"]}
    assert set(articulation) == {"turret", "barrel"}
    for record in articulation.values():
        assert record["pivot_world_ok"] is True
        assert record["descendants_rigid_ok"] is True
        assert record["ancestors_siblings_unchanged"] is True
        assert record["restored_ok"] is True
    assert runtime.observation["sockets_verified"][0]["marker_created"] is True
    assert runtime.observation["collider"]["physics_ray_hit"] is True

    # Profile-specific negative fixtures mutate one semantic property at a time
    # in the real Blender output, then prove the independent decoded-GLB validator
    # reports its separate rule instead of accepting the role label alone.
    negative_mutations = {
        "wrong_axis": ("pivot.axis.barrel", lambda doc: _wrong_axis(doc)),
        "wrong_parent": ("part.parent.barrel", lambda doc: _wrong_parent(doc)),
        "muzzle_orientation": (
            "socket.orientation.muzzle",
            lambda doc: _wrong_muzzle_orientation(doc),
        ),
        "muzzle_behind": ("socket.position.muzzle", lambda doc: _move_muzzle_behind(doc)),
    }
    for name, (rule_id, mutate) in negative_mutations.items():
        document, binary = _read_glb(processed)
        mutate(document)
        bad_glb = tmp_path / f"negative-{name}.glb"
        _write_glb(bad_glb, document, binary)
        bad_result = validate_glb_v07(
            bad_glb,
            spec,
            source_observation=process_result.verified_normalization,
        )
        assert not bad_result.passed, name
        failed_rule_ids = {
            finding.rule_id for finding in bad_result.findings if finding.severity.value == "FAIL"
        }
        assert rule_id in failed_rule_ids, name


def _named_node(document: dict[str, Any], name: str) -> dict[str, Any]:
    return next(node for node in document["nodes"] if node.get("name") == name)


def _wrong_axis(document: dict[str, Any]) -> None:
    _named_node(document, "PART_barrel")["extras"]["gf_axis"] = [0.0, 1.0, 0.0]


def _wrong_parent(document: dict[str, Any]) -> None:
    hull = _named_node(document, "PART_hull")
    turret = _named_node(document, "PART_turret")
    barrel = _named_node(document, "PART_barrel")
    turret["children"].remove(document["nodes"].index(barrel))
    hull["children"].append(document["nodes"].index(barrel))


def _wrong_muzzle_orientation(document: dict[str, Any]) -> None:
    _named_node(document, "SOCKET_muzzle")["rotation"] = [0.0, 1.0, 0.0, 0.0]


def _move_muzzle_behind(document: dict[str, Any]) -> None:
    _named_node(document, "SOCKET_muzzle")["translation"] = [0.0, 0.0, 1.4]
