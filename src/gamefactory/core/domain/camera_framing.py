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
# Every named view has a camera placement (ADR 0014). The asset faces -Z with
# +Y up, so its right side is +X. ``side`` is the legacy alias of ``right``.
PLACED_VIEWS = IMPLEMENTED_VIEWS
# asset-profile-0.5.0 is frozen: its documents keep the V0.5 view vocabulary.
LEGACY_PROFILE_VIEWS = frozenset({"front", "three_quarter", "three_quarter_front", "side", "top"})

_THREE_QUARTER = (1.0, 0.65, -1.0)
_THREE_QUARTER_REAR = (1.0, 0.65, 1.0)

# Unit-vector sources from the asset center toward the camera, and axis labels.
_VIEW_TABLE: dict[str, tuple[tuple[float, float, float], str]] = {
    "front": ((0.0, 0.0, -1.0), "-Z"),
    "rear": ((0.0, 0.0, 1.0), "+Z"),
    "left": ((-1.0, 0.0, 0.0), "-X"),
    "right": ((1.0, 0.0, 0.0), "+X"),
    "side": ((1.0, 0.0, 0.0), "+X"),
    "three_quarter": (_THREE_QUARTER, "+X-Z"),
    "three_quarter_front": (_THREE_QUARTER, "+X-Z"),
    "three_quarter_rear": (_THREE_QUARTER_REAR, "+X+Z"),
    "top": ((0.0, 1.0, 0.0), "+Y"),
}


