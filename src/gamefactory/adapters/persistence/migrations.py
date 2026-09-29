"""Versioned schema migrations for SQLite persistence.

Applies incremental DDL updates and records migration history in schema_migrations table.
"""

import sqlite3
from collections.abc import Callable

from gamefactory.adapters.persistence.database import Database
from gamefactory.core.domain.errors import ConfigurationError
from gamefactory.core.domain.models import utc_now_iso


def _migration_0001_initial(conn: sqlite3.Connection) -> None:
    """Initial schema for V0.1 core entities."""
    schema_sql = """
    CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        engine_type TEXT NOT NULL,
        root_path TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS workflows (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        name TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL,
        name TEXT NOT NULL,
        task_type TEXT NOT NULL,
        cost_class TEXT NOT NULL,
        depends_on_json TEXT NOT NULL,
        status TEXT NOT NULL,
        parameters_json TEXT NOT NULL,
        max_retries INTEGER NOT NULL DEFAULT 1,
        timeout_seconds REAL NOT NULL DEFAULT 60.0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS executions (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        attempt_number INTEGER NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        external_op_id TEXT,
        pid INTEGER,
        host TEXT,
        error_message TEXT,
        exit_code INTEGER,
        stdout TEXT,
        stderr TEXT,
        cost REAL NOT NULL DEFAULT 0.0,
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
        UNIQUE (task_id, attempt_number)
    );

    CREATE TABLE IF NOT EXISTS approvals (
        id TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        approval_type TEXT NOT NULL,
        status TEXT NOT NULL,
        reason TEXT NOT NULL,
        cost_class TEXT NOT NULL,
        operation_hash TEXT NOT NULL,
        actor TEXT,
        comment TEXT,
        requested_at TEXT NOT NULL,
        decided_at TEXT,
        artifact_ids_json TEXT NOT NULL,
        FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE,
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS artifacts (
        id TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        artifact_type TEXT NOT NULL,
        producer TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        file_size INTEGER NOT NULL,
        validation_state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE,
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS evidences (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        execution_id TEXT NOT NULL,
        evidence_type TEXT NOT NULL,
        summary TEXT NOT NULL,
        raw_data_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
        FOREIGN KEY (execution_id) REFERENCES executions(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS quality_gates (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        gate_type TEXT NOT NULL,
        status TEXT NOT NULL,
        evaluated_at TEXT,
        reason TEXT,
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS audit_events (
        id TEXT PRIMARY KEY,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        action TEXT NOT NULL,
        actor TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        previous_state TEXT,
        new_state TEXT,
        details_json TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_tasks_workflow ON tasks(workflow_id);
    CREATE INDEX IF NOT EXISTS idx_executions_task ON executions(task_id);
    CREATE INDEX IF NOT EXISTS idx_approvals_workflow ON approvals(workflow_id);
    CREATE INDEX IF NOT EXISTS idx_artifacts_workflow ON artifacts(workflow_id);
    CREATE INDEX IF NOT EXISTS idx_evidences_task ON evidences(task_id);
    CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id);
    """
    for statement in schema_sql.split(";"):
        if statement.strip():
            conn.execute(statement)


# Migration registry: version number -> (name, callable)
def _migration_0002_provider_invocations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS provider_invocations (id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, task_id TEXT NOT NULL, provider TEXT NOT NULL, operation_hash TEXT NOT NULL, invoked_at TEXT NOT NULL, FOREIGN KEY(workflow_id) REFERENCES workflows(id) ON DELETE CASCADE, FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE)"
    )


