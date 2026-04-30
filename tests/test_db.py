"""Tests for shared DB helpers."""

import pathlib
import sqlite3

import pytest

from db.migrate import migrate
from magpie.db import get_connection


def test_get_connection_raises_with_migrate_hint_when_schema_missing(
    tmp_path: pathlib.Path,
) -> None:
    db = tmp_path / "unmigrated.db"
    sqlite3.connect(db).close()

    with pytest.raises(RuntimeError, match=r"db/migrate\.py"):
        get_connection(db)


def test_get_connection_succeeds_after_migrate(tmp_path: pathlib.Path) -> None:
    db = tmp_path / "ok.db"
    migrate(db)
    with get_connection(db) as conn:
        row = conn.execute("SELECT 1 FROM drives LIMIT 1").fetchone()
    assert row is None
