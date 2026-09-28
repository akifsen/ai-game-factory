#!/usr/bin/env python3
"""Verify Blender embedded Python dependencies and module-path resolution.

Runs preflight dependency inspection via ProcessRunner with minimal environment:
1. Without configured module paths (informational only; records baseline).
2. With configured module paths (must PASS with required modules available, else exits non-zero).
Writes machine-readable JSON evidence to the requested output file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path, PurePath
from typing import Any

from gamefactory.adapters.dcc.blender import BlenderAdapter
from gamefactory.adapters.dcc.blender_environment import (
    REQUIRED_BLENDER_PYTHON_MODULES,
    format_blender_preflight_failure_message,
    parse_blender_python_paths,
    run_blender_dependency_preflight,
)
from gamefactory.core.domain.models import utc_now_iso
from gamefactory.core.execution.process_runner import ProcessRunner


def is_path_under_prefix(target: str | Path | None, prefix: str | Path) -> bool:
    """Check if target path is equal to or contained within prefix.

    Performs normalized, path-component-aware comparison without naive substring matching.
    """
    if target is None:
        return False
    target_str = str(target).strip()
    prefix_str = str(prefix).strip()
    if not target_str or not prefix_str:
        return False

    t_path = Path(target_str)
    p_path = Path(prefix_str)
    if t_path.exists():
        try:
            t_path = t_path.resolve()
        except OSError:
            pass
    if p_path.exists():
        try:
            p_path = p_path.resolve()
        except OSError:
            pass

    norm_t = PurePath(os.path.normpath(str(t_path)))
    norm_p = PurePath(os.path.normpath(str(p_path)))

    return norm_t.is_relative_to(norm_p)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify Blender embedded Python runtime dependencies."
    )
    parser.add_argument(
        "--blender",
        type=Path,
        default=None,
        help="Path to Blender executable (defaults to host detection).",
    )
    parser.add_argument(
        "--python-path",
        dest="python_paths",
        action="append",
        default=[],
        help="Python module search path (repeatable or os.pathsep-delimited).",
    )
    parser.add_argument(
        "--forbid-python-prefix",
        dest="forbidden_prefixes",
        action="append",
        default=[],
        help="Forbidden Python prefix path (repeatable).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path to output JSON evidence report.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Timeout in seconds for preflight probe executions.",
    )
    return parser.parse_args(argv)


def _resolve_blender_executable(explicit: Path | None, runner: ProcessRunner) -> Path:
    if explicit is not None:
        p = explicit.expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"Specified Blender executable does not exist: {p}")
        return p

    detection = BlenderAdapter(runner).detect_tool()
    if not detection.available or not detection.executable_path:
        raise RuntimeError(
            f"Blender executable not available on host: {detection.details.get('reason', 'unknown')}"
        )
    return Path(detection.executable_path).resolve()


def _collect_python_paths(raw_list: list[str]) -> tuple[Path, ...]:
    if not raw_list:
        return ()
    joined = os.pathsep.join(raw_list)
    return parse_blender_python_paths(joined)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    runner = ProcessRunner(sanitize_output=True)

    try:
        blender_exe = _resolve_blender_executable(args.blender, runner)
    except Exception as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1

    try:
        configured_paths = _collect_python_paths(args.python_paths)
    except Exception as exc:
        sys.stderr.write(f"ERROR: Invalid Python path argument: {exc}\n")
        return 1

    # (a) Unconfigured preflight - INFORMATIONAL only, never fail on it
    unconfigured_preflight = run_blender_dependency_preflight(
        blender_executable=blender_exe,
        runner=runner,
        python_paths=(),
        timeout_seconds=args.timeout,
    )

    # (b) Configured preflight - Must PASS with all required modules
    configured_preflight = run_blender_dependency_preflight(
        blender_executable=blender_exe,
        runner=runner,
        python_paths=configured_paths,
        timeout_seconds=args.timeout,
    )

    unconf_numpy = unconfigured_preflight.modules.get("numpy", {})
    unconf_status = "PASS" if unconf_numpy.get("available", False) else "FAIL"

    ctypes_mod = configured_preflight.runtime_modules.get("ctypes", {})
    ctypes_status = "PASS" if ctypes_mod.get("available", False) else "FAIL"

    numpy_mod = configured_preflight.modules.get("numpy", {})
    numpy_status = "PASS" if numpy_mod.get("available", False) else "FAIL"
    numpy_version = numpy_mod.get("version") or "(unknown)"
    numpy_path = numpy_mod.get("file") or "(unknown)"

    paths_display = (
        os.pathsep.join(str(p) for p in configured_paths)
        if configured_paths
        else "(none configured)"
    )

    prefix_violations: list[str] = []
    candidates = [
        ("sys.executable", configured_preflight.python_executable),
        ("sys.prefix", configured_preflight.python_prefix),
        ("sys.base_prefix", configured_preflight.python_base_prefix),
    ]
    for forbidden in args.forbidden_prefixes:
        for name, cand in candidates:
            if cand and is_path_under_prefix(cand, forbidden):
                prefix_violations.append(
                    f"{name} ({cand}) is equal to or under forbidden prefix {forbidden}"
                )

    prefix_check_status = "FAIL" if prefix_violations else "PASS"
    forbidden_display = (
        ", ".join(str(p) for p in args.forbidden_prefixes) if args.forbidden_prefixes else "(none)"
    )

    print("BLENDER_DEPENDENCY_CHECK")
    print(
        f"blender: {configured_preflight.executable} "
        f"{configured_preflight.blender_version or 'unknown'}"
    )
    print(f"unconfigured python: {unconfigured_preflight.python_executable or 'unknown'}")
    print(f"numpy without explicit path -> {unconf_status} (informational)")
    print(f"Blender Python version: {configured_preflight.python_version or 'unknown'}")
    print(f"sys.executable: {configured_preflight.python_executable or 'unknown'}")
    print(f"sys.prefix: {configured_preflight.python_prefix or 'unknown'}")
    print(f"sys.base_prefix: {configured_preflight.python_base_prefix or 'unknown'}")
    print(f"ctypes -> {ctypes_status}")
    print(f"numpy -> {numpy_status}")
    print(f"numpy version: {numpy_version}")
    print(f"numpy path: {numpy_path}")
    print(f"configured paths: {paths_display}")
    print(f"prefix contamination -> {prefix_check_status} (forbidden: {forbidden_display})")

    prefix_pass = len(prefix_violations) == 0
    configured_pass = configured_preflight.status == "PASS"
    overall_status = "PASS" if (configured_pass and prefix_pass) else "FAIL"

    evidence: dict[str, Any] = {
        "status": overall_status,
        "timestamp": utc_now_iso(),
        "blender_executable": str(blender_exe),
        "configured_python_paths": [str(p) for p in configured_paths],
        "required_modules": list(REQUIRED_BLENDER_PYTHON_MODULES),
        "runtime_modules": configured_preflight.runtime_modules,
        "prefix_check": {
            "status": "PASS" if prefix_pass else "FAIL",
            "forbidden_prefixes": list(args.forbidden_prefixes),
            "violations": prefix_violations,
        },
        "unconfigured_preflight": unconfigured_preflight.to_dict(),
        "configured_preflight": configured_preflight.to_dict(),
    }

    if args.output is not None:
        out_path = args.output.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")

    has_error = False
    if configured_preflight.status != "PASS":
        sys.stderr.write(format_blender_preflight_failure_message(configured_preflight) + "\n")
        has_error = True

    if prefix_violations:
        sys.stderr.write(
            "Blender Python prefix contamination check failed:\n"
            + "\n".join(f"  {v}" for v in prefix_violations)
            + "\n"
        )
        has_error = True

    if has_error:
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
