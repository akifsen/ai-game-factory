"""Validation rules for decoded internal skinned GLBs (ADR 0019)."""

from __future__ import annotations

from gamefactory.adapters.assets.internal_skin_decode import DecodedInternalSkinnedGLB
from gamefactory.adapters.assets.internal_skin_math import invert_mat4, mat3_basis_columns
from gamefactory.core.domain.asset_contracts import ValidationFinding
from gamefactory.core.domain.asset_contracts import ValidationFindingSeverity as Severity
from gamefactory.core.domain.internal_skin_contract import InternalSkinContract

_WEIGHT_SUM_TOL = 1e-3
_IBM_TOL = 1e-3
_BASIS_TOL = 0.08


def _invert4(m: list[list[float]]) -> list[list[float]]:
    return invert_mat4(m)


def _blender_armature_wrapper_index(
    decoded: DecodedInternalSkinnedGLB, parents: dict[int, int]
) -> int | None:
    """Optional single armature object node between asset root and skeleton root (Blender export)."""
    nodes = decoded.document["nodes"]
    root_children = nodes[decoded.asset_root_index].get("children", [])
    mesh_idx = decoded.primitive.node_index
    candidates = [
        c for c in root_children if isinstance(c, int) and c != mesh_idx and 0 <= c < len(nodes)
    ]
    if len(candidates) != 1:
        return None
    wrap = candidates[0]
    skel = decoded.skeleton_root_index
    if parents.get(skel) == wrap and parents.get(wrap) == decoded.asset_root_index:
        return wrap
    return None


def _mat4_from_flat(values: tuple[float, ...]) -> list[list[float]]:
    return [[values[c * 4 + r] for c in range(4)] for r in range(4)]


def _max_entry_diff(a: list[list[float]], b: list[list[float]]) -> float:
    return max(abs(a[r][c] - b[r][c]) for r in range(4) for c in range(4))


