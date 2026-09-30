"""The Python side never trusts a V0.7 Godot observation's own status (ADR 0013/0015)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.dcc.blender_processor import BlenderAssetProcessor
from gamefactory.adapters.fakes import assembly_generator as ag
from gamefactory.core.domain.asset_contracts import parse_asset_specification_v07
from gamefactory.core.domain.asset_profiles import render_scene_contract
from gamefactory.core.domain.errors import RuntimeValidationFailedError
from gamefactory.workflows.asset_production import (
    runtime_contract_v07,
    validate_observation_v07,
)

VEHICLE = parse_asset_specification_v07(ag.assembly_spec(ag.VEHICLE_TANK))
CHARACTER = parse_asset_specification_v07(ag.character_spec(ag.HUMANOID_CHARACTER))


def _vehicle_observation() -> dict[str, Any]:
    return {
        "schema_version": "asset-runtime-observation-0.7.0",
        "geometry_mode": "assembly",
        "collider_policy": "box",
        "collision_shape_class": "BoxShape3D",
        "hierarchy": {
            "ok": True,
            "parts": [{"part_id": p, "ok": True} for p in ("hull", "turret", "barrel")],
            "sockets": [{"socket_id": "muzzle", "ok": True, "node_class": "Marker3D"}],
        },
        "articulation": [
            {
                "part_id": part,
                "motion": "revolute",
                "moved": True,
                "pivot_ok": True,
                "descendants_rigid": True,
                "others_unchanged": True,
                "restored": True,
            }
            for part in ("turret", "barrel")
        ],
    }


def _character_observation() -> dict[str, Any]:
    return {
        "schema_version": "asset-runtime-observation-0.7.0",
        "geometry_mode": "single_mesh",
        "collider_policy": "capsule",
        "collision_shape_class": "CapsuleShape3D",
        "capsule": {"ok": True, "observed": {"radius_m": 0.2800000011920929, "height_m": 1.8}},
    }


def test_complete_observations_are_accepted() -> None:
    validate_observation_v07(_vehicle_observation(), VEHICLE)
    validate_observation_v07(_character_observation(), CHARACTER)


@pytest.mark.parametrize(
    ("edit", "match"),
    [
        (lambda o: o.update(schema_version="asset-runtime-observation-0.5.0"), "0.7.0"),
        (lambda o: o.update(collision_shape_class="CapsuleShape3D"), "BoxShape3D"),
        (lambda o: o["hierarchy"].update(ok=False), "hierarchy"),
        (lambda o: o["hierarchy"]["parts"].pop(), "every declared part"),
        (lambda o: o["hierarchy"]["sockets"][0].update(node_class="Node3D"), "socket"),
        (lambda o: o["articulation"].pop(), "every movable part"),
        (lambda o: o["articulation"][0].update(pivot_ok=False), "articulation failed"),
        (lambda o: o["articulation"][1].update(motion="prismatic"), "articulation failed"),
        (lambda o: o["articulation"][0].update(others_unchanged=False), "articulation failed"),
    ],
)
def test_incomplete_assembly_observation_is_rejected(edit: Any, match: str) -> None:
    observation = _vehicle_observation()
    edit(observation)
    with pytest.raises(RuntimeValidationFailedError, match=match):
        validate_observation_v07(observation, VEHICLE)


@pytest.mark.parametrize(
    "edit",
    [
        lambda o: o["capsule"].update(ok=False),
        lambda o: o["capsule"]["observed"].update(radius_m=0.3),
        lambda o: o.update(collision_shape_class="BoxShape3D"),
    ],
)
def test_wrong_capsule_observation_is_rejected(edit: Any) -> None:
    observation = copy.deepcopy(_character_observation())
    edit(observation)
    with pytest.raises(RuntimeValidationFailedError):
        validate_observation_v07(observation, CHARACTER)


def test_runtime_contract_carries_hierarchy_sockets_and_tolerances() -> None:
    block = runtime_contract_v07(VEHICLE)
    assert block["geometry_mode"] == "assembly"
    assert [row["node"] for row in block["hierarchy"]] == [
        "PART_hull",
        "PART_turret",
        "PART_barrel",
    ]
    assert block["hierarchy"][1]["parent_node"] == "PART_hull"
    assert block["sockets"][0]["rest_forward"] == [0.0, 0.0, -1.0]
    assert block["pivot_tolerance_m"] == 0.005
    capsule = runtime_contract_v07(CHARACTER)
    assert capsule["capsule"] == {"radius_m": 0.28, "height_m": 1.8, "axis": "+Y"}
    assert capsule["hierarchy"] == []


def test_scene_contract_for_capsule_and_unchanged_box_wrapper() -> None:
    profile = CHARACTER.bound_profile()
    text = render_scene_contract(profile.scene_contract(CHARACTER))
    assert '[sub_resource type="CapsuleShape3D" id="capsule"]' in text
    assert 'shape = SubResource("capsule")' in text
    legacy = {
        "root": "Node3D",
        "visual": "Visual",
        "physics": "StaticBody3D",
        "collision": "CollisionShape3D",
    }
    assert "CapsuleShape3D" not in render_scene_contract(legacy)
    assert render_scene_contract({**legacy, "shape": "BoxShape3D"}) == render_scene_contract(legacy)


def test_blender_processing_refuses_assemblies(tmp_path: Path) -> None:
    raw = tmp_path / "raw.glb"
    raw.write_bytes(ag.assembly_glb(ag.VEHICLE_TANK))
    with pytest.raises(ValueError, match="single-mesh only"):
        BlenderAssetProcessor("/nonexistent/blender").process_asset(
            raw, tmp_path / "out.glb", VEHICLE
        )
