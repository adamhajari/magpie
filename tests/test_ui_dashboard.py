"""Dashboard aggregate stats."""

import pathlib
import sqlite3
import uuid

import pytest

from db.migrate import migrate
from magpie.dupes import populate_exact_duplicate_groups
from magpie.ui import app as app_module


@pytest.fixture()
def tmp_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db_path = tmp_path / "magpie.db"
    migrate(db_path)
    return db_path


def _insert_file(
    conn: sqlite3.Connection,
    *,
    path: str,
    content_hash: str = "shared",
    size_bytes: int = 1000,
    drive_id: str | None = None,
) -> str:
    fid = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO files (id, path, indexed_at, content_hash, status, size_bytes, drive_id)
        VALUES (?, ?, datetime('now'), ?, 'present', ?, ?)
        """,
        (fid, path, content_hash, size_bytes, drive_id),
    )
    return fid


def test_trash_duplicate_metric_counts_exact_hash_even_when_not_in_duplicate_groups(
    tmp_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trash files share content_hash with a keeper but are omitted from duplicate_groups on repopulate."""
    monkeypatch.setattr(app_module, "DB_PATH", tmp_db)

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    keeper_id = _insert_file(conn, path="/a/keep.bin", content_hash="h1", size_bytes=500)
    trash_id = _insert_file(conn, path="/b/trash.bin", content_hash="h1", size_bytes=500)
    conn.execute(
        """
        INSERT INTO labels (file_id, action, source, labeled_at)
        VALUES (?, 'trash', 'user', datetime('now'))
        """,
        (trash_id,),
    )
    conn.commit()
    conn.close()

    populate_exact_duplicate_groups(tmp_db)

    app_module._fetch_stats.invalidate()
    stats = app_module._fetch_stats("")
    assert stats["trash_duplicate_file_count"] == 1
    assert stats["trash_duplicate_total_size"] == 500


def test_trash_duplicate_metric_respects_drive_filter(
    tmp_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_db)
    drive_a = str(uuid.uuid4())
    drive_b = str(uuid.uuid4())

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT INTO drives (id, label, source, last_seen_at)
        VALUES (?, 'drive-a', 'local', datetime('now')),
               (?, 'drive-b', 'local', datetime('now'))
        """,
        (drive_a, drive_b),
    )
    _insert_file(conn, path="/a/keep.bin", content_hash="h1", drive_id=drive_a)
    tid = _insert_file(conn, path="/b/tr.bin", content_hash="h1", drive_id=drive_b)
    conn.execute(
        """
        INSERT INTO labels (file_id, action, source, labeled_at)
        VALUES (?, 'trash', 'user', datetime('now'))
        """,
        (tid,),
    )
    conn.commit()
    conn.close()

    populate_exact_duplicate_groups(tmp_db)

    app_module._fetch_stats.invalidate()
    all_stats = app_module._fetch_stats("")
    drive_b_stats = app_module._fetch_stats(drive_b)

    assert all_stats["trash_duplicate_file_count"] == 1
    assert drive_b_stats["trash_duplicate_file_count"] == 1

    drive_a_only = app_module._fetch_stats(drive_a)
    assert drive_a_only["trash_duplicate_file_count"] == 0


def test_duplicate_file_total_size_sums_distinct_members(
    tmp_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_db)

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _insert_file(conn, path="/a/x.bin", content_hash="hx", size_bytes=400)
    _insert_file(conn, path="/b/y.bin", content_hash="hx", size_bytes=400)
    conn.commit()
    conn.close()

    populate_exact_duplicate_groups(tmp_db)

    app_module._fetch_stats.invalidate()
    stats = app_module._fetch_stats("")
    assert stats["duplicate_file_count"] == 2
    assert stats["duplicate_file_total_size"] == 800
