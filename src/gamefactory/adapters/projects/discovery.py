"""Bounded, read-only discovery for ordinary Godot project directories."""

from __future__ import annotations

import os
import re
import stat as stat_module
from pathlib import Path
from typing import Any

from gamefactory.adapters.engines.godot_staging import _is_reparse
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner

_MAX_FILES = 20000
_MAX_BYTES = 1024 * 1024 * 1024
_PLATFORM_HINTS = {
    "windows": (".exe", "windows"),
    "linux": ("linux", "x11", "wayland"),
    "macos": ("macos", "osx"),
    "web": ("web", "html5"),
    "android": ("android",),
    "ios": ("ios",),
}


def _read_limited(path: Path, limit: int) -> str:
    if _is_reparse(path):
        raise ValidationError(f"Project metadata cannot be a link: {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            raise ValidationError("Project metadata must be a regular file")
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise ValidationError(f"Project metadata exceeds {limit} bytes")
        return data.decode("utf-8", "replace")
    finally:
        os.close(fd)


def _git(root: Path, executable: str, runner: Any, timeout: float = 4.0) -> dict[str, Any]:
    try:
        branch = runner.run(
            CommandRequest(
                [executable, "-C", str(root), "branch", "--show-current"],
                root,
                timeout_seconds=timeout,
            )
        )
        status = runner.run(
            CommandRequest(
                [
                    executable,
                    "-C",
                    str(root),
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=all",
                ],
                root,
                timeout_seconds=timeout,
            )
        )
        if branch.exit_code or status.exit_code or branch.timed_out or status.timed_out:
            return {"available": False}
        entries = [item for item in status.stdout.split("\0") if item]
        truncated = status.stdout_truncated or len(entries) > 5000
        return {
            "available": True,
            "branch": branch.stdout.strip(),
            "dirty": bool(entries) or truncated,
            "entries": entries[:5000],
            "truncated": truncated,
        }
    except Exception:
        return {"available": False}


def discover_project(
    root: Path | str,
    *,
    include_git: bool = True,
    git_executable: str | None = None,
    runner: Any | None = None,
) -> dict[str, Any]:
    """Inspect a project without running its code, editor, or importer."""
    original = Path(root)
    current = original.absolute()
    while current != current.parent:
        if _is_reparse(current):
            raise ValidationError("Project path cannot contain a symbolic link or junction")
        current = current.parent
    project = original.resolve(strict=True)
    if not project.is_dir():
        raise ValidationError("Project root must be a directory")
    files: list[dict[str, Any]] = []
    total = 0
    scenes: list[str] = []
    scripts: list[str] = []
    assets: list[str] = []
    tests: list[str] = []
    exports: list[str] = []
    hints: set[str] = set()
    analysis_truncated = False
    cfg = project / "project.godot"
    if _is_reparse(cfg):
        raise ValidationError("project.godot cannot be a symbolic link or junction")
    for walk_root, dirs, names in os.walk(project, topdown=True, followlinks=False):
        base = Path(walk_root)
        dirs[:] = sorted(
            d
            for d in dirs
            if d.lower()
            not in {".git", ".godot", ".gamefactory", ".venv", "node_modules", "__pycache__"}
            and not _is_reparse(base / d)
        )
        for name in sorted(names):
            path = base / name
            if _is_reparse(path):
                continue
            try:
                stat = path.stat()
                if not path.is_file():
                    continue
                rel = path.relative_to(project).as_posix()
                total += stat.st_size
                if len(files) >= _MAX_FILES or total > _MAX_BYTES:
                    raise ValidationError("Project discovery limit exceeded")
            except OSError:
                continue
            files.append({"path": rel, "size": stat.st_size, "extension": path.suffix.lower()})
            ext = path.suffix.lower()
            if ext == ".tscn":
                scenes.append(rel)
            elif ext == ".gd":
                scripts.append(rel)
            elif ext in {
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
                ".svg",
                ".wav",
                ".ogg",
                ".mp3",
                ".glb",
                ".gltf",
                ".obj",
                ".fbx",
                ".ttf",
                ".otf",
            }:
                assets.append(rel)
            if re.search(r"(^|/)(test|tests|testing)(/|$)|(^|/)test_[^/]+$", rel, re.I):
                tests.append(rel)
            if name == "export_presets.cfg":
                exports.append(rel)
    if not cfg.is_file():
        raise ValidationError("Selected directory is not a Godot project (project.godot missing)")
    cfg_text = _read_limited(cfg, 1024 * 1024).lower()
    dimension_types: set[str] = set()
    for rel in scenes:
        scene_file = project / Path(rel)
        try:
            sample = _read_limited(scene_file, 64 * 1024)
        except (OSError, ValidationError):
            analysis_truncated = True
            continue
        dimension_types.update(
            "2d" for t in ("Node2D", "CanvasItem", "Control") if f'type="{t}"' in sample
        )
        dimension_types.update(
            "3d" for t in ("Node3D", "Node3D", "MeshInstance3D") if f'type="{t}"' in sample
        )
    for rel in scripts:
        script_file = project / Path(rel)
        try:
            sample = _read_limited(script_file, 64 * 1024)
            if re.search(r"(?m)^extends\s+(Node2D|Control|CanvasItem)\b", sample):
                dimension_types.add("2d")
            if re.search(r"(?m)^extends\s+Node3D\b", sample):
                dimension_types.add("3d")
        except (OSError, ValidationError):
            analysis_truncated = True
            pass
    dimension = (
        next(iter(dimension_types))
        if len(dimension_types) == 1
        else ("mixed" if len(dimension_types) > 1 else "unknown")
    )
    version_match = re.search(r'config/features\s*=\s*PackedStringArray\("(\d+\.\d+)"', cfg_text)
    preset_path = project / "export_presets.cfg"
    if _is_reparse(preset_path):
        raise ValidationError("export_presets.cfg cannot be a symbolic link or junction")
    if preset_path.is_file():
        content = _read_limited(preset_path, 1024 * 1024)
        for platform in ("Windows Desktop", "Linux/X11", "macOS", "Web", "Android", "iOS"):
            if re.search(rf'(?m)^platform\s*=\s*"{re.escape(platform)}"', content):
                hints.add(platform)
    result = {
        "schema_version": 1,
        "root": str(project),
        "godot_project": True,
        "godot_version_hint": version_match.group(1) if version_match else None,
        "dimension_hint": dimension,
        "analysis_truncated": analysis_truncated,
        "files": files,
        "scenes": scenes,
        "scripts": scripts,
        "assets": assets,
        "tests": tests,
        "export_presets": exports,
        "platform_hints": sorted(hints),
        "counts": {"files": len(files), "bytes": total},
    }
    if include_git:
        result["git"] = _git(
            project,
            git_executable or ("git.exe" if os.name == "nt" else "git"),
            runner or ProcessRunner(sanitize_output=True),
        )
    return result
