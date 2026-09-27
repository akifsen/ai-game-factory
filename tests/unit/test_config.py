"""Unit tests for configuration schema, validation, and idempotent initialization."""

import os
import subprocess
from pathlib import Path

import pytest

from gamefactory.config.loader import ConfigLoader
from gamefactory.core.domain.errors import ConfigurationError, FactoryError


class TestConfigLoader:
    def test_init_project_idempotent_and_non_destructive(self, tmp_path: Path) -> None:
        # Create an existing game file
        user_script = tmp_path / "player.gd"
        user_script.write_text("extends CharacterBody2D", encoding="utf-8")

        # 1. First init
        root, cfg, existed_first = ConfigLoader.init_project(tmp_path, project_name="My Game")
        assert root == tmp_path
        assert cfg.project.name == "My Game"
        assert existed_first is False
        assert (tmp_path / ".gamefactory" / "factory.yml").exists()
        assert (tmp_path / ".gamefactory" / "state" / "factory.db").exists()
        assert user_script.read_text(encoding="utf-8") == "extends CharacterBody2D"

        # 2. Second init (idempotent)
        _, cfg2, existed_second = ConfigLoader.init_project(tmp_path)
        assert existed_second is True
        assert cfg2.project.name == "My Game"
        assert user_script.read_text(encoding="utf-8") == "extends CharacterBody2D"

    def test_unknown_field_rejected(self, tmp_path: Path) -> None:
        factory_dir = tmp_path / ".gamefactory"
        factory_dir.mkdir()
        config_file = factory_dir / "factory.yml"
        config_file.write_text(
            """
schema_version: "0.1.0"
project:
  id: "test"
  name: "Test"
unknown_unsupported_field: "dangerous_injection"
            """.strip(),
            encoding="utf-8",
        )

        with pytest.raises(ConfigurationError, match="Extra inputs are not permitted"):
            ConfigLoader.load_config(tmp_path)

    def test_unsupported_schema_version_rejected(self, tmp_path: Path) -> None:
        factory_dir = tmp_path / ".gamefactory"
        factory_dir.mkdir()
        config_file = factory_dir / "factory.yml"
        config_file.write_text(
            """
schema_version: "99.0.0"
project:
  id: "test"
  name: "Test"
            """.strip(),
            encoding="utf-8",
        )

        with pytest.raises(ConfigurationError, match="Unsupported schema version"):
            ConfigLoader.load_config(tmp_path)

    @pytest.mark.parametrize("budget", ["-1", ".nan", ".inf"])
    def test_non_finite_or_negative_budgets_rejected(self, tmp_path: Path, budget: str) -> None:
        factory_dir = tmp_path / ".gamefactory"
        factory_dir.mkdir()
        (factory_dir / "factory.yml").write_text(
            f"schema_version: '0.1.0'\nproject: {{id: test, name: Test}}\npolicies: {{project_budget: {budget}}}\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigurationError):
            ConfigLoader.load_config(tmp_path)

    def test_secret_field_and_missing_schema_version_rejected(self, tmp_path: Path) -> None:
        factory_dir = tmp_path / ".gamefactory"
        factory_dir.mkdir()
        config = factory_dir / "factory.yml"
        config.write_text("project: {id: test, name: Test}\n", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="schema_version"):
            ConfigLoader.load_config(tmp_path)
        config.write_text(
            "schema_version: '0.1.0'\nproject: {id: test, name: Test}\napi_key: do-not-echo-this-value\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigurationError) as exc_info:
            ConfigLoader.load_config(tmp_path)
        assert "do-not-echo-this-value" not in str(exc_info.value)

    def test_user_defaults_are_overridden_by_project_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        appdata = tmp_path / "appdata"
        user_file = appdata / "gamefactory" / "config.yml"
        user_file.parent.mkdir(parents=True)
        user_file.write_text("engine:\n  executable_path: C:/user/godot.exe\n", encoding="utf-8")
        monkeypatch.setenv("APPDATA", str(appdata))
        project = tmp_path / "project"
        project_factory = project / ".gamefactory"
        project_factory.mkdir(parents=True)
        (project_factory / "factory.yml").write_text(
            "schema_version: '0.1.0'\nproject: {id: game, name: Game}\nengine:\n  executable_path: C:/project/godot.exe\n",
            encoding="utf-8",
        )
        assert ConfigLoader.load_config(project).engine.executable_path == "C:/project/godot.exe"

    def test_unicode_project_name_generates_valid_ascii_id(self, tmp_path: Path) -> None:
        _, config, _ = ConfigLoader.init_project(tmp_path, project_name="İğne Dünyası")
        assert config.project.name == "İğne Dünyası"
        assert config.project.id

    def test_malformed_yaml_rejected(self, tmp_path: Path) -> None:
        factory_dir = tmp_path / ".gamefactory"
        factory_dir.mkdir()
        config_file = factory_dir / "factory.yml"
        config_file.write_text("::: not valid yaml ::: [", encoding="utf-8")

        with pytest.raises(ConfigurationError, match="Malformed YAML"):
            ConfigLoader.load_config(tmp_path)

    @pytest.mark.skipif(os.name != "nt", reason="Windows junction validation")
    def test_init_rejects_factory_junction_escape_without_touching_target(
        self, tmp_path: Path
    ) -> None:
        project = tmp_path / "project"
        outside = tmp_path / "user-owned"
        project.mkdir()
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("user data", encoding="utf-8")
        link = project / ".gamefactory"
        _make_junction(link, outside)
        try:
            with pytest.raises(FactoryError):
                ConfigLoader.init_project(project)
            assert sentinel.read_text(encoding="utf-8") == "user data"
            assert not (outside / "factory.yml").exists()
        finally:
            os.rmdir(link)

    @pytest.mark.skipif(os.name != "nt", reason="Windows junction validation")
    def test_init_rejects_state_junction_escape_without_touching_target(
        self, tmp_path: Path
    ) -> None:
        project = tmp_path / "project"
        outside = tmp_path / "state-target"
        (project / ".gamefactory").mkdir(parents=True)
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("user data", encoding="utf-8")
        link = project / ".gamefactory" / "state"
        _make_junction(link, outside)
        try:
            with pytest.raises(FactoryError):
                ConfigLoader.init_project(project)
            assert sentinel.read_text(encoding="utf-8") == "user data"
            assert not (outside / "factory.db").exists()
        finally:
            os.rmdir(link)


def _make_junction(link: Path, target: Path) -> None:
    def quote(path: Path) -> str:
        return "'" + str(path).replace("'", "''") + "'"

    command = f"New-Item -ItemType Junction -Path {quote(link)} -Target {quote(target)} | Out-Null"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
