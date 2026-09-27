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


MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "0001_initial_schema", _migration_0001_initial),
    (2, "0002_provider_invocations", _migration_0002_provider_invocations),
    (3, "0003_execution_cost_metadata", _migration_0003_execution_cost_metadata),
    (4, "0004_execution_retry_classification", _migration_0004_execution_retry_classification),
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
