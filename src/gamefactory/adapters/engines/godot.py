"""Godot Engine adapter for discovery, inspection, and capability detection.

Performs real environment checks using safe platform-aware subprocess execution.
Core domain code does not import this adapter directly.
"""

import os
import re
import shutil
from pathlib import Path

from gamefactory.adapters.engines.base import (
    EngineAdapter,
    EngineDetectionResult,
    ProjectInspectionResult,
)
from gamefactory.core.execution.process_runner import (
    CommandRequest,
    ProcessRunner,
)


class GodotAdapter(EngineAdapter):
    """Adapter for Godot 4.x game engine."""

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self.runner = runner or ProcessRunner(sanitize_output=True)

    def find_candidate_executable(self, custom_path: str | None = None) -> str | None:
        """Locate Godot executable via custom path, environment variables, PATH, or common locations."""
        # 1. Custom path explicitly provided
        if custom_path:
            p = Path(custom_path).resolve()
            if p.is_file():
                return str(p)
            if p.is_dir():
                found = self._find_in_directory(p)
                if found:
                    return found

        # 2. Environment variable override
        env_path = os.environ.get("GAMEFACTORY_GODOT_PATH")
        if env_path:
            p = Path(env_path).resolve()
            if p.is_file():
                return str(p)
            if p.is_dir():
                found = self._find_in_directory(p)
                if found:
                    return found

        # 3. System PATH search
        for name in ("godot", "godot.exe", "godot4", "godot4.exe", "godot-console.exe"):
            found_which = shutil.which(name)
            if found_which:
                return found_which

        # 4. Conventional install locations (never machine/user-specific paths)
        common_locations = [
            Path.home() / "devel" / "godot",
            Path.home() / "godot",
            Path(r"C:\Program Files\Godot"),
        ]
        for loc in common_locations:
            if loc.is_dir():
                found = self._find_in_directory(loc)
                if found:
                    return found

        return None

    def _find_in_directory(self, directory: Path) -> str | None:
        """Search a directory and its immediate subdirectories for a Godot executable."""
        candidates = [
            f
            for pattern in ("*console.exe", "*.exe", "godot*")
            for f in directory.glob(pattern)
            if f.is_file()
        ]
        for sub in directory.iterdir():
            if sub.is_dir():
                candidates.extend(
                    f
                    for pattern in ("*console.exe", "*.exe", "godot*")
                    for f in sub.glob(pattern)
                    if f.is_file()
                )
        candidates = [path for path in candidates if not path.name.startswith(".")]
        # The Windows console stub requires the corresponding GUI executable next to it.
        paired_console = [
            path
            for path in candidates
            if path.name.lower().endswith("_console.exe")
            and path.with_name(path.name[: -len("_console.exe")] + ".exe").is_file()
        ]
        if paired_console:
            return str(paired_console[0])
        if candidates:
            return str(candidates[0])
        return None

    def detect_engine(self, custom_path: str | None = None) -> EngineDetectionResult:
        """Detect Godot on the host system and query its version."""
        exe_path: str | None
        if custom_path:
            p = Path(custom_path).resolve()
            if not p.is_file():
                return EngineDetectionResult(
                    available=False,
                    engine_type="godot",
                    executable_path=custom_path,
                    status="MISCONFIGURED",
                    details={
                        "reason": f"Configured Godot executable path does not exist: '{custom_path}'"
                    },
                )
            exe_path = str(p)
        else:
            exe_path = self.find_candidate_executable()

        if not exe_path:
            return EngineDetectionResult(
                available=False,
                engine_type="godot",
                status="UNAVAILABLE",
                details={
                    "reason": "Godot executable was not found on PATH or configured locations."
                },
            )

        # Execute safe --version check
        try:
            req = CommandRequest(
                args=[exe_path, "--version"],
                cwd=Path.cwd(),
                timeout_seconds=10.0,
            )
            res = self.runner.run(req)
            if res.exit_code == 0 and res.stdout.strip():
                version_str = res.stdout.strip().splitlines()[0]
                return EngineDetectionResult(
                    available=True,
                    engine_type="godot",
                    version=version_str,
                    executable_path=exe_path,
                    status="AVAILABLE",
                    details={"raw_version": version_str},
                )
            else:
                return EngineDetectionResult(
                    available=False,
                    engine_type="godot",
                    executable_path=exe_path,
                    status="MISCONFIGURED",
                    details={
                        "reason": "Executable failed version check",
                        "exit_code": res.exit_code,
                        "stderr": res.stderr,
                    },
                )
        except Exception as exc:
            return EngineDetectionResult(
                available=False,
                engine_type="godot",
                executable_path=exe_path,
                status="MISCONFIGURED",
                details={"reason": f"Execution error during version check: {exc}"},
            )

    def inspect_project(self, project_dir: Path | str) -> ProjectInspectionResult:
        """Inspect directory for project.godot and extract project metadata."""
        dir_path = Path(project_dir).resolve()
        project_file = dir_path / "project.godot"

        if not project_file.exists() or not project_file.is_file():
            return ProjectInspectionResult(
                is_project=False,
                engine_type="godot",
                details={"reason": "No project.godot found in directory."},
            )

        content = project_file.read_text(encoding="utf-8", errors="replace")

        project_name = None
        main_scene = None
        features: list[str] = []

        # Parse config/name
        name_match = re.search(r'config/name\s*=\s*"([^"]+)"', content)
        if name_match:
            project_name = name_match.group(1)

        # Parse run/main_scene
        scene_match = re.search(r'run/main_scene\s*=\s*"([^"]+)"', content)
        if scene_match:
            main_scene = scene_match.group(1)

        # Parse config/features
        features_match = re.search(r"config/features\s*=\s*PackedStringArray\(([^)]+)\)", content)
        if features_match:
            raw_features = features_match.group(1)
            features = [f.strip(' "') for f in raw_features.split(",") if f.strip(' "')]

        return ProjectInspectionResult(
            is_project=True,
            engine_type="godot",
            project_name=project_name or dir_path.name,
            project_file_path=str(project_file),
            features=features,
            main_scene=main_scene,
            details={"config_length_bytes": len(content)},
        )
