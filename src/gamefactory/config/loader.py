"""Project discovery and layered configuration loader for AI Game Factory.

Finds project roots, validates strictly versioned factory.yml, and initializes
.gamefactory/ configuration idempotently without modifying user game files.
"""

import hashlib
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError as PydanticValidationError

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import ProjectRepository
from gamefactory.config.schema import (
    DccConfig,
    EngineConfig,
    PolicyConfig,
    ProjectConfig,
    UserConfig,
)
from gamefactory.core.domain.errors import ConfigurationError
from gamefactory.core.domain.models import Project
from gamefactory.core.execution.path_guard import PathGuard, assert_managed_directory

SUPPORTED_SCHEMA_VERSIONS = {"0.1.0"}


class ConfigLoader:
    """Discovers project root, parses factory.yml, and manages project initialization."""

    @staticmethod
    def find_project_root(start_dir: Path | str | None = None) -> Path | None:
        """Search current directory and parents for .gamefactory/factory.yml or project.godot."""
        curr = Path(start_dir or Path.cwd()).resolve()

        # Check up directory tree
        for directory in [curr, *curr.parents]:
            if (directory / ".gamefactory" / "factory.yml").is_file():
                return directory
            if (directory / "project.godot").is_file():
                return directory

        return None

    @staticmethod
    def load_config(project_root: Path | str) -> ProjectConfig:
        """Load and validate .gamefactory/factory.yml from project root."""
        root = Path(project_root).resolve()
        config_file = PathGuard(root).resolve_safe_path(".gamefactory/factory.yml")

        if not config_file.exists():
            raise ConfigurationError(
                f"Configuration file not found: '{config_file}'. Run 'gamefactory init' first.",
                details={"config_file": str(config_file)},
            )

        try:
            content = config_file.read_text(encoding="utf-8")
            raw_data = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            location = (
                f"line {mark.line + 1}, column {mark.column + 1}" if mark else "unknown location"
            )
            raise ConfigurationError(
                f"Malformed YAML in '{config_file}' ({location}). Check the file syntax.",
                details={"config_file": str(config_file), "location": location},
            ) from exc

        if not isinstance(raw_data, dict):
            raise ConfigurationError(
                f"Invalid configuration format in '{config_file}': expected a dictionary/map",
                details={"config_file": str(config_file)},
            )

        if "schema_version" not in raw_data:
            raise ConfigurationError(
                f"Missing required schema_version in '{config_file}'. Add schema_version: '0.1.0'.",
                details={"config_file": str(config_file), "field": "schema_version"},
            )
        if raw_data.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
            value = raw_data.get("schema_version")
            raise ConfigurationError(
                f"Unsupported schema version '{value}' in '{config_file}'. Supported versions: {sorted(SUPPORTED_SCHEMA_VERSIONS)}",
                details={"schema_version": value, "supported": sorted(SUPPORTED_SCHEMA_VERSIONS)},
            )

        try:
            user_raw = ConfigLoader._load_user_config()
            merged: dict[str, Any] = {
                "engine": EngineConfig().model_dump(),
                "dcc": DccConfig().model_dump(),
                "policies": PolicyConfig().model_dump(),
            }
            for layer in (user_raw, raw_data):
                for key, value in layer.items():
                    if isinstance(value, dict) and isinstance(merged.get(key), dict):
                        merged[key] = {**merged[key], **value}
                    else:
                        merged[key] = value
            cfg = ProjectConfig.model_validate(merged)
        except PydanticValidationError as exc:
            field_errors: list[dict[str, Any]] = []
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"])
                field_errors.append({"field": loc, "message": err["msg"], "type": err["type"]})
            raise ConfigurationError(
                f"Invalid configuration in '{config_file}': {field_errors[0]['message']} at field '{field_errors[0]['field']}'",
                details={"config_file": str(config_file), "errors": field_errors},
            ) from exc

        if cfg.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ConfigurationError(
                f"Unsupported schema version '{cfg.schema_version}' in '{config_file}'. Supported versions: {sorted(SUPPORTED_SCHEMA_VERSIONS)}",
                details={
                    "schema_version": cfg.schema_version,
                    "supported": list(SUPPORTED_SCHEMA_VERSIONS),
                },
            )

        return cfg

    @staticmethod
    def user_config_path() -> Path:
        """Return the per-user configuration file path without creating it."""
        if os.environ.get("APPDATA"):
            base = Path(os.environ["APPDATA"])
        elif os.environ.get("XDG_CONFIG_HOME"):
            base = Path(os.environ["XDG_CONFIG_HOME"])
        else:
            base = Path.home() / ".config"
        return base / "gamefactory" / "config.yml"

    @staticmethod
    def _load_user_config() -> dict[str, Any]:
        path = ConfigLoader.user_config_path()
        if not path.is_file():
            return {}
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            location = (
                f"line {mark.line + 1}, column {mark.column + 1}" if mark else "unknown location"
            )
            raise ConfigurationError(
                f"Malformed YAML in user config '{path}' ({location})."
            ) from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ConfigurationError(f"Invalid user configuration in '{path}': expected a map.")
        try:
            validated = UserConfig.model_validate(raw)
        except PydanticValidationError as exc:
            first = exc.errors()[0]
            field = ".".join(str(part) for part in first["loc"])
            raise ConfigurationError(
                f"Invalid user configuration at field '{field}': {first['msg']}"
            ) from exc
        return validated.model_dump(exclude_unset=True)

    @classmethod
    def init_project(
        cls,
        target_dir: Path | str,
        project_name: str | None = None,
        engine_type: str = "godot",
        custom_godot_path: str | None = None,
        custom_blender_path: str | None = None,
    ) -> tuple[Path, ProjectConfig, bool]:
        """Initialize .gamefactory/ in target_dir idempotently and non-destructively.

        Returns (project_root, ProjectConfig, already_existed).
        """
        root = Path(target_dir).resolve()
        factory_dir = root / ".gamefactory"
        config_file = factory_dir / "factory.yml"
        state_dir = factory_dir / "state"
        locks_dir = factory_dir / "locks"

        root.mkdir(parents=True, exist_ok=True)
        factory_dir = assert_managed_directory(root, ".gamefactory")
        state_dir = assert_managed_directory(root, ".gamefactory/state")
        locks_dir = assert_managed_directory(root, ".gamefactory/locks")
        factory_dir.mkdir(exist_ok=True)
        state_dir.mkdir(exist_ok=True)
        locks_dir.mkdir(exist_ok=True)
        config_file = PathGuard(root).resolve_safe_path(".gamefactory/factory.yml")

        already_existed = config_file.exists()

        if already_existed:
            cfg = cls.load_config(root)
        else:
            name = project_name or root.name
            normalized_name = (
                unicodedata.normalize("NFKD", name)
                .encode("ascii", "ignore")
                .decode("ascii")
                .lower()
            )
            proj_id = re.sub(r"[^a-z0-9._-]+", "-", normalized_name).strip("-._")
            if not proj_id:
                proj_id = f"game-{hashlib.sha256(name.encode('utf-8')).hexdigest()[:8]}"
            if not proj_id[0].isalnum():
                proj_id = f"game-{proj_id}"
            proj_id = proj_id[:80]
            # Exclusive creation prevents a concurrent init from overwriting user data.
            project_contract: dict[str, Any] = {
                "schema_version": "0.1.0",
                "project": {"id": proj_id, "name": name, "version": "0.1.0"},
            }
            engine_contract: dict[str, Any] = {"type": engine_type}
            if custom_godot_path is not None:
                engine_contract["executable_path"] = custom_godot_path
            if engine_contract != {"type": "godot"}:
                project_contract["engine"] = engine_contract
            if custom_blender_path is not None:
                project_contract["dcc"] = {"blender_path": custom_blender_path}
            yaml_content = yaml.safe_dump(project_contract, sort_keys=False, allow_unicode=True)
            try:
                with config_file.open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(yaml_content)
            except FileExistsError:
                already_existed = True
            cfg = cls.load_config(root)

        # Initialize SQLite database and run migrations
        db_path = PathGuard(root).resolve_safe_path(".gamefactory/state/factory.db")
        db = Database(db_path)
        MigrationRunner(db).apply_all()

        # Ensure project entity is stored in database
        proj_repo = ProjectRepository(db)
        proj_repo.save(
            Project(
                id=cfg.project.id,
                name=cfg.project.name,
                engine_type=cfg.engine.type,
                root_path=str(root),
            )
        )

        return root, cfg, already_existed
