"""Narrow invariants for scripts.verify_v08_candidate_ci."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import venv
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.verify_v08_candidate_ci import (  # noqa: E402
    CANDIDATE_SLOW_MODULES,
    EVIDENCE_SLOW_MODULES,
    EXPECTED_PACKAGE_VERSION,
    RAW_CANDIDATE_SLOW_TOTAL,
    SHARD_MODULES,
    CandidateCiError,
    assert_bash_script_syntax,
    assert_junit,
    assert_pure_py3_none_any_wheel,
    assert_raw_inventory,
    assert_runtime_slow_inventory_matches_canonical,
    canonical_nodes_for_modules,
    canonical_slow_nodes_path,
    discover_evidence_slow_modules,
    expected_platform_skips,
    extract_candidate_job_bash_scripts,
    installed_package_outside_checkout_probe_source,
    junit_skipped_nodeids,
    junit_testcase_nodeid,
    load_canonical_slow_nodes,
    neutralize_github_actions_expressions,
    parse_junit_counts,
    parse_junit_summary,
    read_canonical_slow_nodes_file,
    resolve_bash_executable,
    sample_candidate_real_xvfb_collect_script,
    verify_installed_package_outside_checkout,
    verify_shard_inventory_assignments,
    verify_wheel_file_builder,
    verify_wheel_file_consumer,
    write_collect_nodeids_file,
    write_source_provenance,
)


def _write_junit(path: Path, xml_body: str) -> None:
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>' + xml_body,
        encoding="utf-8",
    )


def _good_wheel(tmp_path: Path, *, tag: str = "py3-none-any", version: str = "0.8.0rc2") -> Path:
    wheel = tmp_path / f"gamefactory-{version}-{tag}.whl"
    dist = f"gamefactory-{version}.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"{dist}/WHEEL",
            f"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: {tag}\n",
        )
        archive.writestr(
            f"{dist}/METADATA",
            f"Metadata-Version: 2.1\nName: gamefactory\nVersion: {version}\n",
        )
    return wheel


def test_candidate_ci_workflow_bash_run_blocks_pass_bash_n() -> None:
    bash = resolve_bash_executable()
    if bash is None:
        pytest.skip("bash not available")
    ci_yaml = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    scripts = extract_candidate_job_bash_scripts(ci_yaml)
    assert scripts, "expected candidate job bash run blocks in ci.yml"
    for _job_name, body in scripts:
        neutral = neutralize_github_actions_expressions(body)
        assert_bash_script_syntax(neutral, bash_executable=bash)


def test_candidate_real_xvfb_collect_script_passes_bash_n() -> None:
    bash = resolve_bash_executable()
    if bash is None:
        pytest.skip("bash not available")
    assert_bash_script_syntax(sample_candidate_real_xvfb_collect_script(), bash_executable=bash)


def test_canonical_fixture_is_frozen_425_inventory() -> None:
    path = canonical_slow_nodes_path(REPO_ROOT)
    nodes = read_canonical_slow_nodes_file(path)
    assert len(nodes) == RAW_CANDIDATE_SLOW_TOTAL
    loaded = load_canonical_slow_nodes(REPO_ROOT)
    assert loaded == nodes
    shard_union: set[str] = set()
    for shard_modules in SHARD_MODULES.values():
        shard_nodes = canonical_nodes_for_modules(loaded, shard_modules)
        assert len(shard_nodes) == sum(CANDIDATE_SLOW_MODULES[m] for m in shard_modules)
        assert not shard_union.intersection(shard_nodes)
        shard_union.update(shard_nodes)
    assert shard_union == set(loaded)


def test_canonical_fixture_rejects_duplicate_line(tmp_path: Path) -> None:
    canonical = canonical_slow_nodes_path(REPO_ROOT)
    bad = tmp_path / "dup.txt"
    lines = canonical.read_text(encoding="utf-8").splitlines()
    bad.write_text("\n".join(lines[:2] + [lines[0]] + lines[2:]) + "\n", encoding="utf-8")
    with pytest.raises(CandidateCiError, match="duplicate"):
        read_canonical_slow_nodes_file(bad)


def test_runtime_identity_mismatch_detects_substitution() -> None:
    canonical = load_canonical_slow_nodes(REPO_ROOT)
    swapped = frozenset(
        (canonical - {next(iter(canonical))}) | {"tests/unit/test_v08_candidate_workflow.py::bogus"}
    )
    assert len(swapped) == len(canonical)
    with pytest.raises(CandidateCiError, match="identity mismatch"):
        assert_runtime_slow_inventory_matches_canonical(
            swapped, canonical, label="substitution probe"
        )


def test_marker_scope_mismatch_same_count_fails() -> None:
    canonical = load_canonical_slow_nodes(REPO_ROOT)
    marked = frozenset(
        list(canonical)[:-1] + ["tests/unit/test_v08_candidate_runtime_verify.py::not_slow"]
    )
    assert len(marked) == len(canonical)
    with pytest.raises(CandidateCiError, match="identity mismatch"):
        assert_runtime_slow_inventory_matches_canonical(
            marked, canonical, label="marker scope probe"
        )


def test_consumer_provenance_distinct_from_builder(tmp_path: Path) -> None:
    builder_dir = tmp_path / "builder-source"
    consumer_dir = tmp_path / "consumer-source"
    write_source_provenance(REPO_ROOT, builder_dir, role="builder")
    head = (builder_dir / "commit.txt").read_text(encoding="utf-8").strip()
    write_source_provenance(
        REPO_ROOT,
        consumer_dir,
        expected_head=head,
        require_clean=False,
        role="consumer",
    )
    assert (builder_dir / "builder-role.txt").is_file()
    assert (consumer_dir / "consumer-role.txt").is_file()
    assert (builder_dir / "source-manifest.sha256").is_file()
    assert (consumer_dir / "source-manifest.sha256").is_file()
    assert (builder_dir / "builder-source-status.txt").is_file()
    assert (consumer_dir / "source-status.txt").is_file()
    assert not (consumer_dir / "builder-source-status.txt").exists()


def test_shard_inventory_assigns_each_slow_module_once() -> None:
    verify_shard_inventory_assignments()
    assert sum(CANDIDATE_SLOW_MODULES.values()) == RAW_CANDIDATE_SLOW_TOTAL
    evidence_in_shards = [
        module
        for modules in SHARD_MODULES.values()
        for module in modules
        if module in EVIDENCE_SLOW_MODULES
    ]
    assert len(evidence_in_shards) == len(EVIDENCE_SLOW_MODULES)
    assert len(evidence_in_shards) == len(set(evidence_in_shards))


def test_evidence_modules_on_disk_match_frozen_list() -> None:
    on_disk = discover_evidence_slow_modules(REPO_ROOT)
    assert set(on_disk) == set(EVIDENCE_SLOW_MODULES)


def test_raw_inventory_collect_only_matches_frozen_total() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        assert_raw_inventory(REPO_ROOT, Path(sys.executable), collect_cwd=Path(tmp))


def test_candidate_slow_marker_does_not_propagate_to_helper_modules() -> None:
    from scripts.verify_v08_candidate_ci import (
        assert_marker_not_propagated,
        assert_ordinary_suite_excludes_candidate_slow,
    )

    assert_marker_not_propagated(REPO_ROOT, Path(sys.executable), collect_cwd=REPO_ROOT)
    assert_ordinary_suite_excludes_candidate_slow(
        REPO_ROOT, Path(sys.executable), collect_cwd=REPO_ROOT
    )


def test_linux_shard1_platform_skip_allowlist_size() -> None:
    allowed = expected_platform_skips("linux", 1)
    assert len(allowed) == 3


def test_junit_assert_rejects_empty_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.xml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(CandidateCiError, match="empty"):
        parse_junit_counts(empty)


def test_junit_assert_rejects_zero_tests(tmp_path: Path) -> None:
    path = tmp_path / "zero.xml"
    _write_junit(
        path,
        '<testsuite name="x" tests="0" failures="0" errors="0" skipped="0"></testsuite>',
    )
    with pytest.raises(CandidateCiError, match="zero tests"):
        parse_junit_counts(path)


def test_junit_rejects_header_case_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "mismatch.xml"
    _write_junit(
        path,
        '<testsuite name="x" tests="6" failures="0" errors="0" skipped="0">'
        '<testcase classname="tests.integration.test_x" file="tests/integration/test_x.py" '
        'name="only_one" time="0.1"/>'
        "</testsuite>",
    )
    with pytest.raises(CandidateCiError, match="tests mismatch"):
        parse_junit_counts(path)


def test_junit_rejects_duplicate_identity(tmp_path: Path) -> None:
    path = tmp_path / "dup.xml"
    _write_junit(
        path,
        '<testsuite name="x" tests="2" failures="0" errors="0" skipped="0">'
        '<testcase classname="tests.integration.test_x" file="tests/integration/test_x.py" '
        'name="t" time="0.1"/>'
        '<testcase classname="tests.integration.test_x" file="tests/integration/test_x.py" '
        'name="t" time="0.1"/>'
        "</testsuite>",
    )
    with pytest.raises(CandidateCiError, match="duplicate"):
        parse_junit_summary(path)


def test_junit_accepts_reconciled_totals(tmp_path: Path) -> None:
    path = tmp_path / "ok.xml"
    cases = "".join(
        f'<testcase classname="tests.integration.test_x" file="tests/integration/test_x.py" '
        f'name="t{i}" time="0.1"/>'
        for i in range(6)
    )
    _write_junit(
        path,
        f'<testsuite name="x" tests="6" failures="0" errors="0" skipped="0">{cases}</testsuite>',
    )
    assert_junit(path, expected_tests=6, expected_failures=0, expected_errors=0, expected_skipped=0)


def test_junit_rejects_substituted_node_same_count(tmp_path: Path) -> None:
    path = tmp_path / "subst.xml"
    collect_nodes = frozenset({f"tests/integration/test_x.py::t{i}" for i in range(6)})
    _write_junit(
        path,
        '<testsuite name="x" tests="6" failures="0" errors="0" skipped="0">'
        '<testcase classname="tests.integration.test_x" file="tests/integration/test_x.py" '
        'name="wrong" time="0.1"/>'
        + "".join(
            f'<testcase classname="tests.integration.test_x" file="tests/integration/test_x.py" '
            f'name="t{i}" time="0.1"/>'
            for i in range(5)
        )
        + "</testsuite>",
    )
    with pytest.raises(CandidateCiError, match="identity mismatch"):
        assert_junit(
            path,
            expected_tests=6,
            expected_failures=0,
            expected_errors=0,
            expected_skipped=0,
            collect_nodeids=collect_nodes,
        )


def test_junit_skip_nodeids_detect_unexpected(tmp_path: Path) -> None:
    path = tmp_path / "skip.xml"
    ET.ElementTree(
        ET.fromstring(
            """
            <testsuite tests="2" failures="0" errors="0" skipped="1">
              <testcase file="tests/unit/a.py" name="ok" classname="tests.unit.a" time="0.1"/>
              <testcase file="tests/unit/a.py" name="skip_me" classname="tests.unit.a" time="0.0">
                <skipped message="platform"/>
              </testcase>
            </testsuite>
            """
        )
    ).write(path, encoding="utf-8", xml_declaration=True)
    skipped = junit_skipped_nodeids(path)
    assert skipped == {"tests/unit/a.py::skip_me"}


def test_junit_class_nodeid_normalization() -> None:
    case = ET.fromstring(
        '<testcase file="tests/unit/a.py" name="method" '
        'classname="tests.unit.a.TestClass" time="0.1"/>'
    )
    assert junit_testcase_nodeid(case) == "tests/unit/a.py::TestClass::method"


def test_wheel_rejects_foreign_tag(tmp_path: Path) -> None:
    wheel = _good_wheel(tmp_path, tag="cp311-win_amd64")
    with pytest.raises(CandidateCiError, match="filename"):
        assert_pure_py3_none_any_wheel(wheel)


def test_wheel_rejects_ambiguous_metadata(tmp_path: Path) -> None:
    wheel = tmp_path / "gamefactory-0.8.0rc2-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "gamefactory-0.8.0rc2.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            "other-1.0.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: other\nVersion: 1.0.0\n",
        )
        archive.writestr(
            "gamefactory-0.8.0rc2.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: gamefactory\nVersion: 0.8.0rc2\n",
        )
    with pytest.raises(CandidateCiError, match="exactly one dist-info"):
        assert_pure_py3_none_any_wheel(wheel)


def test_wheel_sha_mismatch_raises(tmp_path: Path) -> None:
    wheel = _good_wheel(tmp_path)
    actual_sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    wrong_sha = "0" * 64
    assert actual_sha != wrong_sha
    head = (
        __import__("subprocess")
        .check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True)
        .strip()
    )
    with pytest.raises(CandidateCiError, match="sha256 mismatch"):
        verify_wheel_file_consumer(
            wheel,
            expected_sha256=wrong_sha,
            expected_head=head,
            repo=REPO_ROOT,
            python=Path(sys.executable),
            workspace=REPO_ROOT,
        )


def test_wheel_builder_accepts_minimal_pure_wheel(tmp_path: Path) -> None:
    wheel = _good_wheel(tmp_path)
    head = (
        __import__("subprocess")
        .check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True)
        .strip()
    )
    verify_wheel_file_builder(wheel, expected_head=head, repo=REPO_ROOT)


def test_collect_nodes_roundtrip(tmp_path: Path) -> None:
    from scripts.verify_v08_candidate_ci import read_collect_nodeids_file

    nodes = frozenset({"tests/unit/a.py::t1", "tests/unit/a.py::TestC::t2"})
    path = tmp_path / "nodes.txt"
    write_collect_nodeids_file(nodes, path)
    assert read_collect_nodeids_file(path) == nodes


def test_collect_nodes_cli_expected_shard_accepts_exact_canonical_subset(
    tmp_path: Path,
) -> None:
    from scripts.verify_v08_candidate_ci import main, read_collect_nodeids_file

    canonical = load_canonical_slow_nodes(REPO_ROOT)
    shard = 2
    expected = canonical_nodes_for_modules(canonical, SHARD_MODULES[shard])
    output = tmp_path / "shard-collect.txt"
    module = SHARD_MODULES[shard][0]
    with patch(
        "scripts.verify_v08_candidate_ci.run_pytest_collect_nodeids",
        return_value=expected,
    ):
        rc = main(
            [
                "collect-nodes",
                "--python",
                sys.executable,
                "--repo",
                str(REPO_ROOT),
                "--output",
                str(output),
                "--path",
                module,
                "--expected-shard",
                str(shard),
            ]
        )
    assert rc == 0
    assert read_collect_nodeids_file(output) == expected
    assert len(expected) == CANDIDATE_SLOW_MODULES[module]


def test_collect_nodes_cli_expected_shard_rejects_same_count_substitution(
    tmp_path: Path,
) -> None:
    from scripts.verify_v08_candidate_ci import main

    canonical = load_canonical_slow_nodes(REPO_ROOT)
    shard = 2
    expected = canonical_nodes_for_modules(canonical, SHARD_MODULES[shard])
    swapped = frozenset(
        (expected - {next(iter(expected))})
        | {"tests/unit/test_v08_candidate_evidence_c2b.py::bogus_substitute"}
    )
    assert len(swapped) == len(expected)
    output = tmp_path / "shard-collect-bad.txt"
    module = SHARD_MODULES[shard][0]
    with patch(
        "scripts.verify_v08_candidate_ci.run_pytest_collect_nodeids",
        return_value=swapped,
    ):
        rc = main(
            [
                "collect-nodes",
                "--python",
                sys.executable,
                "--repo",
                str(REPO_ROOT),
                "--output",
                str(output),
                "--path",
                module,
                "--expected-shard",
                str(shard),
            ]
        )
    assert rc == 1
    assert not output.is_file()


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
    direct_url: str | None = None,
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
    if direct_url is not None:
        (dist_info / "direct_url.json").write_text(direct_url, encoding="utf-8")


def _bootstrap_isolated_venv(tmp_path: Path) -> tuple[Path, Path, Path]:
    venv_dir = tmp_path / "probe-venv"
    venv.create(venv_dir, with_pip=False, clear=True)
    python = _venv_python(venv_dir)
    site_packages = _site_packages_dir(python)
    _write_minimal_gamefactory_dist(site_packages)
    return venv_dir, python, site_packages


def test_installed_package_probe_source_compiles() -> None:
    source = installed_package_outside_checkout_probe_source()
    ast.parse(source)
    compile(source, "<installed-package-probe>", "exec")


def test_installed_package_probe_source_has_no_semicolon_compound_syntax() -> None:
    source = installed_package_outside_checkout_probe_source()
    assert "for key in" in source
    assert "try:" in source
    assert "if direct:" in source
    assert "; for " not in source
    assert "; try:" not in source


def test_verify_installed_package_outside_checkout_success_in_temp_venv(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _, python, _ = _bootstrap_isolated_venv(tmp_path)
    with tempfile.TemporaryDirectory():
        payload = verify_installed_package_outside_checkout(python, workspace)
    assert payload["version"] == EXPECTED_PACKAGE_VERSION
    assert payload["metadata_version"] == EXPECTED_PACKAGE_VERSION
    assert "sitepackages" in payload
    assert payload["python_executable"] == str(python.resolve())


def test_verify_installed_package_rejects_wrong_module_version(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    venv_dir = tmp_path / "probe-venv"
    venv.create(venv_dir, with_pip=False, clear=True)
    python = _venv_python(venv_dir)
    site_packages = _site_packages_dir(python)
    _write_minimal_gamefactory_dist(
        site_packages,
        module_body='__version__ = "0.0.0"\n',
    )
    with tempfile.TemporaryDirectory():
        with pytest.raises(CandidateCiError, match="installed package check failed"):
            verify_installed_package_outside_checkout(python, workspace)


def test_verify_installed_package_rejects_wrong_metadata_version(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    venv_dir = tmp_path / "probe-venv"
    venv.create(venv_dir, with_pip=False, clear=True)
    python = _venv_python(venv_dir)
    site_packages = _site_packages_dir(python)
    _write_minimal_gamefactory_dist(
        site_packages,
        version="0.8.0rc0",
        module_body=f'__version__ = "{EXPECTED_PACKAGE_VERSION}"\n',
    )
    with tempfile.TemporaryDirectory():
        with pytest.raises(CandidateCiError, match="installed package check failed"):
            verify_installed_package_outside_checkout(python, workspace)


def test_verify_installed_package_rejects_module_outside_site_packages(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    venv_dir = tmp_path / "probe-venv"
    venv.create(venv_dir, with_pip=False, clear=True)
    python = _venv_python(venv_dir)
    site_packages = _site_packages_dir(python)
    outside_root = tmp_path / "outside_pkg"
    outside_pkg = outside_root / "gamefactory"
    outside_pkg.mkdir(parents=True)
    (outside_pkg / "__init__.py").write_text(
        f'__version__ = "{EXPECTED_PACKAGE_VERSION}"\n',
        encoding="utf-8",
    )
    dist_info = site_packages / f"gamefactory-{EXPECTED_PACKAGE_VERSION}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: gamefactory\nVersion: {EXPECTED_PACKAGE_VERSION}\n",
        encoding="utf-8",
    )
    (site_packages / "outside.pth").write_text(str(outside_root) + "\n", encoding="utf-8")
    with tempfile.TemporaryDirectory():
        with pytest.raises(CandidateCiError, match="installed package check failed"):
            verify_installed_package_outside_checkout(python, workspace)


def test_verify_installed_package_rejects_editable_direct_url(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    venv_dir = tmp_path / "probe-venv"
    venv.create(venv_dir, with_pip=False, clear=True)
    python = _venv_python(venv_dir)
    site_packages = _site_packages_dir(python)
    editable_url = json.dumps(
        {
            "dir_info": {"editable": True},
            "url": "file:///tmp/fake",
        }
    )
    _write_minimal_gamefactory_dist(site_packages, direct_url=editable_url)
    with tempfile.TemporaryDirectory():
        with pytest.raises(CandidateCiError, match="installed package check failed"):
            verify_installed_package_outside_checkout(python, workspace)


def test_verify_installed_package_not_syntax_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _, python, _ = _bootstrap_isolated_venv(tmp_path)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["GAMEFACTORY_CI_WORKSPACE"] = str(workspace.resolve())
    probe = installed_package_outside_checkout_probe_source()
    with tempfile.TemporaryDirectory() as isolated_cwd:
        proc = subprocess.run(
            [str(python), "-I", "-c", probe],
            cwd=isolated_cwd,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    assert proc.returncode == 0, proc.stderr
    assert "SyntaxError" not in proc.stderr
