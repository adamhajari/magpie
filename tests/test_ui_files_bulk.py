"""Files list bulk labeling."""

from __future__ import annotations

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


def test_bulk_label_applies_to_selected_files(
    tmp_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_db)

    d1 = str(uuid.uuid4())
    f1 = str(uuid.uuid4())
    f2 = str(uuid.uuid4())
    f3 = str(uuid.uuid4())

    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO drives (id, label, last_seen_at) VALUES (?, ?, datetime('now'))",
        (d1, "Drive A"),
    )
    for fid, path in [
        (f1, "/tmp/a.jpg"),
        (f2, "/tmp/b.jpg"),
        (f3, "/tmp/c.jpg"),
    ]:
        conn.execute(
            """
            INSERT INTO files (id, path, drive_id, indexed_at, mime_type, extension, status, size_bytes)
            VALUES (?, ?, ?, datetime('now'), 'image/jpeg', 'jpg', 'present', 10)
            """,
            (fid, path, d1),
        )
    conn.commit()
    conn.close()

    client = TestClient(app_module.app)
    page = client.get("/files")
    assert page.status_code == 200
    assert b'name="file_ids"' in page.content

    data = {
        "action": "keep_cold",
        "mime": "",
        "drive_id": "",
        "label_status": "",
        "date_from": "",
        "date_to": "",
        "sort": "date",
        "order": "desc",
        "page": "1",
        "file_ids": [f1, f3],
    }
    r = client.post("/files/bulk-label", data=data)
    assert r.status_code == 200
    assert b">Cold<" in r.content

    conn2 = sqlite3.connect(tmp_db)
    by_f = dict(conn2.execute("SELECT file_id, action FROM labels ORDER BY file_id").fetchall())
    conn2.close()
    assert by_f.get(f1) == "keep_cold"
    assert by_f.get(f3) == "keep_cold"
    assert f2 not in by_f


def test_bulk_label_empty_selection_400(
    tmp_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_db)
    client = TestClient(app_module.app)
    data = {
        "action": "trash",
        "mime": "",
        "drive_id": "",
        "label_status": "",
        "date_from": "",
        "date_to": "",
        "sort": "date",
        "order": "desc",
        "page": "1",
    }
    r = client.post("/files/bulk-label", data=data)
    assert r.status_code == 400
