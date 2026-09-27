"""Review capture selection keeps accepted views and can replace only side."""

import pytest

from gamefactory.core.domain.errors import RuntimeValidationFailedError
from gamefactory.core.domain.models import Artifact
from gamefactory.workflows.asset_production import (
    select_review_captures,
    validate_side_framing,
)


def _capture(artifact_id: str, relative_path: str, created_at: str) -> Artifact:
    return Artifact(
        id=artifact_id,
        workflow_id="WF-1",
        task_id="TASK-1",
        artifact_type="asset-runtime-capture",
        producer="asset_godot",
        relative_path=relative_path,
        content_hash="a" * 64,
        created_at=created_at,
    )


def test_single_attempt_selects_its_three_angles() -> None:
    stage = ".gamefactory/scratch/asset-WF-1-EXEC-old/captures"
    captures = [
        _capture("front", f"{stage}/front.png", "2026-01-01T00:00:00"),
        _capture("side", f"{stage}/side.png", "2026-01-01T00:00:02"),
        _capture("quarter", f"{stage}/three_quarter.png", "2026-01-01T00:00:01"),
    ]
    selected = select_review_captures(captures, "EXEC-old")
    assert [item.id for item in selected] == ["front", "quarter", "side"]


def test_later_side_replaces_only_the_side_view() -> None:
    original = ".gamefactory/scratch/asset-WF-1-EXEC-old/captures"
    corrected = ".gamefactory/scratch/asset-WF-1-EXEC-new/captures"
    captures = [
        _capture("front", f"{original}/front.png", "2026-01-01T00:00:00"),
        _capture("quarter", f"{original}/three_quarter.png", "2026-01-01T00:00:01"),
        _capture("side-old", f"{original}/side.png", "2026-01-01T00:00:02"),
        _capture("side-new", f"{corrected}/side.png", "2026-01-02T00:00:00"),
    ]
    selected = select_review_captures(captures, "EXEC-old")
    assert [item.id for item in selected] == ["front", "quarter", "side-new"]


def test_side_framing_contract() -> None:
    validate_side_framing(
        {
            "inside_viewport": True,
            "margin_ok": True,
            "height_ratio": 0.65,
            "horizontally_centered": True,
            "reference_between_camera_and_asset": False,
            "view_axis": "+X",
        }
    )
    with pytest.raises(RuntimeValidationFailedError):
        validate_side_framing(
            {
                "inside_viewport": True,
                "margin_ok": True,
                "height_ratio": 0.4,
                "horizontally_centered": True,
                "reference_between_camera_and_asset": False,
                "view_axis": "+X",
            }
        )
