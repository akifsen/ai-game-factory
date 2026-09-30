"""V0.7 assembly/character validation: positive and negative fixtures (ADR 0013-0018).

Every fixture is generated deterministically from one design, and each negative
fixture changes exactly one thing so its own rule id reports it.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.assets.assembly_processor import (
    AssemblyProcessingError,
    normalize_assembly,
)
from gamefactory.adapters.assets.glb_validator import composition_for, validate_glb
from gamefactory.adapters.assets.validation_rules import NormalizationRecord
from gamefactory.adapters.fakes import assembly_generator as ag
from gamefactory.core.domain import transforms as tf
from gamefactory.core.domain.assembly_source import (
    AssemblySourceRegistration,
    parse_source_registration,
)
from gamefactory.core.domain.asset_contracts import (
    AssetSpecificationV07,
    AssetValidationResult,
    parse_asset_specification_v07,
)
from gamefactory.core.domain.asset_profiles import (
    ProfileContractError,
    builtin_registry,
    parse_profile_document_v07,
)
from gamefactory.core.domain.errors import SpecInvalidError

ASSEMBLIES = sorted(ag.ASSEMBLY_DESIGNS)


def _spec(design: ag.AssemblyDesign) -> AssetSpecificationV07:
    return parse_asset_specification_v07(ag.assembly_spec(design))


def _process(
    tmp_path: Path, design: ag.AssemblyDesign, glb: bytes, source_front: str
) -> tuple[AssetSpecificationV07, Path, NormalizationRecord]:
    spec = _spec(design)
    source = tmp_path / f"{design.asset_id}-{source_front}-source.glb"
    source.write_bytes(glb)
    registration = parse_source_registration(
        ag.source_registration(design, glb, source_front=source_front)
    )
    registration.check_against_spec(spec)
    out = tmp_path / f"{design.asset_id}-{source_front}-processed.glb"
    result = normalize_assembly(
        source,
        registration,
        out,
        asset_id=spec.asset_id,
        processing_contract=spec.bound_profile().processing_contract(spec),
    )
    return spec, out, result.record


def _failing(result: AssetValidationResult) -> set[str]:
    return {f.rule_id for f in result.findings if f.severity.value == "FAIL"}


def _validate_edit(
    tmp_path: Path, design: ag.AssemblyDesign, edit: ag.GLBEdit
) -> AssetValidationResult:
    glb = edit.build()
    spec, out, record = _process(tmp_path, design, glb, "-Z")
    return validate_glb(out, spec, normalization=record)


# --- positive fixtures -------------------------------------------------------------


@pytest.mark.parametrize("asset_id", ASSEMBLIES)
@pytest.mark.parametrize("source_front", ["-Z", "+Z"])
def test_positive_assembly_passes_every_group(
    tmp_path: Path, asset_id: str, source_front: str
) -> None:
    design = ag.ASSEMBLY_DESIGNS[asset_id]
    spec, out, record = _process(
        tmp_path, design, ag.assembly_glb(design, source_front=source_front), source_front
    )
    result = validate_glb(out, spec, normalization=record)
    assert result.status.value == "PASS", [f for f in result.findings if f.severity.value != "PASS"]
    rule_ids = [f.rule_id for f in result.findings]
    for required in (
        "part.missing",
        "part.parent",
        "pivot.collapsed",
        "pivot.position",
        "pivot.orientation",
        "pivot.axis",
        "orientation.source_front",
        "collider.geometry",
    ):
        assert required in rule_ids
    assert ("socket.orientation" in rule_ids) == bool(design.sockets)
    assert "orientation.identity" not in rule_ids
    assert record.normalization_applied == (source_front == "+Z")


def test_minus_z_source_is_retained_byte_for_byte(tmp_path: Path) -> None:
    glb = ag.assembly_glb(ag.VEHICLE_TANK)
    _, out, record = _process(tmp_path, ag.VEHICLE_TANK, glb, "-Z")
    assert out.read_bytes() == glb
    assert record.as_dict()["normalization_transform"]["quaternion_xyzw"] == [0.0, 0.0, 0.0, 1.0]


def test_plus_z_source_gets_exactly_one_root_rotation(tmp_path: Path) -> None:
    glb = ag.assembly_glb(ag.VEHICLE_TANK, source_front="+Z")
    _, out, record = _process(tmp_path, ag.VEHICLE_TANK, glb, "+Z")
    assert out.read_bytes() != glb
    assert record.as_dict() == {
        "source_front": "+Z",
        "normalization_applied": True,
        "normalization_transform": {
            "quaternion_xyzw": [0.0, 1.0, 0.0, 0.0],
            "matrix": [
                [-1.0, 0.0, 0.0, 0.0],
                [0.0, 1.0, 0.0, 0.0],
                [0.0, 0.0, -1.0, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ],
        },
        "resulting_front": "-Z",
    }
    # The normalized result is the canonical -Z assembly, node for node.
    canonical = ag.GLBEdit.of(ag.assembly_glb(ag.VEHICLE_TANK))
    produced = ag.GLBEdit.of(out.read_bytes())
    for name in ("PART_hull", "PART_turret", "PART_barrel", "SOCKET_muzzle"):
        a, b = produced.node(name), canonical.node(name)
        assert tf.matrices_close(
            tf.trs_matrix(a["translation"], a["rotation"], a["scale"]),
            tf.trs_matrix(b["translation"], b["rotation"], b["scale"]),
            1e-6,
        ), name


def test_generation_is_deterministic() -> None:
    for design in ag.ASSEMBLY_DESIGNS.values():
        assert ag.assembly_glb(design) == ag.assembly_glb(design)
        assert ag.assembly_spec(design) == ag.assembly_spec(design)
    assert ag.character_glb(ag.HUMANOID_CHARACTER) == ag.character_glb(ag.HUMANOID_CHARACTER)


def test_humanoid_character_capsule_fixture_passes(tmp_path: Path) -> None:
    design = ag.HUMANOID_CHARACTER
    assert 0.25 <= design.capsule_radius_m <= 0.35
    spec = parse_asset_specification_v07(ag.character_spec(design))
    path = tmp_path / "character.glb"
    path.write_bytes(ag.character_glb(design))
    result = validate_glb(path, spec)
    assert result.status.value == "PASS"
    assert composition_for(spec).groups == ("core", "single_mesh", "collider_capsule")
    assert {"collider.capsule.shape", "collider.capsule.fit"} <= {
        f.rule_id for f in result.findings
    }


# --- negative fixtures: pivots -----------------------------------------------------


def test_collapsed_pivot_assembly_fails_pivot_collapsed(tmp_path: Path) -> None:
    glb = ag.assembly_glb(ag.VEHICLE_TANK, collapsed=True)
    spec, out, record = _process(tmp_path, ag.VEHICLE_TANK, glb, "-Z")
    result = validate_glb(out, spec, normalization=record)
    failing = _failing(result)
    assert "pivot.collapsed" in failing
    # The hierarchy itself survived, as in the spike: the tree rules pass.
    assert not failing & {"part.missing", "part.parent", "part.unexpected", "socket.missing"}


def test_right_position_wrong_basis_fails_only_pivot_orientation(tmp_path: Path) -> None:
    edit = ag.GLBEdit.of(ag.assembly_glb(ag.VEHICLE_TANK))
    edit.set("PART_turret", rotation=ag.rotate_about_y(30.0))
    failing = _failing(_validate_edit(tmp_path, ag.VEHICLE_TANK, edit))
    assert "pivot.orientation" in failing
    assert not failing & {"pivot.position", "pivot.axis", "pivot.collapsed"}


def test_right_position_and_basis_wrong_axis_fails_only_pivot_axis(tmp_path: Path) -> None:
    edit = ag.GLBEdit.of(ag.assembly_glb(ag.VEHICLE_TANK))
    edit.node("PART_turret")["extras"]["gf_axis"] = [0.0, 0.0, 1.0]
    failing = _failing(_validate_edit(tmp_path, ag.VEHICLE_TANK, edit))
    assert failing == {"pivot.axis"}


def test_wrong_motion_kind_fails_pivot_axis(tmp_path: Path) -> None:
    edit = ag.GLBEdit.of(ag.assembly_glb(ag.VEHICLE_TANK))
    edit.node("PART_barrel")["extras"] = {"gf_motion": "fixed"}
    assert _failing(_validate_edit(tmp_path, ag.VEHICLE_TANK, edit)) == {"pivot.axis"}


# --- negative fixtures: part tree --------------------------------------------------


def test_part_under_wrong_parent_fails_part_parent(tmp_path: Path) -> None:
    edit = ag.GLBEdit.of(ag.assembly_glb(ag.VEHICLE_TANK)).reparent("PART_barrel", "PART_hull")
    assert "part.parent" in _failing(_validate_edit(tmp_path, ag.VEHICLE_TANK, edit))


@pytest.mark.parametrize(
    "scale", [[1.0, 2.0, 1.0], [0.0, 0.0, 0.0], [-1.0, -1.0, -1.0], [1.0, 1.0, -1.0]]
)
def test_non_uniform_zero_or_negative_part_scale_fails_part_scale(
    tmp_path: Path, scale: list[float]
) -> None:
    edit = ag.GLBEdit.of(ag.assembly_glb(ag.VEHICLE_TANK)).set("PART_turret", scale=scale)
    result = _validate_edit(tmp_path, ag.VEHICLE_TANK, edit)
    assert "part.scale" in _failing(result) or any(
        f.rule_id == "glb.parse" and f.severity.value == "FAIL" for f in result.findings
    )


def test_missing_and_unexpected_parts(tmp_path: Path) -> None:
    edit = ag.GLBEdit.of(ag.assembly_glb(ag.WEAPON_RIFLE))
    edit.set("PART_magazine", name="PART_clip")
    failing = _failing(_validate_edit(tmp_path, ag.WEAPON_RIFLE, edit))
    assert {"part.missing", "part.unexpected"} <= failing


def test_non_identity_root_is_rejected_by_processing(tmp_path: Path) -> None:
    edit = ag.GLBEdit.of(ag.assembly_glb(ag.VEHICLE_TANK))
    edit.set("ROOT", rotation=ag.rotate_about_y(90.0))
    with pytest.raises(AssemblyProcessingError, match="ROOT must be identity"):
        _validate_edit(tmp_path, ag.VEHICLE_TANK, edit)


# --- negative fixtures: sockets ----------------------------------------------------


def test_socket_with_right_name_wrong_orientation(tmp_path: Path) -> None:
    edit = ag.GLBEdit.of(ag.assembly_glb(ag.VEHICLE_TANK))
    edit.set("SOCKET_muzzle", rotation=ag.rotate_about_y(90.0))
    failing = _failing(_validate_edit(tmp_path, ag.VEHICLE_TANK, edit))
    assert "socket.orientation" in failing
    assert not failing & {"socket.position", "socket.parent", "socket.missing"}


def test_socket_under_wrong_parent(tmp_path: Path) -> None:
    edit = ag.GLBEdit.of(ag.assembly_glb(ag.VEHICLE_TANK)).reparent("SOCKET_muzzle", "PART_turret")
    assert "socket.parent" in _failing(_validate_edit(tmp_path, ag.VEHICLE_TANK, edit))


def test_socket_not_at_the_forward_end(tmp_path: Path) -> None:
    design = ag.VEHICLE_TANK
    moved = ag.AssemblyDesign(
        **{
            **design.__dict__,
            "sockets": (ag.SocketDesign("muzzle", "barrel", (0.0, 0.0, -0.4)),),
        }
    )
    glb = ag.assembly_glb(moved)
    spec, out, record = _process(tmp_path, moved, glb, "-Z")
    failing = _failing(validate_glb(out, spec, normalization=record))
    assert failing == {"socket.placement"}


@pytest.mark.parametrize("defect", ["scale", "mesh", "children"])
def test_socket_with_scale_mesh_or_children_fails_socket_structure(
    tmp_path: Path, defect: str
) -> None:
    edit = ag.GLBEdit.of(ag.assembly_glb(ag.VEHICLE_TANK))
    if defect == "scale":
        edit.set("SOCKET_muzzle", scale=[2.0, 2.0, 2.0])
    elif defect == "mesh":
        edit.set("SOCKET_muzzle", mesh=0)
        edit.node("SM_vehicle_tank_test_hull_LOD0").pop("mesh")
    else:
        edit.document["nodes"].append({"name": "SOCKET_extra_child"})
        edit.node("SOCKET_muzzle")["children"] = [len(edit.document["nodes"]) - 1]
    assert "socket.structure" in _failing(_validate_edit(tmp_path, ag.VEHICLE_TANK, edit))


def test_missing_declared_socket(tmp_path: Path) -> None:
    edit = ag.GLBEdit.of(ag.assembly_glb(ag.WEAPON_RIFLE)).set("SOCKET_muzzle", name="EMPTY")
    failing = _failing(_validate_edit(tmp_path, ag.WEAPON_RIFLE, edit))
    assert {"socket.missing", "part.unexpected"} <= failing


# --- negative fixtures: source_front ------------------------------------------------


def test_missing_source_front_is_rejected_at_registration() -> None:
    glb = ag.assembly_glb(ag.VEHICLE_TANK)
    data = ag.source_registration(ag.VEHICLE_TANK, glb, source_front=None)
    with pytest.raises(SpecInvalidError, match="requires source_front"):
        parse_source_registration(data)


@pytest.mark.parametrize("value", ["+X", "-z", "Z", "", None, 180])
def test_any_other_source_front_is_rejected(value: Any) -> None:
    glb = ag.assembly_glb(ag.VEHICLE_TANK)
    data = ag.source_registration(ag.VEHICLE_TANK, glb)
    data["source_front"] = value
    with pytest.raises(SpecInvalidError, match="source_front"):
        parse_source_registration(data)


def test_result_with_an_extra_rotation_fails_orientation_source_front(tmp_path: Path) -> None:
    glb = ag.assembly_glb(ag.VEHICLE_TANK, source_front="+Z")
    spec, out, record = _process(tmp_path, ag.VEHICLE_TANK, glb, "+Z")
    tampered = ag.GLBEdit.of(out.read_bytes())
    hull = tampered.node("PART_hull")
    extra = tf.mat_mul(
        tf.trs_matrix(rotation=ag.rotate_about_y(10.0)),
        tf.trs_matrix(hull["translation"], hull["rotation"], hull["scale"]),
    )
    hull["rotation"] = list(tf.rotation_to_quat(tf.rotation_of(extra)))
    out.write_bytes(tampered.build())
    assert "orientation.source_front" in _failing(validate_glb(out, spec, normalization=record))


def test_normalization_record_is_required_for_assemblies(tmp_path: Path) -> None:
    glb = ag.assembly_glb(ag.VEHICLE_TANK)
    spec, out, _ = _process(tmp_path, ag.VEHICLE_TANK, glb, "-Z")
    assert "orientation.source_front" in _failing(validate_glb(out, spec))


def test_record_that_disagrees_with_its_source_front_fails(tmp_path: Path) -> None:
    glb = ag.assembly_glb(ag.VEHICLE_TANK)
    spec, out, record = _process(tmp_path, ag.VEHICLE_TANK, glb, "-Z")
    forged = NormalizationRecord("-Z", True, (0.0, 1.0, 0.0, 0.0), "-Z", record.source)
    assert "orientation.source_front" in _failing(validate_glb(out, spec, normalization=forged))


def test_plus_z_source_registered_as_minus_z_is_caught_by_the_socket_forward(
    tmp_path: Path,
) -> None:
    glb = ag.assembly_glb(ag.VEHICLE_TANK, source_front="+Z")
    spec, out, record = _process(tmp_path, ag.VEHICLE_TANK, glb, "-Z")
    assert "socket.orientation" in _failing(validate_glb(out, spec, normalization=record))


# --- negative fixtures: registration ----------------------------------------------


def test_registration_rejects_paid_true_and_mismatched_maps() -> None:
    glb = ag.assembly_glb(ag.VEHICLE_TANK)
    data = ag.source_registration(ag.VEHICLE_TANK, glb)
    paid = {**data, "paid": True}
    with pytest.raises(SpecInvalidError, match="never paid"):
        parse_source_registration(paid)
    spec = _spec(ag.VEHICLE_TANK)
    wrong = copy.deepcopy(data)
    wrong["part_map"][1]["parent"] = "root"
    with pytest.raises(SpecInvalidError, match="part_map"):
        parse_source_registration(wrong).check_against_spec(spec)
    no_socket = {**data, "socket_map": []}
    with pytest.raises(SpecInvalidError, match="socket_map"):
        parse_source_registration(no_socket).check_against_spec(spec)
    registration: AssemblySourceRegistration = parse_source_registration(data)
    with pytest.raises(SpecInvalidError, match="SHA-256"):
        registration.check_artifact(glb[:-4] + b"\x01\x00\x00\x00")
    assert registration.artifact_sha256 == hashlib.sha256(glb).hexdigest()


# --- negative fixtures: character, rigs and capsules ------------------------------


def test_rigged_or_skinned_glb_under_character_fails(tmp_path: Path) -> None:
    spec = parse_asset_specification_v07(ag.character_spec(ag.HUMANOID_CHARACTER))
    for key, value in (
        ("skins", [{"joints": [0]}]),
        ("animations", [{"channels": [], "samplers": []}]),
    ):
        edit = ag.GLBEdit.of(ag.character_glb(ag.HUMANOID_CHARACTER))
        edit.document[key] = value
        path = tmp_path / f"{key}.glb"
        path.write_bytes(edit.build())
        result = validate_glb(path, spec)
        assert result.status.value == "FAIL"
        assert any(f.rule_id == "glb.parse" and f.severity.value == "FAIL" for f in result.findings)


def test_rigged_character_cannot_be_selected_by_a_production_spec() -> None:
    data = ag.character_spec(ag.HUMANOID_CHARACTER)
    data["profile"] = "rigged_character"
    with pytest.raises(SpecInvalidError, match="UNSUPPORTED"):
        parse_asset_specification_v07(data)
    assert "rigged_character" not in {p.profile_id for p in builtin_registry().available_v07}


@pytest.mark.parametrize(
    ("radius", "height", "match"),
    [(0.09, 1.8, "radius_m must be >= 0.10"), (0.3, 0.6, "strictly greater")],
)
def test_capsule_structural_limits(radius: float, height: float, match: str) -> None:
    data = ag.character_spec(ag.HUMANOID_CHARACTER)
    data["collider"]["capsule"] = {"radius_m": radius, "height_m": height}
    with pytest.raises(SpecInvalidError, match=match):
        parse_asset_specification_v07(data)


def test_capsule_that_does_not_fit_the_bounds() -> None:
    data = ag.character_spec(ag.HUMANOID_CHARACTER)
    data["collider"]["capsule"] = {"radius_m": 0.45, "height_m": 1.8}
    with pytest.raises(SpecInvalidError, match="exceeds max horizontal bounds"):
        parse_asset_specification_v07(data)


def test_capsule_fit_is_also_checked_against_measured_geometry(tmp_path: Path) -> None:
    spec = parse_asset_specification_v07(ag.character_spec(ag.HUMANOID_CHARACTER))
    narrow = ag.CharacterDesign("humanoid_character_test", 0.3, 1.8, 0.3, 0.28, 1.8)
    path = tmp_path / "narrow.glb"
    path.write_bytes(ag.character_glb(narrow))
    assert "collider.capsule.fit" in _failing(validate_glb(path, spec))


def test_capsule_glb_must_not_carry_a_collider_mesh(tmp_path: Path) -> None:
    spec = parse_asset_specification_v07(ag.character_spec(ag.HUMANOID_CHARACTER))
    edit = ag.GLBEdit.of(ag.character_glb(ag.HUMANOID_CHARACTER, include_lod1=True))
    edit.set("SM_humanoid_character_test_LOD1", name="COL_humanoid_character_test")
    path = tmp_path / "col.glb"
    path.write_bytes(edit.build())
    assert "collider.capsule.shape" in _failing(validate_glb(path, spec))


# --- negative fixtures: views and profile selection -------------------------------


def test_profile_with_an_unknown_view_is_rejected() -> None:
    document = builtin_registry().get_v07("vehicle", 1).document.model_dump(mode="json")
    document["review_views"] = ["front", "underside"]
    with pytest.raises(ProfileContractError, match="review view is not implemented"):
        parse_profile_document_v07(document)


def test_assembly_spec_cannot_be_provider_generated() -> None:
    data = ag.assembly_spec(ag.VEHICLE_TANK)
    data["source_kind"] = "provider_generated"
    with pytest.raises(SpecInvalidError, match="local_operator_assembly"):
        parse_asset_specification_v07(data)


@pytest.mark.parametrize(
    ("profile", "role", "kind", "axis"),
    [
        ("vehicle", "turret", "revolute", [1.0, 0.0, 0.0]),
        ("vehicle", "turret", "prismatic", [0.0, 1.0, 0.0]),
    ],
)
def test_profile_role_motion_constraints(
    profile: str, role: str, kind: str, axis: list[float]
) -> None:
    data = ag.assembly_spec(ag.VEHICLE_TANK)
    part = next(p for p in data["parts"] if p["role"] == role)
    part["pivot"]["motion"] = {"kind": kind, "axis": axis}
    with pytest.raises(SpecInvalidError):
        parse_asset_specification_v07(data)
