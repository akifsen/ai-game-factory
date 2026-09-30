"""Deterministic generator for V0.7 assembly and character fixtures.

A fixture is described once (:class:`AssemblyDesign`) and every artifact is
derived from that description: the asset-spec-0.7.0 document, the
``local_operator_assembly`` source registration and the GLB bytes. The root
part pivot is chosen so the rest-pose bounds satisfy the origin policy, and the
specification dimensions are measured from the same geometry.

No textures are embedded, so the GLB bytes are identical on every platform.
Nothing here contacts a provider or reads external files.
"""

from __future__ import annotations

import copy
import hashlib
import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from gamefactory.adapters.assets.glb_io import read_glb, write_glb
from gamefactory.core.domain import transforms as tf

Vec = tuple[float, float, float]


@dataclass(frozen=True)
class BoxPart:
    part_id: str
    role: str
    parent: str
    position: Vec
    box_min: Vec
    box_max: Vec
    basis: tuple[float, float, float, float] | None = None  # None means identity
    motion: str = "fixed"
    axis: Vec | None = None


@dataclass(frozen=True)
class SocketDesign:
    socket_id: str
    parent_part: str
    translation: Vec
    rotation: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class AssemblyDesign:
    asset_id: str
    profile: str
    category: str
    intent: str
    parts: tuple[BoxPart, ...]
    sockets: tuple[SocketDesign, ...] = ()
    origin_policy: str = "bottom_center"
    lod_policy: str = "lod0_only"
    collider: str = "box"
    authoring_tool: tuple[str, str] = ("gamefactory-fixture", "0.7.0")


@dataclass(frozen=True)
class CharacterDesign:
    asset_id: str
    width_m: float
    height_m: float
    depth_m: float
    capsule_radius_m: float
    capsule_height_m: float
    lod_policy: str = "lod0_only"
    intent: str = "Humanoid character test fixture with a capsule collider"


# --- designs -------------------------------------------------------------------

VEHICLE_TANK = AssemblyDesign(
    asset_id="vehicle_tank_test",
    profile="vehicle",
    category="vehicle",
    intent="Tracked vehicle test assembly: hull, yaw turret, pitch barrel and muzzle",
    parts=(
        BoxPart("hull", "hull", "root", (0.0, 0.0, 0.0), (-1.2, 0.0, -1.9), (1.2, 0.8, 1.9)),
        BoxPart(
            "turret",
            "turret",
            "hull",
            (0.0, 0.8, 0.2),
            (-0.7, 0.0, -0.7),
            (0.7, 0.5, 0.7),
            motion="revolute",
            axis=(0.0, 1.0, 0.0),
        ),
        BoxPart(
            "barrel",
            "barrel",
            "turret",
            (0.0, 0.25, -0.6),
            (-0.08, -0.08, -1.5),
            (0.08, 0.08, 0.0),
            motion="revolute",
            axis=(1.0, 0.0, 0.0),
        ),
    ),
    sockets=(SocketDesign("muzzle", "barrel", (0.0, 0.0, -1.5)),),
)

WEAPON_RIFLE = AssemblyDesign(
    asset_id="weapon_rifle_test",
    profile="weapon",
    category="weapon",
    intent="Rifle test assembly: receiver, barrel with muzzle, magazine and sliding bolt",
    origin_policy="center",
    parts=(
        BoxPart(
            "receiver", "receiver", "root", (0.0, 0.0, 0.0), (-0.03, -0.06, -0.3), (0.03, 0.06, 0.4)
        ),
        BoxPart(
            "barrel",
            "barrel",
            "receiver",
            (0.0, 0.02, -0.3),
            (-0.012, -0.012, -0.5),
            (0.012, 0.012, 0.0),
        ),
        BoxPart(
            "magazine",
            "magazine",
            "receiver",
            (0.0, -0.06, -0.05),
            (-0.02, -0.18, -0.04),
            (0.02, 0.0, 0.04),
        ),
        BoxPart(
            "slide",
            "slide",
            "receiver",
            (0.0, 0.06, -0.1),
            (-0.025, 0.0, -0.15),
            (0.025, 0.03, 0.15),
            motion="prismatic",
            axis=(0.0, 0.0, 1.0),
        ),
    ),
    sockets=(SocketDesign("muzzle", "barrel", (0.0, 0.0, -0.5)),),
)

