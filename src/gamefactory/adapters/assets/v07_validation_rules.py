"""Closed ordered V0.7 rule runners over one frozen decoded context."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from gamefactory.adapters.assets.v07_geometry_validation import (
    GeometryFinding,
    V07RuleGroup,
    _matrix_is_identity,
)
from gamefactory.adapters.assets.v07_validation_context import V07ValidationContext, thaw


@dataclass(frozen=True)
class V07RuleExecutionContext:
    decoded: V07ValidationContext
    max_file_size_bytes: int


Runner = Callable[[V07RuleExecutionContext], tuple[GeometryFinding, ...]]


def _finding(
    rule: str, passed: bool, expected: object, actual: object, message: str
) -> GeometryFinding:
    return GeometryFinding(rule, passed, str(expected), str(actual), message)


def _input_failure(
    group: V07RuleGroup, context: V07RuleExecutionContext
) -> tuple[GeometryFinding, ...]:
    if context.decoded.parse_error is None:
        return ()
    return (
        _finding(
            f"input.{group.value}",
            False,
            "bounded decoded GLB context",
            context.decoded.parse_error,
            f"{group.value} rules cannot run without their GLB input",
        ),
    )


def _nodes(context: V07RuleExecutionContext) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    document = thaw(context.decoded.document or {})
    return document, document.get("nodes", [])


def _world(context: V07RuleExecutionContext) -> dict[int, list[list[float]]]:
    return {
        index: [list(row) for row in matrix]
        for index, matrix in context.decoded.world_matrices.items()
    }


def _run_core(context: V07RuleExecutionContext) -> tuple[GeometryFinding, ...]:
    failed = _input_failure(V07RuleGroup.CORE, context)
    if failed:
        return (
            _finding(
                "glb.parse",
                False,
                "bounded supported GLB 2.0",
                context.decoded.parse_error,
                "Safe geometry decode failed",
            ),
            *failed,
        )
    c = context.decoded
    document, _ = _nodes(context)
    names = [str(node.get("name") or "") for node in document["nodes"]]
    materials = document.get("materials", [])
    texture_max = max((max(size) for size in c.textures), default=0)
    return (
        _finding(
            "glb.parse", True, "bounded supported GLB 2.0", "parsed", "Actual accessor data decoded"
        ),
        _finding(
            "geometry.nonempty",
            bool(c.points) and c.triangle_count > 0,
            "nonempty triangles",
            c.triangle_count,
            "Decoded positions and indices",
        ),
        _finding(
            "nodes.unique",
            len(names) == len(set(names)),
            "unique named nodes",
            names,
            "Duplicate names make importer resolution ambiguous",
        ),
        _finding(
            "materials.budget",
            len(materials) <= c.specification.material_budget.max_materials
            and (not c.material_ids or max(c.material_ids) < len(materials)),
            c.specification.material_budget.max_materials,
            len(materials),
            "Material budget and references",
        ),
        _finding(
            "textures.dimension",
            texture_max <= c.specification.texture_budget.max_dimension,
            c.specification.texture_budget.max_dimension,
            texture_max,
            "Embedded texture dimensions",
        ),
        _finding(
            "geometry.lod0_budget",
            sum(m.triangle_count for m in c.meshes if m.name.endswith("LOD0"))
            <= c.specification.geometry_budget.max_triangles_lod0,
            c.specification.geometry_budget.max_triangles_lod0,
            sum(m.triangle_count for m in c.meshes if m.name.endswith("LOD0")),
            "LOD0 triangle budget",
        ),
        _finding(
            "geometry.lod1_budget",
            sum(m.triangle_count for m in c.meshes if m.name.endswith("LOD1"))
            <= c.specification.geometry_budget.max_triangles_lod1,
            c.specification.geometry_budget.max_triangles_lod1,
            sum(m.triangle_count for m in c.meshes if m.name.endswith("LOD1")),
            "LOD1 triangle budget",
        ),
    )


def _run_assembly(context: V07RuleExecutionContext) -> tuple[GeometryFinding, ...]:
    failed = _input_failure(V07RuleGroup.ASSEMBLY, context)
    if failed:
        return failed
    from gamefactory.adapters.assets.glb_validator import _node_matrix
    from gamefactory.adapters.assets.v07_geometry_validation import _validate_assembly_bounds

    c = context.decoded
    spec, profile = c.specification, c.profile
    document, nodes = _nodes(context)
    results: list[GeometryFinding] = []

    def add(*args: Any) -> None:
        results.append(_finding(*args))

    node_by_name = {str(node.get("name", "")): (index, node) for index, node in enumerate(nodes)}
    node_mesh = {name: value for name, value in node_by_name.items() if "mesh" in value[1]}
    mesh_by_name = {mesh.name: mesh for mesh in c.meshes}
    parts = {part.part_id: part for part in (spec.parts or [])}
    roots = [(index, node) for index, node in enumerate(nodes) if node.get("name") == "ROOT"]
    root_index, root_node = roots[0] if len(roots) == 1 else (-1, {})
    root_identity = root_index >= 0 and _matrix_is_identity(_node_matrix(dict(root_node)))
    add(
        "root.structure",
        root_identity,
        "one identity ROOT node",
        len(roots),
        "Assembly root is explicit and identity",
    )
    active_root = root_index >= 0 and c.active_scene_roots == (root_index,)
    all_reachable = c.reachable_nodes == frozenset(range(len(nodes)))
    add(
        "scene.assembly_root",
        active_root and all_reachable,
        "active scene contains only ROOT and all nodes are reachable",
        (c.active_scene_roots, sorted(c.reachable_nodes)),
        "Assembly hierarchy must be fully reachable from its sole active ROOT",
    )
    expected_parts = {f"PART_{part_id}" for part_id in parts}
    actual_parts = {name for name in node_by_name if name.startswith("PART_")}
    add(
        "part.missing",
        expected_parts <= actual_parts,
        sorted(expected_parts),
        sorted(actual_parts),
        "Every declared part has a PART node",
    )
    add(
        "part.unexpected",
        actual_parts <= expected_parts,
        sorted(expected_parts),
        sorted(actual_parts),
        "No undeclared PART nodes",
    )
    parent_by_index = c.parent_by_index
    part_indices = {
        name: value[0] for name, value in node_by_name.items() if name.startswith("PART_")
    }
    for part_id, part in parts.items():
        name = f"PART_{part_id}"
        entry = node_by_name.get(name)
        if entry is None:
            continue
        node_index, node = entry
        expected_parent = (
            root_index if part.parent == "root" else part_indices.get(f"PART_{part.parent}")
        )
        add(
            f"part.parent.{part_id}",
            parent_by_index.get(node_index) == expected_parent,
            expected_parent,
            parent_by_index.get(node_index),
            "PART node parent matches declared topology",
        )
        part_lods: dict[int, tuple[tuple[float, float, float], ...]] = {}
        for lod in (0, 1):
            mesh_name = f"SM_{spec.asset_id}_{part_id}_LOD{lod}"
            info = mesh_by_name.get(mesh_name)
            required = lod == 0 or bool(
                profile.document.processing.lod1_required or spec.lod_policy == "lod0_lod1"
            )
            add(
                f"part.mesh.{part_id}.lod{lod}",
                (info is not None and info.triangle_count > 0) or not required,
                "required named direct mesh" if required else "optional LOD1",
                "present" if info else "missing",
                "LOD meshes are named per part",
            )
            if info is None:
                continue
            mesh_index, mesh_node = node_mesh.get(mesh_name, (-1, {}))
            direct = parent_by_index.get(mesh_index) == node_index
            identity = mesh_index >= 0 and _matrix_is_identity(_node_matrix(dict(mesh_node)))
            add(
                f"part.mesh_parent.{part_id}.lod{lod}",
                direct and identity,
                name,
                parent_by_index.get(mesh_index),
                "LOD mesh is a direct identity child of its PART node",
            )
            part_lods[lod] = info.points
        lod0 = part_lods.get(0, ())
        if lod0:
            mins = tuple(min(p[a] for p in lod0) for a in range(3))
            maxs = tuple(max(p[a] for p in lod0) for a in range(3))
            size = tuple(maxs[a] - mins[a] for a in range(3))
            add(
                f"part.bounds.{part_id}",
                all(v > 0 for v in size),
                "nondegenerate actual LOD0 vertex bounds",
                size,
                "Per-part bounds use decoded transformed vertices",
            )
            for lod, points in part_lods.items():
                if lod == 0:
                    continue
                lo = tuple(min(p[a] for p in points) for a in range(3))
                hi = tuple(max(p[a] for p in points) for a in range(3))
                same = all(
                    abs(mins[a] - lo[a]) <= 0.01 and abs(maxs[a] - hi[a]) <= 0.01 for a in range(3)
                )
                add(
                    f"lod.bounds.{part_id}",
                    same,
                    (mins, maxs),
                    (lo, hi),
                    "Each part LOD preserves its own actual bounds",
                )
    asset_meshes = {name for name in node_mesh if name.startswith(f"SM_{spec.asset_id}_")}
    expected_meshes = {f"SM_{spec.asset_id}_{part_id}_LOD0" for part_id in parts}
    expected_meshes |= {
        f"SM_{spec.asset_id}_{part_id}_LOD1"
        for part_id in parts
        if profile.document.processing.lod1_required
        or spec.lod_policy == "lod0_lod1"
        or f"SM_{spec.asset_id}_{part_id}_LOD1" in mesh_by_name
    }
    expected_all = set(expected_meshes)
    if spec.collider.policy == "box":
        expected_all.add(f"COL_{spec.asset_id}")
    actual_all = set(node_mesh)
    add(
        "mesh.set",
        actual_all == expected_all,
        sorted(expected_all),
        sorted(actual_all),
        "No extra, unnamed or foreign meshes are accepted",
    )
    expected_nodes = (
        expected_parts
        | expected_all
        | {f"SOCKET_{s.socket_id}" for s in (spec.sockets or [])}
        | {"ROOT"}
    )
    add(
        "nodes.assembly_set",
        set(node_by_name) == expected_nodes,
        sorted(expected_nodes),
        sorted(node_by_name),
        "Only declared parts, meshes, sockets and colliders are allowed",
    )
    add(
        "part.mesh_set",
        expected_meshes == asset_meshes,
        sorted(expected_meshes),
        sorted(asset_meshes),
        "Assembly has exactly declared per-part LOD meshes",
    )
    _validate_assembly_bounds(list(c.meshes), spec, profile, add)
    return tuple(results)


def _run_pivots(context: V07RuleExecutionContext) -> tuple[GeometryFinding, ...]:
    failed = _input_failure(V07RuleGroup.PIVOTS, context)
    if failed:
        return failed
    from gamefactory.adapters.assets.glb_validator import _node_matrix
    from gamefactory.adapters.assets.v07_geometry_validation import (
        _angular_difference_degrees,
        _finite_number,
        _quaternion_matrix,
    )

    c = context.decoded
    spec, profile = c.specification, c.profile
    _, nodes = _nodes(context)
    by_name = {str(node.get("name", "")): (index, node) for index, node in enumerate(nodes)}
    assembly = profile.assembly
    results: list[GeometryFinding] = []

    def add(*args: Any) -> None:
        results.append(_finding(*args))

    for part in spec.parts or []:
        entry = by_name.get(f"PART_{part.part_id}")
        if entry is None:
            continue
        _, node = entry
        local = _node_matrix(node)
        columns = [tuple(local[row][col] for row in range(3)) for col in range(3)]
        lengths = tuple(math.sqrt(sum(value * value for value in column)) for column in columns)
        raw_scale = node.get("scale", (1.0, 1.0, 1.0))
        raw_scale_ok = (
            isinstance(raw_scale, (list, tuple))
            and len(raw_scale) == 3
            and all(_finite_number(v) and float(v) > 0 for v in raw_scale)
            and max(float(v) for v in raw_scale) - min(float(v) for v in raw_scale) <= 1e-5
        )
        orthogonal = all(
            abs(sum(columns[a][i] * columns[b][i] for i in range(3)))
            <= 1e-5 * max(1.0, lengths[a] * lengths[b])
            for a, b in ((0, 1), (0, 2), (1, 2))
        )
        determinant = (
            local[0][0] * (local[1][1] * local[2][2] - local[1][2] * local[2][1])
            - local[0][1] * (local[1][0] * local[2][2] - local[1][2] * local[2][0])
            + local[0][2] * (local[1][0] * local[2][1] - local[1][1] * local[2][0])
        )
        scale_ok = (
            raw_scale_ok
            and all(math.isfinite(v) for col in columns for v in col)
            and min(lengths) > 1e-12
            and max(lengths) - min(lengths) <= 1e-5
            and orthogonal
            and determinant > 1e-12
        )
        add(
            f"part.scale.{part.part_id}",
            scale_ok,
            "uniform positive scale",
            (raw_scale, lengths),
            "Part node scale must be uniform, positive and nonzero",
        )
        pivot = part.pivot
        declared = tuple(float(v) for v in pivot.position_m)
        translation = tuple(local[i][3] for i in range(3))
        tolerance = assembly.pivot_tolerance_m if assembly else 0.01
        collapsed = any(abs(v) > tolerance for v in declared) and all(
            abs(v) <= tolerance for v in translation
        )
        add(
            f"pivot.collapsed.{part.part_id}",
            not collapsed,
            "declared nonzero pivot retained",
            translation,
            "A declared nonzero pivot cannot collapse to its parent origin",
        )
        position_ok = all(
            abs(a - b) <= tolerance for a, b in zip(translation, declared, strict=True)
        )
        add(
            f"pivot.position.{part.part_id}",
            position_ok,
            declared,
            translation,
            "Local part translation matches declared pivot",
        )
        expected = _quaternion_matrix(pivot.basis)
        actual = [
            [
                local[row][col] / lengths[col] if lengths[col] > 1e-12 else math.nan
                for col in range(3)
            ]
            for row in range(3)
        ]
        angle_tol = assembly.basis_tolerance_deg if assembly else 0.01
        rotation_ok = (
            min(lengths) > 1e-12 and _angular_difference_degrees(actual, expected) <= angle_tol
        )
        add(
            f"pivot.orientation.{part.part_id}",
            rotation_ok,
            pivot.basis,
            "local basis",
            "Part rotation matches declared pivot basis",
        )
        extras = node.get("extras")
        motion = pivot.motion
        extras_ok = isinstance(extras, Mapping) and extras.get("gf_motion") == motion.kind
        if motion.kind == "fixed":
            extras_ok = extras_ok and "gf_axis" not in (extras or {})
        else:
            axis = extras.get("gf_axis") if isinstance(extras, Mapping) else None
            extras_ok = (
                extras_ok
                and isinstance(axis, (list, tuple))
                and len(axis) == 3
                and motion.axis is not None
                and all(
                    _finite_number(axis[i]) and abs(float(axis[i]) - float(motion.axis[i])) <= 1e-6
                    for i in range(3)
                )
            )
        constraint = assembly.role_motion_constraints.get(part.role) if assembly else None
        if constraint:
            extras_ok = extras_ok and motion.kind == constraint.kind
            if constraint.axis is not None:
                extras_ok = (
                    extras_ok
                    and motion.axis is not None
                    and all(
                        abs(float(a) - float(b)) <= 1e-6
                        for a, b in zip(motion.axis, constraint.axis, strict=True)
                    )
                )
        add(
            f"pivot.axis.{part.part_id}",
            bool(extras_ok),
            f"{motion.kind}:{motion.axis}",
            extras,
            "Motion kind and axis match typed specification and GLB extras",
        )
    return tuple(results)


def _run_source_orientation(context: V07RuleExecutionContext) -> tuple[GeometryFinding, ...]:
    failed = _input_failure(V07RuleGroup.SOURCE_ORIENTATION, context)
    if failed:
        return failed
    from gamefactory.adapters.assets.v07_geometry_validation import _valid_source_observation

    c = context.decoded
    document = thaw(c.document or {})
    passed = _valid_source_observation(
        c.source_observation,
        c.artifact_sha256,
        document,
        c.binary,
        list(c.meshes),
        c.specification,
        context.max_file_size_bytes,
    )
    return (
        _finding(
            "orientation.source_front",
            passed,
            "verified retained source bytes and one allowed normalization",
            c.source_observation,
            "Source orientation cannot be inferred from the processed GLB",
        ),
    )


def _run_sockets(context: V07RuleExecutionContext) -> tuple[GeometryFinding, ...]:
    failed = _input_failure(V07RuleGroup.SOCKETS, context)
    if failed:
        return failed
    from gamefactory.adapters.assets.v07_geometry_validation import _validate_sockets

    c = context.decoded
    document, nodes = _nodes(context)
    by_name = {str(node.get("name", "")): (index, node) for index, node in enumerate(nodes)}
    node_mesh = {name: value for name, value in by_name.items() if "mesh" in value[1]}
    results: list[GeometryFinding] = []
    _validate_sockets(
        document,
        by_name,
        node_mesh,
        c.parent_by_index,
        _world(context),
        c.specification,
        c.profile,
        list(c.meshes),
        lambda *args: results.append(_finding(*args)),
    )
    return tuple(results)


def _run_capsule(context: V07RuleExecutionContext) -> tuple[GeometryFinding, ...]:
    failed = _input_failure(V07RuleGroup.CAPSULE, context)
    if failed:
        return failed
    from gamefactory.adapters.assets.v07_geometry_validation import _validate_capsule

    results: list[GeometryFinding] = []
    _validate_capsule(
        context.decoded.specification,
        context.decoded.profile,
        list(context.decoded.meshes),
        lambda *args: results.append(_finding(*args)),
    )
    return tuple(results)


def _run_box(context: V07RuleExecutionContext) -> tuple[GeometryFinding, ...]:
    failed = _input_failure(V07RuleGroup.BOX_COLLIDER, context)
    if failed:
        return failed
    from gamefactory.adapters.assets.glb_validator import _node_matrix
    from gamefactory.adapters.assets.v07_geometry_validation import (
        _validate_assembly_box,
        box_mesh_geometry_matches,
    )

    document, _ = _nodes(context)
    results: list[GeometryFinding] = []
    if context.decoded.profile.geometry_mode == "assembly":
        _validate_assembly_box(
            document,
            context.decoded.binary,
            list(context.decoded.meshes),
            context.decoded.specification,
            context.decoded.profile,
            lambda *args: results.append(_finding(*args)),
        )
    else:
        spec = context.decoded.specification
        nodes = document.get("nodes", [])
        scene_index = int(document.get("scene", 0))
        scenes = document.get("scenes", [])
        roots = (
            set(scenes[scene_index].get("nodes", [])) if 0 <= scene_index < len(scenes) else set()
        )
        collider_name = f"COL_{spec.asset_id}"
        collider_index = next(
            (i for i, node in enumerate(nodes) if node.get("name") == collider_name), -1
        )
        collider = next(
            (mesh for mesh in context.decoded.meshes if mesh.name == collider_name), None
        )
        visual = next(
            (mesh for mesh in context.decoded.meshes if mesh.name == f"SM_{spec.asset_id}_LOD0"),
            None,
        )
        collider_transform_ok = (
            collider_index in roots
            and collider_index >= 0
            and _matrix_is_identity(_node_matrix(nodes[collider_index]))
        )
        results.append(
            _finding(
                "collider.parent",
                collider_transform_ok,
                "identity scene-root collider",
                collider_index,
                "Single-mesh box collider is a direct identity scene node",
            )
        )
        okay = collider is not None and visual is not None
        if okay and collider is not None and visual is not None:
            cmin = tuple(min(p[i] for p in collider.points) for i in range(3))
            cmax = tuple(max(p[i] for p in collider.points) for i in range(3))
            vmin = (
                min(p[0] for p in visual.points),
                min(p[1] for p in visual.points),
                min(p[2] for p in visual.points),
            )
            vmax = (
                max(p[0] for p in visual.points),
                max(p[1] for p in visual.points),
                max(p[2] for p in visual.points),
            )
            tolerance = context.decoded.profile.document.processing.dimension_tolerance_m
            okay = box_mesh_geometry_matches(
                document,
                context.decoded.binary,
                collider_name,
                collider.points,
                vmin,
                vmax,
                tolerance,
            ) and all(
                abs(cmin[i] - vmin[i]) <= tolerance and abs(cmax[i] - vmax[i]) <= tolerance
                for i in range(3)
            )
        results.append(
            _finding(
                "collider.box",
                bool(okay),
                "single-mesh visual rest bounds",
                "valid" if okay else "missing or mismatched",
                "Single-mesh box collider matches actual LOD0 bounds",
            )
        )
    return tuple(results)


def _run_single_mesh(context: V07RuleExecutionContext) -> tuple[GeometryFinding, ...]:
    failed = _input_failure(V07RuleGroup.SINGLE_MESH, context)
    if failed:
        return failed
    from gamefactory.adapters.assets.v07_geometry_validation import _validate_single_mesh

    c = context.decoded
    document, _ = _nodes(context)
    results: list[GeometryFinding] = []
    _validate_single_mesh(
        document,
        list(c.meshes),
        c.specification,
        c.profile,
        lambda *args: results.append(_finding(*args)),
    )
    return tuple(results)


RULE_RUNNERS: Mapping[V07RuleGroup, Runner] = MappingProxyType(
    {
        V07RuleGroup.CORE: _run_core,
        V07RuleGroup.ASSEMBLY: _run_assembly,
        V07RuleGroup.SOURCE_ORIENTATION: _run_source_orientation,
        V07RuleGroup.PIVOTS: _run_pivots,
        V07RuleGroup.SOCKETS: _run_sockets,
        V07RuleGroup.CAPSULE: _run_capsule,
        V07RuleGroup.SINGLE_MESH: _run_single_mesh,
        V07RuleGroup.BOX_COLLIDER: _run_box,
    }
)


def require_implemented_v07_groups(groups: tuple[V07RuleGroup, ...]) -> None:
    if not groups:
        raise ValueError("V0.7 validation requires a non-empty rule plan")
    if any(not isinstance(group, V07RuleGroup) for group in groups):
        raise ValueError("V0.7 rule plan requires closed V07RuleGroup capabilities")
    if len(set(groups)) != len(groups):
        raise ValueError("V0.7 rule plan cannot contain duplicate groups")
    unknown = [group for group in groups if group not in RULE_RUNNERS]
    if unknown:
        raise ValueError(f"unimplemented V0.7 rule groups: {unknown}")
    if groups != tuple(group for group in V07RuleGroup if group in groups):
        raise ValueError("V0.7 rule groups must follow the canonical order")


def evaluate_ordered_v07_rules(
    context: V07RuleExecutionContext, groups: tuple[V07RuleGroup, ...]
) -> tuple[GeometryFinding, ...]:
    require_implemented_v07_groups(groups)
    return tuple(finding for group in groups for finding in RULE_RUNNERS[group](context))
