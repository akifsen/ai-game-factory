"""Focused integration tests for the verify_godot_live_crash tooling."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.verify_godot_live_crash import (  # noqa: E402
    CheckFailure,
    _check_sentinel_alive,
    _cleanup_sentinel,
    _get_process_identity,
    _prepare_controlled_fixture,
    _spawn_sentinel,
    _win32_close_handle,
)


def test_process_identity_query_current() -> None:
    """_get_process_identity must identify the current process and verify liveness."""
    current_exe = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    info, handle = _get_process_identity(os.getpid(), current_exe)
    try:
        assert info["pid"] == os.getpid()
        assert info["is_alive"] is True
        assert Path(info["exe_path"]).resolve() == current_exe
        if sys.platform == "win32":
            assert info["creation_filetime"] > 0
            assert handle is not None
    finally:
        if handle is not None and sys.platform == "win32":
            _win32_close_handle(handle)


def test_process_identity_rejection_wrong_executable() -> None:
    """_get_process_identity must fail when process image does not match expected executable."""
    wrong_exe = Path(sys.executable).parent / "completely_wrong_executable_name.exe"
    with pytest.raises(CheckFailure, match="executable mismatch"):
        info, handle = _get_process_identity(os.getpid(), wrong_exe)
        if handle is not None and sys.platform == "win32":
            _win32_close_handle(handle)


def test_sentinel_lifecycle() -> None:
    """Sentinel process must spawn, report alive, and be cleanly terminated."""
    sentinel = _spawn_sentinel(Path(sys.executable))
    try:
        assert sentinel.pid > 0
        assert _check_sentinel_alive(sentinel) is True
    finally:
        _cleanup_sentinel(sentinel)
    assert _check_sentinel_alive(sentinel) is False


def test_controlled_fixture_preparation(tmp_path: Path) -> None:
    """Fixture preparation must create handshake configuration with journal, token, and valid GDScript."""
    fixture_src = REPO_ROOT / "examples" / "godot-verification"
    dest = tmp_path / "fixture_dest"
    ready = tmp_path / "ready.json"
    release = tmp_path / "release.txt"
    journal = tmp_path / "journal.jsonl"
    token = "test-token-12345"

    _prepare_controlled_fixture(
        fixture_src,
        dest,
        ready,
        release,
        journal_file=journal,
        token=token,
        max_wait_ms=10000,
    )

    assert (dest / "project.godot").is_file()
    assert (dest / "scenario.json").is_file()
    assert (dest / "handshake_config.json").is_file()
    cfg = json.loads((dest / "handshake_config.json").read_text(encoding="utf-8"))
    assert cfg["ready_file"] == ready.as_posix()
    assert cfg["release_file"] == release.as_posix()
    assert cfg["journal_file"] == journal.as_posix()
    assert cfg["token"] == token
    assert cfg["max_wait_ms"] == 10000

    main_gd = (dest / "main.gd").read_text(encoding="utf-8")
    assert "func verification_snapshot() -> Dictionary:" in main_gd
    assert "func apply_damage(amount: int) -> void:" in main_gd
    assert "func defeat_enemy() -> void:" in main_gd
    assert "func _perform_handshake() -> void:" in main_gd
    assert "handshake_config.json" in main_gd
    assert "journal_file" in main_gd


def test_wrapper_intercepts_subprocess_launch(tmp_path: Path) -> None:
    """Test-only entry wrapper must intercept subprocess.Popen and prove counted."""
    launch_log = tmp_path / "probe-observations.jsonl"
    installed_python = Path(sys.executable).resolve()
    script_path = REPO_ROOT / "scripts" / "verify_godot_live_crash.py"

    cmd = [
        str(installed_python),
        str(script_path),
        "--entry-wrapper",
        "--launch-log",
        str(launch_log),
        "--wrapper-probe",
    ]

    res = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        shell=False,
    )
    assert res.returncode == 0, (
        f"Wrapper probe failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    )

    assert launch_log.is_file(), "Launch observations log was not created"
    lines = [
        json.loads(line)
        for line in launch_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    events = [entry.get("event") for entry in lines]
    assert "wrapper_invocation_start" in events
    assert "subprocess_launch" in events
    assert "wrapper_invocation_end" in events

    launches = [entry for entry in lines if entry.get("event") == "subprocess_launch"]
    assert len(launches) == 1
    launch = launches[0]
    assert isinstance(launch["pid"], int) and launch["pid"] > 0
    assert any("import sys" in arg or "-c" in arg for arg in launch["argv"])
    assert launch["create_time"] is not None


@pytest.mark.real_godot
def test_real_godot_live_crash(tmp_path: Path) -> None:
    """End-to-end verification of Godot live crash recovery against installed wheel CLI."""
    godot_val = os.environ.get("GAMEFACTORY_TEST_REAL_GODOT") or os.environ.get(
        "GAMEFACTORY_TEST_GODOT"
    )
    if not godot_val:
        pytest.skip(
            "Opt-in real Godot test requires GAMEFACTORY_TEST_REAL_GODOT or GAMEFACTORY_TEST_GODOT env var"
        )

    godot = Path(godot_val).resolve()
    if not godot.is_file():
        pytest.fail(f"Specified Godot executable does not exist: {godot}")

    # Determine installed Python and CLI from caller env or installation manifest
    installed_python_env = os.environ.get("GAMEFACTORY_INSTALLED_PYTHON")
    installed_cli_env = os.environ.get("GAMEFACTORY_INSTALLED_CLI")
    if installed_python_env and installed_cli_env:
        installed_python = Path(installed_python_env).resolve()
        installed_cli = Path(installed_cli_env).resolve()
    else:
        cli_name = "gamefactory.exe" if sys.platform == "win32" else "gamefactory"
        sibling_cli = Path(sys.executable).parent / cli_name
        if sibling_cli.is_file():
            installed_python = Path(sys.executable).resolve()
            installed_cli = sibling_cli.resolve()
        else:
            install_json = REPO_ROOT / "docs" / "reports" / "v0.2-closeout" / "installation.json"
            if install_json.is_file():
                data = json.loads(install_json.read_text(encoding="utf-8"))
                p = data.get("installed_python")
                c = data.get("installed_cli")
                if p and c and Path(p).is_file() and Path(c).is_file():
                    installed_python = Path(p).resolve()
                    installed_cli = Path(c).resolve()
                else:
                    pytest.skip(
                        "Installed wheel environment not found from closeout installation report"
                    )
            else:
                pytest.skip("Installed wheel environment not found")

    if not installed_cli.is_file() or not installed_python.is_file():
        pytest.skip("Installed wheel environment not found")

    report_file = tmp_path / "live_crash_report.json"
    workspace = tmp_path / "live_crash_ws"

    cmd = [
        str(installed_python),
        str(REPO_ROOT / "scripts" / "verify_godot_live_crash.py"),
        "--cli",
        str(installed_cli),
        "--python",
        str(installed_python),
        "--fixture",
        str(REPO_ROOT / "examples" / "godot-verification"),
        "--godot",
        str(godot),
        "--output",
        str(report_file),
        "--workspace",
        str(workspace),
        "--timeout",
        "120",
    ]

    result = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        shell=False,
    )

    if result.returncode != 0:
        details = (
            report_file.read_text(encoding="utf-8") if report_file.is_file() else "<no report>"
        )
        pytest.fail(
            f"Live crash script failed with returncode {result.returncode}.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}\nreport:\n{details}"
        )

    assert report_file.is_file(), "Report file was not generated"
    data = json.loads(report_file.read_text(encoding="utf-8"))
    assert data.get("status") == "PASSED", f"Report status is not PASSED: {data}"
    assert data.get("checks", {}).get("sentinel", {}).get("survived_full_recovery") is True
    assert (
        data.get("checks", {}).get("after_retry", {}).get("workflow", {}).get("status") == "BLOCKED"
    )
    assert (
        data.get("checks", {}).get("launch_evidence", {}).get("no_duplicate_launches_proven")
        is True
    )


@pytest.mark.parametrize(
    "reason",
    ["os_error: denied", "malformed_stat", "starttime_mismatch: reused", "exe_mismatch: other"],
)
def test_unknown_child_state_is_not_successful_cleanup(reason: str) -> None:
    from scripts.verify_godot_live_crash import _require_terminal_posix_reason

    with pytest.raises(CheckFailure, match="Cannot confirm"):
        _require_terminal_posix_reason(reason)
