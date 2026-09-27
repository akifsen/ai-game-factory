"""Profile-specific validation failures stay on the bound profile contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gamefactory.adapters.assets.glb_validator import validate_glb
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.core.domain.asset_contracts import parse_asset_specification
from gamefactory.core.domain.production_receipt import read_production_receipt

FIXTURES = Path("src/gamefactory/resources/fixtures")


def _spec(name: str):
    return parse_asset_specification(FIXTURES / name)


def _write(path: Path, spec, **kwargs) -> None:
    create_box_glb(
        width_m=spec.dimensions.width_m,
        depth_m=spec.dimensions.depth_m,
        height_m=spec.dimensions.height_m,
        mesh_name=f"SM_{spec.asset_id}",
        collider_name=f"COL_{spec.asset_id}",
        origin=spec.origin_policy,
        output_path=path,
        **kwargs,
    )


def _rules(path: Path, spec) -> dict[str, str]:
    return {
        finding.rule_id: finding.severity.value for finding in validate_glb(path, spec).findings
    }


def test_static_prop_missing_collider_fails(tmp_path: Path) -> None:
    spec = parse_asset_specification(
        Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
    )
    path = tmp_path / "static.glb"
    _write(path, spec, include_lod1=True, include_collider=False)
    assert _rules(path, spec)["collider.geometry"] == "FAIL"


def test_pickup_missing_collider_fails_and_lod1_is_optional(tmp_path: Path) -> None:
    spec = _spec("energy_pickup_test.yml")
    path = tmp_path / "pickup.glb"
    _write(path, spec, include_lod1=False, include_collider=True)
    result = validate_glb(path, spec)
    assert result.status.value == "PASS", result.to_dict()

    missing = tmp_path / "pickup-missing.glb"
    _write(missing, spec, include_lod1=False, include_collider=False)
    assert _rules(missing, spec)["collider.geometry"] == "FAIL"


def test_modular_off_grid_dimensions_fail(tmp_path: Path) -> None:
    spec = _spec("wall_panel_test.yml")
    path = tmp_path / "panel.glb"
    create_box_glb(
        width_m=1.1,
        depth_m=0.25,
        height_m=2.0,
        mesh_name=f"SM_{spec.asset_id}",
        collider_name=f"COL_{spec.asset_id}",
        origin="bottom_center",
        include_collider=True,
        output_path=path,
    )
    rules = _rules(path, spec)
    assert rules["scale.bounds"] == "FAIL"
    assert rules["dimensions.snap"] == "FAIL"


def test_common_budget_and_invalid_glb_and_wrong_profile_names(tmp_path: Path) -> None:
    spec = _spec("energy_pickup_test.yml")
    path = tmp_path / "budget.glb"
    _write(
        path,
        spec,
        include_collider=True,
        include_lod1=False,
        num_materials=3,
        include_texture=True,
        texture_size=(4096, 4096),
    )
    rules = _rules(path, spec)
    assert rules["materials.budget"] == "FAIL"
    assert rules["textures.dimension"] == "FAIL"

    broken = tmp_path / "broken.glb"
    _write(broken, spec, include_collider=True)
    broken.write_bytes(broken.read_bytes()[:24])
    assert validate_glb(broken, spec).status.value == "FAIL"

    other = _spec("wall_panel_test.yml")
    assert _rules(path, other)["nodes.profile"] == "FAIL"


def test_v04_manifest_reads_as_static_prop_without_rewriting(tmp_path: Path) -> None:
    manifest = {
        "schema_version": "asset-evidence-0.4.0",
        "asset_id": "prop_energy_crate_01",
        "revision": 1,
        "specification_fingerprint": "a" * 64,
        "processed_glb_sha256": "b" * 64,
        "files": [
            {"role": "concept", "sha256": "c" * 64},
            {"role": "raw_glb", "sha256": "d" * 64},
            {"role": "processed_glb", "sha256": "b" * 64},
            {"role": "validation", "sha256": "e" * 64},
            {"role": "runtime_observation", "sha256": "f" * 64},
            {"role": "runtime_capture", "angle": "front", "sha256": "1" * 64},
            {"role": "runtime_capture", "angle": "side", "sha256": "2" * 64},
        ],
        "final_review": {"fingerprint": "g" * 64},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    before = path.read_bytes()
    receipt = read_production_receipt(path)
    assert path.read_bytes() == before
    assert receipt["profile_qualified"] == "static_prop@1"
    assert receipt["processed_artifact_hash"] == "b" * 64
    assert receipt["render_hashes"]["front"] == "1" * 64
    assert receipt["historical"] is True


def test_receipt_rejects_unknown_schema() -> None:
    from gamefactory.core.domain.errors import ValidationError

    with pytest.raises(ValidationError, match="unsupported"):
        read_production_receipt({"schema_version": "production-receipt-9"})