AIRCRAFT_TRAINER = AssemblyDesign(
    asset_id="aircraft_trainer_test",
    profile="aircraft",
    category="aircraft",
    intent="Light aircraft test assembly: fuselage, propeller, rudder and elevator",
    parts=(
        BoxPart(
            "fuselage", "fuselage", "root", (0.0, 0.0, 0.0), (-0.4, -0.4, -3.0), (0.4, 0.4, 3.0)
        ),
        BoxPart(
            "propeller",
            "propeller",
            "fuselage",
            (0.0, 0.0, -3.0),
            (-1.0, -0.1, -0.1),
            (1.0, 0.1, 0.0),
            motion="revolute",
            axis=(0.0, 0.0, 1.0),
        ),
        BoxPart(
            "rudder",
            "rudder",
            "fuselage",
            (0.0, 0.4, 2.6),
            (-0.02, 0.0, 0.0),
            (0.02, 1.0, 0.4),
            motion="revolute",
            axis=(0.0, 1.0, 0.0),
        ),
        BoxPart(
            "elevator",
            "elevator",
            "fuselage",
            (0.0, 0.0, 2.6),
            (-1.5, -0.02, 0.0),
            (1.5, 0.02, 0.4),
            motion="revolute",
            axis=(1.0, 0.0, 0.0),
        ),
    ),
)

HUMANOID_CHARACTER = CharacterDesign(
    asset_id="humanoid_character_test",
    width_m=0.6,
    height_m=1.8,
    depth_m=0.35,
    capsule_radius_m=0.28,
    capsule_height_m=1.8,
)

ASSEMBLY_DESIGNS = {d.asset_id: d for d in (VEHICLE_TANK, WEAPON_RIFLE, AIRCRAFT_TRAINER)}


# --- geometry -------------------------------------------------------------------


def _round(v: float) -> float:
    r = round(float(v), 6)
    return 0.0 if r == 0 else r


def _box_corners(box_min: Vec, box_max: Vec) -> list[Vec]:
    return [
        (box_min[0], box_min[1], box_min[2]),
        (box_max[0], box_min[1], box_min[2]),
        (box_max[0], box_min[1], box_max[2]),
        (box_min[0], box_min[1], box_max[2]),
        (box_min[0], box_max[1], box_min[2]),
        (box_max[0], box_max[1], box_min[2]),
        (box_max[0], box_max[1], box_max[2]),
        (box_min[0], box_max[1], box_max[2]),
    ]


# Outward-facing triangles for the corner order above.
_BOX_INDICES = (
    0, 1, 2, 0, 2, 3,  # bottom
    4, 6, 5, 4, 7, 6,  # top
    0, 4, 5, 0, 5, 1,  # front (-Z)
    3, 2, 6, 3, 6, 7,  # back (+Z)
    0, 3, 7, 0, 7, 4,  # left (-X)
    1, 5, 6, 1, 6, 2,  # right (+X)
)  # fmt: skip


def _part_local(part: BoxPart) -> tf.Matrix:
    return tf.trs_matrix(part.position, part.basis or tf.IDENTITY_QUAT)


def part_world_matrices(parts: Sequence[BoxPart]) -> dict[str, tf.Matrix]:
    by_id = {p.part_id: p for p in parts}
    world: dict[str, tf.Matrix] = {}

    def resolve(pid: str) -> tf.Matrix:
        if pid not in world:
            part = by_id[pid]
            parent = tf.identity() if part.parent == "root" else resolve(part.parent)
            world[pid] = tf.mat_mul(parent, _part_local(part))
        return world[pid]

    for part in parts:
        resolve(part.part_id)
    return world


def _transform(m: tf.Matrix, p: Sequence[float]) -> Vec:
    return (
        m[0][0] * p[0] + m[0][1] * p[1] + m[0][2] * p[2] + m[0][3],
        m[1][0] * p[0] + m[1][1] * p[1] + m[1][2] * p[2] + m[1][3],
        m[2][0] * p[0] + m[2][1] * p[1] + m[2][2] * p[2] + m[2][3],
    )


def rest_bounds(parts: Sequence[BoxPart]) -> tuple[Vec, Vec]:
    world = part_world_matrices(parts)
    points = [
        _transform(world[p.part_id], corner)
        for p in parts
        for corner in _box_corners(p.box_min, p.box_max)
    ]
    mins = tuple(min(pt[i] for pt in points) for i in range(3))
    maxs = tuple(max(pt[i] for pt in points) for i in range(3))
    return (mins[0], mins[1], mins[2]), (maxs[0], maxs[1], maxs[2])


