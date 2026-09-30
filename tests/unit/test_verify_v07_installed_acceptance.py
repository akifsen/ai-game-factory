"""Unit tests for scripts/verify_v07_installed_acceptance.py."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# Dynamically import verify_v07_installed_acceptance script module
SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "verify_v07_installed_acceptance.py"
spec = importlib.util.spec_from_file_location("verify_v07_installed_acceptance", SCRIPT_PATH)
assert spec and spec.loader
verifier = importlib.util.module_from_spec(spec)
sys.modules["verify_v07_installed_acceptance"] = verifier
spec.loader.exec_module(verifier)


def test_scrub_provider_secrets() -> None:
    dirty_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/runner",
        "GITHUB_WORKSPACE": "/workspace",
        "RUNNER_TEMP": "/tmp",
        "MESHY_API_KEY": "super_secret_meshy_token",
        "OPENAI_API_KEY": "sk-12345",
        "ANTHROPIC_API_KEY": "ant-secret",
        "STABILITY_API_KEY": "stab-secret",
        "CUSTOM_SECRET_KEY": "forbidden",
        "SAFE_SETTING": "enabled",
    }
    cleaned, scrubbed = verifier.scrub_provider_secrets(dirty_env)

    assert "MESHY_API_KEY" not in cleaned
    assert "OPENAI_API_KEY" not in cleaned
    assert "ANTHROPIC_API_KEY" not in cleaned
    assert "STABILITY_API_KEY" not in cleaned
    assert "CUSTOM_SECRET_KEY" not in cleaned

    assert cleaned["PATH"] == "/usr/bin:/bin"
    assert cleaned["GITHUB_WORKSPACE"] == "/workspace"
    assert cleaned["RUNNER_TEMP"] == "/tmp"
    assert cleaned["SAFE_SETTING"] == "enabled"

    assert "MESHY_API_KEY" in scrubbed
    assert "OPENAI_API_KEY" in scrubbed
    assert "ANTHROPIC_API_KEY" in scrubbed


def test_parse_junit_xml_positive(tmp_path: Path) -> None:
    xml_content = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
  <testsuite name="pytest" errors="0" failures="0" skipped="0" tests="3" time="1.5">
    <testcase classname="tests.test_a" name="test_one" time="0.5" />
    <testcase classname="tests.test_a" name="test_two" time="0.6" />
    <testcase classname="tests.test_a" name="test_three" time="0.4" />
  </testsuite>
</testsuites>
"""
    xml_file = tmp_path / "report.xml"
    xml_file.write_text(xml_content, encoding="utf-8")

    total, failures, errors, skipped, duration, cases = verifier.parse_junit_xml(xml_file)
    assert total == 3
    assert failures == 0
    assert errors == 0
    assert skipped == 0
    assert duration == pytest.approx(1.5)
    assert len(cases) == 3
    assert all(c.status == "passed" for c in cases)
    assert cases[0].name == "test_one"


def test_parse_junit_xml_with_failures_and_skips(tmp_path: Path) -> None:
    xml_content = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" errors="1" failures="1" skipped="1" tests="4" time="2.0">
  <testcase classname="tests.test_b" name="test_ok" time="0.5" />
  <testcase classname="tests.test_b" name="test_skip" time="0.1">
    <skipped message="Blender not installed" />
  </testcase>
  <testcase classname="tests.test_b" name="test_fail" time="0.4">
    <failure message="AssertionError: 1 != 2">Traceback details</failure>
  </testcase>
  <testcase classname="tests.test_b" name="test_err" time="0.2">
    <error message="RuntimeError: broken">Error details</error>
  </testcase>