def _migration_0003_execution_cost_metadata(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE executions ADD COLUMN estimated_cost REAL NOT NULL DEFAULT 0.0")
    conn.execute(
        "ALTER TABLE executions ADD COLUMN cost_unit TEXT NOT NULL DEFAULT 'provider_units'"
    )
    conn.execute("ALTER TABLE executions ADD COLUMN provider TEXT")


def _migration_0004_execution_retry_classification(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE executions ADD COLUMN retryable INTEGER NOT NULL DEFAULT 0")


def _migration_0005_asset_operations_and_intent(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS asset_revisions (
            asset_id TEXT NOT NULL,
            revision_number INTEGER NOT NULL,
            workflow_id TEXT NOT NULL,
            spec_hash TEXT NOT NULL,
            concept_hash TEXT,
            raw_glb_hash TEXT,
            processed_glb_hash TEXT,
            validation_report_hash TEXT,
            runtime_evidence_hashes_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (asset_id, revision_number),
            FOREIGN KEY(workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_operation_intents (
            id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            task_id TEXT NOT NULL UNIQUE,
            asset_id TEXT NOT NULL,
            revision_number INTEGER NOT NULL,
            provider TEXT NOT NULL,
            operation TEXT NOT NULL,
            concept_hash TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL UNIQUE,
            approval_id TEXT NOT NULL,
            estimated_cost REAL,
            actual_cost REAL,
            cost_unit TEXT NOT NULL DEFAULT 'credits',
            external_task_id TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(workflow_id) REFERENCES workflows(id) ON DELETE CASCADE,
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_asset_revisions_asset ON asset_revisions(asset_id);"
    )
    conn.execute("DROP INDEX IF EXISTS idx_intents_task;")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_intents_task ON provider_operation_intents(task_id);"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_intents_fingerprint ON provider_operation_intents(request_fingerprint);"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_intents_external_task ON provider_operation_intents(external_task_id) WHERE external_task_id IS NOT NULL;"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_intents_workflow ON provider_operation_intents(workflow_id);"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_intents_asset_revision_provider_op ON provider_operation_intents(asset_id, revision_number, provider, operation);"
    )


def _migration_0006_asset_revision_profile(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE asset_revisions ADD COLUMN profile_id TEXT")
    conn.execute("ALTER TABLE asset_revisions ADD COLUMN profile_version INTEGER")


def _migration_0007_cost_ledger(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cost_ledger (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            workflow_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            execution_id TEXT,
            intent_id TEXT,
            request_fingerprint TEXT,
            entry_type TEXT NOT NULL CHECK(entry_type IN ('RESERVE','SETTLE','RELEASE','ADJUSTMENT')),
            amount REAL NOT NULL,
            cost_unit TEXT NOT NULL,
            reason TEXT NOT NULL,
            source TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK (entry_type = 'ADJUSTMENT' OR amount >= 0)
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cost_ledger_project ON cost_ledger(project_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cost_ledger_workflow ON cost_ledger(workflow_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cost_ledger_task ON cost_ledger(task_id);")
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_cost_ledger_no_update
        BEFORE UPDATE ON cost_ledger
        BEGIN
            SELECT RAISE(ABORT, 'cost_ledger is append-only: updates are forbidden');
        END;
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_cost_ledger_no_delete
        BEFORE DELETE ON cost_ledger
        BEGIN
            SELECT RAISE(ABORT, 'cost_ledger is append-only: deletes are forbidden');
        END;
        """
    )
    conn.execute(
        """
        INSERT INTO cost_ledger (
            id, project_id, workflow_id, task_id, execution_id, intent_id, request_fingerprint,
            entry_type, amount, cost_unit, reason, source, actor, created_at
        )
        SELECT
            'LEDGER-LEGACY-' || e.id,
            w.project_id,
            w.id,
            t.id,
            e.id,
            poi.id,
            poi.request_fingerprint,
            'RESERVE',
            e.cost,
            e.cost_unit,
            'Historical execution cost carried forward as a conservative hold',
            'legacy_execution_cost',
            'migration-0007',
            e.started_at
        FROM executions e
        JOIN tasks t ON t.id = e.task_id
        JOIN workflows w ON w.id = t.workflow_id
        LEFT JOIN provider_operation_intents poi ON poi.task_id = t.id
        WHERE e.cost > 0;
        """
    )


def _migration_0008_paid_request_and_readiness(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paid_request_snapshots (
            id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            asset_id TEXT NOT NULL,
            revision_number INTEGER NOT NULL,
            concept_version INTEGER NOT NULL,
            schema_version TEXT NOT NULL,
            snapshot_sha256 TEXT NOT NULL,
            canonical_json TEXT NOT NULL,
            artifact_id TEXT,
            status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'SUPERSEDED')),
            created_at TEXT NOT NULL,
            superseded_at TEXT
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_paid_request_snapshots_workflow ON paid_request_snapshots(workflow_id);"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS production_readiness_reports (
            id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            snapshot_sha256 TEXT NOT NULL,
            result TEXT NOT NULL CHECK(result IN ('PASS', 'FAIL')),
            schema_version TEXT NOT NULL,
            report_json TEXT NOT NULL,
            report_sha256 TEXT NOT NULL,
            artifact_id TEXT,
            status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'SUPERSEDED')),
            created_at TEXT NOT NULL,
            superseded_at TEXT
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_production_readiness_reports_workflow ON production_readiness_reports(workflow_id);"
    )
    conn.execute(
        "ALTER TABLE provider_operation_intents ADD COLUMN paid_request_snapshot_hash TEXT;"
    )
    conn.execute("ALTER TABLE approvals ADD COLUMN paid_request_snapshot_hash TEXT;")
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_paid_request_snapshots_immutable
        BEFORE UPDATE ON paid_request_snapshots
        BEGIN
            SELECT CASE
                WHEN OLD.canonical_json != NEW.canonical_json
                  OR OLD.snapshot_sha256 != NEW.snapshot_sha256
                  OR OLD.id != NEW.id
                  OR OLD.workflow_id != NEW.workflow_id
                  OR OLD.task_id != NEW.task_id
                  OR OLD.asset_id != NEW.asset_id
                  OR OLD.revision_number != NEW.revision_number
                  OR OLD.concept_version != NEW.concept_version
                  OR OLD.schema_version != NEW.schema_version
                  OR OLD.created_at != NEW.created_at
                THEN RAISE(ABORT, 'paid_request_snapshots: canonical_json and snapshot_sha256 are immutable')
            END;
        END;
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_production_readiness_reports_immutable
        BEFORE UPDATE ON production_readiness_reports
        BEGIN
            SELECT CASE
                WHEN OLD.report_json != NEW.report_json
                  OR OLD.report_sha256 != NEW.report_sha256
                  OR OLD.id != NEW.id
                  OR OLD.workflow_id != NEW.workflow_id
                  OR OLD.task_id != NEW.task_id
                  OR OLD.snapshot_sha256 != NEW.snapshot_sha256
                  OR OLD.result != NEW.result
                  OR OLD.schema_version != NEW.schema_version
                  OR OLD.created_at != NEW.created_at
                THEN RAISE(ABORT, 'production_readiness_reports: report_json and report_sha256 are immutable')
            END;
        END;
        """
    )


def _migration_0009_concept_versions(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS concept_versions (
            id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            asset_id TEXT NOT NULL,
            revision_number INTEGER NOT NULL,
            version INTEGER NOT NULL,
            artifact_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            provenance_artifact_id TEXT,
            provenance_hash TEXT,
            provenance_type TEXT,
            source_type TEXT,
            status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'SUPERSEDED')),
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            superseded_at TEXT,
            UNIQUE(asset_id, revision_number, version)
        );
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_concept_versions_active
        ON concept_versions(asset_id, revision_number)
        WHERE status = 'ACTIVE';
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_concept_versions_workflow ON concept_versions(workflow_id);"
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_concept_versions_update_restricted
        BEFORE UPDATE ON concept_versions
        BEGIN
            SELECT CASE
                WHEN OLD.id != NEW.id
                  OR OLD.workflow_id != NEW.workflow_id
                  OR OLD.asset_id != NEW.asset_id
                  OR OLD.revision_number != NEW.revision_number
                  OR OLD.version != NEW.version
                  OR OLD.artifact_id != NEW.artifact_id
                  OR OLD.content_hash != NEW.content_hash
                  OR OLD.provenance_artifact_id IS NOT NEW.provenance_artifact_id
                  OR OLD.provenance_hash IS NOT NEW.provenance_hash
                  OR OLD.provenance_type IS NOT NEW.provenance_type
                  OR OLD.source_type IS NOT NEW.source_type
                  OR OLD.actor != NEW.actor
                  OR OLD.reason != NEW.reason
                  OR OLD.created_at != NEW.created_at
                THEN RAISE(ABORT, 'concept_versions: only status and superseded_at may change on update')
            END;
        END;
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_concept_versions_no_delete
        BEFORE DELETE ON concept_versions
        BEGIN
            SELECT RAISE(ABORT, 'concept_versions: deletes are forbidden');
        END;
        """
    )


MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "0001_initial_schema", _migration_0001_initial),
    (2, "0002_provider_invocations", _migration_0002_provider_invocations),
    (3, "0003_execution_cost_metadata", _migration_0003_execution_cost_metadata),
    (4, "0004_execution_retry_classification", _migration_0004_execution_retry_classification),
    (5, "0005_asset_operations_and_intent", _migration_0005_asset_operations_and_intent),
    (6, "0006_asset_revision_profile", _migration_0006_asset_revision_profile),
    (7, "0007_cost_ledger", _migration_0007_cost_ledger),
    (8, "0008_paid_request_and_readiness", _migration_0008_paid_request_and_readiness),
    (9, "0009_concept_versions", _migration_0009_concept_versions),
]


class MigrationRunner:
    """Manages schema versions and executes pending migrations."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def init_migration_table(self, conn: sqlite3.Connection) -> None:
        """Create schema_migrations table if not present."""
        conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        """)

    def get_applied_versions(self, conn: sqlite3.Connection) -> set[int]:
        """Return the set of already applied migration versions."""
        self.init_migration_table(conn)
        cursor = conn.execute("SELECT version FROM schema_migrations ORDER BY version ASC;")
        return {row[0] for row in cursor.fetchall()}

    def apply_all(self) -> int:
        """Apply all pending migrations in order. Returns count of applied migrations."""
        applied_count = 0
        with self.db.transaction() as conn:
            # Reserve the writer slot before inspecting history so another process
            # cannot race the same pending version or run DDL concurrently.
            conn.execute("BEGIN IMMEDIATE")
            applied = self.get_applied_versions(conn)
            max_known = max((m[0] for m in MIGRATIONS), default=0)
            known_versions = [migration[0] for migration in MIGRATIONS]
            if known_versions != list(range(1, max_known + 1)):
                raise ConfigurationError(
                    "Application migration registry is not contiguous from version 1",
                    details={"known_versions": known_versions},
                )

            for v in applied:
                if v > max_known:
                    raise ConfigurationError(
                        f"Database schema version {v} is newer than application supported version {max_known}",
                        details={"database_version": v, "supported_version": max_known},
                    )

            if applied:
                highest_applied = max(applied)
                expected_history = set(range(1, highest_applied + 1))
                if applied != expected_history:
                    raise ConfigurationError(
                        "Database migration history contains a gap",
                        details={"applied_versions": sorted(applied)},
                    )

            for version, name, migration_fn in MIGRATIONS:
                if version not in applied:
                    migration_fn(conn)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?);",
                        (version, name, utc_now_iso()),
                    )
                    applied_count += 1

        return applied_count
