"""V0.8-1 internal skin decoder/validator (ADR 0019)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gamefactory.adapters.assets.glb_validator import validate_glb
from gamefactory.adapters.assets.internal_skin import validate_internal_skinned_glb
from gamefactory.adapters.fakes import assembly_generator as ag
from gamefactory.adapters.fakes.humanoid_skin_fixture import (
    Variant,
    build_humanoid_skinned_glb,
    humanoid_fixture_sha256,
    mutate_glb_document,
)
from gamefactory.core.domain.asset_contracts import parse_asset_specification_v07
from gamefactory.core.domain.errors import SpecInvalidError
from gamefactory.core.domain.internal_skin_contract import (
    InternalSkinContractError,
    load_internal_skin_contract,
    parse_internal_skin_contract,
)

CONTRACT = load_internal_skin_contract()

# Pin positive fixture bytes (semantic contract); update only when fixture semantics change.
PINNED_POSITIVE_SHA256 = "00d48ac499bc8dc64532ea9aab535791afe4b144578799881bb95d5d9b43886e"


def _write(tmp_path: Path, variant: Variant) -> Path:
    path = tmp_path / f"{variant}.glb"
    path.write_bytes(build_humanoid_skinned_glb(variant))
    return path


def test_humanoid_fixture_is_deterministic() -> None:
    a = build_humanoid_skinned_glb("positive")
    b = build_humanoid_skinned_glb("positive")
    assert a == b
    digest = humanoid_fixture_sha256()
    assert digest == humanoid_fixture_sha256()
    assert digest == PINNED_POSITIVE_SHA256


def test_positive_internal_skin_passes(tmp_path: Path) -> None:
    result = validate_internal_skinned_glb(_write(tmp_path, "positive"), CONTRACT)
    assert result.status.value == "PASS"
    assert not any(f.severity.value == "FAIL" for f in result.findings)


@pytest.mark.parametrize(
    ("variant", "rule_id"),
    [
        ("weight_sum", "internal_skin.weights.normalized"),
        ("five_influences", "internal_skin.parse"),
        ("missing_bone", "internal_skin.topology"),
        ("wrong_parent", "internal_skin.topology"),
        ("inverse_bind_mismatch", "internal_skin.inverse_bind"),
        ("rest_mismatch", "internal_skin.rest_pose"),
        ("invalid_joint_reference", "internal_skin.parse"),
        ("unweighted_vertex", "internal_skin.weights.coverage"),
        ("negative_weight", "internal_skin.parse"),
        ("nonfinite_weight", "internal_skin.parse"),
        ("malformed_accessor", "internal_skin.parse"),
        ("animation_present", "internal_skin.parse"),
    ],
)
def test_internal_skin_negative_fixtures(tmp_path: Path, variant: Variant, rule_id: str) -> None:
    result = validate_internal_skinned_glb(_write(tmp_path, variant), CONTRACT)
    assert result.status.value == "FAIL"
    assert any(f.rule_id == rule_id and f.severity.value == "FAIL" for f in result.findings)


@pytest.mark.parametrize(
    ("mutator", "rule_id"),
    [
        (lambda d: d["buffers"][0].update({"uri": "outside.bin"}), "internal_skin.parse"),
        (
            lambda d: d["bufferViews"][-1].update({"byteLength": 4}),
            "internal_skin.parse",
        ),
        (
            lambda d: d["meshes"][0]["primitives"][0].update({"targets": [{"POSITION": 0}]}),
            "internal_skin.parse",
        ),
        (
            lambda d: d["meshes"][0]["primitives"][0]["attributes"].update({"WEIGHTS_1": 2}),
            "internal_skin.parse",
        ),
        (
            lambda d: d["meshes"][0]["primitives"][0]["attributes"].update({"JOINTS_1": 1}),
            "internal_skin.parse",
        ),
        (
            lambda d: d["accessors"][0].update({"count": -1}),
            "internal_skin.parse",
        ),
        (
            lambda d: d["skins"][0]["joints"].__setitem__(1, 1.9),
            "internal_skin.parse",
        ),
        (
            lambda d: d["skins"][0]["joints"].__setitem__(1, "1"),
            "internal_skin.parse",
        ),
        (
            lambda d: d["skins"][0]["joints"].__setitem__(1, True),
            "internal_skin.parse",
        ),
        (
            lambda d: d["scenes"][0]["nodes"].__setitem__(0, 0.5),
            "internal_skin.parse",
        ),
        (
            lambda d: d["scenes"][0]["nodes"].__setitem__(0, "0"),
            "internal_skin.parse",
        ),
        (
            lambda d: d["scenes"][0]["nodes"].__setitem__(0, True),
            "internal_skin.parse",
        ),
        (
            lambda d: d.update({"scene": 0.0}),
            "internal_skin.parse",
        ),
        (
            lambda d: d["skins"][0].update({"skeleton": 1.9}),
            "internal_skin.parse",
        ),
        (
            lambda d: d["skins"][0].update({"skeleton": "1"}),
            "internal_skin.parse",
        ),
        (
            lambda d: d["skins"][0].update({"inverseBindMatrices": 5.0}),
            "internal_skin.parse",
        ),
        (
            lambda d: d["nodes"][-1].update({"mesh": 0.0}),
            "internal_skin.parse",
        ),
        (
            lambda d: d["meshes"][0]["primitives"][0]["attributes"].update({"POSITION": "0"}),
            "internal_skin.parse",
        ),
    ],
)
def test_decoder_rejects_malformed_glb_mutations(
    tmp_path: Path, mutator: object, rule_id: str
) -> None:
    glb = mutate_glb_document(build_humanoid_skinned_glb("positive"), mutator)
    path = tmp_path / "mutated.glb"
    path.write_bytes(glb)
    result = validate_internal_skinned_glb(path, CONTRACT)
    assert result.status.value == "FAIL"
    assert any(f.rule_id == rule_id and f.severity.value == "FAIL" for f in result.findings)


def _minimal_contract_dict() -> dict:
    identity = {"y_axis": [0.0, 1.0, 0.0], "neg_z_axis": [0.0, 0.0, -1.0]}
    return {
        "contract_id": "x",
        "joint_count_max": 12,
        "rest_pose": "T",
        "asset_root_name": "HumanoidRoot",
        "bones": [{"name": "Hips", "parent": "HumanoidRoot"}],
        "rest_joint_origins": {"Hips": [0, 1, 0]},
        "rest_joint_bases": {"Hips": identity},
        "deformation_oracle": {
            "pose_bone": "Hips",
            "rotation_type": "axis_angle",
            "rotation_axis": [0, 0, 1],
            "rotation_degrees": 15,
            "affected_region": {"min": [0, 0, 0], "max": [1, 1, 1]},
            "unaffected_region": {"min": [0, 0, 0], "max": [1, 1, 1]},
            "min_affected_displacement": 0.01,
            "max_affected_displacement": 0.45,
            "max_unaffected_displacement": 0.01,
        },
    }


def test_contract_parser_rejects_malformed() -> None:
    base = _minimal_contract_dict()
    base["deformation_oracle"]["rotation_axis"] = [0, 0, 0]
    with pytest.raises(InternalSkinContractError, match="rotation_axis"):
        parse_internal_skin_contract(base)

    bad_pose = _minimal_contract_dict()
    bad_pose["deformation_oracle"]["pose_bone"] = "MissingBone"
    with pytest.raises(InternalSkinContractError, match="pose_bone"):
        parse_internal_skin_contract(bad_pose)

    zero_rot = _minimal_contract_dict()
    zero_rot["deformation_oracle"]["rotation_degrees"] = 0
    with pytest.raises(InternalSkinContractError, match="rotation_degrees"):
        parse_internal_skin_contract(zero_rot)


def test_rotated_leaf_rest_fails_basis_with_consistent_ibm(tmp_path: Path) -> None:
    result = validate_internal_skinned_glb(_write(tmp_path, "rotated_leaf_rest"), CONTRACT)
    assert result.status.value == "FAIL"
    assert any(f.rule_id == "internal_skin.rest_pose" for f in result.findings)
    assert not any(
        f.rule_id == "internal_skin.inverse_bind"
        for f in result.findings
        if f.severity.value == "FAIL"
    )


def test_production_character_still_rejects_skinned_glb(tmp_path: Path) -> None:
    spec = parse_asset_specification_v07(ag.character_spec(ag.HUMANOID_CHARACTER))
    path = _write(tmp_path, "positive")
    result = validate_glb(path, spec)
    assert result.status.value == "FAIL"
    assert any(f.rule_id == "glb.parse" for f in result.findings)


def test_rigged_character_profile_still_unsupported() -> None:
    data = ag.character_spec(ag.HUMANOID_CHARACTER)
    data["profile"] = "rigged_character"
    with pytest.raises(SpecInvalidError, match="UNSUPPORTED"):
        parse_asset_specification_v07(data)
    assert "rigged_character" not in {p.profile_id for p in builtin_registry().available_v07}


def test_contract_loads_from_package_resources() -> None:
    assert load_internal_skin_contract().contract_id == "humanoid_12bone_v1"


from gamefactory.core.domain.asset_profiles import builtin_registry  # noqa: E402
