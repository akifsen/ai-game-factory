#!/usr/bin/env python3
"""V0.7 real-tool acceptance: assemblies, character capsule and provider isolation.

Drives the installed ``gamefactory`` CLI against a copy of a Godot fixture
project with real Blender and real Godot:

1. vehicle@1 authored in real Blender facing Blender's front, exported with the
   default glTF exporter (a +Z source), registered and assembled;
2. weapon@1 (-Z source) and aircraft@1 (+Z source) from deterministic fixtures;
3. a collapsed-pivot vehicle, which must fail validation with pivot.collapsed;
4. an assembly specification offered to the provider path, which must be
   rejected before any snapshot, approval or intent;
5. character@1 through the fake provider, real Blender (no collider mesh) and
   real Godot with a CapsuleShape3D.

Every completed bundle is cold-verified with ``python -I``. Approvals here are
TEST-ONLY decisions, never production human reviews. Real Meshy calls = 0.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
TEST_ACTOR = "TEST-ONLY-V07-ACCEPTANCE"


class AcceptanceFailure(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Driver:
    def __init__(self, args: argparse.Namespace, project: Path, report: dict[str, Any]) -> None:
        self.args = args
        self.project = project
        self.report = report

    def cli(self, *command: str, expected: set[int] | None = None) -> Any:
        full = [
            str(self.args.cli),
            "--json",
            "--project",
            str(self.project),
            "--godot-path",
            str(self.args.godot),
            "--blender-path",
            str(self.args.blender),
            *command,
        ]
        proc = subprocess.run(
            full,
            cwd=self.project.parent,
            capture_output=True,
            text=True,
            timeout=self.args.command_timeout,
            check=False,
        )
        record = {"command": command[:2], "exit": proc.returncode}
        self.report["commands"].append(record)
        allowed = expected if expected is not None else {0}
        if proc.returncode not in allowed:
            raise AcceptanceFailure(
                f"{' '.join(command[:3])} exited {proc.returncode}: {proc.stdout[-2000:]}"
                f"{proc.stderr[-2000:]}"
            )
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"raw": proc.stdout}

    def approve_pending(self, workflow_id: str, approval_type: str) -> str:
        approvals = self.cli("approvals", "--workflow", workflow_id)
        pending = [
            a
            for a in approvals
            if a.get("approval_type") == approval_type and a.get("status") == "PENDING"
        ]
        if len(pending) != 1:
            raise AcceptanceFailure(f"expected one pending {approval_type}: {approvals}")
        self.cli(
            "approve",
            pending[0]["id"],
            "--actor",
            TEST_ACTOR,
            "--comment",
            f"TEST ONLY: automated V0.7 acceptance decision for {approval_type}; "
            "not a production human review.",
        )
        return str(pending[0]["id"])

    def cold_verify(self, workflow_id: str) -> dict[str, Any]:
        exported = self.cli("report", "--workflow", workflow_id)
        bundle = Path(exported["bundle"])
        copy = self.project.parent / "cold" / workflow_id
        shutil.copytree(bundle, copy)
        proc = subprocess.run(
            [sys.executable, "-I", str(copy / "verify_asset_bundle.py"), str(copy)],
            cwd=copy.parent,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            raise AcceptanceFailure(f"cold verification failed: {proc.stdout}{proc.stderr}")
        result = json.loads(proc.stdout)
        result["manifest"] = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
        return result

    def db_count(self, table: str, workflow_id: str | None = None) -> int:
        db = self.project / ".gamefactory" / "state" / "factory.db"
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            if workflow_id is None:
                return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            return int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE workflow_id = ?", (workflow_id,)
                ).fetchone()[0]
            )


def _write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _assembly(
    driver: Driver,
    inputs: Path,
    name: str,
    spec: dict[str, Any],
    source: Path,
    source_front: str,
    tool: tuple[str, str],
) -> dict[str, Any]:
    spec_path = _write_json(inputs / f"{name}.spec.json", spec)
    registration = inputs / f"{name}.registration.json"
    driver.cli(
        "asset",
        "register-source",
        "--spec",
        str(spec_path),
        "--source",
        str(source),
        f"--source-front={source_front}",
        "--authoring-tool",
        tool[0],
        "--authoring-tool-version",
        tool[1],
        "--actor",
        TEST_ACTOR,
        "--reason",
        f"V0.7 acceptance fixture {name}",
        "--output",
        str(registration),
    )
    created = driver.cli(
        "asset",
        "assemble",
        "--spec",
        str(spec_path),
        "--source",
        str(source),
        "--registration",
        str(registration),
        expected={3},
    )
    workflow_id = str(created["workflow_id"])
    if created.get("paid_provider_invocations") != 0:
        raise AcceptanceFailure(f"{name}: assembly reported provider activity")
    approval = driver.approve_pending(workflow_id, "final_visual_review")
    resumed = driver.cli("resume", workflow_id)
    if resumed.get("status") != "COMPLETED":
        raise AcceptanceFailure(f"{name}: workflow did not complete: {resumed}")
    verified = driver.cold_verify(workflow_id)
    manifest = verified.pop("manifest")
    if (
        verified.get("status") != "PASS"
        or verified.get("source_front") != source_front
        or manifest.get("paid") is not False
        or manifest.get("schema_version") != "asset-evidence-0.7.0"
    ):
        raise AcceptanceFailure(f"{name}: bundle is not a PASS 0.7.0 assembly: {verified}")
    intents = driver.db_count("provider_operation_intents", workflow_id)
    if intents != 0:
        raise AcceptanceFailure(f"{name}: assembly created provider intents")
    return {
        "workflow_id": workflow_id,
        "profile": manifest["profile_qualified"],
        "source_front": source_front,
        "authoring_tool": list(tool),
        "final_approval_id": approval,
        "review_views": manifest["review_views"],
        "rule_groups": manifest["validator"]["rule_groups"],
        "cold_verification": verified,
        "provider_intents": intents,
    }


def run(args: argparse.Namespace, report: dict[str, Any]) -> None:
    from PIL import Image

    from gamefactory.adapters.fakes import assembly_generator as ag

    workspace = Path(tempfile.mkdtemp(prefix="gamefactory-v07-", dir=args.workspace))
    report["workspace"] = str(workspace)
    project = workspace / "project"
    shutil.copytree(args.fixture, project, ignore=shutil.ignore_patterns(".godot", ".gamefactory"))
    inputs = project / ".acceptance-inputs"
    inputs.mkdir()
    driver = Driver(args, project, report)
    driver.cli("init")
    profiles = driver.cli("asset", "profiles")["asset_profiles"]
    report["checks"]["profiles"] = {row["qualified"]: row["status"] for row in profiles}

    # 1. vehicle@1 authored in real Blender (+Z source from the default exporter).
    design_path = _write_json(inputs / "vehicle.blender-design.json", ag.blender_design(ag.VEHICLE_TANK))
    blender_source = inputs / "vehicle_blender.glb"
    authored = subprocess.run(
        [
            str(args.blender),
            "--background",
            "--factory-startup",
            "--python",
            str(REPO / "scripts" / "blender_author_assembly.py"),
            "--",
            "--design",
            str(design_path),
            "--output",
            str(blender_source),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    lines = [line for line in authored.stdout.splitlines() if line.startswith("{")]
    if authored.returncode != 0 or not lines:
        raise AcceptanceFailure(f"Blender authoring failed: {authored.stdout}{authored.stderr}")
    blender_info = json.loads(lines[-1])
    report["checks"]["vehicle_blender_authored"] = _assembly(
        driver,
        inputs,
        "vehicle_blender",
        ag.assembly_spec(ag.VEHICLE_TANK),
        blender_source,
        "+Z",
        ("blender", str(blender_info["blender_version"])),
    )

    # 2. weapon@1 (-Z) and aircraft@1 (+Z) deterministic fixtures.
    for name, design, front in (
        ("weapon", ag.WEAPON_RIFLE, "-Z"),
        ("aircraft", ag.AIRCRAFT_TRAINER, "+Z"),
    ):
        source = inputs / f"{name}.glb"
        source.write_bytes(ag.assembly_glb(design, source_front=front))
        report["checks"][name] = _assembly(
            driver,
            inputs,
            name,
            ag.assembly_spec(design),
            source,
            front,
            ("gamefactory-fixture", "0.7.0"),
        )

    # 3. collapsed pivots fail validation (the V0.7 spike defect).
    collapsed_spec = ag.assembly_spec(ag.VEHICLE_TANK)
    collapsed_spec["asset_id"] = "vehicle_collapsed_test"
    collapsed_design = ag.AssemblyDesign(
        **{**ag.VEHICLE_TANK.__dict__, "asset_id": "vehicle_collapsed_test"}
    )
    collapsed_source = inputs / "vehicle_collapsed.glb"
    collapsed_source.write_bytes(ag.assembly_glb(collapsed_design, collapsed=True))
    spec_path = _write_json(inputs / "vehicle_collapsed.spec.json", collapsed_spec)
    registration = _write_json(
        inputs / "vehicle_collapsed.registration.json",
        ag.source_registration(collapsed_design, collapsed_source.read_bytes()),
    )
    failed = driver.cli(
        "asset",
        "assemble",
        "--spec",
        str(spec_path),
        "--source",
        str(collapsed_source),
        "--registration",
        str(registration),
        expected={2},
    )
    reports = sorted(
        (project / ".gamefactory/assets/vehicle_collapsed_test").rglob("validation-attempt-*.json")
    )
    findings = json.loads(reports[-1].read_text(encoding="utf-8"))["findings"] if reports else []
    failing = sorted({f["rule_id"] for f in findings if f["severity"] == "FAIL"})
    if failed.get("status") != "FAILED" or "pivot.collapsed" not in failing:
        raise AcceptanceFailure(f"collapsed pivots were not rejected: {failed} {failing}")
    report["checks"]["collapsed_pivot_rejected"] = {
        "workflow_id": failed.get("workflow_id"),
        "status": failed.get("status"),
        "failing_rules": failing,
    }

    # 4. An assembly specification never reaches the provider path.
    concept = inputs / "concept.png"
    Image.new("RGB", (64, 64), (40, 90, 150)).save(concept)
    provenance = _write_json(
        inputs / "concept-provenance.json",
        {
            "sha256": _sha256(concept),
            "source_type": "synthetic_acceptance_fixture",
            "created_at": datetime.now(UTC).isoformat(),
            "prompt": "Scripted synthetic concept for offline acceptance.",
            "model": {"id": "gamefactory-acceptance-fixture-v1"},
            "generation": {"provider": "scripted_fixture", "paid": False},
        },
    )
    before = driver.db_count("workflows")
    driver.cli(
        "asset",
        "create",
        "--spec",
        str(inputs / "weapon.spec.json"),
        "--concept",
        str(concept),
        "--provenance",
        str(provenance),
        "--provider",
        "fake",
        expected={1, 2},
    )
    if driver.db_count("workflows") != before or driver.db_count("provider_operation_intents"):
        raise AcceptanceFailure("assembly specification created provider-path state")
    report["checks"]["assembly_provider_binding_rejected"] = {
        "workflows_created": 0,
        "provider_intents": driver.db_count("provider_operation_intents"),
    }

    # 5. character@1: fake provider, real Blender (no collider mesh), capsule in Godot.
    character_spec = _write_json(
        inputs / "character.spec.json", ag.character_spec(ag.HUMANOID_CHARACTER)
    )
    created = driver.cli(
        "asset",
        "create",
        "--spec",
        str(character_spec),
        "--concept",
        str(concept),
        "--provenance",
        str(provenance),
        "--provider",
        "fake",
        expected={3},
    )
    workflow_id = str(created["workflow_id"])
    driver.approve_pending(workflow_id, "concept_review")
    driver.cli("resume", workflow_id, expected={3})
    driver.approve_pending(workflow_id, "paid_generation")
    driver.cli("resume", workflow_id, expected={3})
    driver.approve_pending(workflow_id, "final_visual_review")
    done = driver.cli("resume", workflow_id)
    if done.get("status") != "COMPLETED":
        raise AcceptanceFailure(f"character workflow did not complete: {done}")
    verified = driver.cold_verify(workflow_id)
    manifest = verified.pop("manifest")
    if (
        manifest.get("schema_version") != "asset-evidence-0.7.0"
        or manifest.get("source_kind") != "provider_generated"
        or manifest["validator"]["rule_groups"] != ["core", "single_mesh", "collider_capsule"]
    ):
        raise AcceptanceFailure(f"character bundle is not a 0.7.0 capsule bundle: {manifest}")
    report["checks"]["character_capsule"] = {
        "workflow_id": workflow_id,
        "cold_verification": verified,
        "rule_groups": manifest["validator"]["rule_groups"],
        "review_views": manifest["review_views"],
        "provider": "fake",
    }
    report["invariants"] = {
        "real_meshy_calls": 0,
        "assembly_provider_intents": sum(
            report["checks"][k]["provider_intents"]
            for k in ("vehicle_blender_authored", "weapon", "aircraft")
        ),
    }
    if not args.keep_workspace:
        shutil.rmtree(workspace, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--blender", type=Path, required=True)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, default=REPO / "examples" / "minimal-godot")
    parser.add_argument("--command-timeout", type=float, default=600.0)
    parser.add_argument("--keep-workspace", action="store_true")
    args = parser.parse_args(argv)
    for name in ("cli", "blender", "godot"):
        setattr(args, name, getattr(args, name).expanduser().resolve())
    args.workspace = args.workspace.expanduser().resolve()
    if args.output.exists():
        print(f"refusing to overwrite {args.output}", file=sys.stderr)
        return 2
    report: dict[str, Any] = {
        "schema_version": "v07-acceptance-1",
        "started_at": datetime.now(UTC).isoformat(),
        "status": "FAIL",
        "commands": [],
        "checks": {},
        "environment": {
            "python": sys.version.split()[0],
            "display": os.environ.get("DISPLAY"),
        },
    }
    try:
        run(args, report)
        report["status"] = "PASS"
    except Exception as exc:  # noqa: BLE001 - the report records any failure
        report["error"] = f"{type(exc).__name__}: {exc}"
    report["finished_at"] = datetime.now(UTC).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(args.output)}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
