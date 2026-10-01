"""V0.8-3A candidate GLB validation composition and rig/capsule negatives."""

from __future__ import annotations

from pathlib import Path

import pytest
from v08_candidate_budget_glb import (
    NORMATIVE_MIN_TRIANGLE_BUDGET,
    build_candidate_budget_glb,
    spec_bound_to_glb,
)

from gamefactory.adapters.assets.glb_validator import validate_glb
from gamefactory.adapters.assets.v08_candidate_geometry import (
    capsule_center_from_aabb,
    visual_aabb_in_reference_root_frame,
)
from gamefactory.adapters.assets.v08_candidate_rules import (
    CandidateRuleContext,
    _visual_primitive,
    decode_candidate_glb,
    triangle_count_for_primitive,
)
from gamefactory.adapters.assets.v08_candidate_validate import (
    composition_for_v08_candidate,
    validate_v08_candidate_glb,
    validation_report_metadata,
)
from gamefactory.adapters.fakes import assembly_generator as ag
from gamefactory.adapters.fakes.humanoid_skin_fixture import Variant, build_humanoid_skinned_glb
from gamefactory.core.domain.asset_contracts import parse_asset_specification_v07
from gamefactory.core.domain.internal_skin_contract import load_internal_skin_contract
from gamefactory.core.domain.v08_candidate_contracts import (
    CANDIDATE_RULE_GROUPS,
    load_packaged_candidate_profile,
    load_packaged_candidate_specification,
    profile_document_hash,
)


def _write(tmp_path: Path, variant: Variant) -> Path:
    path = tmp_path / f"{variant}.glb"
    path.write_bytes(build_humanoid_skinned_glb(variant))
    return path


def test_candidate_composition_order_is_fixed() -> None:
    spec = load_packaged_candidate_specification()
    profile = load_packaged_candidate_profile()
    composition = composition_for_v08_candidate(spec, profile)
    assert composition.groups == CANDIDATE_RULE_GROUPS
    metadata = validation_report_metadata(spec, composition)
    assert metadata["rule_groups"] == list(CANDIDATE_RULE_GROUPS)
    assert metadata["production_eligible"] is False
    assert metadata["candidate_state"] == "CLOSED"


def test_capsule_center_matches_adr0021_formula(tmp_path: Path) -> None:
    path = _write(tmp_path, "positive")
    decoded = decode_candidate_glb(path)
    mins, maxs = visual_aabb_in_reference_root_frame(decoded)
    center = capsule_center_from_aabb(mins, maxs)
    assert center == (-0.26000000163912773, 1.2249999642372131, 0.0)


def test_positive_candidate_fixture_passes(tmp_path: Path) -> None:
    spec = load_packaged_candidate_specification()
    result = validate_v08_candidate_glb(_write(tmp_path, "positive"), spec)
    assert result.status.value == "PASS"
    assert not any(f.severity.value == "FAIL" for f in result.findings)
    rule_ids = [f.rule_id for f in result.findings]
    assert rule_ids.index("core_v08_candidate.glb.hash") < rule_ids.index("rig_skin.topology")
    assert rule_ids.index("rig_skin.topology") < rule_ids.index("collider.capsule.shape")


@pytest.mark.parametrize(
    ("variant", "rule_fragment"),
    [
        ("missing_bone", "rig_skin.topology"),
        ("wrong_parent", "rig_skin.topology"),
        ("five_influences", "parse"),
        ("weight_sum", "rig_skin.weights.normalized"),
        ("unweighted_vertex", "rig_skin.weights.coverage"),
        ("inverse_bind_mismatch", "rig_skin.inverse_bind"),
        ("rest_mismatch", "rig_skin.rest_pose"),
        ("animation_present", "parse"),
    ],
)
def test_rig_skin_negatives_fail(tmp_path: Path, variant: Variant, rule_fragment: str) -> None:
    spec = load_packaged_candidate_specification()
    result = validate_v08_candidate_glb(_write(tmp_path, variant), spec)
    assert result.status.value == "FAIL"
    fails = [f for f in result.findings if f.severity.value == "FAIL"]
    assert any(rule_fragment in f.rule_id for f in fails)


def test_malformed_glb_fails_closed(tmp_path: Path) -> None:
    spec = load_packaged_candidate_specification()
    path = _write(tmp_path, "malformed_accessor")
    result = validate_v08_candidate_glb(path, spec)
    assert result.status.value == "FAIL"
    assert any("parse" in f.rule_id for f in result.findings if f.severity.value == "FAIL")