</testsuite>
"""
    xml_file = tmp_path / "report_fails.xml"
    xml_file.write_text(xml_content, encoding="utf-8")

    total, failures, errors, skipped, duration, cases = verifier.parse_junit_xml(xml_file)
    assert total == 4
    assert failures == 1
    assert errors == 1
    assert skipped == 1
    assert duration == pytest.approx(2.0)
    assert len(cases) == 4

    status_map = {c.name: c for c in cases}
    assert status_map["test_ok"].status == "passed"
    assert status_map["test_skip"].status == "skipped"
    assert status_map["test_skip"].message == "Blender not installed"
    assert status_map["test_fail"].status == "failed"
    assert status_map["test_fail"].message == "AssertionError: 1 != 2"
    assert status_map["test_err"].status == "error"
    assert status_map["test_err"].message == "RuntimeError: broken"


def test_parse_junit_xml_missing_or_corrupt(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.xml"
    with pytest.raises(verifier.VerificationError, match="not found"):
        verifier.parse_junit_xml(missing)

    corrupt = tmp_path / "corrupt.xml"
    corrupt.write_text("not valid xml", encoding="utf-8")
    with pytest.raises(verifier.VerificationError, match="Malformed JUnit XML"):
        verifier.parse_junit_xml(corrupt)


def test_parse_junit_xml_malformed_counts_and_zero_cases(tmp_path: Path) -> None:
    # Zero test cases
    zero_file = tmp_path / "zero.xml"
    zero_file.write_text(
        '<testsuite tests="0" failures="0" errors="0" skipped="0" time="0.0"/>',
        encoding="utf-8",
    )
    with pytest.raises(verifier.VerificationError, match="zero test cases"):
        verifier.parse_junit_xml(zero_file)

    # Incoherent total tests vs actual cases
    mismatch_total = tmp_path / "mismatch_total.xml"
    mismatch_total.write_text(
        '<testsuite tests="5" failures="0" errors="0" skipped="0" time="0.5">'
        '<testcase name="c1" classname="t" time="0.1"/>'
        "</testsuite>",
        encoding="utf-8",
    )
    with pytest.raises(verifier.VerificationError, match="Incoherent test counts"):
        verifier.parse_junit_xml(mismatch_total)

    # Incoherent failure count
    mismatch_fail = tmp_path / "mismatch_fail.xml"
    mismatch_fail.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0" time="0.5">'
        '<testcase name="c1" classname="t" time="0.1"><failure message="boom"/></testcase>'
        "</testsuite>",
        encoding="utf-8",
    )
    with pytest.raises(verifier.VerificationError, match="Incoherent failure counts"):
        verifier.parse_junit_xml(mismatch_fail)

    # Incoherent error count
    mismatch_err = tmp_path / "mismatch_err.xml"
    mismatch_err.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0" time="0.5">'
        '<testcase name="c1" classname="t" time="0.1"><error message="err"/></testcase>'
        "</testsuite>",
        encoding="utf-8",
    )
    with pytest.raises(verifier.VerificationError, match="Incoherent error counts"):
        verifier.parse_junit_xml(mismatch_err)

    # Incoherent skipped count
    mismatch_skip = tmp_path / "mismatch_skip.xml"
    mismatch_skip.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0" time="0.5">'
        '<testcase name="c1" classname="t" time="0.1"><skipped message="skip"/></testcase>'
        "</testsuite>",
        encoding="utf-8",
    )
    with pytest.raises(verifier.VerificationError, match="Incoherent skipped counts"):
        verifier.parse_junit_xml(mismatch_skip)

    # Negative count
    neg_count = tmp_path / "neg_count.xml"
    neg_count.write_text(
        '<testsuite tests="-1" failures="0" errors="0" skipped="0" time="0.5">'
        '<testcase name="c1" classname="t" time="0.1"/>'
        "</testsuite>",
        encoding="utf-8",
    )
    with pytest.raises(verifier.VerificationError, match="Negative count"):
        verifier.parse_junit_xml(neg_count)


@pytest.mark.parametrize("duration", ["NaN", "Infinity", "-Infinity", "-0.1"])
def test_parse_junit_xml_rejects_invalid_suite_durations(tmp_path: Path, duration: str) -> None:
    report = tmp_path / "invalid_suite_duration.xml"
    report.write_text(
        f'<testsuite tests="1" failures="0" errors="0" skipped="0" time="{duration}">'
        '<testcase name="case" classname="suite" time="0.1"/>'
        "</testsuite>",
        encoding="utf-8",
    )
    with pytest.raises(verifier.VerificationError, match="Invalid suite duration"):
        verifier.parse_junit_xml(report)


@pytest.mark.parametrize("duration", ["NaN", "Infinity", "-Infinity", "-0.1"])
def test_parse_junit_xml_rejects_invalid_testcase_durations(tmp_path: Path, duration: str) -> None:
    report = tmp_path / "invalid_testcase_duration.xml"
    report.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0" time="0.1">'
        f'<testcase name="case" classname="suite" time="{duration}"/>'
        "</testsuite>",
        encoding="utf-8",
    )
    with pytest.raises(verifier.VerificationError, match="Invalid testcase time"):
        verifier.parse_junit_xml(report)


def test_run_suite_missing_file_handling(tmp_path: Path) -> None:
    req_suite = verifier.SuiteDefinition(
        suite_id="test_req",
        name="Required Missing",
        rel_path="tests/nonexistent_req.py",
        required=True,
    )
    opt_suite = verifier.SuiteDefinition(
        suite_id="test_opt",
        name="Optional Missing",
        rel_path="tests/nonexistent_opt.py",
        required=False,
    )

    dummy_python = Path(tempfile.gettempdir()) / "dummy_python"
    res_req = verifier.run_suite(
        suite=req_suite,
        python_exe=dummy_python,
        repo_root=tmp_path,
        evidence_dir=tmp_path / "evidence",
        run_dir=tmp_path / "run",
        base_env={},
    )
    assert res_req.status == "FAIL"
    assert res_req.exit_code == 1
    assert "Required test suite file not found" in (res_req.error_message or "")

    res_opt = verifier.run_suite(
        suite=opt_suite,
        python_exe=dummy_python,
        repo_root=tmp_path,
        evidence_dir=tmp_path / "evidence",
        run_dir=tmp_path / "run",
        base_env={},
    )
    assert res_opt.status == "OMITTED"
    assert res_opt.exit_code == 0
    assert "Conditional test suite file does not exist" in (res_opt.error_message or "")


def test_run_suite_stale_junit_and_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure pre-existing JUnit file cannot cause a false PASS when subprocess outputs nothing."""
    suite_file = tmp_path / "tests" / "test_dummy.py"
    suite_file.parent.mkdir(parents=True, exist_ok=True)
    suite_file.write_text("# dummy test file\n", encoding="utf-8")

    evidence_dir = tmp_path / "evidence"
    reports_dir = evidence_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Old stale JUnit file from previous run or another suite
    stale_file = reports_dir / "my_suite.xml"
    stale_file.write_text(
        '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0" time="0.1">'
        '<testcase classname="old" name="stale_success" time="0.1"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )

    suite = verifier.SuiteDefinition(
        suite_id="my_suite",
        name="Stale JUnit Probe Suite",
        rel_path="tests/test_dummy.py",
        required=True,
    )

    # Subprocess outputs nothing and creates no report (e.g. crash or mock returncode=0)
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="no fresh junit created", stderr=""
        ),
    )

    result = verifier.run_suite(
        suite=suite,
        python_exe=Path(sys.executable),
        repo_root=tmp_path,
        evidence_dir=evidence_dir,
        run_dir=tmp_path,
        base_env={},
    )

    # Must fail closed: fresh report was not produced
    assert result.status == "FAIL"
    assert result.total_tests == 0
    assert "JUnit XML report not produced" in (result.error_message or "")

    # Pre-existing foreign file was NOT deleted
    assert stale_file.is_file()


