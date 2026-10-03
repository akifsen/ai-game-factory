#!/usr/bin/env python3
"""Optional local V0.8 installed shard 4 runtime measurement harness (preliminary).

This runner records per-node pytest phase timings and installed-package provenance.
It does not perform wheel build/install, full V15A acceptance, or CI dispatch.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import io
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import types
import zipfile
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pytest

PROFILE_SHARD = 4

ALLOWED_EXPECTED_VERSIONS: frozenset[str] = frozenset({"0.8.0rc8", "0.8.0rc9"})

REPORT_SCHEMA = "v08-installed-shard-runtime-profile/v1-preliminary"

METRICS_NOT_YET_COLLECTED: tuple[str, ...] = (
    "subprocess_count",
    "subprocess_wall_seconds",
    "godot_invocation_seconds",
    "blender_invocation_seconds",
    "process_runner_seconds",
    "workspace_copy_seconds",
    "content_hash_seconds",
    "cold_verifier_invocation_count",
    "immutable_fixture_construction_seconds",
    "installed_wheel_sha256",
    "builder_consumer_provenance_diff",
)

METRICS_FILLED_BY_COST_OBSERVATION: frozenset[str] = frozenset(
    {
        "subprocess_count",
        "subprocess_wall_seconds",
        "godot_invocation_seconds",
        "blender_invocation_seconds",
        "process_runner_seconds",
        "cold_verifier_invocation_count",
    }
)

METRICS_FILLED_BY_WHEEL_OPTION: frozenset[str] = frozenset(
    {
        "installed_wheel_sha256",
        "builder_consumer_provenance_diff",
    }
)

NAMED_ENTRYPOINT_METRIC_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "workspace_copy_seconds": (
        "gamefactory.adapters.assets.v08_candidate_evidence._copy_tree_bounded",
        "shutil.copytree",
    ),
    "content_hash_seconds": (
        "gamefactory.adapters.engines.godot_staging.sha256_file",
        "gamefactory.adapters.assets.v08_candidate_evidence.fingerprint_cold_bundle_payload",
    ),
    "immutable_fixture_construction_seconds": (
        "tests.unit.v08_candidate_c2b_readiness_fixtures.run_completed_managed_evidence",
    ),
}

METRICS_FILLED_BY_NAMED_ENTRYPOINT: frozenset[str] = frozenset(
    NAMED_ENTRYPOINT_METRIC_REQUIREMENTS.keys()
)

POPEN_COUNT_CATEGORIES: tuple[str, ...] = (
    "godot",
    "blender",
    "python_cold_verifier",
    "other",
)

CATEGORY_DURATION_BUCKETS: tuple[str, ...] = (
    *POPEN_COUNT_CATEGORIES,
    "process_runner",
)

COST_OBSERVE_ENV = "GAMEFACTORY_V08_PROFILE_OBSERVE_COSTS"
COST_SIDECAR_ENV = "GAMEFACTORY_V08_PROFILE_COSTS_PATH"

NAMED_COST_ENTRYPOINTS: tuple[str, ...] = (
    "tests.unit.v08_candidate_c2b_readiness_fixtures.run_completed_managed_evidence",
    "gamefactory.adapters.assets.v08_candidate_evidence._copy_tree_bounded",
    "shutil.copytree",
    "shutil.copy2",
    "gamefactory.adapters.engines.godot_staging.sha256_file",
    "gamefactory.adapters.assets.v08_candidate_evidence.fingerprint_cold_bundle_payload",
    "gamefactory.adapters.assets.v08_candidate_evidence.trusted_cold_verify_candidate_bundle",
)

COLD_VERIFIER_SCRIPT_BASENAMES: frozenset[str] = frozenset(
    {
        "verify_candidate_bundle.py",
    }
)

PROVENANCE_IDENTITY_KEYS: tuple[str, ...] = ("module_path", "version", "metadata_version")

PhaseWhen = Literal["setup", "call", "teardown"]


class ProfileRuntimeError(RuntimeError):
    """Raised when profile guard or provenance checks fail."""


@dataclass
class PhaseOutcome:
    when: PhaseWhen
    outcome: str
    duration_seconds: float
    longrepr: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "when": self.when,
            "outcome": self.outcome,
            "duration_seconds": self.duration_seconds,
            "longrepr": self.longrepr,
        }


@dataclass
class NodeTimingRecord:
    nodeid: str
    phases: list[PhaseOutcome] = field(default_factory=list)

    def total_call_seconds(self) -> float:
        total = 0.0
        for phase in self.phases:
            if phase.when == "call":
                total += phase.duration_seconds
        return total

    def to_json(self) -> dict[str, Any]:
        return {
            "nodeid": self.nodeid,
            "phases": [p.to_json() for p in self.phases],
        }


@dataclass
class TimedOperationRecord:
    spawn_label: str
    category: str
    duration_seconds: float
    outcome: str
    nodeid: str | None = None
    phase: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "spawn_label": self.spawn_label,
            "category": self.category,
            "duration_seconds": self.duration_seconds,
            "outcome": self.outcome,
            "nodeid": self.nodeid,
            "phase": self.phase,
        }


@dataclass
class NamedEntrypointInvocationRecord:
    qualified_name: str
    duration_seconds: float
    outcome: str
    nodeid: str | None = None
    phase: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "qualified_name": self.qualified_name,
            "duration_seconds": self.duration_seconds,
            "outcome": self.outcome,
            "nodeid": self.nodeid,
            "phase": self.phase,
        }


@dataclass
class CostObservationSession:
    """Child-process observation: audit Popen counts; time subprocess.run / ProcessRunner.run only."""

    installed_monotonic: float = field(default_factory=time.monotonic)
    deactivated: bool = False
    audit_hook_active: bool = False
    audit_hook_removable: bool = False
    _context_lock: threading.Lock = field(default_factory=threading.Lock)
    _current_nodeid: str | None = None
    _current_phase: str | None = None
    popen_counts_by_category: dict[str, int] = field(
        default_factory=lambda: {
            "godot": 0,
            "blender": 0,
            "python_cold_verifier": 0,
            "other": 0,
        }
    )
    timed_operations: list[TimedOperationRecord] = field(default_factory=list)
    named_entrypoint_invocations: list[NamedEntrypointInvocationRecord] = field(
        default_factory=list
    )
    unavailable_named_entrypoints: list[str] = field(default_factory=list)
    wrapper_overhead_seconds: float = 0.0
    _wrapper_overhead_measured: bool = False
    audit_hook_invocations: int = 0
    _orig_subprocess_run: Callable[..., subprocess.CompletedProcess[Any]] | None = None
    _orig_process_runner_run: Callable[..., Any] | None = None
    _audit_hook: Callable[..., Any] | None = None
    _named_wrapper_restores: list[tuple[Any, str, Callable[..., Any]]] = field(default_factory=list)

    def set_context(self, nodeid: str | None, phase: str | None) -> None:
        with self._context_lock:
            self._current_nodeid = nodeid
            self._current_phase = phase

    def _context_snapshot(self) -> tuple[str | None, str | None]:
        with self._context_lock:
            return self._current_nodeid, self._current_phase

    def _record_wrapper_overhead(self, seconds: float) -> None:
        self._wrapper_overhead_measured = True
        if seconds > 0:
            self.wrapper_overhead_seconds += seconds


def _strip_outer_quote_pairs(text: str) -> str:
    stripped = text.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}:
        return stripped[1:-1]
    return stripped


def _basename_lower(value: object) -> str:
    return Path(_strip_outer_quote_pairs(str(value))).name.lower()


def _basename_contains_engine(token: object, engine: str) -> bool:
    return engine in _basename_lower(token)


def _argv_has_cold_verifier_script(tokens: list[Any]) -> bool:
    for part in tokens:
        if _basename_lower(part) in COLD_VERIFIER_SCRIPT_BASENAMES:
            return True
    return False


def _split_windows_commandline_tokens(commandline: str) -> list[str]:
    stripped = commandline.strip()
    if not stripped:
        return []
    return shlex.split(stripped, posix=False)


def argv_from_subprocess_popen_audit(audit_args: tuple[Any, ...]) -> list[Any]:
    """Normalize subprocess.Popen audit argv without logging raw command lines."""
    if not audit_args:
        return []
    if len(audit_args) >= 2 and isinstance(audit_args[1], str):
        return _split_windows_commandline_tokens(audit_args[1])
    if len(audit_args) >= 2 and isinstance(audit_args[1], (list, tuple)):
        argv_tail = list(audit_args[1])
        executable = audit_args[0]
        if executable is None:
            return argv_tail
        if not argv_tail:
            return [executable]
        if _basename_lower(argv_tail[0]) == _basename_lower(executable):
            return argv_tail
        return [executable, *argv_tail]
    if audit_args[0] is not None:
        return [audit_args[0]]
    return []


def classify_subprocess_category(args: list[Any]) -> str:
    if not args:
        return "other"
    tokens = [part for part in args if part is not None and str(part) != ""]
    if not tokens:
        return "other"
    if _argv_has_cold_verifier_script(tokens):
        return "python_cold_verifier"
    if _basename_contains_engine(tokens[0], "godot"):
        return "godot"
    if _basename_contains_engine(tokens[0], "blender"):
        return "blender"
    return "other"


def spawn_label_for_subprocess_run(args: list[Any]) -> str:
    category = classify_subprocess_category(args)
    if category == "python_cold_verifier":
        return "trusted_cold_verify_candidate_bundle_spawn"
    return f"subprocess_run:{category}"


def build_named_entrypoint_coverage(
    session: CostObservationSession,
) -> dict[str, Any]:
    totals: dict[str, dict[str, float | int]] = {}
    for record in session.named_entrypoint_invocations:
        bucket = totals.setdefault(
            record.qualified_name,
            {"invocation_count": 0, "duration_seconds": 0.0},
        )
        bucket["invocation_count"] = int(bucket["invocation_count"]) + 1
        bucket["duration_seconds"] = float(bucket["duration_seconds"]) + record.duration_seconds
    unavailable = frozenset(session.unavailable_named_entrypoints)
    rows: dict[str, Any] = {}
    for entry in NAMED_COST_ENTRYPOINTS:
        if entry in totals:
            stats = totals[entry]
            rows[entry] = {
                "coverage": "instrumented_invocation",
                "invocation_count": stats["invocation_count"],
                "duration_seconds": stats["duration_seconds"],
                "lower_bound_only": False,
            }
        elif entry in unavailable:
            rows[entry] = {
                "coverage": "entrypoint_unavailable",
                "lower_bound_only": True,
            }
        else:
            rows[entry] = {
                "coverage": "not_observed_instrumentation_lower_bound",
                "lower_bound_only": True,
            }
    return rows


def _named_invocation_outcome(exc: BaseException | None) -> str:
    if exc is None:
        return "success"
    return "error"


def _wrap_named_entrypoint(
    session: CostObservationSession,
    *,
    owner: Any,
    attr: str,
    qualified_name: str,
) -> None:
    original = getattr(owner, attr)

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if session.deactivated:
            return original(*args, **kwargs)
        overhead_start = time.monotonic()
        nodeid, phase = session._context_snapshot()
        session._record_wrapper_overhead(time.monotonic() - overhead_start)
        invoke_start = time.monotonic()
        exc: BaseException | None = None
        try:
            return original(*args, **kwargs)
        except BaseException as caught:
            exc = caught
            raise
        finally:
            duration = time.monotonic() - invoke_start
            session.named_entrypoint_invocations.append(
                NamedEntrypointInvocationRecord(
                    qualified_name=qualified_name,
                    duration_seconds=duration,
                    outcome=_named_invocation_outcome(exc),
                    nodeid=nodeid,
                    phase=phase,
                )
            )

    setattr(owner, attr, wrapper)
    session._named_wrapper_restores.append((owner, attr, original))


def _install_named_entrypoint_wrappers(session: CostObservationSession) -> None:
    _wrap_named_entrypoint(
        session,
        owner=shutil,
        attr="copytree",
        qualified_name=NAMED_COST_ENTRYPOINTS[2],
    )
    _wrap_named_entrypoint(
        session,
        owner=shutil,
        attr="copy2",
        qualified_name=NAMED_COST_ENTRYPOINTS[3],
    )
    optional_targets: tuple[tuple[str, str, str], ...] = (
        (
            "gamefactory.adapters.assets.v08_candidate_evidence",
            "_copy_tree_bounded",
            NAMED_COST_ENTRYPOINTS[1],
        ),
        (
            "gamefactory.adapters.engines.godot_staging",
            "sha256_file",
            NAMED_COST_ENTRYPOINTS[4],
        ),
        (
            "gamefactory.adapters.assets.v08_candidate_evidence",
            "fingerprint_cold_bundle_payload",
            NAMED_COST_ENTRYPOINTS[5],
        ),
        (
            "gamefactory.adapters.assets.v08_candidate_evidence",
            "trusted_cold_verify_candidate_bundle",
            NAMED_COST_ENTRYPOINTS[6],
        ),
        (
            "tests.unit.v08_candidate_c2b_readiness_fixtures",
            "run_completed_managed_evidence",
            NAMED_COST_ENTRYPOINTS[0],
        ),
    )
    for module_name, attr, qualified in optional_targets:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            session.unavailable_named_entrypoints.append(qualified)
            continue
        if not hasattr(module, attr):
            session.unavailable_named_entrypoints.append(qualified)
            continue
        _wrap_named_entrypoint(session, owner=module, attr=attr, qualified_name=qualified)


def _process_runner_outcome(result: Any) -> str:
    timed_out = bool(getattr(result, "timed_out", False))
    if timed_out:
        return "timeout"
    exit_code = getattr(result, "exit_code", 0)
    if exit_code != 0:
        return "nonzero_exit"
    return "success"


def _operation_outcome_from_subprocess(
    *,
    exc: BaseException | None,
    completed: subprocess.CompletedProcess[Any] | None,
) -> str:
    if exc is not None:
        if isinstance(exc, subprocess.TimeoutExpired):
            return "timeout"
        if isinstance(exc, subprocess.CalledProcessError):
            return "nonzero_exit"
        return "error"
    if completed is not None and completed.returncode != 0:
        return "nonzero_exit"
    return "success"


def _extract_subprocess_argv(*popen_args: Any, **kwargs: Any) -> list[Any]:
    if popen_args:
        first = popen_args[0]
        if isinstance(first, (list, tuple)):
            return list(first)
    if "args" in kwargs:
        value = kwargs["args"]
        if isinstance(value, (list, tuple)):
            return list(value)
    return list(popen_args)


def install_cost_observation() -> CostObservationSession:
    session = CostObservationSession()
    session._orig_subprocess_run = subprocess.run

    def audit_hook(event: str, args: tuple[Any, ...]) -> None:
        session.audit_hook_invocations += 1
        if session.deactivated:
            return
        if event != "subprocess.Popen":
            return
        popen_args = argv_from_subprocess_popen_audit(args)
        if not popen_args:
            return
        category = classify_subprocess_category(popen_args)
        session.popen_counts_by_category[category] = (
            session.popen_counts_by_category.get(category, 0) + 1
        )

    session._audit_hook = audit_hook
    sys.addaudithook(audit_hook)
    session.audit_hook_active = True
    session.audit_hook_removable = False

    def wrapped_subprocess_run(*popen_args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if session.deactivated:
            assert session._orig_subprocess_run is not None
            return session._orig_subprocess_run(*popen_args, **kwargs)
        call_args = _extract_subprocess_argv(*popen_args, **kwargs)
        label = spawn_label_for_subprocess_run(call_args)
        category = classify_subprocess_category(call_args)
        nodeid, phase = session._context_snapshot()
        wrap_start = time.monotonic()
        completed: subprocess.CompletedProcess[Any] | None = None
        exc: BaseException | None = None
        try:
            assert session._orig_subprocess_run is not None
            completed = session._orig_subprocess_run(*popen_args, **kwargs)
        except BaseException as caught:
            exc = caught
            raise
        finally:
            duration = time.monotonic() - wrap_start
            session.timed_operations.append(
                TimedOperationRecord(
                    spawn_label=label,
                    category=category,
                    duration_seconds=duration,
                    outcome=_operation_outcome_from_subprocess(exc=exc, completed=completed),
                    nodeid=nodeid,
                    phase=phase,
                )
            )

        return completed

    subprocess.run = wrapped_subprocess_run  # type: ignore[assignment]

    try:
        from gamefactory.core.execution.process_runner import ProcessRunner
    except ImportError:
        ProcessRunner = None  # type: ignore[misc, assignment]

    if ProcessRunner is not None:
        session._orig_process_runner_run = ProcessRunner.run

        def wrapped_process_runner_run(self: Any, request: Any) -> Any:
            if session.deactivated:
                assert session._orig_process_runner_run is not None
                return session._orig_process_runner_run(self, request)
            call_args = list(getattr(request, "args", []) or [])
            label = f"process_runner_run:{classify_subprocess_category(call_args)}"
            category = classify_subprocess_category(call_args)
            nodeid, phase = session._context_snapshot()
            wrap_start = time.monotonic()
            result: Any = None
            exc: BaseException | None = None
            try:
                assert session._orig_process_runner_run is not None
                result = session._orig_process_runner_run(self, request)
            except BaseException as caught:
                exc = caught
                raise
            finally:
                duration = time.monotonic() - wrap_start
                if exc is not None:
                    outcome = "error"
                else:
                    outcome = _process_runner_outcome(result)
                session.timed_operations.append(
                    TimedOperationRecord(
                        spawn_label=label,
                        category=category,
                        duration_seconds=duration,
                        outcome=outcome,
                        nodeid=nodeid,
                        phase=phase,
                    )
                )
            return result

        ProcessRunner.run = wrapped_process_runner_run  # type: ignore[method-assign]

    _install_named_entrypoint_wrappers(session)
    return session


def deactivate_cost_observation(session: CostObservationSession) -> None:
    session.deactivated = True
    for owner, attr, original in reversed(session._named_wrapper_restores):
        setattr(owner, attr, original)
    session._named_wrapper_restores.clear()
    if session._orig_subprocess_run is not None:
        subprocess.run = session._orig_subprocess_run
    if session._orig_process_runner_run is not None:
        from gamefactory.core.execution.process_runner import ProcessRunner

        ProcessRunner.run = session._orig_process_runner_run


def finalize_cost_observation(session: CostObservationSession) -> dict[str, Any]:
    totals = aggregate_cost_categories(session.timed_operations)
    return {
        "schema": "v08-installed-shard-runtime-costs/v1-preliminary",
        "popen_counts_by_category": dict(session.popen_counts_by_category),
        "popen_duration_attribution": "unattributed_direct_popen_only",
        "timed_operations": [row.to_json() for row in session.timed_operations],
        "timed_operations_overlap_note": (
            "nested_durations_may_overlap; never sum with pytest node phase wall times"
        ),
        "category_duration_seconds": totals,
        "named_entrypoint_invocations": [
            row.to_json() for row in session.named_entrypoint_invocations
        ],
        "named_entrypoint_coverage": build_named_entrypoint_coverage(session),
        "observer_context": {
            "audit_hook_active": session.audit_hook_active,
            "audit_hook_removable": session.audit_hook_removable,
            "audit_hook_deactivated_on_cleanup": session.deactivated,
            "audit_hook_invocations": session.audit_hook_invocations,
            "wrapper_overhead_seconds": (
                session.wrapper_overhead_seconds if session._wrapper_overhead_measured else None
            ),
            "wrapper_overhead_status": (
                "measured_bookkeeping_limited_coverage"
                if session._wrapper_overhead_measured
                else "unknown"
            ),
            "session_monotonic_seconds": time.monotonic() - session.installed_monotonic,
        },
    }


def aggregate_cost_categories(records: list[TimedOperationRecord]) -> dict[str, float]:
    totals = {
        "godot": 0.0,
        "blender": 0.0,
        "python_cold_verifier": 0.0,
        "process_runner": 0.0,
        "other": 0.0,
    }
    for record in records:
        bucket = record.category
        if record.spawn_label.startswith("process_runner_run"):
            bucket = "process_runner"
        totals[bucket] = totals.get(bucket, 0.0) + record.duration_seconds
    return totals


def write_cost_observation_sidecar(path: Path, payload: dict[str, Any]) -> None:
    _reject_existing_path(path, label="cost observation sidecar")
    _write_exclusive_json(path, payload, label="cost observation sidecar", indent=None)


class ShardCostObservationPlugin:
    """Pytest plugin: attribute subprocess timing to the active node phase."""

    def __init__(self, session: CostObservationSession) -> None:
        self._session = session

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_setup(self, item: Any) -> Any:
        nodeid = item.nodeid.replace("\\", "/")
        self._session.set_context(nodeid, "setup")
        try:
            yield
        finally:
            self._session.set_context(None, None)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item: Any) -> Any:
        nodeid = item.nodeid.replace("\\", "/")
        self._session.set_context(nodeid, "call")
        try:
            yield
        finally:
            self._session.set_context(None, None)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_teardown(self, item: Any) -> None:
        nodeid = item.nodeid.replace("\\", "/")
        self._session.set_context(nodeid, "teardown")
        try:
            yield
        finally:
            self._session.set_context(None, None)


@dataclass
class PytestShardResult:
    exit_code: int
    collect_actual: frozenset[str]
    child_probe_before: dict[str, Any] | None
    child_probe_after: dict[str, Any] | None


class ShardTimingPlugin:
    """Pytest plugin: record setup/call/teardown outcome and duration per node."""

    def __init__(self, output_path: Path) -> None:
        self._output_path = output_path
        self._records: dict[str, NodeTimingRecord] = {}
        self._session_start: float | None = None
        self._session_end: float | None = None

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item: Any, call: Any) -> Any:
        outcome = yield
        report = outcome.get_result()
        when: PhaseWhen = call.when
        nodeid = item.nodeid.replace("\\", "/")
        record = self._records.setdefault(nodeid, NodeTimingRecord(nodeid=nodeid))
        if report.failed:
            phase_outcome = "failed"
        elif report.skipped:
            phase_outcome = "skipped"
        elif report.passed:
            phase_outcome = "passed"
        else:
            phase_outcome = "unknown"
        longrepr: str | None = None
        if phase_outcome in {"failed", "skipped"} and report.longrepr is not None:
            longrepr = str(report.longrepr)
        duration = float(getattr(report, "duration", 0.0) or 0.0)
        record.phases.append(
            PhaseOutcome(
                when=when,
                outcome=phase_outcome,
                duration_seconds=duration,
                longrepr=longrepr,
            )
        )

    def pytest_sessionstart(self, session: Any) -> None:
        self._session_start = time.monotonic()

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        self._session_end = time.monotonic()
        payload = {
            "session_start_monotonic": self._session_start,
            "session_end_monotonic": self._session_end,
            "pytest_exitstatus": exitstatus,
            "nodes": [r.to_json() for r in sorted(self._records.values(), key=lambda r: r.nodeid)],
        }
        _write_exclusive_json(self._output_path, payload, label="pytest timing sidecar")


def _source_checkout_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _reject_existing_path(path: Path, *, label: str) -> None:
    if path.exists():
        raise ProfileRuntimeError(f"refusing to overwrite existing {label}: {path}")


def _write_exclusive_text(path: Path, text: str, *, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    except FileExistsError:
        raise ProfileRuntimeError(f"refusing to overwrite existing {label}: {path}") from None


def _write_exclusive_json(path: Path, payload: Any, *, label: str, indent: int | None = 2) -> None:
    if indent is None:
        text = json.dumps(payload, sort_keys=True) + "\n"
    else:
        text = json.dumps(payload, indent=indent, sort_keys=True) + "\n"
    _write_exclusive_text(path, text, label=label)


def _ensure_evidence_parent_dirs(*paths: Path) -> None:
    parents = {path.parent.resolve() for path in paths}
    for parent in sorted(parents):
        parent.mkdir(parents=True, exist_ok=True)


def _assert_disjoint_from_roots(path: Path, roots: tuple[Path, ...], *, label: str) -> None:
    resolved = path.resolve()
    for root in roots:
        anchor = root.resolve()
        if resolved == anchor or resolved.is_relative_to(anchor):
            raise ProfileRuntimeError(
                f"{label} must not live under frozen or checkout root: {resolved}"
            )


def guard_output_evidence_paths(
    *,
    test_root: Path,
    output_json: Path,
    junit_xml: Path,
    timing_path: Path,
    basetemp_parent: Path,
) -> None:
    checkout = _source_checkout_root()
    forbidden = (test_root.resolve(), checkout.resolve())
    for path, label in (
        (output_json, "output report"),
        (junit_xml, "junit xml"),
        (timing_path, "pytest timing sidecar"),
        (basetemp_parent, "basetemp parent"),
    ):
        _reject_existing_path(path, label=label)
        _assert_disjoint_from_roots(path, forbidden, label=label)

    resolved_outputs = {
        output_json.resolve(),
        junit_xml.resolve(),
        timing_path.resolve(),
    }
    if len(resolved_outputs) != 3:
        raise ProfileRuntimeError("output, junit, and timing paths must be distinct")

    if (
        output_json.parent.resolve() == test_root.resolve()
        or output_json.parent.resolve() == checkout.resolve()
    ):
        _assert_disjoint_from_roots(output_json.parent, forbidden, label="output directory")


def _subprocess_env(*, workspace: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["GAMEFACTORY_CI_WORKSPACE"] = str(workspace.resolve())
    return env


def _pytest_subprocess_env(*, workspace: Path, test_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["GAMEFACTORY_CI_WORKSPACE"] = str(workspace.resolve())
    env["GAMEFACTORY_CI_TEST_ROOT"] = str(test_root.resolve())
    return env


def load_frozen_candidate_ci(test_root: Path) -> types.ModuleType:
    script = (test_root / "scripts" / "verify_v08_candidate_ci.py").resolve()
    if not script.is_file():
        raise ProfileRuntimeError(f"frozen verify helper missing under test-root: {script}")
    checkout_script = (Path(__file__).resolve().parent / "verify_v08_candidate_ci.py").resolve()
    if script == checkout_script:
        from scripts import verify_v08_candidate_ci as module

        return module

    module_name = f"_frozen_verify_v08_candidate_ci_{abs(hash(script))}"
    spec = importlib.util.spec_from_file_location(module_name, script)
    if spec is None or spec.loader is None:
        raise ProfileRuntimeError(f"cannot load frozen verify helper: {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def installed_probe_source(expected_version: str, test_root: Path) -> str:
    """Probe source: site-packages install, workspace/test-root src rejection."""
    frozen_ci = load_frozen_candidate_ci(test_root)
    base = frozen_ci.installed_package_outside_checkout_probe_source(expected_version)
    extra = f"""