def test_bad_capsule_spec_fails_fit(tmp_path: Path) -> None:
    spec = load_packaged_candidate_specification()
    data = spec.model_dump(mode="json")
    data["collider"]["capsule"]["height_m"] = 5.0
    from gamefactory.core.domain.v08_candidate_contracts import (
        AssetSpecificationV08Candidate,
        load_packaged_candidate_profile,
    )

    bad = AssetSpecificationV08Candidate.model_validate(data)
    load_packaged_candidate_profile().check_specification(bad)
    result = validate_v08_candidate_glb(_write(tmp_path, "positive"), bad)
    assert result.status.value == "FAIL"
    assert any(f.rule_id == "collider.capsule.fit" for f in result.findings)


def test_production_character_still_rejects_skinned_glb(tmp_path: Path) -> None:
    spec = parse_asset_specification_v07(ag.character_spec(ag.HUMANOID_CHARACTER))
    path = _write(tmp_path, "positive")
    result = validate_glb(path, spec)
    assert result.status.value == "FAIL"
    assert any(f.rule_id == "glb.parse" for f in result.findings)


def test_wrong_glb_hash_fails(tmp_path: Path) -> None:
    spec = load_packaged_candidate_specification()
    data = spec.model_dump(mode="json")
    data["processed_glb_sha256"] = "f" * 64
    from gamefactory.core.domain.v08_candidate_contracts import AssetSpecificationV08Candidate

    bad = AssetSpecificationV08Candidate.model_validate(data)
    result = validate_v08_candidate_glb(_write(tmp_path, "positive"), bad)
    assert result.status.value == "FAIL"
    assert any(f.rule_id == "core_v08_candidate.glb.hash" for f in result.findings)


def test_forged_profile_document_hash_fails_at_validator_boundary(tmp_path: Path) -> None:
    spec = load_packaged_candidate_specification()
    data = spec.model_dump(mode="json")
    data["profile_document_hash"] = "f" * 64
    from gamefactory.core.domain.v08_candidate_contracts import AssetSpecificationV08Candidate

    forged = AssetSpecificationV08Candidate.model_validate(data)
    result = validate_v08_candidate_glb(_write(tmp_path, "positive"), forged)
    assert result.status.value == "FAIL"
    assert any(f.rule_id == "core_v08_candidate.binding" for f in result.findings)


def test_stale_profile_hash_after_policy_change_fails_binding(tmp_path: Path) -> None:
    spec = load_packaged_candidate_specification()
    data = spec.model_dump(mode="json")
    data["profile_document_hash"] = "0" * 64
    from gamefactory.core.domain.v08_candidate_contracts import AssetSpecificationV08Candidate

    stale = AssetSpecificationV08Candidate.model_validate(data)
    result = validate_v08_candidate_glb(_write(tmp_path, "positive"), stale)
    assert result.status.value == "FAIL"
    assert any("profile_document_hash" in f.actual for f in result.findings)


def test_mutated_dimensions_after_parse_fails_binding(tmp_path: Path) -> None:
    spec = load_packaged_candidate_specification()
    data = spec.model_dump(mode="json")
    data["dimensions"]["depth_m"] = 0.05
    from gamefactory.core.domain.v08_candidate_contracts import AssetSpecificationV08Candidate

    bad = AssetSpecificationV08Candidate.model_validate(data)
    result = validate_v08_candidate_glb(_write(tmp_path, "positive"), bad)
    assert result.status.value == "FAIL"
    assert any(f.rule_id == "core_v08_candidate.binding" for f in result.findings)


def _assert_budget_over_limit_isolated(result) -> None:
    assert result.status.value == "FAIL"
    fails = [f for f in result.findings if f.severity.value == "FAIL"]
    assert [f.rule_id for f in fails] == ["core_v08_candidate.budget.triangles"]
    non_budget_core = [
        f
        for f in result.findings
        if f.rule_id.startswith("core_v08_candidate.")
        and f.rule_id != "core_v08_candidate.budget.triangles"
    ]
    assert all(f.severity.value == "PASS" for f in non_budget_core)
    assert all(
        f.severity.value == "PASS" for f in result.findings if f.rule_id.startswith("rig_skin.")
    )
    assert all(
        f.severity.value == "PASS" for f in result.findings if f.rule_id.startswith("collider.")
    )
    assert not any("parse" in f.rule_id and f.severity.value == "FAIL" for f in result.findings)