def centered_design(design: AssemblyDesign) -> AssemblyDesign:
    """Move the root part pivot so the rest pose satisfies the origin policy."""
    mins, maxs = rest_bounds(design.parts)
    root = next(p for p in design.parts if p.parent == "root")
    dx = -(mins[0] + maxs[0]) / 2
    dz = -(mins[2] + maxs[2]) / 2
    dy = -mins[1] if design.origin_policy == "bottom_center" else -(mins[1] + maxs[1]) / 2
    moved = replace(
        root,
        position=(
            _round(root.position[0] + dx),
            _round(root.position[1] + dy),
            _round(root.position[2] + dz),
        ),
    )
    return replace(design, parts=tuple(moved if p is root else p for p in design.parts))


# --- documents -------------------------------------------------------------------


def _quat_or_identity(q: Sequence[float] | None) -> Any:
    return "identity" if q is None else [float(v) for v in q]


def assembly_spec(design: AssemblyDesign) -> dict[str, Any]:
    design = centered_design(design)
    mins, maxs = rest_bounds(design.parts)
    parts = []
    for part in design.parts:
        motion: dict[str, Any] = {"kind": part.motion}
        if part.motion != "fixed":
            motion["axis"] = [float(v) for v in part.axis or ()]
        parts.append(
            {
                "part_id": part.part_id,
                "role": part.role,
                "parent": part.parent,
                "pivot": {
                    "position_m": [float(v) for v in part.position],
                    "basis": _quat_or_identity(part.basis),
                    "motion": motion,
                },
            }
        )
    spec: dict[str, Any] = {
        "schema_version": "0.7.0",
        "asset_id": design.asset_id,
        "category": design.category,
        "profile": design.profile,
        "profile_version": 1,
        "intent": design.intent,
        "source_kind": "local_operator_assembly",
        "dimensions": {
            "width_m": _round(maxs[0] - mins[0]),
            "height_m": _round(maxs[1] - mins[1]),
            "depth_m": _round(maxs[2] - mins[2]),
        },
        "origin_policy": design.origin_policy,
        "lod_policy": design.lod_policy,
        "geometry_budget": {"max_triangles_lod0": 5000, "max_triangles_lod1": 2500},
        "material_budget": {"max_materials": 2},
        "texture_budget": {"max_dimension": 1024},
        "collider": {"policy": design.collider, "capsule": None},
        "parts": parts,
    }
    if design.sockets:
        spec["sockets"] = [
            {
                "socket_id": s.socket_id,
                "parent_part": s.parent_part,
                "translation_m": [float(v) for v in s.translation],
                "rotation": _quat_or_identity(s.rotation),
                "placement": "forward_end",
            }
            for s in design.sockets
        ]
    return spec


def character_spec(design: CharacterDesign) -> dict[str, Any]:
    return {
        "schema_version": "0.7.0",
        "asset_id": design.asset_id,
        "category": "character",
        "profile": "character",
        "profile_version": 1,
        "intent": design.intent,
        "source_kind": "provider_generated",
        "dimensions": {
            "width_m": design.width_m,
            "height_m": design.height_m,
            "depth_m": design.depth_m,
        },
        "origin_policy": "bottom_center",
        "lod_policy": design.lod_policy,
        "geometry_budget": {"max_triangles_lod0": 5000, "max_triangles_lod1": 2500},
        "material_budget": {"max_materials": 2},
        "texture_budget": {"max_dimension": 1024},
        "collider": {
            "policy": "capsule",
            "capsule": {"radius_m": design.capsule_radius_m, "height_m": design.capsule_height_m},
        },
    }


def source_registration(
    design: AssemblyDesign,
    glb: bytes,
    *,
    source_front: str | None = "-Z",
    actor: str = "fixture-operator",
    reason: str = "Deterministic V0.7 fixture assembly",
) -> dict[str, Any]:
    registration: dict[str, Any] = {
        "schema_version": "asset-source-registration-0.7.0",
        "source_provenance_type": "local_operator_assembly",
        "paid": False,
        "artifact_sha256": hashlib.sha256(glb).hexdigest(),
        "artifact_bytes": len(glb),
        "authoring_tool": {"name": design.authoring_tool[0], "version": design.authoring_tool[1]},
        "part_map": [
            {"part_id": p.part_id, "role": p.role, "parent": p.parent} for p in design.parts
        ],
        "socket_map": [
            {"socket_id": s.socket_id, "parent_part": s.parent_part} for s in design.sockets
        ],
        "actor": actor,
        "reason": reason,
    }
    if source_front is not None:
        registration["source_front"] = source_front
    return registration


# --- GLB writing -------------------------------------------------------------------


