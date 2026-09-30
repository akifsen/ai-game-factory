"""Runtime parity check for the canonical Python and Godot review-view tables."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from gamefactory.core.domain.camera_framing import (
    PLACED_VIEWS,
    view_axis_label,
    view_direction,
    view_up,
)


@pytest.mark.skipif(
    not os.environ.get("GAMEFACTORY_TEST_GODOT"),
    reason="set GAMEFACTORY_TEST_GODOT to run the real Godot placement-table probe",
)
def test_godot_runtime_view_table_matches_python(tmp_path: Path) -> None:
    godot = Path(os.environ["GAMEFACTORY_TEST_GODOT"])
    if not godot.is_file():
        pytest.fail(f"configured Godot console executable does not exist: {godot}")
    repo = Path(__file__).resolve().parents[2]
    harness = repo / "src/gamefactory/resources/godot/asset_runtime_harness.gd"
    (tmp_path / "asset_runtime_harness.gd").write_bytes(harness.read_bytes())
    (tmp_path / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    (tmp_path / "view_probe.gd").write_text(
        """extends SceneTree
const Harness = preload("res://asset_runtime_harness.gd")
const VIEW_IDS = ["front", "rear", "left", "right", "side", "three_quarter", "three_quarter_front", "three_quarter_rear", "top"]
func _initialize() -> void:
    var table := {}
    for view in VIEW_IDS:
        var placement: Dictionary = Harness.view_placement(view)
        var direction: Vector3 = placement.direction
        var up: Vector3 = placement.up
        table[view] = {"direction": [direction.x, direction.y, direction.z], "up": [up.x, up.y, up.z], "axis_label": placement.axis_label}
    print("VIEW_TABLE_JSON:" + JSON.stringify(table))
    quit(0)
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(godot), "--headless", "--path", str(tmp_path), "--script", "res://view_probe.gd"],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    table_line = next(
        (
            line.removeprefix("VIEW_TABLE_JSON:")
            for line in result.stdout.splitlines()
            if line.startswith("VIEW_TABLE_JSON:")
        ),
        None,
    )
    assert table_line is not None, result.stdout + result.stderr
    godot_table = json.loads(table_line)
    assert set(godot_table) == PLACED_VIEWS
    for view in PLACED_VIEWS:
        assert godot_table[view]["direction"] == pytest.approx(view_direction(view))
        assert godot_table[view]["up"] == pytest.approx(view_up(view))
        assert godot_table[view]["axis_label"] == view_axis_label(view)


@pytest.mark.parametrize(
    ("angles", "message"),
    [
        (["front", "front_typo"], "unknown capture angle front_typo"),
        (["front", "front"], "duplicate capture angle front"),
    ],
)
@pytest.mark.skipif(
    not os.environ.get("GAMEFACTORY_TEST_GODOT"),
    reason="set GAMEFACTORY_TEST_GODOT to run the real Godot request guard",
)
def test_godot_harness_rejects_invalid_angle_requests(
    tmp_path: Path, angles: list[str], message: str
) -> None:
    godot = Path(os.environ["GAMEFACTORY_TEST_GODOT"])
    if not godot.is_file():
        pytest.fail(f"configured Godot console executable does not exist: {godot}")
    repo = Path(__file__).resolve().parents[2]
    harness = repo / "src/gamefactory/resources/godot/asset_runtime_harness.gd"
    (tmp_path / "asset_runtime_harness.gd").write_bytes(harness.read_bytes())
    (tmp_path / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    request = {
        "workflow_id": "probe",
        "revision": 1,
        "asset_id": "probe",
        "glb": "missing.glb",
        "output_dir": ".",
        "execution_id": "probe",
        "attempt_number": 1,
        "processed_glb_sha256": "0" * 64,
        "observation_path": "observation.json",
        "angles": angles,
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    result = subprocess.run(
        [
            str(godot),
            "--headless",
            "--path",
            str(tmp_path),
            "--script",
            "res://asset_runtime_harness.gd",
            "--",
            "--request",
            str(request_path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode != 0
    assert message in result.stdout + result.stderr
