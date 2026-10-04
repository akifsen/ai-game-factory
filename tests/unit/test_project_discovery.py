from __future__ import annotations

from pathlib import Path

import pytest

from gamefactory.adapters.projects.discovery import discover_project
from gamefactory.adapters.projects.onboarding import create_godot_project
from gamefactory.core.domain.errors import ValidationError


def test_discovery_reports_scenes_scripts_assets_and_dimension(tmp_path: Path) -> None:
    root = tmp_path / "game"
    create_godot_project(root, "Game", "3d")
    (root / "assets").mkdir()
    (root / "assets" / "icon.png").write_bytes(b"png")
    (root / "tests").mkdir()
    (root / "tests" / "test_game.gd").write_text("extends Node\n")
    (root / "export_presets.cfg").write_text('[preset.0]\nname="Web Build"\nplatform="Web"\n')
    result = discover_project(root, include_git=False)
    assert result["dimension_hint"] == "3d"
    assert "main.tscn" in result["scenes"] and "main.gd" in result["scripts"]
    assert "assets/icon.png" in result["assets"] and "tests/test_game.gd" in result["tests"]
    assert "Web" in result["platform_hints"]


def test_discovery_rejects_non_project(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        discover_project(tmp_path, include_git=False)
