"""Real Godot V0.7 static-character runtime integration tests."""

from __future__ import annotations

import hashlib
import json
import os
import struct
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.dcc.blender import BlenderAdapter
from gamefactory.adapters.dcc.character_processor import CharacterProcessor
from gamefactory.adapters.dcc.godot_character import (
    _actual_glb_facts,
    _strict_json,
    _validate_observation,
    verify_godot_character,
)
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.core.domain.asset_contracts import parse_asset_specification_v07
from gamefactory.core.domain.asset_profiles import (
    AssetProfileV07,
    ProfileRegistry,
    builtin_registry,
    parse_profile_document_v07,
)
from gamefactory.core.domain.errors import ValidationError


def _profile() -> AssetProfileV07:
    framing = builtin_registry().get("static_prop").document.model_dump(mode="json")["framing"]
    return AssetProfileV07(
        parse_profile_document_v07(
            {
                "schema_version": "asset-profile-0.7.0",
                "profile_id": "real_godot_character",
                "version": 1,
                "categories": ["character"],
                "review_views": ["front", "rear", "left", "right", "three_quarter"],
                "framing": framing,
                "dimension_rules": {"min_m": 0.1, "max_m": 10.0},
                "processing": {
                    "lod0_required": True,
                    "lod1_required": True,
                    "allowed_lod_policies": ["lod0_lod1"],
                    "allowed_collider_policies": ["capsule"],
                    "allowed_origin_policies": ["bottom_center", "center"],
                    "default_origin_policy": "bottom_center",
                    "dimension_tolerance_m": 0.02,
                    "snap_grid_m": None,
                    "rig_forbidden": True,
                    "animation_forbidden": True,
                    "max_materials": 8,
                    "max_texture_dimension": 4096,
                    "max_triangles_lod0": 20000,
                },
                "godot": {"body_kind": "static_body", "require_ray_hit": True, "require_area": False},
                "runtime": {"bounds_tolerance_ratio": 0.1, "bounds_tolerance_floor_m": 0.15},
                "geometry_mode": "single_mesh",
                "accepted_source_kinds": ["provider_generated"],
                "assembly": None,
            }
        )
    )


def _spec(profile: AssetProfileV07, origin_policy: str):
    return parse_asset_specification_v07(
        {
            "schema_version": "0.7.0",
            "asset_id": "runtime_character",
            "category": "character",
            "profile": profile.profile_id,
            "profile_version": profile.version,
            "intent": "Godot runtime proof fixture",
            "source_kind": "provider_generated",
            "dimensions": {"width_m": 0.6, "height_m": 1.8, "depth_m": 0.4},
            "orientation": {"up": "+Y", "front": "-Z"},
            "origin_policy": origin_policy,
            "lod_policy": "lod0_lod1",
            "geometry_budget": {
                "max_triangles_lod0": 20000,
                "max_triangles_lod1": 10000,
                "lod_ratio": 0.5,
            },
            "material_budget": {"max_materials": 8},
            "texture_budget": {"max_dimension": 4096},
            "collider": {"policy": "capsule", "capsule": {"radius_m": 0.3, "height_m": 1.7}},
        },
        registry=ProfileRegistry(available=(), unsupported=(), available_v07=(profile,)),
    )


def _read_glb(path: Path) -> tuple[dict[str, Any], bytes]:
    data = path.read_bytes()
    length = struct.unpack_from("<I", data, 12)[0]
    document = json.loads(data[20 : 20 + length].decode("utf-8").rstrip())
    binary_offset = 20 + length + 8
    return document, data[binary_offset:]


def _write_glb(path: Path, document: dict[str, Any], binary: bytes) -> None:
    json_chunk = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * ((-len(json_chunk)) % 4)
    binary += b"\0" * ((-len(binary)) % 4)
    total = 12 + 8 + len(json_chunk) + 8 + len(binary)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(json_chunk), 0x4E4F534A)
        + json_chunk
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )


