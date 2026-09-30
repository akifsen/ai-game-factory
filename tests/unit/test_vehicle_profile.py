"""Private candidate checks for the unadvertised V0.7 vehicle profile."""

from __future__ import annotations

from importlib.resources import files
from typing import Any

import pytest
import yaml

from gamefactory.core.domain.asset_contracts import parse_asset_specification_v07
from gamefactory.core.domain.asset_profiles import (
    UNSUPPORTED_PROFILE_IDS,
    AssetProfileV07,
    ProfileContractError,
    ProfileRegistry,
    SpecInvalidError,
    builtin_registry,
    parse_profile_document_v07,
)


def _candidate_profile() -> AssetProfileV07:
    path = files("gamefactory").joinpath("resources/profiles/vehicle.yml")
    return AssetProfileV07(parse_profile_document_v07(path.read_text(encoding="utf-8")))


def _candidate_registry(profile: AssetProfileV07) -> ProfileRegistry:
    # This deliberately test-local binding does not mutate builtin availability.
    unsupported = tuple(item for item in UNSUPPORTED_PROFILE_IDS if item != profile.profile_id)
    return ProfileRegistry(available=(), unsupported=unsupported, available_v07=(profile,))


def _candidate_spec_data() -> dict[str, Any]:
    path = files("gamefactory").joinpath("resources/specs/armored_vehicle_test.yml")
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_vehicle_profile_and_spec_bind_only_in_private_candidate_registry() -> None:
    profile = _candidate_profile()
    registry = _candidate_registry(profile)
    spec = parse_asset_specification_v07(_candidate_spec_data(), registry=registry)

    assert profile.profile_id == "vehicle"
    assert profile.geometry_mode == "assembly"
    assert profile.accepted_source_kinds == ("local_operator_assembly",)
    assert profile.document.review_views == [
        "front",
        "rear",
        "left",
        "right",
        "side",
        "three_quarter",
        "three_quarter_front",
        "three_quarter_rear",
        "top",
    ]
    assert profile.document.processing.lod0_required is True
    assert profile.document.processing.lod1_required is True
    assert profile.document.godot.body_kind == "static_body"
    assert profile.assembly.role_motion_constraints["hull"].kind == "fixed"
    assert profile.assembly.role_motion_constraints["turret"].axis == [0.0, 1.0, 0.0]
    assert profile.assembly.role_motion_constraints["barrel"].axis == [1.0, 0.0, 0.0]
    assert profile.assembly.required_sockets[0].socket_id == "muzzle"
    assert profile.assembly.required_sockets[0].placement == "forward_end"
    assert spec.bound_profile() is profile
    assert [(part.part_id, part.parent) for part in spec.parts] == [
        ("hull", "root"),
        ("turret", "hull"),
        ("barrel", "turret"),
    ]
    assert spec.parts[0].pivot.position_m == [0.0, 0.45, 0.21]
    assert spec.parts[1].pivot.position_m == [0.0, 0.45, 0.1]
    assert spec.parts[2].pivot.position_m == [0.0, 0.0, -0.82]
    assert spec.sockets[0].translation_m == [0.0, 0.0, -1.4]
    assert spec.collider.policy == "box"
    assert spec.source_kind == "local_operator_assembly"

    builtin = builtin_registry()
    with pytest.raises(ProfileContractError, match="vehicle is UNSUPPORTED"):
        builtin.get_v07("vehicle", 1)
    with pytest.raises(SpecInvalidError, match="vehicle is UNSUPPORTED"):
        parse_asset_specification_v07(_candidate_spec_data())


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("wrong_axis", "motion axis"),
        ("wrong_socket_parent", "expected required role"),
        ("wrong_socket_placement", "placement"),
    ],
)
def test_vehicle_candidate_contract_rejects_profile_semantic_mutations(
    mutation: str, message: str
) -> None:
    profile = _candidate_profile()
    data = _candidate_spec_data()
    if mutation == "wrong_axis":
        data["parts"][2]["pivot"]["motion"]["axis"] = [0.0, 1.0, 0.0]
    elif mutation == "wrong_socket_parent":
        data["sockets"][0]["parent_part"] = "hull"
    elif mutation == "wrong_socket_placement":
        data["sockets"][0]["placement"] = "arbitrary"

    with pytest.raises(SpecInvalidError, match=message):
        parse_asset_specification_v07(data, registry=_candidate_registry(profile))
