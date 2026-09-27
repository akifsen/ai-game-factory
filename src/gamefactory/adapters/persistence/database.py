"""SQLite connection management and database initialization.

Enforces PRAGMA foreign_keys = ON, PRAGMA busy_timeout = 5000, and WAL mode.
"""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


class Database:
    """Manages SQLite connections with foreign keys and WAL mode."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        """Create and configure a new SQLite connection."""
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=10.0,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        conn.row_factory = sqlite3.Row
        # Enforce foreign key constraints strictly
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
        except sqlite3.OperationalError:
            # WAL mode may fail on certain network shares; fallback to default journal
            pass
        return conn

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager providing an atomic transaction block."""
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