def _fixture_glbs(directory: Path, spec: Any) -> tuple[Path, Path]:
    raw = directory / f"raw-{spec.origin_policy}.glb"
    processed = directory / f"processed-{spec.origin_policy}.glb"
    create_box_glb(
        width_m=0.6,
        height_m=1.8,
        depth_m=0.4,
        mesh_name="raw_humanoid_fixture",
        origin="center",
        output_path=raw,
    )
    create_box_glb(
        width_m=0.6,
        height_m=1.8,
        depth_m=0.4,
        mesh_name=f"SM_{spec.asset_id}_LOD0",
        origin=spec.origin_policy,
        output_path=processed,
    )
    document, binary = _read_glb(processed)
    document["meshes"].append(dict(document["meshes"][0]))
    document["meshes"][0]["name"] = f"SM_{spec.asset_id}_LOD0"
    document["meshes"][1]["name"] = f"SM_{spec.asset_id}_LOD1"
    document["nodes"] = [
        {"name": "ROOT", "children": [1, 2]},
        {"name": f"SM_{spec.asset_id}_LOD0", "mesh": 0},
        {"name": f"SM_{spec.asset_id}_LOD1", "mesh": 1},
    ]
    document["scenes"] = [{"nodes": [0]}]
    document["scene"] = 0
    _write_glb(processed, document, binary)
    return raw, processed


@pytest.fixture
def godot_executable() -> str:
    value = os.environ.get("GAMEFACTORY_TEST_GODOT")
    if not value:
        pytest.skip("Set GAMEFACTORY_TEST_GODOT to run actual Godot character runtime tests")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        pytest.fail(f"Configured Godot executable does not exist: {path}")
    return str(path)


@pytest.mark.real_godot
@pytest.mark.parametrize("origin_policy", ["bottom_center", "center"])
def test_real_godot_character_import_capsule_ray_and_rendered_capture(
    tmp_path: Path, godot_executable: str, origin_policy: str
) -> None:
    profile = _profile()
    spec = _spec(profile, origin_policy)
    raw, processed = _fixture_glbs(tmp_path, spec)
    raw_hash = hashlib.sha256(raw.read_bytes()).hexdigest()
    processed_hash = hashlib.sha256(processed.read_bytes()).hexdigest()
    result = verify_godot_character(
        spec,
        profile,
        raw,
        raw_hash,
        processed,
        processed_hash,
        execution_id=f"character-{origin_policy}",
        workflow_id=f"character-{origin_policy}",
        output_dir=tmp_path / f"godot-{origin_policy}",
        godot_executable=godot_executable,
        timeout_seconds=90,
    )
    assert result.status == "PASS"
    assert result.raw_glb_sha256 == raw_hash
    assert result.processed_glb_sha256 == processed_hash
    assert result.observation["status"] == "PASS"
    assert result.observation["collider"]["shape_class"] == "CapsuleShape3D"
    assert result.observation["collider"]["radius_m"] == pytest.approx(0.3)
    assert result.observation["collider"]["height_m"] == pytest.approx(1.7)
    assert result.observation["collider"]["center_y_m"] == pytest.approx(
        0.85 if origin_policy == "bottom_center" else 0.0
    )
    assert result.observation["collider"]["physics_ray_hit"] is True
    assert set(result.captures) == {"front", "rear", "left", "right", "three_quarter"}
    assert all(len(png) > 1000 for png in result.captures.values())

    request = json.loads(result.artifacts["runtime-request.json"])
    facts = _actual_glb_facts(processed.read_bytes(), spec, profile, processed)
    mutations = []
    for view in request["review_views"]:
        for field, replacement in (
            ("camera_up", [True, 0, 0]),
            ("camera_direction", [0, 10**400, 0]),
        ):
            forged = deepcopy(result.observation)
            forged["view_framing"][view][field] = replacement
            mutations.append(forged)
        forged = deepcopy(result.observation)
        forged["view_framing"][view]["view_axis"] = "+Y"
        mutations.append(forged)
    for mutation in (
        {"lods": {**deepcopy(result.observation["lods"]), f"SM_{spec.asset_id}_LOD0": {
            **deepcopy(result.observation["lods"][f"SM_{spec.asset_id}_LOD0"]),
            "local_position": [False, 0, 0],
        }}},
        {"lods": {**deepcopy(result.observation["lods"]), f"SM_{spec.asset_id}_LOD1": {
            **deepcopy(result.observation["lods"][f"SM_{spec.asset_id}_LOD1"]),
            "local_basis": [[1, True, 0], [0, 1, 0], [0, 0, 1]],
        }}},
        {"lods": {**deepcopy(result.observation["lods"]), f"SM_{spec.asset_id}_LOD0": {
            **deepcopy(result.observation["lods"][f"SM_{spec.asset_id}_LOD0"]),
            "local_scale": [1, 10**400, 1],
        }}},
        {"lods": {**deepcopy(result.observation["lods"]), f"SM_{spec.asset_id}_LOD0": {
            **deepcopy(result.observation["lods"][f"SM_{spec.asset_id}_LOD0"]),
            "surface_count": True,
        }}},
        {"collider": {**result.observation["collider"], "radius_m": False}},
        {"collider": {**result.observation["collider"], "center_y_m": 999}},
        {"collider": {**result.observation["collider"], "physics_ray_hit": False}},
        {"request_digest": "0" * 64},
        {"attempt_number": request["attempt_number"] + 1},
    ):
        forged = deepcopy(result.observation)
        forged.update(mutation)
        mutations.append(forged)
    for forged in mutations:
        with pytest.raises(ValidationError):
            _validate_observation(
                forged, spec=spec, profile=profile, views=request["review_views"],
                request=request, facts=facts,
            )


