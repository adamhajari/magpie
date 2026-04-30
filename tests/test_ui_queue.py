"""Web UI action queue routes."""

import pathlib
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from db.migrate import migrate
from magpie.ui import app as app_module


@pytest.fixture()
def tmp_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db_path = tmp_path / "magpie.db"
    migrate(db_path)
    return db_path


def _insert_file(conn: sqlite3.Connection, path: str, size_bytes: int = 500) -> str:
    fid = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO files (id, path, indexed_at, status, size_bytes, modified_at)
        VALUES (?, ?, datetime('now'), 'present', ?, datetime('now'))
        """,
        (fid, path, size_bytes),
    )
    return fid


def test_queue_page_empty_db(tmp_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_db)
    client = TestClient(app_module.app)
    r = client.get("/queue")
    assert r.status_code == 200
    assert "Action Queue" in r.text


def test_approve_all_unapprove_and_stream(
    tmp_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_db)
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    f1 = _insert_file(conn, "/tmp/q_one.jpg")
    f2 = _insert_file(conn, "/tmp/q_two.jpg")
    conn.execute(
        """
        INSERT INTO labels (file_id, action, source, labeled_at, approved_at)
        VALUES (?, 'trash', 'user', datetime('now'), NULL)
        """,
        (f1,),
    )
    conn.execute(
        """
        INSERT INTO labels (file_id, action, source, confidence, labeled_at, approved_at)
        VALUES (?, 'trash', 'classifier', 0.91, datetime('now'), datetime('now'))
        """,
        (f2,),
    )
    conn.commit()
    conn.close()

    client = TestClient(app_module.app)
    r = client.post("/labels/approve-all?action=trash", follow_redirects=False)
    assert r.status_code == 303
    assert "approved=1" in (r.headers.get("location") or "")

    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT COUNT(*) FROM labels WHERE action='trash' AND approved_at IS NOT NULL"
    ).fetchone()
    assert row[0] == 2
    conn.close()

    r2 = client.get("/queue")
    assert r2.status_code == 200
    assert "q_two.jpg" in r2.text

    r3 = client.post(f"/labels/{f2}/unapprove", follow_redirects=False)
    assert r3.status_code == 303

    conn = sqlite3.connect(tmp_db)
    ap = conn.execute(
        "SELECT approved_at FROM labels WHERE file_id = ?", (f2,)
    ).fetchone()[0]
    assert ap is None
    conn.close()

    with client.stream("GET", "/queue/execute-stream?action=trash") as stream:
        assert stream.status_code == 200
        body = b"".join(stream.iter_bytes())
    assert b"exit_code" in body
    assert b"execute" in body.lower() or b"approved_files" in body

    bad = client.get("/queue/execute-stream?action=nope")
    assert bad.status_code == 200
    assert b"invalid" in bad.content.lower()


def test_approve_all_invalid_action(tmp_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_db)
    client = TestClient(app_module.app)
    r = client.post("/labels/approve-all?action=invalid")
    assert r.status_code == 400


def test_cli_execute(tmp_db: pathlib.Path) -> None:
    from typer.testing import CliRunner

    from magpie.cli import app as cli_app

    runner = CliRunner()
    result = runner.invoke(cli_app, ["execute", "--action", "trash", "--db", str(tmp_db)])
    assert result.exit_code == 0
    assert "approved_files" in result.stdout or "execute" in result.stdout
