"""Unit tests for paid request snapshot domain model, serialization, and migration 0008."""

import copy
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.external.meshy_cli import resolve_paid_request
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    PaidRequestSnapshotRecord,
    PaidRequestSnapshotRepository,
    ProductionReadinessRecord,
    ProductionReadinessRepository,
)
from gamefactory.core.domain.errors import PaidRequestInvalidError
from gamefactory.core.domain.models import ApprovalRequest, ApprovalStatus, CostClass
from gamefactory.core.domain.paid_request import (
    PAID_REQUEST_SCHEMA,
    PaidRequestSnapshot,
    canonical_json,
    paid_request_sha256,
    validate_paid_request,
)

GOLDEN_SNAPSHOT_HASH = "4431868f6e2bc0f435cbb1748daaeaa606a6df086f563b19283714a4a5409246"


def _make_valid_snapshot_dict() -> dict[str, Any]:
    binding = {
        "asset_id": "prop_test_01",
        "revision_number": 1,
        "concept_version": 1,
        "concept_sha256": "a" * 64,
        "specification_sha256": "b" * 64,
        "profile_id": "static_prop",
        "profile_version": 1,
    }
    cost = {
        "estimate": 5.0,
        "reservation": 5.0,
        "unit": "credits",
    }
    spec = {"geometry_budget": {"max_triangles_lod0": 10000}}
    return resolve_paid_request(binding, spec, cost)


def test_golden_snapshot_hash() -> None:
    snapshot = _make_valid_snapshot_dict()
    digest = paid_request_sha256(snapshot)
    assert digest == GOLDEN_SNAPSHOT_HASH


def test_canonical_json_stability_across_key_orderings() -> None:
    snap1 = _make_valid_snapshot_dict()
    # Reverse top-level and inner keys
    snap2 = {k: snap1[k] for k in reversed(list(snap1.keys()))}
    snap2["request"] = {k: snap1["request"][k] for k in reversed(list(snap1["request"].keys()))}
    snap2["binding"] = {k: snap1["binding"][k] for k in reversed(list(snap1["binding"].keys()))}

    assert canonical_json(snap1) == canonical_json(snap2)
    assert paid_request_sha256(snap1) == paid_request_sha256(snap2)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda s: s["request"].__setitem__("texture_resolution", "1k"),
        lambda s: s["request"].__setitem__("target_polycount", 8000),
        lambda s: s["request"].__setitem__("enable_pbr", False),
        lambda s: s["request"].__setitem__("model_type", "lowpoly"),
        lambda s: s["cost"].__setitem__("reservation", 10.0),
    ],
)
def test_mutating_material_request_fields_changes_hash(mutator: Any) -> None:
    base = _make_valid_snapshot_dict()
    base_hash = paid_request_sha256(base)

    mutated = copy.deepcopy(base)
    mutator(mutated)
    mutated_hash = paid_request_sha256(mutated)

    assert mutated_hash != base_hash


def test_snapshot_rejects_timestamps_and_report_paths() -> None:
    base = _make_valid_snapshot_dict()

    with_timestamp = copy.deepcopy(base)
    with_timestamp["timestamp"] = "2026-09-29T12:00:00Z"
    with pytest.raises(PaidRequestInvalidError):
        validate_paid_request(with_timestamp)

    with_created_at = copy.deepcopy(base)
    with_created_at["request"]["created_at"] = "2026-09-29T12:00:00Z"
    with pytest.raises(PaidRequestInvalidError):
        validate_paid_request(with_created_at)

    with_report_path = copy.deepcopy(base)
    with_report_path["report_path"] = "evidence/readiness.json"
    with pytest.raises(PaidRequestInvalidError):
        validate_paid_request(with_report_path)


@pytest.mark.parametrize(
    "abs_path",
    [
        "/tmp/model.glb",
        "C:\\Users\\model.glb",
        "C:/Users/model.glb",
        "\\\\server\\share\\model.glb",
    ],
)
def test_snapshot_rejects_absolute_path_values(abs_path: str) -> None:
    base = _make_valid_snapshot_dict()
    base["request"]["image_enhancement"] = abs_path
    with pytest.raises(PaidRequestInvalidError, match="Absolute filesystem path"):
        validate_paid_request(base)


def test_snapshot_rejects_credentials_in_keys_and_values(monkeypatch: pytest.MonkeyPatch) -> None:
    base = _make_valid_snapshot_dict()

    # Disallowed key containing "api_key"
    with_key = copy.deepcopy(base)
    with_key["request"]["api_key"] = "test-val"
    with pytest.raises(PaidRequestInvalidError, match="Disallowed credential-like key"):
        validate_paid_request(with_key)

    # Disallowed secret present in env
    fake_token = "synthetic_long_secret_token_1234567890_abc"
    monkeypatch.setenv("MESHY_TEST_API_KEY", fake_token)

    with_secret = copy.deepcopy(base)
    with_secret["request"]["image_enhancement"] = fake_token
    with pytest.raises(PaidRequestInvalidError, match="Credential or secret-like value detected"):
        validate_paid_request(with_secret)

    # resolve_paid_request never contains env secrets
    resolved = _make_valid_snapshot_dict()
    assert fake_token not in canonical_json(resolved)