def test_strict_observation_json_rejects_nonfinite_and_unrepresentable_numbers() -> None:
    with pytest.raises(ValidationError, match="strict UTF-8 JSON|numeric value"):
        _strict_json(b'{"status":"PASS","value":1e999}', "observation")
    with pytest.raises(ValidationError, match="strict UTF-8 JSON|numeric value"):
        _strict_json(b'{"status":"PASS","value":' + str(10**400).encode() + b'}', "observation")


@pytest.mark.real_godot
def test_real_blender_character_processing_composes_with_godot_runtime(
    tmp_path: Path, godot_executable: str
) -> None:
    blender_executable = os.environ.get("GAMEFACTORY_TEST_BLENDER") or BlenderAdapter().find_candidate_executable()
    if not blender_executable:
        pytest.skip("Set GAMEFACTORY_TEST_BLENDER or install Blender to run composition test")
    profile = _profile()
    spec = _spec(profile, "bottom_center")
    raw = tmp_path / "blender-source-character.glb"
    create_box_glb(
        width_m=0.6,
        height_m=1.8,
        depth_m=0.4,
        mesh_name="source_humanoid",
        origin="center",
        output_path=raw,
    )
    raw_hash = hashlib.sha256(raw.read_bytes()).hexdigest()
    processed = tmp_path / "blender-processed-character.glb"
    report = tmp_path / "blender-character-report.json"
    processed_result = CharacterProcessor(blender_executable=blender_executable).process_character(
        raw,
        spec,
        profile,
        expected_raw_glb_sha256=raw_hash,
        processed_glb_path=processed,
        report_path=report,
        timeout_seconds=120,
    )
    assert processed_result.status == "SUCCESS"
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == raw_hash
    runtime = verify_godot_character(
        spec,
        profile,
        raw,
        raw_hash,
        processed,
        processed_result.processed_glb_sha256,
        execution_id="blender-godot-character",
        workflow_id="blender-godot-character",
        output_dir=tmp_path / "godot-blender-character",
        godot_executable=godot_executable,
        timeout_seconds=120,
    )
    assert runtime.status == "PASS"
    assert runtime.raw_glb_sha256 == raw_hash
    assert runtime.processed_glb_sha256 == processed_result.processed_glb_sha256
    assert set(runtime.captures) == {"front", "rear", "left", "right", "three_quarter"}
