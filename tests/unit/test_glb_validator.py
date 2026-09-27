from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.glb_validator import validate_glb
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.core.domain.asset_contracts import parse_asset_specification


def _spec():
    return parse_asset_specification(
        Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
    )


def _document(raw: bytes) -> tuple[dict[str, Any], bytes]:
    json_len = struct.unpack_from("<I", raw, 12)[0]
    doc = json.loads(raw[20 : 20 + json_len].decode().rstrip())
    bin_start = 20 + json_len + 8
    return doc, raw[bin_start:]


def _write_glb(path: Path, doc: dict[str, Any], binary: bytes) -> None:
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


def _good(path: Path) -> None:
    asset_id = _spec().asset_id
    create_box_glb(
        mesh_name=f"SM_{asset_id}",
        collider_name=f"COL_{asset_id}",
        include_lod1=True,
        include_collider=True,
        include_texture=True,
        output_path=path,
    )


def test_validator_passes_real_geometry_textured_fixture(tmp_path: Path) -> None:
    path = tmp_path / "asset.glb"
    _good(path)
    result = validate_glb(path, _spec())
    assert result.status.value == "PASS", result.to_dict()


def test_validator_rejects_truncated_glb_and_out_of_bounds_accessor(tmp_path: Path) -> None:
    path = tmp_path / "bad.glb"
    _good(path)
    path.write_bytes(path.read_bytes()[:-8])
    assert validate_glb(path, _spec()).status.value == "FAIL"

    _good(path)
    doc, _ = _document(path.read_bytes())
    doc["accessors"][0]["count"] = 100000
    binary = _document(path.read_bytes())[1]
    _write_glb(path, doc, binary)
    assert validate_glb(path, _spec()).status.value == "FAIL"


def test_validator_enforces_budgets_and_lod_collider_geometry(tmp_path: Path) -> None:
    path = tmp_path / "asset.glb"
    create_box_glb(
        include_lod1=True,
        include_collider=True,
        num_materials=3,
        include_texture=True,
        texture_size=(4096, 4096),
        output_path=path,
    )
    rules = {f.rule_id: f.severity.value for f in validate_glb(path, _spec()).findings}
    assert rules["materials.budget"] == "FAIL"
    assert rules["textures.dimension"] == "FAIL"

    _good(path)
    doc, binary = _document(path.read_bytes())
    next(n for n in doc["nodes"] if n["name"].endswith("_LOD1"))["name"] = "UNEXPECTED_MESH"
    _write_glb(path, doc, binary)
    rules = {f.rule_id: f.severity.value for f in validate_glb(path, _spec()).findings}
    assert rules["lod1.present"] == "FAIL"


def test_validator_catches_processed_asset_mutations(tmp_path: Path) -> None:
    path = tmp_path / "asset.glb"
    _good(path)
    doc, binary = _document(path.read_bytes())
    doc["nodes"] = [n for n in doc["nodes"] if not n["name"].startswith("COL_")]
    doc["scenes"][0]["nodes"] = list(range(len(doc["nodes"])))
    doc["meshes"] = doc["meshes"][:2]
    _write_glb(path, doc, binary)
    rules = {f.rule_id: f.severity.value for f in validate_glb(path, _spec()).findings}
    assert rules["collider.geometry"] == "FAIL"

    _good(path)
    doc, binary = _document(path.read_bytes())
    doc["nodes"][0]["scale"] = [10.0, 10.0, 10.0]
    _write_glb(path, doc, binary)
    rules = {f.rule_id: f.severity.value for f in validate_glb(path, _spec()).findings}
    assert rules["scale.bounds"] == "FAIL"


def test_validator_rejects_missing_uv_for_texture_and_external_references(tmp_path: Path) -> None:
    path = tmp_path / "asset.glb"
    _good(path)
    doc, binary = _document(path.read_bytes())
    doc["meshes"][0]["primitives"][0]["attributes"].pop("TEXCOORD_0")
    _write_glb(path, doc, binary)
    assert validate_glb(path, _spec()).status.value == "FAIL"


def test_validator_decodes_texture_stream_and_validates_all_material_slots(tmp_path: Path) -> None:
    path = tmp_path / "asset.glb"
    _good(path)
    doc, binary = _document(path.read_bytes())
    image_view = doc["bufferViews"][doc["images"][0]["bufferView"]]
    offset = image_view.get("byteOffset", 0)
    damaged = bytearray(binary)
    damaged[offset + 48] ^= 0xFF
    _write_glb(path, doc, bytes(damaged))
    assert validate_glb(path, _spec()).status.value == "FAIL"

    _good(path)
    doc, binary = _document(path.read_bytes())
    doc["materials"][0]["normalTexture"] = {"index": 7}
    _write_glb(path, doc, binary)
    assert validate_glb(path, _spec()).status.value == "FAIL"

    _good(path)
    doc, binary = _document(path.read_bytes())
    doc["meshes"][0]["primitives"][0]["material"] = -1
    _write_glb(path, doc, binary)
    assert validate_glb(path, _spec()).status.value == "FAIL"

    _good(path)
    doc, binary = _document(path.read_bytes())
    doc["images"] = [{"uri": "../../outside.png"}]
    _write_glb(path, doc, binary)
    assert validate_glb(path, _spec()).status.value == "FAIL"