def test_paid_request_snapshot_immutability_and_load_verified() -> None:
    snap_dict = _make_valid_snapshot_dict()
    snapshot = PaidRequestSnapshot.from_content(snap_dict)

    # Modifying the returned content does not affect snapshot
    content1 = snapshot.content
    content1["request"]["target_polycount"] = 500
    assert snapshot.content["request"]["target_polycount"] == 10000

    # load_verified with valid text
    loaded = PaidRequestSnapshot.load_verified(snapshot.canonical_json, snapshot.sha256)
    assert loaded.sha256 == snapshot.sha256

    # load_verified with mismatched hash raises
    with pytest.raises(PaidRequestInvalidError, match="SHA-256 mismatch"):
        PaidRequestSnapshot.load_verified(snapshot.canonical_json, "f" * 64)

    # load_verified with non-canonical text raises
    non_canonical = snapshot.canonical_json.replace(",", ", ")
    with pytest.raises(PaidRequestInvalidError):
        PaidRequestSnapshot.load_verified(non_canonical, snapshot.sha256)


def test_migration_0008_fresh_db_and_immutability(tmp_path: Path) -> None:
    db = Database(tmp_path / "migration0008.db")
    runner = MigrationRunner(db)
    runner.apply_all()

    snap_repo = PaidRequestSnapshotRepository(db)
    rec = PaidRequestSnapshotRecord(
        id="snap-01",
        workflow_id="wf-01",
        task_id="task-01",
        asset_id="asset-01",
        revision_number=1,
        concept_version=1,
        schema_version=PAID_REQUEST_SCHEMA,
        snapshot_sha256=GOLDEN_SNAPSHOT_HASH,
        canonical_json='{"test": 1}',
        status="ACTIVE",
    )
    snap_repo.save(rec)

    active = snap_repo.get_active_for_workflow("wf-01")
    assert active is not None
    assert active.id == "snap-01"

    # UPDATE of canonical_json raises via trigger
    with pytest.raises(
        (sqlite3.IntegrityError, sqlite3.OperationalError),
        match="canonical_json and snapshot_sha256 are immutable",
    ):
        with db.transaction() as conn:
            conn.execute(
                "UPDATE paid_request_snapshots SET canonical_json = 'bad' WHERE id = 'snap-01';"
            )

    # UPDATE of status and superseded_at succeeds via mark_superseded
    superseded_count = snap_repo.mark_superseded(["snap-01"], at="2026-09-29T12:00:00Z")
    assert superseded_count == 1
    assert snap_repo.get_active_for_workflow("wf-01") is None

    # Readiness repo round trip
    readiness_repo = ProductionReadinessRepository(db)
    r_rec = ProductionReadinessRecord(
        id="rep-01",
        workflow_id="wf-01",
        task_id="task-01",
        snapshot_sha256=GOLDEN_SNAPSHOT_HASH,
        result="PASS",
        schema_version="production-readiness-0.6.0",
        report_json='{"status": "ok"}',
        report_sha256="c" * 64,
        status="ACTIVE",
    )
    readiness_repo.save(r_rec)
    active_rep = readiness_repo.get_active_for_workflow("wf-01")
    assert active_rep is not None
    assert active_rep.result == "PASS"

    with pytest.raises(
        (sqlite3.IntegrityError, sqlite3.OperationalError),
        match="report_json and report_sha256 are immutable",
    ):
        with db.transaction() as conn:
            conn.execute(
                "UPDATE production_readiness_reports SET report_json = 'bad' WHERE id = 'rep-01';"
            )


def test_approval_repository_round_trips_paid_request_snapshot_hash(tmp_path: Path) -> None:
    db = Database(tmp_path / "approvals_hash.db")
    runner = MigrationRunner(db)
    runner.apply_all()

    # Setup parent workflow and task
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, engine_type, root_path, created_at, updated_at) VALUES ('p-1', 'P1', 'godot', '.', '', '')"
        )
        conn.execute(
            "INSERT INTO workflows (id, project_id, name, status, created_at, updated_at) VALUES ('wf-1', 'p-1', 'W1', 'PENDING', '', '')"
        )
        conn.execute(
            "INSERT INTO tasks (id, workflow_id, name, task_type, cost_class, depends_on_json, status, parameters_json, created_at, updated_at) VALUES ('t-1', 'wf-1', 'T1', 'test', 'LOCAL', '[]', 'PENDING', '{}', '', '')"
        )

    app_repo = ApprovalRepository(db)
    app = ApprovalRequest(
        id="app-hash-01",
        workflow_id="wf-1",
        task_id="t-1",
        approval_type="paid_generation",
        status=ApprovalStatus.APPROVED,
        cost_class=CostClass.PAID,
        operation_hash="d" * 64,
        paid_request_snapshot_hash=GOLDEN_SNAPSHOT_HASH,
    )
    app_repo.save(app)

    loaded = app_repo.get("app-hash-01")
    assert loaded is not None
    assert loaded.paid_request_snapshot_hash == GOLDEN_SNAPSHOT_HASH

    # Older approval without the hash loads as None
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO approvals (id, workflow_id, task_id, approval_type, status, reason, cost_class, operation_hash, requested_at, artifact_ids_json)
            VALUES ('app-old-01', 'wf-1', 't-1', 'concept_review', 'PENDING', '', 'LOCAL', 'e', '', '[]');
            """
        )
    loaded_old = app_repo.get("app-old-01")
    assert loaded_old is not None
    assert loaded_old.paid_request_snapshot_hash is None