def test_run_suite_refuses_preexisting_explicit_report(tmp_path: Path) -> None:
    """When an explicit target JUnit path already exists, refuse without deleting."""
    suite_file = tmp_path / "tests" / "test_dummy.py"
    suite_file.parent.mkdir(parents=True, exist_ok=True)
    suite_file.write_text("# dummy test file\n", encoding="utf-8")

    evidence_dir = tmp_path / "evidence"
    reports_dir = evidence_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    explicit_junit = reports_dir / "existing_explicit.xml"
    original_content = "<foreign_content/>"
    explicit_junit.write_text(original_content, encoding="utf-8")

    suite = verifier.SuiteDefinition(
        suite_id="explicit_suite",
        name="Explicit Suite",
        rel_path="tests/test_dummy.py",
        required=True,
    )

    result = verifier.run_suite(
        suite=suite,
        python_exe=Path(sys.executable),
        repo_root=tmp_path,
        evidence_dir=evidence_dir,
        run_dir=tmp_path,
        base_env={},
        junit_path=explicit_junit,
    )

    assert result.status == "FAIL"
    assert "already exists; refusing to overwrite" in (result.error_message or "")
    # Foreign file preserved intact
    assert explicit_junit.read_text(encoding="utf-8") == original_content


def test_inspect_tool_versions_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    blender_exe = tmp_path / "blender.exe"
    godot_exe = tmp_path / "godot.exe"
    blender_exe.write_text("dummy", encoding="utf-8")
    godot_exe.write_text("dummy", encoding="utf-8")

    def mock_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        exe = str(cmd[0])
        if "blender" in exe:
            return SimpleNamespace(
                returncode=0,
                stdout="Blender 5.2.1 LTS (hash 1234abcd)\nCopyright 2002-2026 Blender Foundation",
                stderr="",
            )
        elif "godot" in exe:
            return SimpleNamespace(
                returncode=0,
                stdout="4.7.2.stable.official.ed1daf0bf\n",
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="unknown tool")

    monkeypatch.setattr(verifier.subprocess, "run", mock_run)

    blender_info, godot_info = verifier.inspect_tool_versions(
        blender_exe=blender_exe,
        godot_exe=godot_exe,
        expected_godot_version="4.7.2.stable.official.ed1daf0bf",
    )

    assert blender_info["available"] is True
    assert blender_info["parsed_version"] == [5, 2, 1]
    assert godot_info["available"] is True
    assert godot_info["version_string"] == "4.7.2.stable.official.ed1daf0bf"


