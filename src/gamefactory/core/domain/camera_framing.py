"""Bounds-driven camera placement for asset review captures.

The helper consumes an axis-aligned bounding box and a view id. It does not
know asset identifiers. Godot applies the same distance rule at runtime using
the framing policy carried in the capture request.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

IMPLEMENTED_VIEWS = frozenset(
    {
        "front",
        "rear",
        "left",
        "right",
        "side",
        "three_quarter",
        "three_quarter_front",
        "three_quarter_rear",
        "top",
    }
)
# Views with a placed camera in V0.5. The others are named so a profile cannot
# request them silently; profile loading rejects any view outside this set.
PLACED_VIEWS = frozenset({"front", "three_quarter", "three_quarter_front", "side", "top"})

_THREE_QUARTER = (1.0, 0.65, -1.0)


def _normalize(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(sum(component * component for component in vector))
    if length <= 1e-9 or not math.isfinite(length):
        raise ValueError("camera direction is degenerate")
    x, y, z = (component / length for component in vector)
    return (x, y, z)


def view_direction(view: str) -> tuple[float, float, float]:
    """Unit vector from the asset center toward the camera."""
    if view not in PLACED_VIEWS:
        raise ValueError(f"view '{view}' has no V0.5 camera placement")
    table = {
        "front": (0.0, 0.0, -1.0),
        "three_quarter": _THREE_QUARTER,
        "three_quarter_front": _THREE_QUARTER,
        "side": (1.0, 0.0, 0.0),
        "top": (0.0, 1.0, 0.0),
    }
    return _normalize(table[view])


def view_up(view: str) -> tuple[float, float, float]:
    if view == "top":
        return (0.0, 0.0, -1.0)
    return (0.0, 1.0, 0.0)


def view_axis_label(view: str) -> str:
    return {
        "front": "-Z",
        "three_quarter": "+X-Z",
        "three_quarter_front": "+X-Z",
        "side": "+X",
        "top": "+Y",
    }[view]


@dataclass(frozen=True)
class BoundsAABB:
    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float

    @property
    def size(self) -> tuple[float, float, float]:
        return (self.max_x - self.min_x, self.max_y - self.min_y, self.max_z - self.min_z)

    @property
    def center(self) -> tuple[float, float, float]:
        return (
            (self.min_x + self.max_x) / 2.0,
            (self.min_y + self.max_y) / 2.0,
            (self.min_z + self.max_z) / 2.0,
        )


@dataclass(frozen=True)
class CameraFrame:
    position: tuple[float, float, float]
    target: tuple[float, float, float]
    up: tuple[float, float, float]
    near: float
    far: float
    fov_degrees: float
    view: str


def _silhouette(bounds: BoundsAABB, view: str) -> tuple[float, float]:
    """Return (vertical, horizontal) world extents facing the camera."""
    width, height, depth = bounds.size
    if view == "top":
        return depth, width
    if view == "side":
        return height, depth
    if view == "front":
        return height, width
    dominant = max(width, height, depth)
    return dominant, dominant


def frame_camera(
    bounds: BoundsAABB,
    view: str,
    *,
    fov_degrees: float,
    target_screen_fraction: float,
    viewport: tuple[int, int],
) -> CameraFrame:
    """Place a camera so the asset fills about ``target_screen_fraction`` of the frame."""
    if viewport[0] <= 0 or viewport[1] <= 0:
        raise ValueError("viewport size must be positive")
    if not 0.05 <= target_screen_fraction <= 0.95:
        raise ValueError("target screen fraction is outside 0.05-0.95")
    if not 1.0 <= fov_degrees <= 120.0:
        raise ValueError("fov is outside 1-120 degrees")
    vertical, horizontal = _silhouette(bounds, view)
    if min(vertical, horizontal) <= 1e-6:
        raise ValueError("asset bounds are empty")
    half_fov = math.radians(fov_degrees * 0.5)
    aspect = viewport[0] / viewport[1]
    tangent = math.tan(half_fov)
    distance_vertical = vertical / (2.0 * tangent * target_screen_fraction)
    distance_horizontal = horizontal / (2.0 * tangent * target_screen_fraction * aspect)
    distance = max(distance_vertical, distance_horizontal)
    direction = view_direction(view)
    center = bounds.center
    position = (
        center[0] + direction[0] * distance,
        center[1] + direction[1] * distance,
        center[2] + direction[2] * distance,
    )
    return CameraFrame(
        position=position,
        target=center,
        up=view_up(view),
        near=max(distance * 0.01, 0.01),
        far=max(distance * 20.0, 50.0),
        fov_degrees=fov_degrees,
        view=view,
    )


def reference_offset(
    bounds: BoundsAABB, view: str, reference_size: float = 1.0
) -> tuple[float, float, float]:
    """Park a scale reference on the far side of the asset, off the view ray.

    Top views look down the Y axis, so the reference moves to +X instead of
    sitting between the lens and the silhouette.
    """
    center = bounds.center
    span = max(bounds.size)
    half = reference_size * 0.5
    if view == "top":
        return (
            bounds.max_x + half + span + 0.25,
            bounds.min_y + half,
            center[2],
        )
    direction = view_direction(view)
    away = (-direction[0], 0.0, -direction[2])
    length = math.sqrt(away[0] ** 2 + away[2] ** 2)
    if length <= 1e-9:
        away = (1.0, 0.0, 0.0)
        length = 1.0
    away = (away[0] / length, 0.0, away[2] / length)
    distance = span + half + 0.5
    return (
        center[0] + away[0] * distance,
        bounds.min_y + half,
        center[2] + away[2] * distance,
    )
