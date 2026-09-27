"""Regression tests for Python interpreter resolution in Godot verification scripts.

Verifies that verify_godot_recovery.py and verify_godot_live_crash.py preserve
supplied virtual environment symlink interpreter paths without dereferencing,
ensuring sub-process invocations retain virtual environment package resolution.
"""

from __future__ import annotations

import subprocess
import sys
import venv
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.verify_godot_live_crash import _resolve_python as live_resolve_python  # noqa: E402
from scripts.verify_godot_recovery import _resolve_python as recovery_resolve_python  # noqa: E402

RESOLVERS = [
    pytest.param(recovery_resolve_python, id="recovery"),
    pytest.param(live_resolve_python, id="live_crash"),
]


@pytest.mark.parametrize("resolve_fn", RESOLVERS)
def test_valid_interpreter_path_resolved_to_absolute(
    resolve_fn: Callable[[Path | str], Path],
) -> None:
    """Valid interpreter paths must be normalized to absolute paths."""
    current_exe = Path(sys.executable)
    result = resolve_fn(current_exe)
    assert result == current_exe.absolute()
    assert result.is_file()


@pytest.mark.parametrize("resolve_fn", RESOLVERS)
def test_relative_interpreter_path_resolved_to_absolute(
    resolve_fn: Callable[[Path | str], Path],
) -> None:
    """Relative interpreter path must be converted to absolute without dereference."""
    current_exe = Path(sys.executable)
    try:
        rel_path = current_exe.relative_to(Path.cwd())
    except ValueError:
        # If sys.executable is on a different drive or outside cwd
        rel_path = current_exe

    result = resolve_fn(rel_path)
    assert result == current_exe.absolute()
    assert result.is_absolute()


@pytest.mark.parametrize("resolve_fn", RESOLVERS)
def test_nonexistent_python_path_rejected(
    resolve_fn: Callable[[Path | str], Path], tmp_path: Path
) -> None:
    """Non-existent Python paths must raise FileNotFoundError."""
    missing = tmp_path / "nonexistent" / "bin" / "python"
    with pytest.raises(FileNotFoundError):
        resolve_fn(missing)


