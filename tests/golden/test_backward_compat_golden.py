"""Backward-compatibility golden tests for V0.4-V0.6 behavior.

Validates that current observable behavior matches the frozen golden data:
- GLB validation findings order and content across static_prop, pickup, and modular_piece
- Specification fingerprints and model_dump for 0.4.0 and 0.5.0
- Profile processing, scene, runtime, and capture request contracts
- Built-in registry availability and profile models

To regenerate goldens (never run during automated test suite):
    .venv/Scripts/python.exe tests/golden/generate_goldens.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.assets.glb_validator import validate_glb
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.core.domain.asset_contracts import (
    AssetSpecification,
    parse_asset_specification,
    spec_fingerprint,
)
from gamefactory.core.domain.asset_profiles import (
    AssetProfile,
    builtin_registry,
)
from tests.golden.generate_goldens import (
    _document,
    _write_glb,
    normalize_validation_result,
)

GOLDEN_FILE = Path(__file__).parent / "golden_data.json"


@pytest.fixture(scope="module")
def golden_data() -> dict[str, Any]:
    assert GOLDEN_FILE.is_file(), f"Golden file not found: {GOLDEN_FILE}"
    return json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))


_HASH_PLACEHOLDER = "<SHA256-OF-ARTIFACT>"


def _mask_artifact_hash(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mask the ``glb.hash`` value; everything else, including order, is compared."""
    return [
        {**f, "actual": _HASH_PLACEHOLDER} if f["rule_id"] == "glb.hash" else f for f in findings
    ]


def _assert_matches_golden(result: Any, glb_path: Path, expected: dict[str, Any]) -> None:
    """Compare validator output with the frozen golden.

    The fixture GLBs embed a PNG texture encoded by Pillow, whose zlib output is
    not byte-stable across platforms and library builds. The ``glb.hash`` value is
    therefore checked against the SHA-256 of the file actually validated, and then
    masked. Rule ids, severities, messages, expected/actual values and their order
    are still compared exactly.
    """
    normalized = normalize_validation_result(result)
    hashes = [f for f in normalized["findings"] if f["rule_id"] == "glb.hash"]
    for finding in hashes:
        assert finding["actual"] == hashlib.sha256(glb_path.read_bytes()).hexdigest()
    assert normalized["status"] == expected["status"]
    assert normalized["summary"] == expected["summary"]
    assert _mask_artifact_hash(normalized["findings"]) == _mask_artifact_hash(expected["findings"])


# --- Helper functions to create GLB test fixtures matching generate_goldens.py ---


