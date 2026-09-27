"""Blender asset processing adapter coordinating deterministic background execution."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.glb_validator import preflight_glb
from gamefactory.adapters.dcc.blender import BlenderAdapter
from gamefactory.core.domain.asset_contracts import AssetSpecification
from gamefactory.core.domain.errors import DccFailedError
from gamefactory.core.domain.models import utc_now_iso
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BlenderAssetProcessor:
    """Manages deterministic Blender background processing for static props."""

    def __init__(
        self,
        blender_executable: Path | str | None = None,
        runner: ProcessRunner | None = None,
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
            timeout_seconds=timeout_seconds,
        )

        res = self.runner.run(req)
        for path, label in (
            (raw_glb, "raw GLB"),
            (processed_glb, "processed GLB"),
            (resolved_report, "processing report"),
        ):
            _reject_linked_components(path, label)
        if res.exit_code != 0:
            raise DccFailedError(
                f"Blender processing failed with exit code {res.exit_code}: {res.stderr or res.stdout}",
                exit_code=res.exit_code,
                stderr=res.stderr,
                details={"stdout": res.stdout, "asset_id": spec.asset_id},
            )

        if not processed_glb.is_file() or processed_glb.stat().st_size == 0:
            raise DccFailedError(
                f"Blender succeeded but output processed GLB is missing or empty: {processed_glb}",
                exit_code=res.exit_code,
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
        )
