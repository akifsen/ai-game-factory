"""Closed V0.8-3A candidate validation rule groups."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.internal_skin_decode import (
    DecodedInternalSkinnedGLB,
    decode_internal_skinned_glb,
)
from gamefactory.adapters.assets.internal_skin_validate import run_internal_skin_checks
from gamefactory.adapters.assets.v08_candidate_geometry import (
    envelope_size_from_aabb,
    visual_aabb_in_reference_root_frame,
)
from gamefactory.adapters.assets.validation_rules import InvalidGLB
from gamefactory.core.domain.asset_contracts import ValidationFinding
from gamefactory.core.domain.asset_contracts import ValidationFindingSeverity as Severity
from gamefactory.core.domain.internal_skin_contract import (
    InternalSkinContract,
)
from gamefactory.core.domain.v08_candidate_contracts import (
    CANDIDATE_RULE_GROUPS,
    CANDIDATE_VISUAL_MESH_NAME,
    AssetProfileV08Candidate,
    AssetSpecificationV08Candidate,
)

Rule = Callable[..., list[ValidationFinding]]


@dataclass(frozen=True)
class CandidateRuleContext:
    artifact: str
    spec: AssetSpecificationV08Candidate
    profile: AssetProfileV08Candidate
    contract: dict[str, Any]
    decoded: DecodedInternalSkinnedGLB
    skin_contract: InternalSkinContract


def _finding(
    ctx: CandidateRuleContext,
    rule_id: str,
    ok: bool,
    expected: str,
    actual: str,
    message: str,
) -> ValidationFinding:
    return ValidationFinding(
        rule_id,
        Severity.PASS if ok else Severity.FAIL,
        expected,
        actual,
        ctx.artifact,
        message,
    )


def _remap_skin_findings(
    findings: list[ValidationFinding], artifact: str
) -> list[ValidationFinding]:
    remapped: list[ValidationFinding] = []
    for item in findings:
        rule_id = item.rule_id
        if rule_id.startswith("internal_skin."):
            rule_id = "rig_skin." + rule_id.removeprefix("internal_skin.")
        remapped.append(
            ValidationFinding(
                rule_id,
                item.severity,
                item.expected,
                item.actual,
                artifact,
                item.message,
            )
        )
    return remapped


def rule_core_glb_hash(ctx: CandidateRuleContext) -> list[ValidationFinding]:
    expected = ctx.spec.processed_glb_sha256
    actual = ctx.decoded.sha256
    return [
        _finding(
            ctx,
            "core_v08_candidate.glb.hash",
            actual == expected,
            expected,
            actual,
            "Processed GLB SHA-256 must match the bound candidate specification",
        )
    ]


def rule_core_mesh_identity(ctx: CandidateRuleContext) -> list[ValidationFinding]:
    name = ctx.decoded.primitive.node_name
    ok = name == CANDIDATE_VISUAL_MESH_NAME == ctx.spec.visual_mesh_name
    return [
        _finding(
            ctx,
            "core_v08_candidate.mesh.identity",
            ok,
            CANDIDATE_VISUAL_MESH_NAME,
            name,
            "Visual mesh node must be SM_HumanoidSkin",
        )
    ]


def rule_core_reference_root(ctx: CandidateRuleContext) -> list[ValidationFinding]:
    root_name = ctx.skin_contract.asset_root_name
    nodes = ctx.decoded.document["nodes"]
    root_idx = ctx.decoded.asset_root_index
    name_to_index = {
        str(n.get("name") or ""): i for i, n in enumerate(nodes) if isinstance(n, dict)
    }
    ok = (
        root_name in name_to_index
        and name_to_index[root_name] == root_idx
        and isinstance(nodes[root_idx], dict)
        and nodes[root_idx].get("name") == root_name
    )
    actual = (
        str(nodes[root_idx].get("name"))
        if 0 <= root_idx < len(nodes) and isinstance(nodes[root_idx], dict)
        else "invalid scene root"
    )
    return [
        _finding(
            ctx,
            "core_v08_candidate.reference_root",
            ok,
            f"sole scene root {root_name}",
            actual,
            "Humanoid reference root must be the sole scene root (any node index)",
        )
    ]


def rule_core_dimensions(ctx: CandidateRuleContext) -> list[ValidationFinding]:
    tol = float(ctx.contract["dimension_tolerance_m"])
    mins, maxs = visual_aabb_in_reference_root_frame(ctx.decoded)
    width, height, depth = envelope_size_from_aabb(mins, maxs)
    declared = ctx.spec.dimensions
    checks = (
        ("width_m", declared.width_m, width),
        ("height_m", declared.height_m, height),
        ("depth_m", declared.depth_m, depth),
    )
    findings: list[ValidationFinding] = []
    for label, declared_value, measured in checks:
        ok = abs(float(declared_value) - measured) <= tol
        findings.append(
            _finding(
                ctx,
                f"core_v08_candidate.dimensions.{label}",
                ok,
                f"{declared_value} ± {tol} m",
                f"{round(measured, 6)} m measured",
                "Declared envelope must match measured visual bounds in the reference-root frame",
            )
        )
    return findings


def _visual_primitive(ctx: CandidateRuleContext) -> dict[str, Any]:
    node_idx = ctx.decoded.primitive.node_index
    nodes = ctx.decoded.document["nodes"]
    mesh_index = nodes[node_idx].get("mesh")
    if mesh_index is None:
        raise ValueError("visual mesh node has no mesh")
    mesh = ctx.decoded.document["meshes"][int(mesh_index)]
    primitives = mesh.get("primitives", [])
    if len(primitives) != 1:
        raise ValueError("visual mesh must have exactly one primitive")
    primitive = primitives[0]
    if not isinstance(primitive, dict):
        raise ValueError("visual primitive is malformed")
    return primitive


def triangle_count_for_primitive(
    document: dict[str, Any], binary: bytes, primitive: dict[str, Any]
) -> int:
    from gamefactory.adapters.assets.glb_validator import _accessor

    attrs = primitive.get("attributes", {})
    if "POSITION" not in attrs:
        raise ValueError("primitive missing POSITION")
    positions = _accessor(document, binary, attrs["POSITION"], "VEC3")
    vertex_count = len(positions)
    if vertex_count == 0:
        return 0
    indices_idx = primitive.get("indices")
    if indices_idx is None:
        if vertex_count % 3 != 0:
            raise ValueError("non-indexed triangle primitive requires a multiple of three vertices")
        return vertex_count // 3
    indices = _accessor(document, binary, indices_idx, "SCALAR")
    if len(indices) % 3 != 0:
        raise ValueError("triangle index count must be a multiple of three")
    return len(indices) // 3


def rule_core_budgets(ctx: CandidateRuleContext) -> list[ValidationFinding]:
    document = ctx.decoded.document
    findings: list[ValidationFinding] = []
    try:
        primitive = _visual_primitive(ctx)
        triangle_count = triangle_count_for_primitive(document, ctx.decoded.binary, primitive)
    except (KeyError, TypeError, ValueError) as exc:
        findings.append(
            _finding(
                ctx,
                "core_v08_candidate.budget.triangles",
                False,
                "decodable visual mesh triangle count",
                str(exc),
                "Could not measure LOD0 triangles for the selected visual mesh",
            )
        )
        triangle_count = -1
    max_tri = ctx.spec.geometry_budget.max_triangles_lod0
    if triangle_count >= 0:
        findings.append(
            _finding(
                ctx,
                "core_v08_candidate.budget.triangles",
                triangle_count <= max_tri,
                f"<= {max_tri}",
                str(triangle_count),
                "LOD0 triangle budget for the validated visual mesh",
            )
        )
    material_count = len(document.get("materials", []))
    mat_budget = ctx.spec.material_budget.max_materials
    findings.append(
        _finding(
            ctx,
            "core_v08_candidate.budget.materials",
            material_count <= mat_budget,
            f"<= {mat_budget}",
            str(material_count),
            "Declared material count in the GLB",
        )
    )
    images = document.get("images")
    if isinstance(images, list) and images:
        findings.append(
            _finding(
                ctx,
                "core_v08_candidate.budget.textures",
                False,
                "no embedded GLB images for candidate static validation",
                f"{len(images)} image(s)",
                "Embedded textures are not decoded in the bounded candidate path",
            )
        )
    return findings


def rule_core_animation_forbidden(ctx: CandidateRuleContext) -> list[ValidationFinding]:
    animations = ctx.decoded.document.get("animations")
    ok = not animations
    return [
        _finding(
            ctx,
            "core_v08_candidate.animation.forbidden",
            ok,
            "no animations",
            f"{len(animations) if isinstance(animations, list) else 'present'}",
            "Candidate forbids glTF animations",
        )
    ]


def rule_rig_skin_bundle(ctx: CandidateRuleContext) -> list[ValidationFinding]:
    findings = run_internal_skin_checks(ctx.decoded, ctx.skin_contract)
    return _remap_skin_findings(findings, ctx.artifact)


def rule_collider_capsule_shape(ctx: CandidateRuleContext) -> list[ValidationFinding]:
    capsule = ctx.spec.collider.capsule
    assert capsule is not None
    r, h = float(capsule.radius_m), float(capsule.height_m)
    problems: list[str] = []
    if not (math.isfinite(r) and math.isfinite(h)):
        problems.append("non-finite capsule values")
    elif r < 0.10:
        problems.append(f"radius_m {r} < 0.10")
    elif h <= 2 * r:
        problems.append(f"height_m {h} <= 2 x radius_m {2 * r}")
    col_nodes = [
        str(n.get("name"))
        for n in ctx.decoded.document.get("nodes", [])
        if isinstance(n, dict) and str(n.get("name", "")).startswith("COL_")
    ]
    if col_nodes:
        problems.append(f"GLB carries collider nodes {col_nodes}")
    actual = f"radius_m={capsule.radius_m}, height_m={capsule.height_m}"
    return [
        _finding(
            ctx,
            "collider.capsule.shape",
            not problems,
            "radius_m >= 0.10, height_m > 2 x radius_m, finite; no COL_ mesh in the GLB",
            "; ".join(problems) if problems else actual,
            "Capsule is runtime-only; the GLB carries no collider mesh",
        )
    ]


def rule_collider_capsule_fit(ctx: CandidateRuleContext) -> list[ValidationFinding]:
    capsule = ctx.spec.collider.capsule
    assert capsule is not None
    tol = float(ctx.contract["dimension_tolerance_m"])
    mins, maxs = visual_aabb_in_reference_root_frame(ctx.decoded)
    width, height, depth = envelope_size_from_aabb(mins, maxs)
    ok = (
        capsule.height_m <= height + tol
        and 2 * capsule.radius_m <= max(width, depth) + tol
        and capsule.height_m <= ctx.spec.dimensions.height_m + tol
        and 2 * capsule.radius_m
        <= max(ctx.spec.dimensions.width_m, ctx.spec.dimensions.depth_m) + tol
    )
    actual = (
        f"height {capsule.height_m} vs measured {round(height, 6)}; "
        f"diameter {2 * capsule.radius_m} vs measured max(width, depth) "
        f"{round(max(width, depth), 6)}"
    )
    return [
        _finding(
            ctx,
            "collider.capsule.fit",
            ok,
            f"capsule fits the measured and specified bounds ± {tol} m",
            actual,
            "Capsule height and diameter must fit inside the asset envelope",
        )
    ]


CORE_V08_CANDIDATE_RULES: tuple[Rule, ...] = (
    rule_core_glb_hash,
    rule_core_mesh_identity,
    rule_core_reference_root,
    rule_core_dimensions,
    rule_core_budgets,
    rule_core_animation_forbidden,
)
RIG_SKIN_RULES: tuple[Rule, ...] = (rule_rig_skin_bundle,)
COLLIDER_CAPSULE_RULES: tuple[Rule, ...] = (
    rule_collider_capsule_shape,
    rule_collider_capsule_fit,
)

CANDIDATE_RULE_GROUP_MAP: dict[str, tuple[Rule, ...]] = {
    "core_v08_candidate": CORE_V08_CANDIDATE_RULES,
    "rig_skin": RIG_SKIN_RULES,
    "collider_capsule": COLLIDER_CAPSULE_RULES,
}


@dataclass(frozen=True)
class CandidateComposition:
    name: str
    groups: tuple[str, ...]
    rules: tuple[Rule, ...]


def select_v08_candidate_composition(
    capabilities: dict[str, Any],
) -> CandidateComposition:
    required: dict[str, Any] = {
        "geometry_mode": "single_mesh",
        "rigging": "humanoid_skin",
        "collider_policy": "capsule",
        "source_kind": "local_verified_rig",
        "rest_pose": "T",
        "runtime_body": "static_body",
        "animation_forbidden": True,
    }
    for key, expected in required.items():
        actual = capabilities.get(key)
        if actual != expected:
            raise ValueError(
                f"unsupported {key} for candidate composition: expected {expected!r}, got {actual!r}"
            )
    groups = CANDIDATE_RULE_GROUPS
    rules = tuple(rule for group in groups for rule in CANDIDATE_RULE_GROUP_MAP[group])
    name = f"{capabilities['geometry_mode']}_{capabilities['rigging']}_{capabilities['collider_policy']}"
    return CandidateComposition(name, groups, rules)


_PARSE_ERRORS = (
    OSError,
    InvalidGLB,
    KeyError,
    TypeError,
    ValueError,
    IndexError,
)


def decode_candidate_glb(
    path: Path, *, max_file_size_bytes: int = 50 * 1024 * 1024
) -> DecodedInternalSkinnedGLB:
    return decode_internal_skinned_glb(path, max_file_size_bytes=max_file_size_bytes)
