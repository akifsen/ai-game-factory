"""Staged Godot process for internal skin deformation oracle (ADR 0019)."""

from __future__ import annotations

import json
import shutil
import uuid
from importlib import resources
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.internal_rig_canonical import (
    reviewed_text_sha256,
    runtime_request_digest,
    sha256_bytes,
)
from gamefactory.adapters.assets.internal_skin_decode import decode_internal_skinned_glb
from gamefactory.adapters.assets.internal_skin_region import (
    REGION_BOUNDARY_TOLERANCE,
    region_population_counts,
)
from gamefactory.core.domain.internal_skin_contract import InternalSkinContract
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner


class OracleStageError(ValueError):
    """Oracle staging directory is unsafe or already present."""


class OracleExecutionError(RuntimeError):
    """Godot import/script failed or produced an unusable oracle result."""


def _embed_glb_in_godot(glb_bytes: bytes, scene_path: Path) -> None:
    scene_path.write_text(
        """[gd_scene load_steps=2 format=3]

[ext_resource type="PackedScene" path="res://asset.glb" id="1"]

[node name="Root" type="Node3D"]

[node name="Asset" parent="." instance=ExtResource("1")]
""",
        encoding="utf-8",
    )
    (scene_path.parent / "asset.glb").write_bytes(glb_bytes)


def _assert_fresh_stage_dir(stage: Path) -> None:
    if stage.exists():
        raise OracleStageError(f"oracle stage already exists: {stage}")
    if stage.is_symlink():
        raise OracleStageError(f"oracle stage path is a symlink: {stage}")
    parent = stage.parent
    if parent.exists() and parent.is_symlink():
        raise OracleStageError(f"oracle output parent is a symlink: {parent}")


def _godot_version(godot_executable: Path, proc: ProcessRunner) -> str:
    result = proc.run(
        CommandRequest(
            args=[str(godot_executable), "--version"],
            cwd=Path.cwd(),
            timeout_seconds=30.0,
            minimal_env=True,
        )
    )
    line = (result.stdout or result.stderr or "").strip().splitlines()
    if not line:
        raise OracleExecutionError("Godot --version produced no output")
    return line[0].strip()


def _expected_region_populations(
    glb_path: Path, contract: InternalSkinContract
) -> tuple[int, int, int]:
    decoded = decode_internal_skinned_glb(glb_path)
    oracle = contract.oracle
    affected = {"min": list(oracle.affected.min_xyz), "max": list(oracle.affected.max_xyz)}
    unaffected = {"min": list(oracle.unaffected.min_xyz), "max": list(oracle.unaffected.max_xyz)}
    aff, unaff = region_population_counts(
        decoded.primitive.positions,
        affected,
        unaffected,
        boundary_tolerance=REGION_BOUNDARY_TOLERANCE,
    )
    return len(decoded.primitive.positions), aff, unaff


