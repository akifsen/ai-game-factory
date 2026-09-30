"""Closed rule groups and their composition for GLB validation (ADR 0018).

Each rule is a pure function ``RuleInput -> list[ValidationFinding]``. A rule
group is an ordered, closed tuple of rules. The composition used for an asset is
derived from typed profile/specification capabilities (geometry mode, collider
policy, declared sockets, source normalization), never from a profile id.

The historical single-mesh box profiles (``static_prop@1``, ``pickup@1``,
``modular_piece@1``) use the fixed :data:`LEGACY_COMPOSITION`, which reproduces
the V0.4-V0.6 finding order exactly (see ``tests/golden``).

A rule may raise ``InvalidGLB``; the driver then appends a failing ``glb.parse``
finding and stops, as the V0.6 validator did.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from gamefactory.core.domain import transforms as gm
from gamefactory.core.domain.asset_contracts import ValidationFinding
from gamefactory.core.domain.asset_contracts import ValidationFindingSeverity as Severity


class InvalidGLB(ValueError):
    """The GLB is outside the safe, supported subset."""


@dataclass(frozen=True)
class MeshInfo:
    name: str
    triangle_count: int
    material_ids: frozenset[int]
    points: tuple[tuple[float, float, float], ...]
    node_index: int = -1
    local_points: tuple[tuple[float, float, float], ...] = ()


@dataclass(frozen=True)
class NodeInfo:
    index: int
    name: str
    parent: int | None
    children: tuple[int, ...]
    mesh: int | None
    local: gm.Matrix
    world: gm.Matrix
    extras: dict[str, Any]


@dataclass(frozen=True)
class ParsedGLB:
    document: dict[str, Any]
    binary: bytes
    sha256: str
    meshes: list[MeshInfo]
    points: list[tuple[float, float, float]]
    material_ids: set[int]
    textures: list[tuple[int, int]]
    triangles: int
    world: dict[int, gm.Matrix]
    nodes: list[NodeInfo]
    scene_roots: tuple[int, ...]

    def node_named(self, name: str) -> NodeInfo | None:
        matches = [node for node in self.nodes if node.name == name]
        return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class NormalizationRecord:
    """``source_front`` normalization of an operator-authored assembly (ADR 0014)."""

    source_front: str | None
    normalization_applied: bool
    quaternion_xyzw: tuple[float, float, float, float]
    resulting_front: str
    source: ParsedGLB | None = None

    def as_dict(self) -> dict[str, Any]:
        rotation = gm.quat_to_rotation(self.quaternion_xyzw)
        matrix = [[*[round(v, 12) + 0.0 for v in row], 0.0] for row in rotation]
        matrix.append([0.0, 0.0, 0.0, 1.0])
        return {
            "source_front": self.source_front,
            "normalization_applied": self.normalization_applied,
            "normalization_transform": {
                "quaternion_xyzw": [float(v) for v in self.quaternion_xyzw],
                "matrix": matrix,
            },
            "resulting_front": self.resulting_front,
        }


def normalization_for(source_front: str | None) -> tuple[bool, gm.Quat]:
    """Return (applied, quaternion) for a declared source front, or raise."""
    if source_front == "-Z":
        return False, gm.IDENTITY_QUAT
    if source_front == "+Z":
        return True, gm.SOURCE_FRONT_PLUS_Z_QUAT
    raise ValueError(f"source_front must be '-Z' or '+Z', got {source_front!r}")


@dataclass
class RuleInput:
    """Everything a rule may read. Derived values are computed lazily and cached."""

    artifact: str
    parsed: ParsedGLB
    spec: Any
    contract: dict[str, Any]
    assembly: Any = None  # AssemblyContract for assembly profiles
    normalization: NormalizationRecord | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def finding(
        self,
        rule: str,
        passed: bool,
        expected: str,
        actual: str,
        message: str,
        warning: bool = False,
    ) -> ValidationFinding:
        severity = Severity.PASS if passed else Severity.WARNING if warning else Severity.FAIL
        return ValidationFinding(rule, severity, expected, actual, self.artifact, message)

    # --- names -----------------------------------------------------------------

    @property
    def asset_id(self) -> str:
        return str(self.spec.asset_id)

    @property
    def lod1_required(self) -> bool:
        return bool(self.contract["lod1_required"])

    @property
    def tolerance(self) -> float:
        return float(self.contract["dimension_tolerance_m"])

    @property
    def collider_name(self) -> str:
        return f"COL_{self.asset_id}"

    @cached_property
    def by_name(self) -> dict[str, MeshInfo]:
        return {info.name: info for info in self.parsed.meshes}

    # --- single mesh ------------------------------------------------------------

    @cached_property
    def lod0(self) -> MeshInfo | None:
        return self.by_name.get(f"SM_{self.asset_id}_LOD0")

    @cached_property
    def lod1(self) -> MeshInfo | None:
        return self.by_name.get(f"SM_{self.asset_id}_LOD1")

    # --- assembly ----------------------------------------------------------------

    @property
    def is_assembly(self) -> bool:
        return self.contract.get("geometry_mode") == "assembly"

    @cached_property
    def parts(self) -> list[Any]:
        return list(getattr(self.spec, "parts", None) or [])

    @cached_property
    def sockets(self) -> list[Any]:
        return list(getattr(self.spec, "sockets", None) or [])

    def part_mesh(self, part_id: str, lod: int) -> MeshInfo | None:
        return self.by_name.get(f"SM_{self.asset_id}_{part_id}_LOD{lod}")

    @cached_property
    def lod0_meshes(self) -> list[MeshInfo]:
        if not self.is_assembly:
            return [self.lod0] if self.lod0 else []
        return [m for p in self.parts if (m := self.part_mesh(p.part_id, 0)) is not None]

    @cached_property
    def lod1_meshes(self) -> list[MeshInfo]:
        if not self.is_assembly:
            return [self.lod1] if self.lod1 else []
        return [m for p in self.parts if (m := self.part_mesh(p.part_id, 1)) is not None]

    @cached_property
    def lod0_triangles(self) -> int:
        return sum(m.triangle_count for m in self.lod0_meshes)

    @cached_property
    def lod1_triangles(self) -> int:
        return sum(m.triangle_count for m in self.lod1_meshes)

    @cached_property
    def visual_points(self) -> list[tuple[float, float, float]]:
        return [p for m in self.lod0_meshes for p in m.points]

    def visual_extent(self) -> tuple[list[float], list[float]]:
        points = self.visual_points
        if not points:
            return [0.0] * 3, [0.0] * 3
        return (
            [min(p[i] for p in points) for i in range(3)],
            [max(p[i] for p in points) for i in range(3)],
        )

    def measured_bounds(self) -> tuple[list[float], list[float], list[float]]:
        """Visual min, max and size. Raises when there is no visual geometry."""
        if not self.visual_points:
            raise InvalidGLB("no non-collider visual geometry")
        mins, maxs = self.visual_extent()
        return mins, maxs, [maxs[i] - mins[i] for i in range(3)]

    def origin_ok(self) -> bool:
        mins, maxs, _ = self.measured_bounds()
        tol = self.tolerance
        ok = abs((mins[0] + maxs[0]) / 2) <= tol and abs((mins[2] + maxs[2]) / 2) <= tol
        if self.spec.origin_policy == "bottom_center":
            ok = ok and abs(mins[1]) <= tol
        elif self.spec.origin_policy == "center":
            ok = ok and abs((mins[1] + maxs[1]) / 2) <= tol
        return ok


Rule = Callable[[RuleInput], list[ValidationFinding]]


# =============================================================================
# core
# =============================================================================


def rule_glb_parse(inp: RuleInput) -> list[ValidationFinding]:
    return [
        inp.finding(
            "glb.parse",
            True,
            "GLB 2.0 with bounded embedded buffers",
            "parsed",
            "GLB structure and accessors are valid",
        )
    ]


def rule_glb_hash(inp: RuleInput) -> list[ValidationFinding]:
    return [
        inp.finding(
            "glb.hash", True, "SHA-256 recorded", inp.parsed.sha256, "Artifact hash computed"
        )
    ]


def rule_mesh_nonempty(inp: RuleInput) -> list[ValidationFinding]:
    tris = inp.parsed.triangles
    return [
        inp.finding(
            "mesh.nonempty",
            bool(inp.parsed.points) and tris > 0,
            "nonempty triangle geometry",
            f"{tris} triangles",
            "Actual POSITION and index data decoded",
        )
    ]


def rule_lod0_budget(inp: RuleInput) -> list[ValidationFinding]:
    budget = inp.spec.geometry_budget.max_triangles_lod0
    tris = inp.lod0_triangles
    return [
        inp.finding(
            "geometry.lod0_budget",
            bool(inp.lod0_meshes) and tris <= budget,
            f"<= {budget}",
            str(tris),
            "LOD0 triangle budget",
        )
    ]


def rule_lod1_budget(inp: RuleInput) -> list[ValidationFinding]:
    budget = inp.spec.geometry_budget.max_triangles_lod1
    tris = inp.lod1_triangles
    return [
        inp.finding(
            "geometry.lod1_budget",
            not inp.lod1_meshes or tris <= budget,
            f"<= {budget}",
            str(tris),
            "LOD1 triangle budget",
        )
    ]


def rule_materials_budget(inp: RuleInput) -> list[ValidationFinding]:
    count = len(inp.parsed.document.get("materials", []))
    budget = inp.spec.material_budget.max_materials
    return [
        inp.finding(
            "materials.budget",
            count <= budget,
            f"<= {budget}",
            str(count),
            "Declared material count in GLB",
        )
    ]


def rule_textures_dimension(inp: RuleInput) -> list[ValidationFinding]:
    texture_max = max((max(w, h) for w, h in inp.parsed.textures), default=0)
    budget = inp.spec.texture_budget.max_dimension
    return [
        inp.finding(
            "textures.dimension",
            texture_max <= budget,
            f"<= {budget}px",
            str(texture_max),
            "Embedded texture dimensions decoded from image bytes",
        )
    ]


def rule_scale_bounds(inp: RuleInput) -> list[ValidationFinding]:
    # Bounds include visual meshes only; the collider is checked independently.
    _, _, actual = inp.measured_bounds()
    dims = inp.spec.dimensions
    target = [dims.width_m, dims.height_m, dims.depth_m]
    tol = inp.tolerance
    ok = all(abs(a - t) <= tol for a, t in zip(actual, target, strict=True))
    return [
        inp.finding(
            "scale.bounds",
            ok,
            f"X/Y/Z = {target}m ± {tol}m",
            str(actual),
            "Dimensions measured from transformed decoded vertices",
        )
    ]


def rule_origin_policy(inp: RuleInput) -> list[ValidationFinding]:
    mins, maxs, _ = inp.measured_bounds()
    return [
        inp.finding(
            "origin.policy",
            inp.origin_ok(),
            inp.spec.origin_policy,
            f"min={mins}, max={maxs}",
            "Origin evaluated from transformed geometry",
        )
    ]


def rule_material_references(inp: RuleInput) -> list[ValidationFinding]:
    material_ids = inp.parsed.material_ids
    if material_ids and max(material_ids) >= len(inp.parsed.document.get("materials", [])):
        raise InvalidGLB("primitive references nonexistent material")
    return []


# =============================================================================
# single_mesh
# =============================================================================


def _expected_single_mesh_names(inp: RuleInput) -> set[str]:
    names = {f"SM_{inp.asset_id}_LOD0"}
    if inp.lod1_required:
        names.add(f"SM_{inp.asset_id}_LOD1")
    if inp.contract.get("collider_policy", "box") == "box":
        names.add(inp.collider_name)
    return names


def rule_nodes_unique(inp: RuleInput) -> list[ValidationFinding]:
    names = [info.name for info in inp.parsed.meshes]
    return [
        inp.finding(
            "nodes.unique",
            len(names) == len(set(names)),
            "unique mesh node names",
            str(names),
            "Duplicate names are ambiguous during engine import",
        )
    ]


def rule_nodes_profile(inp: RuleInput) -> list[ValidationFinding]:
    names = [info.name for info in inp.parsed.meshes]
    expected = _expected_single_mesh_names(inp)
    return [
        inp.finding(
            "nodes.profile",
            set(names) == expected,
            f"exactly {sorted(expected)}",
            str(names),
            "Unexpected or missing mesh nodes are rejected",
        )
    ]


def rule_lod0_present(inp: RuleInput) -> list[ValidationFinding]:
    lod0 = inp.lod0
    return [
        inp.finding(
            "lod0.present",
            lod0 is not None and lod0.triangle_count > 0,
            "LOD0 mesh geometry",
            "present" if lod0 else "missing",
            "LOD0 node must reference nonempty triangles",
        )
    ]


def rule_lod1_present(inp: RuleInput) -> list[ValidationFinding]:
    lod1, required = inp.lod1, inp.lod1_required
    return [
        inp.finding(
            "lod1.present",
            (lod1 is not None and lod1.triangle_count > 0) or not required,
            "LOD1 mesh geometry" if required else "LOD1 optional",
            "present" if lod1 else "missing",
            "LOD1 is required by the profile contract"
            if required
            else "LOD1 is optional for this profile",
        )
    ]


def rule_dimensions_snap(inp: RuleInput) -> list[ValidationFinding]:
    snap_grid = inp.contract.get("snap_grid_m")
    if not snap_grid:
        return []
    _, _, actual = inp.measured_bounds()
    grid, tol = float(snap_grid), inp.tolerance
    snap_ok = all(abs((v / grid) - round(v / grid)) * grid <= tol for v in actual)
    return [
        inp.finding(
            "dimensions.snap",
            snap_ok and inp.origin_ok(),
            f"module axes are multiples of {snap_grid} m and the origin is on the snap pivot",
            str(actual),
            "Modular dimensions and origin must land on the profile snap grid",
        )
    ]


def rule_lod1_bounds(inp: RuleInput) -> list[ValidationFinding]:
    mins, maxs, _ = inp.measured_bounds()
    lod0, lod1, tol = inp.lod0, inp.lod1, inp.tolerance
    lod1_points = lod1.points if lod1 else ()
    if not inp.lod1_required and not lod1_points:
        ok = True
    else:
        ok = bool(lod0 and lod1_points) and all(
            abs(min(p[i] for p in lod1_points) - mins[i]) <= tol
            and abs(max(p[i] for p in lod1_points) - maxs[i]) <= tol
            for i in range(3)
        )
    return [
        inp.finding(
            "lod1.bounds",
            ok,
            f"LOD1 bounds match LOD0 ± {tol}m",
            str(lod1 and lod1.points[:1]),
            "LOD1 geometry must preserve the visual envelope",
        )
    ]


def rule_orientation_identity(inp: RuleInput) -> list[ValidationFinding]:
    ident = gm.identity()
    ok = True
    for node_index, node in enumerate(inp.parsed.document.get("nodes", [])):
        if "mesh" not in node:
            continue
        if str(node.get("name") or "").startswith("COL_"):
            continue
        ok = ok and all(
            abs(inp.parsed.world[node_index][r][c] - ident[r][c]) <= 1e-5
            for r in range(4)
            for c in range(4)
        )
    return [
        inp.finding(
            "orientation.identity",
            ok,
            "mesh nodes carry no residual transforms; +Y up/-Z front coordinates",
            "identity" if ok else "non-identity transform",
            "Coordinate convention is checked through node transforms and world-space bounds",
        )
    ]


# =============================================================================
# collider_box
# =============================================================================


def rule_collider_geometry(inp: RuleInput) -> list[ValidationFinding]:
    collider = inp.by_name.get(inp.collider_name)
    colliders = [collider] if collider else []
    vmin, vmax = inp.visual_extent()

    def box_ok(c: MeshInfo) -> bool:
        pts = c.points
        extent = [max(p[i] for p in pts) - min(p[i] for p in pts) for i in range(3)]
        return (
            c.triangle_count == 12
            and len({tuple(round(v, 6) for v in p) for p in pts}) == 8
            and abs(extent[0] * extent[1] * extent[2]) > 1e-9
            and all(
                abs(min(p[i] for p in pts) - vmin[i]) <= 0.01
                and abs(max(p[i] for p in pts) - vmax[i]) <= 0.01
                for i in range(3)
            )
        )

    ok = bool(colliders) and all(box_ok(c) for c in colliders)
    if ok and inp.is_assembly:
        root = inp.parsed.node_named("ROOT")
        node = inp.parsed.nodes[colliders[0].node_index]
        ok = root is not None and node.parent == root.index
    return [
        inp.finding(
            "collider.geometry",
            ok,
            "axis-aligned box geometry matching visual bounds",
            f"{len(colliders)} collider mesh(es)",
            "Collider decoded vertices and triangles are checked against visual bounds",
        )
    ]


# =============================================================================
# collider_capsule (ADR 0015)
# =============================================================================


def rule_capsule_shape(inp: RuleInput) -> list[ValidationFinding]:
    collider = getattr(inp.spec, "collider", None)
    capsule = getattr(collider, "capsule", None)
    col_nodes = [n.name for n in inp.parsed.nodes if n.name.startswith("COL_")]
    problems: list[str] = []
    if capsule is None:
        problems.append("specification declares no capsule")
    else:
        r, h = float(capsule.radius_m), float(capsule.height_m)
        if not (math.isfinite(r) and math.isfinite(h)):
            problems.append("non-finite capsule values")
        elif r < 0.10:
            problems.append(f"radius_m {r} < 0.10")
        elif h <= 2 * r:
            problems.append(f"height_m {h} <= 2 x radius_m {2 * r}")
    if col_nodes:
        problems.append(f"GLB carries collider nodes {col_nodes}; a capsule has no mesh")
    actual = (
        f"radius_m={capsule.radius_m}, height_m={capsule.height_m}"
        if capsule is not None
        else "none"
    )
    return [
        inp.finding(
            "collider.capsule.shape",
            not problems,
            "radius_m >= 0.10, height_m > 2 x radius_m, finite; no COL_ mesh in the GLB",
            "; ".join(problems) if problems else actual,
            "Capsule is built from the contract on +Y; the GLB carries no collider mesh",
        )
    ]


def rule_capsule_fit(inp: RuleInput) -> list[ValidationFinding]:
    capsule = getattr(getattr(inp.spec, "collider", None), "capsule", None)
    _, _, size = inp.measured_bounds()
    tol = inp.tolerance
    if capsule is None:
        ok, actual = False, "no capsule"
    else:
        width, height, depth = size
        ok = (
            capsule.height_m <= height + tol
            and 2 * capsule.radius_m <= max(width, depth) + tol
            and capsule.height_m <= inp.spec.dimensions.height_m + tol
            and 2 * capsule.radius_m
            <= max(inp.spec.dimensions.width_m, inp.spec.dimensions.depth_m) + tol
        )
        actual = (
            f"height {capsule.height_m} vs measured {round(height, 6)}; "
            f"diameter {2 * capsule.radius_m} vs measured max(width, depth) "
            f"{round(max(width, depth), 6)}"
        )
    return [
        inp.finding(
            "collider.capsule.fit",
            ok,
            f"capsule fits the measured and specified bounds ± {tol}m",
            actual,
            "Capsule height and diameter must fit inside the asset envelope",
        )
    ]


# =============================================================================
# parts (ADR 0013)
# =============================================================================


def _part_node_name(part_id: str) -> str:
    return f"PART_{part_id}"


def _allowed_assembly_names(inp: RuleInput) -> set[str]:
    names = {"ROOT"}
    for part in inp.parts:
        names.add(_part_node_name(part.part_id))
        names.add(f"SM_{inp.asset_id}_{part.part_id}_LOD0")
        names.add(f"SM_{inp.asset_id}_{part.part_id}_LOD1")
    for socket in inp.sockets:
        names.add(f"SOCKET_{socket.socket_id}")
    if inp.contract.get("collider_policy") == "box":
        names.add(inp.collider_name)
    return names


def rule_assembly_nodes_unique(inp: RuleInput) -> list[ValidationFinding]:
    names = [node.name for node in inp.parsed.nodes]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    return [
        inp.finding(
            "nodes.unique",
            not duplicates and all(names),
            "unique, non-empty node names",
            str(duplicates) if duplicates else f"{len(names)} unique node(s)",
            "Duplicate or empty names are ambiguous during engine import",
        )
    ]


def rule_part_root(inp: RuleInput) -> list[ValidationFinding]:
    root = inp.parsed.node_named("ROOT")
    problems: list[str] = []
    if root is None:
        problems.append("ROOT node missing or duplicated")
    else:
        if inp.parsed.scene_roots != (root.index,):
            problems.append(f"scene roots {list(inp.parsed.scene_roots)} are not exactly ROOT")
        if not gm.matrices_close(root.local, gm.identity(), 1e-6):
            problems.append("ROOT transform is not identity")
        if root.mesh is not None:
            problems.append("ROOT carries a mesh")
    return [
        inp.finding(
            "part.root",
            not problems,
            "one identity ROOT node is the only scene root",
            "; ".join(problems) if problems else "ROOT identity",
            "Assemblies hang from one identity root in the canonical frame",
        )
    ]


def rule_part_missing(inp: RuleInput) -> list[ValidationFinding]:
    missing = [
        p.part_id for p in inp.parts if inp.parsed.node_named(_part_node_name(p.part_id)) is None
    ]
    return [
        inp.finding(
            "part.missing",
            not missing,
            f"PART_ nodes for {[p.part_id for p in inp.parts]}",
            f"missing {missing}" if missing else "all present",
            "Every declared part needs exactly one PART_<part_id> node",
        )
    ]


def rule_part_unexpected(inp: RuleInput) -> list[ValidationFinding]:
    allowed = _allowed_assembly_names(inp)
    unexpected = sorted(
        n.name
        for n in inp.parsed.nodes
        if n.name not in allowed and not n.name.startswith("SOCKET_")
    )
    return [
        inp.finding(
            "part.unexpected",
            not unexpected,
            "only ROOT, declared PART_/SM_/SOCKET_ nodes and the collider",
            f"unexpected {unexpected}" if unexpected else "none",
            "Undeclared nodes are rejected rather than guessed",
        )
    ]


def rule_part_parent(inp: RuleInput) -> list[ValidationFinding]:
    wrong: list[str] = []
    for part in inp.parts:
        node = inp.parsed.node_named(_part_node_name(part.part_id))
        if node is None:
            continue
        expected = "ROOT" if part.parent == "root" else _part_node_name(part.parent)
        actual = inp.parsed.nodes[node.parent].name if node.parent is not None else "<scene>"
        if actual != expected:
            wrong.append(f"{part.part_id}: parent {actual}, expected {expected}")
    return [
        inp.finding(
            "part.parent",
            not wrong,
            "each PART_ node sits under its declared parent",
            "; ".join(wrong) if wrong else "all parents match",
            "The part tree must equal the declared hierarchy",
        )
    ]


def rule_part_scale(inp: RuleInput) -> list[ValidationFinding]:
    wrong: list[str] = []
    for part in inp.parts:
        node = inp.parsed.node_named(_part_node_name(part.part_id))
        if node is None:
            continue
        sx, sy, sz = gm.scale_of(node.local)
        det = gm.determinant3(node.local)
        if det <= 1e-12 or max(sx, sy, sz) - min(sx, sy, sz) > 1e-6:
            wrong.append(f"{part.part_id}: scale ({sx:.6g}, {sy:.6g}, {sz:.6g}), det {det:.6g}")
    return [
        inp.finding(
            "part.scale",
            not wrong,
            "uniform positive part scale",
            "; ".join(wrong) if wrong else "uniform positive",
            "Non-uniform, zero or mirrored part scale is rejected",
        )
    ]


def rule_part_mesh(inp: RuleInput) -> list[ValidationFinding]:
    problems: list[str] = []
    for part in inp.parts:
        part_node = inp.parsed.node_named(_part_node_name(part.part_id))
        lods = [0, 1] if inp.lod1_required else [0]
        present_lod1 = inp.part_mesh(part.part_id, 1) is not None
        if present_lod1 and 1 not in lods:
            lods.append(1)
        for lod in lods:
            name = f"SM_{inp.asset_id}_{part.part_id}_LOD{lod}"
            mesh = inp.part_mesh(part.part_id, lod)
            if mesh is None:
                problems.append(f"{name} missing")
                continue
            node = inp.parsed.nodes[mesh.node_index]
            if part_node is None or node.parent != part_node.index:
                problems.append(f"{name} is not a direct child of PART_{part.part_id}")
            if not gm.matrices_close(node.local, gm.identity(), 1e-6):
                problems.append(f"{name} local transform is not identity")
            if node.children:
                problems.append(f"{name} has children")
            if mesh.triangle_count <= 0:
                problems.append(f"{name} is empty")
    return [
        inp.finding(
            "part.mesh",
            not problems,
            "SM_<asset>_<part>_LOD0 (and LOD1 when required) directly under each part, identity",
            "; ".join(problems) if problems else "all part meshes present",
            "Part geometry lives in the part's local frame",
        )
    ]


# =============================================================================
# orientation (ADR 0014)
# =============================================================================


def rule_orientation_source_front(inp: RuleInput) -> list[ValidationFinding]:
    record = inp.normalization
    problems: list[str] = []
    if record is None:
        problems.append("normalization record missing")
    else:
        try:
            applied, quat = normalization_for(record.source_front)
        except ValueError as exc:
            problems.append(str(exc))
            applied, quat = False, gm.IDENTITY_QUAT
        if not problems:
            if record.normalization_applied != applied:
                problems.append("normalization_applied does not match source_front")
            if gm.quat_angle_deg(record.quaternion_xyzw, quat) > 1e-6:
                problems.append("normalization quaternion is not the declared one")
            if record.resulting_front != "-Z":
                problems.append(f"resulting_front {record.resulting_front}, expected -Z")
        root = inp.parsed.node_named("ROOT")
        if root is None or not gm.matrices_close(root.world, gm.identity(), 1e-6):
            problems.append("processed ROOT is not identity")
        if record.source is None:
            problems.append("retained source GLB missing")
        elif not problems:
            problems.extend(_compare_to_source(inp.parsed, record.source, quat))
    return [
        inp.finding(
            "orientation.source_front",
            not problems,
            "result == source x at most one declared 180 deg +Y root rotation",
            "; ".join(problems[:4]) if problems else str(record.source_front if record else None),
            "No orientation is inferred or corrected beyond the declared source_front",
        )
    ]


def _compare_to_source(processed: ParsedGLB, source: ParsedGLB, quat: gm.Quat) -> list[str]:
    problems: list[str] = []
    rot = gm.trs_matrix(rotation=quat)
    src_names = sorted(n.name for n in source.nodes)
    out_names = sorted(n.name for n in processed.nodes)
    if src_names != out_names:
        return [f"node set differs from source: {sorted(set(src_names) ^ set(out_names))}"]
    src_root = source.node_named("ROOT")
    if src_root is None:
        return ["source has no ROOT"]
    for node in processed.nodes:
        src = source.node_named(node.name)
        if src is None:
            problems.append(f"{node.name} missing in source")
            continue
        src_parent = source.nodes[src.parent].name if src.parent is not None else None
        out_parent = processed.nodes[node.parent].name if node.parent is not None else None
        if src_parent != out_parent:
            problems.append(f"{node.name} parent changed")
            continue
        if node.name == "ROOT":
            continue
        if out_parent == "ROOT":
            # The one allowed change: the root rotation (and any source ROOT
            # transform) is baked into root-level children.
            expected = gm.mat_mul(rot, gm.mat_mul(src_root.local, src.local))
        else:
            expected = src.local
        if not gm.matrices_close(node.local, expected, 1e-5):
            problems.append(f"{node.name} transform is not source x declared rotation")
    for mesh in processed.meshes:
        src_mesh = next((m for m in source.meshes if m.name == mesh.name), None)
        if src_mesh is None or src_mesh.local_points != mesh.local_points:
            problems.append(f"{mesh.name} local geometry changed")
    return problems


# =============================================================================
# pivot (ADR 0013)
# =============================================================================


def _declared_pivot(part: Any) -> tuple[gm.Vec3, gm.Quat]:
    pos = tuple(float(v) for v in part.pivot.position_m)
    return (pos[0], pos[1], pos[2]), gm.spec_quat(part.pivot.basis)


def _part_nodes(inp: RuleInput) -> list[tuple[Any, NodeInfo]]:
    result = []
    for part in inp.parts:
        node = inp.parsed.node_named(_part_node_name(part.part_id))
        if node is not None:
            result.append((part, node))
    return result


def _pivot_tolerance(inp: RuleInput) -> float:
    return float(inp.assembly.pivot_tolerance_m) if inp.assembly else 0.01


def _basis_tolerance(inp: RuleInput) -> float:
    return float(inp.assembly.basis_tolerance_deg) if inp.assembly else 1.0


def rule_pivot_collapsed(inp: RuleInput) -> list[ValidationFinding]:
    tol = _pivot_tolerance(inp)
    collapsed = []
    for part, node in _part_nodes(inp):
        declared, _ = _declared_pivot(part)
        actual = gm.translation_of(node.local)
        if math.dist(declared, (0.0, 0.0, 0.0)) > tol and math.dist(actual, (0.0, 0.0, 0.0)) <= tol:
            collapsed.append(part.part_id)
    return [
        inp.finding(
            "pivot.collapsed",
            not collapsed,
            "no declared off-origin pivot sits at its parent origin",
            f"collapsed {collapsed}" if collapsed else "none",
            "A clean import can still collapse every pivot to the origin",
        )
    ]


def rule_pivot_position(inp: RuleInput) -> list[ValidationFinding]:
    tol = _pivot_tolerance(inp)
    wrong = []
    for part, node in _part_nodes(inp):
        declared, _ = _declared_pivot(part)
        actual = gm.translation_of(node.local)
        if math.dist(declared, actual) > tol:
            wrong.append(f"{part.part_id}: {[round(v, 6) for v in actual]} vs {list(declared)}")
    return [
        inp.finding(
            "pivot.position",
            not wrong,
            f"PART_ local translation == declared position_m ± {tol}m",
            "; ".join(wrong) if wrong else "all positions match",
            "Pivot origins are compared in the parent's frame",
        )
    ]


def rule_pivot_orientation(inp: RuleInput) -> list[ValidationFinding]:
    tol = _basis_tolerance(inp)
    wrong = []
    for part, node in _part_nodes(inp):
        _, declared = _declared_pivot(part)
        try:
            actual = gm.rotation_to_quat(gm.rotation_of(node.local))
        except ValueError as exc:
            wrong.append(f"{part.part_id}: {exc}")
            continue
        angle = gm.quat_angle_deg(actual, declared)
        if angle > tol:
            wrong.append(f"{part.part_id}: {angle:.3f} deg off")
    return [
        inp.finding(
            "pivot.orientation",
            not wrong,
            f"PART_ local basis == declared basis ± {tol} deg",
            "; ".join(wrong) if wrong else "all bases match",
            "The part basis is compared independently of its position",
        )
    ]


def rule_pivot_axis(inp: RuleInput) -> list[ValidationFinding]:
    wrong = []
    for part, node in _part_nodes(inp):
        motion = part.pivot.motion
        kind = node.extras.get("gf_motion")
        axis = node.extras.get("gf_axis")
        if kind != motion.kind:
            wrong.append(f"{part.part_id}: gf_motion {kind!r}, expected {motion.kind!r}")
            continue
        if motion.kind == "fixed":
            if axis is not None:
                wrong.append(f"{part.part_id}: fixed part carries gf_axis")
            continue
        declared = [float(v) for v in motion.axis]
        if (
            not isinstance(axis, list)
            or len(axis) != 3
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in axis)
            or max(abs(float(a) - d) for a, d in zip(axis, declared, strict=True)) > 1e-6
        ):
            wrong.append(f"{part.part_id}: gf_axis {axis!r}, expected {declared}")
    return [
        inp.finding(
            "pivot.axis",
            not wrong,
            "PART_ extras gf_motion/gf_axis == declared motion",
            "; ".join(wrong) if wrong else "all motions match",
            "The motion axis is checked separately from position and basis",
        )
    ]


# =============================================================================
# sockets (ADR 0013)
# =============================================================================


def _socket_node(inp: RuleInput, socket_id: str) -> NodeInfo | None:
    return inp.parsed.node_named(f"SOCKET_{socket_id}")


def _socket_tolerances(inp: RuleInput) -> tuple[float, float]:
    if inp.assembly is None:
        return 0.02, 5.0
    return float(inp.assembly.socket_position_tolerance_m), float(
        inp.assembly.socket_angle_tolerance_deg
    )


def rule_socket_missing(inp: RuleInput) -> list[ValidationFinding]:
    missing = [s.socket_id for s in inp.sockets if _socket_node(inp, s.socket_id) is None]
    return [
        inp.finding(
            "socket.missing",
            not missing,
            f"SOCKET_ nodes for {[s.socket_id for s in inp.sockets]}",
            f"missing {missing}" if missing else "all present",
            "Every declared socket needs exactly one SOCKET_<socket_id> node",
        )
    ]


def rule_socket_structure(inp: RuleInput) -> list[ValidationFinding]:
    declared = {f"SOCKET_{s.socket_id}" for s in inp.sockets}
    problems: list[str] = []
    for node in inp.parsed.nodes:
        if not node.name.startswith("SOCKET_"):
            continue
        if node.name not in declared:
            problems.append(f"{node.name} is not declared")
            continue
        if node.mesh is not None:
            problems.append(f"{node.name} carries a mesh")
        if node.children:
            problems.append(f"{node.name} has children")
        if any(abs(s - 1.0) > 1e-6 for s in gm.scale_of(node.local)):
            problems.append(f"{node.name} has scale")
    return [
        inp.finding(
            "socket.structure",
            not problems,
            "declared sockets only; no mesh, no children, identity scale",
            "; ".join(problems) if problems else "ok",
            "Sockets are empty attachment frames",
        )
    ]


def rule_socket_parent(inp: RuleInput) -> list[ValidationFinding]:
    wrong = []
    for socket in inp.sockets:
        node = _socket_node(inp, socket.socket_id)
        if node is None:
            continue
        actual = inp.parsed.nodes[node.parent].name if node.parent is not None else "<scene>"
        if actual != _part_node_name(socket.parent_part):
            wrong.append(f"{socket.socket_id}: under {actual}, expected PART_{socket.parent_part}")
    return [
        inp.finding(
            "socket.parent",
            not wrong,
            "each socket sits directly under its declared parent part",
            "; ".join(wrong) if wrong else "all parents match",
            "Sockets must follow their parent part rigidly",
        )
    ]


def rule_socket_position(inp: RuleInput) -> list[ValidationFinding]:
    tol, _ = _socket_tolerances(inp)
    wrong = []
    for socket in inp.sockets:
        node = _socket_node(inp, socket.socket_id)
        if node is None:
            continue
        declared = tuple(float(v) for v in socket.translation_m)
        actual = gm.translation_of(node.local)
        if math.dist(declared, actual) > tol:
            wrong.append(f"{socket.socket_id}: {[round(v, 6) for v in actual]} vs {list(declared)}")
    return [
        inp.finding(
            "socket.position",
            not wrong,
            f"socket local translation == declared translation_m ± {tol}m",
            "; ".join(wrong) if wrong else "all positions match",
            "Socket positions are compared in the parent part's frame",
        )
    ]


def rule_socket_orientation(inp: RuleInput) -> list[ValidationFinding]:
    _, angle_tol = _socket_tolerances(inp)
    required = {r.socket_id: r for r in (inp.assembly.required_sockets if inp.assembly else [])}
    wrong = []
    for socket in inp.sockets:
        node = _socket_node(inp, socket.socket_id)
        if node is None:
            continue
        try:
            actual = gm.rotation_to_quat(gm.rotation_of(node.local))
        except ValueError as exc:
            wrong.append(f"{socket.socket_id}: {exc}")
            continue
        angle = gm.quat_angle_deg(actual, gm.spec_quat(socket.rotation))
        if angle > angle_tol:
            wrong.append(f"{socket.socket_id}: {angle:.3f} deg from declared rotation")
        rest = required.get(socket.socket_id)
        if rest is not None and rest.rest_forward is not None:
            forward = gm.apply_rotation(gm.rotation_of(node.world), (0.0, 0.0, -1.0))
            off = gm.vector_angle_deg(forward, rest.rest_forward)
            if off > angle_tol:
                wrong.append(f"{socket.socket_id}: rest forward {off:.3f} deg from profile")
    return [
        inp.finding(
            "socket.orientation",
            not wrong,
            f"socket rotation == declared, forward (local -Z) within {angle_tol} deg",
            "; ".join(wrong) if wrong else "all orientations match",
            "Socket forward is local -Z (ADR 0013)",
        )
    ]


def rule_socket_placement(inp: RuleInput) -> list[ValidationFinding]:
    tol, _ = _socket_tolerances(inp)
    fractions = {
        r.socket_id: float(r.forward_end_fraction)
        for r in (inp.assembly.required_sockets if inp.assembly else [])
    }
    wrong = []
    for socket in inp.sockets:
        node = _socket_node(inp, socket.socket_id)
        mesh = inp.part_mesh(socket.parent_part, 0)
        if node is None or mesh is None or not mesh.local_points:
            continue
        forward = gm.apply_rotation(gm.rotation_of(node.local), (0.0, 0.0, -1.0))
        projections = [sum(p[i] * forward[i] for i in range(3)) for p in mesh.local_points]
        lo, hi = min(projections), max(projections)
        position = gm.translation_of(node.local)
        s = sum(position[i] * forward[i] for i in range(3))
        fraction = fractions.get(socket.socket_id, 0.2)
        if not (hi - fraction * (hi - lo) - tol <= s <= hi + tol):
            wrong.append(
                f"{socket.socket_id}: at {s:.4f} along forward, parent extent "
                f"[{lo:.4f}, {hi:.4f}], allowed last {fraction:g}"
            )
    return [
        inp.finding(
            "socket.placement",
            not wrong,
            "forward_end: socket lies at the forward end of its parent part",
            "; ".join(wrong) if wrong else "all placements match",
            "Placement is measured along the socket forward in the parent part's frame",
        )
    ]


# =============================================================================
# Registry and composition
# =============================================================================

RULE_GROUPS: dict[str, tuple[Rule, ...]] = {
    "core": (
        rule_glb_parse,
        rule_glb_hash,
        rule_mesh_nonempty,
        rule_lod0_budget,
        rule_lod1_budget,
        rule_materials_budget,
        rule_textures_dimension,
        rule_scale_bounds,
        rule_origin_policy,
        rule_material_references,
    ),
    "single_mesh": (
        rule_nodes_unique,
        rule_nodes_profile,
        rule_lod0_present,
        rule_lod1_present,
        rule_dimensions_snap,
        rule_lod1_bounds,
        rule_orientation_identity,
    ),
    "parts": (
        rule_assembly_nodes_unique,
        rule_part_root,
        rule_part_missing,
        rule_part_unexpected,
        rule_part_parent,
        rule_part_scale,
        rule_part_mesh,
    ),
    "orientation": (rule_orientation_source_front,),
    "pivot": (rule_pivot_collapsed, rule_pivot_position, rule_pivot_orientation, rule_pivot_axis),
    "sockets": (
        rule_socket_missing,
        rule_socket_structure,
        rule_socket_parent,
        rule_socket_position,
        rule_socket_orientation,
        rule_socket_placement,
    ),
    "collider_box": (rule_collider_geometry,),
    "collider_capsule": (rule_capsule_shape, rule_capsule_fit),
}

# The V0.4-V0.6 emission order for single-mesh box profiles. It interleaves the
# core, single_mesh and collider_box groups and is frozen by tests/golden.
LEGACY_COMPOSITION: tuple[Rule, ...] = (
    rule_glb_parse,
    rule_glb_hash,
    rule_nodes_unique,
    rule_nodes_profile,
    rule_mesh_nonempty,
    rule_lod0_present,
    rule_lod1_present,
    rule_collider_geometry,
    rule_lod0_budget,
    rule_lod1_budget,
    rule_materials_budget,
    rule_textures_dimension,
    rule_scale_bounds,
    rule_origin_policy,
    rule_dimensions_snap,
    rule_lod1_bounds,
    rule_orientation_identity,
    rule_material_references,
)
LEGACY_GROUPS = ("core", "single_mesh", "collider_box")


@dataclass(frozen=True)
class Composition:
    """Groups selected for one validation, and the ordered rules they expand to."""

    name: str
    groups: tuple[str, ...]
    rules: tuple[Rule, ...]


def select_composition(
    *,
    geometry_mode: str,
    collider_policy: str,
    sockets_declared: bool,
    requires_normalization: bool,
) -> Composition:
    """Derive the rule composition from typed capabilities only (ADR 0018)."""
    if geometry_mode == "single_mesh" and collider_policy == "box":
        return Composition("legacy_single_mesh_box", LEGACY_GROUPS, LEGACY_COMPOSITION)
    if collider_policy not in {"box", "capsule"}:
        raise ValueError(f"unsupported collider policy {collider_policy!r}")
    collider_group = "collider_box" if collider_policy == "box" else "collider_capsule"
    if geometry_mode == "single_mesh":
        groups: tuple[str, ...] = ("core", "single_mesh", collider_group)
    elif geometry_mode == "assembly":
        selected = ["core", "parts"]
        if requires_normalization:
            selected.append("orientation")
        selected.append("pivot")
        if sockets_declared:
            selected.append("sockets")
        selected.append(collider_group)
        groups = tuple(selected)
    else:
        raise ValueError(f"unsupported geometry mode {geometry_mode!r}")
    rules = tuple(rule for group in groups for rule in RULE_GROUPS[group])
    return Composition(f"{geometry_mode}_{collider_policy}", groups, rules)
