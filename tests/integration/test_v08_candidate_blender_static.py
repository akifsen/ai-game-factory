"""Real Blender export static acceptance for V0.8-3A candidate validation."""

from __future__ import annotations

import hashlib
import os
import subprocess
from importlib import resources
from pathlib import Path

import pytest

from gamefactory.adapters.assets.v08_candidate_geometry import (
    envelope_size_from_aabb,
    visual_aabb_in_reference_root_frame,
)
from gamefactory.adapters.assets.v08_candidate_rules import decode_candidate_glb
from gamefactory.adapters.assets.v08_candidate_validate import validate_v08_candidate_glb
from gamefactory.core.domain.v08_candidate_contracts import (
    load_packaged_candidate_profile,
    parse_asset_specification_v08_candidate,
    profile_document_hash,
)

BLENDER = os.environ.get(
    "GAMEFACTORY_TEST_BLENDER",
    r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe",
)

CANONICAL_WIDTH_M = 0.56
CANONICAL_DEPTH_M = 0.12
CANONICAL_HEIGHT_M = 0.5125
DIMENSION_TOLERANCE_M = 0.02


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


def _blender_candidate_spec(
    *,
    glb_path: Path,
    width_m: float,
    depth_m: float,
    height_m: float,
) -> dict:
    profile = load_packaged_candidate_profile()
    return {
        "schema_version": "0.8.0-candidate",
        "asset_id": "humanoid_skin_blender_static_01",
        "category": "character",
        "profile": "rigged_character",
        "profile_version": 1,
        "intent": "CLOSED candidate static acceptance for canonical Blender humanoid export",
        "source_kind": "local_verified_rig",
        "dimensions": {
            "width_m": width_m,
            "depth_m": depth_m,
            "height_m": height_m,
        },
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
        "target_import_path": "assets/generated/character/humanoid_skin_blender_static_01/",
        "collider": {
            "policy": "capsule",
            "capsule": {"radius_m": 0.1, "height_m": 0.5},
        },
        "profile_document_hash": profile_document_hash(profile.document),
        "processed_glb_sha256": hashlib.sha256(glb_path.read_bytes()).hexdigest(),
    }


@pytest.mark.skipif(not Path(BLENDER).is_file(), reason="Blender executable not available")
def test_blender_export_passes_candidate_static_validation(tmp_path: Path) -> None:
    out = tmp_path / "blender_humanoid.glb"
    _run_blender_export(out)
    decoded = decode_candidate_glb(out)
    mins, maxs = visual_aabb_in_reference_root_frame(decoded)
    width_m, height_m, depth_m = envelope_size_from_aabb(mins, maxs)
    assert abs(width_m - CANONICAL_WIDTH_M) <= DIMENSION_TOLERANCE_M
    assert abs(depth_m - CANONICAL_DEPTH_M) <= DIMENSION_TOLERANCE_M
    assert abs(height_m - CANONICAL_HEIGHT_M) <= DIMENSION_TOLERANCE_M
    spec = parse_asset_specification_v08_candidate(
        _blender_candidate_spec(
            glb_path=out,
            width_m=width_m,
            depth_m=depth_m,
            height_m=height_m,
        )
    )
    result = validate_v08_candidate_glb(out, spec)
    assert result.status.value == "PASS"
    assert not any(f.severity.value == "FAIL" for f in result.findings)
    root_findings = [f for f in result.findings if f.rule_id == "core_v08_candidate.reference_root"]
    assert root_findings and root_findings[0].severity.value == "PASS"
    hash_findings = [f for f in result.findings if f.rule_id == "core_v08_candidate.glb.hash"]
    assert hash_findings and hash_findings[0].severity.value == "PASS"
