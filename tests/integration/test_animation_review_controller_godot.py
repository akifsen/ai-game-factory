"""Cheap Godot validation for V087a animation review controller (no C2B / Blender)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest

GODOT = os.environ.get(
    "GAMEFACTORY_TEST_GODOT",
    r"C:\Users\lenovo\devel\godot\Godot_v4.7.2-stable_win64.exe\Godot_v4.7.2-stable_win64_console.exe",
)

_REVIEW_FILES = (
    "animation_review.tscn",
    "animation_review_controller.gd",
    "animation_review_inspect.gd",
)

_V08_PACKAGE = resources.files("gamefactory.resources.v08_candidate")

_BASE_PATH_FILE = (
    Path(__file__).resolve().parents[2] / ".verification" / "v087-base-package-path.txt"
)


def _configured_clip_preview_root() -> str:
    from_env = os.environ.get("GAMEFACTORY_ANIMATION_CLIP_PREVIEW_PACKAGE", "").strip()
    if from_env:
        return from_env
    if _BASE_PATH_FILE.is_file():
        return _BASE_PATH_FILE.read_text(encoding="utf-8-sig").strip()
    return ""


CLIP_PREVIEW_ROOT = _configured_clip_preview_root()

_REQUIRED_PACKAGE_FILES = (
    "project.godot",
    "animation_clip_preview.tscn",
    "animation_clip_preview_player.gd",
    "animation_clip.json",
    "animation_clip_manifest.json",
)


def _stage_review_scripts(package_dir: Path) -> None:
    for name in _REVIEW_FILES:
        (package_dir / name).write_bytes(_V08_PACKAGE.joinpath(name).read_bytes())


def _strip_autoload_main_scene(project_godot: Path) -> None:
    text = project_godot.read_text(encoding="utf-8")
    stripped = text.replace('run/main_scene="res://animation_clip_preview.tscn"\n', "")
    if stripped != text:
        project_godot.write_text(stripped, encoding="utf-8", newline="\n")


def _require_clip_preview_package() -> Path:
    if not CLIP_PREVIEW_ROOT:
        pytest.skip(
            "Set GAMEFACTORY_ANIMATION_CLIP_PREVIEW_PACKAGE or provide "
            ".verification/v087-base-package-path.txt for clip preview integration"
        )
    root = Path(CLIP_PREVIEW_ROOT)
    if not root.is_dir():
        pytest.fail(
            f"GAMEFACTORY_ANIMATION_CLIP_PREVIEW_PACKAGE is not a directory: {CLIP_PREVIEW_ROOT}"
        )
    missing = [name for name in _REQUIRED_PACKAGE_FILES if not (root / name).is_file()]
    if missing:
        pytest.fail(f"clip preview package missing: {', '.join(missing)}")
    return root


def _assert_no_script_errors(combined_output: str) -> None:
    if "SCRIPT ERROR" in combined_output or "ParseError" in combined_output:
        pytest.fail(combined_output[-2500:])
    for line in combined_output.splitlines():
        if re.search(r"\bERROR:\s", line):
            pytest.fail(combined_output[-2500:])


def _godot_import(staged: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(GODOT), "--headless", "--path", str(staged), "--import", "--quit"],
        cwd=staged,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.mark.skipif(not Path(GODOT).is_file(), reason="Godot executable not available")
def test_animation_review_scripts_check_only(tmp_path: Path) -> None:
    package = _require_clip_preview_package()
    staged = tmp_path / "check"
    shutil.copytree(package, staged)
    _stage_review_scripts(staged)
    import_result = _godot_import(staged)
    import_combined = import_result.stdout + import_result.stderr
    assert import_result.returncode == 0, import_combined[-1500:]
    _assert_no_script_errors(import_combined)
    for script in ("animation_review_controller.gd", "animation_review_inspect.gd"):
        completed = subprocess.run(
            [
                str(GODOT),
                "--headless",
                "--path",
                str(staged),
                "--script",
                f"res://{script}",
                "--check-only",
            ],
            cwd=staged,
            capture_output=True,
            text=True,
            timeout=180,
        )
        combined = completed.stdout + completed.stderr
        assert completed.returncode == 0, combined[-2000:]
        _assert_no_script_errors(combined)


@pytest.mark.skipif(not Path(GODOT).is_file(), reason="Godot executable not available")
def test_animation_review_scene_launch_quits(tmp_path: Path) -> None:
    package = _require_clip_preview_package()
    staged = tmp_path / "scene"
    shutil.copytree(package, staged)
    _stage_review_scripts(staged)
    import_result = _godot_import(staged)
    import_combined = import_result.stdout + import_result.stderr
    assert import_result.returncode == 0, import_combined[-1500:]
    _assert_no_script_errors(import_combined)
    completed = subprocess.run(
        [
            str(GODOT),
            "--rendering-method",
            "gl_compatibility",
            "--audio-driver",
            "Dummy",
            "--path",
            str(staged),
            "--scene",
            "res://animation_review.tscn",
            "--quit-after",
            "48",
        ],
        cwd=staged,
        capture_output=True,
        text=True,
        timeout=180,
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode == 0, combined[-2000:]
    _assert_no_script_errors(combined)


@pytest.mark.skipif(not Path(GODOT).is_file(), reason="Godot executable not available")
def test_animation_review_controller_numerics_inspect(tmp_path: Path) -> None:
    package = _require_clip_preview_package()
    staged = tmp_path / "numerics"
    shutil.copytree(package, staged)
    _stage_review_scripts(staged)
    _strip_autoload_main_scene(staged / "project.godot")
    import_result = _godot_import(staged)
    import_combined = import_result.stdout + import_result.stderr
    assert import_result.returncode == 0, import_combined[-1500:]
    _assert_no_script_errors(import_combined)
    numerics = subprocess.run(
        [
            str(GODOT),
            "--rendering-method",
            "gl_compatibility",
            "--audio-driver",
            "Dummy",
            "--path",
            str(staged),
            "--script",
            "res://animation_review_inspect.gd",
            "--",
            "--gf-controller-numerics-only",
        ],
        cwd=staged,
        capture_output=True,
        text=True,
        timeout=180,
    )
    numerics_combined = numerics.stdout + numerics.stderr
    assert numerics.returncode == 0, numerics_combined[-2500:]
    _assert_no_script_errors(numerics_combined)
    assert "PASS: animation_review_controller_numerics" in numerics.stdout

    screenshot = tmp_path / "animation_review_interactive.png"
    interactive = subprocess.run(
        [
            str(GODOT),
            "--rendering-method",
            "gl_compatibility",
            "--audio-driver",
            "Dummy",
            "--path",
            str(staged),
            "--script",
            "res://animation_review_inspect.gd",
            "--",
            "--gf-screenshot=" + str(screenshot),
        ],
        cwd=staged,
        capture_output=True,
        text=True,
        timeout=240,
    )
    interactive_combined = interactive.stdout + interactive.stderr
    assert interactive.returncode == 0, interactive_combined[-4000:]
    _assert_no_script_errors(interactive_combined)
    assert "PASS: animation_review_interactive" in interactive.stdout
    assert "GF_REVIEW_CAMERA_ORACLE" in interactive_combined
    assert screenshot.is_file() and screenshot.stat().st_size > 0