class _GLBBuilder:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.views: list[dict[str, Any]] = []
        self.accessors: list[dict[str, Any]] = []
        self.meshes: list[dict[str, Any]] = []

    def _view(self, data: bytes, target: int) -> int:
        while len(self.buffer) % 4:
            self.buffer.append(0)
        self.views.append(
            {"buffer": 0, "byteOffset": len(self.buffer), "byteLength": len(data), "target": target}
        )
        self.buffer.extend(data)
        return len(self.views) - 1

    def box_mesh(self, name: str, box_min: Vec, box_max: Vec) -> int:
        corners = _box_corners(box_min, box_max)
        pos = struct.pack("<" + "f" * 24, *[c for corner in corners for c in corner])
        idx = struct.pack("<" + "H" * 36, *_BOX_INDICES)
        pos_view = self._view(pos, 34962)
        idx_view = self._view(idx, 34963)
        f32 = [struct.unpack("<f", struct.pack("<f", v))[0] for v in (*box_min, *box_max)]
        self.accessors.append(
            {
                "bufferView": pos_view,
                "componentType": 5126,
                "count": 8,
                "type": "VEC3",
                "min": f32[:3],
                "max": f32[3:],
            }
        )
        pos_acc = len(self.accessors) - 1
        self.accessors.append(
            {"bufferView": idx_view, "componentType": 5123, "count": 36, "type": "SCALAR"}
        )
        idx_acc = len(self.accessors) - 1
        self.meshes.append(
            {
                "name": name,
                "primitives": [
                    {"attributes": {"POSITION": pos_acc}, "indices": idx_acc, "material": 0}
                ],
            }
        )
        return len(self.meshes) - 1


def _node_trs(
    position: Sequence[float],
    rotation: Sequence[float] | None,
    scale: Sequence[float] | None = None,
) -> dict[str, Any]:
    node: dict[str, Any] = {"translation": [float(v) for v in position]}
    node["rotation"] = [float(v) for v in (rotation or tf.IDENTITY_QUAT)]
    node["scale"] = [float(v) for v in (scale or (1.0, 1.0, 1.0))]
    return node


def assembly_glb(
    design: AssemblyDesign,
    *,
    source_front: str = "-Z",
    collapsed: bool = False,
    include_lod1: bool | None = None,
) -> bytes:
    """Build the operator-authored assembly GLB.

    ``source_front="+Z"`` authors the same assembly facing +Z (the root-level
    parts carry the 180 degree turn), as Blender's glTF exporter produces.
    ``collapsed=True`` reproduces the V0.7 spike defect: every PART_ node is
    identity and the geometry is baked into asset space.
    """
    design = centered_design(design)
    lod1 = design.lod_policy == "lod0_lod1" if include_lod1 is None else include_lod1
    builder = _GLBBuilder()
    world = part_world_matrices(design.parts)
    nodes: list[dict[str, Any]] = [{"name": "ROOT", "children": []}]
    part_index: dict[str, int] = {}
    flip = tf.trs_matrix(rotation=tf.SOURCE_FRONT_PLUS_Z_QUAT)

    for part in design.parts:
        if collapsed:
            node: dict[str, Any] = {"name": f"PART_{part.part_id}"}
        else:
            local = _part_local(part)
            if part.parent == "root" and source_front == "+Z":
                local = tf.mat_mul(flip, local)
            quat = tf.rotation_to_quat(tf.rotation_of(local))
            node = {
                "name": f"PART_{part.part_id}",
                **_node_trs(
                    [_round(v) for v in tf.translation_of(local)], [_round(v) for v in quat]
                ),
            }
        extras: dict[str, Any] = {"gf_motion": part.motion}
        if part.motion != "fixed":
            extras["gf_axis"] = [float(v) for v in part.axis or ()]
        node["extras"] = extras
        node["children"] = []
        nodes.append(node)
        part_index[part.part_id] = len(nodes) - 1

    for part in design.parts:
        parent = 0 if part.parent == "root" else part_index[part.parent]
        nodes[parent]["children"].append(part_index[part.part_id])
        box_min, box_max = part.box_min, part.box_max
        if collapsed:
            corners = [_transform(world[part.part_id], c) for c in _box_corners(box_min, box_max)]
            box_min = tuple(min(c[i] for c in corners) for i in range(3))  # type: ignore[assignment]
            box_max = tuple(max(c[i] for c in corners) for i in range(3))  # type: ignore[assignment]
        for lod in (0, 1) if lod1 else (0,):
            name = f"SM_{design.asset_id}_{part.part_id}_LOD{lod}"
            mesh = builder.box_mesh(name, box_min, box_max)
            nodes.append({"name": name, "mesh": mesh})
            nodes[part_index[part.part_id]]["children"].append(len(nodes) - 1)

    for socket in design.sockets:
        position: Sequence[float] = socket.translation
        if collapsed:
            position = _transform(world[socket.parent_part], socket.translation)
        nodes.append(
            {
                "name": f"SOCKET_{socket.socket_id}",
                **_node_trs([_round(v) for v in position], socket.rotation),
            }
        )
        nodes[part_index[socket.parent_part]]["children"].append(len(nodes) - 1)

    if design.collider == "box":
        mins, maxs = rest_bounds(design.parts)
        if source_front == "+Z":
            mins, maxs = (-maxs[0], mins[1], -maxs[2]), (-mins[0], maxs[1], -mins[2])
        mesh = builder.box_mesh(f"COL_{design.asset_id}", mins, maxs)
        nodes.append({"name": f"COL_{design.asset_id}", "mesh": mesh})
        nodes[0]["children"].append(len(nodes) - 1)

    for node in nodes:
        if not node.get("children"):
            node.pop("children", None)
    document = {
        "asset": {"version": "2.0", "generator": "gamefactory assembly fixture 0.7.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": nodes,
        "meshes": builder.meshes,
        "materials": [
            {
                "name": "M_fixture",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.42, 0.47, 0.4, 1.0],
                    "metallicFactor": 0.1,
                    "roughnessFactor": 0.8,
                },
            }
        ],
        "accessors": builder.accessors,
        "bufferViews": builder.views,
        "buffers": [{"byteLength": len(builder.buffer)}],
    }
    return write_glb(document, bytes(builder.buffer))