test_root = pathlib.Path({str(test_root.resolve())!r}).resolve()
src_root = test_root / "src"
assert not mod.is_relative_to(src_root), (mod, src_root)
checkout_src = pathlib.Path(__import__("os").environ.get("GAMEFACTORY_CI_TEST_ROOT", "")).resolve() / "src"
if str(checkout_src) != "." and checkout_src.is_dir():
    assert not mod.is_relative_to(checkout_src), (mod, checkout_src)
"""
    return base + extra


def exec_installed_probe_in_process(
    *,
    expected_version: str,
    test_root: Path,
    workspace: Path,
) -> dict[str, Any]:
    probe = installed_probe_source(expected_version, test_root)
    prior_test_root = os.environ.get("GAMEFACTORY_CI_TEST_ROOT")
    prior_workspace = os.environ.get("GAMEFACTORY_CI_WORKSPACE")
    os.environ["GAMEFACTORY_CI_TEST_ROOT"] = str(test_root.resolve())
    os.environ["GAMEFACTORY_CI_WORKSPACE"] = str(workspace.resolve())
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            exec(compile(probe, "<installed_probe>", "exec"), {"__name__": "__main__"})
    finally:
        if prior_test_root is None:
            os.environ.pop("GAMEFACTORY_CI_TEST_ROOT", None)
        else:
            os.environ["GAMEFACTORY_CI_TEST_ROOT"] = prior_test_root
        if prior_workspace is None:
            os.environ.pop("GAMEFACTORY_CI_WORKSPACE", None)
        else:
            os.environ["GAMEFACTORY_CI_WORKSPACE"] = prior_workspace
    raw = buffer.getvalue().strip()
    if not raw:
        raise ProfileRuntimeError("installed probe produced no stdout")
    return json.loads(raw)


def run_installed_probe(
    python: Path,
    *,
    workspace: Path,
    test_root: Path,
    expected_version: str,
    cwd: Path,
) -> dict[str, Any]:
    probe = installed_probe_source(expected_version, test_root)
    env = _subprocess_env(workspace=workspace)
    env["GAMEFACTORY_CI_TEST_ROOT"] = str(test_root.resolve())
    proc = subprocess.run(
        [str(python), "-I", "-c", probe],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise ProfileRuntimeError(
            "installed package provenance check failed:\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout.strip())


def provenance_snapshots_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(key) == right.get(key) for key in PROVENANCE_IDENTITY_KEYS)


def _provenance_mismatch_detail(
    label: str,
    left: dict[str, Any],
    right: dict[str, Any],
) -> str:
    diffs = {
        key: {"before": left.get(key), "after": right.get(key)}
        for key in PROVENANCE_IDENTITY_KEYS
        if left.get(key) != right.get(key)
    }
    return f"provenance_mismatch:{label}:{json.dumps(diffs, sort_keys=True)}"


def provenance_identity_guard_failures(
    *,
    provenance_before: dict[str, Any] | None,
    provenance_after: dict[str, Any] | None,
    child_probe_before: dict[str, Any] | None,
    child_probe_after: dict[str, Any] | None,
) -> list[str]:
    failures: list[str] = []
    if provenance_before is None:
        failures.append("provenance_before_missing")
        return failures
    if provenance_after is None:
        failures.append("provenance_after_not_executed")
    if child_probe_before is None:
        failures.append("child_probe_before_missing")
    if child_probe_after is None:
        failures.append("child_probe_after_missing")

    pairs: tuple[tuple[str, dict[str, Any] | None, dict[str, Any] | None], ...] = (
        ("parent_before_after", provenance_before, provenance_after),
        ("child_before_after", child_probe_before, child_probe_after),
        ("parent_child_before", provenance_before, child_probe_before),
        ("parent_child_after", provenance_after, child_probe_after),
    )
    for label, left, right in pairs:
        if left is None or right is None:
            continue
        if not provenance_snapshots_equal(left, right):
            failures.append(_provenance_mismatch_detail(label, left, right))
    return failures


def shard4_modules(ci: types.ModuleType) -> tuple[str, ...]:
    modules = ci.SHARD_MODULES.get(PROFILE_SHARD)
    if not modules:
        raise ProfileRuntimeError(f"shard {PROFILE_SHARD} modules missing in frozen inventory")
    return tuple(modules)


def expected_shard4_nodes(ci: types.ModuleType, test_root: Path) -> frozenset[str]:
    canonical = ci.load_canonical_slow_nodes(test_root)
    modules = shard4_modules(ci)
    return ci.canonical_nodes_for_modules(canonical, modules)


def guard_collected_nodes(
    ci: types.ModuleType,
    collected: frozenset[str],
    expected: frozenset[str],
) -> None:
    ci.assert_runtime_slow_inventory_matches_canonical(
        collected,
        expected,
        label=f"runtime-nodes shard {PROFILE_SHARD}",
    )


def run_pytest_collect_shard(
    ci: types.ModuleType,
    python: Path,
    test_root: Path,
    *,
    collect_cwd: Path,
) -> frozenset[str]:
    modules = list(shard4_modules(ci))
    return ci.run_pytest_collect_nodeids(
        python,
        test_root,
        modules,
        cwd=collect_cwd,
        outside_checkout=True,
    )


def _absolute_testpaths(test_root: Path, modules: tuple[str, ...]) -> list[str]:
    return [str((test_root / rel).resolve()) for rel in modules]


def _pytest_child_script() -> str:
    return r"""
