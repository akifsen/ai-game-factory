"""Integration tests for real and degraded Godot and Blender detection."""

from pathlib import Path

from gamefactory.adapters.dcc.blender import BlenderAdapter
from gamefactory.adapters.engines.godot import GodotAdapter


class TestGodotAndBlenderDetection:
    def test_real_blender_detection_or_graceful_degradation(self) -> None:
        adapter = BlenderAdapter()
        res = adapter.detect_tool()
        # In this Windows environment, Blender is installed on PATH
        assert res.tool_name == "blender"
        if res.available:
            assert res.status == "AVAILABLE"
            assert res.version is not None
            assert "Blender" in res.version
            assert res.executable_path is not None
        else:
            assert res.status == "UNAVAILABLE"
            assert "reason" in res.details

    def test_missing_blender_path_degrades_gracefully(self) -> None:
        adapter = BlenderAdapter()
        res = adapter.detect_tool(custom_path="C:/non_existent_blender_path/blender.exe")
        assert res.available is False
        assert res.status == "MISCONFIGURED"
        assert "does not exist" in res.details.get("reason", "")

    def test_real_godot_detection_or_graceful_degradation(self) -> None:
        adapter = GodotAdapter()
        res = adapter.detect_engine()
        assert res.engine_type == "godot"
        if res.available:
            assert res.status == "AVAILABLE"
            assert res.version is not None
            assert "4." in res.version or "3." in res.version
            assert res.executable_path is not None
        else:
            assert res.status in ("UNAVAILABLE", "MISCONFIGURED")

    def test_missing_godot_path_degrades_gracefully(self) -> None:
        adapter = GodotAdapter()
        res = adapter.detect_engine(custom_path="C:/non_existent_godot/godot.exe")
        assert res.available is False
        assert res.status == "MISCONFIGURED"
        assert "does not exist" in res.details.get("reason", "")

    def test_inspect_godot_project_file(self, tmp_path: Path) -> None:
        project_file = tmp_path / "project.godot"
        project_file.write_text(
            """
; Engine configuration file.
[application]
config/name="Test Iron Bastion"
run/main_scene="res://main.tscn"
config/features=PackedStringArray("4.3", "GL Compatibility")
            """.strip(),
            encoding="utf-8",
        )

        adapter = GodotAdapter()
        inspection = adapter.inspect_project(tmp_path)
        assert inspection.is_project is True
        assert inspection.project_name == "Test Iron Bastion"
        assert inspection.main_scene == "res://main.tscn"
        assert "4.3" in inspection.features

    def test_inspect_empty_directory_not_godot_project(self, tmp_path: Path) -> None:
        adapter = GodotAdapter()
        inspection = adapter.inspect_project(tmp_path)
        assert inspection.is_project is False