def _vec_angle_delta(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    import math

    la = math.sqrt(sum(c * c for c in a))
    lb = math.sqrt(sum(c * c for c in b))
    if la < 1e-9 or lb < 1e-9:
        return 1.0
    dot = sum(a[i] * b[i] for i in range(3)) / (la * lb)
    dot = max(-1.0, min(1.0, dot))
    return math.acos(dot)


def _finding(
    rule_id: str, ok: bool, expected: str, actual: str, message: str, artifact: str = ""
) -> ValidationFinding:
    return ValidationFinding(
        rule_id,
        Severity.PASS if ok else Severity.FAIL,
        expected,
        actual,
        artifact,
        message,
    )


def validate_topology(
    decoded: DecodedInternalSkinnedGLB, contract: InternalSkinContract
) -> ValidationFinding:
    nodes = decoded.document["nodes"]
    name_to_index = {str(n.get("name") or ""): i for i, n in enumerate(nodes)}
    if contract.asset_root_name not in name_to_index:
        return _finding(
            "internal_skin.topology",
            False,
            f"asset root {contract.asset_root_name}",
            "missing",
            "asset root node not found",
        )
    if name_to_index[contract.asset_root_name] != decoded.asset_root_index:
        return _finding(
            "internal_skin.topology",
            False,
            "asset root is scene root",
            "mismatch",
            "scene root name does not match contract",
        )
    if len(decoded.joint_node_indices) > contract.joint_count_max:
        return _finding(
            "internal_skin.topology",
            False,
            f"joint count <= {contract.joint_count_max}",
            str(len(decoded.joint_node_indices)),
            "too many joints",
        )
    parents: dict[int, int] = {}
    for parent, node in enumerate(nodes):
        for child in node.get("children", []):
            if isinstance(child, int):
                parents[child] = parent
    armature_wrap = _blender_armature_wrapper_index(decoded, parents)
    wrong: list[str] = []
    for bone in contract.bones:
        if bone.name not in name_to_index:
            wrong.append(f"missing bone {bone.name}")
            continue
        idx = name_to_index[bone.name]
        if idx not in decoded.joint_node_indices:
            wrong.append(f"{bone.name} is not in skin joints")
            continue
        parent_name = bone.parent
        if parent_name is None:
            wrong.append(f"{bone.name} missing parent in contract")
            continue
        if parent_name not in name_to_index and parent_name != contract.asset_root_name:
            wrong.append(f"unknown parent {parent_name} for {bone.name}")
            continue
        skel_name = str(nodes[decoded.skeleton_root_index].get("name") or "")
        if parent_name == contract.asset_root_name:
            if bone.name == skel_name and armature_wrap is not None:
                parent_idx = armature_wrap
            else:
                parent_idx = decoded.asset_root_index
        else:
            parent_idx = name_to_index[parent_name]
        parent_field = decoded.document["nodes"][parent_idx]
        if parent_idx not in {decoded.asset_root_index, *decoded.joint_node_indices} and (
            armature_wrap is None or parent_idx != armature_wrap
        ):
            wrong.append(f"parent {parent_name} not in skeleton")
        listed = parent_field.get("children", [])
        if idx not in listed:
            wrong.append(f"{bone.name} not child of {parent_name}")
        actual_parent = parents.get(idx)
        if actual_parent != parent_idx:
            wrong.append(f"{bone.name} parent index mismatch")
    return _finding(
        "internal_skin.topology",
        not wrong,
        "declared bone map and parent chain",
        "; ".join(wrong) if wrong else "ok",
        "bone topology must match contract",
    )


def validate_rest_pose(
    decoded: DecodedInternalSkinnedGLB, contract: InternalSkinContract
) -> ValidationFinding:
    nodes = decoded.document["nodes"]
    name_to_index = {str(n.get("name") or ""): i for i, n in enumerate(nodes)}
    tol = contract.rest_pose_tolerance
    issues: list[str] = []

    def bone_world(name: str) -> tuple[float, float, float]:
        idx = name_to_index.get(name)
        if idx is None or idx not in decoded.joint_world_rest:
            raise KeyError(name)
        m = decoded.joint_world_rest[idx]
        return (m[0][3], m[1][3], m[2][3])

    for bone_name, expected in contract.rest_joint_origins.items():
        try:
            actual = bone_world(bone_name)
        except KeyError:
            issues.append(f"missing joint {bone_name}")
            continue
        for axis, label in enumerate("xyz"):
            if abs(actual[axis] - expected[axis]) > tol:
                issues.append(
                    f"{bone_name}.{label} expected {expected[axis]:.3f} got {actual[axis]:.3f}"
                )

    for bone_name, ref in contract.rest_joint_bases.items():
        idx = name_to_index.get(bone_name)
        if idx is None or idx not in decoded.joint_world_rest:
            issues.append(f"missing joint basis for {bone_name}")
            continue
        y_axis, neg_z = mat3_basis_columns(decoded.joint_world_rest[idx])
        if _vec_angle_delta(y_axis, ref.y_axis) > _BASIS_TOL:
            issues.append(f"{bone_name} rest +Y basis mismatch")
        if _vec_angle_delta(neg_z, ref.neg_z_axis) > _BASIS_TOL:
            issues.append(f"{bone_name} rest -Z basis mismatch")

    try:
        chest = bone_world("Chest")
        lua = bone_world("LeftUpperArm")
        rua = bone_world("RightUpperArm")
        lle = bone_world("LeftLowerArm")
        lha = bone_world("LeftHand")
        if lua[0] >= chest[0] - 0.02:
            issues.append("left upper arm not on -X of chest")
        if rua[0] <= chest[0] + 0.02:
            issues.append("right upper arm not on +X of chest")
        if abs(lua[1] - chest[1]) > tol or abs(rua[1] - chest[1]) > tol:
            issues.append("upper arms not level with chest in Y")
        if lua[0] <= lle[0] or lle[0] <= lha[0]:
            issues.append("left arm chain does not extend -X")
        if chest[1] <= decoded.joint_world_rest[name_to_index["Hips"]][1][3]:
            issues.append("chest not above hips in +Y")
    except KeyError:
        issues.append("T-pose axis bones missing")

    return _finding(
        "internal_skin.rest_pose",
        not issues,
        "T-pose joint origins and arm axes (+Y up, -Z front)",
        "; ".join(issues[:10]) if issues else "ok",
        "rest pose measured from joint globals against contract reference",
    )


def validate_weights(decoded: DecodedInternalSkinnedGLB) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    bad_sum: list[str] = []
    unweighted: list[str] = []
    too_many: list[str] = []
    for i, vw in enumerate(decoded.primitive.vertex_weights):
        active = [(j, w) for j, w in zip(vw.joints, vw.weights, strict=True) if w > 0]
        if len(active) > 4:
            too_many.append(str(i))
        total = sum(vw.weights)
        if total <= 1e-8:
            unweighted.append(str(i))
        elif abs(total - 1.0) > _WEIGHT_SUM_TOL:
            bad_sum.append(f"{i}:{total:.6f}")
    findings.append(
        _finding(
            "internal_skin.weights.normalized",
            not bad_sum,
            f"weight sum within {_WEIGHT_SUM_TOL}",
            ", ".join(bad_sum[:8]) if bad_sum else "ok",
            "vertex weights must normalize",
        )
    )
    findings.append(
        _finding(
            "internal_skin.weights.coverage",
            not unweighted,
            "every vertex has positive total weight",
            f"{len(unweighted)} unweighted" if unweighted else "ok",
            "unweighted vertices are forbidden",
        )
    )
    findings.append(
        _finding(
            "internal_skin.weights.influence_count",
            not too_many,
            "at most four influences",
            ", ".join(too_many[:8]) if too_many else "ok",
            "too many influences per vertex",
        )
    )
    return findings


def validate_inverse_bind(decoded: DecodedInternalSkinnedGLB) -> ValidationFinding:
    mismatches: list[str] = []
    for joint_i, node_index in enumerate(decoded.joint_node_indices):
        world = decoded.joint_world_rest[node_index]
        try:
            expected = _invert4(world)
        except ValueError:
            mismatches.append(f"joint {joint_i}: singular rest matrix")
            continue
        actual = _mat4_from_flat(decoded.inverse_bind_matrices[joint_i])
        diff = _max_entry_diff(expected, actual)
        if diff > _IBM_TOL:
            mismatches.append(f"joint {joint_i}: max entry diff {diff:.6f}")
    return _finding(
        "internal_skin.inverse_bind",
        not mismatches,
        f"inverse bind within {_IBM_TOL} of rest inverse",
        "; ".join(mismatches[:6]) if mismatches else "ok",
        "inverse bind matrices must match rest pose",
    )


def validate_animation_absent(decoded: DecodedInternalSkinnedGLB) -> ValidationFinding:
    has_anim = bool(decoded.document.get("animations"))
    return _finding(
        "internal_skin.animation_forbidden",
        not has_anim,
        "no animations",
        "present" if has_anim else "absent",
        "animations are forbidden in internal subset",
    )


def run_internal_skin_checks(
    decoded: DecodedInternalSkinnedGLB, contract: InternalSkinContract
) -> list[ValidationFinding]:
    return [
        validate_animation_absent(decoded),
        validate_topology(decoded, contract),
        validate_rest_pose(decoded, contract),
        *validate_weights(decoded),
        validate_inverse_bind(decoded),
    ]