def _create_static_prop_fixture(case_name: str, work_dir: Path, spec: AssetSpecification) -> Path:
    base_path = work_dir / f"sp_{case_name}.glb"
    if case_name == "pass":
        create_box_glb(
            width_m=spec.dimensions.width_m,
            depth_m=spec.dimensions.depth_m,
            height_m=spec.dimensions.height_m,
            mesh_name=f"SM_{spec.asset_id}",
            collider_name=f"COL_{spec.asset_id}",
            include_lod1=True,
            include_collider=True,
            include_texture=True,
            origin=spec.origin_policy,
            output_path=base_path,
        )
    elif case_name == "missing_lod1":
        create_box_glb(
            width_m=spec.dimensions.width_m,
            depth_m=spec.dimensions.depth_m,
            height_m=spec.dimensions.height_m,
            mesh_name=f"SM_{spec.asset_id}",
            collider_name=f"COL_{spec.asset_id}",
            include_lod1=False,
            include_collider=True,
            include_texture=True,
            origin=spec.origin_policy,
            output_path=base_path,
        )
    elif case_name == "collider_mismatch":
        create_box_glb(
            width_m=spec.dimensions.width_m,
            depth_m=spec.dimensions.depth_m,
            height_m=spec.dimensions.height_m,
            mesh_name=f"SM_{spec.asset_id}",
            collider_name=f"COL_{spec.asset_id}",
            include_lod1=True,
            include_collider=False,
            include_texture=True,
            origin=spec.origin_policy,
            output_path=base_path,
        )
    elif case_name == "non_identity_transform":
        temp_pass = work_dir / "temp_sp_pass.glb"
        create_box_glb(
            width_m=spec.dimensions.width_m,
            depth_m=spec.dimensions.depth_m,
            height_m=spec.dimensions.height_m,
            mesh_name=f"SM_{spec.asset_id}",
            collider_name=f"COL_{spec.asset_id}",
            include_lod1=True,
            include_collider=True,
            include_texture=True,
            origin=spec.origin_policy,
            output_path=temp_pass,
        )
        doc, binary = _document(temp_pass.read_bytes())
        doc["nodes"][0]["translation"] = [0.0, 1.0, 0.0]
        _write_glb(base_path, doc, binary)
    elif case_name == "wrong_origin":
        create_box_glb(
            width_m=spec.dimensions.width_m,
            depth_m=spec.dimensions.depth_m,
            height_m=spec.dimensions.height_m,
            mesh_name=f"SM_{spec.asset_id}",
            collider_name=f"COL_{spec.asset_id}",
            include_lod1=True,
            include_collider=True,
            include_texture=True,
            origin="center",
            output_path=base_path,
        )
    elif case_name == "skin_present":
        temp_pass = work_dir / "temp_sp_pass.glb"
        if not temp_pass.is_file():
            _create_static_prop_fixture("pass", work_dir, spec)
            temp_pass = work_dir / "sp_pass.glb"
        doc, binary = _document(temp_pass.read_bytes())
        doc["skins"] = [{"joints": [0]}]
        _write_glb(base_path, doc, binary)
    elif case_name == "animation_present":
        temp_pass = work_dir / "temp_sp_pass.glb"
        if not temp_pass.is_file():
            _create_static_prop_fixture("pass", work_dir, spec)
            temp_pass = work_dir / "sp_pass.glb"
        doc, binary = _document(temp_pass.read_bytes())
        doc["animations"] = [{"channels": [], "samplers": []}]
        _write_glb(base_path, doc, binary)
    elif case_name == "over_budget":
        create_box_glb(
            width_m=spec.dimensions.width_m,
            depth_m=spec.dimensions.depth_m,
            height_m=spec.dimensions.height_m,
            mesh_name=f"SM_{spec.asset_id}",
            collider_name=f"COL_{spec.asset_id}",
            include_lod1=True,
            include_collider=True,
            num_materials=3,
            include_texture=True,
            texture_size=(4096, 4096),
            origin=spec.origin_policy,
            output_path=base_path,
        )
    else:
        raise ValueError(f"Unknown static_prop case: {case_name}")
    return base_path


