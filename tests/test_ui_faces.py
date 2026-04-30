"""Web UI face labeling on photo review (Task 4.5b)."""

from __future__ import annotations

import pathlib
import sqlite3
import struct
import uuid

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from db.migrate import migrate
from magpie.ui import app as app_module


@pytest.fixture()
def tmp_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db_path = tmp_path / "magpie.db"
    migrate(db_path)
    return db_path


def _dummy_embedding_blob() -> bytes:
    return struct.pack("<128f", *([0.1] * 128))


def test_face_thumbnail_route_and_label_and_confirm_auto(
    tmp_db: pathlib.Path, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    faces_dir = tmp_path / "faces"
    faces_dir.mkdir(parents=True)

    monkeypatch.setattr(app_module, "DB_PATH", tmp_db)
    monkeypatch.setattr(app_module, "FACES_DIR", faces_dir)

    fid = str(uuid.uuid4())
    fl_id = str(uuid.uuid4())
    pid = str(uuid.uuid4())
    now = "2026-01-01T12:00:00+00:00"
    bbox = "[0,0,10,10]"

    conn = sqlite3.connect(tmp_db)
    conn.execute(
        """
        INSERT INTO files (id, path, indexed_at, mime_type, extension, status, size_bytes)
        VALUES (?, ?, datetime('now'), 'image/jpeg', 'jpg', 'present', 100)
        """,
        (fid, str(tmp_path / "photo.jpg")),
    )
    conn.execute(
        """
        INSERT INTO people (id, name, created_at) VALUES (?, ?, ?)
        """,
        (pid, "AutoPerson", now),
    )
    conn.execute(
        """
        INSERT INTO face_labels (
            id, file_id, person_id, face_index, bbox, embedding,
            source, confidence, labeled_at
        ) VALUES (?, ?, ?, 0, ?, ?, 'auto', 0.82, ?)
        """,
        (fl_id, fid, pid, bbox, _dummy_embedding_blob(), now),
    )
    conn.commit()
    conn.close()

    tiny = faces_dir / f"{fid}_0.jpg"
    Image.new("RGB", (32, 32), color="red").save(tiny, format="JPEG")

    client = TestClient(app_module.app)

    r = client.get(f"/faces/{fid}/0")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("image/jpeg")

    r404 = client.get(f"/faces/{fid}/99")
    assert r404.status_code == 404

    page = client.get(f"/files/{fid}")
    assert page.status_code == 200
    assert b"Faces" in page.content
    assert f"/faces/{fid}/0".encode() in page.content
    assert b"AutoPerson" in page.content
    # File label HTMX targets must include file id (not /files//label).
    assert f'hx-post="/files/{fid}/label"'.encode() in page.content
    assert b'hx-post="/files//label"' not in page.content

    r_confirm = client.post(f"/files/{fid}/faces/0/confirm-auto")
    assert r_confirm.status_code == 200
    assert b"border-gray-700" in r_confirm.content
    assert b"border-amber-500" not in r_confirm.content

    conn2 = sqlite3.connect(tmp_db)
    row = conn2.execute(
        "SELECT source, confidence FROM face_labels WHERE file_id = ? AND face_index = 0",
        (fid,),
    ).fetchone()
    conn2.close()
    assert row[0] == "user"
    assert row[1] is None


def test_label_face_find_or_create_person(
    tmp_db: pathlib.Path, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    faces_dir = tmp_path / "faces"
    faces_dir.mkdir(parents=True)
    monkeypatch.setattr(app_module, "DB_PATH", tmp_db)
    monkeypatch.setattr(app_module, "FACES_DIR", faces_dir)

    fid = str(uuid.uuid4())
    fl_id = str(uuid.uuid4())
    now = "2026-01-02T12:00:00+00:00"
    bbox = "[1,1,20,20]"

    conn = sqlite3.connect(tmp_db)
    conn.execute(
        """
        INSERT INTO files (id, path, indexed_at, mime_type, extension, status, size_bytes)
        VALUES (?, ?, datetime('now'), 'image/jpeg', 'jpg', 'present', 50)
        """,
        (fid, str(tmp_path / "x.jpg")),
    )
    conn.execute(
        """
        INSERT INTO face_labels (
            id, file_id, person_id, face_index, bbox, embedding,
            source, confidence, labeled_at
        ) VALUES (?, ?, NULL, 0, ?, ?, 'user', NULL, ?)
        """,
        (fl_id, fid, bbox, _dummy_embedding_blob(), now),
    )
    conn.commit()
    conn.close()
    Image.new("RGB", (16, 16), color="blue").save(faces_dir / f"{fid}_0.jpg")

    client = TestClient(app_module.app)
    r = client.post(f"/files/{fid}/faces/0/label", data={"name": "  Dana  "})
    assert r.status_code == 200
    assert b"Dana" in r.content

    conn2 = sqlite3.connect(tmp_db)
    pname = conn2.execute("SELECT name FROM people WHERE name = 'Dana'").fetchone()
    fl = conn2.execute(
        "SELECT person_id, source FROM face_labels WHERE file_id = ?",
        (fid,),
    ).fetchone()
    conn2.close()
    assert pname is not None
    assert fl[1] == "user"
    assert fl[0] is not None

    # Second file reuses same person name
    fid2 = str(uuid.uuid4())
    fl2 = str(uuid.uuid4())
    conn3 = sqlite3.connect(tmp_db)
    conn3.execute(
        """
        INSERT INTO files (id, path, indexed_at, mime_type, extension, status, size_bytes)
        VALUES (?, ?, datetime('now'), 'image/jpeg', 'jpg', 'present', 50)
        """,
        (fid2, str(tmp_path / "y.jpg")),
    )
    conn3.execute(
        """
        INSERT INTO face_labels (
            id, file_id, person_id, face_index, bbox, embedding,
            source, confidence, labeled_at
        ) VALUES (?, ?, NULL, 0, ?, ?, 'user', NULL, ?)
        """,
        (fl2, fid2, bbox, _dummy_embedding_blob(), now),
    )
    conn3.commit()
    conn3.close()
    Image.new("RGB", (8, 8), color="green").save(faces_dir / f"{fid2}_0.jpg")

    client.post(f"/files/{fid2}/faces/0/label", data={"name": "Dana"})
    conn4 = sqlite3.connect(tmp_db)
    n_people = conn4.execute("SELECT COUNT(*) FROM people WHERE name = 'Dana'").fetchone()[0]
    conn4.close()
    assert n_people == 1


def test_label_face_clears_person_when_name_empty(
    tmp_db: pathlib.Path, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    faces_dir = tmp_path / "faces"
    faces_dir.mkdir(parents=True)
    monkeypatch.setattr(app_module, "DB_PATH", tmp_db)
    monkeypatch.setattr(app_module, "FACES_DIR", faces_dir)

    fid = str(uuid.uuid4())
    pid = str(uuid.uuid4())
    fl_id = str(uuid.uuid4())
    now = "2026-01-03T12:00:00+00:00"
    bbox = "[0,0,5,5]"

    conn = sqlite3.connect(tmp_db)
    conn.execute(
        """
        INSERT INTO files (id, path, indexed_at, mime_type, extension, status, size_bytes)
        VALUES (?, ?, datetime('now'), 'image/jpeg', 'jpg', 'present', 10)
        """,
        (fid, str(tmp_path / "z.jpg")),
    )
    conn.execute(
        "INSERT INTO people (id, name, created_at) VALUES (?, ?, ?)",
        (pid, "Temp", now),
    )
    conn.execute(
        """
        INSERT INTO face_labels (
            id, file_id, person_id, face_index, bbox, embedding,
            source, confidence, labeled_at
        ) VALUES (?, ?, ?, 0, ?, ?, 'user', NULL, ?)
        """,
        (fl_id, fid, pid, bbox, _dummy_embedding_blob(), now),
    )
    conn.commit()
    conn.close()
    Image.new("RGB", (8, 8), color="white").save(faces_dir / f"{fid}_0.jpg")

    client = TestClient(app_module.app)
    r = client.post(f"/files/{fid}/faces/0/label", data={"name": ""})
    assert r.status_code == 200

    conn2 = sqlite3.connect(tmp_db)
    fl = conn2.execute(
        "SELECT person_id, source FROM face_labels WHERE file_id = ?",
        (fid,),
    ).fetchone()
    conn2.close()
    assert fl[0] is None
    assert fl[1] == "user"


def test_label_face_propagates_to_cluster_siblings(
    tmp_db: pathlib.Path, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that labeling a face in a cluster reassigns all cluster siblings."""
    faces_dir = tmp_path / "faces"
    faces_dir.mkdir(parents=True)
    monkeypatch.setattr(app_module, "DB_PATH", tmp_db)
    monkeypatch.setattr(app_module, "FACES_DIR", faces_dir)

    # Create 3 files with unlabeled faces in the same cluster
    cluster_id = str(uuid.uuid4())
    file_ids = [str(uuid.uuid4()) for _ in range(3)]
    fl_ids = [str(uuid.uuid4()) for _ in range(3)]
    now = "2026-01-04T12:00:00+00:00"
    bbox = "[0,0,10,10]"

    conn = sqlite3.connect(tmp_db)
    for fid, fl_id in zip(file_ids, fl_ids):
        conn.execute(
            """
            INSERT INTO files (id, path, indexed_at, mime_type, extension, status, size_bytes)
            VALUES (?, ?, datetime('now'), 'image/jpeg', 'jpg', 'present', 100)
            """,
            (fid, str(tmp_path / f"{fid}.jpg")),
        )
        conn.execute(
            """
            INSERT INTO face_labels (
                id, file_id, person_id, face_index, bbox, embedding,
                source, confidence, labeled_at, cluster_id
            ) VALUES (?, ?, NULL, 0, ?, ?, 'auto', 0.85, ?, ?)
            """,
            (fl_id, fid, bbox, _dummy_embedding_blob(), now, cluster_id),
        )
        Image.new("RGB", (32, 32), color="red").save(faces_dir / f"{fid}_0.jpg")

    conn.commit()
    conn.close()

    # Label the first face
    client = TestClient(app_module.app)
    r = client.post(f"/files/{file_ids[0]}/faces/0/label", data={"name": "TestPerson"})
    assert r.status_code == 200

    # Check that all faces in the cluster now have the same person_id and cluster_id is cleared
    conn2 = sqlite3.connect(tmp_db)
    all_faces = conn2.execute(
        "SELECT file_id, person_id, cluster_id, source FROM face_labels ORDER BY file_id",
    ).fetchall()
    conn2.close()

    assert len(all_faces) == 3
    # All should have the same person_id
    person_ids = [f[1] for f in all_faces]
    assert all(pid == person_ids[0] for pid in person_ids)
    assert person_ids[0] is not None

    # None should have a cluster_id
    cluster_ids = [f[2] for f in all_faces]
    assert all(cid is None for cid in cluster_ids)

    # All should have source='user'
    sources = [f[3] for f in all_faces]
    assert all(src == "user" for src in sources)


def test_label_cluster_endpoint(
    tmp_db: pathlib.Path, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test the bulk cluster labeling endpoint."""
    faces_dir = tmp_path / "faces"
    faces_dir.mkdir(parents=True)
    monkeypatch.setattr(app_module, "DB_PATH", tmp_db)
    monkeypatch.setattr(app_module, "FACES_DIR", faces_dir)

    # Create 2 files with unlabeled faces in the same cluster
    cluster_id = str(uuid.uuid4())
    file_ids = [str(uuid.uuid4()) for _ in range(2)]
    fl_ids = [str(uuid.uuid4()) for _ in range(2)]
    now = "2026-01-05T12:00:00+00:00"
    bbox = "[0,0,10,10]"

    conn = sqlite3.connect(tmp_db)
    for fid, fl_id in zip(file_ids, fl_ids):
        conn.execute(
            """
            INSERT INTO files (id, path, indexed_at, mime_type, extension, status, size_bytes)
            VALUES (?, ?, datetime('now'), 'image/jpeg', 'jpg', 'present', 100)
            """,
            (fid, str(tmp_path / f"{fid}.jpg")),
        )
        conn.execute(
            """
            INSERT INTO face_labels (
                id, file_id, person_id, face_index, bbox, embedding,
                source, confidence, labeled_at, cluster_id
            ) VALUES (?, ?, NULL, 0, ?, ?, 'auto', 0.85, ?, ?)
            """,
            (fl_id, fid, bbox, _dummy_embedding_blob(), now, cluster_id),
        )
        Image.new("RGB", (32, 32), color="blue").save(faces_dir / f"{fid}_0.jpg")

    conn.commit()
    conn.close()

    # POST to the cluster label endpoint
    client = TestClient(app_module.app)
    r = client.post(
        f"/people/cluster/{cluster_id}/label",
        data={"name": "ClusterPerson"},
        follow_redirects=False,
    )

    # Should redirect to person page (303 See Other)
    assert r.status_code == 303
    location = r.headers.get("location", "")
    assert "/people/" in location

    # Check that all faces are labeled with the same person
    conn2 = sqlite3.connect(tmp_db)
    person_name = conn2.execute(
        """
        SELECT DISTINCT p.name
        FROM face_labels fl
        JOIN people p ON p.id = fl.person_id
        WHERE fl.cluster_id IS NULL AND fl.file_id IN (?, ?)
        """,
        (file_ids[0], file_ids[1]),
    ).fetchone()
    conn2.close()

    assert person_name is not None
    assert person_name[0] == "ClusterPerson"