import importlib.util
import json
import sys
from pathlib import Path

profile_path = Path(sys.argv[1])
timing_path = Path(sys.argv[2])
test_root = Path(sys.argv[3])
expected_version = sys.argv[4]
child_probe_before_path = Path(sys.argv[5])
child_probe_after_path = Path(sys.argv[6])
runtime_collect_path = Path(sys.argv[7])
pytest_args = sys.argv[8:]

spec = importlib.util.spec_from_file_location("_profile_v08_runner", profile_path)
if spec is None or spec.loader is None:
    raise SystemExit("profile module load failed")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

workspace = Path(__import__("os").environ["GAMEFACTORY_CI_WORKSPACE"]).resolve()
probe_before = mod.exec_installed_probe_in_process(
    expected_version=expected_version,
    test_root=test_root,
    workspace=workspace,
)
mod._write_exclusive_json(
    child_probe_before_path,
    probe_before,
    label="child probe before pytest",
    indent=None,
)

ci = mod.load_frozen_candidate_ci(test_root)
expected_nodes = mod.expected_shard4_nodes(ci, test_root)
collected = mod.run_pytest_collect_shard(
    ci,
    Path(sys.executable),
    test_root,
    collect_cwd=Path.cwd(),
)
mod.guard_collected_nodes(ci, collected, expected_nodes)
mod._write_exclusive_json(
    runtime_collect_path,
    {"nodeids": sorted(collected)},
    label="runtime collect sidecar",
    indent=None,
)