def _create_pickup_fixture(case_name: str, work_dir: Path, spec: AssetSpecification) -> Path:
    base_path = work_dir / f"pu_{case_name}.glb"
    if case_name == "pass":
        create_box_glb(
            width_m=spec.dimensions.width_m,
            depth_m=spec.dimensions.depth_m,
            height_m=spec.dimensions.height_m,
            mesh_name=f"SM_{spec.asset_id}",
            collider_name=f"COL_{spec.asset_id}",
            include_lod1=False,
            include_collider=True,
            include_texture=True,
            origin=spec.origin_policy,
            output_path=base_path,
        )
    elif case_name == "collider_mismatch":
        create_box_glb(
            width_m=spec.dimensions.width_m,
            depth_m=spec.dimensions.depth_m,
            height_m=spec.dimensions.height_m,
            mesh_name=f"SM_{spec.asset_id}",
            collider_name=f"COL_{spec.asset_id}",
            include_lod1=False,
            include_collider=False,
            include_texture=True,
            origin=spec.origin_policy,
            output_path=base_path,
        )
    elif case_name == "non_identity_transform":
        temp_pass = work_dir / "temp_pu_pass.glb"
        create_box_glb(
            width_m=spec.dimensions.width_m,
            depth_m=spec.dimensions.depth_m,
            height_m=spec.dimensions.height_m,
            mesh_name=f"SM_{spec.asset_id}",
            collider_name=f"COL_{spec.asset_id}",
            include_lod1=False,
            include_collider=True,
            include_texture=True,
            origin=spec.origin_policy,
            output_path=temp_pass,
        )
        doc, binary = _document(temp_pass.read_bytes())
        doc["nodes"][0]["translation"] = [0.0, 1.0, 0.0]
        _write_glb(base_path, doc, binary)
    elif case_name == "wrong_origin":
        create_box_glb(
            width_m=spec.dimensions.width_m,
            depth_m=spec.dimensions.depth_m,
            height_m=spec.dimensions.height_m,
            mesh_name=f"SM_{spec.asset_id}",
            collider_name=f"COL_{spec.asset_id}",
            include_lod1=False,
            include_collider=True,
            include_texture=True,
            origin="bottom_center",
            output_path=base_path,
        )
    elif case_name == "unexpected_lod1":
        create_box_glb(
            width_m=spec.dimensions.width_m,
            depth_m=spec.dimensions.depth_m,
            height_m=spec.dimensions.height_m,
            mesh_name=f"SM_{spec.asset_id}",
            collider_name=f"COL_{spec.asset_id}",
            include_lod1=True,
            include_collider=True,
            include_texture=True,
            origin=spec.origin_policy,
            output_path=base_path,
        )
    elif case_name == "skin_present":
        temp_pass = work_dir / "temp_pu_pass.glb"
        if not temp_pass.is_file():
            _create_pickup_fixture("pass", work_dir, spec)
            temp_pass = work_dir / "pu_pass.glb"
        doc, binary = _document(temp_pass.read_bytes())
        doc["skins"] = [{"joints": [0]}]
        _write_glb(base_path, doc, binary)
    elif case_name == "animation_present":
        temp_pass = work_dir / "temp_pu_pass.glb"
        if not temp_pass.is_file():
            _create_pickup_fixture("pass", work_dir, spec)
            temp_pass = work_dir / "pu_pass.glb"
        doc, binary = _document(temp_pass.read_bytes())
        doc["animations"] = [{"channels": [], "samplers": []}]
        _write_glb(base_path, doc, binary)
    elif case_name == "over_budget":
        create_box_glb(
            width_m=spec.dimensions.width_m,
            depth_m=spec.dimensions.depth_m,
            height_m=spec.dimensions.height_m,
            mesh_name=f"SM_{spec.asset_id}",
            collider_name=f"COL_{spec.asset_id}",
            include_lod1=False,
            include_collider=True,
            num_materials=3,
            include_texture=True,
            texture_size=(4096, 4096),
            origin=spec.origin_policy,
            output_path=base_path,
        )
    else:
        raise ValueError(f"Unknown pickup case: {case_name}")
    return base_path


