#!/usr/bin/env python3
"""Compact CI helpers for the V0.8 candidate installed-wheel and real-tool gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from xml.etree import ElementTree as ET

RAW_CANDIDATE_SLOW_TOTAL = 425
CANONICAL_SLOW_NODES_REL = Path("tests/fixtures/v08_candidate_slow_nodes.txt")

EXPECTED_WHEEL_DIST_NAME = "gamefactory"
EXPECTED_WHEEL_FILENAME_PREFIX = "gamefactory-0.8.0rc1-"

CANDIDATE_SLOW_MODULES: dict[str, int] = {
    "tests/unit/test_v08_candidate_workflow.py": 88,
    "tests/unit/test_v08_candidate_evidence_cold.py": 88,
    "tests/unit/test_v08_candidate_rig_verifier_portability.py": 15,
    "tests/unit/test_v08_candidate_evidence_c2b.py": 56,
    "tests/unit/test_v08_candidate_evidence_readiness_matrix.py": 73,
    "tests/unit/test_v08_candidate_evidence_readiness_controls.py": 48,
    "tests/unit/test_v08_candidate_evidence_readiness_managed_controls.py": 17,
    "tests/unit/test_v08_candidate_evidence_readiness_upstream.py": 26,
    "tests/unit/test_v08_candidate_evidence_readiness_container.py": 14,
}

SHARD_MODULES: dict[int, tuple[str, ...]] = {
    1: (
        "tests/unit/test_v08_candidate_workflow.py",
        "tests/unit/test_v08_candidate_evidence_cold.py",
        "tests/unit/test_v08_candidate_rig_verifier_portability.py",
    ),
    2: ("tests/unit/test_v08_candidate_evidence_c2b.py",),
    3: ("tests/unit/test_v08_candidate_evidence_readiness_matrix.py",),
    4: (
        "tests/unit/test_v08_candidate_evidence_readiness_controls.py",
        "tests/unit/test_v08_candidate_evidence_readiness_managed_controls.py",
        "tests/unit/test_v08_candidate_evidence_readiness_upstream.py",
        "tests/unit/test_v08_candidate_evidence_readiness_container.py",
    ),
}

EVIDENCE_SLOW_MODULES: tuple[str, ...] = tuple(
    sorted(path for path in CANDIDATE_SLOW_MODULES if "/test_v08_candidate_evidence" in path)
)

LINUX_SHARD1_PLATFORM_SKIP_NODEIDS: frozenset[str] = frozenset(
    {
        "tests/unit/test_v08_candidate_workflow.py::test_fresh_workspace_rejects_junction_parent",
        (
            "tests/unit/test_v08_candidate_workflow.py::"
            "test_fresh_workspace_rejects_junction_parent_without_is_junction_api"
        ),
        "tests/unit/test_v08_candidate_evidence_cold.py::test_rejects_windows_junction_in_bundle",
    }
)

MARKER_PROPAGATION_GUARD_MODULES: tuple[str, ...] = (
    "tests/unit/test_v08_candidate_runtime_verify.py",
    "tests/unit/test_internal_rig_cold.py",
)

EXPECTED_PACKAGE_VERSION = "0.8.0rc1"
_COLLECTED_RE = re.compile(r"(?P<count>\d+)(?:/\d+)?\s+tests?\s+collected")
_GITHUB_ACTIONS_EXPRESSION_RE = re.compile(r"\$\{\{[^}]+\}\}")
_CANDIDATE_JOB_NAME_RE = re.compile(r"^  (candidate[-\w]+):\s*$")


def neutralize_github_actions_expressions(script: str) -> str:
    """Replace GHA template expressions so bash -n can validate run blocks offline."""
    return _GITHUB_ACTIONS_EXPRESSION_RE.sub("PLACEHOLDER", script)


def extract_candidate_job_bash_scripts(ci_yaml_text: str) -> list[tuple[str, str]]:
    """Return (job_name, bash_run_body) for each candidate-* job step using shell: bash."""
    lines = ci_yaml_text.splitlines()
    scripts: list[tuple[str, str]] = []
    job_name: str | None = None
    in_candidate_job = False
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        job_match = _CANDIDATE_JOB_NAME_RE.match(line)
        if job_match:
            job_name = job_match.group(1)
            in_candidate_job = True
            idx += 1
            continue
        if (
            in_candidate_job
            and line.startswith("  ")
            and not line.startswith("    ")
            and line.strip()
        ):
            in_candidate_job = False
            job_name = None
        if in_candidate_job and line.strip() == "shell: bash":
            run_idx = idx + 1
            while run_idx < len(lines):
                stripped = lines[run_idx].strip()
                if stripped == "":
                    run_idx += 1
                    continue
                if stripped.startswith("run: |"):
                    break
                if stripped.startswith("- "):
                    break
                run_idx += 1
            if run_idx < len(lines) and lines[run_idx].strip().startswith("run: |"):
                body_start = run_idx + 1
                body_lines: list[str] = []
                while body_start < len(lines):
                    body_line = lines[body_start]
                    if not body_line.startswith("          "):
                        break
                    body_lines.append(body_line[10:])
                    body_start += 1
                if body_lines and job_name is not None:
                    scripts.append((job_name, "\n".join(body_lines) + "\n"))
                idx = body_start
                continue
        idx += 1
    return scripts


def resolve_bash_executable() -> Path | None:
    if os.name == "nt":
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        if git_bash.is_file():
            return git_bash
    bash = os.environ.get("BASH", "bash")
    return Path(bash) if shutil.which(bash) or Path(bash).is_file() else None


def assert_bash_script_syntax(script: str, *, bash_executable: Path) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".sh",
        delete=False,
        encoding="utf-8",
        newline="\n",
    ) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        proc = subprocess.run(
            [str(bash_executable), "-n", str(script_path)],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise CandidateCiError(f"bash -n failed: {detail}")


def sample_candidate_real_xvfb_collect_script() -> str:
    """Minimal script matching candidate-v083-real generated collect wrapper shape."""
    root = "/tmp/repo"
    venv_py = "/tmp/venv/bin/python"
    collect_nodes = "/tmp/collect-nodes.txt"
    paths = (
        "/tmp/repo/tests/integration/test_v08_candidate_blender_static.py",
        "/tmp/repo/tests/integration/test_v08_candidate_runtime_godot.py",
        "/tmp/repo/tests/integration/test_v08_candidate_workflow_engine.py",
    )
    collect_cmd = (
        f'python "{root}/scripts/verify_v08_candidate_ci.py" collect-nodes '
        f'--python "{venv_py}" --repo "{root}" --collect-cwd "/tmp" '
        f'--output "{collect_nodes}" ' + " ".join(f'--path "{path}"' for path in paths)
    )
    pytest_cmd = (
        f'"{venv_py}" -m pytest --collect-only -q --rootdir "{root}" '
        f'--import-mode=importlib -o "pythonpath={root}" -p no:cacheprovider '
        + " ".join(f'"{path}"' for path in paths)
    )
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            collect_cmd,
            pytest_cmd,
            "",
        ]
    )


class CandidateCiError(RuntimeError):
    pass


@dataclass(frozen=True)
class JunitCounts:
    tests: int
    failures: int
    errors: int
    skipped: int
    identities: frozenset[str]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def canonical_slow_nodes_path(repo: Path) -> Path:
    return (repo / CANONICAL_SLOW_NODES_REL).resolve()


def _is_valid_slow_node_id(nodeid: str) -> bool:
    if "::" not in nodeid:
        return False
    file_part, _rest = nodeid.split("::", 1)
    return file_part.endswith(".py") and "/" in file_part.replace("\\", "/")


def read_canonical_slow_nodes_file(path: Path) -> frozenset[str]:
    if not path.is_file():
        raise CandidateCiError(f"canonical slow node inventory missing: {path}")
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    nodes: list[str] = []
    seen: set[str] = set()
    for lineno, line in enumerate(raw_lines, start=1):
        if not line.strip():
            raise CandidateCiError(f"canonical inventory blank line at {lineno} in {path}")
        nodeid = _normalize_node_id(line)
        if not _is_valid_slow_node_id(nodeid):
            raise CandidateCiError(f"canonical inventory invalid node at {lineno}: {line!r}")
        if nodeid in seen:
            raise CandidateCiError(f"canonical inventory duplicate node at {lineno}: {nodeid}")
        seen.add(nodeid)
        nodes.append(nodeid)
    if len(nodes) != RAW_CANDIDATE_SLOW_TOTAL:
        raise CandidateCiError(
            f"canonical inventory count {len(nodes)} != {RAW_CANDIDATE_SLOW_TOTAL} in {path}"
        )
    return frozenset(nodes)


def load_canonical_slow_nodes(repo: Path) -> frozenset[str]:
    canonical = read_canonical_slow_nodes_file(canonical_slow_nodes_path(repo))
    allowed_modules = set(CANDIDATE_SLOW_MODULES)
    for nodeid in canonical:
        module = nodeid.split("::", 1)[0]
        if module not in allowed_modules:
            raise CandidateCiError(f"canonical node outside nine slow modules: {nodeid}")
    for module in allowed_modules:
        module_nodes = {n for n in canonical if n.split("::", 1)[0] == module}
        expected = CANDIDATE_SLOW_MODULES[module]
        if len(module_nodes) != expected:
            raise CandidateCiError(
                f"canonical module {module} count {len(module_nodes)} != expected {expected}"
            )
    return canonical


def canonical_nodes_for_modules(
    canonical: frozenset[str], modules: Iterable[str]
) -> frozenset[str]:
    allowed = set(modules)
    return frozenset(n for n in canonical if n.split("::", 1)[0] in allowed)


def assert_runtime_slow_inventory_matches_canonical(
    runtime: frozenset[str], canonical: frozenset[str], *, label: str
) -> None:
    if runtime != canonical:
        missing = sorted(canonical - runtime)
        extra = sorted(runtime - canonical)
        raise CandidateCiError(
            f"{label} identity mismatch missing={missing[:5]} extra={extra[:5]} "
            f"missing_count={len(missing)} extra_count={len(extra)}"
        )


def _normalize_node_id(nodeid: str) -> str:
    stripped = nodeid.strip()
    if "::" not in stripped:
        return stripped.replace("\\", "/")
    file_part, rest = stripped.split("::", 1)
    normalized_file = file_part.replace("\\", "/")
    return f"{normalized_file}::{rest}"


def _module_dot_from_file(file_attr: str) -> str:
    path = _normalize_node_id(file_attr)
    if path.endswith(".py"):
        path = path[:-3]
    return path.replace("/", ".")


def junit_testcase_nodeid(case: ET.Element) -> str:
    file_attr = case.get("file")
    name = case.get("name")
    classname = case.get("classname")
    if not name:
        raise CandidateCiError("junit testcase missing name attribute")
    if file_attr:
        file_norm = _normalize_node_id(file_attr)
        module_dot = _module_dot_from_file(file_norm)
        if classname:
            class_norm = classname.replace("\\", "/")
            if class_norm == module_dot:
                return _normalize_node_id(f"{file_norm}::{name}")
            prefix = module_dot + "."
            if class_norm.startswith(prefix):
                class_name = class_norm[len(prefix) :]
                return _normalize_node_id(f"{file_norm}::{class_name}::{name}")
        return _normalize_node_id(f"{file_norm}::{name}")
    if classname:
        module_path = classname.replace(".", "/")
        if not module_path.endswith(".py"):
            module_path = f"{module_path}.py"
        return _normalize_node_id(f"{module_path}::{name}")
    raise CandidateCiError("junit testcase missing file/classname identity")


def _suite_direct_testcases(suite: ET.Element) -> list[ET.Element]:
    return [child for child in suite if child.tag == "testcase"]


def _case_status(case: ET.Element) -> tuple[bool, bool, bool]:
    skipped = case.find("skipped") is not None
    failed = case.find("failure") is not None
    errored = case.find("error") is not None
    return skipped, failed, errored


def _parse_int_attr(element: ET.Element, attr: str) -> int:
    raw = element.get(attr)
    if raw is None:
        raise CandidateCiError(f"junit missing required attribute {attr!r}")
    try:
        return int(raw)
    except ValueError as exc:
        raise CandidateCiError(f"junit malformed attribute {attr}={raw!r}") from exc


def parse_junit_counts(path: Path) -> tuple[int, int, int, int]:
    summary = parse_junit_summary(path)
    return summary.tests, summary.failures, summary.errors, summary.skipped


def parse_junit_summary(path: Path) -> JunitCounts:
    if not path.is_file():
        raise CandidateCiError(f"junit file missing: {path}")
    if path.stat().st_size == 0:
        raise CandidateCiError(f"junit file empty: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise CandidateCiError(f"junit XML malformed: {path}") from exc

    suites: list[ET.Element]
    if root.tag == "testsuites":
        suites = [child for child in root if child.tag == "testsuite"]
        if not suites:
            raise CandidateCiError(f"junit has no testsuite elements: {path}")
    elif root.tag == "testsuite":
        suites = [root]
    else:
        raise CandidateCiError(f"junit unknown root element {root.tag!r}: {path}")

    identities: list[str] = []
    failures = errors = skipped = 0
    for suite in suites:
        cases = _suite_direct_testcases(suite)
        suite_tests = len(cases)
        suite_failures = suite_errors = suite_skipped = 0
        for case in cases:
            nodeid = junit_testcase_nodeid(case)
            if nodeid in identities:
                raise CandidateCiError(f"junit duplicate testcase identity: {nodeid}")
            identities.append(nodeid)
            is_skipped, is_failed, is_errored = _case_status(case)
            if is_skipped:
                suite_skipped += 1
            if is_failed:
                suite_failures += 1
            if is_errored:
                suite_errors += 1
        for attr, actual in (
            ("tests", suite_tests),
            ("failures", suite_failures),
            ("errors", suite_errors),
            ("skipped", suite_skipped),
        ):
            declared = _parse_int_attr(suite, attr)
            if declared != actual:
                raise CandidateCiError(
                    f"junit suite {attr} mismatch declared={declared} actual={actual} in {path}"
                )
        failures += suite_failures
        errors += suite_errors
        skipped += suite_skipped

    tests = len(identities)
    if tests <= 0:
        raise CandidateCiError(f"junit reports zero tests: {path}")
    return JunitCounts(
        tests=tests,
        failures=failures,
        errors=errors,
        skipped=skipped,
        identities=frozenset(identities),
    )


def junit_skipped_nodeids(path: Path) -> set[str]:
    summary = parse_junit_summary(path)
    skipped: set[str] = set()
    root = ET.parse(path).getroot()
    suites = (
        [child for child in root if child.tag == "testsuite"]
        if root.tag == "testsuites"
        else [root]
    )
    for suite in suites:
        for case in _suite_direct_testcases(suite):
            if case.find("skipped") is None:
                continue
            skipped.add(junit_testcase_nodeid(case))
    if len(skipped) != summary.skipped:
        raise CandidateCiError("junit skipped node inventory does not match skipped count")
    return skipped


def assert_junit_matches_collect(
    junit_path: Path,
    collect_nodeids: frozenset[str],
) -> None:
    summary = parse_junit_summary(junit_path)
    if summary.identities != collect_nodeids:
        missing = sorted(collect_nodeids - summary.identities)
        extra = sorted(summary.identities - collect_nodeids)
        raise CandidateCiError(
            f"junit/collect identity mismatch missing={missing[:5]} extra={extra[:5]}"
        )


def verify_shard_inventory_assignments() -> None:
    assigned: dict[str, int] = {}
    for shard, modules in SHARD_MODULES.items():
        for module in modules:
            if module in assigned:
                raise CandidateCiError(f"module {module} assigned to multiple shards")
            if module not in CANDIDATE_SLOW_MODULES:
                raise CandidateCiError(f"unknown slow module in shard {shard}: {module}")
            assigned[module] = shard
    slow_set = set(CANDIDATE_SLOW_MODULES)
    if set(assigned) != slow_set:
        missing = sorted(slow_set - set(assigned))
        extra = sorted(set(assigned) - slow_set)
        raise CandidateCiError(f"shard inventory missing={missing} extra={extra}")
    evidence_assigned = [m for m in assigned if m in EVIDENCE_SLOW_MODULES]
    if len(evidence_assigned) != len(EVIDENCE_SLOW_MODULES):
        raise CandidateCiError("evidence module shard assignment incomplete")
    if len(evidence_assigned) != len(set(evidence_assigned)):
        raise CandidateCiError("duplicate evidence module assignment")


def discover_evidence_slow_modules(repo: Path) -> list[str]:
    unit = repo / "tests" / "unit"
    paths = sorted(
        f"tests/unit/{p.name}" for p in unit.glob("test_v08_candidate_evidence*.py") if p.is_file()
    )
    return paths


def verify_evidence_module_coverage(repo: Path) -> None:
    on_disk = set(discover_evidence_slow_modules(repo))
    expected = set(EVIDENCE_SLOW_MODULES)
    if on_disk != expected:
        raise CandidateCiError(
            f"evidence module coverage mismatch missing={sorted(expected - on_disk)} "
            f"extra={sorted(on_disk - expected)}"
        )


def expected_raw_count_for_modules(modules: tuple[str, ...] | list[str]) -> int:
    return sum(CANDIDATE_SLOW_MODULES[m] for m in modules)


def expected_platform_skips(platform: str, shard: int) -> frozenset[str]:
    if platform.lower() in {"linux", "darwin"} and shard == 1:
        return LINUX_SHARD1_PLATFORM_SKIP_NODEIDS
    return frozenset()


def _collect_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _absolute_testpaths(repo: Path, paths: list[str]) -> list[str]:
    resolved: list[str] = []
    for rel in paths:
        candidate = Path(rel)
        if not candidate.is_absolute():
            candidate = (repo / rel).resolve()
        resolved.append(str(candidate))
    return resolved


def _parse_collect_only_stdout(stdout: str) -> frozenset[str]:
    nodeids: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or _COLLECTED_RE.search(stripped):
            continue
        if "::" not in stripped or ".py" not in stripped.split("::", 1)[0]:
            continue
        if stripped.startswith(("=", "-", "ERROR", "!!!")):
            continue
        nodeids.append(_normalize_node_id(stripped))
    return frozenset(nodeids)


def run_pytest_collect_nodeids(
    python: Path,
    repo: Path,
    paths: list[str],
    *,
    marker: str | None = None,
    cwd: Path | None = None,
    outside_checkout: bool = True,
) -> frozenset[str]:
    abs_paths = _absolute_testpaths(repo, paths)
    cmd = [
        str(python),
        "-m",
        "pytest",
        "--collect-only",
        "-qq",
        "--rootdir",
        str(repo.resolve()),
        "-p",
        "no:cacheprovider",
    ]
    if outside_checkout:
        cmd.extend(
            [
                "--import-mode",
                "importlib",
                "-o",
                f"pythonpath={repo.resolve()}",
            ]
        )
    if marker is not None:
        cmd.extend(["-m", marker])
    cmd.extend(abs_paths)
    if outside_checkout:
        workdir = cwd if cwd is not None else Path(tempfile.gettempdir())
    else:
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
        raise CandidateCiError(
            "pytest collect-only failed:\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}\nreturncode={proc.returncode}"
        )
    nodeids = _parse_collect_only_stdout(proc.stdout)
    match = _COLLECTED_RE.search(proc.stdout)
    if match:
        declared = int(match.group("count"))
        if declared != len(nodeids):
            raise CandidateCiError(
                f"collect-only count mismatch declared={declared} parsed={len(nodeids)}"
            )
    elif proc.returncode == 5 and not nodeids:
        return frozenset()
    elif not nodeids:
        raise CandidateCiError(f"could not parse collect-only nodes from:\n{proc.stdout}")
    return nodeids


def run_pytest_collect_only(
    python: Path,
    repo: Path,
    paths: list[str],
    *,
    marker: str | None = None,
    cwd: Path | None = None,
    outside_checkout: bool = True,
) -> int:
    return len(
        run_pytest_collect_nodeids(
            python,
            repo,
            paths,
            marker=marker,
            cwd=cwd,
            outside_checkout=outside_checkout,
        )
    )


def assert_raw_inventory(
    repo: Path,
    python: Path,
    *,
    collect_cwd: Path | None = None,
) -> None:
    verify_shard_inventory_assignments()
    verify_evidence_module_coverage(repo)
    canonical = load_canonical_slow_nodes(repo)
    modules = sorted(CANDIDATE_SLOW_MODULES)
    module_union: set[str] = set()
    for module in modules:
        nodes = run_pytest_collect_nodeids(
            python, repo, [module], cwd=collect_cwd, outside_checkout=True
        )
        expected = CANDIDATE_SLOW_MODULES[module]
        if len(nodes) != expected:
            raise CandidateCiError(f"module {module} collect {len(nodes)} != expected {expected}")
        expected_nodes = canonical_nodes_for_modules(canonical, (module,))
        assert_runtime_slow_inventory_matches_canonical(
            nodes, expected_nodes, label=f"module {module} runtime"
        )
        overlap = module_union.intersection(nodes)
        if overlap:
            raise CandidateCiError(f"duplicate nodes across slow modules: {sorted(overlap)[:3]}")
        module_union.update(nodes)

    marked = run_pytest_collect_nodeids(
        python, repo, modules, marker="candidate_slow", cwd=collect_cwd, outside_checkout=True
    )
    if marked != frozenset(module_union):
        raise CandidateCiError("candidate_slow marked set != union of nine slow modules")
    assert_runtime_slow_inventory_matches_canonical(
        marked, canonical, label="candidate_slow marked collect"
    )
    assert_runtime_slow_inventory_matches_canonical(
        frozenset(module_union), canonical, label="nine-module union"
    )

    shard_union: set[str] = set()
    for shard, shard_modules in SHARD_MODULES.items():
        shard_nodes = run_pytest_collect_nodeids(
            python, repo, list(shard_modules), cwd=collect_cwd, outside_checkout=True
        )
        expected = expected_raw_count_for_modules(shard_modules)
        if len(shard_nodes) != expected:
            raise CandidateCiError(
                f"shard {shard} collect {len(shard_nodes)} != expected {expected}"
            )
        expected_shard = canonical_nodes_for_modules(canonical, shard_modules)
        assert_runtime_slow_inventory_matches_canonical(
            shard_nodes, expected_shard, label=f"shard {shard} runtime"
        )
        overlap = shard_union.intersection(shard_nodes)
        if overlap:
            raise CandidateCiError(f"shard {shard} overlaps prior shards: {sorted(overlap)[:3]}")
        shard_union.update(shard_nodes)
    assert_runtime_slow_inventory_matches_canonical(
        frozenset(shard_union), canonical, label="shard union"
    )


def assert_marker_not_propagated(
    repo: Path,
    python: Path,
    *,
    collect_cwd: Path | None = None,
) -> None:
    for module in MARKER_PROPAGATION_GUARD_MODULES:
        marked = run_pytest_collect_only(
            python,
            repo,
            [module],
            marker="candidate_slow",
            cwd=collect_cwd,
            outside_checkout=False,
        )
        if marked != 0:
            raise CandidateCiError(f"marker propagated to helper module {module}: {marked} marked")


def assert_ordinary_suite_excludes_candidate_slow(
    repo: Path,
    python: Path,
    *,
    collect_cwd: Path | None = None,
) -> None:
    all_nodes = run_pytest_collect_nodeids(
        python, repo, ["tests"], cwd=collect_cwd, outside_checkout=False
    )
    ordinary = run_pytest_collect_nodeids(
        python,
        repo,
        ["tests"],
        marker="not candidate_slow",
        cwd=collect_cwd,
        outside_checkout=False,
    )
    marked = run_pytest_collect_nodeids(
        python,
        repo,
        ["tests"],
        marker="candidate_slow",
        cwd=collect_cwd,
        outside_checkout=False,
    )
    canonical = load_canonical_slow_nodes(repo)
    if ordinary & marked:
        raise CandidateCiError("ordinary and candidate_slow marker sets overlap")
    if all_nodes != ordinary | marked:
        raise CandidateCiError(
            f"ordinary marker partition failed: all={len(all_nodes)} "
            f"ordinary={len(ordinary)} marked={len(marked)}"
        )
    assert_runtime_slow_inventory_matches_canonical(
        marked, canonical, label="full-tree candidate_slow marked"
    )
    if canonical & ordinary:
        raise CandidateCiError(
            "canonical slow nodes present in ordinary (not candidate_slow) suite"
        )


def assert_junit(
    path: Path,
    *,
    expected_tests: int,
    expected_failures: int,
    expected_errors: int,
    expected_skipped: int,
    collect_nodeids: frozenset[str] | None = None,
) -> None:
    summary = parse_junit_summary(path)
    actual = (summary.tests, summary.failures, summary.errors, summary.skipped)
    expected = (expected_tests, expected_failures, expected_errors, expected_skipped)
    if actual != expected:
        raise CandidateCiError(f"junit mismatch {path}: actual={actual} expected={expected}")
    if collect_nodeids is not None:
        assert_junit_matches_collect(path, collect_nodeids)


def assert_junit_platform_skips_only(
    path: Path,
    *,
    allowed_skips: frozenset[str],
) -> None:
    skipped = junit_skipped_nodeids(path)
    unexpected = skipped - allowed_skips
    missing = allowed_skips - skipped
    if unexpected:
        raise CandidateCiError(f"unexpected junit skips {sorted(unexpected)}")
    if missing:
        raise CandidateCiError(f"expected platform skips missing from junit: {sorted(missing)}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_wheel_dist_info(archive: zipfile.ZipFile) -> tuple[str, str, str]:
    dist_info_dirs = sorted(
        {
            name.split("/", 1)[0]
            for name in archive.namelist()
            if name.endswith(".dist-info/WHEEL") or name.endswith(".dist-info/METADATA")
        }
    )
    if len(dist_info_dirs) != 1:
        raise CandidateCiError(
            f"wheel expected exactly one dist-info directory, found {dist_info_dirs!r}"
        )
    dist_info = dist_info_dirs[0]
    if not dist_info.startswith(f"{EXPECTED_WHEEL_DIST_NAME}-"):
        raise CandidateCiError(f"unexpected dist-info directory name: {dist_info}")
    wheel_meta = archive.read(f"{dist_info}/WHEEL").decode("utf-8")
    metadata = archive.read(f"{dist_info}/METADATA").decode("utf-8")
    return dist_info, wheel_meta, metadata


def _parse_metadata_fields(metadata: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in metadata.splitlines():
        if line.startswith(" ") or line.startswith("\t"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def assert_pure_py3_none_any_wheel(wheel: Path) -> None:
    if not wheel.is_file():
        raise CandidateCiError(f"wheel missing: {wheel}")
    if not wheel.name.startswith(EXPECTED_WHEEL_FILENAME_PREFIX):
        raise CandidateCiError(f"unexpected wheel filename: {wheel.name}")
    if not wheel.name.endswith("-py3-none-any.whl"):
        raise CandidateCiError(f"wheel filename is not py3-none-any: {wheel.name}")
    with zipfile.ZipFile(wheel) as archive:
        dist_info, wheel_meta, metadata = _read_wheel_dist_info(archive)
        if dist_info != f"{EXPECTED_WHEEL_DIST_NAME}-{EXPECTED_PACKAGE_VERSION}.dist-info":
            raise CandidateCiError(f"unexpected dist-info path: {dist_info}")
        meta_fields = _parse_metadata_fields(metadata)
        if meta_fields.get("Name") != EXPECTED_WHEEL_DIST_NAME:
            raise CandidateCiError(f"wheel Name metadata mismatch: {meta_fields.get('Name')}")
        if meta_fields.get("Version") != EXPECTED_PACKAGE_VERSION:
            raise CandidateCiError(f"wheel Version metadata mismatch: {meta_fields.get('Version')}")
        wheel_lines = {
            line.split(":", 1)[0].strip(): line.split(":", 1)[1].strip()
            for line in wheel_meta.splitlines()
            if ":" in line
        }
        if wheel_lines.get("Root-Is-Purelib") != "true":
            raise CandidateCiError(f"wheel Root-Is-Purelib is not true: {wheel_meta!r}")
        tag_line = wheel_lines.get("Tag")
        if tag_line != "py3-none-any":
            raise CandidateCiError(f"wheel Tag is not py3-none-any: {wheel_meta!r}")


def verify_wheel_file_builder(
    wheel: Path,
    *,
    expected_head: str,
    repo: Path,
) -> None:
    assert_pure_py3_none_any_wheel(wheel)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    if head != expected_head:
        raise CandidateCiError(f"HEAD mismatch expected={expected_head} actual={head}")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--dry-run", "--no-deps", str(wheel)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise CandidateCiError(f"pip dry-run failed for wheel: {proc.stderr}")


def verify_wheel_file_consumer(
    wheel: Path,
    *,
    expected_sha256: str,
    expected_head: str,
    repo: Path,
    python: Path,
    workspace: Path,
) -> None:
    if not expected_sha256:
        raise CandidateCiError("consumer wheel verify requires expected_sha256")
    assert_pure_py3_none_any_wheel(wheel)
    actual_sha = sha256_file(wheel)
    if actual_sha != expected_sha256.lower():
        raise CandidateCiError(
            f"wheel sha256 mismatch expected={expected_sha256} actual={actual_sha}"
        )
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    if head != expected_head:
        raise CandidateCiError(f"HEAD mismatch expected={expected_head} actual={head}")
    verify_installed_package_outside_checkout(python, workspace)


def installed_package_outside_checkout_probe_source(
    expected_version: str = EXPECTED_PACKAGE_VERSION,
) -> str:
    """Return multiline Python executed under ``python -I`` for installed-package verification."""
    # Use explicit newlines so ``python -c`` never receives invalid semicolon-compound syntax.
    return f"""import json
