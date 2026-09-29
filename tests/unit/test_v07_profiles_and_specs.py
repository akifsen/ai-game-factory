"""Unit tests for V0.7 profile and specification foundations (Step 7).

Covers:
- Dual-version profile schema (asset-profile-0.7.0 and asset-profile-0.5.0)
- Dual-version spec schema (asset-spec-0.7.0, asset-spec-0.5.0, asset-spec-0.4.0)
- Geometry modes: single_mesh and assembly
- Role vocabulary, motion constraints, and required sockets
- Sockets and pivots structural and semantic validation
- Capsule collider structural rules and bounds sanity checks
- Registry injection for tests and isolation from builtin_registry
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gamefactory.core.domain.asset_contracts import (
    CapsuleSpec,
    PartMotionSpec,
    PartPivotSpec,
    PartSpec,
    SocketSpec,
    SpecColliderConfig,
    parse_asset_specification,
)
from gamefactory.core.domain.asset_profiles import (
    AssetProfile,
    ProfileContractError,
    ProfileRegistry,
    builtin_registry,
    parse_profile_document,
)
from gamefactory.core.domain.errors import SpecInvalidError

VEHICLE_PROFILE_PATH = Path("tests/fixtures/profiles/vehicle_profile_070.yml")
CHARACTER_PROFILE_PATH = Path("tests/fixtures/profiles/character_profile_070.yml")


@pytest.fixture(scope="module")
def vehicle_profile() -> AssetProfile:
    assert VEHICLE_PROFILE_PATH.is_file()
    return AssetProfile(parse_profile_document(VEHICLE_PROFILE_PATH.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def character_profile() -> AssetProfile:
    assert CHARACTER_PROFILE_PATH.is_file()
    return AssetProfile(parse_profile_document(CHARACTER_PROFILE_PATH.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def test_registry(
    vehicle_profile: AssetProfile, character_profile: AssetProfile
) -> ProfileRegistry:
    return ProfileRegistry(
        available=(vehicle_profile, character_profile),
        unsupported=(),
    )


@pytest.fixture
def valid_vehicle_spec_dict() -> dict:
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
        "collider_policy": "box",
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


# ==================== 1. Profile schema tests ====================


def test_07_assembly_profile_loads_and_05_profile_with_07_field_rejected(
    vehicle_profile: AssetProfile,
) -> None:
    assert vehicle_profile.schema_version == "asset-profile-0.7.0"
    assert vehicle_profile.geometry_mode == "assembly"
    assert vehicle_profile.accepted_source_kinds == ("local_operator_assembly",)
    assert vehicle_profile.assembly is not None
    assert vehicle_profile.assembly.roles == ["hull", "turret", "barrel"]
    assert vehicle_profile.assembly.required_roles == ["hull", "turret", "barrel"]

    # 0.5 profile with 0.7 fields rejected
    p05 = builtin_registry().get("static_prop").document.model_dump(mode="json")
    p05["geometry_mode"] = "assembly"
    with pytest.raises(
        ProfileContractError,
        match="Extra inputs are not permitted: asset-profile-0.5.0 does not accept field 'geometry_mode'",
    ):
        parse_profile_document(p05)

    p05 = builtin_registry().get("static_prop").document.model_dump(mode="json")
    p05["accepted_source_kinds"] = ["local_operator_assembly"]
    with pytest.raises(
        ProfileContractError,
        match="Extra inputs are not permitted: asset-profile-0.5.0 does not accept field 'accepted_source_kinds'",
    ):
        parse_profile_document(p05)

    p05 = builtin_registry().get("static_prop").document.model_dump(mode="json")
    p05["assembly"] = {
        "roles": ["hull"],
        "required_roles": ["hull"],
    }
    with pytest.raises(
        ProfileContractError,
        match="Extra inputs are not permitted: asset-profile-0.5.0 does not accept field 'assembly'",
    ):
        parse_profile_document(p05)


def test_07_profile_with_capsule_loads_and_05_with_capsule_rejected(
    character_profile: AssetProfile,
) -> None:
    assert character_profile.schema_version == "asset-profile-0.7.0"
    assert character_profile.geometry_mode == "single_mesh"
    assert character_profile.document.processing.allowed_collider_policies == ["capsule"]

    p05 = builtin_registry().get("static_prop").document.model_dump(mode="json")
    p05["processing"]["allowed_collider_policies"] = ["capsule"]
    with pytest.raises(ProfileContractError, match="capsule collider is only allowed"):
        parse_profile_document(p05)


def test_unknown_geometry_mode_and_assembly_wrong_source_kind() -> None:
    doc = parse_profile_document(VEHICLE_PROFILE_PATH.read_text(encoding="utf-8")).model_dump(
        mode="json"
    )
    doc["geometry_mode"] = "unknown_mode"
    with pytest.raises(ProfileContractError):
        parse_profile_document(doc)

    doc = parse_profile_document(VEHICLE_PROFILE_PATH.read_text(encoding="utf-8")).model_dump(
        mode="json"
    )
    doc["accepted_source_kinds"] = ["provider_generated"]
    with pytest.raises(ProfileContractError, match="requires accepted_source_kinds =="):
        parse_profile_document(doc)

    doc = parse_profile_document(CHARACTER_PROFILE_PATH.read_text(encoding="utf-8")).model_dump(
        mode="json"
    )
    doc["assembly"] = {"roles": ["r1"], "required_roles": ["r1"]}
    with pytest.raises(ProfileContractError, match="forbids assembly contract"):
        parse_profile_document(doc)


# ==================== 2. Spec schema and binding ====================


def test_valid_07_vehicle_assembly_spec_binds(
    valid_vehicle_spec_dict: dict, test_registry: ProfileRegistry
) -> None:
    spec = parse_asset_specification(valid_vehicle_spec_dict, registry=test_registry)
    assert spec.schema_version == "0.7.0"
    assert spec.source_kind == "local_operator_assembly"
    assert spec.bound_profile().qualified == "vehicle_test@1"
    assert spec.parts is not None and len(spec.parts) == 3
    assert spec.sockets is not None and len(spec.sockets) == 1


def test_assembly_spec_hierarchy_and_part_validation_errors(test_registry: ProfileRegistry) -> None:
    # Cycle detection
    spec_dict = {
        "schema_version": "0.7.0",
        "asset_id": "cycle_test",
        "category": "vehicle",
        "profile": "vehicle_test",
        "profile_version": 1,
        "intent": "Cycle test",
        "source_kind": "local_operator_assembly",
        "dimensions": {"width_m": 1.0, "depth_m": 1.0, "height_m": 1.0},
        "parts": [
            {
                "part_id": "hull",
                "role": "hull",
                "parent": "root",
                "pivot": {
                    "position_m": [0, 0, 0],
                    "basis": "identity",
                    "motion": {"kind": "fixed"},
                },
            },
            {
                "part_id": "a",
                "role": "turret",
                "parent": "b",
                "pivot": {
                    "position_m": [0, 0, 0],
                    "basis": "identity",
                    "motion": {"kind": "revolute", "axis": [0, 1, 0]},
                },
            },
            {
                "part_id": "b",
                "role": "barrel",
                "parent": "a",
                "pivot": {
                    "position_m": [0, 0, 0],
                    "basis": "identity",
                    "motion": {"kind": "revolute", "axis": [1, 0, 0]},
                },
            },
        ],
    }
    with pytest.raises(SpecInvalidError, match="cycle detected"):
        parse_asset_specification(spec_dict, registry=test_registry)

    # Orphan part
    spec_dict["parts"][0]["parent"] = "nonexistent_parent"
    spec_dict["parts"][1]["parent"] = "hull"
    spec_dict["parts"][2]["parent"] = "a"
    with pytest.raises(SpecInvalidError, match="orphan"):
        parse_asset_specification(spec_dict, registry=test_registry)

    # Duplicate part_id
    spec_dict["parts"][0]["parent"] = "root"
    spec_dict["parts"][1]["parent"] = "root"
    spec_dict["parts"][1]["part_id"] = "hull"  # duplicate of parts[0]
    with pytest.raises(SpecInvalidError, match="duplicate part_id"):
        parse_asset_specification(spec_dict, registry=test_registry)

    # Self-parenting
    with pytest.raises((SpecInvalidError, ValueError), match="cannot parent to itself"):
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


def test_pivot_motion_and_quaternion_validation() -> None:
    # Non-unit quaternion
    with pytest.raises(ValueError, match="quaternion basis must be normalized"):
        PartPivotSpec(
            position_m=[0, 0, 0],
            basis=[0.0, 0.0, 0.0, 0.5],
            motion=PartMotionSpec(kind="fixed"),
        )

    # Axis forbidden for fixed
    with pytest.raises(ValueError, match="axis forbidden for fixed"):
        PartMotionSpec(kind="fixed", axis=[0.0, 1.0, 0.0])

    # Axis required for revolute
    with pytest.raises(ValueError, match="axis required for revolute"):
        PartMotionSpec(kind="revolute", axis=None)

    # Non-unit axis
    with pytest.raises(ValueError, match="unit vector"):
        PartMotionSpec(kind="revolute", axis=[0.0, 2.0, 0.0])


def test_socket_validation_errors(
    valid_vehicle_spec_dict: dict, test_registry: ProfileRegistry
) -> None:
    # Socket with unknown parent
    bad_spec = dict(valid_vehicle_spec_dict)
    bad_spec["sockets"] = [
        {
            "socket_id": "s1",
            "parent_part": "unknown_part",
            "translation_m": [0, 0, 0],
            "rotation": "identity",
            "placement": "forward_end",
        }
    ]
    with pytest.raises(SpecInvalidError, match="parent_part 'unknown_part' does not exist"):
        parse_asset_specification(bad_spec, registry=test_registry)

    # Non-unit rotation quaternion on socket
    with pytest.raises(ValueError, match="normalized within 1e-6"):
        SocketSpec(
            socket_id="s1",
            parent_part="hull",
            translation_m=[0, 0, 0],
            rotation=[0.0, 0.0, 0.0, 2.0],
            placement="forward_end",
        )

    # Unknown socket placement rejected
    with pytest.raises(ValueError, match="Input should be 'forward_end'"):
        SocketSpec(
            socket_id="s1",
            parent_part="hull",
            translation_m=[0, 0, 0],
            placement="invalid_placement",  # type: ignore[arg-type]
        )

    # Sockets declared without parts rejected
    spec_no_parts = dict(valid_vehicle_spec_dict)
    del spec_no_parts["parts"]
    with pytest.raises(SpecInvalidError, match="sockets require parts to be defined"):
        parse_asset_specification(spec_no_parts, registry=test_registry)

    spec_null_parts = dict(valid_vehicle_spec_dict, parts=None)
    with pytest.raises(SpecInvalidError, match="sockets require parts to be defined"):
        parse_asset_specification(spec_null_parts, registry=test_registry)


def test_assembly_role_and_socket_binding_failures(
    valid_vehicle_spec_dict: dict, test_registry: ProfileRegistry
) -> None:
    # Unknown role
    bad_role = dict(valid_vehicle_spec_dict)
    bad_role["parts"] = [
        dict(p, role="alien_part" if p["part_id"] == "hull" else p["role"])
        for p in valid_vehicle_spec_dict["parts"]
    ]
    with pytest.raises(SpecInvalidError, match="not in profile role vocabulary"):
        parse_asset_specification(bad_role, registry=test_registry)

    # Missing required role
    missing_role = dict(valid_vehicle_spec_dict)
    missing_role["parts"] = [p for p in valid_vehicle_spec_dict["parts"] if p["role"] != "barrel"]
    missing_role["sockets"] = []
    with pytest.raises(SpecInvalidError, match="required role 'barrel' is missing"):
        parse_asset_specification(missing_role, registry=test_registry)

    # Wrong axis for role (turret requires revolute about +Y, provide +X)
    wrong_axis = dict(valid_vehicle_spec_dict)
    wrong_axis["parts"] = [
        (
            dict(
                p,
                pivot={
                    "position_m": p["pivot"]["position_m"],
                    "basis": "identity",
                    "motion": {"kind": "revolute", "axis": [1.0, 0.0, 0.0]},
                },
            )
            if p["role"] == "turret"
            else p
        )
        for p in valid_vehicle_spec_dict["parts"]
    ]
    with pytest.raises(SpecInvalidError, match="does not match required axis"):
        parse_asset_specification(wrong_axis, registry=test_registry)

    # Missing required socket (muzzle required by vehicle_test profile)
    missing_socket = dict(valid_vehicle_spec_dict)
    missing_socket["sockets"] = []
    with pytest.raises(SpecInvalidError, match="required socket 'muzzle' is missing"):
        parse_asset_specification(missing_socket, registry=test_registry)

    # Socket under wrong-role part (muzzle parented to turret instead of barrel)
    wrong_parent = dict(valid_vehicle_spec_dict)
    wrong_parent["sockets"] = [
        {
            "socket_id": "muzzle",
            "parent_part": "turret",
            "translation_m": [0.0, 0.0, -1.0],
            "rotation": "identity",
            "placement": "forward_end",
        }
    ]
    with pytest.raises(SpecInvalidError, match="expected required role 'barrel'"):
        parse_asset_specification(wrong_parent, registry=test_registry)


def test_parts_with_provider_generated_source_kind_rejected(
    valid_vehicle_spec_dict: dict, test_registry: ProfileRegistry
) -> None:
    spec_dict = dict(valid_vehicle_spec_dict)
    spec_dict["source_kind"] = "provider_generated"
    with pytest.raises(SpecInvalidError, match="require source_kind 'local_operator_assembly'"):
        parse_asset_specification(spec_dict, registry=test_registry)


def test_cross_version_spec_and_profile_binding_rejected(
    valid_vehicle_spec_dict: dict, test_registry: ProfileRegistry
) -> None:
    # 0.7 spec against 0.5 profile
    spec_07_for_05 = dict(valid_vehicle_spec_dict)
    spec_07_for_05["profile"] = "static_prop"
    spec_07_for_05["category"] = "prop"
    spec_07_for_05["parts"] = None
    spec_07_for_05["sockets"] = None
    spec_07_for_05["source_kind"] = "provider_generated"
    with pytest.raises(
        SpecInvalidError, match="asset-spec-0.7.0 cannot bind to asset-profile-0.5.0"
    ):
        parse_asset_specification(spec_07_for_05)

    # 0.5 spec against 0.7 profile
    spec_05_for_07 = {
        "schema_version": "0.5.0",
        "asset_id": "crate_05",
        "category": "vehicle",
        "profile": "vehicle_test",
        "profile_version": 1,
        "intent": "0.5 spec attempting to bind 0.7 profile",
        "dimensions": {"width_m": 1.0, "depth_m": 1.0, "height_m": 1.0},
        "collider_policy": "box",
        "lod_policy": "lod0_lod1",
    }
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(spec_05_for_07, registry=test_registry)


# ==================== 3. Capsule collider tests ====================


def test_capsule_structural_rules() -> None:
    # radius < 0.10 rejected
    with pytest.raises(ValueError, match="radius_m must be >= 0.10"):
        CapsuleSpec(radius_m=0.09, height_m=1.0)

    # radius 0.10 accepted
    cap = CapsuleSpec(radius_m=0.10, height_m=0.5)
    assert cap.radius_m == 0.10
    assert cap.height_m == 0.5

    # height == 2r rejected
    with pytest.raises(ValueError, match="strictly greater than 2 \\* radius_m"):
        CapsuleSpec(radius_m=0.2, height_m=0.4)

    # height < 2r rejected
    with pytest.raises(ValueError, match="strictly greater than 2 \\* radius_m"):
        CapsuleSpec(radius_m=0.2, height_m=0.3)

    # policy box with capsule block rejected
    with pytest.raises(ValueError, match="capsule specification must be null when policy is box"):
        SpecColliderConfig(policy="box", capsule=CapsuleSpec(radius_m=0.15, height_m=0.8))


def test_capsule_bounds_and_binding(test_registry: ProfileRegistry) -> None:
    character_spec_dict = {
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
        "collider_policy": "capsule",
        "collider": {
            "policy": "capsule",
            "capsule": {
                "radius_m": 0.25,
                "height_m": 1.8,
            },
        },
        "lod_policy": "lod0_only",
    }
    # Valid capsule binds
    spec = parse_asset_specification(character_spec_dict, registry=test_registry)
    assert spec.collider_policy == "capsule"
    assert spec.collider is not None and spec.collider.capsule is not None

    # Capsule height exceeds bounds + tolerance
    oversized_height = dict(character_spec_dict)
    oversized_height["collider"] = {
        "policy": "capsule",
        "capsule": {"radius_m": 0.25, "height_m": 1.85},  # > 1.8 + 0.02
    }
    with pytest.raises(SpecInvalidError, match="capsule height_m .* exceeds specification height"):
        parse_asset_specification(oversized_height, registry=test_registry)

    # Capsule diameter exceeds horizontal bounds + tolerance
    oversized_radius = dict(character_spec_dict)
    oversized_radius["collider"] = {
        "policy": "capsule",
        "capsule": {"radius_m": 0.35, "height_m": 1.8},  # 2*0.35 = 0.70 > 0.60 + 0.02
    }
    with pytest.raises(SpecInvalidError, match="capsule diameter .* exceeds max horizontal"):
        parse_asset_specification(oversized_radius, registry=test_registry)


def test_capsule_in_05_spec_rejected() -> None:
    spec_05 = {
        "schema_version": "0.5.0",
        "asset_id": "capsule_in_05",
        "category": "pickup",
        "profile": "pickup",
        "profile_version": 1,
        "intent": "Capsule in 0.5 spec",
        "dimensions": {"width_m": 0.5, "depth_m": 0.5, "height_m": 0.5},
        "collider_policy": "capsule",
        "lod_policy": "lod0_only",
    }
    with pytest.raises(SpecInvalidError, match="only accepts collider_policy 'box'"):
        parse_asset_specification(spec_05)


def test_07_fields_in_04_or_05_specs_rejected() -> None:
    # 0.4 with parts
    crate_04 = parse_asset_specification(
        Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
    ).model_dump(mode="json")
    crate_04["parts"] = [
        {
            "part_id": "p1",
            "role": "hull",
            "parent": "root",
            "pivot": {"position_m": [0, 0, 0], "motion": {"kind": "fixed"}},
        }
    ]
    with pytest.raises(
        SpecInvalidError,
        match="Extra inputs are not permitted: asset-spec-0.4.0 does not accept field 'parts'",
    ):
        parse_asset_specification(crate_04)

    # 0.5 with source_kind
    cell_05 = parse_asset_specification(
        Path("src/gamefactory/resources/specs/pickup_energy_cell_01.yml")
    ).model_dump(mode="json")
    cell_05["source_kind"] = "local_operator_assembly"
    with pytest.raises(
        SpecInvalidError,
        match="Extra inputs are not permitted: asset-spec-0.5.0 does not accept field 'source_kind'",
    ):
        parse_asset_specification(cell_05)


@pytest.mark.parametrize("field", ["parts", "sockets", "source_kind", "collider"])
@pytest.mark.parametrize("schema_version", [None, "0.4.0", "0.5.0"])
def test_historical_spec_rejects_explicit_null_v07_fields(
    schema_version: str | None, field: str
) -> None:
    doc: dict[str, Any] = {
        "asset_id": "null_test_prop",
        "intent": "Testing explicit null rejection",
        "dimensions": {"width_m": 1.0, "depth_m": 1.0, "height_m": 1.0},
        field: None,
    }
    if schema_version is not None:
        doc["schema_version"] = schema_version
        if schema_version == "0.5.0":
            doc["category"] = "prop"
            doc["profile"] = "static_prop"
            doc["profile_version"] = 1
    ver_str = "0.4.0" if schema_version is None else schema_version
    with pytest.raises(
        SpecInvalidError,
        match=f"Extra inputs are not permitted: asset-spec-{ver_str} does not accept field '{field}'",
    ):
        parse_asset_specification(doc)


@pytest.mark.parametrize("field", ["geometry_mode", "accepted_source_kinds", "assembly"])
def test_profile_050_rejects_explicit_null_v07_fields(field: str) -> None:
    p05 = builtin_registry().get("static_prop").document.model_dump(mode="json")
    p05[field] = None
    with pytest.raises(
        ProfileContractError,
        match=f"Extra inputs are not permitted: asset-profile-0.5.0 does not accept field '{field}'",
    ):
        parse_profile_document(p05)


def test_profile_rejects_top_level_assembly_keys_as_extra_inputs() -> None:
    doc = parse_profile_document(VEHICLE_PROFILE_PATH.read_text(encoding="utf-8")).model_dump(
        mode="json"
    )
    doc["roles"] = ["hull"]
    with pytest.raises(ProfileContractError, match="Extra inputs are not permitted"):
        parse_profile_document(doc)

    doc2 = parse_profile_document(VEHICLE_PROFILE_PATH.read_text(encoding="utf-8")).model_dump(
        mode="json"
    )
    doc2["required_roles"] = ["hull"]
    with pytest.raises(ProfileContractError, match="Extra inputs are not permitted"):
        parse_profile_document(doc2)


def test_07_spec_requires_explicit_source_kind(
    valid_vehicle_spec_dict: dict, test_registry: ProfileRegistry
) -> None:
    # Missing source_kind key
    spec_no_sk = dict(valid_vehicle_spec_dict)
    del spec_no_sk["source_kind"]
    with pytest.raises(SpecInvalidError, match="requires explicit source_kind"):
        parse_asset_specification(spec_no_sk, registry=test_registry)

    # Explicit null source_kind
    spec_null_sk = dict(valid_vehicle_spec_dict, source_kind=None)
    with pytest.raises(SpecInvalidError, match="requires explicit source_kind"):
        parse_asset_specification(spec_null_sk, registry=test_registry)


def test_builtin_registry_does_not_expose_test_profiles() -> None:
    reg = builtin_registry()
    with pytest.raises(ProfileContractError, match="not registered"):
        reg.get("vehicle_test")
    with pytest.raises(ProfileContractError, match="not registered"):
        reg.get("character_test")

    available_ids = {p.profile_id for p in reg.available}
    assert available_ids == {"static_prop", "pickup", "modular_piece"}