def _create_modular_fixture(case_name: str, work_dir: Path, spec: AssetSpecification) -> Path:
    base_path = work_dir / f"mod_{case_name}.glb"
    if case_name == "pass":
        create_box_glb(
            width_m=spec.dimensions.width_m,
            depth_m=spec.dimensions.depth_m,
            height_m=spec.dimensions.height_m,
            mesh_name=f"SM_{spec.asset_id}",
            collider_name=f"COL_{spec.asset_id}",
            include_lod1=False,
            include_collider=True,
            include_texture=True,
            origin=spec.origin_policy,
            output_path=base_path,
        )
    elif case_name == "off_grid_dimensions":
        create_box_glb(
            width_m=1.1,
            depth_m=spec.dimensions.depth_m,
            height_m=spec.dimensions.height_m,
            mesh_name=f"SM_{spec.asset_id}",
            collider_name=f"COL_{spec.asset_id}",
            include_lod1=False,
            include_collider=True,
            include_texture=True,
            origin=spec.origin_policy,
            output_path=base_path,
        )
    elif case_name == "collider_mismatch":
        create_box_glb(
            width_m=spec.dimensions.width_m,
            depth_m=spec.dimensions.depth_m,
            height_m=spec.dimensions.height_m,
            mesh_name=f"SM_{spec.asset_id}",
            collider_name=f"COL_{spec.asset_id}",
            include_lod1=False,
            include_collider=False,
            include_texture=True,
            origin=spec.origin_policy,
            output_path=base_path,
        )
    elif case_name == "non_identity_transform":
        temp_pass = work_dir / "temp_mod_pass.glb"
        create_box_glb(
            width_m=spec.dimensions.width_m,
            depth_m=spec.dimensions.depth_m,
            height_m=spec.dimensions.height_m,
            mesh_name=f"SM_{spec.asset_id}",
            collider_name=f"COL_{spec.asset_id}",
            include_lod1=False,
            include_collider=True,
            include_texture=True,
            origin=spec.origin_policy,
            output_path=temp_pass,
        )
        doc, binary = _document(temp_pass.read_bytes())
        doc["nodes"][0]["translation"] = [0.0, 1.0, 0.0]
        _write_glb(base_path, doc, binary)
    elif case_name == "wrong_origin":
        create_box_glb(
            width_m=spec.dimensions.width_m,
            depth_m=spec.dimensions.depth_m,
            height_m=spec.dimensions.height_m,
            mesh_name=f"SM_{spec.asset_id}",
            collider_name=f"COL_{spec.asset_id}",
            include_lod1=False,
            include_collider=True,
            include_texture=True,
            origin="center",
            output_path=base_path,
        )
    elif case_name == "skin_present":
        temp_pass = work_dir / "temp_mod_pass.glb"
        if not temp_pass.is_file():
            _create_modular_fixture("pass", work_dir, spec)
            temp_pass = work_dir / "mod_pass.glb"
        doc, binary = _document(temp_pass.read_bytes())
        doc["skins"] = [{"joints": [0]}]
        _write_glb(base_path, doc, binary)
    elif case_name == "animation_present":
        temp_pass = work_dir / "temp_mod_pass.glb"
        if not temp_pass.is_file():
            _create_modular_fixture("pass", work_dir, spec)
            temp_pass = work_dir / "mod_pass.glb"
        doc, binary = _document(temp_pass.read_bytes())
        doc["animations"] = [{"channels": [], "samplers": []}]
        _write_glb(base_path, doc, binary)
    elif case_name == "over_budget":
        create_box_glb(
            width_m=spec.dimensions.width_m,
            depth_m=spec.dimensions.depth_m,
            height_m=spec.dimensions.height_m,
            mesh_name=f"SM_{spec.asset_id}",
            collider_name=f"COL_{spec.asset_id}",
            include_lod1=False,
            include_collider=True,
            num_materials=5,
            include_texture=True,
            texture_size=(64, 64),
            origin=spec.origin_policy,
            output_path=base_path,
        )
    else:
        raise ValueError(f"Unknown modular_piece case: {case_name}")
    return base_path


# --- Golden tests ---


STATIC_PROP_CASES = [
    "pass",
    "missing_lod1",
    "collider_mismatch",
    "non_identity_transform",
    "wrong_origin",
    "skin_present",
    "animation_present",
    "over_budget",
]

PICKUP_CASES = [
    "pass",
    "collider_mismatch",
    "non_identity_transform",
    "wrong_origin",
    "unexpected_lod1",
    "skin_present",
    "animation_present",
    "over_budget",
]

MODULAR_CASES = [
    "pass",
    "off_grid_dimensions",
    "collider_mismatch",
    "non_identity_transform",
    "wrong_origin",
    "skin_present",
    "animation_present",
    "over_budget",
]


@pytest.mark.parametrize("case_name", STATIC_PROP_CASES)
def test_static_prop_glb_validation_golden(
    case_name: str, tmp_path: Path, golden_data: dict[str, Any]
) -> None:
    spec = parse_asset_specification(
        Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
    )
    glb_path = _create_static_prop_fixture(case_name, tmp_path, spec)
    result = validate_glb(glb_path, spec)
    _assert_matches_golden(
        result, glb_path, golden_data["glb_validation"]["static_prop"][case_name]
    )


