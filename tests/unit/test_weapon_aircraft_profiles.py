"""Private, unadvertised V0.7 weapon and aircraft profile contracts."""

from __future__ import annotations

from copy import deepcopy
from importlib.resources import files
from typing import Any

import pytest
import yaml

from gamefactory.core.domain.asset_contracts import (
    SpecInvalidError,
    parse_asset_specification_v07,
)
from gamefactory.core.domain.asset_profiles import (
    UNSUPPORTED_PROFILE_IDS,
    AssetProfileV07,
    ProfileContractError,
    ProfileRegistry,
    builtin_registry,
    parse_profile_document_v07,
)

_VIEWS = (
    "front",
    "rear",
    "left",
    "right",
    "side",
    "three_quarter",
    "three_quarter_front",
    "three_quarter_rear",
    "top",
)


def _candidate(name: str) -> tuple[AssetProfileV07, dict[str, Any]]:
    profile_path = files("gamefactory").joinpath(f"resources/profiles/{name}.yml")
    spec_path = files("gamefactory").joinpath(f"resources/specs/{name}_test.yml")
    profile = AssetProfileV07(parse_profile_document_v07(profile_path.read_text(encoding="utf-8")))
    data = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return profile, data


def _registry(profile: AssetProfileV07) -> ProfileRegistry:
    unsupported = tuple(
        profile_id for profile_id in UNSUPPORTED_PROFILE_IDS if profile_id != profile.profile_id
    )
    return ProfileRegistry(available=(), unsupported=unsupported, available_v07=(profile,))


@pytest.mark.parametrize(
    ("name", "roles", "moves", "sockets"),
    [
        (
            "weapon",
            {"receiver", "barrel", "grip", "bolt"},
            {"bolt"},
            {"muzzle", "grip_contact"},
        ),
        (
            "aircraft",
            {"fuselage", "wing", "tailplane", "propeller"},
            {"propeller"},
            {"nose_reference", "propeller_hub"},
        ),
    ],
)
def test_weapon_aircraft_candidates_are_private_and_typed(
    name: str, roles: set[str], moves: set[str], sockets: set[str]
) -> None:
    profile, raw_spec = _candidate(name)
    spec = parse_asset_specification_v07(raw_spec, registry=_registry(profile))
    assert profile.assembly is not None
    assert spec.parts is not None and spec.sockets is not None

    assert profile.profile_id == name
    assert profile.geometry_mode == "assembly"
    assert profile.accepted_source_kinds == ("local_operator_assembly",)
    assert set(profile.assembly.roles) == roles
    assert set(profile.assembly.required_roles) >= roles - {"bolt"}
    assert set(profile.document.review_views) == set(_VIEWS)
    assert profile.document.processing.lod0_required is True
    assert profile.document.processing.lod1_required is True
    assert profile.document.processing.rig_forbidden is True
    assert profile.document.processing.animation_forbidden is True
    assert profile.document.godot.body_kind == "static_body"
    assert profile.document.godot.require_ray_hit is True
    assert profile.assembly.required_sockets
    assert {row.socket_id for row in profile.assembly.required_sockets} == sockets
    assert spec.source_kind == "local_operator_assembly"
    bound_profile = spec.bound_profile()
    assert bound_profile.assembly is not None
    assert set(bound_profile.assembly.roles) == roles
    assert {part.part_id for part in spec.parts if part.pivot.motion.kind != "fixed"} == moves
    assert all(part.pivot.position_m != [0.0, 0.0, 0.0] for part in spec.parts)
    assert all(part.pivot.basis == "identity" for part in spec.parts)

    registry = builtin_registry()
    with pytest.raises(ProfileContractError):
        registry.get_v07(name, 1)
    with pytest.raises(SpecInvalidError):
        parse_asset_specification_v07(raw_spec)
    assert builtin_registry().available_v07 == ()


@pytest.mark.parametrize(
    ("name", "mutation", "message"),
    [
        ("weapon", "wrong_axis", "motion axis"),
        ("weapon", "wrong_socket_parent", "expected required role"),
        ("weapon", "missing_grip_contact", "required socket"),
        ("aircraft", "wrong_axis", "motion axis"),
        ("aircraft", "wrong_socket_parent", "expected required role"),
        ("aircraft", "missing_propeller_hub", "required socket"),
    ],
)
def test_weapon_aircraft_profile_and_spec_semantics_reject_mutations(
    name: str, mutation: str, message: str
) -> None:
    profile, raw_spec = _candidate(name)
    data = deepcopy(raw_spec)
    if mutation == "wrong_axis":
        moved = next(part for part in data["parts"] if part["pivot"]["motion"]["kind"] != "fixed")
        moved["pivot"]["motion"]["axis"] = [0.0, 1.0, 0.0]
    elif mutation == "wrong_socket_parent":
        data["sockets"][0]["parent_part"] = data["parts"][-1]["part_id"]
    elif mutation == "missing_grip_contact":
        data["sockets"] = [item for item in data["sockets"] if item["socket_id"] != "grip_contact"]
    else:
        data["sockets"] = [item for item in data["sockets"] if item["socket_id"] != "propeller_hub"]

    with pytest.raises(SpecInvalidError, match=message):
        parse_asset_specification_v07(data, registry=_registry(profile))


def test_weapon_optional_bolt_role_can_be_absent() -> None:
    profile, raw_spec = _candidate("weapon")
    raw_spec["parts"] = [part for part in raw_spec["parts"] if part["part_id"] != "bolt"]
    spec = parse_asset_specification_v07(raw_spec, registry=_registry(profile))
    assert spec.parts is not None
    assert {part.role for part in spec.parts} == {"receiver", "barrel", "grip"}
