"""Web UI duplicate review routes."""

import pathlib
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

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
    created_at: str | None = None,
) -> str:
    fid = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO files (id, path, indexed_at, content_hash, status, size_bytes, created_at)
        VALUES (?, ?, datetime('now'), ?, 'present', ?, ?)
        """,
        (fid, path, content_hash, size_bytes, created_at),
    )
    return fid


def test_duplicates_list_and_keep(
    tmp_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_db)

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _insert_file(conn, path="/a/one.bin", created_at="2020-01-01", size_bytes=100)
    _insert_file(conn, path="/b/two.bin", created_at="2021-01-01", size_bytes=100)
    conn.commit()
    conn.close()

    populate_exact_duplicate_groups(tmp_db)

    conn2 = sqlite3.connect(tmp_db)
    conn2.row_factory = sqlite3.Row
    gid = conn2.execute("SELECT group_id FROM duplicate_groups LIMIT 1").fetchone()[
        "group_id"
    ]
    rows = conn2.execute(
        """
        SELECT dg.file_id, dg.recommended_keep
        FROM duplicate_groups dg
        JOIN files f ON f.id = dg.file_id
        ORDER BY f.path
        """
    ).fetchall()
    conn2.close()

    keeper = next(r["file_id"] for r in rows if r["recommended_keep"])
    other = next(r["file_id"] for r in rows if not r["recommended_keep"])

    client = TestClient(app_module.app)

    r = client.get("/duplicates", follow_redirects=False)
    assert r.status_code == 200
    assert gid.encode() in r.content

    r2 = client.get(f"/duplicates/{gid}", follow_redirects=False)
    assert r2.status_code == 200
    assert b"Keep this one" in r2.content

    # Switch keeper to `other`
    r3 = client.post(
        f"/duplicates/{gid}/keep/{other}", follow_redirects=False
    )
    assert r3.status_code == 303
    assert other in r3.headers["location"] or "updated=1" in r3.headers["location"]

    conn3 = sqlite3.connect(tmp_db)
    conn3.row_factory = sqlite3.Row
    k_rows = conn3.execute(
        "SELECT file_id, recommended_keep FROM duplicate_groups WHERE group_id = ?",
        (gid,),
    ).fetchall()
    assert sum(1 for x in k_rows if x["recommended_keep"]) == 1
    new_keeper = next(x["file_id"] for x in k_rows if x["recommended_keep"])
    assert new_keeper == other

    lbl_other = conn3.execute(
        "SELECT action FROM labels WHERE file_id = ?", (other,)
    ).fetchone()
    assert lbl_other is None or lbl_other["action"] != "trash"

    lbl_keeper_old = conn3.execute(
        "SELECT action FROM labels WHERE file_id = ?", (keeper,)
    ).fetchone()
    assert lbl_keeper_old and lbl_keeper_old["action"] == "trash"
    conn3.close()
