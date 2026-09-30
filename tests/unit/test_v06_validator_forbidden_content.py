"""Tests for V0.6 GLB validator rejection of forbidden skin and animation content.

Pins observed V0.6 behavior: any GLB containing skins or animations is rejected
at the inspection phase with a single failing glb.parse finding.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.assets.glb_validator import validate_glb
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.core.domain.asset_contracts import (
    AssetSpecification,
    ValidationFindingSeverity,
    parse_asset_specification,
)

CRATE_SPEC_PATH = Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
PICKUP_SPEC_PATH = Path("src/gamefactory/resources/fixtures/energy_pickup_test.yml")
WALL_SPEC_PATH = Path("src/gamefactory/resources/fixtures/wall_panel_test.yml")


def _read_document(raw: bytes) -> tuple[dict[str, Any], bytes]:
    json_len = struct.unpack_from("<I", raw, 12)[0]
    return json.loads(raw[20 : 20 + json_len].decode().rstrip()), raw[20 + json_len + 8 :]


def _write_document(path: Path, doc: dict[str, Any], binary: bytes) -> None:
    json_data = json.dumps(doc, separators=(",", ":")).encode()
    json_data += b" " * ((-len(json_data)) % 4)
    binary += b"\0" * ((-len(binary)) % 4)
    total = 12 + 8 + len(json_data) + 8 + len(binary)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(json_data), 0x4E4F534A)
        + json_data
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )


def _build_valid_glb(path: Path, spec: AssetSpecification, profile_key: str) -> None:
    overrides: dict[str, Any] = {
        "width_m": spec.dimensions.width_m,
        "depth_m": spec.dimensions.depth_m,
        "height_m": spec.dimensions.height_m,
        "mesh_name": f"SM_{spec.asset_id}",
        "collider_name": f"COL_{spec.asset_id}",
        "origin": spec.origin_policy,
        "output_path": path,
    }
    if profile_key == "static_prop@1":
        overrides.update({"include_lod1": True, "include_collider": True, "include_texture": True})
    elif profile_key in ("pickup@1", "modular_piece@1"):
        overrides.update({"include_lod1": False, "include_collider": True})
    create_box_glb(**overrides)


@pytest.mark.parametrize(
    ("profile_key", "spec_path"),
    [
        ("static_prop@1", CRATE_SPEC_PATH),
        ("pickup@1", PICKUP_SPEC_PATH),
        ("modular_piece@1", WALL_SPEC_PATH),
    ],
)
def test_validator_rejects_glb_with_skins(
    profile_key: str, spec_path: Path, tmp_path: Path
) -> None:
    spec = parse_asset_specification(spec_path)
    assert spec.bound_profile().qualified == profile_key

    glb_path = tmp_path / f"{spec.asset_id}_skin.glb"
    _build_valid_glb(glb_path, spec, profile_key)

    doc, binary = _read_document(glb_path.read_bytes())
    doc.setdefault("nodes", []).append({"name": "joint_0"})
    joint_idx = len(doc["nodes"]) - 1
    doc["skins"] = [{"joints": [joint_idx]}]
    _write_document(glb_path, doc, binary)

    result = validate_glb(glb_path, spec)
    assert result.status == ValidationFindingSeverity.FAIL

    findings_tuples = [
        (f.rule_id, f.severity.value, f.expected, f.actual, f.message) for f in result.findings
    ]
    expected_findings = [
        (
            "glb.parse",
            "FAIL",
            "supported safe profile GLB subset",
            "skins/rigging are forbidden by the asset profile",
            "GLB could not be safely validated",
        )
    ]
    assert findings_tuples == expected_findings


@pytest.mark.parametrize(
    ("profile_key", "spec_path"),
    [
        ("static_prop@1", CRATE_SPEC_PATH),
        ("pickup@1", PICKUP_SPEC_PATH),
        ("modular_piece@1", WALL_SPEC_PATH),
    ],
)
def test_validator_rejects_glb_with_animations(
    profile_key: str, spec_path: Path, tmp_path: Path
) -> None:
    spec = parse_asset_specification(spec_path)
    assert spec.bound_profile().qualified == profile_key

    glb_path = tmp_path / f"{spec.asset_id}_anim.glb"
    _build_valid_glb(glb_path, spec, profile_key)

    doc, binary = _read_document(glb_path.read_bytes())
    doc["animations"] = [{"channels": [], "samplers": []}]
    _write_document(glb_path, doc, binary)

    result = validate_glb(glb_path, spec)
    assert result.status == ValidationFindingSeverity.FAIL

    findings_tuples = [
        (f.rule_id, f.severity.value, f.expected, f.actual, f.message) for f in result.findings
    ]
    expected_findings = [
        (
            "glb.parse",
            "FAIL",
            "supported safe profile GLB subset",
            "animations are forbidden by the asset profile",
            "GLB could not be safely validated",
        )
    ]
    assert findings_tuples == expected_findings
