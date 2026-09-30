"""Immutable decoded input shared by asset validation rules."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from gamefactory.adapters.assets.glb_validator import _MeshInfo

if TYPE_CHECKING:
    from gamefactory.core.domain.asset_contracts import AssetSpecification
    from gamefactory.core.domain.asset_profiles import AssetProfile


@dataclass(frozen=True)
class DecodedValidationContext:
    """One bounded decode, reused by the selected validation rules."""

    path: Path
    artifact: str
    document: Mapping[str, Any]
    binary: bytes
    meshes: tuple[_MeshInfo, ...]
    points: tuple[tuple[float, float, float], ...]
    material_ids: frozenset[int]
    textures: tuple[tuple[int, int], ...]
    triangle_count: int
    world_matrices: Mapping[int, tuple[tuple[float, ...], ...]]
    asset_specification: AssetSpecification
    profile: AssetProfile
    processing_contract: Mapping[str, Any]


def build_validation_context(
    path: Path,
    document: dict[str, object],
    binary: bytes,
    decoded: tuple[
        list[_MeshInfo],
        list[tuple[float, float, float]],
        set[int],
        list[tuple[int, int]],
        int,
        dict[int, list[list[float]]],
    ],
    spec: AssetSpecification,
    profile: AssetProfile,
    contract: dict[str, Any],
) -> DecodedValidationContext:
    """Freeze the results of an already bounded, successful GLB decode."""
    infos, points, material_ids, textures, tris, world = decoded
    frozen_document = MappingProxyType(document)
    frozen_world = MappingProxyType(
        {index: tuple(tuple(row) for row in matrix) for index, matrix in world.items()}
    )
    return DecodedValidationContext(
        path=path,
        artifact=str(path),
        document=frozen_document,
        binary=binary,
        meshes=tuple(infos),
        points=tuple(points),
        material_ids=frozenset(material_ids),
        textures=tuple(textures),
        triangle_count=tris,
        world_matrices=frozen_world,
        asset_specification=spec,
        profile=profile,
        processing_contract=MappingProxyType(dict(contract)),
    )