@pytest.mark.parametrize("case_name", PICKUP_CASES)
def test_pickup_glb_validation_golden(
    case_name: str, tmp_path: Path, golden_data: dict[str, Any]
) -> None:
    spec = parse_asset_specification(
        Path("src/gamefactory/resources/specs/pickup_energy_cell_01.yml")
    )
    glb_path = _create_pickup_fixture(case_name, tmp_path, spec)
    result = validate_glb(glb_path, spec)
    _assert_matches_golden(result, glb_path, golden_data["glb_validation"]["pickup"][case_name])


@pytest.mark.parametrize("case_name", MODULAR_CASES)
def test_modular_piece_glb_validation_golden(
    case_name: str, tmp_path: Path, golden_data: dict[str, Any]
) -> None:
    spec = parse_asset_specification(Path("src/gamefactory/resources/fixtures/wall_panel_test.yml"))
    glb_path = _create_modular_fixture(case_name, tmp_path, spec)
    result = validate_glb(glb_path, spec)
    _assert_matches_golden(
        result, glb_path, golden_data["glb_validation"]["modular_piece"][case_name]
    )


@pytest.mark.parametrize(
    "doc_name",
    [
        "prop_energy_crate_01_v040",
        "prop_energy_crate_01_v050",
        "pickup_energy_cell_01_v050",
        "energy_pickup_test_v050",
        "wall_panel_test_v050",
    ],
)
def test_spec_fingerprints_and_model_dump_golden(
    doc_name: str, golden_data: dict[str, Any]
) -> None:
    expected = golden_data["spec_documents"][doc_name]
    if doc_name == "prop_energy_crate_01_v040":
        spec = parse_asset_specification(
            Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
        )
    elif doc_name == "prop_energy_crate_01_v050":
        sp_040 = parse_asset_specification(
            Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
        )
        d = sp_040.model_dump(mode="json")
        d["schema_version"] = "0.5.0"
        d["profile_version"] = 1
        spec = parse_asset_specification(d)
    elif doc_name == "pickup_energy_cell_01_v050":
        spec = parse_asset_specification(
            Path("src/gamefactory/resources/specs/pickup_energy_cell_01.yml")
        )
    elif doc_name == "energy_pickup_test_v050":
        spec = parse_asset_specification(
            Path("src/gamefactory/resources/fixtures/energy_pickup_test.yml")
        )
    elif doc_name == "wall_panel_test_v050":
        spec = parse_asset_specification(
            Path("src/gamefactory/resources/fixtures/wall_panel_test.yml")
        )
    else:
        raise ValueError(f"Unknown doc: {doc_name}")

    assert spec_fingerprint(spec) == expected["fingerprint"]
    assert spec.model_dump(mode="json") == expected["model_dump"]


@pytest.mark.parametrize("profile_id", ["static_prop", "pickup", "modular_piece"])
def test_profile_contracts_golden(profile_id: str, golden_data: dict[str, Any]) -> None:
    expected = golden_data["profile_contracts"][profile_id]
    reg = builtin_registry()
    prof: AssetProfile = reg.get(profile_id, 1)

    if profile_id == "static_prop":
        spec = parse_asset_specification(
            Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
        )
    elif profile_id == "pickup":
        spec = parse_asset_specification(
            Path("src/gamefactory/resources/specs/pickup_energy_cell_01.yml")
        )
    elif profile_id == "modular_piece":
        spec = parse_asset_specification(
            Path("src/gamefactory/resources/fixtures/wall_panel_test.yml")
        )
    else:
        raise ValueError(f"Unknown profile: {profile_id}")

    assert prof.processing_contract(spec) == expected["processing_contract"]
    assert prof.scene_contract() == expected["scene_contract"]
    assert prof.runtime_requirements() == expected["runtime_requirements"]
    assert prof.capture_request_profile() == expected["capture_request_profile"]


def test_registry_availability_and_parsed_profiles_golden(
    golden_data: dict[str, Any],
) -> None:
    expected = golden_data["registry"]
    reg = builtin_registry()

    assert reg.availability() == expected["availability"]
    for prof in reg.available:
        assert prof.document.model_dump(mode="json") == expected["parsed_profiles"][prof.profile_id]
