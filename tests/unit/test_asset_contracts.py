"""Unit tests for V0.4 asset specification and domain contracts."""

import json
from pathlib import Path

import pytest
import yaml

from gamefactory.core.domain.asset_contracts import (
    AssetRevision,
    AssetSpecification,
    CostRecord,
    parse_asset_specification,
    spec_fingerprint,
)
from gamefactory.core.domain.errors import SpecInvalidError

SPEC_SCHEMA = Path("src/gamefactory/schemas/asset-spec-0.4.0.schema.json")


@pytest.fixture
def valid_spec_dict() -> dict:
    return {
        "schema_version": "0.4.0",
        "asset_id": "prop_energy_crate_01",
        "category": "prop",
        "profile": "static_prop",
        "intent": "Stylized Sci-Fi Energy Crate for in-game prop placement",
        "dimensions": {
            "width_m": 1.2,
            "depth_m": 1.0,
            "height_m": 1.0,
        },
        "orientation": {
            "up": "+Y",
            "front": "-Z",
        },
        "origin_policy": "bottom_center",
        "geometry_budget": {
            "max_triangles_lod0": 20000,
            "max_triangles_lod1": 10000,
            "lod_ratio": 0.5,
        },
        "material_budget": {
            "max_materials": 2,
        },
        "texture_budget": {
            "max_dimension": 2048,
        },
        "collider_policy": "box",
        "lod_policy": "lod0_lod1",
        "style_constraints": {
            "family": "stylized_scifi",
            "silhouette": "chunky",
            "readability": "high",
            "detail_density": "medium",
        },
        "target_engine": "godot",
        "target_import_path": "assets/generated/props/prop_energy_crate_01/",
    }


def test_valid_spec_parsing(valid_spec_dict: dict) -> None:
    spec = parse_asset_specification(valid_spec_dict)
    assert isinstance(spec, AssetSpecification)
    assert spec.asset_id == "prop_energy_crate_01"
    assert spec.dimensions.width_m == 1.2
    assert spec.geometry_budget.max_triangles_lod0 == 20000
    assert spec.geometry_budget.max_triangles_lod1 == 10000
    assert spec.geometry_budget.lod_ratio == 0.5
    fp1 = spec_fingerprint(spec)
    assert len(fp1) == 64
    fp2 = spec_fingerprint(spec)
    assert fp1 == fp2


def test_canonical_spec_file_exists_and_valid() -> None:
    canonical_path = Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
    assert canonical_path.is_file()
    spec = parse_asset_specification(canonical_path)
    assert spec.asset_id == "prop_energy_crate_01"
    assert spec.profile == "static_prop"
    assert spec.orientation.up == "+Y"
    assert spec.orientation.front == "-Z"


def test_reject_bool_coercion(valid_spec_dict: dict) -> None:
    # Booleans must NOT be coerced to 1.0 or 0.0
    valid_spec_dict["dimensions"]["width_m"] = True
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)

    valid_spec_dict["dimensions"]["width_m"] = 1.2
    valid_spec_dict["geometry_budget"]["max_triangles_lod0"] = True
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)


def test_reject_numeric_string_coercion(valid_spec_dict: dict) -> None:
    valid_spec_dict["dimensions"]["width_m"] = "1.2"
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)

    valid_spec_dict["dimensions"]["width_m"] = 1.2
    valid_spec_dict["geometry_budget"]["max_triangles_lod0"] = "20000"
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)


def test_reject_too_small_dimension(valid_spec_dict: dict) -> None:
    valid_spec_dict["dimensions"]["width_m"] = 0.000001
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)


def test_dimensions_preserve_user_precision_and_boundaries(valid_spec_dict: dict) -> None:
    valid_spec_dict["dimensions"]["width_m"] = 1.23456789
    parsed = parse_asset_specification(valid_spec_dict)
    assert parsed.dimensions.width_m == 1.23456789

    valid_spec_dict["dimensions"]["width_m"] = 0.001
    assert parse_asset_specification(valid_spec_dict).dimensions.width_m == 0.001
    valid_spec_dict["dimensions"]["width_m"] = 100.0
    assert parse_asset_specification(valid_spec_dict).dimensions.width_m == 100.0


def test_reject_duplicate_yaml_keys() -> None:
    yaml_with_dups = """
schema_version: "0.4.0"
asset_id: "prop_energy_crate_01"
asset_id: "prop_duplicate_crate"
category: "prop"
profile: "static_prop"
intent: "Test crate"
dimensions:
  width_m: 1.0
  depth_m: 1.0
  height_m: 1.0
"""
    with pytest.raises(SpecInvalidError, match="Duplicate YAML key 'asset_id' detected"):
        parse_asset_specification(yaml_with_dups)


def test_reject_windows_ads_in_import_path(valid_spec_dict: dict) -> None:
    valid_spec_dict["target_import_path"] = "assets/x:stream"
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)


def test_reject_windows_device_in_import_path(valid_spec_dict: dict) -> None:
    valid_spec_dict["target_import_path"] = "assets/CON/model.glb"
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)

    valid_spec_dict["target_import_path"] = "assets/aux/prop.glb"
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)


def test_reject_unsupported_orientation(valid_spec_dict: dict) -> None:
    # Collinear axes or unsupported orientation
    valid_spec_dict["orientation"] = {"up": "+Y", "front": "-Y"}
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)

    valid_spec_dict["orientation"] = {"up": "+Z", "front": "-Z"}
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)


