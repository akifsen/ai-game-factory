#!/usr/bin/env python3
"""Offline installed-wheel V0.7 acceptance test driver for Linux CI.

Verifies that the isolated installed wheel package (resolved from site-packages,
not checkout source) correctly completes V0.7 real-engine integration suites
offline using real Blender and Godot.

Guarantees:
1. Module provenance: gamefactory resolves from virtualenv site-packages,
   never leaking from checkout source tree.
2. Tool availability & version: real Blender and Godot must be available and
   report compatible versions; missing tools fail closed.
3. Live-provider isolation: live API secrets (e.g. MESHY_API_KEY) are unset
   and verified absent; all tests use isolated fixture actors only.
4. Fail-closed test execution: pytest runs in isolated mode (python -I) from
   runner temp. Zero tests or unexpected skips fail closed with non-zero exit.
5. Evidence archiving: logs, environment, module provenance, wheel hash, and
   machine-readable results are preserved under .verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Maximum log size captured per suite (512 KiB) to prevent unbound disk growth
MAX_LOG_BYTES = 512 * 1024

# Minimum Blender version supported by the Blender adapter (src/gamefactory/adapters/dcc/blender_environment.py)
MIN_SUPPORTED_BLENDER_VERSION: tuple[int, int, int] = (4, 0, 2)

# Official pinned Godot version expected in CI
DEFAULT_EXPECTED_GODOT_VERSION: str = "4.7.2.stable.official.ed1daf0bf"

# Live provider secret environment variable names to scrub and verify unset
LIVE_PROVIDER_SECRET_KEYS = (
    "MESHY_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "STABILITY_API_KEY",
    "REPLICATE_API_TOKEN",
    "TRIPO_API_KEY",
    "RODIN_API_KEY",
)

# Reserved device names in Windows file systems (DOS device names)
WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

# Conservative single-component ASCII run ID pattern
RUN_ID_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_run_id(run_id: str) -> bool:
    """Validate that run_id is a conservative, nonempty, single-component ASCII identifier.

    Rejects paths with slashes, colons, drive-relative prefixes, dots, spaces,
    and Windows-reserved device names.
    """
    if not run_id:
        return False
    if not RUN_ID_PATTERN.fullmatch(run_id):
        return False
    if run_id.endswith(".") or run_id.endswith(" "):
        return False
    if run_id.upper() in WINDOWS_RESERVED_NAMES:
        return False
    return True


@dataclass(frozen=True)
class SuiteDefinition:
    suite_id: str
    name: str
    rel_path: str
    required: bool


# Canonical V0.7 integration suites
V07_SUITES: tuple[SuiteDefinition, ...] = (
    SuiteDefinition(
        suite_id="standalone_character_blender",
        name="Standalone Character Blender Processing",
        rel_path="tests/integration/test_blender_character_processor.py",
        required=True,
    ),
    SuiteDefinition(
        suite_id="standalone_character_godot",
        name="Standalone Character Godot Runtime & Composition",
        rel_path="tests/integration/test_godot_character_real.py",
        required=True,
    ),
    SuiteDefinition(
        suite_id="vehicle_profile_runtime",
        name="Vehicle Profile Blender/Godot Acceptance",
        rel_path="tests/integration/test_vehicle_profile_runtime.py",
        required=True,
    ),
    SuiteDefinition(
        suite_id="assembly_blender",
        name="Local Assembly Authored Processing in Blender",
        rel_path="tests/integration/test_blender_assembly.py",
        required=True,
    ),
    SuiteDefinition(
        suite_id="assembly_godot",
        name="Local Assembly Standalone Godot Runtime Verification",
        rel_path="tests/integration/test_godot_assembly_real.py",
        required=True,
    ),
    SuiteDefinition(
        suite_id="weapon_aircraft_fullworkflow",
        name="Weapon & Aircraft Local Assembly Full Workflow",
        rel_path="tests/integration/test_weapon_aircraft_profiles_runtime.py",
        required=True,
    ),
    SuiteDefinition(
        suite_id="provider_character_evidence",
        name="Provider Character Cold-Verified Evidence",
        rel_path="tests/integration/test_provider_character_evidence.py",
        required=True,
    ),
    SuiteDefinition(
        suite_id="character_paid_workflow",
        name="Character Paid Workflow with Real DCC Gates",
        rel_path="tests/integration/test_v07_character_paid_workflow.py",
        required=True,
    ),
    SuiteDefinition(
        suite_id="character_cli_runtime",
        name="Packaged Character CLI Nine-View Runtime",
        rel_path="tests/integration/test_v07_character_cli_runtime.py",
        required=True,
    ),
    SuiteDefinition(
        suite_id="public_assembly_cli",
        name="Public Vehicle, Weapon & Aircraft CLI Journeys",
        rel_path="tests/integration/test_v07_public_assembly_cli.py",
        required=True,
    ),
)


@dataclass
class TestCaseResult:
    name: str
    classname: str
    time_seconds: float
    status: str  # "passed", "failed", "error", "skipped"
    message: str | None = None


@dataclass
class SuiteRunResult:
    suite_id: str
    name: str
    rel_path: str
    required: bool
    status: str  # "PASS", "FAIL", "SKIPPED", "OMITTED"
    exit_code: int
    total_tests: int
    passed_tests: int
    failed_tests: int
    errored_tests: int
    skipped_tests: int
    duration_seconds: float
    command: list[str]
    log_path: str | None
    junit_path: str | None
    error_message: str | None = None
    cases: list[TestCaseResult] | None = None


class VerificationError(RuntimeError):
    """Raised on fail-closed preflight or execution verification failure."""


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def scrub_provider_secrets(env: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Remove live-provider secret environment variables and return cleaned env and scrubbed keys."""
    cleaned = dict(env)
    scrubbed: list[str] = []
    for key in list(cleaned.keys()):
        upper_key = key.upper()
        if key in LIVE_PROVIDER_SECRET_KEYS or any(
            marker in upper_key for marker in ("MESHY", "API_KEY", "SECRET_KEY")
        ):
            # Do not scrub non-provider GitHub or system vars
            if upper_key in ("GITHUB_WORKSPACE", "RUNNER_TEMP", "GITHUB_ENV"):
                continue
            cleaned.pop(key, None)
            scrubbed.append(key)
    return cleaned, sorted(scrubbed)


