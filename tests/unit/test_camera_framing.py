"""Bounds-driven review cameras do not use asset-specific coordinates."""

import math
from typing import Any

import pytest

from gamefactory.core.domain.camera_framing import (
    IMPLEMENTED_VIEWS,
    PLACED_VIEWS,
    VIEW_PLACEMENTS,
    BoundsAABB,
    CameraFrame,
    camera_basis,
    frame_camera,
    framing_geometry,
    reference_offset,
    view_axis_label,
    view_direction,
    view_up,
)


def project_camera_fractions(
    frame: CameraFrame,
    bounds: BoundsAABB,
    viewport: tuple[int, int] = (1280, 720),
) -> dict[str, Any]:
    """Measure the projected 8-corner fractions of the returned CameraFrame.

    Independent projection implementation using camera look_at basis and perspective.
    """
    to_target = (
        frame.target[0] - frame.position[0],
        frame.target[1] - frame.position[1],
        frame.target[2] - frame.position[2],
    )
    dist = math.sqrt(sum(x * x for x in to_target))
    f = tuple(x / dist for x in to_target)
    f_x_up = (
        f[1] * frame.up[2] - f[2] * frame.up[1],
        f[2] * frame.up[0] - f[0] * frame.up[2],
        f[0] * frame.up[1] - f[1] * frame.up[0],
    )
    r_len = math.sqrt(sum(x * x for x in f_x_up))
    r = tuple(x / r_len for x in f_x_up)
    u = (
        r[1] * f[2] - r[2] * f[1],
        r[2] * f[0] - r[0] * f[2],
        r[0] * f[1] - r[1] * f[0],
    )

    half_fov = math.radians(frame.fov_degrees * 0.5)
    t = math.tan(half_fov)
    aspect = viewport[0] / viewport[1]

    hx, hy, hz = bounds.size[0] * 0.5, bounds.size[1] * 0.5, bounds.size[2] * 0.5
    cx, cy, cz = bounds.center

    corners = [
        (cx + sx * hx, cy + sy * hy, cz + sz * hz)
        for sx in (-1.0, 1.0)
        for sy in (-1.0, 1.0)
        for sz in (-1.0, 1.0)
    ]

    min_x, max_x = float("inf"), float("-inf")
    min_y, max_y = float("inf"), float("-inf")
    min_px, max_px = float("inf"), float("-inf")
    min_py, max_py = float("inf"), float("-inf")

    all_in_front = True
    for c in corners:
        v = (
            c[0] - frame.position[0],
            c[1] - frame.position[1],
            c[2] - frame.position[2],
        )
        z = f[0] * v[0] + f[1] * v[1] + f[2] * v[2]
        if z <= 0.0:
            all_in_front = False
        y = (u[0] * v[0] + u[1] * v[1] + u[2] * v[2]) / (z * t)
        x = (r[0] * v[0] + r[1] * v[1] + r[2] * v[2]) / (z * t * aspect)
        if y < min_y:
            min_y = y
        if y > max_y:
            max_y = y
        if x < min_x:
            min_x = x
        if x > max_x:
            max_x = x

        px = (x + 1.0) * 0.5 * viewport[0]
        py = (1.0 - y) * 0.5 * viewport[1]
        if px < min_px:
            min_px = px
        if px > max_px:
            max_px = px
        if py < min_py:
            min_py = py
        if py > max_py:
            max_py = py

    v_frac = (max_y - min_y) * 0.5
    h_frac = (max_x - min_x) * 0.5
    return {
        "all_in_front": all_in_front,
        "vertical_fraction": v_frac,
        "horizontal_fraction": h_frac,
        "max_fraction": max(v_frac, h_frac),
        "pixel_box": (min_px, min_py, max_px, max_py),
        "inside_margin_4pct": (
            min_px >= 0.04 * viewport[0] - 1e-6
            and max_px <= 0.96 * viewport[0] + 1e-6
            and min_py >= 0.04 * viewport[1] - 1e-6
            and max_py <= 0.96 * viewport[1] + 1e-6
        ),
    }