def _parse_result_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OracleExecutionError(f"could not read oracle result: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OracleExecutionError(f"oracle result is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise OracleExecutionError("oracle result must be a JSON object")
    return parsed


def run_skin_deformation_oracle(
    godot_executable: Path,
    glb_path: Path,
    contract: InternalSkinContract,
    output_dir: Path,
    *,
    contract_sha256: str | None = None,
    harness_sha256: str | None = None,
    runner: ProcessRunner | None = None,
    timeout_seconds: float = 120.0,
    appdata_dir: Path | None = None,
) -> dict[str, Any]:
    """Pose a named bone in real Godot and measure baked vertex displacement."""
    proc = runner or ProcessRunner(sanitize_output=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    stage = output_dir / f"godot_skin_stage_{uuid.uuid4().hex}"
    _assert_fresh_stage_dir(stage)
    stage.mkdir(parents=True)
    glb_bytes = glb_path.read_bytes()
    (stage / "asset.glb").write_bytes(glb_bytes)
    _embed_glb_in_godot(glb_bytes, stage / "Main.tscn")
    harness = Path(
        str(resources.files("gamefactory.resources.godot").joinpath("skin_deformation_harness.gd"))
    )
    harness_reviewed, harness_raw = reviewed_text_sha256(harness)
    if harness_sha256 is not None and harness_sha256 not in {harness_reviewed, harness_raw}:
        raise OracleStageError("harness_sha256 does not match packaged harness")
    shutil.copyfile(harness, stage / "skin_deformation_harness.gd")
    glb_sha256 = sha256_bytes(glb_bytes)
    if contract_sha256 is None:
        contract_path = Path(
            str(
                resources.files("gamefactory.resources.internal_skin").joinpath(
                    "humanoid_12bone_contract.json"
                )
            )
        )
        contract_sha256 = sha256_bytes(contract_path.read_bytes())
    godot_version = _godot_version(godot_executable, proc)
    vertex_count, affected_count, unaffected_count = _expected_region_populations(
        glb_path, contract
    )
    (stage / "project.godot").write_text(
        "\n".join(
            [
                "config_version=5",
                "",
                "[application]",
                'config/name="Internal Skin Oracle"',
                "",
                "[rendering]",
                'renderer/rendering_method="gl_compatibility"',
            ]
        ),
        encoding="utf-8",
    )
    oracle = contract.oracle
    request_path = stage / "request.json"
    result_path = stage / "result.json"
    bound_request: dict[str, Any] = {
        "schema_version": "rig-runtime-request-0.8.0",
        "glb_sha256": glb_sha256,
        "contract_sha256": contract_sha256,
        "harness_sha256": harness_reviewed,
        "godot_version": godot_version,
        "pose_bone": oracle.pose_bone,
        "rotation_axis": list(oracle.rotation_axis),
        "rotation_degrees": oracle.rotation_degrees,
        "affected": {
            "min": list(oracle.affected.min_xyz),
            "max": list(oracle.affected.max_xyz),
        },
        "unaffected": {
            "min": list(oracle.unaffected.min_xyz),
            "max": list(oracle.unaffected.max_xyz),
        },
        "min_affected_displacement": oracle.min_affected_displacement,
        "max_unaffected_displacement": oracle.max_unaffected_displacement,
        "max_affected_displacement": oracle.max_affected_displacement,
        "vertex_count": vertex_count,
        "affected_vertex_count": affected_count,
        "unaffected_vertex_count": unaffected_count,
        "region_boundary_tolerance": REGION_BOUNDARY_TOLERANCE,
    }
    bound_request["request_digest"] = runtime_request_digest(bound_request)
    stage_request = {
        **bound_request,
        "glb": "res://Main.tscn",
        "output_path": str(result_path),
    }
    request_path.write_text(json.dumps(stage_request, sort_keys=True), encoding="utf-8")
    env_overrides: dict[str, str] = {}
    if appdata_dir is not None:
        env_overrides["APPDATA"] = str(appdata_dir)
    import_cmd = [
        str(godot_executable),
        "--path",
        str(stage),
        "--import",
    ]
    import_result = proc.run(
        CommandRequest(
            args=import_cmd,
            cwd=stage,
            timeout_seconds=timeout_seconds,
            minimal_env=False,
            env_overrides=env_overrides,
        )
    )
    if import_result.exit_code != 0:
        raise OracleExecutionError(
            f"Godot --import failed with exit {import_result.exit_code}: "
            f"{import_result.stderr[-1500:]}"
        )
    command = [
        str(godot_executable),
        "--path",
        str(stage),
        "--script",
        "res://skin_deformation_harness.gd",
        "--",
        "--request",
        str(request_path),
    ]
    script_result = proc.run(
        CommandRequest(
            args=command,
            cwd=stage,
            timeout_seconds=timeout_seconds,
            minimal_env=False,
            env_overrides=env_overrides,
        )
    )
    if not result_path.is_file():
        raise OracleExecutionError(
            "oracle did not write result.json "
            f"(script exit {script_result.exit_code}): {script_result.stderr[-1500:]}"
        )
    payload = _parse_result_json(result_path)
    if payload.get("schema_version") != "rig-runtime-observation-0.8.0":
        raise OracleExecutionError("oracle observation schema_version missing or wrong")
    if payload.get("max_affected_displacement") is None:
        reason = str(payload.get("reason", "unknown"))
        if script_result.exit_code != 0:
            raise OracleExecutionError(
                f"oracle process failed without geometry observation: {reason}"
            )
        raise OracleExecutionError(f"oracle did not produce geometry observation: {reason}")
    expected_digest = bound_request["request_digest"]
    if payload.get("request_digest") != expected_digest:
        raise OracleExecutionError("oracle observation request_digest does not match bound request")
    payload["process_exit_code"] = script_result.exit_code
    payload["import_exit_code"] = import_result.exit_code
    payload["stage_dir"] = str(stage)
    return payload
