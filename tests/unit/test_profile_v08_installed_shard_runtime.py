"""Regression tests for scripts.profile_v08_installed_shard_runtime."""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFICATION_REVISION_BASETEMP_PARENT = REPO_ROOT / ".verification" / "profiler-revision-check"
_SRC_ROOT = REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.profile_v08_installed_shard_runtime import (  # noqa: E402
    METRICS_FILLED_BY_COST_OBSERVATION,
    METRICS_FILLED_BY_WHEEL_OPTION,
    NAMED_COST_ENTRYPOINTS,
    OBSERVATION_SCOPE_PYTEST_SESSION,
    PROFILE_SHARD,
    CostObservationSession,
    NodeTimingRecord,
    PhaseOutcome,
    PytestShardResult,
    ShardTimingPlugin,
    _fresh_basetemp,
    _timing_sidecar_paths,
    aggregate_top10,
    argv_from_subprocess_popen_audit,
    build_named_entrypoint_coverage,
    build_report,
    classify_subprocess_category,
    compare_gamefactory_wheel_to_site_packages,
    cost_observation_metrics_satisfied,
    deactivate_cost_observation,
    expected_shard4_nodes,
    finalize_cost_observation,
    guard_collected_nodes,
    guard_output_evidence_paths,
    install_cost_observation,
    installed_probe_source,
    load_frozen_candidate_ci,
    load_timing_payload,
    metrics_missing_for_report,
    named_entrypoint_metrics_satisfied,
    provenance_snapshots_equal,
    run_installed_probe,
    run_profile,
    run_pytest_shard,
    sha256_file_path,
    spawn_label_for_subprocess_run,
    validate_cost_payload,
    validate_timing_payload,
    wheel_provenance_verified_for_metrics,
    write_report,
)
from scripts.verify_v08_candidate_ci import (  # noqa: E402
    EXPECTED_PACKAGE_VERSION,
    SHARD_MODULES,
    canonical_nodes_for_modules,
    expected_raw_count_for_modules,
    installed_package_outside_checkout_probe_source,
    load_canonical_slow_nodes,
)

_INTEGRATION_MODULE = "tests/unit/test_profile_child_integration.py"
_COST_PHASE_MODULE = "tests/unit/test_profile_child_cost_phases.py"
_SESSION_FINISH_MODULE = "tests/unit/test_profile_session_finish_subprocess.py"

_PYTEST_OFFLINE_DIST_NAMES: tuple[str, ...] = (
    "pytest",
    "pluggy",
    "packaging",
    "iniconfig",
    "pygments",
    "colorama",
)


def _copy_installed_distribution(dist_name: str, dest_site_packages: Path) -> None:
    dist = importlib.metadata.distribution(dist_name)
    files = dist.files
    if files is None:
        raise RuntimeError(f"distribution {dist_name!r} has no install file manifest")
    for rel in files:
        src = dist.locate_file(rel)
        dest = dest_site_packages / rel.as_posix()
        if src.is_dir():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def _copy_offline_pytest_runtime(dest_site_packages: Path) -> None:
    for name in _PYTEST_OFFLINE_DIST_NAMES:
        try:
            _copy_installed_distribution(name, dest_site_packages)
        except importlib.metadata.PackageNotFoundError:
            if name == "colorama":
                continue
            raise


def _venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _site_packages_dir(python: Path) -> Path:
    probe = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(probe.stdout.strip())


def _write_minimal_gamefactory_dist(
    site_packages: Path,
    *,
    version: str = EXPECTED_PACKAGE_VERSION,
    module_body: str | None = None,
) -> None:
    pkg_dir = site_packages / "gamefactory"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    body = module_body or f'__version__ = "{version}"\n'
    (pkg_dir / "__init__.py").write_text(body, encoding="utf-8")
    dist_info = site_packages / f"gamefactory-{version}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: gamefactory\nVersion: {version}\n",
        encoding="utf-8",
    )


def _bootstrap_consumer_venv_with_pytest(tmp_path: Path) -> tuple[Path, Path]:
    venv_dir, python = _bootstrap_isolated_venv(tmp_path)
    _copy_offline_pytest_runtime(_site_packages_dir(python))
    return venv_dir, python


def _bootstrap_isolated_venv(
    tmp_path: Path,
    *,
    inherit_site_packages: bool = False,
) -> tuple[Path, Path]:
    venv_dir = tmp_path / "consumer-venv"
    venv.create(
        venv_dir,
        with_pip=False,
        clear=True,
        system_site_packages=inherit_site_packages,
    )
    python = _venv_python(venv_dir)
    site_packages = _site_packages_dir(python)
    _write_minimal_gamefactory_dist(site_packages)
    return venv_dir, python


def _disjoint_consumer_workspace() -> Path:
    return Path(tempfile.mkdtemp(prefix="gf-profile-consumer-"))


def _outside_checkout_collect_cwd() -> Path:
    return Path(tempfile.gettempdir())


def _outside_checkout_profile_paths() -> tuple[Path, Path, Path]:
    base = Path(tempfile.mkdtemp(prefix="gf-profile-evidence-"))
    output = base / "out" / "report.json"
    junit = base / "out" / "junit.xml"
    basetemp_parent = base / "basetemp-parent"
    return output, junit, basetemp_parent


def _verification_revision_basetemp_parent() -> Path:
    parent = VERIFICATION_REVISION_BASETEMP_PARENT
    parent.mkdir(parents=True, exist_ok=True)
    return parent


def _outside_checkout_wheel_path(name: str = "gamefactory.whl") -> Path:
    """Wheel path disjoint from checkout so containment guard can run before SHA."""
    wheel_dir = Path(tempfile.mkdtemp(prefix="gf-profile-wheel-"))
    return wheel_dir / name


def _outside_checkout_consumer_bootstrap() -> tuple[Path, Path]:
    """Consumer venv outside checkout so site-packages passes wheel containment."""
    return _bootstrap_isolated_venv(Path(tempfile.mkdtemp(prefix="gf-profile-consumer-venv-")))


def _minimal_valid_timing_for_nodes(
    nodeids: frozenset[str],
    *,
    pytest_exit_code: int = 0,
) -> dict[str, object]:
    phases = [
        {"when": "setup", "outcome": "passed", "duration_seconds": 0.0},
        {"when": "call", "outcome": "passed", "duration_seconds": 0.001},
        {"when": "teardown", "outcome": "passed", "duration_seconds": 0.0},
    ]
    return {
        "nodes": [{"nodeid": nodeid, "phases": phases} for nodeid in sorted(nodeids)],
        "session_start_monotonic": 0.0,
        "session_end_monotonic": 1.0,
        "pytest_exitstatus": pytest_exit_code,
    }


def shard4_modules_names() -> tuple[str, ...]:
    return SHARD_MODULES[PROFILE_SHARD]


def _write_minimal_integration_frozen_root(root: Path, *, version: str) -> frozenset[str]:
    module_path = root / _INTEGRATION_MODULE
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_text(
        """import pytest


def test_passes() -> None:
    assert True


def test_fails() -> None:
    assert False


@pytest.mark.skip(reason="integration-skip")
def test_skipped() -> None:
    assert True
""",
        encoding="utf-8",
    )
    nodeids = frozenset(
        {
            f"{_INTEGRATION_MODULE}::test_passes",
            f"{_INTEGRATION_MODULE}::test_fails",
            f"{_INTEGRATION_MODULE}::test_skipped",
        }
    )
    fixtures = root / "tests" / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    (fixtures / "v08_candidate_slow_nodes.txt").write_text(
        "\n".join(sorted(nodeids)) + "\n", encoding="utf-8"
    )
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    probe_template = installed_package_outside_checkout_probe_source(version)
    stub = f'''"""Minimal frozen verify stub for profile child integration tests."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

EXPECTED_PACKAGE_VERSION = {version!r}
SHARD_MODULES = {{4: ("{_INTEGRATION_MODULE}",)}}
_PROBE_SOURCE = {probe_template!r}


class CandidateCiError(RuntimeError):
    pass


_COLLECTED_RE = re.compile(r"(?P<count>\\d+)(?:/\\d+)?\\s+tests?\\s+collected")


def _normalize_node_id(nodeid: str, repo: Path) -> str:
    stripped = nodeid.strip()
    if "::" not in stripped:
        return stripped.replace("\\\\", "/")
    file_part, rest = stripped.split("::", 1)
    path = Path(file_part)
    if path.is_absolute():
        try:
            file_part = path.resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            file_part = path.as_posix()
    else:
        file_part = file_part.replace("\\\\", "/")
    return f"{{file_part}}::{{rest}}"


def load_canonical_slow_nodes(repo: Path) -> frozenset[str]:
    path = repo / "tests" / "fixtures" / "v08_candidate_slow_nodes.txt"
    return frozenset(
        _normalize_node_id(line, repo)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def canonical_nodes_for_modules(canonical, modules):
    allowed = set(modules)
    return frozenset(n for n in canonical if n.split("::", 1)[0] in allowed)


def assert_runtime_slow_inventory_matches_canonical(runtime, canonical, *, label: str) -> None:
    if runtime != canonical:
        raise CandidateCiError(f"{{label}} identity mismatch")


def installed_package_outside_checkout_probe_source(expected_version: str = EXPECTED_PACKAGE_VERSION) -> str:
    return _PROBE_SOURCE


def _collect_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _parse_collect_only_stdout(stdout: str, repo: Path):
    nodeids = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or _COLLECTED_RE.search(stripped):
            continue
        if "::" not in stripped or ".py" not in stripped.split("::", 1)[0]:
            continue
        if stripped.startswith(("=", "-", "ERROR", "!!!")):
            continue
        nodeids.append(_normalize_node_id(stripped, repo))
    return frozenset(nodeids)


def run_pytest_collect_nodeids(python, repo, paths, *, cwd=None, outside_checkout=True):
    abs_paths = [str((repo / rel).resolve()) for rel in paths]
    cmd = [
        str(python),
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "--rootdir",
        str(repo.resolve()),
        "-p",
        "no:cacheprovider",
        "--import-mode",
        "importlib",
        "-o",
        f"pythonpath={{repo.resolve()}}",
        *abs_paths,
    ]
    workdir = cwd if cwd is not None else repo
    proc = subprocess.run(
        cmd,
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
        env=_collect_subprocess_env(),
    )
    if proc.returncode not in (0, 5):
        raise CandidateCiError(proc.stderr or proc.stdout)
    return _parse_collect_only_stdout(proc.stdout, repo)
'''
    (scripts / "verify_v08_candidate_ci.py").write_text(stub, encoding="utf-8")
    return nodeids