def _normalize(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(sum(component * component for component in vector))
    if length <= 1e-9 or not math.isfinite(length):
        raise ValueError("camera direction is degenerate")
    x, y, z = (component / length for component in vector)
    return (x, y, z)


def view_direction(view: str) -> tuple[float, float, float]:
    """Unit vector from the asset center toward the camera."""
    if view not in PLACED_VIEWS or view not in _VIEW_TABLE:
        raise ValueError(f"view '{view}' has no camera placement")
    return _normalize(_VIEW_TABLE[view][0])


def view_up(view: str) -> tuple[float, float, float]:
    if view not in _VIEW_TABLE:
        raise ValueError(f"view '{view}' has no camera placement")
    if view == "top":
        return (0.0, 0.0, -1.0)
    return (0.0, 1.0, 0.0)


def view_axis_label(view: str) -> str:
    if view not in _VIEW_TABLE:
        raise ValueError(f"view '{view}' has no camera placement")
    return _VIEW_TABLE[view][1]


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


def _cross(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _support_extent(
    axis: tuple[float, float, float], half_extents: tuple[float, float, float]
) -> float:
    return (
        abs(axis[0]) * half_extents[0]
        + abs(axis[1]) * half_extents[1]
        + abs(axis[2]) * half_extents[2]
    )


def camera_basis(
    view: str,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]:
    """Camera basis from view direction d and view up vector, matching Godot look_at.

    Returns:
        (direction d, up_target up, forward f, right r, up u)
    """
    d = view_direction(view)
    up = view_up(view)
    f = (-d[0], -d[1], -d[2])
    r = _normalize(_cross(f, up))
    u = _cross(r, f)
    return d, up, f, r, u


@dataclass(frozen=True)
class FramingGeometry:
    view: str
    direction: tuple[float, float, float]
    up_vector: tuple[float, float, float]
    forward: tuple[float, float, float]
    right: tuple[float, float, float]
    up: tuple[float, float, float]
    projected_right_half: float  # e_r
    projected_up_half: float  # e_u
    view_depth_half: float  # e_f
    conservative_distance: float  # d_c
    distance: float  # solved distance
    vertical_fraction: float
    horizontal_fraction: float

    @property
    def e_r(self) -> float:
        return self.projected_right_half

    @property
    def e_u(self) -> float:
        return self.projected_up_half

    @property
    def e_f(self) -> float:
        return self.view_depth_half

    @property
    def d_c(self) -> float:
        return self.conservative_distance


def framing_geometry(
    bounds: BoundsAABB,
    view: str,
    fov_degrees: float = 38.0,
    target_screen_fraction: float = 0.65,
    viewport: tuple[int, int] = (1280, 720),
) -> FramingGeometry:
    """Compute basis projection extents and exact bisection distance."""
    if viewport[0] <= 0 or viewport[1] <= 0:
        raise ValueError("viewport size must be positive")
    if not 0.05 <= target_screen_fraction <= 0.95:
        raise ValueError("target screen fraction is outside 0.05-0.95")
    if not 1.0 <= fov_degrees <= 120.0:
        raise ValueError("fov is outside 1-120 degrees")
    size = bounds.size
    if min(size) <= 1e-6:
        raise ValueError("asset bounds are empty")

    hx, hy, hz = size[0] * 0.5, size[1] * 0.5, size[2] * 0.5
    h = (hx, hy, hz)
    d, view_up_vec, f, r, u = camera_basis(view)
    e_r = _support_extent(r, h)
    e_u = _support_extent(u, h)
    e_f = _support_extent(f, h)

    half_fov = math.radians(fov_degrees * 0.5)
    t = math.tan(half_fov)
    aspect = viewport[0] / viewport[1]

    d_c = e_f + max(
        e_u / (t * target_screen_fraction),
        e_r / (t * target_screen_fraction * aspect),
    )

    corners = [
        (sx * hx, sy * hy, sz * hz)
        for sx in (-1.0, 1.0)
        for sy in (-1.0, 1.0)
        for sz in (-1.0, 1.0)
    ]

    def _screen_fractions(dist: float) -> tuple[float, float]:
        min_x = float("inf")
        max_x = float("-inf")
        min_y = float("inf")
        max_y = float("-inf")
        for p in corners:
            z = dist + _dot(f, p)
            y = _dot(u, p) / (z * t)
            x = _dot(r, p) / (z * t * aspect)
            if y < min_y:
                min_y = y
            if y > max_y:
                max_y = y
            if x < min_x:
                min_x = x
            if x > max_x:
                max_x = x
        return (max_y - min_y) * 0.5, (max_x - min_x) * 0.5

    low = e_f + min(1e-4, (d_c - e_f) * 0.5)
    high = d_c
    for _ in range(60):
        mid = (low + high) * 0.5
        v_mid, h_mid = _screen_fractions(mid)
        if max(v_mid, h_mid) > target_screen_fraction:
            low = mid
        else:
            high = mid

    distance = high
    vert_frac, horiz_frac = _screen_fractions(distance)
    return FramingGeometry(
        view=view,
        direction=d,
        up_vector=view_up_vec,
        forward=f,
        right=r,
        up=u,
        projected_right_half=e_r,
        projected_up_half=e_u,
        view_depth_half=e_f,
        conservative_distance=d_c,
        distance=distance,
        vertical_fraction=vert_frac,
        horizontal_fraction=horiz_frac,
    )


def frame_camera(
    bounds: BoundsAABB,
    view: str,
    *,
    fov_degrees: float,
    target_screen_fraction: float,
    viewport: tuple[int, int],
) -> CameraFrame:
    """Place a camera so the asset fills about ``target_screen_fraction`` of the frame."""
    geom = framing_geometry(
        bounds,
        view,
        fov_degrees=fov_degrees,
        target_screen_fraction=target_screen_fraction,
        viewport=viewport,
    )
    distance = geom.distance
    direction = geom.direction
    center = bounds.center
    position = (
        center[0] + direction[0] * distance,
        center[1] + direction[1] * distance,
        center[2] + direction[2] * distance,
    )
    e_f = geom.view_depth_half
    near = max(0.001, min(max(distance * 0.01, 0.01), (distance - e_f) * 0.5))
    far = max(distance * 20.0, 50.0, distance + e_f + 10.0)
    return CameraFrame(
        position=position,
        target=center,
        up=view_up(view),
        near=near,
        far=far,
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


@dataclass(frozen=True)
class ProjectedFramingMetrics:
    height_ratio: float
    fill_ratio: float
    center_offset: float
    inside_margin: bool
    horizontally_centered: bool


def framing_metrics_from_projected_rect(
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    viewport_width: float,
    viewport_height: float,
    margin_fraction: float,
    horizontal_center_tolerance: float = 0.08,
) -> ProjectedFramingMetrics:
    if viewport_width <= 0 or viewport_height <= 0:
        raise ValueError("viewport dimensions must be positive")
    margin_x = viewport_width * margin_fraction
    margin_y = viewport_height * margin_fraction
    max_x = x + width
    max_y = y + height
    inside = (
        x >= margin_x
        and y >= margin_y
        and max_x <= viewport_width - margin_x
        and max_y <= viewport_height - margin_y
    )
    height_ratio = height / viewport_height
    fill_ratio = max(height_ratio, width / viewport_width)
    center_x = x + width * 0.5
    center_offset = abs(center_x - viewport_width * 0.5) / viewport_width
    horizontally_centered = center_offset <= horizontal_center_tolerance
    return ProjectedFramingMetrics(
        height_ratio=height_ratio,
        fill_ratio=fill_ratio,
        center_offset=center_offset,
        inside_margin=inside,
        horizontally_centered=horizontally_centered,
    )