def find_blender(custom_path: Path | None = None) -> Path | None:
    if custom_path:
        p = custom_path.resolve()
        return p if p.is_file() else None
    env_blender = os.environ.get("GAMEFACTORY_TEST_BLENDER") or os.environ.get(
        "GAMEFACTORY_BLENDER_PATH"
    )
    if env_blender:
        p = Path(env_blender).resolve()
        if p.is_file():
            return p
    which_blender = shutil.which("blender")
    if which_blender:
        return Path(which_blender).resolve()
    # Common locations
    for loc in (
        Path("/usr/bin/blender"),
        Path(r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"),
    ):
        if loc.is_file():
            return loc.resolve()
    return None


def find_godot(custom_path: Path | None = None) -> Path | None:
    if custom_path:
        p = custom_path.resolve()
        return p if p.is_file() else None
    env_godot = os.environ.get("GAMEFACTORY_TEST_GODOT") or os.environ.get("GAMEFACTORY_GODOT_PATH")
    if env_godot:
        p = Path(env_godot).resolve()
        if p.is_file():
            return p
    which_godot = shutil.which("godot")
    if which_godot:
        return Path(which_godot).resolve()
    return None


def parse_blender_version_safe(
    version_output: str, max_chars: int = 4096
) -> tuple[int, int, int, str]:
    """Parse and validate Blender version string safely with finite parsing limits."""
    finite_text = version_output[:max_chars].strip()
    if not finite_text:
        raise VerificationError("Blender returned empty version output")

    first_line = finite_text.splitlines()[0][:256].strip()
    match = re.match(r"^Blender\s+(\d+)\.(\d+)(?:\.(\d+))?", first_line)
    if not match:
        raise VerificationError(
            f"Blender output does not contain recognized version string: {first_line!r}"
        )
    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3)) if match.group(3) is not None else 0
    return major, minor, patch, first_line


def parse_godot_version_safe(version_output: str, max_chars: int = 4096) -> str:
    """Parse and validate Godot version string safely with finite parsing limits."""
    finite_text = version_output[:max_chars].strip()
    if not finite_text:
        raise VerificationError("Godot returned empty version output")

    first_line = finite_text.splitlines()[0][:256].strip()
    if not re.match(r"^\d+\.\d+(?:\.\d+)?(?:\..+)?$", first_line):
        raise VerificationError(
            f"Godot output does not contain recognized version string: {first_line!r}"
        )
    return first_line


