"""Smoke test: migration runs without error and creates all expected tables."""

import pathlib
import sqlite3
import tempfile

import pytest

from db.migrate import migrate

EXPECTED_TABLES = {
    "drives",
    "files",
    "file_analysis",
    "file_metadata",
    "duplicate_groups",
    "preferred_duplicate_paths",
    "labels",
    "executions",
    "people",
    "face_labels",
    "face_suggestions",
    "gdrive_preview",
    "dropbox_preview",
}


@pytest.fixture()
def tmp_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db_path = tmp_path / "magpie.db"
    migrate(db_path)
    return db_path


def test_migration_creates_all_tables(tmp_db: pathlib.Path) -> None:
    with sqlite3.connect(tmp_db) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
        ).fetchall()
    tables = {row[0] for row in rows}
    assert EXPECTED_TABLES == tables, f"Missing tables: {EXPECTED_TABLES - tables}"


def test_migration_is_idempotent(tmp_db: pathlib.Path) -> None:
    """Running migrate twice should not raise."""
    migrate(tmp_db)


def test_files_status_constraint(tmp_db: pathlib.Path) -> None:
    """status column rejects values outside the allowed set."""
    import uuid

    file_id = str(uuid.uuid4())
    with sqlite3.connect(tmp_db) as conn:
        conn.execute("PRAGMA foreign_keys=OFF;")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO files (id, path, indexed_at, status) VALUES (?, ?, datetime('now'), ?)",
                (file_id, "/fake/path", "invalid_status"),
            )
