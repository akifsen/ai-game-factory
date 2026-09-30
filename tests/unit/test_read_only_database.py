"""Read-only SQLite adapter guarantees used by inspection/export commands."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.read_only_database import ReadOnlyDatabase


def test_reader_sees_committed_wal_data_and_rejects_mutations(tmp_path: Path) -> None:
    path = tmp_path / "factory.db"
    writer = Database(path).connect()
    try:
        writer.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        writer.execute("INSERT INTO sample VALUES ('visible')")
        writer.commit()
        database_bytes = path.read_bytes()

        reader = ReadOnlyDatabase(path).connect()
        try:
            assert reader.execute("SELECT value FROM sample").fetchone()["value"] == "visible"
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader.execute("INSERT INTO sample VALUES ('forbidden')")
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader.execute("CREATE TABLE forbidden (value TEXT)")
        finally:
            reader.close()
        assert path.read_bytes() == database_bytes
    finally:
        writer.close()


def test_reader_escapes_uri_metacharacters_in_database_filename(tmp_path: Path) -> None:
    # '#' and '%' are URI metacharacters while remaining valid Windows filename characters.
    path = tmp_path / "literal # percent% database.db"
    writer = sqlite3.connect(path)
    try:
        assert writer.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
        writer.execute("CREATE TABLE sample (value INTEGER NOT NULL)")
        writer.execute("INSERT INTO sample VALUES (7)")
        writer.commit()
    finally:
        writer.close()

    with ReadOnlyDatabase(path).transaction() as conn:
        assert conn.execute("SELECT value FROM sample").fetchone()["value"] == 7

    check = sqlite3.connect(path)
    try:
        assert check.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        check.close()


def test_reader_missing_path_does_not_create_file_or_parent(tmp_path: Path) -> None:
    missing = tmp_path / "not-created" / "factory.db"

    with pytest.raises(FileNotFoundError):
        ReadOnlyDatabase(missing)

    assert not missing.parent.exists()
    assert not missing.exists()
