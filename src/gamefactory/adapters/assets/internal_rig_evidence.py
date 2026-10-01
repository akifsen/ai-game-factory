"""Export portable internal rig evidence bundles (V0.8-2)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.internal_rig_canonical import (
    VALIDATOR_CONTRACT_VERSION,
    contract_canonical_digest,
    reviewed_text_sha256,
    runtime_request_digest,
    sha256_bytes,
)
from gamefactory.adapters.assets.internal_skin import validate_internal_skinned_glb
from gamefactory.adapters.engines.skin_deformation_oracle import run_skin_deformation_oracle
from gamefactory.adapters.engines.skin_oracle_verify import verify_oracle_payload
from gamefactory.core.domain.asset_contracts import ValidationFindingSeverity as Severity
from gamefactory.core.domain.internal_skin_contract import (
    InternalSkinContract,
    load_internal_skin_contract,
)


def _write_json(path: Path, value: dict[str, Any]) -> tuple[bytes, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    path.write_bytes(raw)
    return raw, sha256_bytes(raw)


def _manifest_entry(path: str, role: str, raw: bytes) -> dict[str, Any]:
    return {"path": path, "role": role, "size": len(raw), "sha256": sha256_bytes(raw)}


def bytes_with_crlf_line_endings(raw: bytes) -> bytes:
    """Normalize any EOL variant to LF, then encode CRLF (no double conversion)."""
    normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return normalized.replace(b"\n", b"\r\n")


def bind_bundled_source_declaration(
    template: dict[str, Any],
    contract_bytes: bytes,
) -> dict[str, Any]:
    """Copy the frozen template and bind raw/canonical digests to bundled contract bytes."""
    payload = dict(template)
    payload["contract_bytes_sha256"] = sha256_bytes(contract_bytes)
    payload["contract_canonical_sha256"] = contract_canonical_digest(contract_bytes)
    return payload


def bundled_source_declaration_bytes(
    template: dict[str, Any],
    contract_bytes: bytes,
) -> bytes:
    payload = bind_bundled_source_declaration(template, contract_bytes)
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _trusted_cold_verify(bundle_root: Path) -> None:
    verifier = Path(
        str(resources.files("gamefactory.resources.scripts").joinpath("verify_rig_bundle.py"))
    )
    proc = subprocess.run(
        [sys.executable, "-I", str(verifier), str(bundle_root.resolve())],
        capture_output=True,
        text=True,
        cwd=str(bundle_root.parent),
    )
    if proc.returncode != 0:
        first = proc.stdout.splitlines()[0] if proc.stdout else "FAILED"
        raise ValueError(f"staged bundle failed cold verification: {first}")


def _path_crosses_link(path: Path) -> bool:
    for current in [path, *path.parents]:
        junc = getattr(current, "is_junction", None)
        if current.is_symlink() or (junc is not None and junc()):
            return True
        if current.anchor == current:
            break
    return False


def export_rig_evidence_bundle(
    glb_path: Path,
    output_dir: Path,
    *,
    contract: InternalSkinContract | None = None,
    godot_executable: Path | None = None,
    appdata_dir: Path | None = None,
    bundle_id: str = "internal-rig-evidence",
) -> Path:
    """Validate GLB, optionally run Godot oracle, and write rig-evidence-0.8.0."""
    if not isinstance(bundle_id, str) or not bundle_id.strip():
        raise ValueError("bundle_id must be a non-empty string")
    glb_path = glb_path.resolve(strict=True)
    frozen = load_internal_skin_contract()
    if contract is not None and contract != frozen:
        raise ValueError("contract argument must match frozen internal skin contract")
    contract = frozen
    if output_dir.exists():
        raise ValueError("output_dir must not exist")
    if _path_crosses_link(output_dir.parent) or _path_crosses_link(output_dir):
        raise ValueError("output_dir path crosses a symlink or junction")
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    contract_src = Path(
        str(
            resources.files("gamefactory.resources.internal_skin").joinpath(
                "humanoid_12bone_contract.json"
            )
        )
    )
    blender_src = Path(
        str(
            resources.files("gamefactory.resources.blender").joinpath(
                "export_humanoid_12bone_fixture.py"
            )
        )
    )
    harness_src = Path(
        str(resources.files("gamefactory.resources.godot").joinpath("skin_deformation_harness.gd"))
    )
    source_decl_src = Path(
        str(
            resources.files("gamefactory.resources.internal_rig").joinpath(
                "source_declaration.json"
            )
        )
    )

    glb_bytes = glb_path.read_bytes()
    glb_sha256 = sha256_bytes(glb_bytes)
    contract_bytes = contract_src.read_bytes()
    contract_sha256 = sha256_bytes(contract_bytes)
    validation = validate_internal_skinned_glb(glb_path, contract)
    if validation.status != Severity.PASS:
        raise ValueError("cannot export evidence bundle for failing static validation")
    report_payload = validation.to_dict()
    report_payload["schema_version"] = "rig-validation-report-0.8.0"
    report_payload["glb_sha256"] = glb_sha256
    report_payload["contract_id"] = contract.contract_id
    report_payload["validator_contract_version"] = VALIDATOR_CONTRACT_VERSION
    report_payload["contract_canonical_sha256"] = contract_canonical_digest(contract_bytes)
    report_payload["contract_sha256"] = contract_sha256
    harness_reviewed, _ = reviewed_text_sha256(harness_src)
    blender_reviewed, _ = reviewed_text_sha256(blender_src)

    with tempfile.TemporaryDirectory(
        prefix=f".{output_dir.name}.export-stage-",
        dir=output_dir.parent,
    ) as stage_root_str:
        stage_root = Path(stage_root_str)
        bundle_root = stage_root / "bundle"
        bundle_root.mkdir()

        files: list[dict[str, Any]] = []
        (bundle_root / "evidence").mkdir(exist_ok=True)
        glb_rel = "evidence/skinned.glb"
        (bundle_root / glb_rel).write_bytes(glb_bytes)
        files.append(_manifest_entry(glb_rel, "skinned_glb", glb_bytes))

        contract_rel = "evidence/rig_verification_contract.json"
        contract_out = bundle_root / contract_rel
        contract_out.write_bytes(contract_bytes)
        files.append(_manifest_entry(contract_rel, "rig_verification_contract", contract_bytes))

        report_rel = "evidence/rig_validation_report.json"
        report_raw, _ = _write_json(bundle_root / report_rel, report_payload)
        files.append(_manifest_entry(report_rel, "rig_validation_report", report_raw))

        blender_rel = "reviewed/export_humanoid_12bone_fixture.py"
        blender_bytes = blender_src.read_bytes()
        (bundle_root / blender_rel).parent.mkdir(parents=True, exist_ok=True)
        (bundle_root / blender_rel).write_bytes(blender_bytes)
        files.append(_manifest_entry(blender_rel, "reviewed_blender_export_script", blender_bytes))

        harness_rel = "reviewed/skin_deformation_harness.gd"
        harness_bytes = harness_src.read_bytes()
        (bundle_root / harness_rel).parent.mkdir(parents=True, exist_ok=True)
        (bundle_root / harness_rel).write_bytes(harness_bytes)
        files.append(_manifest_entry(harness_rel, "reviewed_godot_harness", harness_bytes))

        source_decl_template = json.loads(source_decl_src.read_bytes().decode("utf-8"))
        source_decl_bytes = bundled_source_declaration_bytes(source_decl_template, contract_bytes)
        source_rel = "evidence/source_declaration.json"
        (bundle_root / source_rel).write_bytes(source_decl_bytes)
        files.append(_manifest_entry(source_rel, "source_declaration", source_decl_bytes))

        request_payload: dict[str, Any] | None = None
        observation_payload: dict[str, Any] | None = None
        runtime_status: str | None = None
        if godot_executable is not None:
            oracle_out = (bundle_root / "_oracle_stage").resolve()
            if oracle_out.parent != bundle_root.resolve():
                raise ValueError("oracle stage must be inside bundle export directory")
            try:
                observation_payload = run_skin_deformation_oracle(
                    godot_executable,
                    glb_path,
                    contract,
                    oracle_out,
                    contract_sha256=contract_sha256,
                    harness_sha256=harness_reviewed,
                    appdata_dir=appdata_dir,
                )
                request_path = Path(observation_payload["stage_dir"]) / "request.json"
                request_payload = json.loads(request_path.read_text(encoding="utf-8"))
                for key in ("glb", "output_path"):
                    request_payload.pop(key, None)
                verify_oracle_payload(
                    observation_payload,
                    contract,
                    expect_pass=True,
                    request=request_payload,
                    glb_sha256=glb_sha256,
                    contract_sha256=contract_sha256,
                    harness_sha256=harness_reviewed,
                )
                for transient in (
                    "process_exit_code",
                    "import_exit_code",
                    "stage_dir",
                    "glb",
                    "output_path",
                ):
                    observation_payload.pop(transient, None)
                    request_payload.pop(transient, None)
                runtime_status = "PASS"
            finally:
                shutil.rmtree(oracle_out, ignore_errors=True)

            request_rel = "evidence/rig_runtime_request.json"
            request_raw, _ = _write_json(bundle_root / request_rel, request_payload)
            files.append(_manifest_entry(request_rel, "rig_runtime_request", request_raw))

            obs_rel = "evidence/rig_runtime_observation.json"
            obs_raw, _ = _write_json(bundle_root / obs_rel, observation_payload)
            files.append(_manifest_entry(obs_rel, "rig_runtime_observation", obs_raw))

        manifest = {
            "schema_version": "rig-evidence-0.8.0",
            "bundle_id": bundle_id,
            "contract_id": contract.contract_id,
            "glb_sha256": glb_sha256,
            "contract_sha256": contract_sha256,
            "harness_reviewed_sha256": harness_reviewed,
            "blender_script_reviewed_sha256": blender_reviewed,
            "validation_status": report_payload["status"],
            "runtime_status": runtime_status,
            "runtime_request_digest": (
                runtime_request_digest(request_payload) if request_payload is not None else None
            ),
            "files": sorted(files, key=lambda item: item["path"]),
        }
        _write_json(bundle_root / "manifest.json", manifest)
        if godot_executable is not None:
            _trusted_cold_verify(bundle_root)
        if output_dir.exists():
            raise ValueError("output_dir must not exist")
        os.replace(bundle_root, output_dir)
    return output_dir