import pathlib
import site
import sys
import sysconfig

import gamefactory
import importlib.metadata

ws = pathlib.Path(__import__("os").environ["GAMEFACTORY_CI_WORKSPACE"]).resolve()
expected = {expected_version!r}
mod = pathlib.Path(gamefactory.__file__).resolve()
meta = importlib.metadata.version("gamefactory")
ver = gamefactory.__version__
assert ver == expected, ver
assert meta == expected, meta
assert not mod.is_relative_to(ws), (mod, ws)

site_dirs: set[pathlib.Path] = set()
for key in ("purelib", "platlib"):
    site_dirs.add(pathlib.Path(sysconfig.get_path(key)).resolve())
venv_prefix = pathlib.Path(sys.prefix).resolve()
try:
    for entry in site.getsitepackages():
        resolved = pathlib.Path(entry).resolve()
        if resolved.is_relative_to(venv_prefix):
            site_dirs.add(resolved)
except Exception:
    pass

assert any(mod.is_relative_to(d) for d in site_dirs), (
    mod,
    sorted(str(d) for d in site_dirs),
)

dist = importlib.metadata.distribution("gamefactory")
direct = None
try:
    direct = dist.read_text("direct_url.json")
except FileNotFoundError:
    direct = None
except Exception as exc:
    raise AssertionError(f"direct_url read failed: {{exc}}") from exc

