"""Blender asset processing adapter coordinating deterministic background execution."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.glb_validator import preflight_glb
from gamefactory.adapters.dcc.blender import BlenderAdapter
from gamefactory.adapters.dcc.blender_environment import (
    BLENDER_PYTHON_FAILURE_EXIT_CODE,
    BLENDER_PYTHONPATH_ENV,
    BlenderDependencyPreflight,
    blender_env_overrides,
    format_blender_preflight_failure_message,
    parse_blender_python_paths,
    run_blender_dependency_preflight,
)
from gamefactory.core.domain.asset_contracts import AssetSpecification
from gamefactory.core.domain.errors import DccFailedError
from gamefactory.core.domain.models import utc_now_iso
from gamefactory.core.execution.process_runner import CommandRequest, CommandResult, ProcessRunner

_INLINE_LOG_CHARS = 16_000


def _absolute_lexical(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _reject_linked_components(path: Path, label: str) -> None:
    current = path
    while True:
        is_junction = getattr(current, "is_junction", lambda: False)
        if current.is_symlink() or is_junction():
            raise ValueError(f"{label} path traverses a symlink or junction: {current}")
        if current.parent == current:
            break
        current = current.parent


def _command_display(args: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def _blender_version_line(stdout: str) -> str:
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Blender "):
            return stripped
    return "(not present in captured stdout)"


def _prefer_traceback(text: str) -> tuple[str, bool]:
    """Keep a Python traceback in the inline message when the log is large."""
    if not text:
        return "(empty)", False
    if len(text) <= _INLINE_LOG_CHARS:
        return text, False
    marker = text.find("Traceback (most recent call last):")
    note = "[inline excerpt; full captured text is in the diagnostics artifact]"
    if marker != -1:
        tail = text[marker:]
        if len(tail) <= _INLINE_LOG_CHARS:
            return f"...{note}...\n{tail}", True
        return f"{tail[:_INLINE_LOG_CHARS]}\n...{note}...\n", True
    head = 2_000
    tail = text[-(_INLINE_LOG_CHARS - head) :]
    return f"{text[:head]}\n...{note}...\n{tail}", True


def _artifact_note(path: Path) -> str:
    if path.is_file():
        return f"path={path} exists=true size={path.stat().st_size} absolute={path.is_absolute()}"
    return f"path={path} exists=false size=0 absolute={path.is_absolute()}"


def _environment_note(
    runner: ProcessRunner,
    request: CommandRequest,
    python_paths: Sequence[Path | str] = (),
) -> str:
    build_env = getattr(runner, "build_env", None)
    overrides = len(request.env_overrides)
    paths_str = os.pathsep.join(str(p) for p in python_paths) if python_paths else "(none)"
    if not callable(build_env):
        return (
            f"minimal_env={str(request.minimal_env).lower()}; "
            f"env_overrides_count={overrides}; keys=(unavailable); "
            f"blender_python_paths={paths_str}"
        )
    keys = ",".join(sorted(str(key) for key in build_env(request)))
    return (
        f"minimal_env={str(request.minimal_env).lower()}; "
        f"env_overrides_count={overrides}; keys={keys or '(none)'}; "
        f"blender_python_paths={paths_str}"
    )


def _write_diagnostics(path: Path, body: str) -> str:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
    except OSError as exc:
        return f"(diagnostics artifact not written: {exc})"
    return str(path)


def _blender_failure(
    summary: str,
    res: CommandResult,
    request: CommandRequest,
    runner: ProcessRunner,
    *,
    script_file: Path,
    raw_glb: Path,
    processed_glb: Path,
    contract_path: Path,
    diagnostics_path: Path,
    asset_id: str,
    python_paths: Sequence[Path | str] = (),
) -> DccFailedError:
    """Build a DCC failure whose message keeps the Blender evidence CI persists."""
    if contract_path.is_file():
        contract_text = contract_path.read_text(encoding="utf-8")
    else:
        contract_text = ""
    stdout_view, stdout_excerpted = _prefer_traceback(res.stdout)
    stderr_view, stderr_excerpted = _prefer_traceback(res.stderr)
    contract_view, contract_excerpted = _prefer_traceback(contract_text)
    parent = processed_glb.parent
    notes: list[str] = []
    if res.stdout_truncated:
        notes.append("stdout capture hit the process runner bound")
    if res.stderr_truncated:
        notes.append("stderr capture hit the process runner bound")
    if stdout_excerpted or stderr_excerpted or contract_excerpted:
        notes.append("inline message is an excerpt")
    capture = "; ".join(notes) if notes else "captured logs fit in this message"
    header = [
        summary,
        f"blender_version: {_blender_version_line(res.stdout)}",
        f"executable: {request.args[0] if request.args else ''}",
        f"command: {_command_display(list(request.args))}",
        f"cwd: {request.cwd}",
        f"exit_code: {res.exit_code}",
        f"processing_script: {script_file}",
        f"input_glb: {_artifact_note(raw_glb)}",
        f"output_glb: {_artifact_note(processed_glb)}",
        (
            f"output_parent: path={parent} exists={parent.is_dir()} "
            f"writable={os.access(parent, os.W_OK)}"
        ),
        f"processing_contract: {contract_path}",
        f"environment: {_environment_note(runner, request, python_paths)}",
        f"capture: {capture}",
    ]
    full_body = "\n".join(
        [
            *header,
            "--- processing contract ---",
            contract_text or "(empty)",
            "--- stdout ---",
            res.stdout or "(empty)",
            "--- stderr ---",
            res.stderr or "(empty)",
        ]
    )
    artifact = _write_diagnostics(diagnostics_path, full_body + "\n")
    message = "\n".join(
        [
            *header,
            "--- processing contract ---",
            contract_view,
            "--- stdout ---",
            stdout_view,
            "--- stderr ---",
            stderr_view,
            f"diagnostics_artifact: {artifact}",
        ]
    )
    return DccFailedError(
        message,
        exit_code=res.exit_code,
        stderr=res.stderr,
        details={
            "stdout": res.stdout,
            "asset_id": asset_id,
            "command": list(request.args),
            "cwd": str(request.cwd),
            "processing_script": str(script_file),
            "input_glb": str(raw_glb),
            "output_glb": str(processed_glb),
            "processing_contract": str(contract_path),
            "diagnostics_artifact": artifact,
            "stdout_truncated": res.stdout_truncated,
            "stderr_truncated": res.stderr_truncated,
            "blender_version": _blender_version_line(res.stdout),
            "blender_python_paths": [str(p) for p in python_paths],
        },
    )


@dataclass(frozen=True)
class BlenderProcessResult:
    status: str
    exit_code: int
    duration_seconds: float
    blender_version: str
    script_sha256: str
    raw_glb_sha256: str
    processed_glb_sha256: str
    raw_metrics: dict[str, Any]
    processed_metrics: dict[str, Any]
    report_data: dict[str, Any]
    processed_at: str = field(default_factory=utc_now_iso)
    dependency_preflight: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BlenderAssetProcessor:
    """Manages deterministic Blender background processing for static props."""

    def __init__(
        self,
        blender_executable: Path | str | None = None,
        runner: ProcessRunner | None = None,
        python_paths: Sequence[Path | str] | None = None,
        dependency_preflight: bool = True,
    ) -> None:
        self.runner = runner or ProcessRunner(sanitize_output=True)
        if blender_executable:
            self.blender_exe = str(Path(blender_executable).resolve())
        else:
            detect = BlenderAdapter(self.runner).detect_tool()
            if not detect.available or not detect.executable_path:
                raise DccFailedError(
                    "Blender executable not available or detected on host",
                    details={"status": detect.status, "reason": detect.details.get("reason")},
                )
            self.blender_exe = detect.executable_path

        if python_paths is None:
            raw_env = os.environ.get(BLENDER_PYTHONPATH_ENV)
            self.python_paths = parse_blender_python_paths(raw_env)
        else:
            self.python_paths = parse_blender_python_paths(python_paths)

        self.dependency_preflight = dependency_preflight
        self.last_dependency_preflight: BlenderDependencyPreflight | None = None
        self._cached_preflight: BlenderDependencyPreflight | None = None

    @staticmethod
    def get_script_path() -> Path:
        """Resolve path to the embedded Blender processing script."""
        pkg_file = files("gamefactory").joinpath("resources/blender/process_asset.py")
        return Path(str(pkg_file)).resolve()

    def process_asset(
        self,
        raw_glb_path: Path,
        processed_glb_path: Path,
        spec: AssetSpecification,
        report_path: Path | None = None,
        timeout_seconds: float = 60.0,
        lod1_ratio: float | None = None,
    ) -> BlenderProcessResult:
        """Run deterministic Blender script on raw GLB to produce processed GLB."""
        raw_glb = _absolute_lexical(raw_glb_path)
        _reject_linked_components(raw_glb, "raw GLB")
        if not raw_glb.is_file():
            raise DccFailedError(f"Input raw GLB does not exist: {raw_glb}")
        if raw_glb.suffix.casefold() != ".glb":
            raise ValueError("raw input must use the .glb extension")

        profile = spec.bound_profile()
        contract = profile.processing_contract(spec)
        if spec.orientation.up != "+Y" or spec.orientation.front != "-Z":
            raise ValueError("Blender processing supports only +Y up and -Z front")
        if contract["collider_policy"] != "box":
            raise ValueError(
                f"Unsupported collider_policy for {profile.qualified}: {contract['collider_policy']}"
            )
        if spec.lod_policy not in profile.document.processing.allowed_lod_policies:
            raise ValueError(f"lod_policy {spec.lod_policy} is not allowed by {profile.qualified}")
        ratio = spec.geometry_budget.lod_ratio if lod1_ratio is None else lod1_ratio
        if not 0.05 <= ratio <= 0.95:
            raise ValueError("lod1_ratio must be between 0.05 and 0.95")
        processed_glb = _absolute_lexical(processed_glb_path)
        resolved_report = _absolute_lexical(
            report_path
            if report_path is not None
            else processed_glb.parent / f"{spec.asset_id}_blender_report.json"
        )
        raw_key = os.path.normcase(str(raw_glb))
        output_key = os.path.normcase(str(processed_glb))
        report_key = os.path.normcase(str(resolved_report))
        if len({raw_key, output_key, report_key}) != 3:
            raise ValueError("raw GLB, processed GLB, and report paths must be distinct")
        for target, label, suffix in (
            (processed_glb, "processed GLB", ".glb"),
            (resolved_report, "processing report", ".json"),
        ):
            _reject_linked_components(target, label)
            if target.exists() or target.is_symlink():
                raise ValueError(f"refusing to overwrite existing {label}: {target}")
            if target.suffix.casefold() != suffix:
                raise ValueError(f"{label} must use the {suffix} extension")

        # Structural, bounds-checked safety inspection occurs before the Blender process.
        source_facts = preflight_glb(raw_glb)
        raw_hash_before = str(source_facts["sha256"])
        if source_facts["triangles"] > spec.geometry_budget.max_triangles_lod0:
            raise ValueError(
                "raw mesh exceeds the LOD0 budget; destructive LOD0 decimation is disabled"
            )
        if source_facts["materials"] > spec.material_budget.max_materials:
            raise ValueError("raw material count exceeds the specification budget")
        if source_facts["texture_max_dimension"] > spec.texture_budget.max_dimension:
            raise ValueError("raw embedded texture exceeds the specification dimension budget")

        processed_glb.parent.mkdir(parents=True, exist_ok=True)

        if self.dependency_preflight:
            if self._cached_preflight is None or self._cached_preflight.status != "PASS":
                preflight = run_blender_dependency_preflight(
                    blender_executable=self.blender_exe,
                    runner=self.runner,
                    python_paths=self.python_paths,
                )
                self.last_dependency_preflight = preflight
                if preflight.status != "PASS":
                    msg = format_blender_preflight_failure_message(preflight)
                    raise DccFailedError(
                        msg,
                        exit_code=preflight.exit_code,
                        stderr=preflight.stderr_excerpt or "",
                        details={
                            "reason": "BLENDER_DEPENDENCY_UNAVAILABLE",
                            "preflight": preflight.to_dict(),
                        },
                    )
                self._cached_preflight = preflight

        script_file = self.get_script_path()
        script_bytes = script_file.read_bytes()
        script_hash = hashlib.sha256(script_bytes).hexdigest()

        resolved_report.parent.mkdir(parents=True, exist_ok=True)
        contract_path = resolved_report.with_suffix(".contract.json")
        if contract_path.exists() or contract_path.is_symlink():
            raise ValueError(f"refusing to overwrite existing processing contract: {contract_path}")
        contract_path.write_text(
            json.dumps(contract, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )

        cmd = [
            self.blender_exe,
            "--background",
            "--factory-startup",
            "--python-exit-code",
            str(BLENDER_PYTHON_FAILURE_EXIT_CODE),
            "--python",
            str(script_file),
            "--",
            "--input-glb",
            str(raw_glb),
            "--output-glb",
            str(processed_glb),
            "--report-path",
            str(resolved_report),
            "--asset-id",
            spec.asset_id,
            "--target-width",
            str(spec.dimensions.width_m),
            "--target-depth",
            str(spec.dimensions.depth_m),
            "--target-height",
            str(spec.dimensions.height_m),
            "--lod1-ratio",
            str(ratio),
            "--origin-policy",
            spec.origin_policy,
            "--lod-policy",
            spec.lod_policy,
            "--contract",
            str(contract_path),
        ]

        req = CommandRequest(
            args=cmd,
            cwd=processed_glb.parent,
            env_overrides=blender_env_overrides(self.python_paths),
            timeout_seconds=timeout_seconds,
        )

        res = self.runner.run(req)
        for path, label in (
            (raw_glb, "raw GLB"),
            (processed_glb, "processed GLB"),
            (resolved_report, "processing report"),
        ):
            _reject_linked_components(path, label)
        diagnostics_path = resolved_report.with_suffix(".blender-diagnostics.txt")
        if res.exit_code != 0:
            raise _blender_failure(
                f"Blender processing failed with exit code {res.exit_code}",
                res,
                req,
                self.runner,
                script_file=script_file,
                raw_glb=raw_glb,
                processed_glb=processed_glb,
                contract_path=contract_path,
                diagnostics_path=diagnostics_path,
                asset_id=spec.asset_id,
                python_paths=self.python_paths,
            )

        if not processed_glb.is_file() or processed_glb.stat().st_size == 0:
            raise _blender_failure(
                f"Blender succeeded but output processed GLB is missing or empty: {processed_glb}",
                res,
                req,
                self.runner,
                script_file=script_file,
                raw_glb=raw_glb,
                processed_glb=processed_glb,
                contract_path=contract_path,
                diagnostics_path=diagnostics_path,
                asset_id=spec.asset_id,
                python_paths=self.python_paths,
            )

        # Invariant check: raw GLB was untouched and preserved!
        raw_bytes_after = raw_glb.read_bytes()
        raw_hash_after = hashlib.sha256(raw_bytes_after).hexdigest()
        if raw_hash_before != raw_hash_after:
            raise DccFailedError(
                "Raw GLB was mutated during Blender processing! Raw must remain immutable."
            )

        processed_bytes = processed_glb.read_bytes()
        processed_hash = hashlib.sha256(processed_bytes).hexdigest()

        report_data: dict[str, Any] = {}
        if not resolved_report.is_file():
            raise DccFailedError(
                "Blender succeeded without writing its processing report", exit_code=res.exit_code
            )
        try:
            report_data = json.loads(resolved_report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DccFailedError(
                f"Blender processing report is invalid: {exc}", exit_code=res.exit_code
            ) from exc
        if report_data.get("status") != "SUCCESS" or report_data.get("exit_code") != 0:
            raise DccFailedError(
                "Blender processing report does not record successful completion",
                exit_code=res.exit_code,
            )
        if report_data.get("input_raw_glb_sha256") != raw_hash_before:
            raise DccFailedError("Blender report raw input hash does not match immutable source")
        if report_data.get("output_sha256") != processed_hash:
            raise DccFailedError("Blender report output hash does not match processed artifact")
        if report_data.get("processing_script_sha256") != script_hash:
            raise DccFailedError("Blender report script hash does not match executed script")

        return BlenderProcessResult(
            status="SUCCESS",
            exit_code=res.exit_code,
            duration_seconds=res.duration_seconds,
            blender_version=report_data.get("blender_version", "unknown"),
            script_sha256=script_hash,
            raw_glb_sha256=raw_hash_before,
            processed_glb_sha256=processed_hash,
            raw_metrics=report_data.get("raw_metrics", {}),
            processed_metrics=report_data.get("processed_metrics", {}),
            report_data=report_data,
            dependency_preflight=(
                self.last_dependency_preflight.to_dict()
                if self.last_dependency_preflight is not None
                else {}
            ),
        )
