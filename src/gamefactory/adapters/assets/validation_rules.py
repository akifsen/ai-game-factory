"""Closed, ordered rule composition for the supported legacy GLB contract."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from gamefactory.adapters.assets.glb_validator import _InvalidGLB, _MeshInfo
from gamefactory.adapters.assets.validation_context import DecodedValidationContext


class Capability(StrEnum):
    LEGACY_SINGLE_MESH = "legacy_single_mesh"
    BOX_COLLIDER = "box_collider"


class RuleGroup(StrEnum):
    MESH_STRUCTURE = "mesh_structure"
    COLLIDER_BOX = "collider_box"
    BUDGETS = "budgets"
    DIMENSIONS_ORIGIN_SNAP = "dimensions_origin_snap"
    LOD_BOUNDS = "lod_bounds"
    ORIENTATION = "orientation"


@dataclass(frozen=True)
class GroupContract:
    group: RuleGroup
    requires: frozenset[Capability]


ORDERED_GROUPS = (
    GroupContract(RuleGroup.MESH_STRUCTURE, frozenset({Capability.LEGACY_SINGLE_MESH})),
    GroupContract(RuleGroup.COLLIDER_BOX, frozenset({Capability.BOX_COLLIDER})),
    GroupContract(RuleGroup.BUDGETS, frozenset({Capability.LEGACY_SINGLE_MESH})),
    GroupContract(RuleGroup.DIMENSIONS_ORIGIN_SNAP, frozenset({Capability.LEGACY_SINGLE_MESH})),
    GroupContract(RuleGroup.LOD_BOUNDS, frozenset({Capability.LEGACY_SINGLE_MESH})),
    GroupContract(RuleGroup.ORIENTATION, frozenset({Capability.LEGACY_SINGLE_MESH})),
)


def select_rule_groups(capabilities: frozenset[Capability]) -> tuple[RuleGroup, ...]:
    if any(not isinstance(item, Capability) for item in capabilities):
        raise ValueError("unknown validation capabilities")
    return tuple(group.group for group in ORDERED_GROUPS if group.requires <= capabilities)


def require_implemented_groups(groups: tuple[RuleGroup, ...]) -> None:
    known = {contract.group for contract in ORDERED_GROUPS}
    for group in groups:
        if not isinstance(group, RuleGroup) or group not in known:
            raise ValueError(f"validation rule group is not implemented: {group}")
    if not groups:
        raise ValueError("no implemented validation group selected")
    if len(set(groups)) != len(groups):
        raise ValueError("validation rule group plan contains duplicates")
    canonical = tuple(contract.group for contract in ORDERED_GROUPS if contract.group in groups)
    if groups != canonical:
        raise ValueError("validation rule groups are not in canonical order")


@dataclass(frozen=True)
class RuleFacts:
    names: tuple[str, ...]
    by_name: dict[str, _MeshInfo]
    lod0: _MeshInfo | None
    lod1: _MeshInfo | None
    colliders: tuple[_MeshInfo, ...]
    lod1_required: bool
    visual_points: tuple[tuple[float, float, float], ...]
    visual_mins: tuple[float, ...]
    visual_maxs: tuple[float, ...]
    mins: tuple[float, ...] | None
    maxs: tuple[float, ...] | None
    actual: tuple[float, ...] | None


def mesh_structure(c: DecodedValidationContext, add: Callable[..., None]) -> RuleFacts:
    spec, profile = c.asset_specification, c.profile
    names = tuple(mesh.name for mesh in c.meshes)
    add(
        "nodes.unique",
        len(names) == len(set(names)),
        "unique mesh node names",
        str(list(names)),
        "Duplicate names are ambiguous during engine import",
    )
    expected_names = profile.expected_mesh_names(spec)
    add(
        "nodes.profile",
        set(names) == expected_names,
        f"exactly {sorted(expected_names)}",
        str(list(names)),
        "Unexpected or missing mesh nodes are rejected",
    )
    by_name = {mesh.name: mesh for mesh in c.meshes}
    lod0 = by_name.get(f"SM_{spec.asset_id}_LOD0")
    lod1 = by_name.get(f"SM_{spec.asset_id}_LOD1")
    collider_name = f"COL_{spec.asset_id}"
    colliders = (by_name[collider_name],) if collider_name in by_name else ()
    add(
        "mesh.nonempty",
        bool(c.points) and c.triangle_count > 0,
        "nonempty triangle geometry",
        f"{c.triangle_count} triangles",
        "Actual POSITION and index data decoded",
    )
    add(
        "lod0.present",
        lod0 is not None and lod0.triangle_count > 0,
        "LOD0 mesh geometry",
        "present" if lod0 else "missing",
        "LOD0 node must reference nonempty triangles",
    )
    required = bool(c.processing_contract["lod1_required"])
    add(
        "lod1.present",
        (lod1 is not None and lod1.triangle_count > 0) or not required,
        "LOD1 mesh geometry" if required else "LOD1 optional",
        "present" if lod1 else "missing",
        "LOD1 is required by the profile contract"
        if required
        else "LOD1 is optional for this profile",
    )
    visual = tuple(lod0.points) if lod0 else ()
    mins = tuple(min(p[i] for p in visual) for i in range(3)) if visual else (0.0, 0.0, 0.0)
    maxs = tuple(max(p[i] for p in visual) for i in range(3)) if visual else (0.0, 0.0, 0.0)
    return RuleFacts(
        names,
        by_name,
        lod0,
        lod1,
        colliders,
        required,
        visual,
        mins,
        maxs,
        mins if visual else None,
        maxs if visual else None,
        tuple(maxs[i] - mins[i] for i in range(3)) if visual else None,
    )


def collider_box(c: DecodedValidationContext, f: RuleFacts, add: Callable[..., None]) -> None:
    ok = bool(f.colliders) and all(
        m.triangle_count == 12
        and len({tuple(round(v, 6) for v in p) for p in m.points}) == 8
        and abs(
            (max(p[0] for p in m.points) - min(p[0] for p in m.points))
            * (max(p[1] for p in m.points) - min(p[1] for p in m.points))
            * (max(p[2] for p in m.points) - min(p[2] for p in m.points))
        )
        > 1e-9
        and all(
            abs(min(p[i] for p in m.points) - f.visual_mins[i]) <= 0.01
            and abs(max(p[i] for p in m.points) - f.visual_maxs[i]) <= 0.01
            for i in range(3)
        )
        for m in f.colliders
    )
    add(
        "collider.geometry",
        ok,
        "axis-aligned box geometry matching visual bounds",
        f"{len(f.colliders)} collider mesh(es)",
        "Collider decoded vertices and triangles are checked against visual bounds",
    )


def budgets(c: DecodedValidationContext, f: RuleFacts, add: Callable[..., None]) -> None:
    spec = c.asset_specification
    add(
        "geometry.lod0_budget",
        f.lod0 is not None and f.lod0.triangle_count <= spec.geometry_budget.max_triangles_lod0,
        f"<= {spec.geometry_budget.max_triangles_lod0}",
        str(f.lod0.triangle_count if f.lod0 else 0),
        "LOD0 triangle budget",
    )
    add(
        "geometry.lod1_budget",
        f.lod1 is None or f.lod1.triangle_count <= spec.geometry_budget.max_triangles_lod1,
        f"<= {spec.geometry_budget.max_triangles_lod1}",
        str(f.lod1.triangle_count if f.lod1 else 0),
        "LOD1 triangle budget",
    )
    count = len(c.document.get("materials", []))
    add(
        "materials.budget",
        count <= spec.material_budget.max_materials,
        f"<= {spec.material_budget.max_materials}",
        str(count),
        "Declared material count in GLB",
    )
    texture_max = max((max(w, h) for w, h in c.textures), default=0)
    add(
        "textures.dimension",
        texture_max <= spec.texture_budget.max_dimension,
        f"<= {spec.texture_budget.max_dimension}px",
        str(texture_max),
        "Embedded texture dimensions decoded from image bytes",
    )


def dimensions_origin_snap(
    c: DecodedValidationContext, f: RuleFacts, add: Callable[..., None]
) -> None:
    if not f.visual_points or f.actual is None or f.mins is None or f.maxs is None:
        raise _InvalidGLB("no non-collider visual geometry")
    spec, contract = c.asset_specification, c.processing_contract
    actual, mins, maxs = f.actual, f.mins, f.maxs
    assert mins is not None and maxs is not None and actual is not None
    target = [spec.dimensions.width_m, spec.dimensions.height_m, spec.dimensions.depth_m]
    tolerance = float(contract["dimension_tolerance_m"])
    add(
        "scale.bounds",
        all(abs(a - t) <= tolerance for a, t in zip(actual, target, strict=True)),
        f"X/Y/Z = {target}m ± {tolerance}m",
        str(list(actual)),
        "Dimensions measured from transformed decoded vertices",
    )
    origin_ok = (
        abs((mins[0] + maxs[0]) / 2) <= tolerance and abs((mins[2] + maxs[2]) / 2) <= tolerance
    )
    if spec.origin_policy == "bottom_center":
        origin_ok = origin_ok and abs(mins[1]) <= tolerance
    elif spec.origin_policy == "center":
        origin_ok = origin_ok and abs((mins[1] + maxs[1]) / 2) <= tolerance
    add(
        "origin.policy",
        origin_ok,
        spec.origin_policy,
        f"min={list(mins)}, max={list(maxs)}",
        "Origin evaluated from transformed geometry",
    )
    grid = contract.get("snap_grid_m")
    if grid:
        snap = all(
            abs((value / float(grid)) - round(value / float(grid))) * float(grid) <= tolerance
            for value in actual
        )
        add(
            "dimensions.snap",
            snap and origin_ok,
            f"module axes are multiples of {grid} m and the origin is on the snap pivot",
            str(list(actual)),
            "Modular dimensions and origin must land on the profile snap grid",
        )


def lod_bounds(c: DecodedValidationContext, f: RuleFacts, add: Callable[..., None]) -> None:
    pts = f.lod1.points if f.lod1 else ()
    tolerance = float(c.processing_contract["dimension_tolerance_m"])
    mins, maxs = f.mins, f.maxs
    if not f.lod1_required and not pts:
        ok = True
    else:
        ok = bool(f.lod0 and pts and mins is not None and maxs is not None)
        if ok:
            assert mins is not None and maxs is not None
            ok = all(
                abs(min(p[i] for p in pts) - mins[i]) <= tolerance
                and abs(max(p[i] for p in pts) - maxs[i]) <= tolerance
                for i in range(3)
            )
    add(
        "lod1.bounds",
        ok,
        f"LOD1 bounds match LOD0 ± {tolerance}m",
        str(f.lod1 and f.lod1.points[:1]),
        "LOD1 geometry must preserve the visual envelope",
    )


def orientation(c: DecodedValidationContext, add: Callable[..., None]) -> None:
    identity = True
    for index, node in enumerate(c.document.get("nodes", [])):
        if "mesh" not in node or str(node.get("name") or "").startswith("COL_"):
            continue
        expected = [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
        identity = identity and all(
            abs(c.world_matrices[index][r][j] - expected[r][j]) <= 1e-5
            for r in range(4)
            for j in range(4)
        )
    add(
        "orientation.identity",
        identity,
        "mesh nodes carry no residual transforms; +Y up/-Z front coordinates",
        "identity" if identity else "non-identity transform",
        "Coordinate convention is checked through node transforms and world-space bounds",
    )
    if c.material_ids and max(c.material_ids) >= len(c.document.get("materials", [])):
        raise _InvalidGLB("primitive references nonexistent material")


RULE_RUNNERS: dict[RuleGroup, Callable[..., object]] = {
    RuleGroup.MESH_STRUCTURE: mesh_structure,
    RuleGroup.COLLIDER_BOX: collider_box,
    RuleGroup.BUDGETS: budgets,
    RuleGroup.DIMENSIONS_ORIGIN_SNAP: dimensions_origin_snap,
    RuleGroup.LOD_BOUNDS: lod_bounds,
    RuleGroup.ORIENTATION: orientation,
}


def evaluate_ordered_rules(
    c: DecodedValidationContext, groups: tuple[RuleGroup, ...], add: Callable[..., None]
) -> None:
    require_implemented_groups(groups)
    facts: RuleFacts | None = None
    for group in groups:
        runner = RULE_RUNNERS[group]
        if group is RuleGroup.MESH_STRUCTURE:
            facts = runner(c, add)  # type: ignore[assignment]
        elif group is RuleGroup.ORIENTATION:
            runner(c, add)
        else:
            if facts is None:
                raise ValueError("mesh structure must precede dependent rule groups")
            runner(c, facts, add)
