"""V0.6 backward compatibility with V0.5/V0.5.1 workflows, fingerprints and databases."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import test_accounting_v06 as harness
from PIL import Image
from test_accounting_v06 import _decision, _setup_accounting_env

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MIGRATIONS, MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    CostLedgerRepository,
    TaskRepository,
)
from gamefactory.cli.main import main
from gamefactory.core.approvals.approval_service import compute_operation_hash
from gamefactory.core.domain.asset_contracts import parse_asset_specification
from gamefactory.core.domain.models import WorkflowStatus, utc_now_iso
from gamefactory.core.domain.production_receipt import read_production_receipt
from gamefactory.workflows.asset_evidence import export_asset_evidence_bundle
from gamefactory.workflows.handlers import RegisteredTaskHandler, TaskHandlerResult

LEGACY_CONCEPT_CONTEXT_KEYS = {
    "asset_id",
    "revision",
    "specification_hash",
    "artifacts",
    "style_constraints",
    "profile_id",
    "profile_version",
    "profile_qualified",
}


def _legacy_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    original = harness.create_asset_production_workflow

    def legacy(*args: Any, **kwargs: Any) -> Any:
        workflow, tasks = original(*args, **kwargs)
        removed = {f"{workflow.id}-PAID-REQUEST", f"{workflow.id}-READINESS"}
        kept = [task for task in tasks if task.id not in removed]
        for task in kept:
            task.parameters = {
                k: v
                for k, v in task.parameters.items()
                if k not in {"graph_version", "paid_reservation"}
            }
            if task.id.endswith("-PAID-GENERATION"):
                task.depends_on = [f"{workflow.id}-CONCEPT-REVIEW"]
        return workflow, kept

    monkeypatch.setattr(harness, "create_asset_production_workflow", legacy)


def _realistic_downstream(engine: Any, handlers: Any) -> None:
    """Stubs that satisfy the cold verifier's processing and runtime bindings."""

    def process(wf: Any, task: Any, execution: Any) -> TaskHandlerResult:
        raw = next(
            a
            for a in handlers.artifacts.list_by_workflow(wf.id)
            if a.artifact_type == "asset-raw-glb"
        )
        path = handlers._path(task, f"processed-attempt-{execution.attempt_number}.glb")
        path.write_bytes((handlers.root / raw.relative_path).read_bytes())
        report = handlers._path(task, f"processing-attempt-{execution.attempt_number}.json")
        report.write_text(
            json.dumps(
                {
                    "status": "SUCCESS",
                    "exit_code": 0,
                    "input_raw_glb_sha256": raw.content_hash,
                    "output_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "processing_script_sha256": hashlib.sha256(b"stub").hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        return TaskHandlerResult(
            1,
            "process stub",
            [
                handlers._register(wf, task, execution, "asset-processed-glb", path),
                handlers._register(wf, task, execution, "asset-processing-report", report),
            ],
        )

    def godot(wf: Any, task: Any, execution: Any) -> TaskHandlerResult:
        processed = next(
            a
            for a in handlers.artifacts.list_by_workflow(wf.id)
            if a.artifact_type == "asset-processed-glb"
        )
        dims = task.parameters["specification"]["dimensions"]
        path = handlers._path(task, f"runtime-{execution.id}.json")
        path.write_text(
            json.dumps(
                {
                    "status": "PASS",
                    "workflow_id": wf.id,
                    "revision": int(task.parameters["revision_number"]),
                    "asset_id": str(task.parameters["asset_id"]),
                    "execution_id": execution.id,
                    "attempt_number": execution.attempt_number,
                    "processed_glb_sha256": processed.content_hash,
                    "mesh_visible": True,
                    "collision_shape_present": True,
                    "physics_body_present": True,
                    "physics_ray_hit": True,
                    "area_present": False,
                    "errors": [],
                    "mesh_bounds": {"size": [dims["width_m"], dims["height_m"], dims["depth_m"]]},
                }
            ),
            encoding="utf-8",
        )
        ids = [handlers._register(wf, task, execution, "asset-runtime-observation", path)]
        for angle in ("front", "three_quarter", "side"):
            capture = handlers._path(task, f"{execution.id}-{angle}.png")
            Image.new("RGB", (2, 2), (20, 70, 140)).save(capture)
            ids.append(handlers._register(wf, task, execution, "asset-runtime-capture", capture))
        return TaskHandlerResult(1, "runtime stub", ids)

    for task_type, stub in (("asset_process", process), ("asset_godot", godot)):
        metadata = engine.handler_registry._handlers[task_type].metadata
        engine.handler_registry._handlers[task_type] = RegisteredTaskHandler(stub, metadata)


def test_legacy_concept_review_fingerprint_shape_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-V0.6 concept approvals must re-verify: their hashed context shape is frozen."""
    _legacy_graph(monkeypatch)
    engine, db, workflow_id, _, handlers = _setup_accounting_env(tmp_path)
    blocked = engine.run_workflow(workflow_id)
    approval = ApprovalRepository(db).get(blocked.pending_approval_id or "")
    assert approval is not None and approval.approval_type == "concept_review"
    task = TaskRepository(db).get(approval.task_id)
    workflow = engine.wf_repo.get(workflow_id)
    assert task is not None and workflow is not None

    context = handlers.concept_review_context(workflow, task)
    assert set(context) == LEGACY_CONCEPT_CONTEXT_KEYS
    by_type = {
        a.artifact_type: a.content_hash for a in handlers.artifacts.list_by_workflow(workflow_id)
    }
    assert context["artifacts"] == {
        key: by_type[key]
        for key in ("asset-specification", "asset-concept", "asset-concept-provenance")
    }
    inputs = engine.approval_inputs(workflow, task)
    assert compute_operation_hash(task.id, "concept_review", inputs) == approval.operation_hash


def test_legacy_workflow_exports_verifiable_050_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _legacy_graph(monkeypatch)
    engine, db, workflow_id, fake, handlers = _setup_accounting_env(tmp_path)
    _realistic_downstream(engine, handlers)
    concept = engine.run_workflow(workflow_id)
    _decision(engine, db, concept.pending_approval_id or "", approve=True)
    paid = engine.run_workflow(workflow_id)
    _decision(engine, db, paid.pending_approval_id or "", approve=True)
    final = engine.run_workflow(workflow_id)
    assert final.status == WorkflowStatus.BLOCKED
    assert fake.invocation_count == 1

    bundle = engine.project_root / "legacy-bundle"
    export_asset_evidence_bundle(engine.project_root, db, workflow_id, bundle)

    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "asset-evidence-0.5.0"
    assert not {"paid_request_snapshot", "production_readiness_report"} & {
        entry["role"] for entry in manifest["files"]
    }
    receipt = read_production_receipt(bundle / "evidence" / "production-receipt.json")
    assert receipt["schema_version"] == "production-receipt-0.5.0"
    verified = subprocess.run(
        [sys.executable, "-I", str(bundle / "verify_asset_bundle.py"), str(bundle)],
        capture_output=True,
        text=True,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr


def _schema6_database(root: Path) -> tuple[Database, dict[str, Any]]:
    """A V0.5.1-shaped database: completed asset workflow at migration version 6."""
    factory = root / ".gamefactory"
    (factory / "state").mkdir(parents=True)
    (factory / "locks").mkdir()
    (factory / "factory.yml").write_text(
        "schema_version: '0.1.0'\nproject:\n  id: legacy-project\n  name: Legacy\n"
        "  version: '0.1.0'\nengine:\n  type: godot\n",
        encoding="utf-8",
    )
    db = Database(factory / "state" / "factory.db")
    runner = MigrationRunner(db)
    workflow_id = "WF-ASSET-legacy01"
    suffixes = [
        ("PREPARE", "asset_prepare", "LOCAL"),
        ("CONCEPT-REVIEW", "asset_concept_review", "LOCAL"),
        ("PAID-GENERATION", "asset_paid_generation", "PAID"),
        ("PROCESS", "asset_process", "LOCAL"),
        ("VALIDATE", "asset_validate", "LOCAL"),
        ("GODOT", "asset_godot", "LOCAL"),
        ("FINAL-REVIEW", "asset_final_review", "LOCAL"),
        ("EVIDENCE", "record_evidence", "LOCAL"),
    ]
    now = utc_now_iso()
    spec = parse_asset_specification(harness.SPEC_PATH).model_dump(mode="json")
    assert spec["asset_id"] == "prop_energy_crate_01"
    with db.connect() as conn:
        runner.init_migration_table(conn)
        for version, name, migration in MIGRATIONS[:6]:
            migration(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, now),
            )
        conn.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-project", "Legacy", "godot", str(root), now, now),
        )
        conn.execute(
            "INSERT INTO workflows VALUES (?, ?, ?, ?, ?, ?)",
            (
                workflow_id,
                "legacy-project",
                "Asset production: prop_energy_crate_01 r001",
                "COMPLETED",
                now,
                now,
            ),
        )
        previous = None
        for suffix, task_type, cost_class in suffixes:
            params: dict[str, Any] = {
                "asset_id": "prop_energy_crate_01",
                "revision_number": 1,
                "specification": spec,
            }
            if task_type == "asset_paid_generation":
                params["cost"] = 20.0
            conn.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"{workflow_id}-{suffix}",
                    workflow_id,
                    suffix,
                    task_type,
                    cost_class,
                    json.dumps([previous] if previous else []),
                    "COMPLETED",
                    json.dumps(params),
                    1,
                    60.0,
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO executions (id, task_id, attempt_number, status, started_at, "
                "completed_at, cost, estimated_cost, cost_unit, retryable) "
                "VALUES (?, ?, 1, 'COMPLETED', ?, ?, ?, ?, 'credits', 0)",
                (
                    f"EXEC-{suffix}",
                    f"{workflow_id}-{suffix}",
                    now,
                    now,
                    20.0 if task_type == "asset_paid_generation" else 0.0,
                    20.0 if task_type == "asset_paid_generation" else 0.0,
                ),
            )
            previous = f"{workflow_id}-{suffix}"
        for approval_type, suffix in (
            ("concept_review", "CONCEPT-REVIEW"),
            ("paid_generation", "PAID-GENERATION"),
            ("final_visual_review", "FINAL-REVIEW"),
        ):
            conn.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, 'APPROVED', 'r', ?, ?, 'operator', NULL, ?, ?, '[]')",
                (
                    f"APP-{suffix}",
                    workflow_id,
                    f"{workflow_id}-{suffix}",
                    approval_type,
                    "PAID" if approval_type == "paid_generation" else "LOCAL",
                    hashlib.sha256(approval_type.encode()).hexdigest(),
                    now,
                    now,
                ),
            )
        concept_hash = hashlib.sha256(b"legacy-concept").hexdigest()
        conn.execute(
            "INSERT INTO artifacts VALUES (?, ?, ?, 'asset-concept', 'asset_prepare', ?, ?, 10, 'VERIFIED', ?)",
            (
                "ART-CONCEPT",
                workflow_id,
                f"{workflow_id}-PREPARE",
                "legacy/concept.png",
                concept_hash,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO evidences VALUES (?, ?, ?, 'handler:asset_prepare', 'ok', '{}', ?)",
            ("EVI-PREPARE", f"{workflow_id}-PREPARE", "EXEC-PREPARE", now),
        )
        conn.execute(
            "INSERT INTO asset_revisions (asset_id, revision_number, workflow_id, spec_hash, "
            "concept_hash, raw_glb_hash, processed_glb_hash, validation_report_hash, "
            "runtime_evidence_hashes_json, created_at, updated_at, profile_id, profile_version) "
            "VALUES (?, 1, ?, ?, ?, ?, ?, ?, '[]', ?, ?, 'static_prop', 1)",
            (
                "prop_energy_crate_01",
                workflow_id,
                "a" * 64,
                concept_hash,
                "b" * 64,
                "c" * 64,
                "d" * 64,
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO provider_operation_intents (id, workflow_id, task_id, asset_id, "
            "revision_number, provider, operation, concept_hash, request_fingerprint, approval_id, "
            "estimated_cost, actual_cost, cost_unit, external_task_id, status, created_at, updated_at) "
            "VALUES ('INTENT-LEGACY', ?, ?, 'prop_energy_crate_01', 1, 'meshy', 'image-to-3d', ?, ?, "
            "'APP-PAID-GENERATION', NULL, 15.0, 'credits', 'ext-legacy-1', 'SUCCEEDED', ?, ?)",
            (
                workflow_id,
                f"{workflow_id}-PAID-GENERATION",
                concept_hash,
                hashlib.sha256(b"paid_generation").hexdigest(),
                now,
                now,
            ),
        )
        conn.commit()
    return db, {"workflow_id": workflow_id, "concept_hash": concept_hash}


def _dump(db: Database, table: str) -> list[tuple[Any, ...]]:
    conn = db.connect()
    try:
        return [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
    finally:
        conn.close()


def test_v051_database_migrates_to_v06_without_reopening_or_rewriting_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "legacy"
    db, facts = _schema6_database(root)
    before = {
        table: _dump(db, table)
        for table in ("workflows", "tasks", "executions", "approvals", "artifacts", "evidences")
    }
    intents_before = _dump(db, "provider_operation_intents")

    applied = MigrationRunner(db).apply_all()

    assert applied == len(MIGRATIONS) - 6
    for table, rows in before.items():
        after = _dump(db, table)
        if table == "approvals":
            # Migration 0008 appends a nullable column; historical values are intact.
            assert [row[:-1] for row in after] == rows
            assert all(row[-1] is None for row in after)
        else:
            assert after == rows, table
    assert [
        row[: len(intents_before[0])] for row in _dump(db, "provider_operation_intents")
    ] == intents_before
    tasks = TaskRepository(db).list_by_workflow(facts["workflow_id"])
    assert {task.status.value for task in tasks} == {"COMPLETED"}
    assert CostLedgerRepository(db).project_net("legacy-project") == 20.0

    assert main(["--project", str(root), "--json", "inspect", facts["workflow_id"]]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["workflow"]["status"] == "COMPLETED"

    assert main(["--project", str(root), "--json", "asset", "inspect", "prop_energy_crate_01"]) == 0
    asset = json.loads(capsys.readouterr().out)
    assert [v["version"] for v in asset["concept_versions"]] == [1]
    assert asset["concept_versions"][0]["sha256"] == facts["concept_hash"]

    assert (
        main(["--project", str(root), "--json", "recovery", "inspect", facts["workflow_id"]]) == 0
    )
    capsys.readouterr()
    assert _dump(db, "tasks") == before["tasks"]  # read-only commands reopened nothing


def test_v05_production_receipt_and_v04_manifest_remain_readable() -> None:
    v05 = {
        "schema_version": "production-receipt-0.5.0",
        "asset_id": "pickup_energy_cell_01",
        "revision": 2,
        "profile_id": "pickup",
        "profile_version": 1,
        "spec_hash": "a" * 64,
        "concept_hash": "b" * 64,
        "provider_request_fingerprint": "c" * 64,
        "provider_task_id": "01a0e9c1-f324-75f4-ab7c-59ee3b6c541d",
        "cost": 15.0,
        "raw_artifact_hash": "d" * 64,
        "processed_artifact_hash": "e" * 64,
        "validation_hash": "f" * 64,
        "runtime_hash": "1" * 64,
        "render_hashes": {"front": "2" * 64},
        "approval_ids": {"final_approval": "APP-e4a33beb"},
        "completed_at": "2026-09-28T22:59:00+00:00",
    }
    assert read_production_receipt(v05)["asset_id"] == "pickup_energy_cell_01"
    v06_missing = {**v05, "schema_version": "production-receipt-0.6.0"}
    with pytest.raises(Exception, match="paid_request_snapshot_sha256"):
        read_production_receipt(v06_missing)
