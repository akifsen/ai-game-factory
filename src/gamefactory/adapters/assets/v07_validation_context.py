"""Frozen, single-decode input context for the injected V0.7 validator."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from gamefactory.adapters.assets.glb_validator import (
    _inspect,
    _InvalidGLB,
    _MeshInfo,
    _read_glb_bytes,
)

if TYPE_CHECKING:
    from gamefactory.adapters.assets.v07_geometry_validation import VerifiedSourceNormalization
    from gamefactory.core.domain.asset_contracts import AssetSpecificationV07
    from gamefactory.core.domain.asset_profiles import AssetProfileV07


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """Return ordinary parser-compatible containers from the immutable snapshot."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class V07ValidationContext:
    """One bounded decode and immutable facts shared by every selected rule group."""

    path: Path
    artifact: str
    artifact_sha256: str
    document: Mapping[str, Any] | None
    binary: bytes
    meshes: tuple[_MeshInfo, ...]
    points: tuple[tuple[float, float, float], ...]
    material_ids: frozenset[int]
    textures: tuple[tuple[int, int], ...]
    triangle_count: int
    world_matrices: Mapping[int, tuple[tuple[float, ...], ...]]
    node_by_name: Mapping[str, tuple[int, Mapping[str, Any]]]
    parent_by_index: Mapping[int, int]
    active_scene_roots: tuple[int, ...]
    reachable_nodes: frozenset[int]
    specification: AssetSpecificationV07
    profile: AssetProfileV07
    source_observation: VerifiedSourceNormalization | None
    parse_error: str | None = None


def build_v07_validation_context(
    path: Path,
    specification: AssetSpecificationV07,
    profile: AssetProfileV07,
    source_observation: VerifiedSourceNormalization | None,
    max_file_size_bytes: int,
) -> V07ValidationContext:
    """Decode once under the shared GLB bounds and freeze all reusable facts."""
    try:
        if not path.is_file():
            raise ValueError("artifact does not exist")
        if path.suffix.casefold() != ".glb":
            raise _InvalidGLB("artifact extension must be .glb")
        if max_file_size_bytes < 1:
            raise ValueError("maximum GLB size must be positive")
        with path.open("rb") as stream:
            raw = stream.read(max_file_size_bytes + 1)
        if len(raw) > max_file_size_bytes:
            raise _InvalidGLB("artifact exceeds maximum file size")
        digest = hashlib.sha256(raw).hexdigest()
        document, binary = _read_glb_bytes(raw, max_file_size_bytes)
        meshes, points, materials, textures, triangles, world = _inspect(document, binary)
        frozen_document = _freeze(document)
        frozen_nodes = frozen_document["nodes"]
        node_by_name = {
            str(node.get("name", "")): (index, node) for index, node in enumerate(frozen_nodes)
        }
        parents = {
            child: parent
            for parent, node in enumerate(frozen_nodes)
            for child in node.get("children", ())
        }
        scenes = document["scenes"]
        scene_index = document.get("scene", 0)
        active_roots = tuple(scenes[scene_index].get("nodes", ()))
        reachable: set[int] = set()
        pending = list(active_roots)
        while pending:
            index = pending.pop()
            if index in reachable:
                continue
            reachable.add(index)
            pending.extend(frozen_nodes[index].get("children", ()))
        return V07ValidationContext(
            path,
            str(path),
            digest,
            frozen_document,
            binary,
            tuple(meshes),
            tuple(points),
            frozenset(materials),
            tuple(textures),
            triangles,
            MappingProxyType(
                {index: tuple(tuple(row) for row in matrix) for index, matrix in world.items()}
            ),
            MappingProxyType(node_by_name),
            MappingProxyType(parents),
            active_roots,
            frozenset(reachable),
            copy.deepcopy(specification),
            copy.deepcopy(profile),
            copy.deepcopy(source_observation),
        )
    except (OSError, _InvalidGLB, KeyError, TypeError, ValueError, IndexError) as exc:
        return V07ValidationContext(
            path,
            str(path),
            "",
            None,
            b"",
            (),
            (),
            frozenset(),
            (),
            0,
            MappingProxyType({}),
            MappingProxyType({}),
            MappingProxyType({}),
            (),
            frozenset(),
            copy.deepcopy(specification),
            copy.deepcopy(profile),
            copy.deepcopy(source_observation),
            str(exc),
        )
