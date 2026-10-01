#!/usr/bin/env python3
"""Concatenate stdlib cold GLB logic into verify_rig_bundle.py."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "src" / "gamefactory" / "adapters" / "assets"


def extract(src: str, name: str) -> str:
    pattern = rf"(?ms)^def {re.escape(name)}\b.*?(?=\n^def |\n^class |\n^@dataclass|\Z)"
    match = re.search(pattern, src)
    if not match:
        raise RuntimeError(f"missing def {name}")
    return match.group(0).rstrip() + "\n\n"


def extract_decode(decode_src: str) -> str:
    header_match = re.search(
        r"(?ms)^((?:@dataclass\(frozen=True\)\nclass .*?\n)+)def _glb_io",
        decode_src,
    )
    if not header_match:
        raise RuntimeError("dataclass block missing")
    names = [
        "_reject_extensions",
        "_world_matrices",
        "_read_mat4_accessor",
        "_infer_skeleton_root_index",
        "_parse_joints_weights",
        "_node_transform_is_identity",
        "decode_internal_skinned_glb",
    ]
    chunks = [header_match.group(1)]
    for name in names:
        fn = extract(decode_src, name)
        fn = fn.replace("def decode_internal_skinned_glb", "def _decode_internal_skinned_glb")
        fn = fn.replace("_glb_io()", "(_read_glb, _accessor, _mat_mul, _node_matrix)")
        fn = re.sub(r"^\s+from gamefactory\..*\n", "", fn, flags=re.M)
        fn = re.sub(r"^\s+import hashlib\n\s+", "    import hashlib\n    ", fn, flags=re.M)
        chunks.append(fn)
    return "".join(chunks)


def patch_validate(src: str) -> str:
    src = src.replace(
        "from gamefactory.adapters.assets.internal_skin_decode import DecodedInternalSkinnedGLB", ""
    )
    src = src.replace("from gamefactory.core.domain.asset_contracts import ValidationFinding", "")
    src = src.replace(
        "from gamefactory.core.domain.asset_contracts import ValidationFindingSeverity as Severity",
        "",
    )
    src = src.replace(
        "from gamefactory.core.domain.internal_skin_contract import InternalSkinContract", ""
    )
    src = src.replace("from gamefactory.adapters.assets.internal_skin_math import invert_mat4", "")
    src = src.replace("InternalSkinContract", "dict[str, Any]")
    src = src.replace("DecodedInternalSkinnedGLB", "DecodedInternalSkinnedGLB")
    src = src.replace("def _finding(", "def _validation_finding(")
    src = src.replace("return _finding(", "return _validation_finding(")
    src = src.replace("Severity.PASS", '"PASS"')
    src = src.replace("Severity.FAIL", '"FAIL"')
    src = src.replace("contract.asset_root_name", 'contract["asset_root_name"]')
    src = src.replace("contract.joint_count_max", 'contract["joint_count_max"]')
    src = src.replace("contract.bones", 'contract["_bones"]')
    src = src.replace("contract.rest_pose_tolerance", 'contract["rest_pose_tolerance"]')
    src = src.replace("contract.rest_joint_origins", 'contract["rest_joint_origins"]')
    src = src.replace("contract.rest_joint_bases", 'contract["_rest_bases"]')
    src = re.sub(r"for bone in contract\[\"_bones\"\]:", "for bone in contract['_bones']:", src)
    src = src.replace("bone.name", "bone['name']")
    src = src.replace("bone.parent", "bone['parent']")
    src = src.replace("ref.y_axis", "ref['y_axis']")
    src = src.replace("ref.neg_z_axis", "ref['neg_z_axis']")
    src = src.replace("-> ValidationFinding:", "-> dict[str, Any]:")
    src = src.replace("-> list[ValidationFinding]:", "-> list[dict[str, Any]]:")
    src = src.replace(
        "findings: list[ValidationFinding] = []", "findings: list[dict[str, Any]] = []"
    )
    return src


def main() -> None:
    math_src = (ASSETS / "internal_skin_math.py").read_text(encoding="utf-8")
    bounds_src = (ASSETS / "internal_skin_gltf_bounds.py").read_text(encoding="utf-8")
    glb_src = (ASSETS / "glb_validator.py").read_text(encoding="utf-8")
    decode_src = (ASSETS / "internal_skin_decode.py").read_text(encoding="utf-8")
    validate_src = patch_validate(
        (ASSETS / "internal_skin_validate.py").read_text(encoding="utf-8")
    )

    bounds_src = bounds_src.replace(
        "from gamefactory.adapters.assets.validation_rules import InvalidGLB\n", ""
    )
    bounds_src = bounds_src.replace("InvalidGLB", "InvalidGLB")
    bounds_src = re.sub(
        r"^\s+from gamefactory\.adapters\.assets\.glb_validator import _accessor\n",
        "",
        bounds_src,
        flags=re.M,
    )
    bounds_src = bounds_src.replace(
        '_accessor(document, binary, indices_idx, "SCALAR")',
        '_accessor(document, binary, indices_idx, "SCALAR")',
    )

    glb_chunk = "\n".join(
        extract(glb_src, name)
        .replace("_InvalidGLB", "InvalidGLB")
        .replace("_COMPONENTS[component]", "_COMPONENTS_GLB[component]")
        for name in [
            "_unique_object",
            "_read_glb",
            "_accessor",
            "_mat_mul",
            "_node_matrix",
        ]
    )

    bounds_chunk = "\n".join(
        extract(bounds_src, name)
        for name in [
            "_require_int",
            "_require_nonneg_int",
            "validate_embedded_glb_resources",
            "validate_skinned_primitive_structure",
            "read_index_triangles",
            "validate_skin_joints",
            "validate_active_scene",
            "validate_node_transforms_finite",
        ]
    )

    math_chunk = "\n".join(
        extract(math_src, name) for name in ["invert_mat4", "mat3_basis_columns"]
    )
    decode_chunk = extract_decode(decode_src)
    validate_chunk = "\n".join(
        extract(validate_src, name)
        for name in [
            "_invert4",
            "_mat4_from_flat",
            "_max_entry_diff",
            "_vec_angle_delta",
            "_blender_armature_wrapper_index",
            "_validation_finding",
            "validate_topology",
            "validate_rest_pose",
            "validate_weights",
            "validate_inverse_bind",
            "validate_animation_absent",
            "run_internal_skin_checks",
        ]
    )

    header = (REPO / "scripts" / "verify_rig_bundle_header.py").read_text(encoding="utf-8")
    footer = (REPO / "scripts" / "verify_rig_bundle_footer.py").read_text(encoding="utf-8")

    const_block = (
        "_COMPONENTS_GLB = {\n"
        "    5120: (1, 'b'),\n"
        "    5121: (1, 'B'),\n"
        "    5122: (2, 'h'),\n"
        "    5123: (2, 'H'),\n"
        "    5125: (4, 'I'),\n"
        "    5126: (4, 'f'),\n"
        "}\n"
        "_COMPONENTS = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}\n"
        "_TYPE_WIDTH = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT2': 4, 'MAT3': 9, 'MAT4': 16}\n"
        "_WIDTHS = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4}\n"
        "_MAX_ACCESSOR_ELEMENTS = 1_000_000\n\n"
    )

    body = (
        const_block + "\n" + math_chunk + glb_chunk + bounds_chunk + decode_chunk + validate_chunk
    )

    content = header + body + footer
    content = content.replace(
        'f"asset root {contract["asset_root_name"]}"',
        "f'asset root {contract[\"asset_root_name\"]}'",
    )
    content = content.replace(
        'f"joint count <= {contract["joint_count_max"]}"',
        "f'joint count <= {contract[\"joint_count_max\"]}'",
    )
    content = content.replace(
        "_read_glb, _accessor, _, _ = (_read_glb, _accessor, _mat_mul, _node_matrix)",
        "# use module-level _read_glb/_accessor",
    )
    content = content.replace(
        "        component_size = _COMPONENTS_GLB[component]",
        "        component_size = _COMPONENTS[component]",
    )
    content = re.sub(r"(?<!_validation_)_finding\(", "_validation_finding(", content)
    content = content.replace("_validation_validation_finding", "_validation_finding")
    content = content.replace(
        "    return ValidationFinding(\n"
        "        rule_id,\n"
        '        "PASS" if ok else "FAIL",\n'
        "        expected,\n"
        "        actual,\n"
        "        artifact,\n"
        "        message,\n"
        "    )",
        "    return {\n"
        '        "rule_id": rule_id,\n'
        '        "severity": "PASS" if ok else "FAIL",\n'
        '        "expected": expected,\n'
        '        "actual": actual,\n'
        '        "artifact": artifact,\n'
        '        "message": message,\n'
        "    }",
    )
    content = content.replace(
        "_, _accessor, _, _ = (_read_glb, _accessor, _mat_mul, _node_matrix)",
        "# use module-level _accessor",
    )
    content = re.sub(
        r"\s+_, _, _mat_mul, _node_matrix = \(_read_glb, _accessor, _mat_mul, _node_matrix\)\n",
        "\n",
        content,
    )
    content = re.sub(
        r"\s+_, _accessor, _, _ = \(_read_glb, _accessor, _mat_mul, _node_matrix\)\n",
        "\n",
        content,
    )
    for rel in (
        "scripts/verify_rig_bundle.py",
        "src/gamefactory/resources/scripts/verify_rig_bundle.py",
    ):
        out = REPO / rel
        out.write_text(content, encoding="utf-8")
    for rel in (
        "scripts/verify_rig_bundle.py",
        "src/gamefactory/resources/scripts/verify_rig_bundle.py",
    ):
        subprocess.run(
            [sys.executable, "-m", "ruff", "format", str(REPO / rel)],
            check=True,
        )
    print("wrote verify_rig_bundle.py", len(content))


if __name__ == "__main__":
    main()
