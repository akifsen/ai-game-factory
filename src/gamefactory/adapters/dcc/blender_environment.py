"""Deterministic Blender Python module-path contracts and dependency preflight.

Ensures required Python modules (such as numpy) are resolvable inside Blender's
embedded Python environment even when host execution environments (e.g. CI)
alter system PATH or isolate library directories.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner

BLENDER_PYTHONPATH_ENV = "GAMEFACTORY_BLENDER_PYTHONPATH"
REQUIRED_BLENDER_PYTHON_MODULES: tuple[str, ...] = ("numpy",)
BLENDER_PYTHON_FAILURE_EXIT_CODE = 17
MIN_SUPPORTED_BLENDER_VERSION: tuple[int, int, int] = (4, 0, 2)
BLENDER_PREFLIGHT_MARKER = "GAMEFACTORY_BLENDER_PREFLIGHT_WRITTEN"
_PREFLIGHT_MARKER = "GAMEFACTORY_BLENDER_PREFLIGHT="


def extract_preflight_result_path(args: Sequence[str]) -> Path | None:
    """Extract the preflight result file path from command line arguments."""
    try:
        if "--" in args:
            trailing = list(args[args.index("--") + 1 :])
            if "--result" in trailing:
                return Path(trailing[trailing.index("--result") + 1])
        if "--result" in args:
            return Path(args[args.index("--result") + 1])
    except (ValueError, IndexError):
        pass
    return None


def _preflight_result_path_from_code(code: str) -> Path | None:
    """Extract embedded result file path from preflight Python expression."""
    match = re.search(r'result_path\s*=\s*(["\'])(.*?)\1', code)
    if match:
        return Path(match.group(2))
    return None


def _generate_preflight_script(
    required_modules: Sequence[str],
    result_path: Path,
) -> str:
    modules_repr = repr(list(required_modules))
    path_repr = repr(str(result_path))
    return f"""import importlib, json, sys
result_path = {path_repr}
if "--" in sys.argv:
    trailing = sys.argv[sys.argv.index("--") + 1:]
    if "--result" in trailing:
        result_path = trailing[trailing.index("--result") + 1]
elif "--result" in sys.argv:
    result_path = sys.argv[sys.argv.index("--result") + 1]

blender_version = None
try:
    import bpy
    blender_version = getattr(getattr(bpy, "app", None), "version_string", None)
    if not blender_version and hasattr(getattr(bpy, "app", None), "version"):
        blender_version = ".".join(str(x) for x in bpy.app.version)
except Exception:
    blender_version = None

modules = {{}}
for name in {modules_repr}:
    try:
        mod = importlib.import_module(name)
        v = getattr(mod, "__version__", None)
        f = getattr(mod, "__file__", None)
        modules[name] = {{
            "available": True,
            "version": str(v) if v is not None else None,
            "file": str(f) if f is not None else None,
            "error": None,
        }}
    except Exception as exc:
        modules[name] = {{
            "available": False,
            "version": None,
            "file": None,
            "error": f"{{type(exc).__name__}}: {{exc}}",
        }}

payload = {{
    "blender_version": str(blender_version) if blender_version is not None else None,
    "python_version": sys.version,
    "python_version_info": list(sys.version_info),
    "python_executable": sys.executable,
    "python_prefix": sys.prefix,
    "python_base_prefix": getattr(sys, "base_prefix", None),
    "sys_path": list(sys.path),
    "modules": modules,
}}
with open(result_path, "w", encoding="utf-8") as f:
    json.dump(payload, f)