def _write_session_finish_subprocess_frozen_root(root: Path, *, version: str) -> frozenset[str]:
    conftest = root / "conftest.py"
    conftest.write_text(
        """import subprocess
import sys


def pytest_sessionfinish(session, exitstatus):
    subprocess.run(
        [sys.executable, "-c", "pass"],
        capture_output=True,
        text=True,
        check=False,
    )
""",
        encoding="utf-8",
    )
    module_path = root / _SESSION_FINISH_MODULE
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_text(
        """def test_trivial() -> None:
    assert True
""",
        encoding="utf-8",
    )
    nodeid = f"{_SESSION_FINISH_MODULE}::test_trivial"
    fixtures = root / "tests" / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    (fixtures / "v08_candidate_slow_nodes.txt").write_text(nodeid + "\n", encoding="utf-8")
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    probe_template = installed_package_outside_checkout_probe_source(version)
    stub = f'''"""Minimal frozen verify stub for profile session-finish subprocess tests."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

EXPECTED_PACKAGE_VERSION = {version!r}
SHARD_MODULES = {{4: ("{_SESSION_FINISH_MODULE}",)}}
_PROBE_SOURCE = {probe_template!r}


class CandidateCiError(RuntimeError):
    pass


_COLLECTED_RE = re.compile(r"(?P<count>\\d+)(?:/\\d+)?\\s+tests?\\s+collected")


def _normalize_node_id(nodeid: str, repo: Path) -> str:
    stripped = nodeid.strip()
    if "::" not in stripped:
        return stripped.replace("\\\\", "/")
    file_part, rest = stripped.split("::", 1)
    path = Path(file_part)
    if path.is_absolute():
        try:
            file_part = path.resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            file_part = path.as_posix()
    else:
        file_part = file_part.replace("\\\\", "/")
    return f"{{file_part}}::{{rest}}"


def load_canonical_slow_nodes(repo: Path) -> frozenset[str]:
    path = repo / "tests" / "fixtures" / "v08_candidate_slow_nodes.txt"
    return frozenset(
        _normalize_node_id(line, repo)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def canonical_nodes_for_modules(canonical, modules):
    allowed = set(modules)
    return frozenset(n for n in canonical if n.split("::", 1)[0] in allowed)


def assert_runtime_slow_inventory_matches_canonical(runtime, canonical, *, label: str) -> None:
    if runtime != canonical:
        raise CandidateCiError(f"{{label}} identity mismatch")


def installed_package_outside_checkout_probe_source(expected_version: str = EXPECTED_PACKAGE_VERSION) -> str:
    return _PROBE_SOURCE


def _collect_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _parse_collect_only_stdout(stdout: str, repo: Path):
    nodeids = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or _COLLECTED_RE.search(stripped):
            continue
        if "::" not in stripped or ".py" not in stripped.split("::", 1)[0]:
            continue
        if stripped.startswith(("=", "-", "ERROR", "!!!")):
            continue
        nodeids.append(_normalize_node_id(stripped, repo))
    return frozenset(nodeids)


def run_pytest_collect_nodeids(python, repo, paths, *, cwd=None, outside_checkout=True):
    abs_paths = [str((repo / rel).resolve()) for rel in paths]
    cmd = [
        str(python),
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "--rootdir",
        str(repo.resolve()),
        "-p",
        "no:cacheprovider",
        "--import-mode",
        "importlib",
        "-o",
        f"pythonpath={{repo.resolve()}}",
        *abs_paths,
    ]
    workdir = cwd if cwd is not None else repo
    proc = subprocess.run(
        cmd,
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
        env=_collect_subprocess_env(),
    )
    if proc.returncode not in (0, 5):
        raise CandidateCiError(proc.stderr or proc.stdout)
    return _parse_collect_only_stdout(proc.stdout, repo)
'''
    (scripts / "verify_v08_candidate_ci.py").write_text(stub, encoding="utf-8")
    return frozenset({nodeid})


def _write_cost_phase_integration_frozen_root(root: Path, *, version: str) -> frozenset[str]:
    module_path = root / _COST_PHASE_MODULE
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_text(
        """import subprocess
import sys

import pytest


def _spawn() -> None:
    subprocess.run(
        [sys.executable, "-c", "pass"],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def spawn_each_phase():
    _spawn()
    yield
    _spawn()


def test_spawn_in_call(spawn_each_phase) -> None:
    _spawn()
""",
        encoding="utf-8",
    )
    nodeid = f"{_COST_PHASE_MODULE}::test_spawn_in_call"
    fixtures = root / "tests" / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    (fixtures / "v08_candidate_slow_nodes.txt").write_text(nodeid + "\n", encoding="utf-8")
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    probe_template = installed_package_outside_checkout_probe_source(version)
    stub = f'''"""Minimal frozen verify stub for profile cost phase tests."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

EXPECTED_PACKAGE_VERSION = {version!r}
SHARD_MODULES = {{4: ("{_COST_PHASE_MODULE}",)}}
_PROBE_SOURCE = {probe_template!r}


class CandidateCiError(RuntimeError):
    pass


_COLLECTED_RE = re.compile(r"(?P<count>\\d+)(?:/\\d+)?\\s+tests?\\s+collected")


def _normalize_node_id(nodeid: str, repo: Path) -> str:
    stripped = nodeid.strip()
    if "::" not in stripped:
        return stripped.replace("\\\\", "/")
    file_part, rest = stripped.split("::", 1)
    path = Path(file_part)
    if path.is_absolute():
        try:
            file_part = path.resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            file_part = path.as_posix()
    else:
        file_part = file_part.replace("\\\\", "/")
    return f"{{file_part}}::{{rest}}"


def load_canonical_slow_nodes(repo: Path) -> frozenset[str]:
    path = repo / "tests" / "fixtures" / "v08_candidate_slow_nodes.txt"
    return frozenset(
        _normalize_node_id(line, repo)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def canonical_nodes_for_modules(canonical, modules):
    allowed = set(modules)
    return frozenset(n for n in canonical if n.split("::", 1)[0] in allowed)


def assert_runtime_slow_inventory_matches_canonical(runtime, canonical, *, label: str) -> None:
    if runtime != canonical:
        raise CandidateCiError(f"{{label}} identity mismatch")


def installed_package_outside_checkout_probe_source(expected_version: str = EXPECTED_PACKAGE_VERSION) -> str:
    return _PROBE_SOURCE


def _collect_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _parse_collect_only_stdout(stdout: str, repo: Path):
    nodeids = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or _COLLECTED_RE.search(stripped):
            continue
        if "::" not in stripped or ".py" not in stripped.split("::", 1)[0]:
            continue
        if stripped.startswith(("=", "-", "ERROR", "!!!")):
            continue
        nodeids.append(_normalize_node_id(stripped, repo))
    return frozenset(nodeids)


def run_pytest_collect_nodeids(python, repo, paths, *, cwd=None, outside_checkout=True):
    abs_paths = [str((repo / rel).resolve()) for rel in paths]
    cmd = [
        str(python),
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "--rootdir",
        str(repo.resolve()),
        "-p",
        "no:cacheprovider",
        "--import-mode",
        "importlib",
        "-o",
        f"pythonpath={{repo.resolve()}}",
        *abs_paths,
    ]
    workdir = cwd if cwd is not None else repo
    proc = subprocess.run(
        cmd,
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
        env=_collect_subprocess_env(),
    )
    if proc.returncode not in (0, 5):
        raise CandidateCiError(proc.stderr or proc.stdout)
    return _parse_collect_only_stdout(proc.stdout, repo)
'''
    (scripts / "verify_v08_candidate_ci.py").write_text(stub, encoding="utf-8")
    return frozenset({nodeid})


def test_shard4_canonical_inventory_is_106_nodes() -> None:
    ci = load_frozen_candidate_ci(REPO_ROOT)
    modules = SHARD_MODULES[PROFILE_SHARD]
    assert modules == tuple(shard4_modules_names())
    canonical = load_canonical_slow_nodes(REPO_ROOT)
    expected = canonical_nodes_for_modules(canonical, modules)
    assert len(expected) == 106
    assert len(expected) == expected_raw_count_for_modules(modules)
    assert expected == expected_shard4_nodes(ci, REPO_ROOT)


def test_installed_probe_rejects_wrong_version(tmp_path: Path) -> None:
    workspace = tmp_path / "outside-workspace"
    workspace.mkdir()
    venv_dir = tmp_path / "venv"
    venv.create(venv_dir, with_pip=False, clear=True)
    python = _venv_python(venv_dir)
    _write_minimal_gamefactory_dist(
        _site_packages_dir(python),
        module_body='__version__ = "0.0.0"\n',
    )
    with pytest.raises(Exception, match="provenance check failed|installed package provenance"):
        run_installed_probe(
            python,
            workspace=workspace,
            test_root=REPO_ROOT,
            expected_version=EXPECTED_PACKAGE_VERSION,
            cwd=tmp_path,
        )