def character_glb(design: CharacterDesign, *, include_lod1: bool | None = None) -> bytes:
    """Single-mesh character stand-in: LOD meshes only, no collider mesh."""
    lod1 = design.lod_policy == "lod0_lod1" if include_lod1 is None else include_lod1
    builder = _GLBBuilder()
    hw, hd = design.width_m / 2, design.depth_m / 2
    nodes: list[dict[str, Any]] = []
    for lod in (0, 1) if lod1 else (0,):
        name = f"SM_{design.asset_id}_LOD{lod}"
        mesh = builder.box_mesh(name, (-hw, 0.0, -hd), (hw, design.height_m, hd))
        nodes.append({"name": name, "mesh": mesh})
    document = {
        "asset": {"version": "2.0", "generator": "gamefactory character fixture 0.7.0"},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": builder.meshes,
        "materials": [
            {"name": "M_fixture", "pbrMetallicRoughness": {"baseColorFactor": [0.6, 0.5, 0.4, 1]}}
        ],
        "accessors": builder.accessors,
        "bufferViews": builder.views,
        "buffers": [{"byteLength": len(builder.buffer)}],
    }
    return write_glb(document, bytes(builder.buffer))


# --- mutation helpers for negative fixtures --------------------------------------


@dataclass
class GLBEdit:
    """Edit a fixture GLB's JSON by node name, keeping the BIN chunk."""

    document: dict[str, Any]
    binary: bytes
    changes: list[str] = field(default_factory=list)

    @classmethod
    def of(cls, glb: bytes) -> GLBEdit:
        document, binary = read_glb(glb)
        return cls(copy.deepcopy(document), binary)

    def node(self, name: str) -> dict[str, Any]:
        for node in self.document["nodes"]:
            if node.get("name") == name:
                return node  # type: ignore[no-any-return]
        raise KeyError(name)

    def index(self, name: str) -> int:
        for i, node in enumerate(self.document["nodes"]):
            if node.get("name") == name:
                return i
        raise KeyError(name)

    def reparent(self, name: str, new_parent: str) -> GLBEdit:
        child = self.index(name)
        for node in self.document["nodes"]:
            if child in node.get("children", []):
                node["children"].remove(child)
                if not node["children"]:
                    node.pop("children")
        self.node(new_parent).setdefault("children", []).append(child)
        self.changes.append(f"reparent {name} -> {new_parent}")
        return self

    def set(self, node_name: str, /, **values: Any) -> GLBEdit:
        self.node(node_name).update(values)
        self.changes.append(f"set {node_name} {sorted(values)}")
        return self

    def build(self) -> bytes:
        return write_glb(self.document, self.binary)


def rotate_about_y(degrees: float) -> list[float]:
    half = math.radians(degrees) / 2
    return [0.0, math.sin(half), 0.0, math.cos(half)]
