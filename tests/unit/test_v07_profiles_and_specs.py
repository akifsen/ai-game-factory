"""Unit tests for V0.7 profile and specification foundations (Phase B).

Covers:
- Dual-version profile schema (asset-profile-0.7.0 and asset-profile-0.5.0)
- Dual-version spec schema (asset-spec-0.7.0, asset-spec-0.5.0, asset-spec-0.4.0)
- Historical rejection of V0.7 fields across null, [], {}, and real values
- Historical dump key sets frozen and no V0.7 fields on historical models
- Geometry modes: single_mesh and assembly
- Role vocabulary, motion constraints, and required sockets
- Sockets and pivots structural and semantic validation
- Capsule collider structural rules and bounds sanity checks
- Registry injection for tests, isolation, and __post_init__ invariants
- Deterministic fingerprints and model_dump hygiene
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from gamefactory.core.domain.asset_contracts import (
    AssetSpecification,
    AssetSpecificationV07,
    CapsuleSpec,
    MotionLimits,
    PartMotionSpec,
    PartPivotSpec,
    PartSpec,
    SocketSpec,
    SpecColliderConfig,
    parse_asset_specification,
    parse_asset_specification_v07,
    spec_fingerprint,
)
from gamefactory.core.domain.asset_profiles import (
    UNSUPPORTED_PROFILE_IDS,
    UNSUPPORTED_PROFILE_IDS_V07,
    AssetProfile,
    AssetProfileV07,
    ProcessingPolicy,
    ProfileContractError,
    ProfileDocument,
    ProfileDocumentV07,
    ProfileRegistry,
    builtin_registry,
    builtin_v07_registry,
    parse_profile_document,
    parse_profile_document_v07,
)
from gamefactory.core.domain.errors import SpecInvalidError

FIXTURE_PROFILES = Path(__file__).resolve().parents[1] / "fixtures" / "profiles"
VEHICLE_PROFILE_PATH = FIXTURE_PROFILES / "vehicle_profile_070.yml"
CHARACTER_PROFILE_PATH = FIXTURE_PROFILES / "character_profile_070.yml"

MINIMAL_040: dict[str, Any] = {
    "schema_version": "0.4.0",
    "asset_id": "min_prop_040",
    "intent": "Minimal historical static prop",
    "dimensions": {"width_m": 1.0, "depth_m": 1.0, "height_m": 1.0},
}

MINIMAL_050: dict[str, Any] = {
    "schema_version": "0.5.0",
    "asset_id": "min_prop_050",
    "category": "prop",
    "profile": "static_prop",
    "profile_version": 1,
    "intent": "Minimal 0.5.0 static prop",
    "dimensions": {"width_m": 0.8, "depth_m": 0.6, "height_m": 1.4},
    "lod_policy": "lod0_lod1",
    "origin_policy": "center",
}


@pytest.fixture(scope="module")
def vehicle_profile() -> AssetProfileV07:
    assert VEHICLE_PROFILE_PATH.is_file()
    doc = parse_profile_document_v07(VEHICLE_PROFILE_PATH.read_text(encoding="utf-8"))
    return AssetProfileV07(doc)


@pytest.fixture(scope="module")
def character_profile() -> AssetProfileV07:
    assert CHARACTER_PROFILE_PATH.is_file()
    doc = parse_profile_document_v07(CHARACTER_PROFILE_PATH.read_text(encoding="utf-8"))
    return AssetProfileV07(doc)


@pytest.fixture(scope="module")
def test_registry(
    vehicle_profile: AssetProfileV07, character_profile: AssetProfileV07
) -> ProfileRegistry:
    return ProfileRegistry(
        available=(),
        unsupported=(),
        available_v07=(vehicle_profile, character_profile),
    )


@pytest.fixture
def valid_vehicle_spec_dict() -> dict[str, Any]:
    return {
        "schema_version": "0.7.0",
        "asset_id": "vehicle_tank_01",
        "category": "vehicle",
        "profile": "vehicle_test",
        "profile_version": 1,
        "intent": "Stylized tracked tank assembly with rotating turret and elevating barrel",
        "source_kind": "local_operator_assembly",
        "dimensions": {
            "width_m": 3.0,
            "depth_m": 5.0,
            "height_m": 2.5,
        },
        "orientation": {
            "up": "+Y",
            "front": "-Z",
        },
        "origin_policy": "bottom_center",
        "geometry_budget": {
            "max_triangles_lod0": 50000,
            "max_triangles_lod1": 25000,
            "lod_ratio": 0.5,
        },
        "material_budget": {
            "max_materials": 4,
        },
        "texture_budget": {
            "max_dimension": 2048,
        },
        "collider": {
            "policy": "box",
        },
        "lod_policy": "lod0_lod1",
        "style_constraints": {
            "family": "stylized_scifi",
            "silhouette": "armored",
            "readability": "high",
            "detail_density": "medium",
        },
        "target_engine": "godot",
        "target_import_path": "assets/generated/vehicles/vehicle_tank_01/",
        "parts": [
            {
                "part_id": "hull",
                "role": "hull",
                "parent": "root",
                "pivot": {
                    "position_m": [0.0, 0.0, 0.0],
                    "basis": "identity",
                    "motion": {"kind": "fixed"},
                },
            },
            {
                "part_id": "turret",
                "role": "turret",
                "parent": "hull",
                "pivot": {
                    "position_m": [0.0, 1.2, 0.5],
                    "basis": "identity",
                    "motion": {"kind": "revolute", "axis": [0.0, 1.0, 0.0]},
                },
            },
            {
                "part_id": "barrel",
                "role": "barrel",
                "parent": "turret",
                "pivot": {
                    "position_m": [0.0, 0.3, -0.8],
                    "basis": "identity",
                    "motion": {"kind": "revolute", "axis": [1.0, 0.0, 0.0]},
                },
            },
        ],
        "sockets": [
            {
                "socket_id": "muzzle",
                "parent_part": "barrel",
                "translation_m": [0.0, 0.0, -1.8],
                "rotation": "identity",
                "placement": "forward_end",
            }
        ],
    }


@pytest.fixture
def valid_character_spec_dict() -> dict[str, Any]:
    return {
        "schema_version": "0.7.0",
        "asset_id": "character_hero_01",
        "category": "character",
        "profile": "character_test",
        "profile_version": 1,
        "intent": "Hero unrigged character review model",
        "source_kind": "provider_generated",
        "dimensions": {
            "width_m": 0.6,
            "depth_m": 0.5,
            "height_m": 1.8,
        },
        "collider": {
            "policy": "capsule",
            "capsule": {
                "radius_m": 0.25,
                "height_m": 1.8,
            },
        },
        "lod_policy": "lod0_only",
    }


# ==============================================================================
# 1. Historical rejection of V0.7 fields, parsers, and profile values
# ==============================================================================


def test_040_spec_profile_version_null_behavior_pinned() -> None:
    # 0.4 spec accepts profile_version: None / null as today's behavior
    spec = parse_asset_specification(dict(MINIMAL_040, profile_version=None))
    assert spec.schema_version == "0.4.0"
    assert spec.profile_version is None

    # Non-null values are strictly rejected
    bad_val: Any
    for bad_val in (1, 2, [], {}, "1"):
        with pytest.raises(SpecInvalidError):
            parse_asset_specification(dict(MINIMAL_040, profile_version=bad_val))


@pytest.mark.parametrize("field", ["parts", "sockets", "source_kind", "collider"])
@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"part_id": "hull"},  # real-ish value
        "local_operator_assembly",
    ],
)
def test_040_spec_rejects_v07_fields(field: str, value: Any) -> None:
    doc = dict(MINIMAL_040, **{field: value})
    with pytest.raises(SpecInvalidError, match="Extra inputs are not permitted"):
        parse_asset_specification(doc)


@pytest.mark.parametrize("field", ["parts", "sockets", "source_kind", "collider", "source_front"])
@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"part_id": "hull"},
        "+Z",
    ],
)
def test_050_spec_rejects_v07_fields(field: str, value: Any) -> None:
    doc = dict(MINIMAL_050, **{field: value})
    with pytest.raises(SpecInvalidError, match="Extra inputs are not permitted"):
        parse_asset_specification(doc)


@pytest.mark.parametrize("field", ["geometry_mode", "assembly", "accepted_source_kinds"])
@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        "single_mesh",
        {"roles": ["hull"]},
    ],
)
def test_050_profile_rejects_v07_fields(field: str, value: Any) -> None:
    doc = builtin_registry().get("static_prop", 1).document.model_dump(mode="json")
    doc[field] = value
    with pytest.raises(ProfileContractError, match="Extra inputs are not permitted"):
        parse_profile_document(doc)


def test_050_profile_rejects_capsule_collider() -> None:
    doc = builtin_registry().get("static_prop", 1).document.model_dump(mode="json")
    doc["processing"]["allowed_collider_policies"] = ["capsule"]
    with pytest.raises(ProfileContractError, match="allowed_collider_policies"):
        parse_profile_document(doc)


def test_050_profile_rejects_vehicle_category() -> None:
    doc = builtin_registry().get("static_prop", 1).document.model_dump(mode="json")
    doc["categories"] = ["vehicle"]
    with pytest.raises(ProfileContractError, match="unsupported asset category"):
        parse_profile_document(doc)


def test_historical_parsers_reject_v07_docs(
    valid_vehicle_spec_dict: dict[str, Any], vehicle_profile: AssetProfileV07
) -> None:
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(valid_vehicle_spec_dict)

    vehicle_doc_dict = vehicle_profile.document.model_dump(mode="json")
    with pytest.raises(ProfileContractError, match="unsupported profile schema"):
        parse_profile_document(vehicle_doc_dict)


def test_v07_parsers_reject_historical_docs() -> None:
    with pytest.raises(
        SpecInvalidError, match="Asset specification V0.7 requires schema_version '0.7.0'"
    ):
        parse_asset_specification_v07(MINIMAL_040)

    with pytest.raises(
        SpecInvalidError, match="Asset specification V0.7 requires schema_version '0.7.0'"
    ):
        parse_asset_specification_v07(MINIMAL_050)

    doc05 = builtin_registry().get("static_prop", 1).document.model_dump(mode="json")
    with pytest.raises(ProfileContractError):
        parse_profile_document_v07(doc05)


# ==============================================================================
# 2. Historical dump key sets unchanged & model_fields purity
# ==============================================================================


def test_historical_dump_key_sets_and_model_fields_purity() -> None:
    spec040 = parse_asset_specification(MINIMAL_040)
    dump040 = spec040.model_dump()
    expected_040_keys = {
        "schema_version",
        "asset_id",
        "category",
        "profile",
        "intent",
        "dimensions",
        "orientation",
        "origin_policy",
        "geometry_budget",
        "material_budget",
        "texture_budget",
        "collider_policy",
        "lod_policy",
        "style_constraints",
        "target_engine",
        "target_import_path",
    }
    assert set(dump040.keys()) == expected_040_keys

    spec050 = parse_asset_specification(MINIMAL_050)
    dump050 = spec050.model_dump()
    expected_050_keys = expected_040_keys | {"profile_version"}
    assert set(dump050.keys()) == expected_050_keys

    # Historical model_fields must contain zero V0.7 fields
    v07_spec_field_names = {
        "parts",
        "sockets",
        "source_kind",
        "collider",
        "source_front",
    }
    for f in v07_spec_field_names:
        assert f not in AssetSpecification.model_fields

    v07_profile_field_names = {
        "geometry_mode",
        "accepted_source_kinds",
        "assembly",
    }
    for f in v07_profile_field_names:
        assert f not in ProfileDocument.model_fields

    # ProcessingPolicy must not allow capsule in historical model
    assert "capsule" not in ProcessingPolicy.model_fields["allowed_collider_policies"].annotation  # type: ignore[operator]


# ==============================================================================
# 3. V0.7 profile document schema and validations
# ==============================================================================


def test_v07_fixtures_load_and_fields_accessible(
    vehicle_profile: AssetProfileV07, character_profile: AssetProfileV07
) -> None:
    assert vehicle_profile.schema_version == "asset-profile-0.7.0"
    assert vehicle_profile.profile_id == "vehicle_test"
    assert vehicle_profile.version == 1
    assert vehicle_profile.geometry_mode == "assembly"
    assert vehicle_profile.accepted_source_kinds == ("local_operator_assembly",)
    assert vehicle_profile.assembly is not None
    assert vehicle_profile.assembly.roles == ["hull", "turret", "barrel"]
    assert vehicle_profile.assembly.required_roles == ["hull", "turret", "barrel"]
    assert vehicle_profile.assembly.socket_position_tolerance_m == 0.02

    assert character_profile.schema_version == "asset-profile-0.7.0"
    assert character_profile.profile_id == "character_test"
    assert character_profile.geometry_mode == "single_mesh"
    assert character_profile.accepted_source_kinds == ("provider_generated",)
    assert character_profile.assembly is None
    assert character_profile.document.processing.allowed_collider_policies == ["capsule"]


def test_v07_profile_geometry_mode_and_source_kind_rules(
    vehicle_profile: AssetProfileV07, character_profile: AssetProfileV07
) -> None:
    doc = vehicle_profile.document.model_dump(mode="json")
    doc["geometry_mode"] = "invalid_mode"
    with pytest.raises(ProfileContractError):
        parse_profile_document_v07(doc)

    doc = vehicle_profile.document.model_dump(mode="json")
    doc["accepted_source_kinds"] = ["provider_generated"]
    with pytest.raises(
        ProfileContractError,
        match="assembly requires accepted_source_kinds == \\['local_operator_assembly'\\]",
    ):
        parse_profile_document_v07(doc)

    doc = character_profile.document.model_dump(mode="json")
    doc["accepted_source_kinds"] = ["local_operator_assembly"]
    with pytest.raises(
        ProfileContractError,
        match="single_mesh requires accepted_source_kinds == \\['provider_generated'\\]",
    ):
        parse_profile_document_v07(doc)

    doc = vehicle_profile.document.model_dump(mode="json")
    doc["assembly"] = None
    with pytest.raises(ProfileContractError, match="assembly requires assembly contract"):
        parse_profile_document_v07(doc)

    doc = character_profile.document.model_dump(mode="json")
    doc["assembly"] = {
        "roles": ["body"],
        "required_roles": ["body"],
        "pivot_tolerance_m": 0.01,
        "basis_tolerance_deg": 1.0,
        "socket_position_tolerance_m": 0.01,
        "socket_angle_tolerance_deg": 1.0,
    }
    with pytest.raises(ProfileContractError, match="single_mesh forbids assembly contract"):
        parse_profile_document_v07(doc)


def test_v07_profile_rejects_alias_and_alternate_shapes(
    vehicle_profile: AssetProfileV07,
) -> None:
    doc = vehicle_profile.document.model_dump(mode="json")
    doc["roles"] = ["hull"]
    with pytest.raises(ProfileContractError, match="Extra inputs are not permitted"):
        parse_profile_document_v07(doc)

    doc = vehicle_profile.document.model_dump(mode="json")
    doc["assembly"]["role_motion"] = {}
    with pytest.raises(ProfileContractError, match="Extra inputs are not permitted"):
        parse_profile_document_v07(doc)

    doc = vehicle_profile.document.model_dump(mode="json")
    doc["assembly"]["tolerances"] = {"pivot": 0.01}
    with pytest.raises(ProfileContractError, match="Extra inputs are not permitted"):
        parse_profile_document_v07(doc)

    doc = vehicle_profile.document.model_dump(mode="json")
    doc["assembly"]["unknown_field"] = "bad"
    with pytest.raises(ProfileContractError, match="Extra inputs are not permitted"):
        parse_profile_document_v07(doc)


def test_v07_assembly_contract_invariants(vehicle_profile: AssetProfileV07) -> None:
    doc = vehicle_profile.document.model_dump(mode="json")
    doc["assembly"]["required_roles"] = ["hull", "nonexistent_role"]
    with pytest.raises(
        ProfileContractError,
        match="required_role 'nonexistent_role' is not in defined roles vocabulary",
    ):
        parse_profile_document_v07(doc)

    doc = vehicle_profile.document.model_dump(mode="json")
    doc["assembly"]["role_motion_constraints"]["unknown_role"] = {"kind": "fixed"}
    with pytest.raises(
        ProfileContractError,
        match="role_motion_constraint key 'unknown_role' is not in defined roles vocabulary",
    ):
        parse_profile_document_v07(doc)

    doc = vehicle_profile.document.model_dump(mode="json")
    doc["assembly"]["required_sockets"][0]["parent_role"] = "unknown_role"
    with pytest.raises(
        ProfileContractError, match="parent_role 'unknown_role' is not in defined roles vocabulary"
    ):
        parse_profile_document_v07(doc)

    doc = vehicle_profile.document.model_dump(mode="json")
    doc["assembly"]["required_sockets"][0]["placement"] = "top"
    with pytest.raises(ProfileContractError):
        parse_profile_document_v07(doc)


@pytest.mark.parametrize(
    "tol_field",
    [
        "pivot_tolerance_m",
        "basis_tolerance_deg",
        "socket_position_tolerance_m",
        "socket_angle_tolerance_deg",
    ],
)
def test_v07_assembly_contract_tolerance_validation(
    vehicle_profile: AssetProfileV07, tol_field: str
) -> None:
    # Missing tolerance
    doc = vehicle_profile.document.model_dump(mode="json")
    del doc["assembly"][tol_field]
    with pytest.raises(ProfileContractError):
        parse_profile_document_v07(doc)

    # Zero tolerance
    doc = vehicle_profile.document.model_dump(mode="json")
    doc["assembly"][tol_field] = 0.0
    with pytest.raises(ProfileContractError):
        parse_profile_document_v07(doc)

    # Negative tolerance
    doc = vehicle_profile.document.model_dump(mode="json")
    doc["assembly"][tol_field] = -0.5
    with pytest.raises(ProfileContractError):
        parse_profile_document_v07(doc)

    # Inf tolerance
    doc = vehicle_profile.document.model_dump(mode="json")
    doc["assembly"][tol_field] = float("inf")
    with pytest.raises(ProfileContractError):
        parse_profile_document_v07(doc)


# ==============================================================================
# 4. V0.7 specification structure and validations
# ==============================================================================


def test_v07_spec_parts_and_sockets_structural_invariants(
    valid_vehicle_spec_dict: dict[str, Any], test_registry: ProfileRegistry
) -> None:
    # Duplicate part_ids
    bad = copy.deepcopy(valid_vehicle_spec_dict)
    bad["parts"][2]["part_id"] = "hull"
    with pytest.raises(SpecInvalidError, match="duplicate part_id found in parts"):
        parse_asset_specification_v07(bad, registry=test_registry)

    # Duplicate roles
    bad = copy.deepcopy(valid_vehicle_spec_dict)
    bad["parts"][1]["role"] = "hull"
    with pytest.raises(SpecInvalidError, match="duplicate role found in parts"):
        parse_asset_specification_v07(bad, registry=test_registry)

    # Unknown parent
    bad = copy.deepcopy(valid_vehicle_spec_dict)
    bad["parts"][1]["parent"] = "ghost_part"
    with pytest.raises(SpecInvalidError, match="unknown parent 'ghost_part'"):
        parse_asset_specification_v07(bad, registry=test_registry)

    # Two roots
    bad = copy.deepcopy(valid_vehicle_spec_dict)
    bad["parts"][1]["parent"] = "root"
    with pytest.raises(SpecInvalidError, match="hierarchy must have exactly one root part"):
        parse_asset_specification_v07(bad, registry=test_registry)

    # No root
    bad = copy.deepcopy(valid_vehicle_spec_dict)
    bad["parts"][0]["parent"] = "barrel"
    with pytest.raises(SpecInvalidError, match="hierarchy must have at least one root part"):
        parse_asset_specification_v07(bad, registry=test_registry)

    # Cycle (a -> b, b -> a)
    bad = copy.deepcopy(valid_vehicle_spec_dict)
    bad["parts"][1]["parent"] = "barrel"
    bad["parts"][2]["parent"] = "turret"
    with pytest.raises(SpecInvalidError, match="cycle detected"):
        parse_asset_specification_v07(bad, registry=test_registry)

    # Self parent
    with pytest.raises(ValueError, match="cannot parent to itself"):
        PartSpec(
            part_id="p1",
            role="hull",
            parent="p1",
            pivot=PartPivotSpec(
                position_m=[0, 0, 0],
                basis="identity",
                motion=PartMotionSpec(kind="fixed"),
            ),
        )

    # Part_id "root" forbidden
    with pytest.raises(ValueError, match="part_id 'root' is forbidden"):
        PartSpec(
            part_id="root",
            role="hull",
            parent="root",
            pivot=PartPivotSpec(
                position_m=[0, 0, 0],
                basis="identity",
                motion=PartMotionSpec(kind="fixed"),
            ),
        )


def test_v07_spec_motion_and_quaternion_rules() -> None:
    # Invalid motion kind
    with pytest.raises(ValueError):
        PartMotionSpec(kind="spinning")  # type: ignore[arg-type]

    # Non-unit axis
    with pytest.raises(ValueError, match="unit vector within 1e-6"):
        PartMotionSpec(kind="revolute", axis=[0.0, 2.0, 0.0])

    # Zero axis
    with pytest.raises(ValueError, match="unit vector within 1e-6"):
        PartMotionSpec(kind="revolute", axis=[0.0, 0.0, 0.0])

    # Axis on fixed
    with pytest.raises(ValueError, match="axis forbidden for fixed"):
        PartMotionSpec(kind="fixed", axis=[0.0, 1.0, 0.0])

    # Missing axis on revolute
    with pytest.raises(ValueError, match="axis required for revolute"):
        PartMotionSpec(kind="revolute", axis=None)

    # Limits on fixed
    with pytest.raises(ValueError, match="limits forbidden for fixed"):
        PartMotionSpec(kind="fixed", limits=MotionLimits(lower=-1.0, upper=1.0))

    # Limits with lower >= upper
    with pytest.raises(ValueError, match="strictly less than"):
        MotionLimits(lower=1.0, upper=0.5)

    with pytest.raises(ValueError, match="strictly less than"):
        MotionLimits(lower=1.0, upper=1.0)

    # Non-unit quaternion on pivot
    with pytest.raises(ValueError, match="normalized within 1e-6"):
        PartPivotSpec(
            position_m=[0, 0, 0],
            basis=[0.0, 0.0, 0.0, 0.5],
            motion=PartMotionSpec(kind="fixed"),
        )

    # Non-unit quaternion on socket
    with pytest.raises(ValueError, match="normalized within 1e-6"):
        SocketSpec(
            socket_id="s1",
            parent_part="hull",
            translation_m=[0, 0, 0],
            rotation=[0.0, 0.0, 0.0, 2.0],
            placement="forward_end",
        )


def test_v07_spec_socket_structural_rules(
    valid_vehicle_spec_dict: dict[str, Any], test_registry: ProfileRegistry
) -> None:
    # Socket parent missing
    bad = copy.deepcopy(valid_vehicle_spec_dict)
    bad["sockets"][0]["parent_part"] = "nonexistent_part"
    with pytest.raises(SpecInvalidError, match="does not exist in declared parts"):
        parse_asset_specification_v07(bad, registry=test_registry)

    # Duplicate socket ids
    bad = copy.deepcopy(valid_vehicle_spec_dict)
    bad["sockets"].append(dict(bad["sockets"][0]))
    with pytest.raises(SpecInvalidError, match="duplicate socket_id found in sockets"):
        parse_asset_specification_v07(bad, registry=test_registry)

    # Unsupported socket placement
    with pytest.raises(ValueError):
        SocketSpec(
            socket_id="s1",
            parent_part="hull",
            translation_m=[0, 0, 0],
            placement="top",  # type: ignore[arg-type]
        )

    # Sockets declared without parts
    bad = copy.deepcopy(valid_vehicle_spec_dict)
    bad["parts"] = None
    with pytest.raises(SpecInvalidError, match="sockets require parts to be defined"):
        parse_asset_specification_v07(bad, registry=test_registry)

    # Parts with provider_generated
    bad = copy.deepcopy(valid_vehicle_spec_dict)
    bad["source_kind"] = "provider_generated"
    with pytest.raises(SpecInvalidError, match="require source_kind 'local_operator_assembly'"):
        parse_asset_specification_v07(bad, registry=test_registry)

    # Missing source_kind
    bad = copy.deepcopy(valid_vehicle_spec_dict)
    del bad["source_kind"]
    with pytest.raises(SpecInvalidError):
        parse_asset_specification_v07(bad, registry=test_registry)

    # source_front rejected
    bad = copy.deepcopy(valid_vehicle_spec_dict)
    bad["source_front"] = "-Z"
    with pytest.raises(SpecInvalidError, match="Extra inputs are not permitted"):
        parse_asset_specification_v07(bad, registry=test_registry)


def test_v07_spec_collider_block_rules() -> None:
    # Missing collider block
    with pytest.raises(ValidationError):
        AssetSpecificationV07.model_validate(
            {
                "schema_version": "0.7.0",
                "asset_id": "test_box",
                "profile": "vehicle_test",
                "profile_version": 1,
                "intent": "Testing missing collider",
                "source_kind": "local_operator_assembly",
                "dimensions": {"width_m": 1.0, "depth_m": 1.0, "height_m": 1.0},
            }
        )

    # Capsule on box policy
    with pytest.raises(ValueError, match="capsule specification must be null when policy is box"):
        SpecColliderConfig(policy="box", capsule=CapsuleSpec(radius_m=0.15, height_m=0.8))

    # Capsule missing for capsule policy
    with pytest.raises(
        ValueError, match="capsule specification must be present when policy is capsule"
    ):
        SpecColliderConfig(policy="capsule", capsule=None)

    # Radius < 0.10 rejected
    with pytest.raises(ValueError, match="radius_m must be >= 0.10"):
        CapsuleSpec(radius_m=0.09, height_m=1.0)

    # Radius == 0.10 accepted
    cap = CapsuleSpec(radius_m=0.10, height_m=0.5)
    assert cap.radius_m == 0.10
    assert cap.height_m == 0.5

    # Height == 2r rejected
    with pytest.raises(ValueError, match="strictly greater than 2 \\* radius_m"):
        CapsuleSpec(radius_m=0.2, height_m=0.4)

    # Height slightly > 2r accepted
    cap2 = CapsuleSpec(radius_m=0.1, height_m=0.2001)
    assert cap2.height_m == 0.2001

    # Radius 0.2 accepted (no 0.25 rule)
    cap3 = CapsuleSpec(radius_m=0.2, height_m=1.0)
    assert cap3.radius_m == 0.2

    # Non-finite values rejected
    with pytest.raises(ValueError):
        CapsuleSpec(radius_m=float("nan"), height_m=1.0)
    with pytest.raises(ValueError):
        CapsuleSpec(radius_m=0.2, height_m=float("inf"))

    # Bool values rejected
    with pytest.raises(ValueError, match="got boolean"):
        CapsuleSpec(radius_m=True, height_m=1.0)


# ==============================================================================
# 5. Profile binding with explicit test registry
# ==============================================================================


def test_valid_specs_bind_successfully(
    valid_vehicle_spec_dict: dict[str, Any],
    valid_character_spec_dict: dict[str, Any],
    test_registry: ProfileRegistry,
) -> None:
    veh_spec = parse_asset_specification_v07(valid_vehicle_spec_dict, registry=test_registry)
    assert veh_spec.bound_profile().qualified == "vehicle_test@1"
    assert veh_spec.collider_policy == "box"

    char_spec = parse_asset_specification_v07(valid_character_spec_dict, registry=test_registry)
    assert char_spec.bound_profile().qualified == "character_test@1"
    assert char_spec.collider_policy == "capsule"


def test_capsule_fit_bounds_check(
    valid_character_spec_dict: dict[str, Any], test_registry: ProfileRegistry
) -> None:
    # Character dimensions: width=0.6, depth=0.5, height=1.8, tolerance=0.02
    # Too tall: height 1.85 > 1.8 + 0.02
    bad_h = copy.deepcopy(valid_character_spec_dict)
    bad_h["collider"]["capsule"]["height_m"] = 1.85
    with pytest.raises(SpecInvalidError, match="capsule height_m .* exceeds specification height"):
        parse_asset_specification_v07(bad_h, registry=test_registry)

    # Too wide: radius 0.35 -> diameter 0.70 > max(0.6, 0.5) + 0.02 = 0.62
    bad_r = copy.deepcopy(valid_character_spec_dict)
    bad_r["collider"]["capsule"]["radius_m"] = 0.35
    with pytest.raises(SpecInvalidError, match="capsule diameter .* exceeds max horizontal"):
        parse_asset_specification_v07(bad_r, registry=test_registry)


def test_binding_enforces_profile_role_vocabulary(
    valid_vehicle_spec_dict: dict[str, Any], test_registry: ProfileRegistry
) -> None:
    bad = copy.deepcopy(valid_vehicle_spec_dict)
    bad["parts"][0]["role"] = "alien_frame"
    with pytest.raises(SpecInvalidError, match="not in profile role vocabulary"):
        parse_asset_specification_v07(bad, registry=test_registry)


def test_binding_enforces_required_roles(
    valid_vehicle_spec_dict: dict[str, Any], test_registry: ProfileRegistry
) -> None:
    bad = copy.deepcopy(valid_vehicle_spec_dict)
    # Remove barrel part
    bad["parts"] = [p for p in bad["parts"] if p["role"] != "barrel"]
    bad["sockets"] = None
    with pytest.raises(SpecInvalidError, match="required role 'barrel' is missing"):
        parse_asset_specification_v07(bad, registry=test_registry)


def test_binding_enforces_motion_constraints(
    valid_vehicle_spec_dict: dict[str, Any], test_registry: ProfileRegistry
) -> None:
    # Wrong motion kind (turret is revolute, make it fixed)
    bad_kind = copy.deepcopy(valid_vehicle_spec_dict)
    bad_kind["parts"][1]["pivot"]["motion"] = {"kind": "fixed"}
    with pytest.raises(SpecInvalidError, match="does not match required kind 'revolute'"):
        parse_asset_specification_v07(bad_kind, registry=test_registry)

    # Wrong motion axis (turret is revolute about +Y, make it +X)
    bad_axis = copy.deepcopy(valid_vehicle_spec_dict)
    bad_axis["parts"][1]["pivot"]["motion"]["axis"] = [1.0, 0.0, 0.0]
    with pytest.raises(SpecInvalidError, match="does not match required axis"):
        parse_asset_specification_v07(bad_axis, registry=test_registry)


def test_binding_enforces_required_sockets(
    valid_vehicle_spec_dict: dict[str, Any], test_registry: ProfileRegistry
) -> None:
    # Missing required socket
    bad_missing = copy.deepcopy(valid_vehicle_spec_dict)
    bad_missing["sockets"] = None
    with pytest.raises(SpecInvalidError, match="required socket 'muzzle' is missing"):
        parse_asset_specification_v07(bad_missing, registry=test_registry)

    # Socket parented to wrong role
    bad_parent = copy.deepcopy(valid_vehicle_spec_dict)
    bad_parent["sockets"][0]["parent_part"] = "turret"
    with pytest.raises(SpecInvalidError, match="expected required role 'barrel'"):
        parse_asset_specification_v07(bad_parent, registry=test_registry)


def test_binding_geometry_mode_checks(
    valid_character_spec_dict: dict[str, Any],
    valid_vehicle_spec_dict: dict[str, Any],
    test_registry: ProfileRegistry,
) -> None:
    # single_mesh profile rejects parts
    bad_char = copy.deepcopy(valid_character_spec_dict)
    bad_char["source_kind"] = "local_operator_assembly"
    bad_char["parts"] = [
        {
            "part_id": "body",
            "role": "character",
            "parent": "root",
            "pivot": {"position_m": [0, 0, 0], "basis": "identity", "motion": {"kind": "fixed"}},
        }
    ]
    with pytest.raises(SpecInvalidError, match="single_mesh profile .* forbids parts"):
        parse_asset_specification_v07(bad_char, registry=test_registry)

    # assembly profile requires parts
    bad_veh = copy.deepcopy(valid_vehicle_spec_dict)
    bad_veh["parts"] = None
    bad_veh["sockets"] = None
    with pytest.raises(SpecInvalidError, match="assembly profile .* requires parts to be defined"):
        parse_asset_specification_v07(bad_veh, registry=test_registry)


def test_binding_source_kind_mismatch(
    valid_vehicle_spec_dict: dict[str, Any],
    vehicle_profile: AssetProfileV07,
) -> None:
    # Profile accepts local_operator_assembly only; if spec had provider_generated (tested via check_specification)
    bad_spec = copy.deepcopy(valid_vehicle_spec_dict)
    bad_spec["source_kind"] = "local_operator_assembly"
    spec = parse_asset_specification_v07(
        bad_spec,
        registry=ProfileRegistry(available=(), unsupported=(), available_v07=(vehicle_profile,)),
    )
    # Mutate to provider_generated and call check_specification
    spec.__dict__["source_kind"] = "provider_generated"
    with pytest.raises(ValueError, match="is not accepted by"):
        vehicle_profile.check_specification(spec)


def test_binding_version_mismatch(
    valid_vehicle_spec_dict: dict[str, Any], test_registry: ProfileRegistry
) -> None:
    bad = copy.deepcopy(valid_vehicle_spec_dict)
    bad["profile_version"] = 2
    with pytest.raises(SpecInvalidError, match="vehicle_test@2 is not registered"):
        parse_asset_specification_v07(bad, registry=test_registry)


def test_v07_spec_against_builtin_registry_fails(
    valid_vehicle_spec_dict: dict[str, Any],
    valid_character_spec_dict: dict[str, Any],
) -> None:
    # vehicle_test is not in builtin_registry (it is not even known, or vehicle is unsupported)
    with pytest.raises(SpecInvalidError, match="not registered"):
        parse_asset_specification_v07(valid_vehicle_spec_dict)

    # Spec naming static_prop@1 against builtin_registry fails (no V0.7 registration for static_prop)
    prop_07 = {
        "schema_version": "0.7.0",
        "asset_id": "prop_07_crate",
        "category": "prop",
        "profile": "static_prop",
        "profile_version": 1,
        "intent": "V07 spec naming static_prop",
        "source_kind": "provider_generated",
        "dimensions": {"width_m": 1.0, "depth_m": 1.0, "height_m": 1.0},
        "collider": {"policy": "box"},
    }
    with pytest.raises(SpecInvalidError, match="static_prop@1 is not registered"):
        parse_asset_specification_v07(prop_07)

    # Spec naming an UNSUPPORTED profile id (e.g. rigged_character) against public catalog fails
    unsupported_spec = copy.deepcopy(valid_character_spec_dict)
    unsupported_spec["profile"] = "rigged_character"
    with pytest.raises(SpecInvalidError, match="profile rigged_character is UNSUPPORTED"):
        parse_asset_specification_v07(unsupported_spec)

    # Negative version against public catalog fails
    bad_version_spec = copy.deepcopy(valid_character_spec_dict)
    bad_version_spec["profile"] = "character"
    bad_version_spec["profile_version"] = 2
    with pytest.raises(SpecInvalidError, match="character@2 is not registered"):
        parse_asset_specification_v07(bad_version_spec)


def test_v07_spec_cannot_bind_v05_profile_even_if_in_available(
    valid_character_spec_dict: dict[str, Any],
) -> None:
    static_05 = builtin_registry().get("static_prop", 1)
    reg_with_05 = ProfileRegistry(
        available=(static_05,),
        unsupported=(),
        available_v07=(),
    )
    spec_dict = {
        "schema_version": "0.7.0",
        "asset_id": "test_crate_07",
        "category": "prop",
        "profile": "static_prop",
        "profile_version": 1,
        "intent": "Attempting to bind V05 profile",
        "source_kind": "provider_generated",
        "dimensions": {"width_m": 1.0, "depth_m": 1.0, "height_m": 1.0},
        "collider": {"policy": "box"},
    }
    # get_v07 only searches available_v07, so it cannot find static_prop in available
    with pytest.raises(SpecInvalidError, match="static_prop@1 is not registered"):
        parse_asset_specification_v07(spec_dict, registry=reg_with_05)


# ==============================================================================
# 6. ProfileRegistry invariants, freezing, and isolation
# ==============================================================================


def test_builtin_registry_contents_and_isolation() -> None:
    reg = builtin_registry()
    availability = reg.availability()
    available_rows = [r for r in availability if r["status"] == "AVAILABLE"]
    unsupported_rows = [r for r in availability if r["status"] == "UNSUPPORTED"]

    assert [r["qualified"] for r in available_rows] == [
        "static_prop@1",
        "pickup@1",
        "modular_piece@1",
    ]
    assert tuple(r["profile_id"] for r in unsupported_rows) == UNSUPPORTED_PROFILE_IDS
    assert "aircraft" not in [r["profile_id"] for r in unsupported_rows]

    # available_v07 is empty
    assert reg.available_v07 == ()

    # No test ids present
    assert not any("test" in r["profile_id"] for r in availability)

    # Historical profiles remain unchanged; V0.7 assembly resources live in
    # their own explicit registry.
    profile_dir = files("gamefactory").joinpath("resources/profiles")
    profile_filenames = {p.name for p in profile_dir.iterdir()}
    assert profile_filenames == {
        "static_prop.yml",
        "pickup.yml",
        "modular_piece.yml",
        "vehicle.yml",
        "weapon.yml",
        "aircraft.yml",
        "character.yml",
    }
    with pytest.raises(ProfileContractError, match="profile vehicle is UNSUPPORTED"):
        reg.get_v07("vehicle", 1)

    v07_reg = builtin_v07_registry()
    assert tuple(profile.qualified for profile in v07_reg.available_v07) == (
        "vehicle@1",
        "weapon@1",
        "aircraft@1",
        "character@1",
    )
    assert v07_reg.unsupported == UNSUPPORTED_PROFILE_IDS_V07
    assert v07_reg.availability()[0:4] == [
        {
            "profile_id": "vehicle",
            "qualified": "vehicle@1",
            "version": "1",
            "status": "AVAILABLE",
        },
        {
            "profile_id": "weapon",
            "qualified": "weapon@1",
            "version": "1",
            "status": "AVAILABLE",
        },
        {
            "profile_id": "aircraft",
            "qualified": "aircraft@1",
            "version": "1",
            "status": "AVAILABLE",
        },
        {
            "profile_id": "character",
            "qualified": "character@1",
            "version": "1",
            "status": "AVAILABLE",
        },
    ]
    assert all(row["status"] == "UNSUPPORTED" for row in v07_reg.availability()[4:])
    assert "character" not in {row["profile_id"] for row in v07_reg.availability()[4:]}
    assert "rigged_character" in {row["profile_id"] for row in v07_reg.availability()[4:]}
    with pytest.raises(ProfileContractError, match="profile rigged_character is UNSUPPORTED"):
        v07_reg.get_v07("rigged_character", 1)
    with pytest.raises(ProfileContractError, match="profile character@2 is not registered"):
        v07_reg.get_v07("character", 2)

    # Registry is frozen
    with pytest.raises(dataclasses.FrozenInstanceError):
        reg.available = ()  # type: ignore[misc]

    # Constructing a test registry does not mutate or affect subsequent builtin_registry()
    test_reg = ProfileRegistry(
        available=(),
        unsupported=(),
        available_v07=(
            AssetProfileV07(
                parse_profile_document_v07(VEHICLE_PROFILE_PATH.read_text(encoding="utf-8"))
            ),
        ),
    )
    assert len(test_reg.available_v07) == 1
    assert len(builtin_registry().available_v07) == 0


def test_registry_post_init_validation(
    vehicle_profile: AssetProfileV07,
) -> None:
    static_05 = builtin_registry().get("static_prop", 1)

    # AssetProfileV07 in available -> TypeError
    with pytest.raises(TypeError, match="available must be a tuple of AssetProfile instances"):
        ProfileRegistry(
            available=(vehicle_profile,),  # type: ignore[arg-type]
            unsupported=(),
        )

    # AssetProfile in available_v07 -> TypeError
    with pytest.raises(
        TypeError, match="available_v07 must be a tuple of AssetProfileV07 instances"
    ):
        ProfileRegistry(
            available=(),
            unsupported=(),
            available_v07=(static_05,),  # type: ignore[arg-type]
        )

    # Non-str in unsupported -> TypeError
    with pytest.raises(TypeError, match="unsupported must be a tuple of str identifiers"):
        ProfileRegistry(
            available=(),
            unsupported=(123,),  # type: ignore[arg-type]
        )

    # List instead of tuple -> TypeError
    with pytest.raises(TypeError):
        ProfileRegistry(
            available=[],  # type: ignore[arg-type]
            unsupported=(),
        )

    # Duplicate qualified ids
    with pytest.raises(ValueError, match="duplicate qualified profile id"):
        ProfileRegistry(
            available=(static_05, static_05),
            unsupported=(),
        )

    # Profile both available and unsupported
    with pytest.raises(ValueError, match="cannot be both available and unsupported"):
        ProfileRegistry(
            available=(static_05,),
            unsupported=("static_prop",),
        )

    with pytest.raises(ValueError, match="vehicle_test.* cannot be both available and unsupported"):
        ProfileRegistry(
            available=(),
            unsupported=("vehicle_test",),
            available_v07=(vehicle_profile,),
        )


def test_registry_get_methods_strictly_partitioned(
    vehicle_profile: AssetProfileV07,
) -> None:
    static_05 = builtin_registry().get("static_prop", 1)
    reg = ProfileRegistry(
        available=(static_05,),
        unsupported=("vehicle",),
        available_v07=(vehicle_profile,),
    )
    # get() returns AssetProfile, never AssetProfileV07
    p05 = reg.get("static_prop", 1)
    assert isinstance(p05, AssetProfile)

    with pytest.raises(ProfileContractError, match="not registered"):
        reg.get("vehicle_test", 1)

    # get_v07 returns AssetProfileV07, never AssetProfile
    p07 = reg.get_v07("vehicle_test", 1)
    assert isinstance(p07, AssetProfileV07)

    with pytest.raises(ProfileContractError, match="not registered"):
        reg.get_v07("static_prop", 1)

    # Unsupported check fires first on both
    with pytest.raises(ProfileContractError, match="profile vehicle is UNSUPPORTED"):
        reg.get("vehicle", 1)

    with pytest.raises(ProfileContractError, match="profile vehicle is UNSUPPORTED"):
        reg.get_v07("vehicle", 1)


def test_wrong_type_context_registry_rejected(
    valid_vehicle_spec_dict: dict[str, Any],
) -> None:
    with pytest.raises(SpecInvalidError, match="registry must be an instance of ProfileRegistry"):
        parse_asset_specification_v07(valid_vehicle_spec_dict, registry="bad_registry")  # type: ignore[arg-type]

    with pytest.raises(
        ValidationError, match="profile_registry must be an instance of ProfileRegistry"
    ):
        AssetSpecificationV07.model_validate(
            valid_vehicle_spec_dict,
            context={"profile_registry": object()},
        )


# ==============================================================================
# 7. Model dump hygiene and deterministic fingerprint
# ==============================================================================


def test_v07_spec_model_dump_hygiene_and_fingerprint(
    valid_vehicle_spec_dict: dict[str, Any], test_registry: ProfileRegistry
) -> None:
    spec = parse_asset_specification_v07(valid_vehicle_spec_dict, registry=test_registry)
    dump = spec.model_dump()

    # Must contain collider block, but NO top-level collider_policy
    assert "collider" in dump
    assert "collider_policy" not in dump

    # Must contain NO private attributes
    assert "_bound_profile" not in dump
    assert not any(k.startswith("_") for k in dump.keys())

    # Fingerprint is deterministic and repeatable
    fp1 = spec_fingerprint(spec)
    assert isinstance(fp1, str) and len(fp1) == 64

    spec2 = parse_asset_specification_v07(valid_vehicle_spec_dict, registry=test_registry)
    assert spec_fingerprint(spec2) == fp1


# --- Review hardening -------------------------------------------------------


def _vehicle_profile_document() -> dict[str, Any]:
    import yaml

    loaded: dict[str, Any] = yaml.safe_load(VEHICLE_PROFILE_PATH.read_text(encoding="utf-8"))
    return loaded


@pytest.mark.parametrize("snap", [float("nan"), float("inf"), float("-inf")])
def test_v07_profile_rejects_non_finite_snap_grid(snap: float) -> None:
    doc = _vehicle_profile_document()
    doc["processing"]["snap_grid_m"] = snap
    with pytest.raises(ProfileContractError, match="snap_grid_m"):
        parse_profile_document_v07(doc)


@pytest.mark.parametrize("bad", [None, "0.01", True, 10**400])
def test_v07_tolerances_reject_non_numbers_as_validation_errors(bad: Any) -> None:
    doc = _vehicle_profile_document()
    doc["assembly"]["pivot_tolerance_m"] = bad
    with pytest.raises(ValidationError, match="pivot_tolerance_m"):
        ProfileDocumentV07.model_validate(doc)
    doc = _vehicle_profile_document()
    doc["assembly"]["required_sockets"][0]["forward_end_fraction"] = bad
    with pytest.raises(ValidationError, match="forward_end_fraction"):
        ProfileDocumentV07.model_validate(doc)


@pytest.mark.parametrize("bad", [None, "1", 10**400])
def test_v07_spec_vectors_reject_non_numbers_as_validation_errors(bad: Any) -> None:
    with pytest.raises(ValidationError, match="axis"):
        PartMotionSpec.model_validate({"kind": "revolute", "axis": [bad, 0.0, 0.0]})
    with pytest.raises(ValidationError, match="position_m"):
        PartPivotSpec.model_validate({"position_m": [bad, 0.0, 0.0], "motion": {"kind": "fixed"}})


def test_v07_profile_version_is_strict_integer() -> None:
    doc = _vehicle_profile_document()
    doc["version"] = "1"
    with pytest.raises(ProfileContractError, match="version"):
        parse_profile_document_v07(doc)


def test_v07_spec_rejects_longer_cycle_detached_from_root(
    valid_vehicle_spec_dict: dict[str, Any], test_registry: ProfileRegistry
) -> None:
    bad = copy.deepcopy(valid_vehicle_spec_dict)
    template = bad["parts"][1]
    extra = []
    for part_id, parent in (("aa", "cc"), ("bb", "aa"), ("cc", "bb")):
        part = copy.deepcopy(template)
        part.update(part_id=part_id, role=f"{part_id}_role", parent=parent)
        extra.append(part)
    bad["parts"].extend(extra)
    with pytest.raises(SpecInvalidError, match="cycle detected"):
        parse_asset_specification_v07(bad, registry=test_registry)


def test_v07_unbound_instance_has_no_bound_profile(
    valid_vehicle_spec_dict: dict[str, Any], test_registry: ProfileRegistry
) -> None:
    spec = parse_asset_specification_v07(valid_vehicle_spec_dict, registry=test_registry)
    unbound = AssetSpecificationV07.model_construct(**dict(spec))
    with pytest.raises(SpecInvalidError, match="not validated against a registry"):
        unbound.bound_profile()


def test_packaged_character_spec_binds_canonical_profile_hash() -> None:
    spec_path = Path(str(files("gamefactory").joinpath("resources/specs/character_test.yml")))
    assert spec_path.is_file()
    spec = parse_asset_specification_v07(spec_path)
    bound = spec.bound_profile()
    assert bound.qualified == "character@1"
    assert bound.profile_id == "character"
    assert bound.version == 1
    assert bound.geometry_mode == "single_mesh"
    assert bound.accepted_source_kinds == ("provider_generated",)
    assert bound.document.godot.body_kind == "static_body"
    assert bound.document.godot.require_ray_hit is True
    assert bound.document.processing.lod0_required is True
    assert bound.document.processing.lod1_required is True
    assert bound.document.processing.rig_forbidden is True
    assert bound.document.processing.animation_forbidden is True
    assert bound.document.processing.allowed_collider_policies == ["capsule"]
    assert len(bound.review_views) == 9

    raw = json.dumps(
        bound.document.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    canonical_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert canonical_hash == "135c1530395daff86a15dde55433eb017d193d0d729d40c6b4d9fdb6a0e1ed61"
