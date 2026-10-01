"""Region membership for internal skin deformation oracle (V0.8-2)."""

from __future__ import annotations

import math
from typing import Any

REGION_BOUNDARY_TOLERANCE = 1e-6

RUNTIME_REQUEST_STRICT_INT_FIELDS = (
    "vertex_count",
    "affected_vertex_count",
    "unaffected_vertex_count",
)


def strict_runtime_int(value: Any, field: str) -> int:
    """Coerce a JSON value to int for digest binding (rejects bool and non-integral floats)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a strict integer")
    as_float = float(value)
    if not math.isfinite(as_float) or as_float != math.trunc(as_float):
        raise ValueError(f"{field} must be a strict integer")
    iv = int(as_float)
    if iv < 0:
        raise ValueError(f"{field} must be non-negative")
    return iv


def strict_region_boundary_tolerance(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("region_boundary_tolerance must be numeric")
    tol = float(value)
    if not math.isfinite(tol):
        raise ValueError("region_boundary_tolerance must be finite")
    if not math.isclose(tol, REGION_BOUNDARY_TOLERANCE, rel_tol=0.0, abs_tol=0.0):
        raise ValueError("region_boundary_tolerance must match pinned REGION_BOUNDARY_TOLERANCE")
    return float(f"{REGION_BOUNDARY_TOLERANCE:.6f}")


def vertex_inside_region_box(
    p: tuple[float, float, float],
    box: dict[str, Any],
    *,
    boundary_tolerance: float = REGION_BOUNDARY_TOLERANCE,
) -> bool:
    """Inclusive region test with epsilon only on min/max comparisons (not displacement limits)."""
    mn, mx = box["min"], box["max"]
    eps = boundary_tolerance
    return (
        (p[0] - float(mx[0])) <= eps
        and (float(mn[0]) - p[0]) <= eps
        and (p[1] - float(mx[1])) <= eps
        and (float(mn[1]) - p[1]) <= eps
        and (p[2] - float(mx[2])) <= eps
        and (float(mn[2]) - p[2]) <= eps
    )


def region_population_counts(
    positions: list[Any] | tuple[Any, ...],
    affected: dict[str, Any],
    unaffected: dict[str, Any],
    *,
    boundary_tolerance: float = REGION_BOUNDARY_TOLERANCE,
) -> tuple[int, int]:
    aff = 0
    unaff = 0
    for pos in positions:
        rp = (float(pos[0]), float(pos[1]), float(pos[2]))
        if vertex_inside_region_box(rp, affected, boundary_tolerance=boundary_tolerance):
            aff += 1
        if vertex_inside_region_box(rp, unaffected, boundary_tolerance=boundary_tolerance):
            unaff += 1
    return aff, unaff
