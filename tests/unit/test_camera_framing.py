"""Bounds-driven review cameras do not use asset-specific coordinates."""

import math

import pytest

from gamefactory.core.domain.camera_framing import BoundsAABB, frame_camera, reference_offset


def test_side_camera_distance_tracks_asset_height() -> None:
    bounds = BoundsAABB(-0.6, 0.0, -0.5, 0.6, 1.0, 0.5)
    frame = frame_camera(
        bounds,
        "side",
        fov_degrees=38,
        target_screen_fraction=0.65,
        viewport=(1280, 720),
    )
    vertical, horizontal = 1.0, 1.0
    half_fov = math.radians(19)
    tangent = math.tan(half_fov)
    aspect = 1280 / 720
    distance = max(
        vertical / (2.0 * tangent * 0.65),
        horizontal / (2.0 * tangent * 0.65 * aspect),
    )
    assert frame.position[0] == pytest.approx(bounds.center[0] + distance)
    assert frame.target == bounds.center
    offset = reference_offset(bounds, "side")
    assert offset[0] < bounds.min_x


def test_top_reference_is_not_on_the_view_axis() -> None:
    bounds = BoundsAABB(-0.15, -0.2, -0.15, 0.15, 0.2, 0.15)
    frame = frame_camera(
        bounds, "top", fov_degrees=38, target_screen_fraction=0.62, viewport=(1280, 720)
    )
    assert frame.up == (0.0, 0.0, -1.0)
    assert frame.position[1] > bounds.max_y
    offset = reference_offset(bounds, "top")
    assert offset[0] > bounds.max_x