def inspect_tool_versions(
    blender_exe: Path,
    godot_exe: Path,
    expected_godot_version: str | None = None,
    min_blender_version: tuple[int, int, int] = MIN_SUPPORTED_BLENDER_VERSION,
) -> tuple[dict[str, Any], dict[str, Any]]:
    # Blender inspection
    try:
        proc = subprocess.run(
            [str(blender_exe), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode != 0:
            raise VerificationError(
                f"Blender --version failed with exit {proc.returncode}: {proc.stderr}"
            )
        major, minor, patch, blender_version = parse_blender_version_safe(proc.stdout or "")
        if (major, minor, patch) < min_blender_version:
            min_str = ".".join(str(x) for x in min_blender_version)
            raise VerificationError(
                f"Blender version '{blender_version}' ({major}.{minor}.{patch}) is below documented adapter minimum ({min_str})"
            )
    except VerificationError:
        raise
    except Exception as exc:
        raise VerificationError(f"Could not execute Blender at {blender_exe}: {exc}") from exc

    blender_info = {
        "executable": str(blender_exe),
        "available": True,
        "version_string": blender_version,
        "parsed_version": [major, minor, patch],
    }

    # Godot inspection
    try:
        proc = subprocess.run(
            [str(godot_exe), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode != 0:
            raise VerificationError(
                f"Godot --version failed with exit {proc.returncode}: {proc.stderr}"
            )
        godot_version = parse_godot_version_safe(proc.stdout or "")
        target_godot_version = (
            expected_godot_version
            or os.environ.get("GODOT_EXPECTED_VERSION")
            or DEFAULT_EXPECTED_GODOT_VERSION
        )
        if godot_version != target_godot_version:
            raise VerificationError(
                f"Godot version '{godot_version}' does not match expected official version '{target_godot_version}'"
            )
    except VerificationError:
        raise
    except Exception as exc:
        raise VerificationError(f"Could not execute Godot at {godot_exe}: {exc}") from exc

    godot_hash = (
        hashlib.sha256(godot_exe.read_bytes()).hexdigest() if godot_exe.is_file() else "unavailable"
    )
    godot_info = {
        "executable": str(godot_exe),
        "available": True,
        "version_string": godot_version,
        "sha256": godot_hash,
        "expected_version": target_godot_version,
    }

    return blender_info, godot_info


def inspect_module_provenance(
    python_exe: Path,
    repo_root: Path,
    allow_source_checkout: bool = False,
) -> dict[str, Any]:
    """Execute a python -I probe to ensure gamefactory imports from installed site-packages."""
    probe_code = (
        "import json, sys, os\n"
        "try:\n"
        "    import gamefactory\n"
        "    mod_file = str(Path(gamefactory.__file__).resolve())\n"
        "    ver = str(getattr(gamefactory, '__version__', 'unknown'))\n"
        "except Exception as e:\n"
        "    mod_file = None\n"
        "    ver = None\n"
        "print(json.dumps({\n"
        "    'module_path': mod_file,\n"
        "    'version': ver,\n"
        "    'sys_prefix': sys.prefix,\n"
        "    'sys_base_prefix': sys.base_prefix,\n"
        "    'sys_executable': sys.executable,\n"
        "    'sys_path': sys.path,\n"
        "}))\n"
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        res = subprocess.run(
            [str(python_exe), "-I", "-c", "from pathlib import Path\n" + probe_code],
            cwd=temp_dir,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    if res.returncode != 0:
        raise VerificationError(
            f"Python interpreter probe failed with exit {res.returncode}: {res.stderr}"
        )

    try:
        parsed = json.loads(res.stdout.strip().splitlines()[-1])
        if not isinstance(parsed, dict):
            raise ValueError(f"Expected dict from probe, got {type(parsed).__name__}")
        data: dict[str, Any] = parsed
    except Exception as exc:
        raise VerificationError(
            f"Failed to parse python probe output: {res.stdout}\n{res.stderr}"
        ) from exc

    mod_file = data.get("module_path")
    if not mod_file:
        raise VerificationError(
            f"gamefactory module could not be imported in python -I from {python_exe}"
        )

    resolved_mod = Path(mod_file).resolve()
    resolved_src = (repo_root / "src").resolve()

    source_leak = False
    try:
        resolved_mod.relative_to(resolved_src)
        source_leak = True
    except ValueError:
        source_leak = False

    is_site_packages = any(
        part in ("site-packages", "dist-packages") for part in resolved_mod.parts
    )

    data["source_checkout_leak"] = source_leak
    data["inside_site_packages"] = is_site_packages

    if source_leak and not allow_source_checkout:
        raise VerificationError(
            f"FAIL-CLOSED: gamefactory was imported from source checkout ({resolved_mod}), "
            f"expected installed wheel in site-packages."
        )

    if not is_site_packages and not allow_source_checkout:
        raise VerificationError(
            f"FAIL-CLOSED: gamefactory was not imported from site-packages ({resolved_mod})."
        )

    return data


def parse_junit_xml(xml_path: Path) -> tuple[int, int, int, int, float, list[TestCaseResult]]:
    """Parse pytest JUnit XML report safely and return test counts and cases."""
    if not xml_path.is_file():
        raise VerificationError(f"JUnit XML report not found at {xml_path}")

    try:
        tree = ET.parse(xml_path)
    except Exception as exc:
        raise VerificationError(f"Malformed JUnit XML report at {xml_path}: {exc}") from exc

    root = tree.getroot()
    suites: list[ET.Element] = []
    if root.tag == "testsuite":
        suites.append(root)
    elif root.tag == "testsuites":
        suites.extend(root.findall("testsuite"))
    else:
        raise VerificationError(
            f"Unrecognized root tag in JUnit XML report at {xml_path}: {root.tag}"
        )

    try:
        total_tests = sum(int(s.attrib.get("tests", 0)) for s in suites)
        failures = sum(int(s.attrib.get("failures", 0)) for s in suites)
        errors = sum(int(s.attrib.get("errors", 0)) for s in suites)
        skipped = sum(int(s.attrib.get("skipped", 0)) for s in suites)
        suite_durations = [float(s.attrib.get("time", 0.0)) for s in suites]
    except (ValueError, TypeError) as exc:
        raise VerificationError(
            f"Malformed numeric attributes in JUnit XML at {xml_path}: {exc}"
        ) from exc

    if total_tests < 0 or failures < 0 or errors < 0 or skipped < 0:
        raise VerificationError(f"Negative count or duration in JUnit XML at {xml_path}")
    if any(not math.isfinite(value) or value < 0 for value in suite_durations):
        raise VerificationError(f"Invalid suite duration in JUnit XML at {xml_path}")
    duration = sum(suite_durations)
    if not math.isfinite(duration):
        raise VerificationError(f"Invalid total duration in JUnit XML at {xml_path}")

    cases: list[TestCaseResult] = []
    for s in suites:
        for tc in s.findall("testcase"):
            name = tc.attrib.get("name", "unknown")
            classname = tc.attrib.get("classname", "unknown")
            try:
                tc_time = float(tc.attrib.get("time", 0.0))
            except (ValueError, TypeError) as exc:
                raise VerificationError(f"Malformed testcase time in JUnit XML: {exc}") from exc
            if not math.isfinite(tc_time) or tc_time < 0:
                raise VerificationError("Invalid testcase time in JUnit XML")
            if tc.find("failure") is not None:
                fail_elem = tc.find("failure")
                msg = fail_elem.attrib.get("message") if fail_elem is not None else None
                cases.append(TestCaseResult(name, classname, tc_time, "failed", msg))
            elif tc.find("error") is not None:
                err_elem = tc.find("error")
                msg = err_elem.attrib.get("message") if err_elem is not None else None
                cases.append(TestCaseResult(name, classname, tc_time, "error", msg))
            elif tc.find("skipped") is not None:
                skip_elem = tc.find("skipped")
                msg = skip_elem.attrib.get("message") if skip_elem is not None else None
                cases.append(TestCaseResult(name, classname, tc_time, "skipped", msg))
            else:
                cases.append(TestCaseResult(name, classname, tc_time, "passed", None))

    # Coherence validation: require actual cases > 0 and coherent counts
    if not cases:
        raise VerificationError(f"JUnit XML contains zero test cases: {xml_path}")

    if len(cases) != total_tests:
        raise VerificationError(
            f"Incoherent test counts in {xml_path}: header tests={total_tests} but found {len(cases)} testcase elements"
        )

    actual_failures = sum(1 for c in cases if c.status == "failed")
    if actual_failures != failures:
        raise VerificationError(
            f"Incoherent failure counts in {xml_path}: header failures={failures} but found {actual_failures} failed cases"
        )

    actual_errors = sum(1 for c in cases if c.status == "error")
    if actual_errors != errors:
        raise VerificationError(
            f"Incoherent error counts in {xml_path}: header errors={errors} but found {actual_errors} error cases"
        )

    actual_skipped = sum(1 for c in cases if c.status == "skipped")
    if actual_skipped != skipped:
        raise VerificationError(
            f"Incoherent skipped counts in {xml_path}: header skipped={skipped} but found {actual_skipped} skipped cases"
        )

    actual_passed = sum(1 for c in cases if c.status == "passed")
    if actual_passed + actual_failures + actual_errors + actual_skipped != total_tests:
        raise VerificationError(
            f"Incoherent test counts sum in {xml_path}: passed ({actual_passed}) + failed ({actual_failures}) + errors ({actual_errors}) + skipped ({actual_skipped}) != total ({total_tests})"
        )

    return total_tests, failures, errors, skipped, duration, cases


def run_suite(
    suite: SuiteDefinition,
    python_exe: Path,
    repo_root: Path,
    evidence_dir: Path,
    run_dir: Path,
    base_env: dict[str, str],
    timeout_seconds: int = 300,
    allow_skip: bool = False,
    junit_path: Path | None = None,
    log_path: Path | None = None,
) -> SuiteRunResult:
    """Run a single integration test suite using pytest in isolated mode."""
    test_file = repo_root / suite.rel_path
    if not test_file.is_file():
        if suite.required:
            return SuiteRunResult(
                suite_id=suite.suite_id,
                name=suite.name,
                rel_path=suite.rel_path,
                required=True,
                status="FAIL",
                exit_code=1,
                total_tests=0,
                passed_tests=0,
                failed_tests=0,
                errored_tests=0,
                skipped_tests=0,
                duration_seconds=0.0,
                command=[],
                log_path=None,
                junit_path=None,
                error_message=f"Required test suite file not found: {suite.rel_path}",
            )
        else:
            return SuiteRunResult(
                suite_id=suite.suite_id,
                name=suite.name,
                rel_path=suite.rel_path,
                required=False,
                status="OMITTED",
                exit_code=0,
                total_tests=0,
                passed_tests=0,
                failed_tests=0,
                errored_tests=0,
                skipped_tests=0,
                duration_seconds=0.0,
                command=[],
                log_path=None,
                junit_path=None,
                error_message=f"Conditional test suite file does not exist: {suite.rel_path}",
            )

    reports_dir = evidence_dir / "reports"
    logs_dir = evidence_dir / "logs"
    reports_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Fresh uniquely-owned per-run report and log paths if not explicitly given
    run_token = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
    target_junit = (
        junit_path if junit_path is not None else reports_dir / f"{suite.suite_id}_{run_token}.xml"
    )
    target_log = (
        log_path if log_path is not None else logs_dir / f"{suite.suite_id}_{run_token}.log"
    )

    # Refuse existing without deleting foreign files
    if target_junit.exists():
        return SuiteRunResult(
            suite_id=suite.suite_id,
            name=suite.name,
            rel_path=suite.rel_path,
            required=suite.required,
            status="FAIL",
            exit_code=1,
            total_tests=0,
            passed_tests=0,
            failed_tests=0,
            errored_tests=0,
            skipped_tests=0,
            duration_seconds=0.0,
            command=[],
            log_path=str(target_log) if target_log.exists() else None,
            junit_path=str(target_junit),
            error_message=f"Report file {target_junit} already exists; refusing to overwrite without deleting foreign files",
        )

    cmd = [
        str(python_exe),
        "-I",
        "-m",
        "pytest",
        str(test_file),
        "-v",
        "-ra",
        "-p",
        "no:cacheprovider",
        f"--junitxml={target_junit}",
        "-o",
        "pythonpath=",
        "-o",
        "addopts=",
    ]

    # Scrub pytest environment variables to prevent inherited test selection or unexpected env plugins
    suite_env = dict(base_env)
    suite_env.pop("PYTHONPATH", None)
    suite_env.pop("PYTHONHOME", None)
    suite_env.pop("PYTEST_ADDOPTS", None)
    suite_env.pop("PYTEST_PLUGINS", None)
    suite_env.pop("PYTEST_CURRENT_TEST", None)
    suite_env.pop("PYTEST_DEBUG", None)
    suite_env.pop("PYTEST_DEBUG_TEMPROOT", None)
    suite_env.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    suite_env.pop("PYTEST_THEME", None)
    suite_env.pop("PYTEST_THEME_MODE", None)

    start_epoch = time.time()
    start_time = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=run_dir,
            env=suite_env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        duration = time.monotonic() - start_time
        exit_code = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start_time
        exit_code = 124
        out_str = (
            exc.stdout.decode("utf-8", errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        err_str = (
            exc.stderr.decode("utf-8", errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        stdout = out_str
        stderr = f"{err_str}\nTIMEOUT after {timeout_seconds}s"
    except Exception as exc:
        duration = time.monotonic() - start_time
        exit_code = 1
        stdout = ""
        stderr = f"Subprocess execution error: {exc}"

    # Bounded log writing with strict byte limits
    cmd_str = " ".join(cmd)
    bounded_stdout = (stdout or "")[: MAX_LOG_BYTES // 2]
    bounded_stderr = (stderr or "")[: MAX_LOG_BYTES // 2]
    log_text = (
        f"COMMAND: {cmd_str}\nCWD: {run_dir}\nEXIT_CODE: {exit_code}\n\n"
        f"=== STDOUT ===\n{bounded_stdout}\n\n=== STDERR ===\n{bounded_stderr}\n"
    )
    log_bytes = log_text.encode("utf-8", errors="replace")
    if len(log_bytes) > MAX_LOG_BYTES:
        trunc_marker = b"\n[... TRUNCATED DUE TO SIZE LIMIT ...]\n"
        log_bytes = log_bytes[: MAX_LOG_BYTES - len(trunc_marker)] + trunc_marker
    target_log.write_bytes(log_bytes)

    # Parse and validate fresh JUnit XML report
    total_tests = 0
    passed_tests = 0
    failed_tests = 0
    errored_tests = 0
    skipped_tests = 0
    cases: list[TestCaseResult] = []
    error_msg: str | None = None

    if not target_junit.is_file():
        status = "FAIL"
        error_msg = f"JUnit XML report not produced at {target_junit}"
    else:
        try:
            mtime = target_junit.stat().st_mtime
            if mtime < (start_epoch - 1.0):
                status = "FAIL"
                error_msg = f"JUnit XML report at {target_junit} is stale (mtime {mtime} < start {start_epoch})"
            else:
                total_tests, failed_tests, errored_tests, skipped_tests, _, cases = parse_junit_xml(
                    target_junit
                )
                passed_tests = total_tests - failed_tests - errored_tests - skipped_tests
                status = "PASS"
        except Exception as exc:
            status = "FAIL"
            error_msg = f"Failed to parse JUnit XML: {exc}"

    # Determine final suite status fail-closed
    if exit_code != 0:
        status = "FAIL"
        if not error_msg:
            error_msg = f"pytest exited with code {exit_code}"
    elif total_tests == 0 or len(cases) == 0:
        status = "FAIL"
        if not error_msg:
            error_msg = "Zero tests collected/executed in suite"
    elif failed_tests > 0 or errored_tests > 0:
        status = "FAIL"
        error_msg = f"Tests failed: {failed_tests} failures, {errored_tests} errors"
    elif skipped_tests > 0 and not allow_skip:
        status = "FAIL"
        error_msg = f"Unexpected skips detected: {skipped_tests} tests were skipped"

    return SuiteRunResult(
        suite_id=suite.suite_id,
        name=suite.name,
        rel_path=suite.rel_path,
        required=suite.required,
        status=status,
        exit_code=exit_code,
        total_tests=total_tests,
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        errored_tests=errored_tests,
        skipped_tests=skipped_tests,
        duration_seconds=duration,
        command=cmd,
        log_path=str(target_log) if target_log.is_file() else None,
        junit_path=str(target_junit) if target_junit.is_file() else None,
        error_message=error_msg,
        cases=cases,
    )


def verify_wheel_hash(
    wheel_path: Path | None,
    wheel_hash_file: Path | None,
    evidence_dir: Path,
    require_wheel: bool = True,
) -> str | None:
    """Verify and archive wheel hash evidence with real SHA-256 digest computation."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    if wheel_path is None:
        if require_wheel:
            raise VerificationError(
                "FAIL-CLOSED: Actual wheel file path is required for release verification. "
                "No digest comparison can be performed without the built wheel."
            )
        # In DEBUG non-release fallback: when no actual wheel is supplied,
        # return None and never fabricate or claim computed wheel SHA,
        # even if a supplied hash file exists.
        return None

    resolved_wheel = wheel_path.resolve()
    if not resolved_wheel.is_file():
        raise VerificationError(f"FAIL-CLOSED: Wheel file not found at {resolved_wheel}")

    # Compute actual SHA-256 digest from wheel file bytes
    computed_digest = hashlib.sha256(resolved_wheel.read_bytes()).hexdigest().lower()

    # If supplied hash file is present, compare digests
    if wheel_hash_file is not None:
        resolved_hash_file = wheel_hash_file.resolve()
        if not resolved_hash_file.is_file():
            raise VerificationError(
                f"FAIL-CLOSED: Supplied wheel hash file not found at {resolved_hash_file}"
            )
        hash_file_content = resolved_hash_file.read_text(encoding="utf-8").strip()
        if not hash_file_content:
            raise VerificationError(
                f"FAIL-CLOSED: Supplied wheel hash file is empty: {resolved_hash_file}"
            )
        expected_digest = hash_file_content.split()[0].strip().lower()
        if not re.match(r"^[0-9a-fA-F]{64}$", expected_digest):
            raise VerificationError(
                f"FAIL-CLOSED: Supplied wheel hash file does not contain a valid 64-character SHA-256 digest: {expected_digest!r}"
            )
        if computed_digest != expected_digest:
            raise VerificationError(
                f"FAIL-CLOSED: Wheel SHA-256 digest mismatch! "
                f"Computed: {computed_digest} (from {resolved_wheel.name}), "
                f"Expected: {expected_digest} (from {resolved_hash_file.name})"
            )

    # Archive verified wheel hash
    (evidence_dir / "wheel.sha256").write_text(
        f"{computed_digest}  {resolved_wheel.name}\n", encoding="utf-8"
    )
    return computed_digest


def emit_preflight_failure_artifacts(
    run_evidence_dir: Path,
    run_id: str,
    error_msg: str,
    python_exe: Path,
    is_debug_run: bool,
    debug_flags: dict[str, Any],
    blender_info: dict[str, Any] | None = None,
    godot_info: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    wheel_sha256: str | None = None,
    scrubbed_keys: list[str] | None = None,
    blender_pythonpath: str | None = None,
) -> None:
    """Emit controlled fail results and provenance summaries on preflight rejection."""
    run_evidence_dir.mkdir(parents=True, exist_ok=True)
    now_iso = utc_now_iso()
    summary_data: dict[str, Any] = {
        "status": "FAIL",
        "release_gate": False,
        "release_mode": not is_debug_run,
        "debug_mode": is_debug_run,
        "debug_flags": debug_flags,
        "run_id": run_id,
        "timestamp": now_iso,
        "error_message": error_msg,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python_version": sys.version,
            "platform_string": platform.platform(),
        },
        "python_executable": str(python_exe),
        "module_provenance": provenance if provenance is not None else {"error": error_msg},
        "tools": {
            "blender": blender_info if blender_info is not None else {"available": False},
            "godot": godot_info if godot_info is not None else {"available": False},
            "blender_pythonpath": blender_pythonpath,
        },
        "secrets": {
            "live_provider_secrets_cleared": scrubbed_keys is not None,
            "scrubbed_keys": scrubbed_keys or [],
        },
        "summary": {
            "total_suites": 0,
            "passed_suites": 0,
            "failed_suites": 0,
            "total_tests": 0,
            "passed_tests": 0,
            "failed_tests": 0,
            "errored_tests": 0,
            "skipped_tests": 0,
        },
        "suites": [],
    }

    results_json = run_evidence_dir / "results.json"
    results_json.write_text(json.dumps(summary_data, indent=2, allow_nan=False), encoding="utf-8")

    provenance_json = run_evidence_dir / "provenance.json"
    provenance_json.write_text(
        json.dumps(
            {
                "provenance": provenance or {"error": error_msg},
                "tools": {
                    "blender": blender_info or {"available": False},
                    "godot": godot_info or {"available": False},
                },
                "wheel_sha256": wheel_sha256,
                "scrubbed_secrets": scrubbed_keys or [],
                "error": error_msg,
            },
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
    )

    env_txt = run_evidence_dir / "environment.txt"
    env_content = (
        f"timestamp={now_iso}\n"
        f"run_id={run_id}\n"
        f"platform={platform.platform()}\n"
        f"python={sys.version}\n"
        f"executable={python_exe}\n"
        f"gamefactory_module={provenance.get('module_path') if provenance else None}\n"
        f"gamefactory_version={provenance.get('version') if provenance else None}\n"
        f"blender_path={blender_info.get('executable') if blender_info else None}\n"
        f"blender_version={blender_info.get('version_string') if blender_info else None}\n"
        f"godot_path={godot_info.get('executable') if godot_info else None}\n"
        f"godot_version={godot_info.get('version_string') if godot_info else None}\n"
        f"godot_sha256={godot_info.get('sha256') if godot_info else None}\n"
        f"blender_pythonpath={blender_pythonpath}\n"
        f"wheel_sha256={wheel_sha256}\n"
        f"live_provider_secrets_scrubbed={scrubbed_keys or []}\n"
        f"error={error_msg}\n"
    )
    env_txt.write_text(env_content, encoding="utf-8")

    cmd_txt = run_evidence_dir / "command.txt"
    cmd_txt.write_text(f"driver: {' '.join(sys.argv)}\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify installed-wheel V0.7 acceptance offline with real Blender and Godot."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Path to repository root.",
    )
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="Path to Python interpreter in the target virtual environment.",
    )
    parser.add_argument(
        "--blender",
        type=Path,
        default=None,
        help="Path to real Blender executable.",
    )
    parser.add_argument(
        "--godot",
        type=Path,
        default=None,
        help="Path to real Godot executable.",
    )
    parser.add_argument(
        "--expected-godot-version",
        default=os.environ.get("GODOT_EXPECTED_VERSION") or DEFAULT_EXPECTED_GODOT_VERSION,
        help="Exact expected Godot version string.",
    )
    parser.add_argument(
        "--blender-pythonpath",
        default=os.environ.get("GAMEFACTORY_BLENDER_PYTHONPATH"),
        help="GAMEFACTORY_BLENDER_PYTHONPATH directory for numpy / Blender dependencies.",
    )
    parser.add_argument(
        "--wheel",
        type=Path,
        default=None,
        help="Path to installed wheel file.",
    )
    parser.add_argument(
        "--wheel-hash",
        type=Path,
        default=None,
        help="Path to file containing SHA-256 of the installed wheel.",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=Path(".verification/ci-v07-installed"),
        help="Base directory to write verification reports and evidence.",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Explicit run ID for exclusive run directory (defaults to auto-generated UUID).",
    )
    parser.add_argument(
        "--suite",
        action="append",
        dest="suites_filter",
        default=[],
        help="Specific suite ID to run (can be repeated). Defaults to all suites.",
    )
    parser.add_argument(
        "--allow-source-checkout",
        action="store_true",
        default=False,
        help="Permit gamefactory import from source checkout (local dev diagnostics only).",
    )
    parser.add_argument(
        "--allow-skip",
        action="store_true",
        default=False,
        help="Tolerate skipped tests instead of failing closed.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout in seconds per suite.",
    )

    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    # Keep a venv's invoked executable path. POSIX venv/bin/python is often a
    # symlink; resolving it can escape the venv and import a system install.
    python_exe = args.python.absolute()
    base_evidence_dir = (
        args.evidence_dir if args.evidence_dir.is_absolute() else (repo_root / args.evidence_dir)
    ).resolve()
    base_evidence_dir.mkdir(parents=True, exist_ok=True)

    if args.run_id:
        if not validate_run_id(args.run_id):
            print(f"ERROR: Invalid run ID {args.run_id!r}", file=sys.stderr)
            return 1
        run_id = args.run_id
    else:
        run_id = str(uuid.uuid4())

    candidate_dir = (base_evidence_dir / run_id).resolve()
    if candidate_dir.parent != base_evidence_dir:
        print(
            f"ERROR: Run ID {run_id!r} resolves outside base evidence directory {base_evidence_dir}",
            file=sys.stderr,
        )
        return 1

    run_evidence_dir = candidate_dir
    try:
        run_evidence_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print(
            f"ERROR: Run evidence directory {run_evidence_dir} already exists. "
            "Refusing path collision without deleting existing files.",
            file=sys.stderr,
        )
        return 1

    cleaned_env, scrubbed_keys = scrub_provider_secrets(dict(os.environ))

    suites_to_run: list[SuiteDefinition] = []
    for suite in V07_SUITES:
        if args.suites_filter and suite.suite_id not in args.suites_filter:
            continue
        suites_to_run.append(suite)

    required_suite_ids = {suite.suite_id for suite in V07_SUITES if suite.required}
    selected_suite_ids = {suite.suite_id for suite in suites_to_run}
    partial_suite_selection = not required_suite_ids.issubset(selected_suite_ids)
    is_debug_run = args.allow_source_checkout or args.allow_skip or partial_suite_selection

    debug_flags: dict[str, Any] = {
        "allow_source_checkout": args.allow_source_checkout,
        "allow_skip": args.allow_skip,
        "partial_suite_selection": partial_suite_selection,
        "unverified_wheel": args.wheel is None,
    }

    print("=== Starting V0.7 Installed-Wheel Offline Acceptance Verification ===")
    print(f"Timestamp: {utc_now_iso()}")
    print(f"Run ID: {run_id}")
    print(f"Repository Root: {repo_root}")
    print(f"Target Python: {python_exe}")
    print(f"Requested Evidence Base: {base_evidence_dir}")
    print(f"Run Evidence Directory: {run_evidence_dir}")

    # 1. Preflight: Tool discovery
    blender_exe = find_blender(args.blender)
    if not blender_exe:
        err = "Real Blender executable not found! Fail-closed."
        print(f"ERROR: {err}", file=sys.stderr)
        emit_preflight_failure_artifacts(
            run_evidence_dir=run_evidence_dir,
            run_id=run_id,
            error_msg=err,
            python_exe=python_exe,
            is_debug_run=is_debug_run,
            debug_flags=debug_flags,
            scrubbed_keys=scrubbed_keys,
            blender_pythonpath=args.blender_pythonpath,
        )
        print(f"Evidence Directory: {run_evidence_dir}")
        return 1

    godot_exe = find_godot(args.godot)
    if not godot_exe:
        err = "Real Godot executable not found! Fail-closed."
        print(f"ERROR: {err}", file=sys.stderr)
        emit_preflight_failure_artifacts(
            run_evidence_dir=run_evidence_dir,
            run_id=run_id,
            error_msg=err,
            python_exe=python_exe,
            is_debug_run=is_debug_run,
            debug_flags=debug_flags,
            blender_info={"executable": str(blender_exe), "available": True},
            scrubbed_keys=scrubbed_keys,
            blender_pythonpath=args.blender_pythonpath,
        )
        print(f"Evidence Directory: {run_evidence_dir}")
        return 1

    try:
        blender_info, godot_info = inspect_tool_versions(
            blender_exe, godot_exe, expected_godot_version=args.expected_godot_version
        )
    except VerificationError as exc:
        err = f"Tool version inspection failure: {exc}"
        print(f"ERROR: {err}", file=sys.stderr)
        emit_preflight_failure_artifacts(
            run_evidence_dir=run_evidence_dir,
            run_id=run_id,
            error_msg=err,
            python_exe=python_exe,
            is_debug_run=is_debug_run,
            debug_flags=debug_flags,
            blender_info={"executable": str(blender_exe), "available": True},
            godot_info={"executable": str(godot_exe), "available": True},
            scrubbed_keys=scrubbed_keys,
            blender_pythonpath=args.blender_pythonpath,
        )
        print(f"Evidence Directory: {run_evidence_dir}")
        return 1

    print(f"Blender: {blender_info['executable']} ({blender_info['version_string']})")
    print(f"Godot: {godot_info['executable']} ({godot_info['version_string']})")

    # 2. Preflight: Module provenance & isolation
    print("Checking module provenance under python -I...")
    try:
        provenance = inspect_module_provenance(
            python_exe, repo_root, allow_source_checkout=args.allow_source_checkout
        )
    except VerificationError as exc:
        err = f"Module provenance inspection failure: {exc}"
        print(f"ERROR: {err}", file=sys.stderr)
        emit_preflight_failure_artifacts(
            run_evidence_dir=run_evidence_dir,
            run_id=run_id,
            error_msg=err,
            python_exe=python_exe,
            is_debug_run=is_debug_run,
            debug_flags=debug_flags,
            blender_info=blender_info,
            godot_info=godot_info,
            scrubbed_keys=scrubbed_keys,
            blender_pythonpath=args.blender_pythonpath,
        )
        print(f"Evidence Directory: {run_evidence_dir}")
        return 1

    print(f"Installed module path: {provenance['module_path']}")
    print(f"Module version: {provenance['version']}")
    print(f"Inside site-packages: {provenance['inside_site_packages']}")
    print(f"Source checkout leak: {provenance['source_checkout_leak']}")

    # 4. Preflight: Wheel checksum
    print("Verifying wheel checksum...")
    try:
        wheel_sha256 = verify_wheel_hash(
            wheel_path=args.wheel,
            wheel_hash_file=args.wheel_hash,
            evidence_dir=run_evidence_dir,
            require_wheel=not is_debug_run,
        )
    except VerificationError as exc:
        err = f"Wheel verification failure: {exc}"
        print(f"ERROR: {err}", file=sys.stderr)
        emit_preflight_failure_artifacts(
            run_evidence_dir=run_evidence_dir,
            run_id=run_id,
            error_msg=err,
            python_exe=python_exe,
            is_debug_run=is_debug_run,
            debug_flags=debug_flags,
            blender_info=blender_info,
            godot_info=godot_info,
            provenance=provenance,
            scrubbed_keys=scrubbed_keys,
            blender_pythonpath=args.blender_pythonpath,
        )
        print(f"Evidence Directory: {run_evidence_dir}")
        return 1

    debug_flags["unverified_wheel"] = (args.wheel is None) or (wheel_sha256 is None)
    provenance["wheel_sha256"] = wheel_sha256
    if wheel_sha256:
        print(f"Wheel SHA-256: {wheel_sha256}")

    # 5. Construct isolated child environment
    print(f"Live provider secrets scrubbed: {scrubbed_keys}")

    test_env = dict(cleaned_env)
    test_env.pop("PYTHONPATH", None)
    test_env.pop("PYTHONHOME", None)
    test_env.pop("PYTEST_ADDOPTS", None)
    test_env.pop("PYTEST_PLUGINS", None)
    test_env["GAMEFACTORY_TEST_BLENDER"] = str(blender_exe)
    test_env["GAMEFACTORY_BLENDER_PATH"] = str(blender_exe)
    test_env["GAMEFACTORY_TEST_GODOT"] = str(godot_exe)
    test_env["GAMEFACTORY_GODOT_PATH"] = str(godot_exe)
    test_env["LIBGL_ALWAYS_SOFTWARE"] = "1"
    if args.blender_pythonpath:
        test_env["GAMEFACTORY_BLENDER_PYTHONPATH"] = args.blender_pythonpath

    # Ensure live provider secrets are verified absent
    assert not any(k in test_env for k in LIVE_PROVIDER_SECRET_KEYS)

    # Temporary execution directory outside repo root
    runner_temp = Path(tempfile.gettempdir()).resolve()

    executed_suites: list[SuiteRunResult] = []
    overall_pass = True

    print(f"\nExecuting {len(suites_to_run)} V0.7 test suites...")
    for suite in suites_to_run:
        print(f"\n--- Suite: {suite.name} ({suite.suite_id}) ---")
        res = run_suite(
            suite=suite,
            python_exe=python_exe,
            repo_root=repo_root,
            evidence_dir=run_evidence_dir,
            run_dir=runner_temp,
            base_env=test_env,
            timeout_seconds=args.timeout,
            allow_skip=args.allow_skip,
        )
        executed_suites.append(res)
        print(f"Outcome: {res.status} (exit {res.exit_code}) in {res.duration_seconds:.2f}s")
        print(
            f"Tests: {res.total_tests} | Passed: {res.passed_tests} | Failed: {res.failed_tests} | "
            f"Errors: {res.errored_tests} | Skipped: {res.skipped_tests}"
        )
        if res.error_message:
            print(f"Details: {res.error_message}")
        if res.status == "FAIL":
            overall_pass = False

    # 6. Aggregate results
    total_tests = sum(r.total_tests for r in executed_suites)
    total_passed = sum(r.passed_tests for r in executed_suites)
    total_failed = sum(r.failed_tests for r in executed_suites)
    total_errored = sum(r.errored_tests for r in executed_suites)
    total_skipped = sum(r.skipped_tests for r in executed_suites)
    total_suites = len(executed_suites)
    passed_suites = sum(1 for r in executed_suites if r.status == "PASS")
    failed_suites = sum(1 for r in executed_suites if r.status == "FAIL")

    if total_tests == 0:
        overall_pass = False

    if overall_pass:
        final_status = "DEBUG_PASS" if is_debug_run else "PASS"
    else:
        final_status = "FAIL"

    release_gate_bool = final_status == "PASS" and not is_debug_run

    summary_data = {
        "status": final_status,
        "release_gate": release_gate_bool,
        "release_mode": not is_debug_run,
        "debug_mode": is_debug_run,
        "debug_flags": {
            "allow_source_checkout": args.allow_source_checkout,
            "allow_skip": args.allow_skip,
            "partial_suite_selection": partial_suite_selection,
            "unverified_wheel": (args.wheel is None) or (wheel_sha256 is None),
        },
        "run_id": run_id,
        "timestamp": utc_now_iso(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python_version": sys.version,
            "platform_string": platform.platform(),
        },
        "python_executable": str(python_exe),
        "module_provenance": provenance,
        "tools": {
            "blender": blender_info,
            "godot": godot_info,
            "blender_pythonpath": args.blender_pythonpath,
        },
        "secrets": {
            "live_provider_secrets_cleared": True,
            "scrubbed_keys": scrubbed_keys,
        },
        "summary": {
            "total_suites": total_suites,
            "passed_suites": passed_suites,
            "failed_suites": failed_suites,
            "total_tests": total_tests,
            "passed_tests": total_passed,
            "failed_tests": total_failed,
            "errored_tests": total_errored,
            "skipped_tests": total_skipped,
        },
        "suites": [
            {
                "suite_id": r.suite_id,
                "name": r.name,
                "rel_path": r.rel_path,
                "required": r.required,
                "status": r.status,
                "exit_code": r.exit_code,
                "total_tests": r.total_tests,
                "passed_tests": r.passed_tests,
                "failed_tests": r.failed_tests,
                "errored_tests": r.errored_tests,
                "skipped_tests": r.skipped_tests,
                "duration_seconds": r.duration_seconds,
                "command": r.command,
                "log_path": r.log_path,
                "junit_path": r.junit_path,
                "error_message": r.error_message,
                "cases": [asdict(c) for c in (r.cases or [])],
            }
            for r in executed_suites
        ],
    }

    # Write evidence artifacts
    results_json = run_evidence_dir / "results.json"
    results_json.write_text(json.dumps(summary_data, indent=2, allow_nan=False), encoding="utf-8")

    provenance_json = run_evidence_dir / "provenance.json"
    provenance_json.write_text(
        json.dumps(
            {
                "provenance": provenance,
                "tools": {"blender": blender_info, "godot": godot_info},
                "wheel_sha256": wheel_sha256,
                "scrubbed_secrets": scrubbed_keys,
            },
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
    )

    env_txt = run_evidence_dir / "environment.txt"
    env_content = (
        f"timestamp={utc_now_iso()}\n"
        f"run_id={run_id}\n"
        f"platform={platform.platform()}\n"
        f"python={sys.version}\n"
        f"executable={python_exe}\n"
        f"gamefactory_module={provenance.get('module_path')}\n"
        f"gamefactory_version={provenance.get('version')}\n"
        f"blender_path={blender_info['executable']}\n"
        f"blender_version={blender_info['version_string']}\n"
        f"godot_path={godot_info['executable']}\n"
        f"godot_version={godot_info['version_string']}\n"
        f"godot_sha256={godot_info['sha256']}\n"
        f"blender_pythonpath={args.blender_pythonpath}\n"
        f"wheel_sha256={wheel_sha256}\n"
        f"live_provider_secrets_scrubbed={scrubbed_keys}\n"
    )
    env_txt.write_text(env_content, encoding="utf-8")

    cmd_txt = run_evidence_dir / "command.txt"
    cmd_lines = [f"driver: {' '.join(sys.argv)}"]
    for r in executed_suites:
        if r.command:
            cmd_lines.append(f"suite {r.suite_id}: {' '.join(r.command)}")
    cmd_txt.write_text("\n".join(cmd_lines) + "\n", encoding="utf-8")

    print("\n=== Verification Summary ===")
    if is_debug_run:
        print(f"Overall Status: {final_status} (DEBUG MODE ACTIVE - NOT A RELEASE PASS)")
    else:
        print(f"Overall Status: {final_status}")
    print(f"Run ID: {run_id}")
    print(f"Total Suites: {total_suites} (Passed: {passed_suites}, Failed: {failed_suites})")
    print(
        f"Total Tests: {total_tests} (Passed: {total_passed}, Failed: {total_failed}, "
        f"Errors: {total_errored}, Skipped: {total_skipped})"
    )
    print(f"Evidence Directory: {run_evidence_dir}")

    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