def simulate_old_one_shot_framing(
    bounds: BoundsAABB,
    view: str,
    *,
    fov_degrees: float = 38.0,
    target_screen_fraction: float = 0.62,
    viewport: tuple[int, int] = (1280, 720),
) -> tuple[float, float, float, float]:
    """Reproduce the pre-fix center-distance and one-shot correction behaviour.

    Returns:
        (old_center_distance, initial_rendered_ratio, corrected_distance, corrected_rendered_ratio)
    """
    width, height, depth = bounds.size
    if view == "top":
        vertical, horizontal = depth, width
    elif view == "side":
        vertical, horizontal = height, depth
    elif view == "front":
        vertical, horizontal = height, width
    else:
        dominant = max(width, height, depth)
        vertical, horizontal = dominant, dominant

    half_fov = math.radians(fov_degrees * 0.5)
    t = math.tan(half_fov)
    aspect = viewport[0] / viewport[1]
    old_distance = max(
        vertical / (2.0 * t * target_screen_fraction),
        horizontal / (2.0 * t * target_screen_fraction * aspect),
    )

    dir_vec = view_direction(view)
    cx, cy, cz = bounds.center
    frame_initial = CameraFrame(
        position=(
            cx + dir_vec[0] * old_distance,
            cy + dir_vec[1] * old_distance,
            cz + dir_vec[2] * old_distance,
        ),
        target=bounds.center,
        up=view_up(view),
        near=0.01,
        far=50.0,
        fov_degrees=fov_degrees,
        view=view,
    )
    initial_proj = project_camera_fractions(frame_initial, bounds, viewport)
    initial_ratio = initial_proj["vertical_fraction"]

    corrected_distance = old_distance * (initial_ratio / target_screen_fraction)
    frame_corrected = CameraFrame(
        position=(
            cx + dir_vec[0] * corrected_distance,
            cy + dir_vec[1] * corrected_distance,
            cz + dir_vec[2] * corrected_distance,
        ),
        target=bounds.center,
        up=view_up(view),
        near=0.01,
        far=50.0,
        fov_degrees=fov_degrees,
        view=view,
    )
    corrected_proj = project_camera_fractions(frame_corrected, bounds, viewport)
    corrected_ratio = corrected_proj["vertical_fraction"]
    return old_distance, initial_ratio, corrected_distance, corrected_ratio


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
    # Axis-aligned side camera distance includes half-depth e_f = 0.6
    distance = 0.6 + max(
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


def test_pickup_regression_tall_y_view_top() -> None:
    """Test A: tall pickup 0.22x0.48x0.22, pickup@1 (fov 38, target 0.62, 1280x720).

    Verifies the pre-fix failure and confirms the basis-projected camera frames
    within the [0.50, 0.75] profile contract.
    """
    bounds = BoundsAABB(-0.11, -0.24, -0.11, 0.11, 0.24, 0.11)

    # Pre-fix one-shot behaviour proof:
    # Initial center-distance 0.515 gave height_ratio 1.161 (overflowing viewport),
    # and the one-shot correction *= 1.161 / 0.62 overshot to ~0.441 (below the 0.50 minimum).
    old_dist, init_ratio, corr_dist, corr_ratio = simulate_old_one_shot_framing(
        bounds,
        "top",
        fov_degrees=38,
        target_screen_fraction=0.62,
        viewport=(1280, 720),
    )
    assert old_dist == pytest.approx(0.515, abs=0.005)
    assert init_ratio == pytest.approx(1.161, abs=0.005)
    assert corr_ratio == pytest.approx(0.441, abs=0.005)
    assert init_ratio > 0.75  # Fails contract high
    assert corr_ratio < 0.50  # Fails contract low

    # New basis-projected exact solve:
    frame = frame_camera(
        bounds,
        "top",
        fov_degrees=38,
        target_screen_fraction=0.62,
        viewport=(1280, 720),
    )
    proj = project_camera_fractions(frame, bounds, (1280, 720))
    assert 0.50 <= proj["vertical_fraction"] <= 0.75
    assert proj["vertical_fraction"] == pytest.approx(0.62, abs=0.005)
    assert proj["all_in_front"] is True
    assert proj["inside_margin_4pct"] is True


def test_cube_static_prop_front_and_side() -> None:
    """Test B: Cube 1x1x1, static_prop (fov 38, target 0.65, 1280x720).

    Asserts front and side views are in [0.55, 0.75] and approx 0.65.
    """
    bounds = BoundsAABB(-0.5, -0.5, -0.5, 0.5, 0.5, 0.5)
    for view in ("front", "side"):
        frame = frame_camera(
            bounds,
            view,
            fov_degrees=38,
            target_screen_fraction=0.65,
            viewport=(1280, 720),
        )
        proj = project_camera_fractions(frame, bounds, (1280, 720))
        assert 0.55 <= proj["vertical_fraction"] <= 0.75
        assert proj["vertical_fraction"] == pytest.approx(0.65, abs=0.005)
        assert proj["inside_margin_4pct"] is True
        assert proj["all_in_front"] is True


def test_wall_modular_piece_all_views() -> None:
    """Test C: Wall 1.0x2.0x0.25, modular_piece (fov 38, target 0.62, 1280x720).

    Asserts front, side, and three_quarter views are in [0.50, 0.75].
    """
    bounds = BoundsAABB(-0.5, -1.0, -0.125, 0.5, 1.0, 0.125)
    for view in ("front", "side", "three_quarter"):
        frame = frame_camera(
            bounds,
            view,
            fov_degrees=38,
            target_screen_fraction=0.62,
            viewport=(1280, 720),
        )
        proj = project_camera_fractions(frame, bounds, (1280, 720))
        assert 0.50 <= proj["max_fraction"] <= 0.75
        assert proj["max_fraction"] == pytest.approx(0.62, abs=0.005)
        assert proj["inside_margin_4pct"] is True
        assert proj["all_in_front"] is True


def test_three_quarter_non_axis_aligned_basis() -> None:
    """Test D: Three-quarter non-axis-aligned basis for pickup and crate.

    Verifies extents match support formula, solved distance <= conservative distance,
    ratio approx target and inside profile range, and all corners in front of camera.
    """
    pickup_bounds = BoundsAABB(-0.11, -0.24, -0.11, 0.11, 0.24, 0.11)
    crate_bounds = BoundsAABB(-0.5, -0.5, -0.5, 0.5, 0.5, 0.5)

    cases = [
        ("pickup", pickup_bounds, 0.62, 0.50, 0.75),
        ("crate", crate_bounds, 0.65, 0.55, 0.75),
    ]
    for _name, bounds, tf, min_ratio, max_ratio in cases:
        geom = framing_geometry(
            bounds,
            "three_quarter",
            fov_degrees=38,
            target_screen_fraction=tf,
            viewport=(1280, 720),
        )
        frame = frame_camera(
            bounds,
            "three_quarter",
            fov_degrees=38,
            target_screen_fraction=tf,
            viewport=(1280, 720),
        )
        proj = project_camera_fractions(frame, bounds, (1280, 720))

        # Extents equal the support formula
        h = (bounds.size[0] * 0.5, bounds.size[1] * 0.5, bounds.size[2] * 0.5)
        _, _, f, r, u = camera_basis("three_quarter")
        expected_e_r = abs(r[0]) * h[0] + abs(r[1]) * h[1] + abs(r[2]) * h[2]
        expected_e_u = abs(u[0]) * h[0] + abs(u[1]) * h[1] + abs(u[2]) * h[2]
        expected_e_f = abs(f[0]) * h[0] + abs(f[1]) * h[1] + abs(f[2]) * h[2]
        assert geom.projected_right_half == pytest.approx(expected_e_r)
        assert geom.projected_up_half == pytest.approx(expected_e_u)
        assert geom.view_depth_half == pytest.approx(expected_e_f)

        # Solved distance <= conservative distance
        assert geom.distance <= geom.conservative_distance

        # Ratio approx target and inside profile range
        assert min_ratio <= proj["max_fraction"] <= max_ratio
        assert proj["max_fraction"] == pytest.approx(tf, abs=0.005)

        # All corners in front of camera
        assert proj["all_in_front"] is True
        assert frame.near < geom.distance - geom.view_depth_half
        assert proj["inside_margin_4pct"] is True


def test_aspect_ratio_landscape_portrait_and_ultrawide() -> None:
    """Test E: Landscape (1280x720), portrait (720x1280), and wide (2560x720).

    Both horizontal and vertical fractions <= target + 1e-6, and binding one approx target.
    """
    bounds = BoundsAABB(-0.5, -0.5, -0.5, 0.5, 0.5, 0.5)
    tf = 0.65
    for vp in ((1280, 720), (720, 1280), (2560, 720)):
        frame = frame_camera(
            bounds,
            "three_quarter",
            fov_degrees=38,
            target_screen_fraction=tf,
            viewport=vp,
        )
        proj = project_camera_fractions(frame, bounds, vp)
        assert proj["vertical_fraction"] <= tf + 1e-6
        assert proj["horizontal_fraction"] <= tf + 1e-6
        assert proj["max_fraction"] == pytest.approx(tf, abs=0.005)
        assert proj["inside_margin_4pct"] is True


def test_horizontal_fit_safety_margin_across_cases() -> None:
    """Test F: Projected rect is inside viewport with a 4% safety margin."""
    pickup = BoundsAABB(-0.11, -0.24, -0.11, 0.11, 0.24, 0.11)
    crate = BoundsAABB(-0.5, -0.5, -0.5, 0.5, 0.5, 0.5)
    wall = BoundsAABB(-0.5, -1.0, -0.125, 0.5, 1.0, 0.125)

    scenarios = [
        (pickup, "top", 0.62, (1280, 720)),
        (pickup, "front", 0.62, (1280, 720)),
        (pickup, "three_quarter", 0.62, (1280, 720)),
        (crate, "front", 0.65, (1280, 720)),
        (crate, "side", 0.65, (1280, 720)),
        (crate, "three_quarter", 0.65, (1280, 720)),
        (crate, "three_quarter", 0.65, (720, 1280)),
        (wall, "front", 0.62, (1280, 720)),
        (wall, "side", 0.62, (1280, 720)),
        (wall, "three_quarter", 0.62, (1280, 720)),
    ]
    for bounds, view, tf, vp in scenarios:
        frame = frame_camera(
            bounds,
            view,
            fov_degrees=38,
            target_screen_fraction=tf,
            viewport=vp,
        )
        proj = project_camera_fractions(frame, bounds, vp)
        assert proj["inside_margin_4pct"] is True, f"Failed margin for {view} at {vp}"
        assert proj["all_in_front"] is True


def test_all_nine_views_have_the_normative_vectors_up_and_labels() -> None:
    diagonal_front = math.sqrt(2.0 + 0.65**2)
    diagonal_rear = math.sqrt(2.0 + 0.65**2)
    expected = {
        "front": ((0, 0, -1), (0, 1, 0), "-Z"),
        "rear": ((0, 0, 1), (0, 1, 0), "+Z"),
        "left": ((-1, 0, 0), (0, 1, 0), "-X"),
        "right": ((1, 0, 0), (0, 1, 0), "+X"),
        "side": ((1, 0, 0), (0, 1, 0), "+X"),
        "three_quarter": (
            (1 / diagonal_front, 0.65 / diagonal_front, -1 / diagonal_front),
            (0, 1, 0),
            "+X-Z",
        ),
        "three_quarter_front": (
            (1 / diagonal_front, 0.65 / diagonal_front, -1 / diagonal_front),
            (0, 1, 0),
            "+X-Z",
        ),
        "three_quarter_rear": (
            (1 / diagonal_rear, 0.65 / diagonal_rear, 1 / diagonal_rear),
            (0, 1, 0),
            "+X+Z",
        ),
        "top": ((0, 1, 0), (0, 0, -1), "+Y"),
    }
    assert set(VIEW_PLACEMENTS) == PLACED_VIEWS == IMPLEMENTED_VIEWS == set(expected)
    for view, (direction, up, label) in expected.items():
        assert view_direction(view) == pytest.approx(direction)
        assert view_up(view) == up
        assert view_axis_label(view) == label


def test_all_views_frame_wide_and_tall_bounds_in_landscape_and_portrait() -> None:
    cases = (
        (BoundsAABB(-2.0, -0.7, -0.25, 2.0, 0.7, 0.25), (1280, 720)),
        (BoundsAABB(-0.25, -2.0, -0.25, 0.25, 2.0, 0.25), (720, 1280)),
    )
    for view in PLACED_VIEWS:
        for bounds, viewport in cases:
            frame = frame_camera(
                bounds,
                view,
                fov_degrees=38,
                target_screen_fraction=0.62,
                viewport=viewport,
            )
            measured = project_camera_fractions(frame, bounds, viewport)
            assert measured["all_in_front"] is True, (view, viewport)
            assert measured["max_fraction"] == pytest.approx(0.62, abs=0.005), (view, viewport)
            assert measured["inside_margin_4pct"] is True, (view, viewport)


@pytest.mark.parametrize("view_function", [view_direction, view_up, view_axis_label])
@pytest.mark.parametrize("unknown_view", ["front_typo", ["front"]])
def test_unknown_camera_view_fails_explicitly(view_function, unknown_view) -> None:
    with pytest.raises(ValueError, match="no camera placement"):
        view_function(unknown_view)


def test_unknown_camera_view_cannot_be_framed() -> None:
    with pytest.raises(ValueError, match="no camera placement"):
        frame_camera(
            BoundsAABB(-0.5, -0.5, -0.5, 0.5, 0.5, 0.5),
            "front_typo",
            fov_degrees=38,
            target_screen_fraction=0.62,
            viewport=(1280, 720),
        )