def test_inspect_tool_versions_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    blender_exe = tmp_path / "blender.exe"
    godot_exe = tmp_path / "godot.exe"
    blender_exe.write_text("dummy", encoding="utf-8")
    godot_exe.write_text("dummy", encoding="utf-8")

    # 1. Blender empty version output
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    with pytest.raises(verifier.VerificationError, match="empty version output"):
        verifier.inspect_tool_versions(blender_exe, godot_exe)

    # 2. Blender garbage version output
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="Segmentation fault (core dumped)\n", stderr=""
        ),
    )
    with pytest.raises(
        verifier.VerificationError, match="does not contain recognized version string"
    ):
        verifier.inspect_tool_versions(blender_exe, godot_exe)

    # 3. Blender unsupported version (< 4.0.2)
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="Blender 3.6.0 LTS\n", stderr=""
        ),
    )
    with pytest.raises(verifier.VerificationError, match="below documented adapter minimum"):
        verifier.inspect_tool_versions(blender_exe, godot_exe)

    # 4. Godot empty version output
    def mock_blender_ok_godot_empty(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        if "blender" in str(cmd[0]):
            return SimpleNamespace(returncode=0, stdout="Blender 4.0.2\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(verifier.subprocess, "run", mock_blender_ok_godot_empty)
    with pytest.raises(verifier.VerificationError, match="empty version output"):
        verifier.inspect_tool_versions(blender_exe, godot_exe)

    # 5. Godot garbage output
    def mock_blender_ok_godot_garbage(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        if "blender" in str(cmd[0]):
            return SimpleNamespace(returncode=0, stdout="Blender 4.0.2\n", stderr="")
        return SimpleNamespace(
            returncode=0, stdout="FATAL: failed to initialize display\n", stderr=""
        )

    monkeypatch.setattr(verifier.subprocess, "run", mock_blender_ok_godot_garbage)
    with pytest.raises(
        verifier.VerificationError, match="does not contain recognized version string"
    ):
        verifier.inspect_tool_versions(blender_exe, godot_exe)

    # 6. Godot unsupported/mismatched version
    def mock_blender_ok_godot_mismatch(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        if "blender" in str(cmd[0]):
            return SimpleNamespace(returncode=0, stdout="Blender 4.0.2\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="3.5.2.stable.official\n", stderr="")

    monkeypatch.setattr(verifier.subprocess, "run", mock_blender_ok_godot_mismatch)
    with pytest.raises(
        verifier.VerificationError, match="does not match expected official version"
    ):
        verifier.inspect_tool_versions(
            blender_exe, godot_exe, expected_godot_version="4.7.2.stable.official.ed1daf0bf"
        )


def _find_sole_run_dir(base_dir: Path) -> Path:
    child_dirs = [p for p in base_dir.iterdir() if p.is_dir()]
    assert len(child_dirs) == 1, f"Expected 1 run directory, found: {child_dirs}"
    return child_dirs[0]


def test_verify_wheel_hash(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    wheel_file = tmp_path / "gamefactory-0.6.0-py3-none-any.whl"
    wheel_file.write_bytes(b"dummy wheel contents for hashing")
    expected_hash = "3a002c0bd522784920a7b3dc59fe9784b6d99f9cd9877c5ae63c083f85ecaa91"

    # From wheel file alone
    digest1 = verifier.verify_wheel_hash(wheel_file, None, evidence_dir)
    assert digest1 == expected_hash
    saved_sha = evidence_dir / "wheel.sha256"
    assert saved_sha.is_file()
    assert expected_hash in saved_sha.read_text(encoding="utf-8")

    # From wheel file with matching hash file
    hash_file = tmp_path / "wheel.sha256.input"
    hash_file.write_text(f"{expected_hash}  gamefactory.whl\n", encoding="utf-8")
    digest2 = verifier.verify_wheel_hash(wheel_file, hash_file, evidence_dir)
    assert digest2 == expected_hash

    # Debug fallback when wheel path is omitted in nonrelease debug mode:
    # Must return None and not write fabricated wheel.sha256 even if supplied hash file exists
    debug_dir = tmp_path / "evidence_debug"
    digest_debug = verifier.verify_wheel_hash(None, hash_file, debug_dir, require_wheel=False)
    assert digest_debug is None
    assert not (debug_dir / "wheel.sha256").exists()


def test_v07_suites_required_definitions() -> None:
    """Character release gates include paid evidence and the packaged nine-view candidate."""
    suite_map = {s.rel_path: s for s in verifier.V07_SUITES}

    # test_v07_character_paid_workflow must be present and REQUIRED
    paid_path = "tests/integration/test_v07_character_paid_workflow.py"
    assert paid_path in suite_map, f"Missing required suite: {paid_path}"
    assert suite_map[paid_path].required is True
    assert suite_map[paid_path].suite_id == "character_paid_workflow"

    # provider_character_evidence must be present and REQUIRED (not conditional)
    provider_path = "tests/integration/test_provider_character_evidence.py"
    assert provider_path in suite_map, f"Missing required suite: {provider_path}"
    assert suite_map[provider_path].required is True
    cli_path = "tests/integration/test_v07_character_cli_runtime.py"
    assert suite_map[cli_path].required is True
    assert suite_map[cli_path].suite_id == "character_cli_runtime"
    assembly_cli_path = "tests/integration/test_v07_public_assembly_cli.py"
    assert suite_map[assembly_cli_path].required is True
    assert suite_map[assembly_cli_path].suite_id == "public_assembly_cli"


def test_debug_mode_labeling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure that using --allow-skip or --allow-source-checkout results in DEBUG_PASS, not release PASS."""
    # Mock find_blender and find_godot
    blender = tmp_path / "blender"
    godot = tmp_path / "godot"
    blender.write_text("dummy", encoding="utf-8")
    godot.write_text("dummy", encoding="utf-8")

    monkeypatch.setattr(verifier, "find_blender", lambda _=None: blender)
    monkeypatch.setattr(verifier, "find_godot", lambda _=None: godot)
    monkeypatch.setattr(
        verifier,
        "inspect_tool_versions",
        lambda *_a, **_kw: (
            {"executable": str(blender), "version_string": "Blender 5.2.1 LTS", "available": True},
            {
                "executable": str(godot),
                "version_string": "4.7.2.stable.official.ed1daf0bf",
                "sha256": "h",
                "available": True,
            },
        ),
    )
    monkeypatch.setattr(
        verifier,
        "inspect_module_provenance",
        lambda *_a, **_kw: {
            "module_path": "/fake/site-packages/gamefactory/__init__.py",
            "version": "0.7.0",
            "inside_site_packages": True,
            "source_checkout_leak": False,
        },
    )

    evidence = tmp_path / "evidence"

    # Test run with --allow-skip
    _ = verifier.main(
        [
            "--evidence-dir",
            str(evidence),
            "--allow-skip",
            "--suite",
            "nonexistent_suite_id_to_skip_all",
        ]
    )

    run_dir = _find_sole_run_dir(evidence)
    results_file = run_dir / "results.json"
    assert results_file.is_file()
    data = json.loads(results_file.read_text(encoding="utf-8"))

    # When debug flags are passed, status cannot be release PASS
    assert data["release_gate"] is False
    assert data["debug_mode"] is True
    assert data["debug_flags"]["allow_skip"] is True
    assert "run_id" in data
    assert "timestamp" in data
    if data["status"] != "FAIL":
        assert data["status"] == "DEBUG_PASS"


def _mock_successful_main_preflight(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    blender = tmp_path / "blender"
    godot = tmp_path / "godot"
    blender.write_text("dummy", encoding="utf-8")
    godot.write_text("dummy", encoding="utf-8")
    monkeypatch.setattr(verifier, "find_blender", lambda _=None: blender)
    monkeypatch.setattr(verifier, "find_godot", lambda _=None: godot)
    monkeypatch.setattr(
        verifier,
        "inspect_tool_versions",
        lambda *_args, **_kwargs: (
            {"executable": str(blender), "version_string": "Blender 5.2.1", "available": True},
            {
                "executable": str(godot),
                "version_string": verifier.DEFAULT_EXPECTED_GODOT_VERSION,
                "sha256": "godot-hash",
                "available": True,
            },
        ),
    )
    monkeypatch.setattr(
        verifier,
        "inspect_module_provenance",
        lambda *_args, **_kwargs: {
            "module_path": "/venv/site-packages/gamefactory/__init__.py",
            "version": "0.7.0",
            "inside_site_packages": True,
            "source_checkout_leak": False,
        },
    )

    monkeypatch.setattr(
        verifier,
        "verify_wheel_hash",
        lambda *_args, **_kwargs: "mocked-wheel-sha256",
    )

    def successful_suite(suite: Any, **_kwargs: Any) -> Any:
        return verifier.SuiteRunResult(
            suite_id=suite.suite_id,
            name=suite.name,
            rel_path=suite.rel_path,
            required=suite.required,
            status="PASS",
            exit_code=0,
            total_tests=1,
            passed_tests=1,
            failed_tests=0,
            errored_tests=0,
            skipped_tests=0,
            duration_seconds=0.1,
            command=["pytest", suite.rel_path],
            log_path=None,
            junit_path=None,
            cases=[verifier.TestCaseResult("case", suite.suite_id, 0.1, "passed")],
        )

    monkeypatch.setattr(verifier, "run_suite", successful_suite)


def test_successful_filtered_suite_is_debug_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_successful_main_preflight(monkeypatch, tmp_path)
    evidence = tmp_path / "filtered-evidence"
    selected = next(suite for suite in verifier.V07_SUITES if suite.required)

    assert verifier.main(["--evidence-dir", str(evidence), "--suite", selected.suite_id]) == 0
    run_dir = _find_sole_run_dir(evidence)
    summary = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    assert summary["status"] == "DEBUG_PASS"
    assert summary["release_gate"] is False
    assert summary["release_mode"] is False
    assert summary["debug_mode"] is True
    assert summary["debug_flags"]["partial_suite_selection"] is True
    assert "run_id" in summary
    assert "timestamp" in summary


def test_all_required_suites_are_needed_for_release_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_successful_main_preflight(monkeypatch, tmp_path)
    evidence = tmp_path / "full-evidence"
    required_ids = [suite.suite_id for suite in verifier.V07_SUITES if suite.required]
    argv = ["--evidence-dir", str(evidence)]
    for suite_id in required_ids:
        argv.extend(["--suite", suite_id])

    assert verifier.main(argv) == 0
    run_dir = _find_sole_run_dir(evidence)
    summary = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    assert summary["status"] == "PASS"
    assert summary["release_gate"] is True
    assert summary["release_mode"] is True
    assert summary["debug_mode"] is False
    assert summary["debug_flags"]["partial_suite_selection"] is False
    assert summary["summary"]["passed_suites"] == len(required_ids)
    assert "run_id" in summary
    assert "timestamp" in summary


def test_verify_wheel_missing_actual_wheel(tmp_path: Path) -> None:
    """Missing actual wheel fails closed in release mode."""
    evidence_dir = tmp_path / "evidence"
    with pytest.raises(verifier.VerificationError, match="Actual wheel file path is required"):
        verifier.verify_wheel_hash(None, None, evidence_dir, require_wheel=True)

    missing_wheel = tmp_path / "nonexistent.whl"
    with pytest.raises(verifier.VerificationError, match="Wheel file not found"):
        verifier.verify_wheel_hash(missing_wheel, None, evidence_dir, require_wheel=True)


def test_main_release_fails_closed_without_actual_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invoking main() in release mode without --wheel must fail closed with exit code 1."""
    blender = tmp_path / "blender"
    godot = tmp_path / "godot"
    blender.write_text("dummy", encoding="utf-8")
    godot.write_text("dummy", encoding="utf-8")
    monkeypatch.setattr(verifier, "find_blender", lambda _=None: blender)
    monkeypatch.setattr(verifier, "find_godot", lambda _=None: godot)
    monkeypatch.setattr(
        verifier,
        "inspect_tool_versions",
        lambda *_args, **_kwargs: (
            {"executable": str(blender), "version_string": "Blender 5.2.1", "available": True},
            {
                "executable": str(godot),
                "version_string": verifier.DEFAULT_EXPECTED_GODOT_VERSION,
                "sha256": "godot-hash",
                "available": True,
            },
        ),
    )
    monkeypatch.setattr(
        verifier,
        "inspect_module_provenance",
        lambda *_args, **_kwargs: {
            "module_path": "/venv/site-packages/gamefactory/__init__.py",
            "version": "0.7.0",
            "inside_site_packages": True,
            "source_checkout_leak": False,
        },
    )

    evidence = tmp_path / "evidence"
    # Default release run without --wheel must fail closed with exit code 1
    assert verifier.main(["--evidence-dir", str(evidence)]) == 1
    run_dir = _find_sole_run_dir(evidence)
    summary = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    assert summary["status"] == "FAIL"
    assert summary["release_gate"] is False
    assert summary["release_mode"] is True
    assert summary["debug_mode"] is False
    assert "run_id" in summary
    assert "timestamp" in summary


def test_verify_wheel_unmatched_hash(tmp_path: Path) -> None:
    """Mismatched computed wheel digest and supplied hash file must raise VerificationError."""
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    wheel_file = tmp_path / "gamefactory-0.7.0-py3-none-any.whl"
    wheel_file.write_bytes(b"actual wheel contents A")

    hash_file = tmp_path / "mismatched.sha256"
    hash_file.write_text(
        "0000000000000000000000000000000000000000000000000000000000000000  gamefactory.whl\n",
        encoding="utf-8",
    )

    with pytest.raises(verifier.VerificationError, match="Wheel SHA-256 digest mismatch"):
        verifier.verify_wheel_hash(wheel_file, hash_file, evidence_dir)


def test_verify_wheel_valid_verified(tmp_path: Path) -> None:
    """Actual wheel file digest matches supplied hash file and writes verified evidence."""
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    wheel_file = tmp_path / "gamefactory-0.7.0-py3-none-any.whl"
    content = b"valid production wheel content for acceptance"
    wheel_file.write_bytes(content)
    expected_digest = hashlib.sha256(content).hexdigest()

    hash_file = tmp_path / "valid.sha256"
    hash_file.write_text(f"{expected_digest}  {wheel_file.name}\n", encoding="utf-8")

    digest = verifier.verify_wheel_hash(wheel_file, hash_file, evidence_dir, require_wheel=True)
    assert digest == expected_digest
    saved_sha = evidence_dir / "wheel.sha256"
    assert saved_sha.is_file()
    assert expected_digest in saved_sha.read_text(encoding="utf-8")


def test_no_inherited_test_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression test: PYTEST_ADDOPTS in parent env cannot reduce test cases in child suite."""
    test_dir = tmp_path / "tests"
    test_dir.mkdir(parents=True, exist_ok=True)
    test_file = test_dir / "test_two_cases.py"
    test_file.write_text(
        "def test_suite_case_one():\n"
        "    assert True\n\n"
        "def test_suite_case_two():\n"
        "    assert True\n",
        encoding="utf-8",
    )

    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    suite = verifier.SuiteDefinition(
        suite_id="two_cases_suite",
        name="Two Cases Suite",
        rel_path="tests/test_two_cases.py",
        required=True,
    )

    # Pollute environment with -k filter that would select only 1 of the 2 tests
    dirty_env = dict(os.environ)
    dirty_env["PYTEST_ADDOPTS"] = "-k test_suite_case_one"
    dirty_env["PYTEST_PLUGINS"] = "some_nonexistent_plugin"
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k test_suite_case_one")

    result = verifier.run_suite(
        suite=suite,
        python_exe=Path(sys.executable),
        repo_root=tmp_path,
        evidence_dir=evidence_dir,
        run_dir=tmp_path,
        base_env=dirty_env,
    )

    assert result.status == "PASS"
    # If PYTEST_ADDOPTS had leaked, total_tests would be 1. It must be 2!
    assert result.total_tests == 2
    assert result.passed_tests == 2
    assert result.failed_tests == 0
    assert result.skipped_tests == 0
    assert result.cases is not None
    case_names = {c.name for c in result.cases}
    assert case_names == {"test_suite_case_one", "test_suite_case_two"}


def test_no_repo_config_test_selection(tmp_path: Path) -> None:
    """Regression test: -o addopts= overrides repo config so config cannot reduce required suite."""
    test_dir = tmp_path / "tests"
    test_dir.mkdir(parents=True, exist_ok=True)
    test_file = test_dir / "test_config_override.py"
    test_file.write_text(
        "def test_config_case_a():\n"
        "    assert True\n\n"
        "def test_config_case_b():\n"
        "    assert True\n",
        encoding="utf-8",
    )

    # Write a pytest.ini in run directory specifying addopts that filter tests
    ini_file = tmp_path / "pytest.ini"
    ini_file.write_text(
        "[pytest]\naddopts = -k test_config_case_a\n",
        encoding="utf-8",
    )

    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    suite = verifier.SuiteDefinition(
        suite_id="config_override_suite",
        name="Config Override Suite",
        rel_path="tests/test_config_override.py",
        required=True,
    )

    result = verifier.run_suite(
        suite=suite,
        python_exe=Path(sys.executable),
        repo_root=tmp_path,
        evidence_dir=evidence_dir,
        run_dir=tmp_path,
        base_env={},
    )

    assert result.status == "PASS"
    assert result.total_tests == 2
    assert result.passed_tests == 2
    assert result.cases is not None
    case_names = {c.name for c in result.cases}
    assert case_names == {"test_config_case_a", "test_config_case_b"}


@pytest.mark.parametrize(
    "failure_kind",
    ["missing_tool", "provenance_leak", "missing_wheel"],
)
def test_preexisting_parent_pass_retained_on_preflight_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_kind: str
) -> None:
    """Pre-existing parent PASS evidence is retained intact when subsequent runs fail preflight."""
    evidence_base = tmp_path / "evidence_base"
    evidence_base.mkdir(parents=True, exist_ok=True)

    # 1. Historical parent PASS report
    historical_json = evidence_base / "results.json"
    historical_content = json.dumps(
        {
            "status": "PASS",
            "release_gate": True,
            "release_mode": True,
            "run_id": "historical-run-pass-12345",
            "timestamp": "2026-09-01T00:00:00Z",
            "summary": {"total_suites": 10, "passed_suites": 10, "failed_suites": 0},
        },
        indent=2,
    )
    historical_json.write_text(historical_content, encoding="utf-8")
    historical_bytes = historical_json.read_bytes()

    # Preflight mocks
    blender = tmp_path / "blender"
    godot = tmp_path / "godot"
    blender.write_text("dummy", encoding="utf-8")
    godot.write_text("dummy", encoding="utf-8")

    if failure_kind == "missing_tool":
        monkeypatch.setattr(verifier, "find_blender", lambda _=None: None)
        monkeypatch.setattr(verifier, "find_godot", lambda _=None: godot)
    elif failure_kind == "provenance_leak":
        monkeypatch.setattr(verifier, "find_blender", lambda _=None: blender)
        monkeypatch.setattr(verifier, "find_godot", lambda _=None: godot)
        monkeypatch.setattr(
            verifier,
            "inspect_tool_versions",
            lambda *_a, **_kw: (
                {"executable": str(blender), "version_string": "Blender 5.2.1", "available": True},
                {
                    "executable": str(godot),
                    "version_string": verifier.DEFAULT_EXPECTED_GODOT_VERSION,
                    "sha256": "h",
                    "available": True,
                },
            ),
        )

        def fail_provenance(*_a: Any, **_kw: Any) -> Any:
            raise verifier.VerificationError(
                "FAIL-CLOSED: gamefactory was imported from source checkout"
            )

        monkeypatch.setattr(verifier, "inspect_module_provenance", fail_provenance)
    elif failure_kind == "missing_wheel":
        monkeypatch.setattr(verifier, "find_blender", lambda _=None: blender)
        monkeypatch.setattr(verifier, "find_godot", lambda _=None: godot)
        monkeypatch.setattr(
            verifier,
            "inspect_tool_versions",
            lambda *_a, **_kw: (
                {"executable": str(blender), "version_string": "Blender 5.2.1", "available": True},
                {
                    "executable": str(godot),
                    "version_string": verifier.DEFAULT_EXPECTED_GODOT_VERSION,
                    "sha256": "h",
                    "available": True,
                },
            ),
        )
        monkeypatch.setattr(
            verifier,
            "inspect_module_provenance",
            lambda *_a, **_kw: {
                "module_path": "/venv/site-packages/gamefactory/__init__.py",
                "version": "0.7.0",
                "inside_site_packages": True,
                "source_checkout_leak": False,
            },
        )

    # In all failure cases, main must return 1
    assert verifier.main(["--evidence-dir", str(evidence_base)]) == 1

    # 1. Historical parent results.json must be untouched with exact identical bytes
    assert historical_json.read_bytes() == historical_bytes

    # 2. A new child run directory was created with unique run ID
    child_dirs = [p for p in evidence_base.iterdir() if p.is_dir()]
    assert len(child_dirs) == 1
    run_dir = child_dirs[0]
    assert run_dir.name != "historical-run-pass-12345"

    # 3. Fresh controlled FAIL summary was emitted in the new run directory
    child_results_file = run_dir / "results.json"
    assert child_results_file.is_file()
    child_summary = json.loads(child_results_file.read_text(encoding="utf-8"))
    assert child_summary["status"] == "FAIL"
    assert child_summary["release_gate"] is False
    assert child_summary["release_mode"] is True
    assert child_summary["run_id"] == run_dir.name
    assert "timestamp" in child_summary
    assert child_summary["error_message"] is not None


def test_full_suite_fail_sets_release_gate_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed test suite in full release mode sets release_gate=False and release_mode=True."""
    _mock_successful_main_preflight(monkeypatch, tmp_path)

    # Mock one of the suites to FAIL
    def failing_suite(suite: Any, **_kwargs: Any) -> Any:
        if suite.suite_id == "standalone_character_blender":
            return verifier.SuiteRunResult(
                suite_id=suite.suite_id,
                name=suite.name,
                rel_path=suite.rel_path,
                required=suite.required,
                status="FAIL",
                exit_code=1,
                total_tests=1,
                passed_tests=0,
                failed_tests=1,
                errored_tests=0,
                skipped_tests=0,
                duration_seconds=0.2,
                command=["pytest", suite.rel_path],
                log_path=None,
                junit_path=None,
                error_message="Test assertion failed",
                cases=[
                    verifier.TestCaseResult(
                        "test_fail", suite.suite_id, 0.2, "failed", "AssertionError"
                    )
                ],
            )
        return verifier.SuiteRunResult(
            suite_id=suite.suite_id,
            name=suite.name,
            rel_path=suite.rel_path,
            required=suite.required,
            status="PASS",
            exit_code=0,
            total_tests=1,
            passed_tests=1,
            failed_tests=0,
            errored_tests=0,
            skipped_tests=0,
            duration_seconds=0.1,
            command=["pytest", suite.rel_path],
            log_path=None,
            junit_path=None,
            cases=[verifier.TestCaseResult("test_pass", suite.suite_id, 0.1, "passed")],
        )

    monkeypatch.setattr(verifier, "run_suite", failing_suite)

    evidence = tmp_path / "full-suite-fail-evidence"
    # Run full mode with all required suites
    ret = verifier.main(["--evidence-dir", str(evidence)])
    assert ret == 1

    run_dir = _find_sole_run_dir(evidence)
    summary = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    assert summary["status"] == "FAIL"
    assert summary["release_mode"] is True
    assert summary["release_gate"] is False
    assert summary["debug_mode"] is False
    assert summary["summary"]["failed_suites"] == 1
    assert summary["summary"]["failed_tests"] == 1


def test_debug_hash_only_wheel_is_unverified_no_fabricated_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In DEBUG mode without actual wheel, supplied hash is not trusted as computed SHA and unverified_wheel is True."""
    _mock_successful_main_preflight(monkeypatch, tmp_path)
    monkeypatch.undo()

    blender = tmp_path / "blender"
    godot = tmp_path / "godot"
    blender.write_text("dummy", encoding="utf-8")
    godot.write_text("dummy", encoding="utf-8")
    monkeypatch.setattr(verifier, "find_blender", lambda _=None: blender)
    monkeypatch.setattr(verifier, "find_godot", lambda _=None: godot)
    monkeypatch.setattr(
        verifier,
        "inspect_tool_versions",
        lambda *_args, **_kwargs: (
            {"executable": str(blender), "version_string": "Blender 5.2.1", "available": True},
            {
                "executable": str(godot),
                "version_string": verifier.DEFAULT_EXPECTED_GODOT_VERSION,
                "sha256": "h",
                "available": True,
            },
        ),
    )
    monkeypatch.setattr(
        verifier,
        "inspect_module_provenance",
        lambda *_args, **_kwargs: {
            "module_path": "/venv/site-packages/gamefactory/__init__.py",
            "version": "0.7.0",
            "inside_site_packages": True,
            "source_checkout_leak": False,
        },
    )
    monkeypatch.setattr(
        verifier,
        "run_suite",
        lambda suite, **_kw: verifier.SuiteRunResult(
            suite_id=suite.suite_id,
            name=suite.name,
            rel_path=suite.rel_path,
            required=suite.required,
            status="PASS",
            exit_code=0,
            total_tests=1,
            passed_tests=1,
            failed_tests=0,
            errored_tests=0,
            skipped_tests=0,
            duration_seconds=0.1,
            command=["pytest"],
            log_path=None,
            junit_path=None,
            cases=[verifier.TestCaseResult("c", suite.suite_id, 0.1, "passed")],
        ),
    )

    hash_file = tmp_path / "wheel.sha256"
    fake_digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    hash_file.write_text(f"{fake_digest}  unbuilt_wheel.whl\n", encoding="utf-8")

    evidence = tmp_path / "debug-hashonly-evidence"
    # Run with --allow-skip (debug mode), --wheel-hash, but NO --wheel
    ret = verifier.main(
        [
            "--evidence-dir",
            str(evidence),
            "--allow-skip",
            "--wheel-hash",
            str(hash_file),
            "--suite",
            "standalone_character_blender",
        ]
    )
    assert ret == 0

    run_dir = _find_sole_run_dir(evidence)
    summary = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    assert summary["status"] == "DEBUG_PASS"
    assert summary["release_gate"] is False
    assert summary["debug_flags"]["unverified_wheel"] is True
    assert summary["module_provenance"]["wheel_sha256"] is None

    prov = json.loads((run_dir / "provenance.json").read_text(encoding="utf-8"))
    assert prov["wheel_sha256"] is None

    # No wheel.sha256 was fabricated in the evidence run dir
    assert not (run_dir / "wheel.sha256").exists()


def test_refuses_path_collision_without_delete(tmp_path: Path) -> None:
    """Pre-existing run directory collision is refused without deleting existing files."""
    evidence_base = tmp_path / "collision_base"
    evidence_base.mkdir(parents=True, exist_ok=True)

    explicit_id = "test-collision-run-id"
    preexisting_dir = evidence_base / explicit_id
    preexisting_dir.mkdir(parents=True, exist_ok=True)
    canary = preexisting_dir / "important_data.txt"
    canary_content = "do not touch or delete this file"
    canary.write_text(canary_content, encoding="utf-8")

    ret = verifier.main(["--evidence-dir", str(evidence_base), "--run-id", explicit_id])
    assert ret == 1

    # Preexisting canary file must remain untouched
    assert canary.read_text(encoding="utf-8") == canary_content


def test_existing_full_pass_and_partial_debug_status_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve existing behavior: full PASS has release_gate=True, partial debug has release_gate=False."""
    _mock_successful_main_preflight(monkeypatch, tmp_path)

    # 1. Full release pass
    full_evidence = tmp_path / "full-pass-evidence"
    assert verifier.main(["--evidence-dir", str(full_evidence)]) == 0
    full_summary = json.loads(
        (_find_sole_run_dir(full_evidence) / "results.json").read_text(encoding="utf-8")
    )
    assert full_summary["status"] == "PASS"
    assert full_summary["release_gate"] is True
    assert full_summary["release_mode"] is True
    assert full_summary["debug_mode"] is False

    # 2. Partial suite debug pass
    debug_evidence = tmp_path / "debug-pass-evidence"
    assert (
        verifier.main(
            ["--evidence-dir", str(debug_evidence), "--suite", "standalone_character_blender"]
        )
        == 0
    )
    debug_summary = json.loads(
        (_find_sole_run_dir(debug_evidence) / "results.json").read_text(encoding="utf-8")
    )
    assert debug_summary["status"] == "DEBUG_PASS"
    assert debug_summary["release_gate"] is False
    assert debug_summary["release_mode"] is False
    assert debug_summary["debug_mode"] is True


@pytest.mark.parametrize(
    "invalid_run_id",
    [
        "D:foo",
        "D:",
        "C:",
        "C:foo",
        "c:/escape",
        "foo/bar",
        "foo\\bar",
        "/absolute",
        "\\absolute",
        "..",
        ".",
        "foo..bar",
        "",
        "CON",
        "con",
        "PRN",
        "prn",
        "AUX",
        "aux",
        "NUL",
        "nul",
        "COM1",
        "com9",
        "LPT1",
        "lpt9",
        "trailing_dot.",
        "trailing_space ",
        "has space",
        "has:colon",
        "has*wildcard",
        "has?question",
    ],
)
def test_validate_run_id_rejects_invalid_component(invalid_run_id: str) -> None:
    """Conservative validator rejects colons, drive letters, slashes, reserved names, dots, and spaces."""
    assert verifier.validate_run_id(invalid_run_id) is False


@pytest.mark.parametrize(
    "valid_run_id",
    [
        "valid-run-id-123",
        "run_456_ABC",
        "a",
        "1",
        "550e8400-e29b-41d4-a716-446655440000",
        "run-with-hyphen-and_underscore-999",
    ],
)
def test_validate_run_id_accepts_valid_component(valid_run_id: str) -> None:
    """Conservative validator accepts single-component ASCII identifiers."""
    assert verifier.validate_run_id(valid_run_id) is True


@pytest.mark.parametrize(
    "escape_run_id",
    [
        "D:foo",
        "D:",
        "C:",
        "C:escape",
        "../escaped",
        "subdir/child",
        "subdir\\child",
        "CON",
        "trailing_dot.",
    ],
)
def test_main_rejects_path_escape_and_invalid_run_id(tmp_path: Path, escape_run_id: str) -> None:
    """CLI driver fails closed on drive-relative, separator, or invalid run IDs without creating dir."""
    evidence_base = tmp_path / "evidence_base"
    ret = verifier.main(["--evidence-dir", str(evidence_base), "--run-id", escape_run_id])
    assert ret == 1
    # Check that no escaped directory was created on disk
    if evidence_base.exists():
        assert list(evidence_base.iterdir()) == []


def test_main_accepts_valid_custom_run_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid single-component run ID is accepted and creates the exact child directory."""
    _mock_successful_main_preflight(monkeypatch, tmp_path)
    evidence_base = tmp_path / "valid_custom_base"
    custom_id = "custom-run_2026-09-30_v07"

    ret = verifier.main(
        [
            "--evidence-dir",
            str(evidence_base),
            "--run-id",
            custom_id,
            "--suite",
            "standalone_character_blender",
        ]
    )
    assert ret == 0

    run_dir = evidence_base / custom_id
    assert run_dir.is_dir()
    assert run_dir.parent.resolve() == evidence_base.resolve()
    summary = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    assert summary["run_id"] == custom_id


def test_main_default_uuid_run_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When --run-id is omitted, a valid UUID4 is generated and used as the child directory."""
    _mock_successful_main_preflight(monkeypatch, tmp_path)
    evidence_base = tmp_path / "default_uuid_base"

    ret = verifier.main(
        [
            "--evidence-dir",
            str(evidence_base),
            "--suite",
            "standalone_character_blender",
        ]
    )
    assert ret == 0

    run_dir = _find_sole_run_dir(evidence_base)
    import uuid

    # Must parse as valid UUID
    parsed_uuid = uuid.UUID(run_dir.name)
    assert str(parsed_uuid) == run_dir.name
    assert run_dir.parent.resolve() == evidence_base.resolve()
    summary = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    assert summary["run_id"] == run_dir.name


def test_main_run_id_collision_preserves_canary(tmp_path: Path) -> None:
    """Attempting to reuse an existing run ID directory fails closed without altering contents."""
    evidence_base = tmp_path / "collision_canary_base"
    evidence_base.mkdir(parents=True, exist_ok=True)
    custom_id = "reused-run-id-99"
    target_dir = evidence_base / custom_id
    target_dir.mkdir(parents=True, exist_ok=True)
    canary = target_dir / "canary.json"
    canary.write_text('{"untouched": true}', encoding="utf-8")

    ret = verifier.main(["--evidence-dir", str(evidence_base), "--run-id", custom_id])
    assert ret == 1
    assert canary.read_text(encoding="utf-8") == '{"untouched": true}'
    # Ensure results.json was not created in the collided dir
    assert not (target_dir / "results.json").exists()
