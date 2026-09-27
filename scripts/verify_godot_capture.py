"""Real rendered-capture acceptance. A fake runner is not a substitute.

The published landscape and portrait reviews stay pending. Any approval in this
script uses the actor pipeline-test and is not a human visual review.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AcceptanceFailure(AssertionError):
    pass


def _run(argv: list[str], cwd: Path, expected: set[int], timeout: float = 180.0) -> Any:
    proc = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
        check=False,
    )
    payload: Any = None
    if "--json" in argv and proc.stdout.strip():
        payload = json.loads(proc.stdout)
    if proc.returncode not in expected:
        raise AcceptanceFailure(
            f"exit {proc.returncode} expected {sorted(expected)}\n{proc.stdout}\n{proc.stderr}"
        )
    return payload


def _copy_review(cli: str, project: Path, workflow_id: str, destination: Path) -> Path:
    artifacts = _run(
        [cli, "--project", str(project), "--json", "artifacts", "--workflow", workflow_id],
        project,
        {0},
    )
    if not isinstance(artifacts, list):
        raise AcceptanceFailure("artifacts command did not return a list")
    pages = [item for item in artifacts if item.get("artifact_type") == "godot-review-html"]
    if len(pages) != 1:
        raise AcceptanceFailure(f"expected one review page, found {len(pages)}")
    relative = pages[0]["relative_path"]
    source = project / relative
    review = source.parent
    if destination.exists():
        raise AcceptanceFailure(f"publish path already exists: {destination}")
    shutil.copytree(review, destination)
    return destination / "index.html"


def _assert_published(
    project: Path, workflow_id: str, review_dir: Path, width: int, height: int
) -> None:
    from gamefactory.adapters.engines.godot_image import check_fixture_regions, decode_png

    candidates = [
        path
        for path in (project / ".gamefactory").rglob("validation-report.json")
        if workflow_id in path.parts
    ]
    if len(candidates) != 1:
        raise AcceptanceFailure(f"expected one validation report for {workflow_id}")
    payload = json.loads(candidates[0].read_text(encoding="utf-8"))
    if payload.get("status") != "PASS":
        raise AcceptanceFailure("published capture is not a technical PASS")
    if len(payload.get("images", [])) != 4:
        raise AcceptanceFailure("expected four checkpoint images")
    for item in payload["images"]:
        image = decode_png((review_dir / item["src"]).read_bytes(), width, height)
        findings = check_fixture_regions(image.image, item["state"])
        if any(row["status"] != "PASS" for row in findings):
            raise AcceptanceFailure(f"published ROI failed for {item['id']}: {findings}")


def _capture(cli: str, project: Path, godot: Path, scenario: str) -> dict[str, Any]:
    return _run(
        [
            cli,
            "--project",
            str(project),
            "--godot-path",
            str(godot),
            "--json",
            "run",
            "godot-capture",
            "--scenario",
            scenario,
        ],
        project,
        {3},
    )


def _prepare(fixture: Path, root: Path, cli: str) -> None:
    shutil.copytree(
        fixture,
        root,
        ignore=shutil.ignore_patterns(".godot", ".gamefactory", ".git", "__pycache__"),
    )
    _run([cli, "--project", str(root), "--json", "init"], root, {0})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--publish", type=Path, required=True)
    args = parser.parse_args()
    cli = str(args.cli.expanduser().resolve())
    fixture = args.fixture.expanduser().resolve()
    godot = args.godot.expanduser().resolve()
    workspace = args.workspace.expanduser().resolve()
    publish = args.publish.expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    publish.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema_version": "0.3.0",
        "started_at": datetime.now(UTC).isoformat(),
        "cli": cli,
        "godot": str(godot),
        "human_review": "PENDING",
        "test_actor_approval_is_not_human_review": True,
        "checks": {},
    }
    try:
        landscape = workspace / "landscape"
        _prepare(fixture, landscape, cli)
        doctor = _run([cli, "--project", str(landscape), "--json", "doctor"], landscape, {0})
        rendered = doctor["capabilities"]["engine.godot.rendered_capture"]
        if rendered["details"].get("runtime_renderer_probed") is not False:
            raise AcceptanceFailure("doctor probed a renderer")
        if rendered["status"] not in {"NOT_VERIFIED", "UNAVAILABLE", "MISCONFIGURED"}:
            raise AcceptanceFailure(f"unexpected rendered capability status {rendered['status']}")
        first = _capture(cli, landscape, godot, "visual-scenario.json")
        page = _copy_review(cli, landscape, first["workflow_id"], publish / "landscape")
        _assert_published(landscape, first["workflow_id"], publish / "landscape", 1280, 720)
        second = _capture(cli, landscape, godot, "visual-scenario.json")
        second_review = next(
            path.parent
            for path in (landscape / ".gamefactory").rglob("index.html")
            if second["workflow_id"] in path.parts
        )
        _assert_published(landscape, second["workflow_id"], second_review, 1280, 720)
        report["checks"]["landscape_pending"] = {
            "workflow_id": first["workflow_id"],
            "approval_id": first.get("pending_approval_id"),
            "repeat_workflow_id": second["workflow_id"],
            "review_html": str(page),
            "status": first["status"],
        }
        portrait_root = workspace / "portrait"
        _prepare(fixture, portrait_root, cli)
        portrait = _capture(cli, portrait_root, godot, "visual-scenario-portrait.json")
        portrait_page = _copy_review(
            cli, portrait_root, portrait["workflow_id"], publish / "portrait"
        )
        _assert_published(portrait_root, portrait["workflow_id"], publish / "portrait", 720, 1280)
        report["checks"]["portrait_pending"] = {
            "workflow_id": portrait["workflow_id"],
            "approval_id": portrait.get("pending_approval_id"),
            "review_html": str(portrait_page),
        }
        mutated = workspace / "mutation"
        _prepare(fixture, mutated, cli)
        scene = mutated / "visual_main.gd"
        original = scene.read_text(encoding="utf-8")
        replacement = original.replace(
            "var filled: float = float(layout.bar.size.x) * float(_player_hp) / 100.0",
            "var filled: float = float(layout.bar.size.x)",
            1,
        )
        if replacement == original:
            raise AcceptanceFailure("could not isolate the HUD mutation")
        scene.write_text(replacement, encoding="utf-8")
        failed = _run(
            [
                cli,
                "--project",
                str(mutated),
                "--godot-path",
                str(godot),
                "--json",
                "run",
                "godot-capture",
                "--scenario",
                "visual-scenario.json",
            ],
            mutated,
            {2},
        )
        mutation_artifacts = _run(
            [
                cli,
                "--project",
                str(mutated),
                "--json",
                "artifacts",
                "--workflow",
                failed["workflow_id"],
            ],
            mutated,
            {0},
        )
        validation = next(
            item
            for item in mutation_artifacts
            if item.get("artifact_type") == "godot-capture-validation"
        )
        validation_payload = json.loads(
            (mutated / validation["relative_path"]).read_text(encoding="utf-8")
        )
        if (
            validation_payload["state"]["status"] != "PASS"
            or validation_payload["status"] != "FAIL"
        ):
            raise AcceptanceFailure("HUD mutation did not separate state PASS from visual FAIL")
        report["checks"]["hud_mutation"] = {
            "workflow_id": failed.get("workflow_id"),
            "status": failed.get("status"),
            "state": "PASS",
            "visual": "FAIL",
            "isolated_copy": True,
        }
        approved_root = workspace / "test-actor"
        _prepare(fixture, approved_root, cli)
        blocked = _capture(cli, approved_root, godot, "visual-scenario.json")
        approval_id = blocked["pending_approval_id"]
        _run(
            [
                cli,
                "--project",
                str(approved_root),
                "--json",
                "approve",
                approval_id,
                "--actor",
                "pipeline-test",
                "--comment",
                "automated test actor, not a human visual review",
            ],
            approved_root,
            {0},
        )
        completed = _run(
            [cli, "--project", str(approved_root), "--json", "resume", blocked["workflow_id"]],
            approved_root,
            {0},
        )
        report["checks"]["test_actor_resume"] = {
            "workflow_id": blocked["workflow_id"],
            "status": completed["status"],
            "actor": "pipeline-test",
            "counts_as_human_review": False,
        }
        report["status"] = "PASS"
    except Exception as exc:
        report["status"] = "FAIL"
        report["error"] = str(exc)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 1
    report["completed_at"] = datetime.now(UTC).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
