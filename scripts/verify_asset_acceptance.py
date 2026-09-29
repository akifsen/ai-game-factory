#!/usr/bin/env python3
"""Run the V0.4 fake-provider asset workflow through installed CLI and local tools.

Every Factory interaction is a fresh process. The deterministic concept and 3D
provider are test fixtures; this script never invokes Meshy or another paid API.
The generated evidence bundle is copied to the requested output location and
cold-verified with ``python -I``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import traceback
import zlib
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AcceptanceFailure(RuntimeError):
    pass


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _write_concept_fixture(path: Path, variant: int = 1) -> None:
    """Draw a small deterministic crate icon without external image libraries.

    ``variant`` 2 is the revised concept used by the in-revision iteration check.
    """
    width = height = 256
    background = (25, 34, 48) if variant == 1 else (48, 30, 26)
    pixels = bytearray()
    for y in range(height):
        row = bytearray([0])  # PNG filter: None
        for x in range(width):
            color = background
            if 47 <= x < 209 and 51 <= y < 210:
                color = (77, 98, 124)
            if 56 <= x < 200 and 61 <= y < 201:
                color = (95, 121, 147)
            if 68 <= x < 188 and (y in range(76, 83) or y in range(184, 191)):
                color = (35, 215, 223)
            if 76 <= x < 180 and 112 <= y < 150:
                color = (43, 173, 190)
            if x in range(47, 57) or x in range(199, 209):
                if 51 <= y < 210:
                    color = (176, 190, 205)
            row.extend(color)
        pixels.extend(row)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(bytes(pixels), level=9))
        + _png_chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def _specification(asset_id: str) -> dict[str, Any]:
    return {
        "schema_version": "0.4.0",
        "asset_id": asset_id,
        "category": "prop",
        "profile": "static_prop",
        "intent": "Synthetic acceptance fixture for a stylized sci-fi energy crate",
        "dimensions": {"width_m": 1.2, "depth_m": 1.0, "height_m": 1.0},
        "orientation": {"up": "+Y", "front": "-Z"},
        "origin_policy": "bottom_center",
        "geometry_budget": {
            "max_triangles_lod0": 20000,
            "max_triangles_lod1": 10000,
            "lod_ratio": 0.5,
        },
        "material_budget": {"max_materials": 2},
        "texture_budget": {"max_dimension": 2048},
        "collider_policy": "box",
        "lod_policy": "lod0_lod1",
        "style_constraints": {
            "family": "stylized_scifi",
            "silhouette": "chunky",
            "readability": "high",
            "detail_density": "medium",
        },
        "target_engine": "godot",
        "target_import_path": f"assets/generated/props/{asset_id}/",
    }


def _invoke(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    expected_exit: int = 0,
) -> dict[str, Any] | list[Any]:
    try:
        process = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AcceptanceFailure(f"could not complete command {command!r}: {exc}") from exc
    record = {
        "argv": command,
        "cwd": str(cwd),
        "exit_code": process.returncode,
        "stdout": process.stdout.strip(),
        "stderr": process.stderr.strip(),
    }
    if process.returncode != expected_exit:
        raise AcceptanceFailure(
            f"command exited {process.returncode}, expected {expected_exit}: {record}"
        )
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise AcceptanceFailure(f"CLI did not emit JSON: {record}") from exc
    if not isinstance(payload, (dict, list)):
        raise AcceptanceFailure(f"CLI JSON root was not an object or list: {record}")
    record["json"] = payload
    return record


def _probe_tool(executable: Path, args: list[str], label: str) -> str:
    if not executable.is_file():
        raise AcceptanceFailure(f"{label} executable does not exist: {executable}")
    try:
        result = subprocess.run(
            [str(executable), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AcceptanceFailure(f"could not probe {label}: {exc}") from exc
    version = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0 or not version:
        raise AcceptanceFailure(
            f"{label} version probe failed (exit {result.returncode}): {version}"
        )
    return version.splitlines()[0]


def _parse_approval_id(payload: dict[str, Any], expected_type: str) -> str:
    value = payload.get("pending_approval_id")
    if isinstance(value, str) and value:
        return value
    raise AcceptanceFailure(
        f"workflow did not return the pending {expected_type} approval ID: {payload}"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _resolve_python(path: Path | str) -> Path:
    """Validate the interpreter without dereferencing its virtual-environment path."""
    expanded = Path(path).expanduser()
    expanded.resolve(strict=True)
    python = Path(os.path.abspath(str(expanded)))
    if not python.is_file():
        raise FileNotFoundError(f"Python interpreter not found or not a regular file: {python}")
    return python


def _run(args: argparse.Namespace, result: dict[str, Any]) -> None:
    workspace = args.workspace.expanduser().resolve()
    output = Path(os.path.abspath(args.output.expanduser()))
    if output.exists() or output.is_symlink():
        raise AcceptanceFailure(f"refusing to overwrite acceptance report: {output}")
    if not workspace.is_dir():
        raise AcceptanceFailure(f"workspace directory must exist: {workspace}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.cli is None and args.python is None:
        raise AcceptanceFailure("supply --cli or --python for the installed gamefactory package")
    if args.python is None:
        python = _resolve_python(sys.executable)
    else:
        python = _resolve_python(args.python)
        if not python.is_file():
            raise AcceptanceFailure(f"Python executable does not exist: {python}")
    if args.cli is not None:
        cli_base = [str(args.cli.expanduser().resolve())]
        if not Path(cli_base[0]).is_file():
            raise AcceptanceFailure(f"installed CLI executable does not exist: {cli_base[0]}")
    else:
        cli_base = [str(python), "-I", "-m", "gamefactory"]

    blender_version = _probe_tool(args.blender.expanduser().resolve(), ["--version"], "Blender")
    godot_version = _probe_tool(args.godot.expanduser().resolve(), ["--version"], "Godot")
    blender_tools: dict[str, Any] = {
        "path": str(args.blender.resolve()),
        "version": blender_version,
    }
    if "GAMEFACTORY_BLENDER_PYTHONPATH" in os.environ:
        blender_tools["blender_python_paths"] = os.environ["GAMEFACTORY_BLENDER_PYTHONPATH"]
    result["tools"] = {
        "blender": blender_tools,
        "godot": {"path": str(args.godot.resolve()), "version": godot_version},
        "python": str(python),
        "cli": str(args.cli.resolve()) if args.cli else "python -I -m gamefactory",
    }
    command_cwd = workspace
    workflow_id = ""
    bundle_copy: Path | None = None

    def factory(*command: str, expected_exit: int = 0) -> dict[str, Any] | list[Any]:
        full = [
            *cli_base,
            "--json",
            "--project",
            str(project),
            "--godot-path",
            str(args.godot.resolve()),
            "--blender-path",
            str(args.blender.resolve()),
            *command,
        ]
        record = _invoke(
            full, cwd=command_cwd, timeout=args.command_timeout, expected_exit=expected_exit
        )
        result["commands"].append(record)
        return record["json"]

    temporary_workspace = (
        nullcontext(tempfile.mkdtemp(prefix="gamefactory-asset-acceptance-", dir=workspace))
        if args.keep_workspace
        else tempfile.TemporaryDirectory(prefix="gamefactory-asset-acceptance-", dir=workspace)
    )
    with temporary_workspace as temp:
        temp = Path(temp)
        if args.keep_workspace:
            result["debug_workspace"] = str(temp)
        project = Path(temp) / "project"
        fixture_project = args.fixture.expanduser().resolve()
        if not fixture_project.is_dir():
            raise AcceptanceFailure(f"Godot fixture project does not exist: {fixture_project}")
        shutil.copytree(
            fixture_project,
            project,
            ignore=shutil.ignore_patterns(".godot", ".gamefactory"),
        )
        command_cwd = Path(temp)
        baseline = {
            file.relative_to(project).as_posix(): _sha256(file)
            for file in project.rglob("*")
            if file.is_file()
        }
        asset_id = "prop_acceptance_crate_01"
        spec_path = Path(temp) / "asset-spec.json"
        concept_path = project / ".acceptance-inputs" / "concept.png"
        provenance_path = project / ".acceptance-inputs" / "concept-provenance.json"
        spec_data = _specification(asset_id)
        spec_path.write_text(json.dumps(spec_data, indent=2), encoding="utf-8")
        _write_concept_fixture(concept_path)
        concept_digest = _sha256(concept_path)
        provenance = {
            "sha256": concept_digest,
            "source_type": "synthetic_acceptance_fixture",
            "created_at": datetime.now(UTC).isoformat(),
            "prompt": "Scripted synthetic icon for offline acceptance; not an AI-generated concept.",
            "model": {"id": "gamefactory-acceptance-fixture-v1"},
            "generation": {"provider": "scripted_fixture", "paid": False},
        }
        provenance_path.write_text(json.dumps(provenance, indent=2), encoding="utf-8")

        initialized = factory("init")
        result["checks"]["project_initialized"] = initialized

        create = factory(
            "asset-create",
            "--spec",
            str(spec_path),
            "--concept",
            str(concept_path),
            "--provenance",
            str(provenance_path),
            "--concept-source-type",
            "imported",
            "--provider",
            "fake",
            "--budget-reservation",
            "5",
            expected_exit=3,
        )
        workflow_id = str(create.get("workflow_id", ""))
        if not workflow_id:
            raise AcceptanceFailure(f"asset-create did not return workflow ID: {create}")
        result["workflow_id"] = workflow_id
        result["asset_id"] = asset_id
        result["concept_sha256"] = concept_digest
        if create.get("paid_provider_invocations", 0) != 0:
            raise AcceptanceFailure("provider ran before concept approval")

        def approve_gate(approval_id: str, gate_type: str) -> dict[str, Any]:
            approvals = factory("approvals", "--workflow", workflow_id)
            if not isinstance(approvals, list):
                raise AcceptanceFailure(f"approvals command did not return a list: {approvals}")
            matches = [
                item
                for item in approvals
                if item.get("id") == approval_id
                and item.get("approval_type") == gate_type
                and item.get("status") == "PENDING"
            ]
            if len(matches) != 1:
                raise AcceptanceFailure(
                    f"expected one pending {gate_type} approval {approval_id}: {approvals}"
                )
            return factory(
                "approve",
                approval_id,
                "--actor",
                "TEST-ONLY-ACCEPTANCE",
                "--comment",
                f"TEST ONLY: deterministic offline acceptance decision for {gate_type}; not a production human approval.",
            )

        first_concept_approval_id = _parse_approval_id(create, "concept review")

        # V0.6 in-revision concept iteration: request changes on concept v1, append a
        # revised concept v2 inside the same revision, and review v2 before any spend.
        factory(
            "request-changes",
            first_concept_approval_id,
            "--actor",
            "TEST-ONLY-ACCEPTANCE",
            "--comment",
            "TEST ONLY: request a revised concept to exercise in-revision iteration.",
        )
        blocked = factory("resume", workflow_id, expected_exit=3)
        if blocked.get("error_code") != "CONCEPT_REVISION_REQUIRED":
            raise AcceptanceFailure(f"changes requested did not block for revision: {blocked}")
        concept_v2_path = project / ".acceptance-inputs" / "concept-v2.png"
        provenance_v2_path = project / ".acceptance-inputs" / "concept-v2-provenance.json"
        _write_concept_fixture(concept_v2_path, variant=2)
        concept_digest = _sha256(concept_v2_path)
        provenance_v2_path.write_text(
            json.dumps({**provenance, "sha256": concept_digest}, indent=2), encoding="utf-8"
        )
        replaced = factory(
            "asset",
            "concept",
            "replace",
            "--workflow",
            workflow_id,
            "--concept",
            str(concept_v2_path),
            "--provenance",
            str(provenance_v2_path),
            "--actor",
            "TEST-ONLY-ACCEPTANCE",
            "--reason",
            "TEST ONLY: revised concept for in-revision iteration acceptance.",
        )
        if replaced.get("new_version") != 2 or replaced.get("new_concept_sha256") != concept_digest:
            raise AcceptanceFailure(f"concept replacement did not append version 2: {replaced}")
        result["checks"]["concept_iteration"] = {
            "old_version": replaced.get("old_version"),
            "new_version": replaced.get("new_version"),
            "new_concept_sha256": concept_digest,
        }
        concept_approval_id = str(replaced.get("new_approval_id", ""))
        approve_gate(concept_approval_id, "concept_review")
        concept_resume = factory("resume", workflow_id, expected_exit=3)
        if concept_resume.get("status") != "BLOCKED":
            raise AcceptanceFailure(
                f"workflow did not stop at paid-generation gate: {concept_resume}"
            )
        result["checks"]["concept_approval_gate"] = "PASS"
        after_concept_gate = factory("inspect", workflow_id)
        if not isinstance(after_concept_gate, dict):
            raise AcceptanceFailure(
                f"post-concept inspect did not return an object: {after_concept_gate}"
            )
        if after_concept_gate.get("paid_provider_invocations") != 0:
            raise AcceptanceFailure("paid provider invoked before paid-operation approval")

        paid_approval_id = _parse_approval_id(concept_resume, "paid generation")
        approve_gate(paid_approval_id, "paid_generation")
        processed = factory("resume", workflow_id, expected_exit=3)
        if processed.get("status") != "BLOCKED":
            raise AcceptanceFailure(f"workflow did not stop for final visual review: {processed}")
        result["checks"]["paid_fake_generation"] = "PASS"

        inspection = factory("inspect", workflow_id)
        invocations = inspection.get("paid_provider_invocations")
        if invocations != 1:
            raise AcceptanceFailure(
                f"expected exactly one durable fake-provider invocation: {inspection}"
            )
        result["paid_provider_invocations"] = invocations
        pending = inspection.get("approvals", [])
        final_rows = [
            item
            for item in pending
            if item.get("approval_type") == "final_visual_review"
            and item.get("status") == "PENDING"
        ]
        if len(final_rows) != 1:
            raise AcceptanceFailure(f"expected final_visual_review gate: {inspection}")
        approve_gate(str(final_rows[0]["id"]), "final_visual_review")
        completed = factory("resume", workflow_id)
        if completed.get("status") != "COMPLETED":
            raise AcceptanceFailure(
                f"workflow failed to complete after test-only final gate: {completed}"
            )
        result["checks"]["final_test_only_visual_gate"] = "PASS"

        repeated = factory("resume", workflow_id)
        if repeated.get("status") != "COMPLETED":
            raise AcceptanceFailure(f"completed resume changed terminal status: {repeated}")
        after_repeat = factory("inspect", workflow_id)
        if after_repeat.get("paid_provider_invocations") != 1:
            raise AcceptanceFailure("completed resume invoked the provider a second time")
        result["checks"]["completed_resume_is_idempotent"] = "PASS"

        exported = factory("report", "--workflow", workflow_id)
        bundle_path = Path(str(exported.get("bundle", ""))).resolve(strict=True)
        if not bundle_path.is_dir() or not (bundle_path / "manifest.json").is_file():
            raise AcceptanceFailure(f"report did not produce an evidence bundle: {exported}")
        manifest = json.loads((bundle_path / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("workflow_id") != workflow_id:
            raise AcceptanceFailure("evidence manifest belongs to another workflow")
        bundle_roles = {entry.get("role") for entry in manifest.get("files", [])}
        if (
            manifest.get("schema_version") != "asset-evidence-0.6.0"
            or not {
                "paid_request_snapshot",
                "production_readiness_report",
            }
            <= bundle_roles
        ):
            raise AcceptanceFailure(
                f"V0.6 bundle lacks the approved request snapshot or readiness report: {manifest.get('schema_version')}"
            )
        result["checks"]["paid_request_snapshot_bound"] = "PASS"
        bundle_copy = output.parent / f"{output.stem}.bundle"
        if bundle_copy.exists() or bundle_copy.is_symlink():
            raise AcceptanceFailure(f"refusing to overwrite evidence bundle: {bundle_copy}")
        shutil.copytree(bundle_path, bundle_copy)
        verifier = bundle_copy / "verify_asset_bundle.py"
        if not verifier.is_file():
            raise AcceptanceFailure("portable bundle does not include its cold verifier")
        cold = _invoke(
            [str(python), "-I", str(verifier), str(bundle_copy)],
            cwd=Path(temp),
            timeout=args.command_timeout,
        )
        result["commands"].append(cold)
        result["checks"]["cold_bundle_verification"] = cold["json"]
        result["bundle"] = str(bundle_copy)
        result["bundle_manifest_sha256"] = _sha256(bundle_copy / "manifest.json")
        provider_entry = next(
            item for item in manifest["files"] if item.get("role") == "provider_operation"
        )
        provider_record = json.loads(
            (bundle_copy / provider_entry["path"]).read_text(encoding="utf-8")
        )
        if (
            provider_record.get("provider") != "fake"
            or provider_record.get("status") != "SUCCEEDED"
            or provider_record.get("workflow_id") != workflow_id
            or provider_record.get("concept_sha256") != concept_digest
        ):
            raise AcceptanceFailure(
                f"provider-operation evidence is not bound to fake run: {provider_record}"
            )
        result["checks"]["provider_operation_record"] = provider_record
        result["bundle_files"] = manifest["files"]
        result["checks"]["source_project_files_unchanged"] = all(
            _sha256(project / relative) == digest for relative, digest in baseline.items()
        )
        if not result["checks"]["source_project_files_unchanged"]:
            raise AcceptanceFailure("asset workflow changed an original Godot fixture file")

        # Alter one copied evidence artifact and prove the cold verifier notices.
        processed_entry = next(
            item for item in manifest["files"] if item.get("role") == "processed_glb"
        )
        tamper_root = output.parent / f"{output.stem}.tampered-bundle"
        if tamper_root.exists() or tamper_root.is_symlink():
            raise AcceptanceFailure(f"refusing to overwrite tamper-test bundle: {tamper_root}")
        shutil.copytree(bundle_copy, tamper_root)
        target = tamper_root / processed_entry["path"]
        contents = bytearray(target.read_bytes())
        if not contents:
            raise AcceptanceFailure("processed GLB in evidence bundle is empty")
        contents[-1] ^= 1
        target.write_bytes(contents)
        tampered = subprocess.run(
            [str(python), "-I", str(tamper_root / "verify_asset_bundle.py"), str(tamper_root)],
            cwd=Path(temp),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.command_timeout,
            shell=False,
        )
        if tampered.returncode == 0:
            raise AcceptanceFailure("cold bundle verifier accepted mutated processed GLB bytes")
        result["checks"]["tampered_bundle_rejected"] = {
            "exit_code": tampered.returncode,
            "stdout": tampered.stdout.strip(),
            "stderr": tampered.stderr.strip(),
            "mutation": "one byte changed in a copied processed GLB",
        }

    result["status"] = "PASS"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, help="installed gamefactory executable")
    parser.add_argument(
        "--python", type=Path, help="Python hosting the installed gamefactory wheel"
    )
    parser.add_argument("--blender", type=Path, required=True)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, default=Path("examples/minimal-godot"))
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new JSON report path")
    parser.add_argument("--command-timeout", type=int, default=300)
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="retain the temporary project for debugging; evidence stays under --output",
    )
    args = parser.parse_args(argv)
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "RUNNING",
        "started_at": datetime.now(UTC).isoformat(),
        "commands": [],
        "checks": {},
        "paid_provider": "fake",
        "paid_provider_invocations": 0,
        "source_kind": "scripted synthetic concept fixture; no SDXL or external image provider claim",
    }
    try:
        if args.command_timeout < 10:
            raise AcceptanceFailure("--command-timeout must be at least 10 seconds")
        _run(args, result)
    except Exception as exc:
        result["status"] = "FAIL"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
    result.setdefault("finished_at", datetime.now(UTC).isoformat())
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists() or args.output.is_symlink():
            print(json.dumps({"status": "FAIL", "error": f"report already exists: {args.output}"}))
            return 1
        args.output.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        print(json.dumps({"status": "FAIL", "error": f"could not write output report: {exc}"}))
        return 1
    print(json.dumps({"status": result["status"], "report": str(args.output)}))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