if direct:
    direct_payload = json.loads(direct)
    dir_info = direct_payload.get("dir_info") or {{}}
    assert not dir_info.get("editable"), direct_payload

payload = {{
    "module_path": str(mod),
    "version": ver,
    "metadata_version": meta,
    "python_executable": sys.executable,
    "purelib": sysconfig.get_path("purelib"),
    "platlib": sysconfig.get_path("platlib"),
    "sitepackages": [str(p) for p in sorted(site_dirs)],
}}
print(json.dumps(payload))
"""


def verify_installed_package_outside_checkout(python: Path, workspace: Path) -> dict[str, Any]:
    probe = installed_package_outside_checkout_probe_source()
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["GAMEFACTORY_CI_WORKSPACE"] = str(workspace.resolve())
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [str(python), "-I", "-c", probe],
            cwd=tmp,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    if proc.returncode != 0:
        raise CandidateCiError(
            f"installed package check failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return cast(dict[str, Any], json.loads(proc.stdout.strip()))


def assert_git_head(repo: Path, expected_head: str) -> str:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    if head != expected_head:
        raise CandidateCiError(f"HEAD mismatch expected={expected_head} actual={head}")
    return head


def assert_clean_tracked_checkout(repo: Path) -> str:
    status = subprocess.check_output(["git", "status", "--porcelain=v1"], cwd=repo, text=True)
    if status.strip():
        raise CandidateCiError(f"checkout not clean:\n{status}")
    return status


def write_source_provenance(
    repo: Path,
    output_dir: Path,
    *,
    expected_head: str | None = None,
    require_clean: bool = False,
    role: str = "builder",
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if require_clean:
        status = assert_clean_tracked_checkout(repo)
    else:
        status = subprocess.check_output(["git", "status", "--porcelain=v1"], cwd=repo, text=True)
    if expected_head is not None:
        head = assert_git_head(repo, expected_head)
    else:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (output_dir / "commit.txt").write_text(head + "\n", encoding="utf-8")
    status_name = "source-status.txt" if role == "consumer" else "builder-source-status.txt"
    (output_dir / status_name).write_text(status, encoding="utf-8", newline="\n")
    if role == "consumer":
        (output_dir / "consumer-role.txt").write_text("consumer\n", encoding="utf-8")
    else:
        (output_dir / "builder-role.txt").write_text("builder\n", encoding="utf-8")
    manifest_path = output_dir / "source-manifest.sha256"
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    paths = [p.decode("utf-8") for p in proc.stdout.split(b"\0") if p]
    lines: list[str] = []
    for rel in sorted(paths):
        lines.append(f"{sha256_file(repo / rel)}  {rel}")
    manifest_path.write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8", newline="\n"
    )
    digest_path = output_dir / "source-manifest-digest.txt"
    digest_path.write_text(
        f"{sha256_file(manifest_path)}  source-manifest.sha256\n",
        encoding="utf-8",
        newline="\n",
    )
    interpreter_path = output_dir / "interpreter-provenance.json"
    interpreter_path.write_text(
        json.dumps(
            {
                "sys_executable": sys.executable,
                "sys_version": sys.version,
                "purelib": sysconfig.get_path("purelib"),
                "platlib": sysconfig.get_path("platlib"),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"head": head, "manifest": str(manifest_path), "role": role}


CI_CANDIDATE_ARTIFACT_NAMES: tuple[str, ...] = (
    "manifest.json",
    "result.json",
    "export-marker.json",
    "trusted-cold-outcome.json",
)


def assert_nonempty_json_files(paths: Iterable[Path]) -> None:
    for path in paths:
        if not path.is_file():
            raise CandidateCiError(f"expected JSON artifact missing: {path}")
        if path.stat().st_size == 0:
            raise CandidateCiError(f"expected JSON artifact empty: {path}")
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CandidateCiError(f"expected JSON artifact malformed: {path}") from exc


def write_archive_hash_manifest(paths: Iterable[Path], output: Path) -> None:
    lines: list[str] = []
    for path in sorted(paths, key=lambda p: p.name):
        if not path.is_file():
            raise CandidateCiError(f"archive hash manifest input missing: {path}")
        lines.append(f"{sha256_file(path)}  {path.name}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8", newline="\n")


def assert_ci_candidate_workflow_artifacts(repo: Path) -> None:
    base = repo / ".verification" / "ci-candidate"
    paths = [base / name for name in CI_CANDIDATE_ARTIFACT_NAMES]
    assert_nonempty_json_files(paths)
    manifest_out = base / "archive-hash-manifest.sha256"
    write_archive_hash_manifest(paths, manifest_out)


def write_collect_nodeids_file(nodeids: frozenset[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(sorted(nodeids)) + ("\n" if nodeids else ""), encoding="utf-8", newline="\n"
    )


def read_collect_nodeids_file(path: Path) -> frozenset[str]:
    if not path.is_file():
        raise CandidateCiError(f"collect node file missing: {path}")
    nodes = frozenset(
        _normalize_node_id(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    return nodes


def _cmd_inventory(args: argparse.Namespace) -> int:
    python = Path(args.python) if args.python else Path(sys.executable)
    repo = Path(args.repo) if args.repo else _repo_root()
    cwd = Path(args.collect_cwd) if args.collect_cwd else Path(tempfile.gettempdir())
    assert_raw_inventory(repo, python, collect_cwd=cwd)
    return 0


def _cmd_marker_propagation(args: argparse.Namespace) -> int:
    python = Path(args.python) if args.python else Path(sys.executable)
    repo = Path(args.repo) if args.repo else _repo_root()
    # Full-tree marker partition needs repository cwd; module inventory uses outside cwd.
    cwd = repo.resolve()
    assert_marker_not_propagated(repo, python, collect_cwd=cwd)
    assert_ordinary_suite_excludes_candidate_slow(repo, python, collect_cwd=cwd)
    return 0


def _cmd_junit_assert(args: argparse.Namespace) -> int:
    collect_nodes: frozenset[str] | None = None
    if args.collect_nodes:
        collect_nodes = read_collect_nodeids_file(Path(args.collect_nodes))
    assert_junit(
        Path(args.junit),
        expected_tests=args.tests,
        expected_failures=args.failures,
        expected_errors=args.errors,
        expected_skipped=args.skipped,
        collect_nodeids=collect_nodes,
    )
    assert_junit_platform_skips_only(
        Path(args.junit),
        allowed_skips=frozenset(args.allowed_skip or []),
    )
    return 0


def _cmd_collect_nodes(args: argparse.Namespace) -> int:
    python = Path(args.python)
    repo = Path(args.repo) if args.repo else _repo_root()
    cwd = Path(args.collect_cwd) if args.collect_cwd else Path(tempfile.gettempdir())
    paths = args.path
    marker = args.marker
    nodes = run_pytest_collect_nodeids(
        python, repo, paths, marker=marker, cwd=cwd, outside_checkout=True
    )
    expected_shard = args.expected_shard
    if expected_shard is not None:
        shard = int(expected_shard)
        if shard not in SHARD_MODULES:
            raise CandidateCiError(f"unknown shard {shard}")
        canonical = load_canonical_slow_nodes(repo)
        expected_nodes = canonical_nodes_for_modules(canonical, SHARD_MODULES[shard])
        assert_runtime_slow_inventory_matches_canonical(
            nodes, expected_nodes, label=f"collect-nodes shard {shard}"
        )
    output = Path(args.output)
    write_collect_nodeids_file(nodes, output)
    print(len(nodes))
    return 0


def _cmd_wheel_verify(args: argparse.Namespace) -> int:
    repo = Path(args.repo) if args.repo else _repo_root()
    wheel = Path(args.wheel)
    mode = args.mode
    if mode == "builder":
        verify_wheel_file_builder(wheel, expected_head=args.expected_head, repo=repo)
        return 0
    if mode == "consumer":
        if not args.python or not args.workspace:
            raise CandidateCiError("consumer wheel verify requires --python and --workspace")
        verify_wheel_file_consumer(
            wheel,
            expected_sha256=args.expected_sha256 or "",
            expected_head=args.expected_head,
            repo=repo,
            python=Path(args.python),
            workspace=Path(args.workspace),
        )
        return 0
    raise CandidateCiError(f"unknown wheel verify mode: {mode}")


def _cmd_provenance(args: argparse.Namespace) -> int:
    repo = Path(args.repo) if args.repo else _repo_root()
    meta = write_source_provenance(
        repo,
        Path(args.output_dir),
        expected_head=args.expected_head,
        require_clean=bool(args.require_clean),
        role=args.role,
    )
    print(json.dumps(meta, sort_keys=True))
    return 0


def _cmd_assert_ci_candidate_artifacts(args: argparse.Namespace) -> int:
    repo = Path(args.repo) if args.repo else _repo_root()
    assert_ci_candidate_workflow_artifacts(repo)
    return 0


def _cmd_shard_expectations(args: argparse.Namespace) -> int:
    shard = int(args.shard)
    if shard not in SHARD_MODULES:
        raise CandidateCiError(f"unknown shard {shard}")
    modules = SHARD_MODULES[shard]
    payload: dict[str, Any] = {
        "shard": shard,
        "modules": list(modules),
        "raw_test_count": expected_raw_count_for_modules(modules),
        "platform_skip_nodeids": sorted(expected_platform_skips(args.platform, shard)),
        "expected_skipped": len(expected_platform_skips(args.platform, shard)),
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    inv = sub.add_parser("inventory", help="verify frozen 425-test shard inventory")
    inv.add_argument("--python", default=None)
    inv.add_argument("--repo", default=None)
    inv.add_argument("--collect-cwd", default=None)
    inv.set_defaults(func=_cmd_inventory)

    prop = sub.add_parser("marker-propagation", help="verify candidate_slow marker scope")
    prop.add_argument("--python", default=None)
    prop.add_argument("--repo", default=None)
    prop.add_argument("--collect-cwd", default=None)
    prop.set_defaults(func=_cmd_marker_propagation)

    junit = sub.add_parser("junit-assert", help="strict junit totals and optional skip allowlist")
    junit.add_argument("--junit", required=True)
    junit.add_argument("--tests", type=int, required=True)
    junit.add_argument("--failures", type=int, default=0)
    junit.add_argument("--errors", type=int, default=0)
    junit.add_argument("--skipped", type=int, default=0)
    junit.add_argument("--allowed-skip", action="append", default=[])
    junit.add_argument("--collect-nodes", default=None)
    junit.set_defaults(func=_cmd_junit_assert)

    collect = sub.add_parser("collect-nodes", help="write normalized collect-only node ids")
    collect.add_argument("--python", required=True)
    collect.add_argument("--output", required=True)
    collect.add_argument("--path", action="append", required=True)
    collect.add_argument("--marker", default=None)
    collect.add_argument("--repo", default=None)
    collect.add_argument("--collect-cwd", default=None)
    collect.add_argument(
        "--expected-shard",
        type=int,
        choices=(1, 2, 3, 4),
        default=None,
        help="assert collected node ids match canonical inventory for this shard",
    )
    collect.set_defaults(func=_cmd_collect_nodes)

    wheel = sub.add_parser("wheel-verify", help="verify wheel provenance and optional install")
    wheel.add_argument("--wheel", required=True)
    wheel.add_argument("--expected-head", required=True)
    wheel.add_argument("--mode", choices=("builder", "consumer"), required=True)
    wheel.add_argument("--expected-sha256", default=None)
    wheel.add_argument("--python", default=None)
    wheel.add_argument("--workspace", default=None)
    wheel.add_argument("--repo", default=None)
    wheel.set_defaults(func=_cmd_wheel_verify)

    prov = sub.add_parser("write-provenance", help="write HEAD/status/manifest under output dir")
    prov.add_argument("--output-dir", required=True)
    prov.add_argument("--repo", default=None)
    prov.add_argument("--expected-head", default=None)
    prov.add_argument("--require-clean", action="store_true")
    prov.add_argument("--role", choices=("builder", "consumer"), default="builder")
    prov.set_defaults(func=_cmd_provenance)

    artifacts = sub.add_parser(
        "assert-ci-candidate-artifacts",
        help="require four nonempty workflow JSON artifacts under .verification/ci-candidate",
    )
    artifacts.add_argument("--repo", default=None)
    artifacts.set_defaults(func=_cmd_assert_ci_candidate_artifacts)

    shard = sub.add_parser("shard-expectations", help="emit shard counts and platform skips")
    shard.add_argument("--shard", required=True)
    shard.add_argument("--platform", required=True)
    shard.set_defaults(func=_cmd_shard_expectations)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except CandidateCiError as exc:
        print(f"candidate-ci: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