print("{BLENDER_PREFLIGHT_MARKER}")
"""


def parse_blender_version(version_str: str | None) -> tuple[int, int, int] | None:
    """Parse major, minor, patch version numbers from a Blender version string."""
    if not version_str:
        return None
    match = re.search(r"\b(\d+)\.(\d+)(?:\.(\d+))?", version_str)
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3)) if match.group(3) is not None else 0
    return major, minor, patch


def parse_blender_python_paths(raw: str | Sequence[Path | str] | None) -> tuple[Path, ...]:
    """Parse, validate, normalize, and deduplicate Blender Python module paths.

    Splits strings by os.pathsep, validates that segments are non-empty, absolute,
    existing directories, normalizes them, and preserves first-occurrence order.
    """
    if raw is None:
        return ()

    raw_items: list[str] = []
    if isinstance(raw, str):
        stripped_raw = raw.strip()
        if not stripped_raw:
            return ()
        segments = raw.split(os.pathsep)
        for seg in segments:
            stripped = seg.strip()
            if not stripped:
                raise ValueError(
                    f"Invalid empty segment in Blender Python path specification: {raw!r}"
                )
            raw_items.append(stripped)
    elif isinstance(raw, Sequence):
        for item in raw:
            item_str = str(item).strip()
            if not item_str:
                raise ValueError("Invalid empty path in Blender Python path list")
            raw_items.append(item_str)
    else:
        raise ValueError(
            f"Expected str, Sequence, or None for Blender Python paths, got {type(raw)}"
        )

    normalized_paths: list[Path] = []
    seen: set[str] = set()

    for item_str in raw_items:
        p = Path(item_str)
        if not p.is_absolute():
            raise ValueError(f"Blender Python path must be absolute: {item_str!r}")
        normalized = Path(os.path.abspath(os.path.normpath(str(p))))
        if not normalized.exists():
            raise ValueError(f"Blender Python path does not exist: {normalized}")
        if not normalized.is_dir():
            raise ValueError(f"Blender Python path is not a directory: {normalized}")

        dedupe_key = os.path.normcase(str(normalized))
        if dedupe_key not in seen:
            seen.add(dedupe_key)
            normalized_paths.append(normalized)

    return tuple(normalized_paths)


def blender_env_overrides(paths: Sequence[Path | str]) -> dict[str, str]:
    """Build environment overrides for Blender process execution.

    Explicitly forwards only the configured module paths via PYTHONPATH.
    Never reads or forwards host PYTHONPATH/PYTHONHOME.
    """
    if paths:
        return {"PYTHONPATH": os.pathsep.join(str(p) for p in paths)}
    return {}


@dataclass(frozen=True)
class BlenderDependencyPreflight:
    """Outcome of verifying Blender Python runtime and required dependencies."""

    status: str
    executable: str
    blender_version: str | None
    python_version: str | None
    python_executable: str | None
    python_prefix: str | None
    sys_path: list[str] = field(default_factory=list)
    configured_python_paths: list[str] = field(default_factory=list)
    modules: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 0
    reason: str | None = None
    stdout_excerpt: str | None = None
    stderr_excerpt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return serializable dictionary representation."""
        return asdict(self)


def format_blender_preflight_failure_message(preflight: BlenderDependencyPreflight) -> str:
    """Build an actionable human-readable failure message without leaking secrets."""
    missing = [name for name, info in preflight.modules.items() if not info.get("available", False)]
    missing_str = ", ".join(missing) if missing else "(none)"
    paths_str = (
        os.pathsep.join(preflight.configured_python_paths)
        if preflight.configured_python_paths
        else "(none configured; set GAMEFACTORY_BLENDER_PYTHONPATH)"
    )
    lines = [
        "Blender dependency preflight failed:",
        f"  blender_executable: {preflight.executable}",
        f"  blender_version: {preflight.blender_version or '(unknown)'}",
        f"  missing_modules: {missing_str}",
        f"  python_version: {preflight.python_version or '(unknown)'}",
        f"  python_executable: {preflight.python_executable or '(unknown)'}",
        f"  configured_python_paths: {paths_str}",
        f"  failure_reason: {preflight.reason or 'unknown failure'}",
        "  hint: Ensure required Python modules are installed and set GAMEFACTORY_BLENDER_PYTHONPATH to the directory containing them (e.g. system python dist-packages).",
    ]
    if preflight.stderr_excerpt:
        lines.append(f"  stderr: {preflight.stderr_excerpt.strip()}")
    return "\n".join(lines)