def test_lod_ratio_editable_not_rigid(valid_spec_dict: dict) -> None:
    # Runtime supports values beyond the recommendation while retaining processor bounds.
    valid_spec_dict["geometry_budget"]["lod_ratio"] = 0.35
    spec1 = parse_asset_specification(valid_spec_dict)
    assert spec1.geometry_budget.lod_ratio == 0.35

    valid_spec_dict["geometry_budget"]["lod_ratio"] = 0.70
    spec2 = parse_asset_specification(valid_spec_dict)
    assert spec2.geometry_budget.lod_ratio == 0.70

    valid_spec_dict["geometry_budget"]["lod_ratio"] = 0.05
    assert parse_asset_specification(valid_spec_dict).geometry_budget.lod_ratio == 0.05
    valid_spec_dict["geometry_budget"]["lod_ratio"] = 0.95
    assert parse_asset_specification(valid_spec_dict).geometry_budget.lod_ratio == 0.95


@pytest.mark.parametrize("ratio", [0.049, 0.951])
def test_reject_unsupported_lod_ratio(valid_spec_dict: dict, ratio: float) -> None:
    valid_spec_dict["geometry_budget"]["lod_ratio"] = ratio
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)


def test_reject_unknown_field(valid_spec_dict: dict) -> None:
    valid_spec_dict["unexpected_field"] = "malicious_or_unknown"
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [("collider_policy", "convex"), ("collider_policy", "primitive"), ("lod_policy", "lod0_only")],
)
def test_reject_unsupported_v04_profile_policies(
    valid_spec_dict: dict, field_name: str, value: str
) -> None:
    valid_spec_dict[field_name] = value
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)


def test_exported_schema_matches_dimension_ranges_and_supported_policies() -> None:
    schema = json.loads(SPEC_SCHEMA.read_text(encoding="utf-8"))
    dimension_schema = schema["$defs"]["DimensionsConfig"]["properties"]
    for dimension in ("width_m", "depth_m", "height_m"):
        assert dimension_schema[dimension]["minimum"] == 0.001
        assert dimension_schema[dimension]["maximum"] == 100.0
    assert schema["properties"]["collider_policy"]["const"] == "box"
    assert schema["properties"]["lod_policy"]["const"] == "lod0_lod1"
    lod_ratio_schema = schema["$defs"]["GeometryBudgetConfig"]["properties"]["lod_ratio"]
    assert lod_ratio_schema["minimum"] == 0.05
    assert lod_ratio_schema["maximum"] == 0.95


def test_reject_negative_dimensions(valid_spec_dict: dict) -> None:
    valid_spec_dict["dimensions"]["width_m"] = -1.5
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)


def test_reject_zero_dimensions(valid_spec_dict: dict) -> None:
    valid_spec_dict["dimensions"]["height_m"] = 0.0
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)


@pytest.mark.parametrize("bad_val", [float("nan"), float("inf"), float("-inf")])
def test_reject_non_finite_dimensions(valid_spec_dict: dict, bad_val: float) -> None:
    valid_spec_dict["dimensions"]["depth_m"] = bad_val
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)


def test_reject_excessive_dimension(valid_spec_dict: dict) -> None:
    valid_spec_dict["dimensions"]["width_m"] = 150.0  # limit is 100m
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)


def test_reject_invalid_lod_order(valid_spec_dict: dict) -> None:
    valid_spec_dict["geometry_budget"]["max_triangles_lod1"] = 25000
    valid_spec_dict["geometry_budget"]["max_triangles_lod0"] = 20000
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)


def test_reject_invalid_texture_dimension(valid_spec_dict: dict) -> None:
    valid_spec_dict["texture_budget"]["max_dimension"] = 1234
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)


def test_reject_path_traversal_in_asset_id(valid_spec_dict: dict) -> None:
    valid_spec_dict["asset_id"] = "../evil_asset"
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)


def test_reject_absolute_or_traversal_import_path(valid_spec_dict: dict) -> None:
    valid_spec_dict["target_import_path"] = "C:/Windows/System32"
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)


@pytest.mark.parametrize("unsafe_path", ["assets/prop /mesh.glb", "assets/prop/ "])
def test_reject_path_with_trailing_spaces_before_normalization(
    valid_spec_dict: dict, unsafe_path: str
) -> None:
    valid_spec_dict["target_import_path"] = unsafe_path
    with pytest.raises(SpecInvalidError, match="trailing|space|path safety"):
        parse_asset_specification(valid_spec_dict)


def test_reject_asset_id_with_outer_whitespace(valid_spec_dict: dict) -> None:
    valid_spec_dict["asset_id"] = " prop_crate "
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)


def test_reject_import_path_with_outer_whitespace(valid_spec_dict: dict) -> None:
    valid_spec_dict["target_import_path"] = " assets/prop_crate"
    with pytest.raises(SpecInvalidError, match="whitespace"):
        parse_asset_specification(valid_spec_dict)

    valid_spec_dict["target_import_path"] = "../../../escape"
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_spec_dict)


def test_parse_yaml_string(valid_spec_dict: dict) -> None:
    yaml_text = yaml.dump(valid_spec_dict)
    spec = parse_asset_specification(yaml_text)
    assert spec.asset_id == "prop_energy_crate_01"


def test_cost_record_honest_defaults() -> None:
    rec = CostRecord(provider="meshy", operation="image-to-3d")
    assert rec.estimated_cost is None
    assert rec.actual_cost is None
    d = rec.to_dict()
    assert d["estimated_cost"] is None
    assert d["actual_cost"] is None


def test_asset_revision_lifecycle_neutrality() -> None:
    rev = AssetRevision(
        asset_id="prop_crate",
        revision_number=1,
        workflow_id="wf_123",
        spec_hash="a" * 64,
    )
    assert rev.workflow_id == "wf_123"
    assert not hasattr(rev, "status")  # Status authority removed from revision
    assert rev.revision_id == "r001"