plugin = mod.ShardTimingPlugin(timing_path)
plugins = [plugin]
cost_session = None
observe_costs = __import__("os").environ.get(mod.COST_OBSERVE_ENV) == "1"
if observe_costs:
    cost_session = mod.install_cost_observation()
    plugins.append(mod.ShardCostObservationPlugin(cost_session))
try:
    exit_code = int(__import__("pytest").main(pytest_args, plugins=plugins))
finally:
    if cost_session is not None:
        mod.deactivate_cost_observation(cost_session)
        costs_payload = mod.finalize_cost_observation(cost_session)
        costs_path = Path(__import__("os").environ[mod.COST_SIDECAR_ENV])
        mod.write_cost_observation_sidecar(costs_path, costs_payload)

probe_after = mod.exec_installed_probe_in_process(
    expected_version=expected_version,
    test_root=test_root,
    workspace=workspace,
)
mod._write_exclusive_json(
    child_probe_after_path,
    probe_after,
    label="child probe after pytest",
    indent=None,
)
raise SystemExit(exit_code)
"""


def resolve_consumer_site_packages(python: Path, *, cwd: Path) -> Path:
    proc = subprocess.run(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ProfileRuntimeError(
            f"consumer site-packages probe failed: stdout={proc.stdout} stderr={proc.stderr}"
        )
    return Path(proc.stdout.strip()).resolve()


def sha256_file_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


_WHEEL_DIST_INFO_SKIP_FILES: frozenset[str] = frozenset(
    {"RECORD", "direct_url.json", "direct_url", "INSTALLER", "REQUESTED"}
)


def _is_generated_gamefactory_package_relpath(rel: str) -> bool:
    parts = rel.split("/")
    if "__pycache__" in parts:
        return True
    return rel.endswith(".pyc")


def _optional_wheel_dist_metadata_members(wheel_path: Path) -> dict[str, str]:
    resolved = wheel_path.resolve()
    metadata: dict[str, str] = {}
    with zipfile.ZipFile(resolved, "r") as archive:
        for member in archive.namelist():
            if member.endswith("/") or ".dist-info/" not in member:
                continue
            if not member.split("/", 1)[0].startswith("gamefactory-"):
                continue
            basename = Path(member).name
            if basename in _WHEEL_DIST_INFO_SKIP_FILES:
                continue
            if basename not in {"METADATA", "WHEEL"}:
                continue
            metadata[member] = hashlib.sha256(archive.read(member)).hexdigest()
    return metadata


def gamefactory_package_files_manifest_from_wheel(wheel_path: Path) -> dict[str, str]:
    resolved = wheel_path.resolve()
    if not resolved.is_file():
        raise ProfileRuntimeError(f"wheel path missing: {resolved}")
    manifest: dict[str, str] = {}
    with zipfile.ZipFile(resolved, "r") as archive:
        for member in archive.namelist():
            if member.endswith("/") or not member.startswith("gamefactory/"):
                continue
            if _is_generated_gamefactory_package_relpath(member):
                continue
            manifest[member] = hashlib.sha256(archive.read(member)).hexdigest()
    if not manifest:
        raise ProfileRuntimeError(f"wheel contains no gamefactory package entries: {resolved}")
    return manifest


def _digest_site_package_file(site_packages: Path, rel: str) -> str:
    root = site_packages.resolve()
    path = (root / rel).resolve()
    if path != root and not path.is_relative_to(root):
        raise ProfileRuntimeError(f"site package path escapes consumer root: {rel}")
    return sha256_file_path(path)


def gamefactory_package_files_manifest_from_site_packages(site_packages: Path) -> dict[str, str]:
    root = site_packages.resolve()
    manifest: dict[str, str] = {}
    package_root = root / "gamefactory"
    if not package_root.is_dir():
        return manifest
    for path in package_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if _is_generated_gamefactory_package_relpath(rel):
            continue
        manifest[rel] = _digest_site_package_file(root, rel)
    return manifest


def compare_gamefactory_wheel_to_site_packages(
    wheel_path: Path,
    site_packages: Path,
) -> list[str]:
    failures: list[str] = []
    wheel_manifest = gamefactory_package_files_manifest_from_wheel(wheel_path)
    site_manifest = gamefactory_package_files_manifest_from_site_packages(site_packages)
    wheel_keys = set(wheel_manifest)
    site_keys = set(site_manifest)
    for extra in sorted(site_keys - wheel_keys):
        failures.append(f"consumer_extra_package_file:{extra}")
    for member, wheel_digest in sorted(wheel_manifest.items()):
        installed_digest = site_manifest.get(member)
        if installed_digest is None:
            failures.append(f"wheel_member_missing_on_consumer:{member}")
        elif installed_digest != wheel_digest:
            failures.append(f"wheel_member_byte_mismatch:{member}")
    optional_wheel_metadata = _optional_wheel_dist_metadata_members(wheel_path)
    if optional_wheel_metadata:
        root = site_packages.resolve()
        for member, wheel_digest in sorted(optional_wheel_metadata.items()):
            installed_path = (root / member).resolve()
            if not installed_path.is_file():
                failures.append(f"wheel_dist_metadata_missing_on_consumer:{member}")
                continue
            installed_digest = _digest_site_package_file(root, member)
            if installed_digest != wheel_digest:
                failures.append(f"wheel_dist_metadata_byte_mismatch:{member}")
    return failures


def verify_wheel_consumer_containment(
    wheel_path: Path,
    site_packages: Path,
    *,
    test_root: Path,
    workspace: Path,
) -> None:
    resolved_wheel = wheel_path.resolve()
    resolved_site = site_packages.resolve()
    if not resolved_wheel.is_file():
        raise ProfileRuntimeError(f"wheel path missing: {resolved_wheel}")
    checkout = _source_checkout_root().resolve()
    verification_root = (checkout / ".verification").resolve()
    forbidden = (test_root.resolve(), workspace.resolve(), checkout)
    for anchor in forbidden:
        if resolved_site == anchor or resolved_site.is_relative_to(anchor):
            raise ProfileRuntimeError(
                "wheel verification requires consumer site-packages outside "
                "checkout, test-root, and workspace"
            )
    wheel_under_verification = resolved_wheel == verification_root or resolved_wheel.is_relative_to(
        verification_root
    )
    if not wheel_under_verification:
        for anchor in forbidden:
            if resolved_wheel == anchor or resolved_wheel.is_relative_to(anchor):
                raise ProfileRuntimeError(
                    "wheel path must not live under checkout, test-root, or workspace roots"
                )


def run_pytest_shard(
    python: Path,
    test_root: Path,
    *,
    workspace: Path,
    basetemp: Path,
    junit_path: Path,
    collect_cwd: Path,
    profile_script: Path,
    timing_path: Path,
    expected_version: str,
    child_probe_before: Path,
    child_probe_after: Path,
    runtime_collect_path: Path,
    observe_costs: bool = False,
    costs_path: Path | None = None,
) -> PytestShardResult:
    modules = shard4_modules(load_frozen_candidate_ci(test_root))
    abs_paths = _absolute_testpaths(test_root, modules)
    sidecars: list[Path] = [
        timing_path,
        child_probe_before,
        child_probe_after,
        runtime_collect_path,
        junit_path,
    ]
    if observe_costs:
        if costs_path is None:
            raise ProfileRuntimeError("costs sidecar path required when observe_costs is enabled")
        sidecars.append(costs_path)
    for sidecar in sidecars:
        _reject_existing_path(sidecar, label="pytest evidence sidecar")
    _ensure_evidence_parent_dirs(*sidecars)
    cmd = [
        str(python),
        "-I",
        "-c",
        _pytest_child_script(),
        str(profile_script.resolve()),
        str(timing_path.resolve()),
        str(test_root.resolve()),
        expected_version,
        str(child_probe_before.resolve()),
        str(child_probe_after.resolve()),
        str(runtime_collect_path.resolve()),
        "-ra",
        "--rootdir",
        str(test_root.resolve()),
        "--import-mode",
        "importlib",
        "-o",
        f"pythonpath={test_root.resolve()}",
        "-p",
        "no:cacheprovider",
        f"--basetemp={basetemp.resolve()}",
        f"--junitxml={junit_path.resolve()}",
        *abs_paths,
    ]
    env = _pytest_subprocess_env(workspace=workspace, test_root=test_root)
    if observe_costs:
        env[COST_OBSERVE_ENV] = "1"
        env[COST_SIDECAR_ENV] = str(costs_path.resolve())  # type: ignore[union-attr]
    proc = subprocess.run(
        cmd,
        cwd=collect_cwd,
        env=env,
        check=False,
    )
    exit_code = int(proc.returncode)
    collect_actual: frozenset[str] = frozenset()
    if runtime_collect_path.is_file():
        payload = json.loads(runtime_collect_path.read_text(encoding="utf-8"))
        collect_actual = frozenset(payload.get("nodeids", []))
    probe_before: dict[str, Any] | None = None
    probe_after: dict[str, Any] | None = None
    if child_probe_before.is_file():
        probe_before = json.loads(child_probe_before.read_text(encoding="utf-8"))
    if child_probe_after.is_file():
        probe_after = json.loads(child_probe_after.read_text(encoding="utf-8"))
    return PytestShardResult(
        exit_code=exit_code,
        collect_actual=collect_actual,
        child_probe_before=probe_before,
        child_probe_after=probe_after,
    )


def load_timing_payload(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ProfileRuntimeError(f"timing payload missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileRuntimeError(f"timing payload malformed: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProfileRuntimeError(f"timing payload must be a JSON object: {path}")
    return payload


def validate_timing_payload(
    payload: dict[str, Any],
    *,
    expected_nodes: frozenset[str],
    pytest_exit_code: int,
) -> list[str]:
    failures: list[str] = []
    nodes = payload.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        failures.append("timing nodes missing or empty")
        return failures

    recorded_exit = payload.get("pytest_exitstatus")
    if recorded_exit != pytest_exit_code:
        failures.append(
            f"timing pytest_exitstatus={recorded_exit!r} != pytest subprocess exit={pytest_exit_code}"
        )

    nodeids = [str(entry.get("nodeid", "")) for entry in nodes]
    if len(nodeids) != len(set(nodeids)):
        failures.append("duplicate nodeids in timing payload")

    observed = frozenset(nodeids)
    if observed != expected_nodes:
        failures.append(
            f"timing node inventory mismatch expected={len(expected_nodes)} observed={len(observed)}"
        )

    session_start = payload.get("session_start_monotonic")
    session_end = payload.get("session_end_monotonic")
    if not isinstance(session_start, (int, float)) or not isinstance(session_end, (int, float)):
        failures.append("timing session monotonic bounds missing or invalid")
    elif session_end < session_start:
        failures.append("timing session_end precedes session_start")

    for entry in nodes:
        nodeid = str(entry.get("nodeid", ""))
        phases = entry.get("phases")
        if not isinstance(phases, list) or not phases:
            failures.append(f"timing phases missing for {nodeid}")
            continue
        whens = [phase.get("when") for phase in phases]
        if len(whens) != len(set(whens)):
            failures.append(f"duplicate timing phases for {nodeid}")
        by_when = {str(phase.get("when")): phase for phase in phases}
        setup = by_when.get("setup")
        if setup is None:
            failures.append(f"missing setup phase for {nodeid}")
            continue
        setup_outcome = setup.get("outcome")
        if setup_outcome == "skipped":
            continue
        if setup_outcome == "failed":
            continue
        if "call" not in by_when:
            failures.append(f"missing call phase for {nodeid}")
            continue
        call_outcome = by_when["call"].get("outcome")
        if call_outcome != "skipped" and "teardown" not in by_when:
            failures.append(f"missing teardown phase for {nodeid}")

    return failures


def _phase_seconds(entry: dict[str, Any]) -> dict[str, float]:
    totals = {"setup": 0.0, "call": 0.0, "teardown": 0.0}
    for phase in entry.get("phases", []):
        when = phase.get("when")
        if when in totals:
            totals[str(when)] += float(phase.get("duration_seconds", 0.0))
    return totals


def aggregate_top10(
    nodes: list[dict[str, Any]],
    *,
    end_to_end_wall_seconds: float,
    pytest_session_wall_seconds: float | None,
) -> list[dict[str, Any]]:
    scored: list[tuple[float, str, dict[str, float]]] = []
    for entry in nodes:
        phases = _phase_seconds(entry)
        total = phases["setup"] + phases["call"] + phases["teardown"]
        scored.append((total, str(entry.get("nodeid", "")), phases))
    scored.sort(key=lambda item: (-item[0], item[1]))
    top = scored[:10]
    total_phase_sum = sum(item[0] for item in scored) or 1.0
    session_wall = (
        pytest_session_wall_seconds
        if pytest_session_wall_seconds and pytest_session_wall_seconds > 0
        else None
    )
    rows: list[dict[str, Any]] = []
    for total, nodeid, phases in top:
        row: dict[str, Any] = {
            "nodeid": nodeid,
            "setup_seconds": phases["setup"],
            "call_seconds": phases["call"],
            "teardown_seconds": phases["teardown"],
            "total_phase_seconds": total,
            "share_of_total_phase_time": total / total_phase_sum if total_phase_sum else 0.0,
        }
        if end_to_end_wall_seconds > 0:
            row["share_of_end_to_end_wall"] = total / end_to_end_wall_seconds
        if session_wall:
            row["share_of_pytest_session_wall"] = total / session_wall
        rows.append(row)
    return rows


def pytest_session_wall_seconds(timing_payload: dict[str, Any]) -> float | None:
    start = timing_payload.get("session_start_monotonic")
    end = timing_payload.get("session_end_monotonic")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end >= start:
        return float(end - start)
    return None


def load_cost_payload(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ProfileRuntimeError(f"cost payload missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileRuntimeError(f"cost payload malformed: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProfileRuntimeError(f"cost payload must be a JSON object: {path}")
    return payload


def _validate_nonnegative_int_count(value: Any, *, label: str) -> list[str]:
    if isinstance(value, bool):
        return [f"{label} cannot be bool"]
    if isinstance(value, int):
        if value < 0:
            return [f"{label} cannot be negative"]
        return []
    return [f"{label} must be a non-negative integer"]


def _validate_finite_nonnegative_seconds(value: Any, *, label: str) -> list[str]:
    if isinstance(value, bool):
        return [f"{label} cannot be bool"]
    if not isinstance(value, (int, float)):
        return [f"{label} must be numeric"]
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return [f"{label} must be finite"]
    if number < 0:
        return [f"{label} cannot be negative"]
    return []


def _validate_timed_operation_record(index: int, row: Any) -> list[str]:
    if not isinstance(row, dict):
        return [f"timed_operations[{index}] must be an object"]
    failures: list[str] = []
    failures.extend(
        _validate_finite_nonnegative_seconds(
            row.get("duration_seconds"),
            label=f"timed_operations[{index}].duration_seconds",
        )
    )
    spawn_label = row.get("spawn_label")
    if not isinstance(spawn_label, str) or not spawn_label:
        failures.append(f"timed_operations[{index}].spawn_label missing or invalid")
    category = row.get("category")
    if not isinstance(category, str) or not category:
        failures.append(f"timed_operations[{index}].category missing or invalid")
    outcome = row.get("outcome")
    if not isinstance(outcome, str) or not outcome:
        failures.append(f"timed_operations[{index}].outcome missing or invalid")
    if isinstance(spawn_label, str) and (
        spawn_label.startswith("subprocess_run:") or spawn_label.startswith("process_runner_run:")
    ):
        nodeid = row.get("nodeid")
        phase = row.get("phase")
        if not isinstance(nodeid, str) or not nodeid:
            failures.append(f"timed_operations[{index}].nodeid missing for wrapped spawn")
        if phase not in {"setup", "call", "teardown"}:
            failures.append(f"timed_operations[{index}].phase missing or invalid for wrapped spawn")
    return failures


def validate_cost_payload(payload: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if payload.get("schema") != "v08-installed-shard-runtime-costs/v1-preliminary":
        failures.append("cost schema mismatch")
        return failures

    popen_counts = payload.get("popen_counts_by_category")
    if not isinstance(popen_counts, dict):
        failures.append("popen_counts_by_category missing or invalid")
        return failures
    for category in POPEN_COUNT_CATEGORIES:
        failures.extend(
            _validate_nonnegative_int_count(
                popen_counts.get(category),
                label=f"popen_counts_by_category.{category}",
            )
        )

    timed_operations = payload.get("timed_operations")
    if not isinstance(timed_operations, list):
        failures.append("timed_operations missing or invalid")
        return failures
    for index, row in enumerate(timed_operations):
        failures.extend(_validate_timed_operation_record(index, row))

    category_duration_seconds = payload.get("category_duration_seconds")
    if not isinstance(category_duration_seconds, dict):
        failures.append("category_duration_seconds missing or invalid")
        return failures
    for bucket in CATEGORY_DURATION_BUCKETS:
        failures.extend(
            _validate_finite_nonnegative_seconds(
                category_duration_seconds.get(bucket),
                label=f"category_duration_seconds.{bucket}",
            )
        )

    coverage = payload.get("named_entrypoint_coverage")
    if not isinstance(coverage, dict):
        failures.append("named_entrypoint_coverage missing or invalid")
    elif not all(entry in coverage for entry in NAMED_COST_ENTRYPOINTS):
        failures.append("named_entrypoint_coverage incomplete")

    named_invocations = payload.get("named_entrypoint_invocations")
    if named_invocations is not None and not isinstance(named_invocations, list):
        failures.append("named_entrypoint_invocations invalid")
    elif isinstance(named_invocations, list):
        for index, row in enumerate(named_invocations):
            if not isinstance(row, dict):
                failures.append(f"named_entrypoint_invocations[{index}] must be an object")
                continue
            failures.extend(
                _validate_finite_nonnegative_seconds(
                    row.get("duration_seconds"),
                    label=f"named_entrypoint_invocations[{index}].duration_seconds",
                )
            )

    return failures


def cost_observation_metrics_satisfied(payload: dict[str, Any] | None) -> frozenset[str]:
    if payload is None:
        return frozenset()
    if validate_cost_payload(payload):
        return frozenset()

    popen_counts = payload["popen_counts_by_category"]
    timed_operations = payload["timed_operations"]
    category_duration_seconds = payload["category_duration_seconds"]

    satisfied: set[str] = {"subprocess_count", "cold_verifier_invocation_count"}

    any_popen = sum(int(popen_counts[category]) for category in POPEN_COUNT_CATEGORIES) > 0
    if not any_popen:
        satisfied.add("subprocess_wall_seconds")

    for popen_category, metric in (
        ("godot", "godot_invocation_seconds"),
        ("blender", "blender_invocation_seconds"),
    ):
        if int(popen_counts[popen_category]) == 0:
            satisfied.add(metric)

    process_runner_timed = any(
        isinstance(row, dict) and str(row.get("spawn_label", "")).startswith("process_runner_run:")
        for row in timed_operations
    )
    process_runner_seconds = category_duration_seconds.get("process_runner", 0.0)
    if not process_runner_timed and float(process_runner_seconds) == 0.0:
        satisfied.add("process_runner_seconds")
    elif process_runner_timed:
        satisfied.add("process_runner_seconds")

    return frozenset(satisfied & METRICS_FILLED_BY_COST_OBSERVATION)


def named_entrypoint_metrics_satisfied(coverage: dict[str, Any] | None) -> frozenset[str]:
    if not isinstance(coverage, dict):
        return frozenset()
    satisfied: set[str] = set()
    for metric, entrypoints in NAMED_ENTRYPOINT_METRIC_REQUIREMENTS.items():
        for entry in entrypoints:
            row = coverage.get(entry)
            if isinstance(row, dict) and row.get("coverage") == "instrumented_invocation":
                satisfied.add(metric)
                break
    return frozenset(satisfied & METRICS_FILLED_BY_NAMED_ENTRYPOINT)


def _cost_payload_valid_for_metrics(payload: dict[str, Any] | None) -> bool:
    if payload is None:
        return False
    return cost_observation_metrics_satisfied(payload) == METRICS_FILLED_BY_COST_OBSERVATION


def wheel_provenance_verified_for_metrics(
    *,
    wheel_path: Path | None,
    wheel_sha256: str | None,
    wheel_provenance_before_failures: list[str] | None,
    wheel_provenance_after_failures: list[str] | None,
    wheel_provenance_before_comparison_succeeded: bool,
    wheel_provenance_after_comparison_succeeded: bool,
) -> bool:
    if wheel_path is None or wheel_sha256 is None:
        return False
    if not wheel_provenance_before_comparison_succeeded:
        return False
    if not wheel_provenance_after_comparison_succeeded:
        return False
    before = wheel_provenance_before_failures or []
    after = wheel_provenance_after_failures or []
    return not before and not after


def metrics_missing_for_report(
    *,
    cost_payload: dict[str, Any] | None,
    wheel_provenance_verified: bool,
) -> list[str]:
    missing = set(METRICS_NOT_YET_COLLECTED)
    missing -= cost_observation_metrics_satisfied(cost_payload)
    coverage = (
        cost_payload.get("named_entrypoint_coverage") if isinstance(cost_payload, dict) else None
    )
    missing -= named_entrypoint_metrics_satisfied(coverage)
    if wheel_provenance_verified:
        missing -= METRICS_FILLED_BY_WHEEL_OPTION
    return sorted(missing)


def build_report(
    *,
    expected_version: str,
    test_root: Path,
    collect_expected: frozenset[str],
    collect_actual: frozenset[str] | None,
    collect_guard_ok: bool,
    provenance_before: dict[str, Any] | None,
    provenance_after: dict[str, Any] | None,
    child_probe_before: dict[str, Any] | None,
    child_probe_after: dict[str, Any] | None,
    provenance_ok: bool,
    timing_payload: dict[str, Any] | None,
    wall_clock_seconds: float,
    pytest_exit_code: int | None,
    guard_failures: list[str],
    observe_costs_enabled: bool = False,
    cost_payload: dict[str, Any] | None = None,
    wheel_path: Path | None = None,
    wheel_sha256: str | None = None,
    wheel_provenance_before_failures: list[str] | None = None,
    wheel_provenance_after_failures: list[str] | None = None,
    wheel_provenance_before_comparison_succeeded: bool = False,
    wheel_provenance_after_comparison_succeeded: bool = False,
) -> dict[str, Any]:
    nodes = (timing_payload or {}).get("nodes", [])
    session_wall = pytest_session_wall_seconds(timing_payload or {})
    wheel_before = wheel_provenance_before_failures or []
    wheel_after = wheel_provenance_after_failures or []
    wheel_verified = wheel_provenance_verified_for_metrics(
        wheel_path=wheel_path,
        wheel_sha256=wheel_sha256,
        wheel_provenance_before_failures=wheel_before,
        wheel_provenance_after_failures=wheel_after,
        wheel_provenance_before_comparison_succeeded=wheel_provenance_before_comparison_succeeded,
        wheel_provenance_after_comparison_succeeded=wheel_provenance_after_comparison_succeeded,
    )
    return {
        "schema": REPORT_SCHEMA,
        "shard": PROFILE_SHARD,
        "expected_version": expected_version,
        "test_root": str(test_root.resolve()),
        "wall_clock_seconds": wall_clock_seconds,
        "pytest_session_wall_seconds": session_wall,
        "pytest_exit_code": pytest_exit_code,
        "provenance_ok": provenance_ok,
        "collect_guard_ok": collect_guard_ok,
        "guard_failures": guard_failures,
        "observe_costs_enabled": observe_costs_enabled,
        "cost_observation": cost_payload,
        "wheel_path": str(wheel_path.resolve()) if wheel_path is not None else None,
        "installed_wheel_sha256": wheel_sha256 if wheel_verified else None,
        "input_wheel_sha256_unverified": (
            wheel_sha256
            if wheel_path is not None and wheel_sha256 is not None and not wheel_verified
            else None
        ),
        "builder_consumer_provenance_diff": {
            "before_failures": wheel_before,
            "after_failures": wheel_after,
            "identity_from_version_only": False,
        },
        "installed_package_before": provenance_before,
        "installed_package_after": provenance_after,
        "installed_package_child_before_pytest": child_probe_before,
        "installed_package_child_after_pytest": child_probe_after,
        "collection": {
            "expected_node_count": len(collect_expected),
            "expected_nodeids": sorted(collect_expected),
            "collected_node_count": len(collect_actual) if collect_actual is not None else None,
            "collected_nodeids": sorted(collect_actual) if collect_actual is not None else None,
        },
        "node_timings": nodes,
        "top10_by_total_phase_duration": aggregate_top10(
            nodes,
            end_to_end_wall_seconds=wall_clock_seconds,
            pytest_session_wall_seconds=session_wall,
        ),
        "metrics_missing": metrics_missing_for_report(
            cost_payload=cost_payload,
            wheel_provenance_verified=wheel_verified,
        ),
        "preliminary": True,
    }


def write_report(path: Path, payload: dict[str, Any]) -> None:
    _reject_existing_path(path, label="output report")
    _write_exclusive_json(path, payload, label="output report")


def _fresh_basetemp(parent: Path) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = parent / f"pytest-basetemp-{stamp}-{os.getpid()}"
    if candidate.exists():
        raise ProfileRuntimeError(f"refusing to reuse existing basetemp path: {candidate}")
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def _timing_sidecar_paths(output_json: Path) -> tuple[Path, Path, Path, Path, Path]:
    timing_path = output_json.parent / f".{output_json.name}.pytest-timings.json"
    return (
        timing_path,
        timing_path.with_name(timing_path.name + ".child-probe-before.json"),
        timing_path.with_name(timing_path.name + ".child-probe-after.json"),
        timing_path.with_name(timing_path.name + ".runtime-collect.json"),
        timing_path.with_name(timing_path.name + ".costs.json"),
    )


def run_profile(
    *,
    python: Path,
    test_root: Path,
    workspace: Path,
    output_json: Path,
    junit_xml: Path,
    basetemp_parent: Path,
    expected_version: str,
    collect_cwd: Path | None = None,
    profile_script: Path | None = None,
    skip_pytest: bool = False,
    observe_costs: bool = False,
    wheel_path: Path | None = None,
) -> int:
    if expected_version not in ALLOWED_EXPECTED_VERSIONS:
        raise ProfileRuntimeError(
            f"unsupported expected-version {expected_version!r}; "
            f"allowed={sorted(ALLOWED_EXPECTED_VERSIONS)}"
        )
    test_root = test_root.resolve()
    workspace = workspace.resolve()
    if (
        workspace == test_root
        or workspace.is_relative_to(test_root)
        or test_root.is_relative_to(workspace)
    ):
        raise ProfileRuntimeError(
            "workspace must be disjoint from the frozen test-root (outside-checkout consumer)"
        )
    timing_path, child_probe_before, child_probe_after, runtime_collect_path, costs_path = (
        _timing_sidecar_paths(output_json)
    )
    guard_output_evidence_paths(
        test_root=test_root,
        output_json=output_json,
        junit_xml=junit_xml,
        timing_path=timing_path,
        basetemp_parent=basetemp_parent,
    )
    _ensure_evidence_parent_dirs(
        output_json,
        junit_xml,
        timing_path,
        child_probe_before,
        child_probe_after,
        runtime_collect_path,
    )

    ci = load_frozen_candidate_ci(test_root)
    expected_nodes = expected_shard4_nodes(ci, test_root)
    cwd = (
        collect_cwd
        if collect_cwd is not None
        else Path(os.environ.get("TEMP", os.environ.get("TMP", "/tmp")))
    )
    script_path = profile_script or Path(__file__).resolve()

    guard_failures: list[str] = []
    provenance_before: dict[str, Any] | None = None
    provenance_after: dict[str, Any] | None = None
    child_probe_before_payload: dict[str, Any] | None = None
    child_probe_after_payload: dict[str, Any] | None = None
    provenance_ok = False
    collect_actual: frozenset[str] | None = None
    collect_guard_ok = False
    pytest_exit: int | None = None
    timing_payload: dict[str, Any] | None = None
    cost_payload: dict[str, Any] | None = None
    wheel_sha256: str | None = None
    wheel_before_failures: list[str] = []
    wheel_after_failures: list[str] = []
    wheel_before_comparison_succeeded = False
    wheel_after_comparison_succeeded = False
    consumer_site_packages: Path | None = None

    if wheel_path is not None:
        try:
            verify_wheel_consumer_containment(
                wheel_path,
                resolve_consumer_site_packages(python, cwd=cwd),
                test_root=test_root,
                workspace=workspace,
            )
            consumer_site_packages = resolve_consumer_site_packages(python, cwd=cwd)
            wheel_sha256 = sha256_file_path(wheel_path.resolve())
            wheel_before_failures = compare_gamefactory_wheel_to_site_packages(
                wheel_path,
                consumer_site_packages,
            )
            wheel_before_comparison_succeeded = True
            if wheel_before_failures:
                guard_failures.extend(
                    [f"wheel_provenance_before:{item}" for item in wheel_before_failures]
                )
        except ProfileRuntimeError as exc:
            guard_failures.append(f"wheel_provenance_before: {exc}")

    wall_start = time.monotonic()
    try:
        provenance_before = run_installed_probe(
            python,
            workspace=workspace,
            test_root=test_root,
            expected_version=expected_version,
            cwd=cwd,
        )
    except ProfileRuntimeError as exc:
        guard_failures.append(f"provenance_before: {exc}")

    if skip_pytest:
        try:
            collect_actual = run_pytest_collect_shard(ci, python, test_root, collect_cwd=cwd)
            guard_collected_nodes(ci, collect_actual, expected_nodes)
            collect_guard_ok = True
        except Exception as exc:
            guard_failures.append(f"collect_guard: {exc}")

    basetemp = _fresh_basetemp(basetemp_parent)
    pytest_executed = False
    if provenance_before is not None and not skip_pytest:
        try:
            shard_result = run_pytest_shard(
                python,
                test_root,
                workspace=workspace,
                basetemp=basetemp,
                junit_path=junit_xml,
                collect_cwd=cwd,
                profile_script=script_path,
                timing_path=timing_path,
                expected_version=expected_version,
                child_probe_before=child_probe_before,
                child_probe_after=child_probe_after,
                runtime_collect_path=runtime_collect_path,
                observe_costs=observe_costs,
                costs_path=costs_path if observe_costs else None,
            )
            pytest_executed = True
            pytest_exit = shard_result.exit_code
            collect_actual = shard_result.collect_actual
            collect_guard_ok = bool(collect_actual)
            child_probe_before_payload = shard_result.child_probe_before
            child_probe_after_payload = shard_result.child_probe_after
            try:
                timing_payload = load_timing_payload(timing_path)
            except ProfileRuntimeError as exc:
                guard_failures.append(f"timing_load: {exc}")
            if observe_costs:
                try:
                    cost_payload = load_cost_payload(costs_path)
                except ProfileRuntimeError as exc:
                    guard_failures.append(f"cost_load: {exc}")
                if cost_payload is not None:
                    guard_failures.extend(validate_cost_payload(cost_payload))
            if timing_payload is not None:
                guard_failures.extend(
                    validate_timing_payload(
                        timing_payload,
                        expected_nodes=expected_nodes,
                        pytest_exit_code=pytest_exit,
                    )
                )
            try:
                provenance_after = run_installed_probe(
                    python,
                    workspace=workspace,
                    test_root=test_root,
                    expected_version=expected_version,
                    cwd=cwd,
                )
            except ProfileRuntimeError as exc:
                guard_failures.append(f"provenance_after: {exc}")
        except ProfileRuntimeError as exc:
            guard_failures.append(f"pytest_shard: {exc}")

    if pytest_executed and timing_payload is None:
        guard_failures.append("timing_missing_after_pytest")

    if pytest_executed and observe_costs and cost_payload is None:
        guard_failures.append("cost_observation_missing_after_pytest")

    if pytest_executed:
        provenance_failures = provenance_identity_guard_failures(
            provenance_before=provenance_before,
            provenance_after=provenance_after,
            child_probe_before=child_probe_before_payload,
            child_probe_after=child_probe_after_payload,
        )
        guard_failures.extend(provenance_failures)
        provenance_ok = (
            timing_payload is not None
            and not provenance_failures
            and provenance_before is not None
            and provenance_after is not None
            and child_probe_before_payload is not None
            and child_probe_after_payload is not None
        )

    if wheel_path is not None and consumer_site_packages is not None:
        try:
            wheel_after_failures = compare_gamefactory_wheel_to_site_packages(
                wheel_path,
                consumer_site_packages,
            )
            wheel_after_comparison_succeeded = True
            if wheel_after_failures:
                guard_failures.extend(
                    [f"wheel_provenance_after:{item}" for item in wheel_after_failures]
                )
        except ProfileRuntimeError as exc:
            guard_failures.append(f"wheel_provenance_after: {exc}")

    wall_seconds = time.monotonic() - wall_start
    report = build_report(
        expected_version=expected_version,
        test_root=test_root,
        collect_expected=expected_nodes,
        collect_actual=collect_actual,
        collect_guard_ok=collect_guard_ok,
        provenance_before=provenance_before,
        provenance_after=provenance_after,
        child_probe_before=child_probe_before_payload,
        child_probe_after=child_probe_after_payload,
        provenance_ok=provenance_ok,
        timing_payload=timing_payload,
        wall_clock_seconds=wall_seconds,
        pytest_exit_code=pytest_exit,
        guard_failures=guard_failures,
        observe_costs_enabled=observe_costs,
        cost_payload=cost_payload,
        wheel_path=wheel_path,
        wheel_sha256=wheel_sha256,
        wheel_provenance_before_failures=wheel_before_failures,
        wheel_provenance_after_failures=wheel_after_failures,
        wheel_provenance_before_comparison_succeeded=wheel_before_comparison_succeeded,
        wheel_provenance_after_comparison_succeeded=wheel_after_comparison_succeeded,
    )
    write_report(output_json, report)

    if guard_failures:
        return 1
    if pytest_executed and not provenance_ok:
        return 1
    if pytest_exit is not None:
        return pytest_exit
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, help="installed consumer interpreter")
    parser.add_argument(
        "--test-root",
        required=True,
        type=Path,
        help="frozen Git archive root (tests/scripts/pyproject only)",
    )
    parser.add_argument(
        "--workspace",
        required=True,
        type=Path,
        help="directory outside test-root used for installed-package workspace checks",
    )
    parser.add_argument("--output", required=True, type=Path, help="JSON measurement report path")
    parser.add_argument("--junit", required=True, type=Path, help="pytest JUnit XML output path")
    parser.add_argument(
        "--basetemp",
        required=True,
        type=Path,
        help="parent directory; a fresh child basetemp is created per run",
    )
    parser.add_argument(
        "--expected-version",
        required=True,
        choices=sorted(ALLOWED_EXPECTED_VERSIONS),
        help="expected installed gamefactory version (RC8 or RC9)",
    )
    parser.add_argument(
        "--collect-cwd",
        default=None,
        type=Path,
        help="cwd for collect/pytest subprocesses (default: system temp)",
    )
    parser.add_argument(
        "--observe-costs",
        action="store_true",
        help="record optional child pytest subprocess/cost sidecar (preparation only)",
    )
    parser.add_argument(
        "--wheel",
        default=None,
        type=Path,
        help="optional wheel for read-only consumer site-packages byte provenance",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_profile(
            python=Path(args.python),
            test_root=Path(args.test_root),
            workspace=Path(args.workspace),
            output_json=Path(args.output),
            junit_xml=Path(args.junit),
            basetemp_parent=Path(args.basetemp),
            expected_version=str(args.expected_version),
            collect_cwd=Path(args.collect_cwd) if args.collect_cwd else None,
            observe_costs=bool(args.observe_costs),
            wheel_path=Path(args.wheel) if args.wheel else None,
        )
    except ProfileRuntimeError as exc:
        print(f"profile-v08-shard-runtime: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
