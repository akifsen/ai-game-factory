"""Geometry helpers for the V0.8-3A candidate (ADR 0021 capsule center)."""

from __future__ import annotations

from collections.abc import Sequence

from gamefactory.adapters.assets.internal_skin_decode import DecodedInternalSkinnedGLB


def visual_aabb_in_reference_root_frame(
    decoded: DecodedInternalSkinnedGLB,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Axis-aligned bounds of the validated visual mesh in the humanoid reference-root frame."""
    positions = decoded.primitive.positions
    if not positions:
        raise ValueError("visual mesh has no positions")
    mins = (
        min(p[0] for p in positions),
        min(p[1] for p in positions),
        min(p[2] for p in positions),
    )
    maxs = (
        max(p[0] for p in positions),
        max(p[1] for p in positions),
        max(p[2] for p in positions),
    )
    return mins, maxs


def capsule_center_from_aabb(
    mins: Sequence[float], maxs: Sequence[float]
) -> tuple[float, float, float]:
    """Midpoint of a validated visual AABB: c_i = (min_i + max_i) / 2 on each axis (ADR 0021)."""
    return (
        (float(mins[0]) + float(maxs[0])) / 2.0,
        (float(mins[1]) + float(maxs[1])) / 2.0,
        (float(mins[2]) + float(maxs[2])) / 2.0,
    )


def envelope_size_from_aabb(
    mins: Sequence[float], maxs: Sequence[float]
) -> tuple[float, float, float]:
    return (
        float(maxs[0]) - float(mins[0]),
        float(maxs[1]) - float(mins[1]),
        float(maxs[2]) - float(mins[2]),
    )
