"""ADR 0014: every named review view is placed; unknown views fail explicitly."""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

from gamefactory.core.domain import camera_framing as cf
from gamefactory.core.domain.errors import RuntimeValidationFailedError
from gamefactory.workflows.asset_production import validate_view_framing

HARNESS = Path("src/gamefactory/resources/godot/asset_runtime_harness.gd")

EXPECTED = {
    "front": ((0.0, 0.0, -1.0), "-Z"),
    "rear": ((0.0, 0.0, 1.0), "+Z"),
    "left": ((-1.0, 0.0, 0.0), "-X"),
    "right": ((1.0, 0.0, 0.0), "+X"),
    "side": ((1.0, 0.0, 0.0), "+X"),
    "top": ((0.0, 1.0, 0.0), "+Y"),
}


def test_every_implemented_view_is_placed() -> None:
    assert cf.PLACED_VIEWS == cf.IMPLEMENTED_VIEWS
    for view in cf.IMPLEMENTED_VIEWS:
        d = cf.view_direction(view)
        assert math.isclose(math.sqrt(sum(c * c for c in d)), 1.0, abs_tol=1e-12)
        assert cf.view_axis_label(view)


@pytest.mark.parametrize(("view", "expected"), sorted(EXPECTED.items()))
def test_principal_views_face_the_documented_axis(
    view: str, expected: tuple[tuple[float, float, float], str]
) -> None:
    direction, label = expected
    assert cf.view_direction(view) == pytest.approx(direction)
    assert cf.view_axis_label(view) == label


def test_side_is_an_alias_of_right_and_rear_quarter_mirrors_front() -> None:
    assert cf.view_direction("side") == cf.view_direction("right")
    front = cf.view_direction("three_quarter")
    rear = cf.view_direction("three_quarter_rear")
    assert rear == pytest.approx((front[0], front[1], -front[2]))


@pytest.mark.parametrize("view", ["back", "underside", "", "SIDE"])
def test_unknown_view_is_rejected_without_fallback(view: str) -> None:
    with pytest.raises(ValueError):
        cf.view_direction(view)
    with pytest.raises(ValueError):
        cf.view_axis_label(view)
    with pytest.raises(ValueError):
        cf.view_up(view)


@pytest.mark.parametrize("view", sorted(cf.PLACED_VIEWS))
def test_every_placed_view_frames_a_box(view: str) -> None:
    geom = cf.framing_geometry(cf.BoundsAABB(-1, 0, -0.5, 1, 1.2, 0.5), view)
    assert 0.6 < max(geom.vertical_fraction, geom.horizontal_fraction) <= 0.65 + 1e-6


def test_harness_view_table_matches_python_and_has_no_fallback() -> None:
    text = HARNESS.read_text(encoding="utf-8")
    pattern = r'"([a-z_]+)": \[Vector3\(([-0-9., ]+)\), "([-+XYZ]+)"\]'
    rows = {
        name: (tuple(float(v) for v in vec.split(",")), label)
        for name, vec, label in re.findall(pattern, text)
    }
    assert set(rows) == set(cf.PLACED_VIEWS)
    for name, (vec, label) in rows.items():
        norm = math.sqrt(sum(v * v for v in vec))
        assert tuple(v / norm for v in vec) == pytest.approx(cf.view_direction(name))
        assert label == cf.view_axis_label(name)
    up = text.split("func _view_up_vector", 1)[1].split("\nfunc ", 1)[0]
    assert 'if angle == "top":\n\t\treturn Vector3(0, 0, -1)' in up and "return Vector3.UP" in up
    assert cf.view_up("top") == (0.0, 0.0, -1.0)
    assert all(cf.view_up(v) == (0.0, 1.0, 0.0) for v in cf.PLACED_VIEWS if v != "top")
    body = text.split("func _view_direction_vector", 1)[1].split("\nfunc ", 1)[0]
    assert "_fail(" in body
    assert "0.65, -1).normalized()" not in body


def test_capture_framing_rejects_an_unplaced_view_and_a_wrong_axis() -> None:
    framing = {
        "height_ratio": 0.65,
        "inside_viewport": True,
        "margin_ok": True,
        "horizontally_centered": True,
        "reference_between_camera_and_asset": False,
        "view_axis": "+Z",
    }
    validate_view_framing(framing, view="rear", minimum=0.55, maximum=0.75)
    with pytest.raises(RuntimeValidationFailedError, match="view.unplaced"):
        validate_view_framing(framing, view="underside", minimum=0.55, maximum=0.75)
    with pytest.raises(RuntimeValidationFailedError, match="not the placed"):
        validate_view_framing(framing, view="left", minimum=0.55, maximum=0.75)


def test_frozen_v05_profile_schema_keeps_its_view_vocabulary() -> None:
    assert frozenset({"front", "three_quarter", "three_quarter_front", "side", "top"}) == (
        cf.LEGACY_PROFILE_VIEWS
    )
