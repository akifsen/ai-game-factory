"""Read-only connections to an existing SQLite database.

Unlike :class:`Database`, this adapter never creates parent directories and
never changes the database journal mode. SQLite may still create its normal
WAL shared-memory sidecar while opening a live WAL database.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from gamefactory.adapters.persistence.database import Database


class ReadOnlyDatabase(Database):
    """Database-compatible reader for an already-existing SQLite file."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).resolve(strict=True)
        if not self.db_path.is_file():
            raise FileNotFoundError(f"Database file does not exist: {self.db_path}")

    def connect(self) -> sqlite3.Connection:
        """Open a URI-escaped ``mode=ro`` connection without changing journal mode."""
        conn = sqlite3.connect(
            f"{self.db_path.as_uri()}?mode=ro",
            uri=True,
            timeout=10.0,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Provide the same context interface while enforcing SQLite read-only mode."""
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