@pytest.mark.parametrize("resolve_fn", RESOLVERS)
def test_directory_python_path_rejected(
    resolve_fn: Callable[[Path | str], Path], tmp_path: Path
) -> None:
    """Directory paths passed as Python interpreter must raise FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="not a regular file"):
        resolve_fn(tmp_path)


@pytest.mark.parametrize("resolve_fn", RESOLVERS)
def test_symlink_interpreter_path_preserved_filesystem(
    resolve_fn: Callable[[Path | str], Path], tmp_path: Path
) -> None:
    """If symlink creation is supported, symlinked interpreter path must be preserved."""
    target = Path(sys.executable)
    symlink_path = tmp_path / (
        "python_symlink.exe" if sys.platform == "win32" else "python_symlink"
    )

    try:
        symlink_path.symlink_to(target)
    except OSError as exc:
        if sys.platform == "win32" and getattr(exc, "winerror", None) == 1314:
            pytest.skip(f"Windows symlink privilege unavailable: {exc}")
        raise

    resolved = resolve_fn(symlink_path)
    assert resolved == symlink_path.absolute()
    # The returned path must be the symlink itself, not the resolved target
    assert resolved != target.resolve()
    assert resolved.resolve() == target.resolve()


@pytest.mark.parametrize("resolve_fn", RESOLVERS)
def test_broken_symlink_rejected(resolve_fn: Callable[[Path | str], Path], tmp_path: Path) -> None:
    """Dangling symlinks must be rejected with FileNotFoundError."""
    dangling = tmp_path / "broken_symlink"
    non_existent_target = tmp_path / "does_not_exist_target"

    try:
        dangling.symlink_to(non_existent_target)
    except OSError as exc:
        if sys.platform == "win32" and getattr(exc, "winerror", None) == 1314:
            pytest.skip(f"Windows symlink privilege unavailable: {exc}")
        raise

    with pytest.raises(FileNotFoundError):
        resolve_fn(dangling)


@pytest.mark.parametrize("resolve_fn", RESOLVERS)
def test_symlink_preservation_synthetic_mock(
    resolve_fn: Callable[[Path | str], Path], tmp_path: Path
) -> None:
    """Synthetic test proving that resolve(strict=True) target is NOT returned when symlinked."""
    fake_venv_python = tmp_path / "fake_venv" / "bin" / "python"
    fake_venv_python.parent.mkdir(parents=True)
    fake_venv_python.write_text("#!/bin/sh\n", encoding="utf-8")

    real_base_python = Path("/opt/hostedtoolcache/Python/3.12.14/x64/bin/python3.12")

    # When resolve(strict=True) is called, simulate symlink dereferencing to real base python
    with patch.object(Path, "resolve", return_value=real_base_python):
        result = resolve_fn(fake_venv_python)
        # Result must be the venv python path, not the resolved base python
        assert result == fake_venv_python.absolute()
        assert str(result) != str(real_base_python)


@pytest.mark.parametrize("resolve_fn", RESOLVERS)
def test_real_venv_regression_without_downloads(
    resolve_fn: Callable[[Path | str], Path], tmp_path: Path
) -> None:
    """Real virtual environment created locally without downloads must preserve venv prefix."""
    venv_dir = tmp_path / "local_test_venv"
    venv.EnvBuilder(with_pip=False, symlinks=sys.platform != "win32").create(venv_dir)

    if sys.platform == "win32":
        venv_python = venv_dir / "Scripts" / "python.exe"
    else:
        venv_python = venv_dir / "bin" / "python"

    assert venv_python.is_file(), f"Venv python missing at {venv_python}"

    resolved = resolve_fn(venv_python)
    assert resolved == venv_python.absolute()

    if sys.platform != "win32":
        assert venv_python.is_symlink(), "POSIX regression requires a symlink interpreter"
        # On Linux/POSIX, venv_python is a symlink pointing to base python
        assert resolved != venv_python.resolve(), "Symlink was incorrectly dereferenced"
        # Proves that running via resolved retains the venv prefix
        proc = subprocess.run(
            [str(resolved), "-c", "import sys; print(sys.prefix)"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc.stdout.strip() == str(venv_dir)

        # Proves that dereferencing the symlink would have corrupted sys.prefix (the original CI bug)
        proc_deref = subprocess.run(
            [str(resolved.resolve()), "-c", "import sys; print(sys.prefix)"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc_deref.stdout.strip() != str(venv_dir)
    else:
        # On Windows or non-symlink venv, verify execution uses the venv prefix
        proc = subprocess.run(
            [str(resolved), "-c", "import sys; print(sys.prefix)"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert Path(proc.stdout.strip()).resolve() == venv_dir.resolve()


def test_cli_rejects_missing_python_executable(tmp_path: Path) -> None:
    """CLI invocations of both scripts must fail if --python points to a nonexistent file."""
    for script_name in ("verify_godot_recovery.py", "verify_godot_live_crash.py"):
        script_path = REPO_ROOT / "scripts" / script_name
        dummy_cli = tmp_path / "dummy_cli"
        dummy_cli.write_text("", encoding="utf-8")
        dummy_fixture = tmp_path / "dummy_fixture"
        dummy_fixture.mkdir(exist_ok=True)
        (dummy_fixture / "project.godot").write_text("", encoding="utf-8")
        dummy_godot = tmp_path / "dummy_godot"
        dummy_godot.write_text("", encoding="utf-8")
        output = tmp_path / "out.json"

        cmd = [
            sys.executable,
            str(script_path),
            "--cli",
            str(dummy_cli),
            "--python",
            str(tmp_path / "missing_python_exe"),
            "--fixture",
            str(dummy_fixture),
            "--godot",
            str(dummy_godot),
            "--output",
            str(output),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        assert proc.returncode != 0
        assert "FileNotFoundError" in proc.stderr or "missing_python_exe" in proc.stderr