def test_triangle_budget_over_normative_min_fails_at_entrypoint_indexed(tmp_path: Path) -> None:
    glb = build_candidate_budget_glb("indexed_dual_mesh")
    path = tmp_path / "over_budget_indexed.glb"
    path.write_bytes(glb)
    spec = spec_bound_to_glb(glb, max_triangles_lod0=NORMATIVE_MIN_TRIANGLE_BUDGET)
    result = validate_v08_candidate_glb(path, spec)
    _assert_budget_over_limit_isolated(result)


def test_triangle_budget_over_normative_min_fails_at_entrypoint_nonindexed(tmp_path: Path) -> None:
    glb = build_candidate_budget_glb("nonindexed_dual_mesh")
    path = tmp_path / "over_budget_nonindexed.glb"
    path.write_bytes(glb)
    spec = spec_bound_to_glb(glb, max_triangles_lod0=NORMATIVE_MIN_TRIANGLE_BUDGET)
    result = validate_v08_candidate_glb(path, spec)
    _assert_budget_over_limit_isolated(result)


@pytest.mark.parametrize("mode", ["indexed_dual_mesh", "nonindexed_dual_mesh"])
def test_triangle_budget_within_limit_passes_at_entrypoint(tmp_path: Path, mode: str) -> None:
    glb = build_candidate_budget_glb(mode)
    path = tmp_path / f"within_budget_{mode}.glb"
    path.write_bytes(glb)
    spec = spec_bound_to_glb(glb, max_triangles_lod0=20000)
    result = validate_v08_candidate_glb(path, spec)
    assert result.status.value == "PASS"
    assert not any(f.severity.value == "FAIL" for f in result.findings)
    tri = [f for f in result.findings if f.rule_id == "core_v08_candidate.budget.triangles"]
    assert tri and tri[0].severity.value == "PASS"


@pytest.mark.parametrize("mode", ["indexed_dual_mesh", "nonindexed_dual_mesh"])
def test_triangle_budget_fixture_preserves_inverse_bind_matrices(tmp_path: Path, mode: str) -> None:
    baseline_path = _write(tmp_path, "positive")
    baseline = decode_candidate_glb(baseline_path)
    glb = build_candidate_budget_glb(mode)
    budget_path = tmp_path / f"budget_{mode}.glb"
    budget_path.write_bytes(glb)
    budget = decode_candidate_glb(budget_path)
    assert budget.inverse_bind_matrices == baseline.inverse_bind_matrices


def test_triangle_budget_fixture_uses_visual_mesh_index_one(tmp_path: Path) -> None:
    glb = build_candidate_budget_glb("indexed_dual_mesh")
    path = tmp_path / "dual_mesh.glb"
    path.write_bytes(glb)
    decoded = decode_candidate_glb(path)
    mesh_index = decoded.document["nodes"][decoded.primitive.node_index]["mesh"]
    assert mesh_index == 1
    assert len(decoded.document["meshes"]) >= 2


def test_triangle_count_matches_decoded_visual_mesh(tmp_path: Path) -> None:
    path = _write(tmp_path, "positive")
    decoded = decode_candidate_glb(path)
    profile = load_packaged_candidate_profile()
    spec = load_packaged_candidate_specification()
    ctx = CandidateRuleContext(
        artifact=str(path),
        spec=spec,
        profile=profile,
        contract=profile.processing_contract(spec),
        decoded=decoded,
        skin_contract=load_internal_skin_contract(),
    )
    primitive = _visual_primitive(ctx)
    count = triangle_count_for_primitive(decoded.document, decoded.binary, primitive)
    assert count > 0


def test_capsule_fit_uses_max_horizontal_extent_not_min(tmp_path: Path) -> None:
    """Diameter bound uses max(width, depth); min(width, depth) would reject this capsule."""
    spec = load_packaged_candidate_specification()
    data = spec.model_dump(mode="json")
    data["collider"]["capsule"]["radius_m"] = 0.15
    data["collider"]["capsule"]["height_m"] = 0.5
    data["profile_document_hash"] = profile_document_hash(
        load_packaged_candidate_profile().document
    )
    from gamefactory.core.domain.v08_candidate_contracts import AssetSpecificationV08Candidate

    wide = AssetSpecificationV08Candidate.model_validate(data)
    result = validate_v08_candidate_glb(_write(tmp_path, "positive"), wide)
    fit = [f for f in result.findings if f.rule_id == "collider.capsule.fit"]
    assert fit and fit[0].severity.value == "PASS"
