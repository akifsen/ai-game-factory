#!/usr/bin/env python3
"""Offline profile fixtures through Blender, validation, Godot, and evidence.

The concepts and 3D provider are labeled TEST FIXTURE inputs. This script never
calls Meshy. It approves gates only with an explicit test-only actor.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import traceback
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FIXTURES = (
    ("energy_pickup_test.yml", "Area3D", ("front", "three_quarter", "top")),
    ("wall_panel_test.yml", "StaticBody3D", ("front", "side", "three_quarter")),
)


class FixtureFailure(RuntimeError):
    pass


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _write_concept(path: Path, kind: str) -> None:
    width = height = 256
    pixels = bytearray()
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            if kind == "pickup":
                color = (12, 24, 36)
                dx = x - 128
                dy = y - 128
                if dx * dx + dy * dy < 70 * 70:
                    color = (40, 220, 210)
                if dx * dx + dy * dy < 28 * 28:
                    color = (230, 255, 250)
            else:
                color = (18, 22, 28)
                if 40 <= x < 216 and 28 <= y < 228:
                    color = (90, 104, 122)
                if 48 <= x < 208 and (y % 32) < 3:
                    color = (180, 196, 210)
            row.extend(color)
        pixels.extend(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(bytes(pixels), level=9))
        + _png_chunk(b"IEND", b"")
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _invoke(command: list[str], cwd: Path, timeout: int, expected_exit: int) -> dict[str, Any]:
    process = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
        check=False,
    )
    if process.returncode != expected_exit:
        raise FixtureFailure(
            f"command exited {process.returncode}, expected {expected_exit}: "
            f"{command} stdout={process.stdout[-2000:]} stderr={process.stderr[-2000:]}"
        )
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise FixtureFailure(f"CLI did not emit JSON: {process.stdout[-500:]}") from exc
    if not isinstance(payload, (dict, list)):
        raise FixtureFailure("CLI JSON root was not an object or list")
    return payload


def _approve(factory, workflow_id: str, approval_id: str, gate_type: str) -> None:
    approvals = factory("approvals", "--workflow", workflow_id)
    matches = [
        item
        for item in approvals
        if item.get("id") == approval_id
        and item.get("approval_type") == gate_type
        and item.get("status") == "PENDING"
    ]
    if len(matches) != 1:
        raise FixtureFailure(f"expected one pending {gate_type} {approval_id}")
    factory(
        "approve",
        approval_id,
        "--actor",
        "TEST-ONLY-PROFILE-FIXTURE",
        "--comment",
        f"TEST FIXTURE ONLY: offline {gate_type} decision; not a production human approval.",
    )


def _run_one(
    factory,
    project: Path,
    temp: Path,
    fixture_name: str,
    physics_node: str,
    views: tuple[str, ...],
    python: Path,
    timeout: int,
) -> dict[str, Any]:
    spec_source = (
        Path(__file__).resolve().parents[1] / "src/gamefactory/resources/fixtures" / fixture_name
    )
    spec = json.loads(
        subprocess.run(
            [
                str(python),
                "-c",
                "import json,sys; from pathlib import Path; import yaml; "
                "print(json.dumps(yaml.safe_load(Path(sys.argv[1]).read_text(encoding='utf-8'))))",
                str(spec_source),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    if "TEST FIXTURE" not in spec["intent"]:
        raise FixtureFailure(f"{fixture_name} is not labeled TEST FIXTURE")
    spec_path = temp / f"{spec['asset_id']}-spec.json"
    concept_path = project / ".fixture-inputs" / f"{spec['asset_id']}-concept.png"
    provenance_path = project / ".fixture-inputs" / f"{spec['asset_id']}-provenance.json"
    spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    _write_concept(concept_path, "pickup" if spec["profile"] == "pickup" else "modular")
    digest = _sha256(concept_path)
    provenance_path.write_text(
        json.dumps(
            {
                "sha256": digest,
                "source_type": "synthetic_test_fixture",
                "created_at": datetime.now(UTC).isoformat(),
                "prompt": "Scripted TEST FIXTURE icon. Not an AI-generated production concept.",
                "model": {"id": "gamefactory-profile-fixture-v1"},
                "generation": {"provider": "scripted_fixture", "paid": False},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    created = factory(
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
    workflow_id = str(created["workflow_id"])
    if created.get("profile_version") != f"{spec['profile']}@1":
        raise FixtureFailure(f"create did not bind profile version: {created}")
    _approve(factory, workflow_id, str(created["pending_approval_id"]), "concept_review")
    paid_gate = factory("resume", workflow_id, expected_exit=3)
    _approve(factory, workflow_id, str(paid_gate["pending_approval_id"]), "paid_generation")
    factory("resume", workflow_id, expected_exit=3)
    inspection = factory("inspect", workflow_id)
    if inspection.get("paid_provider_invocations") != 1:
        raise FixtureFailure(f"expected one fake invocation: {inspection}")
    pending = [
        item
        for item in inspection.get("approvals", [])
        if item.get("approval_type") == "final_visual_review" and item.get("status") == "PENDING"
    ]
    if len(pending) != 1:
        raise FixtureFailure(f"final review gate missing: {inspection}")
    _approve(factory, workflow_id, str(pending[0]["id"]), "final_visual_review")
    completed = factory("resume", workflow_id)
    if completed.get("status") != "COMPLETED":
        raise FixtureFailure(f"fixture workflow did not complete: {completed}")
    inspected = factory("asset", "inspect", spec["asset_id"])
    if inspected.get("profile_version") != f"{spec['profile']}@1":
        raise FixtureFailure(f"inspect profile mismatch: {inspected}")
    if inspected.get("current_gate") != "COMPLETED":
        raise FixtureFailure(f"inspect gate mismatch: {inspected}")
    wrappers = list(project.glob(".gamefactory/scratch/**/asset_wrapper.tscn"))
    if not wrappers or not any(
        physics_node in path.read_text(encoding="utf-8") for path in wrappers
    ):
        raise FixtureFailure(f"{spec['asset_id']} scene does not contain {physics_node}")
    exported = factory("report", "--workflow", workflow_id)
    bundle = Path(str(exported["bundle"])).resolve(strict=True)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "asset-evidence-0.5.0":
        raise FixtureFailure(f"evidence schema is not 0.5.0: {manifest.get('schema_version')}")
    if tuple(manifest.get("review_views", [])) != views:
        raise FixtureFailure(f"review views {manifest.get('review_views')} != {views}")
    verifier = subprocess.run(
        [str(python), "-I", str(bundle / "verify_asset_bundle.py"), str(bundle)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if verifier.returncode != 0:
        raise FixtureFailure(
            f"cold verifier failed for {spec['asset_id']}: {verifier.stdout} {verifier.stderr}"
        )
    retained = temp.parent / "retained-bundles" / spec["asset_id"]
    if retained.exists():
        shutil.rmtree(retained)
    shutil.copytree(bundle, retained)
    return {
        "asset_id": spec["asset_id"],
        "profile": inspected["profile_version"],
        "workflow_id": workflow_id,
        "bundle": str(retained),
        "review_views": list(views),
        "physics_node": physics_node,
        "fake_invocations": 1,
        "cold_verifier": "PASS",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--blender", type=Path, required=True)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, default=Path("examples/minimal-godot"))
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--command-timeout", type=int, default=300)
    args = parser.parse_args(argv)
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "RUNNING",
        "started_at": datetime.now(UTC).isoformat(),
        "label": "TEST FIXTURE",
        "paid_meshy_calls": 0,
        "fixtures": [],
    }
    try:
        workspace = args.workspace.expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        python = Path(os.path.abspath(args.python))
        project_src = args.fixture.expanduser().resolve()
        temp = Path(tempfile.mkdtemp(prefix="profile-fixtures-", dir=workspace))
        result["workspace"] = str(temp)
        try:
            project = temp / "project"
            shutil.copytree(
                project_src, project, ignore=shutil.ignore_patterns(".godot", ".gamefactory")
            )

            def factory(*command: str, expected_exit: int = 0) -> dict[str, Any] | list[Any]:
                payload = _invoke(
                    [
                        str(python),
                        "-I",
                        "-m",
                        "gamefactory",
                        "--json",
                        "--project",
                        str(project),
                        "--godot-path",
                        str(args.godot.resolve()),
                        "--blender-path",
                        str(args.blender.resolve()),
                        *command,
                    ],
                    temp,
                    args.command_timeout,
                    expected_exit,
                )
                return payload

            factory("init")
            for fixture_name, physics_node, views in FIXTURES:
                result["fixtures"].append(
                    _run_one(
                        factory,
                        project,
                        temp,
                        fixture_name,
                        physics_node,
                        views,
                        python,
                        args.command_timeout,
                    )
                )
            database = project / ".gamefactory/state/factory.db"
            connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
            try:
                meshy = connection.execute(
                    "SELECT COUNT(*) FROM provider_operation_intents WHERE provider = 'meshy'"
                ).fetchone()[0]
            finally:
                connection.close()
            if meshy != 0:
                raise FixtureFailure(f"fixture run recorded {meshy} Meshy intents")
            shutil.rmtree(temp, ignore_errors=True)
            result["workspace"] = None
            result["status"] = "PASS"
            result["paid_meshy_calls"] = 0
        except Exception:
            logs = list(temp.glob("project/.gamefactory/scratch/**/godot-render.log"))
            if logs:
                result["godot_render_log"] = logs[-1].read_text(encoding="utf-8", errors="replace")[
                    -4000:
                ]
            observations = list(temp.glob("project/.gamefactory/scratch/**/observation.json"))
            if observations:
                result["godot_observation"] = observations[-1].read_text(
                    encoding="utf-8", errors="replace"
                )[-4000:]
            raise
    except Exception as exc:
        result["status"] = "FAIL"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
    result["finished_at"] = datetime.now(UTC).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "report": str(args.output)}))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
