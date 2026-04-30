"""Tests for automated face matching (Task 3.5)."""

from __future__ import annotations

import pathlib
import uuid
from datetime import datetime, timezone

import numpy as np
import pytest
from typer.testing import CliRunner

from db.migrate import migrate
from magpie.cli import app
from magpie.db import get_connection
from magpie.face_analyzer import _embedding_to_blob
from magpie.face_matcher import NO_LABELED_MSG, run_face_match


@pytest.fixture()
def tmp_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db_path = tmp_path / "magpie.db"
    migrate(db_path)
    return db_path


def _insert_file(conn, path: str = "/tmp/x.jpg") -> str:
    fid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO files (id, path, size_bytes, indexed_at, mime_type, extension, status)
           VALUES (?, ?, 1, ?, 'image/jpeg', 'jpg', 'present')""",
        (fid, path, now),
    )
    return fid


def _insert_person(conn, name: str = "Ada") -> str:
    pid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO people (id, name, created_at) VALUES (?, ?, ?)",
        (pid, name, now),
    )
    return pid


def _insert_face(
    conn,
    *,
    file_id: str,
    face_index: int,
    embedding_vec: np.ndarray,
    person_id: str | None,
    source: str = "user",
    confidence: float | None = None,
) -> str:
    fl_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    blob = _embedding_to_blob(embedding_vec)
    bbox = "[0,0,10,10]"
    conn.execute(
        """
        INSERT INTO face_labels (id, file_id, person_id, face_index, bbox, embedding, source, confidence, labeled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (fl_id, file_id, person_id, face_index, bbox, blob, source, confidence, now),
    )
    return fl_id


def test_no_labeled_faces_message(tmp_db: pathlib.Path) -> None:
    with get_connection(tmp_db) as conn:
        fid = _insert_file(conn)
        proto = np.zeros(512, dtype=np.float32)
        _insert_face(conn, file_id=fid, face_index=0, embedding_vec=proto, person_id=None)
        conn.commit()

    result = run_face_match(tmp_db, threshold=0.6)
    assert result.no_labeled_prototypes is True
    assert result.matched == 0


def test_match_within_threshold_updates_row(tmp_db: pathlib.Path) -> None:
    labeled_vec = np.zeros(512, dtype=np.float32)
    # Euclidean distance 0.3 from prototype
    unknown_vec = labeled_vec.copy()
    unknown_vec[0] = 0.3

    with get_connection(tmp_db) as conn:
        fid_a = _insert_file(conn, "/a.jpg")
        fid_b = _insert_file(conn, "/b.jpg")
        pid = _insert_person(conn)
        _insert_face(
            conn,
            file_id=fid_a,
            face_index=0,
            embedding_vec=labeled_vec,
            person_id=pid,
            source="user",
        )
        fl_unknown = _insert_face(
            conn,
            file_id=fid_b,
            face_index=0,
            embedding_vec=unknown_vec,
            person_id=None,
        )
        conn.commit()

    result = run_face_match(tmp_db, threshold=1.0)
    assert result.no_labeled_prototypes is False
    assert result.matched == 1
    assert result.left_unidentified == 0
    assert len(result.confidences) == 1
    assert result.confidences[0] == pytest.approx(1.0 - 0.3 / 1.0)

    with get_connection(tmp_db) as conn:
        row = conn.execute(
            "SELECT person_id, source, confidence FROM face_labels WHERE id = ?",
            (fl_unknown,),
        ).fetchone()
    assert row["person_id"] == pid
    assert row["source"] == "auto"
    assert row["confidence"] == pytest.approx(0.7)


def test_beyond_threshold_left_unidentified(tmp_db: pathlib.Path) -> None:
    labeled_vec = np.zeros(512, dtype=np.float32)
    unknown_vec = labeled_vec.copy()
    unknown_vec[0] = 0.7

    with get_connection(tmp_db) as conn:
        fid_a = _insert_file(conn)
        fid_b = _insert_file(conn)
        pid = _insert_person(conn)
        _insert_face(
            conn,
            file_id=fid_a,
            face_index=0,
            embedding_vec=labeled_vec,
            person_id=pid,
        )
        fl_unknown = _insert_face(
            conn,
            file_id=fid_b,
            face_index=0,
            embedding_vec=unknown_vec,
            person_id=None,
        )
        conn.commit()

    result = run_face_match(tmp_db, threshold=0.6)
    assert result.matched == 0
    assert result.left_unidentified == 1
    assert result.confidences == []

    with get_connection(tmp_db) as conn:
        row = conn.execute(
            "SELECT person_id, source, confidence FROM face_labels WHERE id = ?",
            (fl_unknown,),
        ).fetchone()
    assert row["person_id"] is None
    assert row["source"] == "user"


def test_nearest_of_two_labeled_people(tmp_db: pathlib.Path) -> None:
    v_alice = np.zeros(512, dtype=np.float32)
    v_bob = np.zeros(512, dtype=np.float32)
    v_bob[1] = 1.0

    unknown = np.zeros(512, dtype=np.float32)
    unknown[0] = 0.1  # closer to alice (dist 0.1) than bob (dist sqrt(0.1^2+1^2))

    with get_connection(tmp_db) as conn:
        fa = _insert_file(conn, "/a.jpg")
        fb = _insert_file(conn, "/b.jpg")
        fc = _insert_file(conn, "/c.jpg")
        alice = _insert_person(conn, "Alice")
        bob = _insert_person(conn, "Bob")
        _insert_face(conn, file_id=fa, face_index=0, embedding_vec=v_alice, person_id=alice)
        _insert_face(conn, file_id=fb, face_index=0, embedding_vec=v_bob, person_id=bob)
        fl_u = _insert_face(conn, file_id=fc, face_index=0, embedding_vec=unknown, person_id=None)
        conn.commit()

    run_face_match(tmp_db, threshold=0.6)
    with get_connection(tmp_db) as conn:
        pid = conn.execute(
            "SELECT person_id FROM face_labels WHERE id = ?",
            (fl_u,),
        ).fetchone()["person_id"]
    assert pid == alice


def test_threshold_positive_validation(tmp_db: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        run_face_match(tmp_db, threshold=0.0)


runner = CliRunner()


def test_cli_no_labeled_prints_message(tmp_db: pathlib.Path) -> None:
    r = runner.invoke(app, ["faces", "match", "--db", str(tmp_db)])
    assert r.exit_code == 0
    assert NO_LABELED_MSG in r.stdout


def test_cli_summary_table(tmp_db: pathlib.Path) -> None:
    labeled_vec = np.zeros(512, dtype=np.float32)
    unknown_vec = labeled_vec.copy()
    unknown_vec[0] = 0.3

    with get_connection(tmp_db) as conn:
        fa = _insert_file(conn)
        fb = _insert_file(conn)
        pid = _insert_person(conn)
        _insert_face(conn, file_id=fa, face_index=0, embedding_vec=labeled_vec, person_id=pid)
        _insert_face(conn, file_id=fb, face_index=0, embedding_vec=unknown_vec, person_id=None)
        conn.commit()

    r = runner.invoke(app, ["faces", "match", "--db", str(tmp_db), "--threshold", "0.6"])
    assert r.exit_code == 0
    assert "Face match summary" in r.stdout
    assert "Faces matched" in r.stdout
    assert "1" in r.stdout
    assert "Confidence min" in r.stdout