def run_blender_dependency_preflight(
    blender_executable: Path | str,
    runner: ProcessRunner,
    python_paths: Sequence[Path | str] = (),
    required_modules: Sequence[str] = REQUIRED_BLENDER_PYTHON_MODULES,
    timeout_seconds: float = 60.0,
    check_version: bool = True,
) -> BlenderDependencyPreflight:
    """Execute a background preflight probe inside Blender's Python runtime.

    Runs Blender with --background, --factory-startup, --python-exit-code 17,
    and --python-expr to safely interrogate the embedded Python interpreter and
    required modules without raising unhandled exceptions in the DCC process.
    Writes probe result to a private temporary result file to prevent ProcessRunner
    output sanitization from redacting numeric version tokens.
    """
    exe_str = str(blender_executable)
    configured_paths_list = [str(p) for p in python_paths]
    env_overrides = blender_env_overrides(python_paths)

    with tempfile.TemporaryDirectory(
        prefix="gf_blender_preflight_", ignore_cleanup_errors=True
    ) as temp_dir_str:
        temp_dir = Path(temp_dir_str).resolve()
        result_file = temp_dir / "preflight_result.json"

        code = _generate_preflight_script(required_modules, result_file)
        cmd = [
            exe_str,
            "--background",
            "--factory-startup",
            "--python-exit-code",
            str(BLENDER_PYTHON_FAILURE_EXIT_CODE),
            "--python-expr",
            code,
            "--",
            "--result",
            str(result_file),
        ]

        req = CommandRequest(
            args=cmd,
            cwd=temp_dir,
            env_overrides=env_overrides,
            timeout_seconds=timeout_seconds,
            minimal_env=True,
        )

        res = runner.run(req)

        stdout_excerpt = res.stdout[-4000:] if len(res.stdout) > 4000 else res.stdout
        stderr_excerpt = res.stderr[-4000:] if len(res.stderr) > 4000 else res.stderr

        if res.exit_code != 0:
            return BlenderDependencyPreflight(
                status="FAIL",
                executable=exe_str,
                blender_version=None,
                python_version=None,
                python_executable=None,
                python_prefix=None,
                sys_path=[],
                configured_python_paths=configured_paths_list,
                modules={},
                exit_code=res.exit_code,
                reason=f"Blender process exited with code {res.exit_code}",
                stdout_excerpt=stdout_excerpt,
                stderr_excerpt=stderr_excerpt,
            )

        if not result_file.is_file():
            return BlenderDependencyPreflight(
                status="FAIL",
                executable=exe_str,
                blender_version=None,
                python_version=None,
                python_executable=None,
                python_prefix=None,
                sys_path=[],
                configured_python_paths=configured_paths_list,
                modules={},
                exit_code=res.exit_code,
                reason=f"Preflight result file not found: {result_file}",
                stdout_excerpt=stdout_excerpt,
                stderr_excerpt=stderr_excerpt,
            )

        try:
            raw_text = result_file.read_text(encoding="utf-8")
            data = json.loads(raw_text)
        except (json.JSONDecodeError, OSError) as exc:
            return BlenderDependencyPreflight(
                status="FAIL",
                executable=exe_str,
                blender_version=None,
                python_version=None,
                python_executable=None,
                python_prefix=None,
                sys_path=[],
                configured_python_paths=configured_paths_list,
                modules={},
                exit_code=res.exit_code,
                reason=f"Failed to parse preflight JSON: {exc}",
                stdout_excerpt=stdout_excerpt,
                stderr_excerpt=stderr_excerpt,
            )

        blender_version = data.get("blender_version")
        python_version = data.get("python_version")
        python_executable = data.get("python_executable")
        python_prefix = data.get("python_prefix")
        sys_path = data.get("sys_path", [])
        modules_info = data.get("modules", {})

        failure_reason: str | None = None

        if check_version:
            parsed_ver = parse_blender_version(blender_version)
            if parsed_ver is None:
                failure_reason = (
                    f"Blender version {blender_version!r} is unknown or could not be parsed"
                )
            elif parsed_ver < MIN_SUPPORTED_BLENDER_VERSION:
                failure_reason = (
                    f"Blender version {blender_version!r} is below minimum supported version 4.0.2"
                )

        if failure_reason is None:
            missing_modules = [
                mod_name
                for mod_name, mod_data in modules_info.items()
                if not mod_data.get("available", False)
            ]
            if missing_modules:
                failure_reason = (
                    f"Required Blender Python module(s) unavailable: {', '.join(missing_modules)}"
                )

        status = "FAIL" if failure_reason is not None else "PASS"

        return BlenderDependencyPreflight(
            status=status,
            executable=exe_str,
            blender_version=blender_version,
            python_version=python_version,
            python_executable=python_executable,
            python_prefix=python_prefix,
            sys_path=sys_path,
            configured_python_paths=configured_paths_list,
            modules=modules_info,
            exit_code=res.exit_code,
            reason=failure_reason,
            stdout_excerpt=stdout_excerpt if status == "FAIL" else None,
            stderr_excerpt=stderr_excerpt if status == "FAIL" else None,
        )
