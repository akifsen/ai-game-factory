"""Integration tests for SQLite persistence, migrations, FK constraints, and attempt history."""

import multiprocessing
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import gamefactory.adapters.persistence.migrations as migrations_module
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MIGRATIONS, MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AuditLogRepository,
    EvidenceRepository,
    ExecutionRepository,
    ProjectRepository,
    QualityGateRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.domain.errors import ConfigurationError
from gamefactory.core.domain.models import (
    ApprovalRequest,
    ApprovalStatus,
    Artifact,
    AuditEvent,
    CostClass,
    Evidence,
    Execution,
    ExecutionStatus,
    GateStatus,
    Project,
    QualityGate,
    Task,
    Workflow,
    WorkflowStatus,
)


def _apply_migrations_concurrently(db_path: str, barrier: Any, results: Any) -> None:
    barrier.wait(timeout=10)
    try:
        results.put(MigrationRunner(Database(db_path)).apply_all())
    except Exception as exc:
        results.put(("error", str(exc)))


class TestPersistence:
    def setup_method(self) -> None:
        pass

    def test_migrations_and_version_tracking(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "factory.db")
        runner = MigrationRunner(db)

        # Initial run applies all migrations known to this version.
        applied = runner.apply_all()
        assert applied == len(MIGRATIONS)

        # Second run is idempotent
        applied_second = runner.apply_all()
        assert applied_second == 0

        # Verify version in schema_migrations
        with db.connect() as conn:
            versions = runner.get_applied_versions(conn)
            assert 1 in versions

    def test_future_schema_version_rejected(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "factory.db")
        runner = MigrationRunner(db)
        runner.apply_all()

        # Artificially insert a future version (e.g. 999)
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO schema_migrations VALUES (999, 'future_migration', '2030-01-01');"
            )
            conn.commit()

        # Running migrations should raise ConfigurationError
        with pytest.raises(ConfigurationError, match="newer than application supported version"):
            runner.apply_all()

    def test_gapped_migration_history_is_rejected(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "factory.db")
        runner = MigrationRunner(db)
        runner.apply_all()
        with db.transaction() as conn:
            conn.execute("DELETE FROM schema_migrations WHERE version = 2")
        with pytest.raises(ConfigurationError, match="history contains a gap"):
            runner.apply_all()

    def test_migration_failure_rolls_back_ddl_and_version_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = Database(tmp_path / "factory.db")

        def fail_after_ddl(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE rollback_probe (id INTEGER PRIMARY KEY)")
            raise RuntimeError("injected migration failure")

        monkeypatch.setattr(
            migrations_module,
            "MIGRATIONS",
            [
                (1, "0001_initial_schema", migrations_module._migration_0001_initial),
                (2, "0002_failure_probe", fail_after_ddl),
            ],
        )
        with pytest.raises(RuntimeError, match="injected migration failure"):
            MigrationRunner(db).apply_all()

        with db.connect() as conn:
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert "schema_migrations" not in tables
        assert "projects" not in tables
        assert "rollback_probe" not in tables

    def test_concurrent_fresh_migrations_apply_once(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "factory.db")
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        results = ctx.Queue()
        processes = [
            ctx.Process(target=_apply_migrations_concurrently, args=(db_path, barrier, results))
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        counts = [results.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
        assert sorted(counts) == [0, len(MIGRATIONS)]

        with Database(db_path).connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(
                MIGRATIONS
            )

    def test_foreign_key_enforcement(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "factory.db")
        MigrationRunner(db).apply_all()

        # Attempt to insert a workflow for a non-existent project
        wf_repo = WorkflowRepository(db)
        orphan_wf = Workflow(id="WF-ORPHAN", project_id="NON_EXISTENT", name="Orphan")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            wf_repo.save(orphan_wf)

    def test_cascade_delete(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "factory.db")
        MigrationRunner(db).apply_all()

        proj_repo = ProjectRepository(db)
        wf_repo = WorkflowRepository(db)
        task_repo = TaskRepository(db)

        proj = Project(id="P-1", name="Project 1", engine_type="godot", root_path="/test")
        proj_repo.save(proj)

        wf = Workflow(id="WF-1", project_id="P-1", name="WF 1")
        wf_repo.save(wf)

        task = Task(id="T-1", workflow_id="WF-1", name="Task 1", task_type="build")
        task_repo.save(task)

        assert wf_repo.get("WF-1") is not None
        assert task_repo.get("T-1") is not None

        # Delete project
        with db.transaction() as conn:
            conn.execute("DELETE FROM projects WHERE id = 'P-1';")

        # Cascaded delete
        assert wf_repo.get("WF-1") is None
        assert task_repo.get("T-1") is None

    def test_attempt_history_preservation(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "factory.db")
        MigrationRunner(db).apply_all()

        proj_repo = ProjectRepository(db)
        wf_repo = WorkflowRepository(db)
        task_repo = TaskRepository(db)
        exec_repo = ExecutionRepository(db)

        proj_repo.save(Project(id="P-1", name="P", engine_type="godot", root_path="/test"))
        wf_repo.save(Workflow(id="WF-1", project_id="P-1", name="WF"))
        task_repo.save(Task(id="T-1", workflow_id="WF-1", name="T", task_type="gen"))

        # Attempt 1: Failed
        exec1 = Execution(
            id="EXEC-1",
            task_id="T-1",
            attempt_number=1,
            status=ExecutionStatus.FAILED,
            error_message="Provider timeout",
        )
        exec_repo.save(exec1)

        # Attempt 2: Succeeded (Retry)
        exec2 = Execution(
            id="EXEC-2",
            task_id="T-1",
            attempt_number=2,
            status=ExecutionStatus.COMPLETED,
            stdout="Generated successfully",
        )
        exec_repo.save(exec2)

        # Query all attempts
        attempts = exec_repo.list_by_task("T-1")
        assert len(attempts) == 2
        assert attempts[0].attempt_number == 1
        assert attempts[0].status == ExecutionStatus.FAILED
        assert attempts[1].attempt_number == 2
        assert attempts[1].status == ExecutionStatus.COMPLETED

        # Latest attempt query
        latest = exec_repo.get_latest_attempt("T-1")
        assert latest is not None
        assert latest.attempt_number == 2
        assert latest.status == ExecutionStatus.COMPLETED

    def test_reopen_database_preserves_records(self, tmp_path: Path) -> None:
        db_file = tmp_path / "factory.db"
        db1 = Database(db_file)
        MigrationRunner(db1).apply_all()

        proj_repo1 = ProjectRepository(db1)
        proj_repo1.save(
            Project(id="P-PERSIST", name="Persist Test", engine_type="godot", root_path="/root")
        )

        # Reopen with new Database instance
        db2 = Database(db_file)
        proj_repo2 = ProjectRepository(db2)
        loaded = proj_repo2.get("P-PERSIST")
        assert loaded is not None
        assert loaded.name == "Persist Test"

    def test_full_entity_persistence_roundtrip(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "factory.db")
        MigrationRunner(db).apply_all()

        proj_repo = ProjectRepository(db)
        wf_repo = WorkflowRepository(db)
        task_repo = TaskRepository(db)
        exec_repo = ExecutionRepository(db)
        app_repo = ApprovalRepository(db)
        art_repo = ArtifactRepository(db)
        evi_repo = EvidenceRepository(db)
        gate_repo = QualityGateRepository(db)
        audit_repo = AuditLogRepository(db)

        # 1. Project & Workflow & Task
        proj_repo.save(Project(id="P-RT", name="Roundtrip", engine_type="godot", root_path="/p"))
        wf_repo.save(
            Workflow(id="WF-RT", project_id="P-RT", name="Full RT", status=WorkflowStatus.RUNNING)
        )
        task_repo.save(
            Task(
                id="T-RT",
                workflow_id="WF-RT",
                name="Generate Character",
                task_type="3d_gen",
                cost_class=CostClass.PAID,
                parameters={"prompt": "warrior"},
            )
        )

        # 2. Execution
        exec_repo.save(
            Execution(
                id="EXEC-RT",
                task_id="T-RT",
                attempt_number=1,
                status=ExecutionStatus.COMPLETED,
                cost=5.0,
            )
        )

        # 3. Approval
        app_repo.save(
            ApprovalRequest(
                id="APP-RT",
                workflow_id="WF-RT",
                task_id="T-RT",
                approval_type="paid_generation",
                status=ApprovalStatus.APPROVED,
                actor="Lead",
                cost_class=CostClass.PAID,
            )
        )

        # 4. Artifact
        art_repo.save(
            Artifact(
                id="ART-RT",
                workflow_id="WF-RT",
                task_id="T-RT",
                artifact_type="model_3d",
                producer="FakeProvider",
                relative_path="models/warrior.glb",
                content_hash="abc123hash",
                file_size=1024,
            )
        )

        # 5. Evidence
        evi_repo.save(
            Evidence(
                id="EVI-RT",
                task_id="T-RT",
                execution_id="EXEC-RT",
                evidence_type="inspection",
                summary="Passed visual inspection",
            )
        )

        # 6. Quality Gate
        gate_repo.save(
            QualityGate(
                id="GATE-RT",
                task_id="T-RT",
                gate_type="asset_validation",
                status=GateStatus.PASSED,
            )
        )

        # 7. Audit Log
        audit_repo.append(
            AuditEvent(
                id="AUD-RT",
                entity_type="Task",
                entity_id="T-RT",
                action="COMPLETED",
                actor="System",
            )
        )

        # Verify all entities retrieved correctly
        assert wf_repo.get("WF-RT") is not None
        assert task_repo.get("T-RT") is not None
        assert exec_repo.get("EXEC-RT") is not None
        assert app_repo.get("APP-RT") is not None
        assert art_repo.get("ART-RT") is not None
        assert len(evi_repo.list_by_task("T-RT")) == 1
        assert len(gate_repo.list_by_task("T-RT")) == 1
        assert len(audit_repo.list_by_entity("Task", "T-RT")) == 1
