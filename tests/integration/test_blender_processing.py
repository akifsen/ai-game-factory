"""Integration tests for Blender asset processing pipeline (Sections 12-17, 32, 48)."""

import json
import struct
from pathlib import Path

import pytest

from gamefactory.adapters.assets.glb_validator import validate_glb
from gamefactory.adapters.dcc.blender import BlenderAdapter
from gamefactory.adapters.dcc.blender_processor import BlenderAssetProcessor
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.core.domain.asset_contracts import parse_asset_specification
from gamefactory.core.domain.errors import DccFailedError


def _rewrite_glb(source: Path, destination: Path, mutate) -> None:
    raw = source.read_bytes()
    json_length = struct.unpack_from("<I", raw, 12)[0]
    document = json.loads(raw[20 : 20 + json_length].decode("utf-8").rstrip())
    bin_offset = 20 + json_length + 8
    binary = raw[bin_offset:]
    mutate(document)
    json_chunk = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * ((-len(json_chunk)) % 4)
    total = 12 + 8 + len(json_chunk) + 8 + len(binary)
    destination.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(json_chunk), 0x4E4F534A)
        + json_chunk
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )


@pytest.fixture
def blender_available() -> bool:
    detect = BlenderAdapter().detect_tool()
    return detect.available


def test_blender_process_asset_end_to_end(tmp_path: Path, blender_available: bool) -> None:
    if not blender_available:
        pytest.skip("Blender executable not available on host")

    raw_glb = tmp_path / "raw.glb"
    # Create raw GLB with dimensions 2.0 x 1.5 x 1.8 (different from target 1.2 x 1.0 x 1.0)
    create_box_glb(width_m=2.0, depth_m=1.5, height_m=1.8, output_path=raw_glb)
    raw_size_before = raw_glb.stat().st_size
    raw_bytes_before = raw_glb.read_bytes()

    spec_path = Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
    spec = parse_asset_specification(spec_path)

    processed_glb = tmp_path / "processed.glb"
    report_file = tmp_path / "report.json"

    processor = BlenderAssetProcessor()
    result = processor.process_asset(
        raw_glb_path=raw_glb,
        processed_glb_path=processed_glb,
        spec=spec,
        report_path=report_file,
        timeout_seconds=45.0,
    )

    assert result.status == "SUCCESS"
    assert result.exit_code == 0
    assert processed_glb.is_file()
    assert processed_glb.stat().st_size > 0
    assert report_file.is_file()

    # Invariant: Raw GLB must remain byte-for-byte immutable
    assert raw_glb.read_bytes() == raw_bytes_before
    assert raw_glb.stat().st_size == raw_size_before

    # Verify report metrics
    report = result.report_data
    assert "processed_metrics" in report
    metrics = report["processed_metrics"]
    assert "lod0_triangles" in metrics
    assert "lod1_triangles" in metrics
    assert "collider_triangles" in metrics
    assert metrics["lod1_triangles"] <= metrics["lod0_triangles"]
    assert "COL_prop_energy_crate_01" in metrics.get("nodes", [])
    validation = validate_glb(processed_glb, spec)
    assert validation.status.value == "PASS", validation.to_dict()
    assert result.raw_glb_sha256 != result.processed_glb_sha256
    assert len(result.script_sha256) == 64

    # Mutate actual Blender output copies: geometry-derived rules must catch both.
    no_collider = tmp_path / "mutated-no-collider.glb"

    def remove_collider(document) -> None:
        collider = next(n for n in document["nodes"] if n["name"] == f"COL_{spec.asset_id}")
        mesh_id = collider["mesh"]
        node_id = document["nodes"].index(collider)
        document["nodes"].pop(node_id)
        document["scenes"][0]["nodes"].remove(node_id)
        for root_index, value in enumerate(document["scenes"][0]["nodes"]):
            if value > node_id:
                document["scenes"][0]["nodes"][root_index] -= 1
        document["meshes"].pop(mesh_id)
        for node in document["nodes"]:
            if node.get("mesh", len(document["meshes"])) > mesh_id:
                node["mesh"] -= 1

    _rewrite_glb(processed_glb, no_collider, remove_collider)
    no_collider_rules = {
        f.rule_id: f.severity.value for f in validate_glb(no_collider, spec).findings
    }
    assert no_collider_rules["collider.geometry"] == "FAIL"

    oversized = tmp_path / "mutated-scale-x10.glb"

    def scale_visual(document) -> None:
        node = next(n for n in document["nodes"] if n["name"] == f"SM_{spec.asset_id}_LOD0")
        node["scale"] = [10.0, 10.0, 10.0]

    _rewrite_glb(processed_glb, oversized, scale_visual)
    oversized_rules = {f.rule_id: f.severity.value for f in validate_glb(oversized, spec).findings}
    assert oversized_rules["scale.bounds"] == "FAIL"


def test_blender_process_nonexistent_raw_raises(tmp_path: Path, blender_available: bool) -> None:
    if not blender_available:
        pytest.skip("Blender executable not available on host")

    processor = BlenderAssetProcessor()
    spec = parse_asset_specification(
        Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
    )
    with pytest.raises(DccFailedError, match="Input raw GLB does not exist"):
        processor.process_asset(
            raw_glb_path=tmp_path / "missing.glb",
            processed_glb_path=tmp_path / "out.glb",
            spec=spec,
        )


def test_real_blender_glb_axis_mapping_with_unequal_height_and_depth(tmp_path: Path) -> None:
    detection = BlenderAdapter().detect_tool()
    if not detection.available or not detection.executable_path:
        pytest.skip("Blender executable is unavailable")
    base = parse_asset_specification(
        Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
    )
    values = base.model_dump(mode="python")
    values["dimensions"] = {"width_m": 1.2, "depth_m": 0.8, "height_m": 1.6}
    spec = parse_asset_specification(values)
    raw = tmp_path / "axis-probe-raw.glb"
    processed = tmp_path / "axis-probe-processed.glb"
    create_box_glb(
        width_m=2.0,
        depth_m=1.7,
        height_m=2.4,
        mesh_name=f"SM_{spec.asset_id}",
        collider_name=f"COL_{spec.asset_id}",
        output_path=raw,
    )
    result = BlenderAssetProcessor(detection.executable_path).process_asset(raw, processed, spec)
    validation = validate_glb(processed, spec)
    assert result.exit_code == 0
    assert validation.status.value == "PASS", validation.to_dict()
    dimensions = json.loads(
        next(f for f in validation.findings if f.rule_id == "scale.bounds").actual
    )
    assert all(
        abs(actual - expected) <= 0.01
        for actual, expected in zip(dimensions, [1.2, 1.6, 0.8], strict=True)
    )
