"""Pre-spend production readiness gate domain model and reporting.

Defines readiness checks, context, probe protocols, and report builder.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Protocol

from gamefactory.core.domain.models import utc_now_iso
from gamefactory.core.domain.paid_request import PaidRequestSnapshot

READINESS_SCHEMA: str = "production-readiness-0.6.0"


@dataclass
class ReadinessCheck:
    """A single diagnostic preflight check result."""

    name: str
    category: str
    status: str  # "PASS" or "FAIL"
    critical: bool
    detail: str
    observed: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "status": self.status,
            "critical": self.critical,
            "detail": self.detail,
            "observed": dict(self.observed),
        }


@dataclass
class ReadinessContext:
    """Execution context and configuration provided to readiness probes."""

    root: Path
    asset_dir: Path | str
    snapshot_content: dict[str, Any]
    snapshot_sha256: str
    provider: Any
    blender_path: str | None = None
    godot_path: str | None = None
    specification: dict[str, Any] | Any = field(default_factory=dict)
    profile: Any = None
    snapshot: PaidRequestSnapshot | None = None

    def __post_init__(self) -> None:
        if self.snapshot is None and self.snapshot_content and self.snapshot_sha256:
            try:
                self.snapshot = PaidRequestSnapshot(self.snapshot_content, self.snapshot_sha256)
            except Exception:
                pass


class ReadinessProbes(Protocol):
    """Protocol for evaluating pre-spend production readiness probes."""

    def evaluate(self, context: ReadinessContext) -> list[ReadinessCheck]: ...

    def recheck_critical(self, context: ReadinessContext) -> list[ReadinessCheck]: ...


_PREREQUISITE_FAILED = "prerequisite failed"
REQUIRED_RUNTIME_REQUIREMENT_KEYS = frozenset(
    {
        "require_physics_body",
        "require_area",
        "require_ray_hit",
        "bounds_tolerance_floor_m",
        "bounds_tolerance_ratio",
    }
)


def _check(
    name: str,
    category: str,
    passed: bool,
    detail: str,
    observed: dict[str, Any] | None = None,
    *,
    critical: bool = True,
) -> ReadinessCheck:
    return ReadinessCheck(
        name=name,
        category=category,
        status="PASS" if passed else "FAIL",
        critical=critical,
        detail=detail,
        observed=dict(observed or {}),
    )


def _skipped(names: tuple[str, ...], category: str) -> list[ReadinessCheck]:
    return [_check(name, category, False, _PREREQUISITE_FAILED) for name in names]


def _executable_identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"path": str(path), "size_bytes": stat.st_size}


class DefaultReadinessProbes:
    """Real pre-spend readiness probes. No probe submits paid provider work.

    Collaborators are injectable so the gate can be tested without Blender or
    Godot installed. The Blender minimum version is the repository-wide
    ``MIN_SUPPORTED_BLENDER_VERSION`` enforced by the dependency preflight;
    Godot must be major version 4, the engine the runtime harness targets.
    """

    def __init__(
        self,
        blender_adapter: Any = None,
        godot_adapter: Any = None,
        dependency_preflight: Callable[..., Any] | None = None,
        min_free_bytes: int = 256 * 1024 * 1024,
        blender_python_paths: Sequence[Path | str] | None = None,
        runner: Any = None,
    ) -> None:
        self.blender_adapter = blender_adapter
        self.godot_adapter = godot_adapter
        self.dependency_preflight = dependency_preflight
        self.min_free_bytes = min_free_bytes
        self.blender_python_paths = blender_python_paths
        self.runner = runner

    # -- collaborators -------------------------------------------------
    def _runner(self) -> Any:
        if self.runner is None:
            from gamefactory.core.execution.process_runner import ProcessRunner

            self.runner = ProcessRunner(sanitize_output=True)
        return self.runner

    def _blender(self) -> Any:
        if self.blender_adapter is None:
            from gamefactory.adapters.dcc.blender import BlenderAdapter

            self.blender_adapter = BlenderAdapter(self._runner())
        return self.blender_adapter

    def _godot(self) -> Any:
        if self.godot_adapter is None:
            from gamefactory.adapters.engines.godot import GodotAdapter

            self.godot_adapter = GodotAdapter(self._runner())
        return self.godot_adapter

    def _preflight(self, executable: str) -> Any:
        from gamefactory.adapters.dcc.blender_environment import (
            BLENDER_PYTHONPATH_ENV,
            parse_blender_python_paths,
            run_blender_dependency_preflight,
        )

        if self.blender_python_paths is None:
            paths = parse_blender_python_paths(os.environ.get(BLENDER_PYTHONPATH_ENV))
        else:
            paths = parse_blender_python_paths(self.blender_python_paths)
        preflight = self.dependency_preflight or run_blender_dependency_preflight
        return preflight(blender_executable=executable, runner=self._runner(), python_paths=paths)

    # -- individual probe groups ----------------------------------------
    def provider_checks(self, context: ReadinessContext) -> list[ReadinessCheck]:
        provider = context.provider
        name = getattr(provider, "name", "unknown")
        checker = getattr(provider, "check_paid_request", None)
        if not callable(checker):
            return [
                _check(
                    "provider_adapter_available",
                    "provider",
                    False,
                    "provider does not implement the paid request adapter contract",
                    {"provider": name},
                ),
                *_skipped(("provider_credential_configured",), "provider"),
            ]
        try:
            checker(context.snapshot_content)
            adapter = _check(
                "provider_adapter_available",
                "provider",
                True,
                "current adapter can execute the approved snapshot",
                {"provider": name},
            )
        except Exception as exc:
            adapter = _check(
                "provider_adapter_available",
                "provider",
                False,
                f"snapshot not executable by current adapter: {type(exc).__name__}: {exc}",
                {"provider": name},
            )
        try:
            configured = bool(provider.is_configured())
            detail = (
                "provider credential configured" if configured else "provider credential missing"
            )
        except Exception as exc:
            configured = False
            detail = f"provider configuration probe failed: {type(exc).__name__}"
        return [
            adapter,
            _check(
                "provider_credential_configured", "provider", configured, detail, {"provider": name}
            ),
        ]

    def _executable_checks(
        self, tool: str, configured: str | None, *, require_exec_bit: bool
    ) -> tuple[list[ReadinessCheck], Path | None]:
        category = tool
        if not configured:
            return (
                [
                    _check(
                        f"{tool}_configured",
                        category,
                        False,
                        f"{tool} executable is not configured",
                    ),
                    *_skipped((f"{tool}_executable",), category),
                ],
                None,
            )
        path = Path(configured).expanduser()
        checks = [_check(f"{tool}_configured", category, True, "configured", {"path": str(path)})]
        if not path.exists():
            checks.append(
                _check(
                    f"{tool}_executable",
                    category,
                    False,
                    "configured path does not exist",
                    {"path": str(path)},
                )
            )
            return checks, None
        if not path.is_file():
            checks.append(
                _check(
                    f"{tool}_executable",
                    category,
                    False,
                    "configured path is not a regular file",
                    {"path": str(path)},
                )
            )
            return checks, None
        if require_exec_bit and not os.access(path, os.X_OK):
            checks.append(
                _check(
                    f"{tool}_executable",
                    category,
                    False,
                    "configured path is not executable",
                    {"path": str(path)},
                )
            )
            return checks, None
        checks.append(
            _check(
                f"{tool}_executable",
                category,
                True,
                "regular executable file",
                _executable_identity(path.resolve()),
            )
        )
        return checks, path.resolve()

    def blender_checks(self, blender_path: str | None) -> list[ReadinessCheck]:
        from gamefactory.adapters.dcc.blender_environment import (
            MIN_SUPPORTED_BLENDER_VERSION,
            parse_blender_version,
        )

        checks, executable = self._executable_checks(
            "blender", blender_path, require_exec_bit=False
        )
        later = (
            "blender_launch_version",
            "blender_version_supported",
            "blender_python_dependencies",
        )
        if executable is None:
            return [*checks, *_skipped(later, "blender")]
        detection = self._blender().detect_tool(custom_path=str(executable))
        launched = bool(detection.available and detection.version)
        checks.append(
            _check(
                "blender_launch_version",
                "blender",
                launched,
                detection.version or str(detection.details.get("reason", "launch failed")),
                {"version": detection.version},
            )
        )
        if not launched:
            return [*checks, *_skipped(later[1:], "blender")]
        parsed = parse_blender_version(detection.version)
        supported = parsed is not None and parsed >= MIN_SUPPORTED_BLENDER_VERSION
        minimum = ".".join(str(part) for part in MIN_SUPPORTED_BLENDER_VERSION)
        checks.append(
            _check(
                "blender_version_supported",
                "blender",
                supported,
                f"requires >= {minimum}",
                {"version": detection.version, "minimum": minimum},
            )
        )
        if not supported:
            return [*checks, *_skipped(later[2:], "blender")]
        try:
            preflight = self._preflight(str(executable))
            passed = getattr(preflight, "status", "FAIL") == "PASS"
            detail = (
                "dependency preflight PASS"
                if passed
                else str(getattr(preflight, "reason", None) or "dependency preflight failed")
            )
            observed = {
                "reason_code": getattr(preflight, "reason_code", None),
                "python_version": getattr(preflight, "python_version", None),
            }
        except Exception as exc:
            passed, detail, observed = False, f"preflight error: {type(exc).__name__}", {}
        checks.append(_check("blender_python_dependencies", "blender", passed, detail, observed))
        return checks

    def godot_checks(self, godot_path: str | None) -> list[ReadinessCheck]:
        checks, executable = self._executable_checks("godot", godot_path, require_exec_bit=True)
        later = ("godot_launch_version", "godot_version_supported")
        if executable is None:
            return [*checks, *_skipped(later, "godot")]
        detection = self._godot().detect_engine(custom_path=str(executable))
        launched = bool(detection.available and detection.version)
        checks.append(
            _check(
                "godot_launch_version",
                "godot",
                launched,
                detection.version or str(detection.details.get("reason", "launch failed")),
                {"version": detection.version},
            )
        )
        if not launched:
            return [*checks, *_skipped(later[1:], "godot")]
        major = str(detection.version).strip().split(".", 1)[0]
        checks.append(
            _check(
                "godot_version_supported",
                "godot",
                major == "4",
                "requires Godot 4.x",
                {"version": detection.version},
            )
        )
        return checks

    def workspace_checks(self, root: Path, asset_dir: Path | str) -> list[ReadinessCheck]:
        from gamefactory.core.execution.path_guard import assert_managed_directory

        checks: list[ReadinessCheck] = []
        directory = Path(asset_dir)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / f".readiness-{uuid.uuid4().hex}.tmp"
            with probe.open("x", encoding="utf-8") as handle:
                handle.write("ok")
            probe.unlink()
            checks.append(
                _check("workspace_writable", "workspace", True, "asset directory writable")
            )
        except OSError as exc:
            checks.append(
                _check(
                    "workspace_writable",
                    "workspace",
                    False,
                    f"asset directory not writable: {type(exc).__name__}",
                )
            )
        try:
            scratch = assert_managed_directory(root, ".gamefactory/scratch")
            scratch.mkdir(parents=True, exist_ok=True)
            checks.append(
                _check(
                    "scratch_creatable", "workspace", True, "managed scratch directory available"
                )
            )
        except Exception as exc:
            checks.append(
                _check(
                    "scratch_creatable",
                    "workspace",
                    False,
                    f"scratch unavailable: {type(exc).__name__}",
                )
            )
        try:
            free = shutil.disk_usage(root).free
            checks.append(
                _check(
                    "free_space",
                    "workspace",
                    free >= self.min_free_bytes,
                    f"requires at least {self.min_free_bytes} free bytes",
                    {"free_bytes": free, "minimum_bytes": self.min_free_bytes},
                )
            )
        except OSError as exc:
            checks.append(
                _check(
                    "free_space",
                    "workspace",
                    False,
                    f"disk usage unavailable: {type(exc).__name__}",
                )
            )
        return checks

    def profile_checks(self, specification: Any) -> list[ReadinessCheck]:
        from gamefactory.core.domain.asset_contracts import parse_asset_specification
        from gamefactory.core.domain.asset_profiles import builtin_registry
        from gamefactory.core.domain.camera_framing import PLACED_VIEWS

        names = (
            "profile_supported",
            "profile_review_views_implemented",
            "profile_runtime_validations_implemented",
        )
        try:
            profile = parse_asset_specification(specification).bound_profile()
        except Exception as exc:
            return [
                _check(
                    names[0], "profile", False, f"profile binding invalid: {type(exc).__name__}"
                ),
                *_skipped(names[1:], "profile"),
            ]
        rows = builtin_registry().availability()
        available = any(
            row.get("profile_id") == profile.profile_id and row.get("status") == "AVAILABLE"
            for row in rows
        )
        views = list(profile.review_views)
        views_ok = bool(views) and all(view in PLACED_VIEWS for view in views)
        try:
            requirements = profile.runtime_requirements()
            runtime_ok = isinstance(
                requirements, dict
            ) and REQUIRED_RUNTIME_REQUIREMENT_KEYS <= set(requirements)
        except Exception:
            runtime_ok = False
        return [
            _check(
                names[0], "profile", available, "profile available", {"profile": profile.qualified}
            ),
            _check(names[1], "profile", views_ok, "review views implemented", {"views": views}),
            _check(names[2], "profile", runtime_ok, "runtime validations implemented"),
        ]

    # -- protocol -------------------------------------------------------
    def evaluate(self, context: ReadinessContext) -> list[ReadinessCheck]:
        return [
            *self.provider_checks(context),
            *self.blender_checks(context.blender_path),
            *self.godot_checks(context.godot_path),
            *self.workspace_checks(Path(context.root), context.asset_dir),
            *self.profile_checks(context.specification),
        ]

    def recheck_critical(self, context: ReadinessContext) -> list[ReadinessCheck]:
        """Cheap critical recheck immediately before a paid submission; launches nothing."""
        blender, _ = self._executable_checks(
            "blender", context.blender_path, require_exec_bit=False
        )
        godot, _ = self._executable_checks("godot", context.godot_path, require_exec_bit=True)
        workspace = [
            check
            for check in self.workspace_checks(Path(context.root), context.asset_dir)
            if check.name == "workspace_writable"
        ]
        return [*self.provider_checks(context), *blender, *godot, *workspace]

    def capability_summary(
        self,
        root: Path,
        blender_path: str | None,
        godot_path: str | None,
        provider: Any = None,
    ) -> dict[str, dict[str, Any]]:
        """Doctor view built from the same probes; never submits provider work."""

        def summarize(checks: list[ReadinessCheck]) -> dict[str, Any]:
            failing = [check for check in checks if check.status != "PASS"]
            return {
                "status": "PASS" if not failing else "FAIL",
                "reason": failing[0].detail if failing else "all checks passed",
                "checks": [check.to_dict() for check in checks],
            }

        if provider is None:
            provider_summary: dict[str, Any] = {
                "status": "NOT_EVALUATED",
                "reason": "checked at workflow time against the approved request snapshot",
                "checks": [],
            }
        else:
            try:
                configured = bool(provider.is_configured())
            except Exception:
                configured = False
            provider_summary = {
                "status": "PASS" if configured else "FAIL",
                "reason": "credential configured" if configured else "credential missing",
                "checks": [],
            }
        workspace_root = Path(root) / ".gamefactory"
        return {
            "provider": provider_summary,
            "blender": summarize(self.blender_checks(blender_path)),
            "godot": summarize(self.godot_checks(godot_path)),
            "workspace": summarize(self.workspace_checks(Path(root), workspace_root / "scratch")),
        }


def build_readiness_report(
    context: ReadinessContext,
    checks: list[ReadinessCheck],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build canonical production readiness report dictionary."""
    binding = context.snapshot_content.get("binding", {})
    all_pass = all(c.status == "PASS" for c in checks)
    result = "PASS" if all_pass else "FAIL"

    checks_data: list[dict[str, Any]] = []
    for c in checks:
        if hasattr(c, "to_dict"):
            checks_data.append(c.to_dict())
        elif is_dataclass(c):
            checks_data.append(asdict(c))
        elif isinstance(c, dict):
            checks_data.append(dict(c))
        else:
            checks_data.append(
                {
                    "name": getattr(c, "name", ""),
                    "category": getattr(c, "category", ""),
                    "status": getattr(c, "status", "FAIL"),
                    "critical": getattr(c, "critical", True),
                    "detail": getattr(c, "detail", ""),
                    "observed": getattr(c, "observed", {}),
                }
            )

    tool_identities: dict[str, Any] = {
        "provider": getattr(context.provider, "name", "unknown"),
        "blender": str(context.blender_path) if context.blender_path else None,
        "godot": str(context.godot_path) if context.godot_path else None,
    }

    capabilities: list[str] = [c["name"] for c in checks_data if c["status"] == "PASS"]

    environment: dict[str, Any] = {
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "python_version": sys.version.split()[0],
    }

    return {
        "schema": READINESS_SCHEMA,
        "asset_id": binding.get("asset_id", ""),
        "revision_number": binding.get("revision_number", 1),
        "profile_id": binding.get("profile_id"),
        "profile_version": binding.get("profile_version"),
        "paid_request_snapshot_sha256": context.snapshot_sha256,
        "result": result,
        "checks": checks_data,
        "tool_identities": tool_identities,
        "capabilities": capabilities,
        "environment": environment,
        "generated_at": generated_at or utc_now_iso(),
    }