def test_installed_probe_rejects_gamefactory_under_test_root_src(tmp_path: Path) -> None:
    workspace = tmp_path / "outside-workspace"
    workspace.mkdir()
    fake_root = tmp_path / "frozen-root"
    fake_root.mkdir()
    (fake_root / "scripts").mkdir()
    probe_donor = REPO_ROOT / "scripts" / "verify_v08_candidate_ci.py"
    (fake_root / "scripts" / "verify_v08_candidate_ci.py").write_text(
        probe_donor.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    src_pkg = fake_root / "src" / "gamefactory"
    src_pkg.mkdir(parents=True)
    (src_pkg / "__init__.py").write_text(
        f'__version__ = "{EXPECTED_PACKAGE_VERSION}"\n',
        encoding="utf-8",
    )

    venv_dir = tmp_path / "venv"
    venv.create(venv_dir, with_pip=False, clear=True)
    python = _venv_python(venv_dir)
    site_packages = _site_packages_dir(python)
    dist_info = site_packages / f"gamefactory-{EXPECTED_PACKAGE_VERSION}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: gamefactory\nVersion: {EXPECTED_PACKAGE_VERSION}\n",
        encoding="utf-8",
    )
    (site_packages / "src-tree.pth").write_text(str(fake_root / "src") + "\n", encoding="utf-8")

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["GAMEFACTORY_CI_WORKSPACE"] = str(workspace.resolve())
    env["GAMEFACTORY_CI_TEST_ROOT"] = str(fake_root.resolve())
    probe = installed_probe_source(EXPECTED_PACKAGE_VERSION, fake_root)
    proc = subprocess.run(
        [str(python), "-I", "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode != 0
    combined = f"{proc.stdout}\n{proc.stderr}"
    assert "src" in combined.lower() or "AssertionError" in combined


def test_collect_guard_mismatch_skips_pytest(tmp_path: Path) -> None:
    workspace = _disjoint_consumer_workspace()
    _, python = _bootstrap_isolated_venv(tmp_path)
    output, junit, basetemp = _outside_checkout_profile_paths()
    ci = load_frozen_candidate_ci(REPO_ROOT)
    expected = expected_shard4_nodes(ci, REPO_ROOT)
    swapped = frozenset(
        (expected - {next(iter(expected))})
        | {"tests/unit/test_v08_candidate_evidence_readiness_controls.py::bogus"}
    )
    with (
        patch(
            "scripts.profile_v08_installed_shard_runtime.run_pytest_collect_shard",
            return_value=swapped,
        ),
        patch(
            "scripts.profile_v08_installed_shard_runtime.run_pytest_shard",
        ) as run_shard,
    ):
        rc = run_profile(
            python=python,
            test_root=REPO_ROOT,
            workspace=workspace,
            output_json=output,
            junit_xml=junit,
            basetemp_parent=basetemp,
            expected_version=EXPECTED_PACKAGE_VERSION,
            collect_cwd=_outside_checkout_collect_cwd(),
            skip_pytest=True,
        )
        run_shard.assert_not_called()
    assert rc == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["collect_guard_ok"] is False
    assert payload["pytest_exit_code"] is None
    assert not junit.is_file()


def test_report_preserves_failed_and_skipped_phase_outcomes() -> None:
    nodes = [
        {
            "nodeid": "tests/unit/a.py::test_ok",
            "phases": [
                {"when": "setup", "outcome": "passed", "duration_seconds": 0.01, "longrepr": None},
                {"when": "call", "outcome": "passed", "duration_seconds": 0.2, "longrepr": None},
                {
                    "when": "teardown",
                    "outcome": "passed",
                    "duration_seconds": 0.01,
                    "longrepr": None,
                },
            ],
        },
        {
            "nodeid": "tests/unit/b.py::test_fail",
            "phases": [
                {"when": "setup", "outcome": "passed", "duration_seconds": 0.01, "longrepr": None},
                {
                    "when": "call",
                    "outcome": "failed",
                    "duration_seconds": 0.5,
                    "longrepr": "AssertionError: boom",
                },
                {
                    "when": "teardown",
                    "outcome": "passed",
                    "duration_seconds": 0.01,
                    "longrepr": None,
                },
            ],
        },
        {
            "nodeid": "tests/unit/c.py::test_skip",
            "phases": [
                {
                    "when": "setup",
                    "outcome": "skipped",
                    "duration_seconds": 0.0,
                    "longrepr": "skip reason",
                },
            ],
        },
    ]
    timing_payload = {
        "nodes": nodes,
        "session_start_monotonic": 1.0,
        "session_end_monotonic": 2.0,
        "pytest_exitstatus": 1,
    }
    report = build_report(
        expected_version=EXPECTED_PACKAGE_VERSION,
        test_root=REPO_ROOT,
        collect_expected=frozenset(),
        collect_actual=frozenset(),
        collect_guard_ok=True,
        provenance_before={
            "module_path": "/x",
            "version": EXPECTED_PACKAGE_VERSION,
            "metadata_version": EXPECTED_PACKAGE_VERSION,
        },
        provenance_after={
            "module_path": "/x",
            "version": EXPECTED_PACKAGE_VERSION,
            "metadata_version": EXPECTED_PACKAGE_VERSION,
        },
        child_probe_before={
            "module_path": "/x",
            "version": EXPECTED_PACKAGE_VERSION,
            "metadata_version": EXPECTED_PACKAGE_VERSION,
        },
        child_probe_after={
            "module_path": "/x",
            "version": EXPECTED_PACKAGE_VERSION,
            "metadata_version": EXPECTED_PACKAGE_VERSION,
        },
        provenance_ok=True,
        timing_payload=timing_payload,
        wall_clock_seconds=1.0,
        pytest_exit_code=1,
        guard_failures=[],
    )
    stored = report["node_timings"]
    assert stored[1]["phases"][1]["outcome"] == "failed"
    assert stored[1]["phases"][1]["longrepr"] == "AssertionError: boom"
    assert stored[2]["phases"][0]["outcome"] == "skipped"
    top = report["top10_by_total_phase_duration"]
    assert top[0]["nodeid"].endswith("test_fail")
    assert top[0]["total_phase_seconds"] == pytest.approx(0.52)
    assert top[0]["share_of_total_phase_time"] > 0.5


def test_write_report_does_not_mutate_test_root_sources(tmp_path: Path) -> None:
    marker = REPO_ROOT / "tests" / "fixtures" / "v08_candidate_slow_nodes.txt"
    before = marker.stat().st_mtime_ns
    out = tmp_path / "report.json"
    write_report(
        out,
        build_report(
            expected_version=EXPECTED_PACKAGE_VERSION,
            test_root=REPO_ROOT,
            collect_expected=frozenset({"tests/unit/a.py::t"}),
            collect_actual=frozenset({"tests/unit/a.py::t"}),
            collect_guard_ok=True,
            provenance_before=None,
            provenance_after=None,
            child_probe_before=None,
            child_probe_after=None,
            provenance_ok=False,
            timing_payload={"nodes": []},
            wall_clock_seconds=0.0,
            pytest_exit_code=None,
            guard_failures=["provenance_before: example"],
        ),
    )
    assert out.is_file()
    assert marker.stat().st_mtime_ns == before


def test_guard_collected_nodes_delegates_to_frozen_ci() -> None:
    ci = load_frozen_candidate_ci(REPO_ROOT)
    canonical = load_canonical_slow_nodes(REPO_ROOT)
    expected = canonical_nodes_for_modules(canonical, SHARD_MODULES[PROFILE_SHARD])
    with pytest.raises(Exception, match="identity mismatch"):
        guard_collected_nodes(ci, frozenset(), expected)


def test_guard_output_refusal_preserves_sentinel_bytes(tmp_path: Path) -> None:
    workspace = _disjoint_consumer_workspace()
    _, python = _bootstrap_isolated_venv(tmp_path)
    output, junit, basetemp = _outside_checkout_profile_paths()
    output.parent.mkdir(parents=True, exist_ok=True)
    sentinel = b"sentinel-do-not-overwrite"
    output.write_bytes(sentinel)
    with pytest.raises(Exception, match="refusing to overwrite"):
        run_profile(
            python=python,
            test_root=REPO_ROOT,
            workspace=workspace,
            output_json=output,
            junit_xml=junit,
            basetemp_parent=basetemp,
            expected_version=EXPECTED_PACKAGE_VERSION,
            collect_cwd=_outside_checkout_collect_cwd(),
        )
    assert output.read_bytes() == sentinel


def test_write_report_refuses_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text("existing\n", encoding="utf-8")
    with pytest.raises(Exception, match="refusing to overwrite"):
        write_report(path, {"schema": "x"})


def test_validate_timing_rejects_empty_and_inventory_mismatch() -> None:
    ci = load_frozen_candidate_ci(REPO_ROOT)
    expected = expected_shard4_nodes(ci, REPO_ROOT)
    assert validate_timing_payload({"nodes": []}, expected_nodes=expected, pytest_exit_code=0)
    sample = {
        "nodes": [
            {
                "nodeid": "tests/unit/a.py::t",
                "phases": [
                    {"when": "setup", "outcome": "passed", "duration_seconds": 0.0},
                    {"when": "call", "outcome": "passed", "duration_seconds": 0.1},
                    {"when": "teardown", "outcome": "passed", "duration_seconds": 0.0},
                ],
            }
        ],
        "session_start_monotonic": 0.0,
        "session_end_monotonic": 1.0,
        "pytest_exitstatus": 0,
    }
    failures = validate_timing_payload(sample, expected_nodes=expected, pytest_exit_code=0)
    assert any("inventory mismatch" in item for item in failures)


def test_incomplete_timing_after_pytest_fails_gate(tmp_path: Path) -> None:
    workspace = _disjoint_consumer_workspace()
    _, python = _bootstrap_isolated_venv(tmp_path)
    output, junit, basetemp = _outside_checkout_profile_paths()
    ci = load_frozen_candidate_ci(REPO_ROOT)
    expected = expected_shard4_nodes(ci, REPO_ROOT)
    probes = {
        "module_path": "/site-packages/gamefactory/__init__.py",
        "version": EXPECTED_PACKAGE_VERSION,
        "metadata_version": EXPECTED_PACKAGE_VERSION,
    }
    shard_result = PytestShardResult(
        exit_code=0,
        collect_actual=expected,
        child_probe_before=probes,
        child_probe_after=probes,
    )
    with (
        patch(
            "scripts.profile_v08_installed_shard_runtime.run_pytest_shard",
            return_value=shard_result,
        ),
        patch(
            "scripts.profile_v08_installed_shard_runtime.run_installed_probe",
            return_value=probes,
        ),
    ):
        rc = run_profile(
            python=python,
            test_root=REPO_ROOT,
            workspace=workspace,
            output_json=output,
            junit_xml=junit,
            basetemp_parent=basetemp,
            expected_version=EXPECTED_PACKAGE_VERSION,
            collect_cwd=_outside_checkout_collect_cwd(),
        )
    assert rc == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["provenance_ok"] is False
    assert any("timing" in item for item in payload["guard_failures"])


def test_provenance_before_after_mismatch_fails(tmp_path: Path) -> None:
    workspace = _disjoint_consumer_workspace()
    _, python = _bootstrap_isolated_venv(tmp_path)
    output, junit, basetemp = _outside_checkout_profile_paths()
    ci = load_frozen_candidate_ci(REPO_ROOT)
    expected = expected_shard4_nodes(ci, REPO_ROOT)
    before = {
        "module_path": "/site-packages/gamefactory/__init__.py",
        "version": EXPECTED_PACKAGE_VERSION,
        "metadata_version": EXPECTED_PACKAGE_VERSION,
    }
    after = dict(before)
    after["module_path"] = "/other/site-packages/gamefactory/__init__.py"
    calls = {"count": 0}

    def _probe(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        return before if calls["count"] == 1 else after

    timing_payload = _minimal_valid_timing_for_nodes(expected, pytest_exit_code=0)
    shard_result = PytestShardResult(
        exit_code=0,
        collect_actual=expected,
        child_probe_before=before,
        child_probe_after=before,
    )
    with (
        patch(
            "scripts.profile_v08_installed_shard_runtime.run_installed_probe", side_effect=_probe
        ),
        patch(
            "scripts.profile_v08_installed_shard_runtime.run_pytest_shard",
            return_value=shard_result,
        ),
        patch(
            "scripts.profile_v08_installed_shard_runtime.load_timing_payload",
            return_value=timing_payload,
        ),
    ):
        rc = run_profile(
            python=python,
            test_root=REPO_ROOT,
            workspace=workspace,
            output_json=output,
            junit_xml=junit,
            basetemp_parent=basetemp,
            expected_version=EXPECTED_PACKAGE_VERSION,
            collect_cwd=_outside_checkout_collect_cwd(),
        )
    assert rc == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["provenance_ok"] is False
    assert payload["pytest_exit_code"] == 0
    assert any(
        "provenance_mismatch:parent_before_after" in item for item in payload["guard_failures"]
    )


def test_provenance_snapshots_equal_requires_identity_keys() -> None:
    base = {
        "module_path": "/a",
        "version": EXPECTED_PACKAGE_VERSION,
        "metadata_version": EXPECTED_PACKAGE_VERSION,
        "python_executable": "ignored",
    }
    assert provenance_snapshots_equal(base, dict(base))
    changed = dict(base)
    changed["module_path"] = "/b"
    assert not provenance_snapshots_equal(base, changed)


def test_pytest_child_bootstrap_integration(tmp_path: Path) -> None:
    frozen_root = Path(tempfile.mkdtemp(prefix="gf-profile-frozen-"))
    expected_nodes = _write_minimal_integration_frozen_root(
        frozen_root, version=EXPECTED_PACKAGE_VERSION
    )
    workspace = _disjoint_consumer_workspace()
    _, python = _bootstrap_consumer_venv_with_pytest(tmp_path)
    profile_script = REPO_ROOT / "scripts" / "profile_v08_installed_shard_runtime.py"
    output, junit, _basetemp_parent = _outside_checkout_profile_paths()
    timing_path, child_before, child_after, runtime_collect, _costs = _timing_sidecar_paths(output)
    basetemp = _fresh_basetemp(_verification_revision_basetemp_parent())
    result = run_pytest_shard(
        python,
        frozen_root,
        workspace=workspace,
        basetemp=basetemp,
        junit_path=junit,
        collect_cwd=_outside_checkout_collect_cwd(),
        profile_script=profile_script,
        timing_path=timing_path,
        expected_version=EXPECTED_PACKAGE_VERSION,
        child_probe_before=child_before,
        child_probe_after=child_after,
        runtime_collect_path=runtime_collect,
        observe_costs=False,
    )
    assert result.exit_code == 1
    assert result.collect_actual == expected_nodes
    assert result.child_probe_before is not None
    assert result.child_probe_after is not None
    assert provenance_snapshots_equal(result.child_probe_before, result.child_probe_after)
    timing_payload = load_timing_payload(timing_path)
    failures = validate_timing_payload(
        timing_payload,
        expected_nodes=expected_nodes,
        pytest_exit_code=result.exit_code,
    )
    assert not failures
    nodes_by_id = {entry["nodeid"]: entry for entry in timing_payload["nodes"]}
    assert nodes_by_id[f"{_INTEGRATION_MODULE}::test_passes"]["phases"][-1]["outcome"] == "passed"
    assert nodes_by_id[f"{_INTEGRATION_MODULE}::test_fails"]["phases"][1]["outcome"] == "failed"
    assert nodes_by_id[f"{_INTEGRATION_MODULE}::test_skipped"]["phases"][0]["outcome"] == "skipped"
    assert junit.is_file()


def test_pytest_child_script_loads_profile_dataclass_module(tmp_path: Path) -> None:
    profile_script = REPO_ROOT / "scripts" / "profile_v08_installed_shard_runtime.py"
    load_only = """
import importlib.util
import sys
from pathlib import Path

profile_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("_profile_v08_runner", profile_path)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
print(mod.PhaseOutcome.__name__)
"""
    proc = subprocess.run(
        [str(sys.executable), "-I", "-c", load_only, str(profile_script.resolve())],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "PhaseOutcome"
    assert "AttributeError" not in proc.stderr


def test_installed_probe_success_in_isolated_venv(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _, python = _bootstrap_isolated_venv(tmp_path)
    site_packages = _site_packages_dir(python)
    payload = run_installed_probe(
        python,
        workspace=workspace,
        test_root=REPO_ROOT,
        expected_version=EXPECTED_PACKAGE_VERSION,
        cwd=tmp_path,
    )
    assert payload["version"] == EXPECTED_PACKAGE_VERSION
    module_path = Path(payload["module_path"]).resolve()
    assert module_path.is_relative_to(site_packages.resolve())
    assert not module_path.is_relative_to(REPO_ROOT / "src")


def test_exec_installed_probe_in_process_matches_subprocess(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _, python = _bootstrap_consumer_venv_with_pytest(tmp_path)
    subprocess_payload = run_installed_probe(
        python,
        workspace=workspace,
        test_root=REPO_ROOT,
        expected_version=EXPECTED_PACKAGE_VERSION,
        cwd=tmp_path,
    )
    profile_script = REPO_ROOT / "scripts" / "profile_v08_installed_shard_runtime.py"
    inline_script = f"""
import importlib.util
import json
import sys
from pathlib import Path

profile_path = Path({str(profile_script.resolve())!r})
spec = importlib.util.spec_from_file_location("_profile_exec_probe", profile_path)
if spec is None or spec.loader is None:
    raise SystemExit("profile module load failed")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
payload = mod.exec_installed_probe_in_process(
    expected_version={EXPECTED_PACKAGE_VERSION!r},
    test_root=Path({str(REPO_ROOT.resolve())!r}),
    workspace=Path({str(workspace.resolve())!r}),
)
print(json.dumps(payload))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONNOUSERSITE"] = "1"
    proc = subprocess.run(
        [str(python), "-c", inline_script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    inline_payload = json.loads(proc.stdout.strip())
    assert provenance_snapshots_equal(subprocess_payload, inline_payload)


def test_aggregate_top10_ranks_total_phase_time() -> None:
    nodes = [
        {
            "nodeid": "a",
            "phases": [
                {"when": "setup", "duration_seconds": 1.0},
                {"when": "call", "duration_seconds": 0.1},
                {"when": "teardown", "duration_seconds": 0.1},
            ],
        },
        {
            "nodeid": "b",
            "phases": [
                {"when": "setup", "duration_seconds": 0.0},
                {"when": "call", "duration_seconds": 0.5},
                {"when": "teardown", "duration_seconds": 0.0},
            ],
        },
    ]
    top = aggregate_top10(
        nodes,
        end_to_end_wall_seconds=10.0,
        pytest_session_wall_seconds=2.0,
    )
    assert top[0]["nodeid"] == "a"
    assert top[0]["total_phase_seconds"] == pytest.approx(1.2)


def test_guard_output_evidence_paths_rejects_checkout_outputs(tmp_path: Path) -> None:
    timing_path, _, _, _, _ = _timing_sidecar_paths(tmp_path / "report.json")
    with pytest.raises(Exception, match="must not live under"):
        guard_output_evidence_paths(
            test_root=REPO_ROOT,
            output_json=REPO_ROOT / "report.json",
            junit_xml=tmp_path / "junit.xml",
            timing_path=timing_path,
            basetemp_parent=tmp_path / "basetemp",
        )


def _write_tiny_gamefactory_wheel(path: Path, *, version: str, init_body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("gamefactory/__init__.py", init_body)
        archive.writestr(
            f"gamefactory-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: gamefactory\nVersion: {version}\n".encode(),
        )


def _install_tiny_wheel_layout(site_packages: Path, *, version: str, init_body: bytes) -> None:
    pkg = site_packages / "gamefactory"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_bytes(init_body)
    dist_info = site_packages / f"gamefactory-{version}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_bytes(
        f"Metadata-Version: 2.1\nName: gamefactory\nVersion: {version}\n".encode()
    )


def test_shard_timing_plugin_writes_json(tmp_path: Path) -> None:
    timing_path = tmp_path / "timing.json"
    plugin = ShardTimingPlugin(timing_path)
    plugin.pytest_sessionstart(session=object())
    nodeid = "tests/unit/x.py::test_a"
    record = plugin._records.setdefault(nodeid, NodeTimingRecord(nodeid=nodeid))
    record.phases.append(
        PhaseOutcome(when="call", outcome="passed", duration_seconds=0.01, longrepr=None)
    )
    plugin.pytest_sessionfinish(session=object(), exitstatus=0)
    payload = json.loads(timing_path.read_text(encoding="utf-8"))
    assert payload["pytest_exitstatus"] == 0
    assert payload["nodes"][0]["nodeid"] == nodeid


def test_subprocess_run_wrapper_preserves_success_and_errors(tmp_path: Path) -> None:
    session = install_cost_observation()
    try:
        ok = subprocess.run(
            [sys.executable, "-c", "print('ok')"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert ok.stdout.strip() == "ok"
        with pytest.raises(subprocess.CalledProcessError):
            subprocess.run(
                [sys.executable, "-c", "import sys; sys.exit(3)"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                check=True,
            )
    finally:
        deactivate_cost_observation(session)
    assert session.timed_operations[-1].outcome == "nonzero_exit"
    assert session.timed_operations[0].outcome == "success"
    for row in session.timed_operations:
        assert row.observation_scope == OBSERVATION_SCOPE_PYTEST_SESSION
        assert row.nodeid is None and row.phase is None


def test_subprocess_run_wrapper_preserves_timeout(tmp_path: Path) -> None:
    session = install_cost_observation()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess.run(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=0.05,
            )
    finally:
        deactivate_cost_observation(session)
    assert session.timed_operations[-1].outcome == "timeout"


def test_direct_popen_count_has_no_attributed_duration(tmp_path: Path) -> None:
    session = install_cost_observation()
    try:
        before_ops = len(session.timed_operations)
        proc = subprocess.Popen(
            [sys.executable, "-c", "print('popen')"],
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            text=True,
        )
        stdout, _ = proc.communicate(timeout=5)
        assert stdout.strip() == "popen"
    finally:
        deactivate_cost_observation(session)
    payload = finalize_cost_observation(session)
    assert payload["popen_duration_attribution"] == "unattributed_direct_popen_only"
    assert payload["popen_counts_by_category"]["other"] >= 1
    assert len(session.timed_operations) == before_ops


def test_classify_subprocess_category_and_cold_spawn_label() -> None:
    assert classify_subprocess_category(["godot.exe", "--version"]) == "godot"
    assert classify_subprocess_category(["blender", "--version"]) == "blender"
    cold = [sys.executable, "-I", "verify_candidate_bundle.py", "/tmp/bundle"]
    assert classify_subprocess_category(cold) == "python_cold_verifier"
    assert spawn_label_for_subprocess_run(cold) == "trusted_cold_verify_candidate_bundle_spawn"
    assert classify_subprocess_category([sys.executable, "--story=godot"]) == "other"
    assert classify_subprocess_category([sys.executable, "-c", "print('godot')"]) == "other"
    assert classify_subprocess_category([sys.executable, "script.py", "godot-data"]) == "other"


def test_classify_cold_verifier_with_outer_quotes_on_windows_path_token() -> None:
    quoted = '"C:\\\\path with spaces\\\\verify_candidate_bundle.py"'
    argv = [sys.executable, quoted, "bundle"]
    assert classify_subprocess_category(argv) == "python_cold_verifier"
    audit_argv = argv_from_subprocess_popen_audit((None, f"{sys.executable} {quoted} bundle"))
    assert classify_subprocess_category(audit_argv) == "python_cold_verifier"


def test_argv_from_subprocess_popen_audit_linux_list_tuple_shapes() -> None:
    linux_argv = ["/usr/bin/godot", "--version"]
    assert argv_from_subprocess_popen_audit((None, linux_argv, None, None)) == linux_argv
    assert argv_from_subprocess_popen_audit(("/opt/godot", ("--import",), None, None)) == [
        "/opt/godot",
        "--import",
    ]
    assert argv_from_subprocess_popen_audit(
        ("/opt/godot", ["/opt/godot", "--headless"], None, None)
    ) == ["/opt/godot", "--headless"]


def test_windows_audit_popen_counts_godot_probe_exe(tmp_path: Path) -> None:
    probe = tmp_path / "godot-probe.exe"
    shutil.copy(sys.executable, probe)
    session = install_cost_observation()
    try:
        before = session.popen_counts_by_category["godot"]
        proc = subprocess.Popen(
            [str(probe), "--version"],
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proc.communicate(timeout=30)
    finally:
        deactivate_cost_observation(session)
    assert session.popen_counts_by_category["godot"] == before + 1


def test_windows_audit_popen_counts_cold_verifier_script(tmp_path: Path) -> None:
    script = tmp_path / "verify_candidate_bundle.py"
    script.write_text("raise SystemExit(0)\n", encoding="utf-8")
    session = install_cost_observation()
    try:
        before = session.popen_counts_by_category["python_cold_verifier"]
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proc.communicate(timeout=30)
        assert proc.returncode == 0
    finally:
        deactivate_cost_observation(session)
    assert session.popen_counts_by_category["python_cold_verifier"] == before + 1


def test_windows_audit_popen_counts_cold_verifier_in_directory_with_spaces(
    tmp_path: Path,
) -> None:
    spaced = tmp_path / "path with spaces"
    spaced.mkdir()
    script = spaced / "verify_candidate_bundle.py"
    script.write_text("raise SystemExit(0)\n", encoding="utf-8")
    session = install_cost_observation()
    try:
        before = session.popen_counts_by_category["python_cold_verifier"]
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proc.communicate(timeout=30)
        assert proc.returncode == 0
    finally:
        deactivate_cost_observation(session)
    assert session.popen_counts_by_category["python_cold_verifier"] == before + 1


def test_named_entrypoint_coverage_reports_lower_bounds_when_unobserved() -> None:
    coverage = build_named_entrypoint_coverage(CostObservationSession())
    assert all(row["lower_bound_only"] for row in coverage.values())


def test_named_copy2_wrapper_records_instrumented_invocation(tmp_path: Path) -> None:
    session = install_cost_observation()
    try:
        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_text("payload\n", encoding="utf-8")
        shutil.copy2(src, dst)
    finally:
        deactivate_cost_observation(session)
    coverage = build_named_entrypoint_coverage(session)
    copy2_row = coverage[NAMED_COST_ENTRYPOINTS[3]]
    assert copy2_row["coverage"] == "instrumented_invocation"
    assert copy2_row["invocation_count"] == 1
    assert copy2_row["lower_bound_only"] is False
    payload = finalize_cost_observation(session)
    assert payload["observer_context"]["wrapper_overhead_status"] == (
        "measured_bookkeeping_limited_coverage"
    )


def test_subprocess_run_counts_popen_without_audit_suppression(tmp_path: Path) -> None:
    session = install_cost_observation()
    try:
        before = dict(session.popen_counts_by_category)
        subprocess.run(
            [sys.executable, "-c", "print('one')"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        after = dict(session.popen_counts_by_category)
    finally:
        deactivate_cost_observation(session)
    assert after["other"] == before.get("other", 0) + 1
    assert len(session.timed_operations) == 1


def test_nested_subprocess_runs_count_two_popens(tmp_path: Path) -> None:
    session = install_cost_observation()
    try:
        before = session.popen_counts_by_category["other"]
        for _ in range(2):
            subprocess.run(
                [sys.executable, "-c", "pass"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                check=True,
            )
    finally:
        deactivate_cost_observation(session)
    assert session.popen_counts_by_category["other"] == before + 2
    assert len(session.timed_operations) == 2


def test_subprocess_pre_spawn_failure_does_not_increment_popen(tmp_path: Path) -> None:
    session = install_cost_observation()
    try:
        before = dict(session.popen_counts_by_category)
        with pytest.raises(TypeError):
            subprocess.run([123], cwd=tmp_path, check=True)
        after = dict(session.popen_counts_by_category)
    finally:
        deactivate_cost_observation(session)
    assert after == before
    assert session.timed_operations[-1].outcome == "error"


def test_process_runner_nonzero_exit_and_popen_count(tmp_path: Path) -> None:
    from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner

    session = install_cost_observation()
    try:
        before = session.popen_counts_by_category["other"]
        session.set_context("tests/unit/example.py::test_x", "call")
        runner = ProcessRunner(sanitize_output=False)
        result = runner.run(
            CommandRequest(
                args=[sys.executable, "-c", "import sys; sys.exit(9)"],
                cwd=tmp_path,
                minimal_env=False,
            )
        )
        assert result.exit_code == 9
    finally:
        deactivate_cost_observation(session)
    assert session.popen_counts_by_category["other"] == before + 1
    last = session.timed_operations[-1]
    assert last.outcome == "nonzero_exit"
    assert last.spawn_label.startswith("process_runner_run:")
    assert last.nodeid == "tests/unit/example.py::test_x"
    assert last.phase == "call"


def test_wheel_record_rewrite_does_not_fail_package_compare(tmp_path: Path) -> None:
    version = EXPECTED_PACKAGE_VERSION
    body = f'__version__ = "{version}"\n'.encode()
    wheel = tmp_path / "gamefactory.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("gamefactory/__init__.py", body)
        archive.writestr(
            f"gamefactory-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: gamefactory\nVersion: {version}\n".encode(),
        )
        archive.writestr(
            f"gamefactory-{version}.dist-info/RECORD",
            b"gamefactory/__init__.py,sha256=abc,0\n",
        )
    site_packages = tmp_path / "site-packages"
    _install_tiny_wheel_layout(site_packages, version=version, init_body=body)
    (site_packages / f"gamefactory-{version}.dist-info" / "RECORD").write_text(
        "pip-rewritten-record-line\n",
        encoding="utf-8",
    )
    assert compare_gamefactory_wheel_to_site_packages(wheel, site_packages) == []


def test_wheel_extra_consumer_package_file_fails(tmp_path: Path) -> None:
    version = EXPECTED_PACKAGE_VERSION
    body = f'__version__ = "{version}"\n'.encode()
    wheel = tmp_path / "gamefactory.whl"
    _write_tiny_gamefactory_wheel(wheel, version=version, init_body=body)
    site_packages = tmp_path / "site-packages"
    _install_tiny_wheel_layout(site_packages, version=version, init_body=body)
    (site_packages / "gamefactory" / "extra.py").write_text("x\n", encoding="utf-8")
    failures = compare_gamefactory_wheel_to_site_packages(wheel, site_packages)
    assert any("consumer_extra_package_file" in item for item in failures)


def test_wheel_byte_manifest_matches_consumer_site_packages(tmp_path: Path) -> None:
    version = EXPECTED_PACKAGE_VERSION
    body = f'__version__ = "{version}"\n'.encode()
    wheel = tmp_path / "dist" / f"gamefactory-{version}-py3-none-any.whl"
    _write_tiny_gamefactory_wheel(wheel, version=version, init_body=body)
    site_packages = tmp_path / "site-packages"
    _install_tiny_wheel_layout(site_packages, version=version, init_body=body)
    assert compare_gamefactory_wheel_to_site_packages(wheel, site_packages) == []
    (site_packages / "gamefactory" / "__init__.py").write_text("tampered\n", encoding="utf-8")
    failures = compare_gamefactory_wheel_to_site_packages(wheel, site_packages)
    assert any("byte_mismatch" in item for item in failures)


def test_run_profile_wheel_mismatch_returns_exit_one(tmp_path: Path) -> None:
    workspace = _disjoint_consumer_workspace()
    _, python = _outside_checkout_consumer_bootstrap()
    site_packages = _site_packages_dir(python)
    version = EXPECTED_PACKAGE_VERSION
    body = f'__version__ = "{version}"\n'.encode()
    wheel = _outside_checkout_wheel_path()
    _write_tiny_gamefactory_wheel(wheel, version=version, init_body=body)
    _install_tiny_wheel_layout(site_packages, version=version, init_body=b"tampered\n")
    output, junit, basetemp = _outside_checkout_profile_paths()
    rc = run_profile(
        python=python,
        test_root=REPO_ROOT,
        workspace=workspace,
        output_json=output,
        junit_xml=junit,
        basetemp_parent=basetemp,
        expected_version=version,
        collect_cwd=_outside_checkout_collect_cwd(),
        skip_pytest=True,
        wheel_path=wheel,
    )
    assert rc == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["installed_wheel_sha256"] is None
    assert payload["input_wheel_sha256_unverified"] == sha256_file_path(wheel)
    assert "installed_wheel_sha256" in payload["metrics_missing"]
    assert any("wheel_provenance_before" in item for item in payload["guard_failures"])


def test_pytest_child_bootstrap_integration_with_cost_observation(tmp_path: Path) -> None:
    frozen_root = Path(tempfile.mkdtemp(prefix="gf-profile-frozen-costs-"))
    expected_nodes = _write_minimal_integration_frozen_root(
        frozen_root, version=EXPECTED_PACKAGE_VERSION
    )
    workspace = _disjoint_consumer_workspace()
    _, python = _bootstrap_consumer_venv_with_pytest(tmp_path)
    profile_script = REPO_ROOT / "scripts" / "profile_v08_installed_shard_runtime.py"
    output, junit, _basetemp_parent = _outside_checkout_profile_paths()
    timing_path, child_before, child_after, runtime_collect, costs_path = _timing_sidecar_paths(
        output
    )
    basetemp = _fresh_basetemp(_verification_revision_basetemp_parent())
    result = run_pytest_shard(
        python,
        frozen_root,
        workspace=workspace,
        basetemp=basetemp,
        junit_path=junit,
        collect_cwd=_outside_checkout_collect_cwd(),
        profile_script=profile_script,
        timing_path=timing_path,
        expected_version=EXPECTED_PACKAGE_VERSION,
        child_probe_before=child_before,
        child_probe_after=child_after,
        runtime_collect_path=runtime_collect,
        observe_costs=True,
        costs_path=costs_path,
    )
    assert result.exit_code == 1
    assert costs_path.is_file()
    costs = json.loads(costs_path.read_text(encoding="utf-8"))
    assert not validate_cost_payload(costs)
    assert costs["schema"] == "v08-installed-shard-runtime-costs/v1-preliminary"
    assert costs["observer_context"]["audit_hook_removable"] is False
    assert costs["observer_context"]["audit_hook_deactivated_on_cleanup"] is True
    assert costs["observer_context"]["wrapper_overhead_status"] in {
        "unknown",
        "measured_bookkeeping_limited_coverage",
    }
    assert "never sum with pytest node phase wall times" in costs["timed_operations_overlap_note"]
    failures = validate_timing_payload(
        load_timing_payload(timing_path),
        expected_nodes=expected_nodes,
        pytest_exit_code=result.exit_code,
    )
    assert not failures


def test_cost_observation_attributes_setup_call_teardown_spawns(tmp_path: Path) -> None:
    frozen_root = Path(tempfile.mkdtemp(prefix="gf-profile-cost-phases-"))
    expected_nodes = _write_cost_phase_integration_frozen_root(
        frozen_root, version=EXPECTED_PACKAGE_VERSION
    )
    nodeid = next(iter(expected_nodes))
    workspace = _disjoint_consumer_workspace()
    _, python = _bootstrap_consumer_venv_with_pytest(tmp_path)
    profile_script = REPO_ROOT / "scripts" / "profile_v08_installed_shard_runtime.py"
    output, junit, _basetemp_parent = _outside_checkout_profile_paths()
    timing_path, child_before, child_after, runtime_collect, costs_path = _timing_sidecar_paths(
        output
    )
    basetemp = _fresh_basetemp(_verification_revision_basetemp_parent())
    result = run_pytest_shard(
        python,
        frozen_root,
        workspace=workspace,
        basetemp=basetemp,
        junit_path=junit,
        collect_cwd=_outside_checkout_collect_cwd(),
        profile_script=profile_script,
        timing_path=timing_path,
        expected_version=EXPECTED_PACKAGE_VERSION,
        child_probe_before=child_before,
        child_probe_after=child_after,
        runtime_collect_path=runtime_collect,
        observe_costs=True,
        costs_path=costs_path,
    )
    assert result.exit_code == 0
    costs = json.loads(costs_path.read_text(encoding="utf-8"))
    assert not validate_cost_payload(costs)
    phase_ops = [
        row
        for row in costs["timed_operations"]
        if row.get("nodeid") == nodeid and row.get("spawn_label", "").startswith("subprocess_run:")
    ]
    phases = {str(row["phase"]) for row in phase_ops}
    assert phases == {"setup", "call", "teardown"}
    assert len(phase_ops) == 3
    for row in phase_ops:
        assert row.get("observation_scope") is None


def test_cost_observation_session_finish_subprocess_is_session_scoped(tmp_path: Path) -> None:
    frozen_root = Path(tempfile.mkdtemp(prefix="gf-profile-session-finish-"))
    _write_session_finish_subprocess_frozen_root(frozen_root, version=EXPECTED_PACKAGE_VERSION)
    workspace = _disjoint_consumer_workspace()
    _, python = _bootstrap_consumer_venv_with_pytest(tmp_path)
    profile_script = REPO_ROOT / "scripts" / "profile_v08_installed_shard_runtime.py"
    output, junit, _basetemp_parent = _outside_checkout_profile_paths()
    timing_path, child_before, child_after, runtime_collect, costs_path = _timing_sidecar_paths(
        output
    )
    basetemp = _fresh_basetemp(_verification_revision_basetemp_parent())
    result = run_pytest_shard(
        python,
        frozen_root,
        workspace=workspace,
        basetemp=basetemp,
        junit_path=junit,
        collect_cwd=_outside_checkout_collect_cwd(),
        profile_script=profile_script,
        timing_path=timing_path,
        expected_version=EXPECTED_PACKAGE_VERSION,
        child_probe_before=child_before,
        child_probe_after=child_after,
        runtime_collect_path=runtime_collect,
        observe_costs=True,
        costs_path=costs_path,
    )
    assert result.exit_code == 0
    costs = json.loads(costs_path.read_text(encoding="utf-8"))
    assert not validate_cost_payload(costs)
    session_ops = [
        row
        for row in costs["timed_operations"]
        if row.get("observation_scope") == OBSERVATION_SCOPE_PYTEST_SESSION
    ]
    assert session_ops
    assert all(row.get("nodeid") is None and row.get("phase") is None for row in session_ops)
    assert costs["timed_operations_observation_scope_counts"][OBSERVATION_SCOPE_PYTEST_SESSION] >= 1


def test_fresh_basetemp_child_dir_name_is_short(tmp_path: Path) -> None:
    parent = tmp_path / "basetemp-parent"
    first = _fresh_basetemp(parent)
    second = _fresh_basetemp(parent)
    assert first.name.startswith("pt-")
    assert second.name.startswith("pt-")
    assert first != second
    assert len(first.name) <= 16
    assert not first.name.startswith("pytest-basetemp")


def test_fresh_basetemp_distinct_when_clock_and_pid_unchanged(tmp_path: Path) -> None:
    """mkdtemp must not collide when time/PID-based ids would repeat (same millisecond)."""
    parent = tmp_path / "basetemp-parent"
    frozen_time = 1_700_000_000.0
    frozen_pid = 4242
    with (
        patch("scripts.profile_v08_installed_shard_runtime.time.time", return_value=frozen_time),
        patch("scripts.profile_v08_installed_shard_runtime.os.getpid", return_value=frozen_pid),
    ):
        first = _fresh_basetemp(parent)
        second = _fresh_basetemp(parent)
    assert first != second
    assert first.is_dir() and second.is_dir()


def _minimal_cost_payload() -> dict[str, object]:
    coverage = {
        entry: {"coverage": "not_observed_instrumentation_lower_bound", "lower_bound_only": True}
        for entry in NAMED_COST_ENTRYPOINTS
    }
    return {
        "schema": "v08-installed-shard-runtime-costs/v1-preliminary",
        "popen_counts_by_category": {
            "godot": 0,
            "blender": 0,
            "python_cold_verifier": 0,
            "other": 0,
        },
        "named_entrypoint_coverage": coverage,
        "named_entrypoint_invocations": [],
        "timed_operations": [],
        "category_duration_seconds": {
            "godot": 0.0,
            "blender": 0.0,
            "python_cold_verifier": 0.0,
            "process_runner": 0.0,
            "other": 0.0,
        },
    }


def _fully_valid_zero_activity_cost_payload() -> dict[str, object]:
    return _minimal_cost_payload()


def test_metrics_missing_keeps_cost_metrics_without_valid_sidecar() -> None:
    missing = metrics_missing_for_report(cost_payload=None, wheel_provenance_verified=False)
    assert METRICS_FILLED_BY_COST_OBSERVATION.issubset(missing)


def test_metrics_missing_drops_cost_metrics_with_valid_payload() -> None:
    missing = metrics_missing_for_report(
        cost_payload=_fully_valid_zero_activity_cost_payload(),
        wheel_provenance_verified=False,
    )
    assert not METRICS_FILLED_BY_COST_OBSERVATION.intersection(missing)


def test_metrics_missing_keeps_lower_bound_named_coverage_from_clearing_entrypoint_metrics() -> (
    None
):
    missing = metrics_missing_for_report(
        cost_payload=_fully_valid_zero_activity_cost_payload(),
        wheel_provenance_verified=False,
    )
    assert "workspace_copy_seconds" in missing
    assert "content_hash_seconds" in missing
    assert "immutable_fixture_construction_seconds" in missing
    assert not named_entrypoint_metrics_satisfied(
        _fully_valid_zero_activity_cost_payload()["named_entrypoint_coverage"]
    )


def test_validate_cost_payload_rejects_invalid_durations_and_bool_counts() -> None:
    base = _fully_valid_zero_activity_cost_payload()
    bad_nan = dict(base)
    bad_nan["category_duration_seconds"] = dict(base["category_duration_seconds"])
    bad_nan["category_duration_seconds"]["godot"] = float("nan")
    assert any("godot" in item for item in validate_cost_payload(bad_nan))

    bad_bool = dict(base)
    bad_bool["popen_counts_by_category"] = dict(base["popen_counts_by_category"])
    bad_bool["popen_counts_by_category"]["other"] = True
    assert any("bool" in item for item in validate_cost_payload(bad_bool))

    bad_wrapped = dict(base)
    bad_wrapped["timed_operations"] = [
        {
            "spawn_label": "subprocess_run:other",
            "category": "other",
            "duration_seconds": 0.01,
            "outcome": "success",
            "nodeid": None,
            "phase": "call",
        }
    ]
    assert any("partial" in item for item in validate_cost_payload(bad_wrapped))

    bad_session_missing_scope = dict(base)
    bad_session_missing_scope["timed_operations"] = [
        {
            "spawn_label": "subprocess_run:other",
            "category": "other",
            "duration_seconds": 0.01,
            "outcome": "success",
            "nodeid": None,
            "phase": None,
        }
    ]
    assert any(
        "observation_scope" in item for item in validate_cost_payload(bad_session_missing_scope)
    )

    good_session_scope = dict(base)
    good_session_scope["timed_operations"] = [
        {
            "spawn_label": "subprocess_run:other",
            "category": "other",
            "duration_seconds": 0.01,
            "outcome": "success",
            "nodeid": None,
            "phase": None,
            "observation_scope": OBSERVATION_SCOPE_PYTEST_SESSION,
        }
    ]
    good_session_scope["category_duration_seconds"] = dict(base["category_duration_seconds"])
    good_session_scope["category_duration_seconds"]["other"] = 0.01
    assert not validate_cost_payload(good_session_scope)


def test_cost_metrics_keep_duration_missing_when_popen_without_timing() -> None:
    payload = _fully_valid_zero_activity_cost_payload()
    payload = dict(payload)
    payload["popen_counts_by_category"] = dict(payload["popen_counts_by_category"])
    payload["popen_counts_by_category"]["godot"] = 2
    assert not validate_cost_payload(payload)
    satisfied = cost_observation_metrics_satisfied(payload)
    assert "subprocess_count" in satisfied
    assert "cold_verifier_invocation_count" in satisfied
    assert "godot_invocation_seconds" not in satisfied
    assert "subprocess_wall_seconds" not in satisfied
    missing = metrics_missing_for_report(cost_payload=payload, wheel_provenance_verified=False)
    assert "godot_invocation_seconds" in missing
    assert "subprocess_wall_seconds" in missing


def test_cost_metrics_keep_duration_missing_when_one_of_two_godot_timed() -> None:
    payload = dict(_fully_valid_zero_activity_cost_payload())
    payload["popen_counts_by_category"] = dict(payload["popen_counts_by_category"])
    payload["popen_counts_by_category"]["godot"] = 2
    payload["timed_operations"] = [
        {
            "spawn_label": "subprocess_run:godot",
            "category": "godot",
            "duration_seconds": 0.25,
            "outcome": "success",
            "nodeid": "tests/unit/a.py::test_x",
            "phase": "call",
        }
    ]
    payload["category_duration_seconds"] = dict(payload["category_duration_seconds"])
    payload["category_duration_seconds"]["godot"] = 0.25
    satisfied = cost_observation_metrics_satisfied(payload)
    assert "godot_invocation_seconds" not in satisfied
    assert "subprocess_wall_seconds" not in satisfied


def test_cost_metrics_mixed_categories_partial_other_keeps_subprocess_wall_missing() -> None:
    payload = dict(_fully_valid_zero_activity_cost_payload())
    payload["popen_counts_by_category"] = dict(payload["popen_counts_by_category"])
    payload["popen_counts_by_category"]["godot"] = 1
    payload["popen_counts_by_category"]["other"] = 2
    payload["timed_operations"] = [
        {
            "spawn_label": "subprocess_run:godot",
            "category": "godot",
            "duration_seconds": 0.1,
            "outcome": "success",
            "nodeid": "tests/unit/a.py::test_x",
            "phase": "call",
        },
        {
            "spawn_label": "subprocess_run:other",
            "category": "other",
            "duration_seconds": 0.2,
            "outcome": "success",
            "nodeid": "tests/unit/a.py::test_x",
            "phase": "call",
        },
    ]
    payload["category_duration_seconds"] = dict(payload["category_duration_seconds"])
    payload["category_duration_seconds"]["godot"] = 0.1
    payload["category_duration_seconds"]["other"] = 0.2
    satisfied = cost_observation_metrics_satisfied(payload)
    assert "godot_invocation_seconds" not in satisfied
    assert "subprocess_wall_seconds" not in satisfied


def test_cost_metrics_full_wrapped_spawn_coverage_still_keeps_subprocess_wall_missing() -> None:
    payload = dict(_fully_valid_zero_activity_cost_payload())
    payload["popen_counts_by_category"] = dict(payload["popen_counts_by_category"])
    payload["popen_counts_by_category"]["other"] = 2
    payload["timed_operations"] = [
        {
            "spawn_label": "subprocess_run:other",
            "category": "other",
            "duration_seconds": 0.01,
            "outcome": "success",
            "nodeid": "tests/unit/a.py::test_x",
            "phase": "call",
        },
        {
            "spawn_label": "subprocess_run:other",
            "category": "other",
            "duration_seconds": 0.02,
            "outcome": "success",
            "nodeid": "tests/unit/a.py::test_y",
            "phase": "call",
        },
    ]
    payload["category_duration_seconds"] = dict(payload["category_duration_seconds"])
    payload["category_duration_seconds"]["other"] = 0.03
    satisfied = cost_observation_metrics_satisfied(payload)
    assert "subprocess_wall_seconds" not in satisfied


def test_cost_metrics_pre_spawn_failure_plus_direct_popen_subprocess_wall_missing(
    tmp_path: Path,
) -> None:
    session = install_cost_observation()
    try:
        with pytest.raises(TypeError):
            subprocess.run([123], cwd=tmp_path, check=True)
        proc = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            text=True,
        )
        proc.communicate(timeout=5)
    finally:
        deactivate_cost_observation(session)
    payload = finalize_cost_observation(session)
    assert payload["popen_counts_by_category"]["other"] >= 1
    assert any(row.outcome == "error" for row in session.timed_operations)
    satisfied = cost_observation_metrics_satisfied(payload)
    assert "subprocess_wall_seconds" not in satisfied


def test_nested_process_runner_and_subprocess_same_phase_count_one_spawn(tmp_path: Path) -> None:
    from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner

    session = install_cost_observation()
    try:
        session.set_context("tests/unit/example.py::test_nested", "call")
        before = session.popen_counts_by_category["other"]
        runner = ProcessRunner(sanitize_output=False)
        runner.run(
            CommandRequest(
                args=[sys.executable, "-c", "print('nested')"],
                cwd=tmp_path,
                minimal_env=False,
            )
        )
    finally:
        deactivate_cost_observation(session)
    assert session.popen_counts_by_category["other"] == before + 1
    payload = finalize_cost_observation(session)
    durations = payload["category_duration_seconds"]
    assert durations["other"] == 0.0
    assert durations["process_runner"] > 0.0
    satisfied = cost_observation_metrics_satisfied(payload)
    assert "subprocess_wall_seconds" not in satisfied
    assert "process_runner_seconds" in satisfied


def test_named_copy2_instrumentation_clears_only_workspace_copy_metric(tmp_path: Path) -> None:
    session = install_cost_observation()
    try:
        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_text("payload\n", encoding="utf-8")
        shutil.copy2(src, dst)
    finally:
        deactivate_cost_observation(session)
    payload = finalize_cost_observation(session)
    coverage = payload["named_entrypoint_coverage"]
    assert named_entrypoint_metrics_satisfied(coverage) == frozenset()
    missing = metrics_missing_for_report(cost_payload=payload, wheel_provenance_verified=False)
    assert "workspace_copy_seconds" in missing


def test_metrics_missing_keeps_cost_metrics_with_incomplete_duration_sidecar() -> None:
    payload = _minimal_cost_payload()
    del payload["timed_operations"]
    del payload["category_duration_seconds"]
    missing = metrics_missing_for_report(cost_payload=payload, wheel_provenance_verified=False)
    assert METRICS_FILLED_BY_COST_OBSERVATION.issubset(missing)


def test_wheel_provenance_verified_requires_both_comparisons_succeeded() -> None:
    wheel = REPO_ROOT / "label-only.whl"
    digest = "deadbeef"
    assert not wheel_provenance_verified_for_metrics(
        wheel_path=wheel,
        wheel_sha256=digest,
        wheel_provenance_before_failures=[],
        wheel_provenance_after_failures=[],
        wheel_provenance_before_comparison_succeeded=False,
        wheel_provenance_after_comparison_succeeded=False,
    )
    assert not wheel_provenance_verified_for_metrics(
        wheel_path=wheel,
        wheel_sha256=digest,
        wheel_provenance_before_failures=[],
        wheel_provenance_after_failures=[],
        wheel_provenance_before_comparison_succeeded=True,
        wheel_provenance_after_comparison_succeeded=False,
    )
    assert wheel_provenance_verified_for_metrics(
        wheel_path=wheel,
        wheel_sha256=digest,
        wheel_provenance_before_failures=[],
        wheel_provenance_after_failures=[],
        wheel_provenance_before_comparison_succeeded=True,
        wheel_provenance_after_comparison_succeeded=True,
    )


def test_run_profile_wheel_compare_raises_keeps_wheel_metrics_missing(tmp_path: Path) -> None:
    workspace = _disjoint_consumer_workspace()
    _, python = _outside_checkout_consumer_bootstrap()
    version = EXPECTED_PACKAGE_VERSION
    wheel = _outside_checkout_wheel_path()
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"gamefactory-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: gamefactory\nVersion: {version}\n".encode(),
        )
    output, junit, basetemp = _outside_checkout_profile_paths()
    rc = run_profile(
        python=python,
        test_root=REPO_ROOT,
        workspace=workspace,
        output_json=output,
        junit_xml=junit,
        basetemp_parent=basetemp,
        expected_version=version,
        collect_cwd=_outside_checkout_collect_cwd(),
        skip_pytest=True,
        wheel_path=wheel,
    )
    assert rc == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["installed_wheel_sha256"] is None
    assert payload["input_wheel_sha256_unverified"] == sha256_file_path(wheel)
    assert METRICS_FILLED_BY_WHEEL_OPTION.issubset(payload["metrics_missing"])
    assert any("wheel_provenance_before" in item for item in payload["guard_failures"])


def test_build_report_wheel_mismatch_keeps_wheel_metrics_missing() -> None:
    wheel = REPO_ROOT / "nonexistent-for-label.whl"
    digest = "abc123"
    report = build_report(
        expected_version=EXPECTED_PACKAGE_VERSION,
        test_root=REPO_ROOT,
        collect_expected=frozenset(),
        collect_actual=frozenset(),
        collect_guard_ok=True,
        provenance_before=None,
        provenance_after=None,
        child_probe_before=None,
        child_probe_after=None,
        provenance_ok=False,
        timing_payload={"nodes": []},
        wall_clock_seconds=0.0,
        pytest_exit_code=0,
        guard_failures=["wheel_provenance_before:example"],
        wheel_path=wheel,
        wheel_sha256=digest,
        wheel_provenance_before_failures=["wheel_member_byte_mismatch:gamefactory/__init__.py"],
    )
    assert report["installed_wheel_sha256"] is None
    assert report["input_wheel_sha256_unverified"] == digest
    assert METRICS_FILLED_BY_WHEEL_OPTION.issubset(report["metrics_missing"])


def test_run_profile_pytest_exit_zero_missing_cost_sidecar_keeps_cost_metrics(
    tmp_path: Path,
) -> None:
    from scripts.profile_v08_installed_shard_runtime import ProfileRuntimeError  # noqa: E402

    workspace = _disjoint_consumer_workspace()
    _, python = _bootstrap_isolated_venv(tmp_path)
    output, junit, basetemp = _outside_checkout_profile_paths()
    ci = load_frozen_candidate_ci(REPO_ROOT)
    expected = expected_shard4_nodes(ci, REPO_ROOT)
    probes = {
        "module_path": "/site-packages/gamefactory/__init__.py",
        "version": EXPECTED_PACKAGE_VERSION,
        "metadata_version": EXPECTED_PACKAGE_VERSION,
    }
    timing_payload = _minimal_valid_timing_for_nodes(expected, pytest_exit_code=0)
    shard_result = PytestShardResult(
        exit_code=0,
        collect_actual=expected,
        child_probe_before=probes,
        child_probe_after=probes,
    )
    with (
        patch(
            "scripts.profile_v08_installed_shard_runtime.run_installed_probe",
            return_value=probes,
        ),
        patch(
            "scripts.profile_v08_installed_shard_runtime.run_pytest_shard",
            return_value=shard_result,
        ),
        patch(
            "scripts.profile_v08_installed_shard_runtime.load_timing_payload",
            return_value=timing_payload,
        ),
        patch(
            "scripts.profile_v08_installed_shard_runtime.load_cost_payload",
            side_effect=ProfileRuntimeError("cost payload missing"),
        ),
    ):
        rc = run_profile(
            python=python,
            test_root=REPO_ROOT,
            workspace=workspace,
            output_json=output,
            junit_xml=junit,
            basetemp_parent=basetemp,
            expected_version=EXPECTED_PACKAGE_VERSION,
            collect_cwd=_outside_checkout_collect_cwd(),
            observe_costs=True,
        )
    assert rc == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["pytest_exit_code"] == 0
    assert payload["cost_observation"] is None
    assert METRICS_FILLED_BY_COST_OBSERVATION.issubset(payload["metrics_missing"])
    assert any("cost_observation_missing" in item for item in payload["guard_failures"])


def test_run_profile_pytest_exit_zero_wheel_provenance_failed_keeps_wheel_metrics(
    tmp_path: Path,
) -> None:
    workspace = _disjoint_consumer_workspace()
    _, python = _outside_checkout_consumer_bootstrap()
    site_packages = _site_packages_dir(python)
    version = EXPECTED_PACKAGE_VERSION
    body = f'__version__ = "{version}"\n'.encode()
    wheel = _outside_checkout_wheel_path()
    _write_tiny_gamefactory_wheel(wheel, version=version, init_body=body)
    _install_tiny_wheel_layout(site_packages, version=version, init_body=b"tampered\n")
    output, junit, basetemp = _outside_checkout_profile_paths()
    ci = load_frozen_candidate_ci(REPO_ROOT)
    expected = expected_shard4_nodes(ci, REPO_ROOT)
    probes = {
        "module_path": "/site-packages/gamefactory/__init__.py",
        "version": version,
        "metadata_version": version,
    }
    timing_payload = _minimal_valid_timing_for_nodes(expected, pytest_exit_code=0)
    shard_result = PytestShardResult(
        exit_code=0,
        collect_actual=expected,
        child_probe_before=probes,
        child_probe_after=probes,
    )
    with (
        patch(
            "scripts.profile_v08_installed_shard_runtime.run_installed_probe",
            return_value=probes,
        ),
        patch(
            "scripts.profile_v08_installed_shard_runtime.run_pytest_shard",
            return_value=shard_result,
        ),
        patch(
            "scripts.profile_v08_installed_shard_runtime.load_timing_payload",
            return_value=timing_payload,
        ),
    ):
        rc = run_profile(
            python=python,
            test_root=REPO_ROOT,
            workspace=workspace,
            output_json=output,
            junit_xml=junit,
            basetemp_parent=basetemp,
            expected_version=version,
            collect_cwd=_outside_checkout_collect_cwd(),
            wheel_path=wheel,
        )
    assert rc == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["pytest_exit_code"] == 0
    assert payload["installed_wheel_sha256"] is None
    assert payload["input_wheel_sha256_unverified"] == sha256_file_path(wheel)
    assert METRICS_FILLED_BY_WHEEL_OPTION.issubset(payload["metrics_missing"])
