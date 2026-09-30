"""ADR 0018: closed rule groups and capability-derived composition."""

from __future__ import annotations

import inspect

import pytest

from gamefactory.adapters.assets import validation_rules as vr
from gamefactory.adapters.assets.glb_validator import composition_for
from gamefactory.core.domain.asset_contracts import parse_asset_specification

CLOSED_GROUPS = {
    "core",
    "single_mesh",
    "parts",
    "orientation",
    "pivot",
    "sockets",
    "collider_box",
    "collider_capsule",
}


def test_rule_group_set_is_closed() -> None:
    assert set(vr.RULE_GROUPS) == CLOSED_GROUPS


def test_every_rule_belongs_to_exactly_one_group() -> None:
    seen: dict[object, str] = {}
    for group, rules in vr.RULE_GROUPS.items():
        for rule in rules:
            assert rule not in seen, f"{rule.__name__} is in {seen[rule]} and {group}"
            seen[rule] = group


def test_legacy_composition_is_exactly_the_legacy_groups() -> None:
    legacy_rules = [r for g in vr.LEGACY_GROUPS for r in vr.RULE_GROUPS[g]]
    assert sorted(r.__name__ for r in vr.LEGACY_COMPOSITION) == sorted(
        r.__name__ for r in legacy_rules
    )
    assert len(vr.LEGACY_COMPOSITION) == len(set(vr.LEGACY_COMPOSITION))


def test_selection_takes_no_profile_id() -> None:
    params = set(inspect.signature(vr.select_composition).parameters)
    assert params == {
        "geometry_mode",
        "collider_policy",
        "sockets_declared",
        "requires_normalization",
    }


@pytest.mark.parametrize(
    ("mode", "collider", "sockets", "normalize", "groups"),
    [
        ("single_mesh", "box", False, False, ("core", "single_mesh", "collider_box")),
        ("single_mesh", "capsule", False, False, ("core", "single_mesh", "collider_capsule")),
        (
            "assembly",
            "box",
            True,
            True,
            ("core", "parts", "orientation", "pivot", "sockets", "collider_box"),
        ),
        ("assembly", "box", False, True, ("core", "parts", "orientation", "pivot", "collider_box")),
        (
            "assembly",
            "capsule",
            False,
            False,
            ("core", "parts", "pivot", "collider_capsule"),
        ),
    ],
)
def test_composition_groups(
    mode: str, collider: str, sockets: bool, normalize: bool, groups: tuple[str, ...]
) -> None:
    composition = vr.select_composition(
        geometry_mode=mode,
        collider_policy=collider,
        sockets_declared=sockets,
        requires_normalization=normalize,
    )
    assert composition.groups == groups


def test_unknown_capabilities_are_rejected() -> None:
    with pytest.raises(ValueError):
        vr.select_composition(
            geometry_mode="assembly",
            collider_policy="convex",
            sockets_declared=False,
            requires_normalization=False,
        )
    with pytest.raises(ValueError):
        vr.select_composition(
            geometry_mode="skinned",
            collider_policy="box",
            sockets_declared=False,
            requires_normalization=False,
        )


@pytest.mark.parametrize(
    "path",
    [
        "src/gamefactory/resources/specs/prop_energy_crate_01.yml",
        "src/gamefactory/resources/specs/pickup_energy_cell_01.yml",
        "src/gamefactory/resources/fixtures/wall_panel_test.yml",
    ],
)
def test_historical_specs_use_the_legacy_composition(path: str) -> None:
    spec = parse_asset_specification(open(path, encoding="utf-8").read())
    composition = composition_for(spec)
    assert composition.rules == vr.LEGACY_COMPOSITION
    assert "skin_internal" not in composition.groups
