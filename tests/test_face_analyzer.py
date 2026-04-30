"""Tests for face detection / embeddings (Task 3.4)."""

from __future__ import annotations

import json
import pathlib
import struct
import uuid
from datetime import datetime, timezone

import numpy as np
import pytest
from PIL import Image
from typer.testing import CliRunner

from db.migrate import migrate
from magpie.cli import app
from magpie.db import get_connection
from magpie.face_analyzer import (
    _embedding_to_blob,
    _find_face_targets,
    _location_to_bbox,
    load_rgb_numpy,
    run_analyze_faces,
)


@pytest.fixture()
def tmp_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db_path = tmp_path / "magpie.db"
    migrate(db_path)
    return db_path


@pytest.fixture()
def db_with_images(tmp_db: pathlib.Path, tmp_path: pathlib.Path):
    """Two indexed images + one non-image."""
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    jpg = media_dir / "a.jpg"
    Image.new("RGB", (64, 48), color=(200, 100, 50)).save(jpg, format="JPEG")
    png = media_dir / "b.png"
    Image.new("RGB", (32, 32), color="blue").save(png, format="PNG")
    txt = media_dir / "note.txt"
    txt.write_text("hello")

    rows: list[dict] = []
    with get_connection(tmp_db) as conn:
        for p, mime, ext in (
            (jpg, "image/jpeg", "jpg"),
            (png, "image/png", "png"),
            (txt, "text/plain", "txt"),
        ):
            fid = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """INSERT INTO files (id, path, size_bytes, indexed_at, mime_type, extension, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'present')""",
                (fid, str(p), p.stat().st_size if p.is_file() else 0, now, mime, ext),
            )
            rows.append({"id": fid, "path": str(p), "ext": ext})
        conn.commit()
    return media_dir, tmp_db, rows


runner = CliRunner()


def test_location_to_bbox() -> None:
    # top, right, bottom, left = 10, 60, 50, 20 → x=20, y=10, w=40, h=40
    assert _location_to_bbox(10, 60, 50, 20) == [20, 10, 40, 40]


def test_embedding_blob_roundtrip() -> None:
    vec = np.arange(512, dtype=np.float64)
    blob = _embedding_to_blob(vec)
    assert len(blob) == 512 * 4
    back = np.array(struct.unpack(f"<{512}f", blob), dtype=np.float32)
    assert back.shape == (512,)
    np.testing.assert_allclose(back, vec.astype(np.float32), rtol=1e-6)


def test_find_face_targets_filters_and_limit(db_with_images: tuple) -> None:
    media_dir, tmp_db, rows = db_with_images
    targets = _find_face_targets(media_dir, tmp_db, limit=None, force=False)
    ids = {t["id"] for t in targets}
    jpg = next(r for r in rows if r["ext"] == "jpg")
    png = next(r for r in rows if r["ext"] == "png")
    assert jpg["id"] in ids
    assert png["id"] in ids
    assert all(r["ext"] != "txt" for r in rows if r["id"] in ids)

    one = _find_face_targets(media_dir, tmp_db, limit=1, force=False)
    assert len(one) == 1


def test_find_face_targets_skips_when_face_detection_done(db_with_images: tuple) -> None:
    media_dir, tmp_db, rows = db_with_images
    jpg = next(r for r in rows if r["ext"] == "jpg")
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(tmp_db) as conn:
        conn.execute(
            "UPDATE files SET face_detection_at = ? WHERE id = ?",
            (now, jpg["id"]),
        )
        conn.commit()

    targets = _find_face_targets(media_dir, tmp_db, limit=None, force=False)
    ids = {t["id"] for t in targets}
    assert jpg["id"] not in ids


def test_find_face_targets_force_includes_done(db_with_images: tuple) -> None:
    media_dir, tmp_db, rows = db_with_images
    jpg = next(r for r in rows if r["ext"] == "jpg")
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(tmp_db) as conn:
        conn.execute(
            "UPDATE files SET face_detection_at = ? WHERE id = ?",
            (now, jpg["id"]),
        )
        conn.commit()

    targets = _find_face_targets(media_dir, tmp_db, limit=None, force=True)
    ids = {t["id"] for t in targets}
    assert jpg["id"] in ids


def test_load_rgb_numpy_jpg(db_with_images: tuple) -> None:
    media_dir, _tmp_db, rows = db_with_images
    jpg = next(r for r in rows if r["ext"] == "jpg")
    arr = load_rgb_numpy(pathlib.Path(jpg["path"]), "jpg")
    assert arr.ndim == 3
    assert arr.shape[2] == 3
    assert arr.dtype == np.uint8


def test_run_analyze_faces_with_fake_module(
    db_with_images: tuple,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_dir, tmp_db, rows = db_with_images
    jpg = next(r for r in rows if r["ext"] == "jpg")

    class FakeFace:
        def __init__(self, h: int, w: int) -> None:
            self.bbox = np.array([4.0, 4.0, float(w - 4), float(h - 4)])
            self.embedding = np.linspace(0, 1, 512, dtype=np.float32)

    class FakeApp:
        def get(self, bgr: np.ndarray):
            h, w = bgr.shape[:2]
            return [FakeFace(h, w)]

    faces_dir = tmp_path / "faces"
    run_analyze_faces(
        media_dir,
        tmp_db,
        dry_run=False,
        limit=1,
        force=False,
        faces_dir=faces_dir,
        _app=FakeApp(),
    )

    thumb = faces_dir / f"{jpg['id']}_0.jpg"
    assert thumb.is_file()
    im = Image.open(thumb)
    assert max(im.size) <= 256

    with get_connection(tmp_db) as conn:
        fl = conn.execute(
            "SELECT file_id, face_index, bbox, source, confidence, person_id FROM face_labels",
        ).fetchall()
        fd = conn.execute(
            "SELECT face_detection_at FROM files WHERE id = ?",
            (jpg["id"],),
        ).fetchone()
    assert len(fl) == 1
    assert fl[0]["file_id"] == jpg["id"]
    assert fl[0]["face_index"] == 0
    assert fl[0]["source"] == "user"
    assert fl[0]["confidence"] is None
    assert fl[0]["person_id"] is None
    bbox = json.loads(fl[0]["bbox"])
    assert len(bbox) == 4 and all(isinstance(x, int) for x in bbox)
    assert fd["face_detection_at"] is not None


def test_dry_run_faces_cli_no_writes(db_with_images: tuple) -> None:
    media_dir, tmp_db, _ = db_with_images
    result = runner.invoke(
        app,
        ["analyze", "faces", str(media_dir), "--db", str(tmp_db), "--dry-run"],
    )
    assert result.exit_code == 0
    assert "Dry run" in result.output
    with get_connection(tmp_db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM face_labels").fetchone()[0]
    assert n == 0


def test_migration_files_has_face_detection_column(tmp_db: pathlib.Path) -> None:
    with get_connection(tmp_db) as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(files)")}
    assert "face_detection_at" in cols


def test_find_face_targets_skips_dot_underscore(tmp_db: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """._-prefixed files (macOS resource forks) are silently excluded from face analysis."""
    media_dir = tmp_path / "media"
    media_dir.mkdir()

    real_jpg = media_dir / "photo.jpg"
    Image.new("RGB", (64, 48), color="red").save(real_jpg, format="JPEG")

    resource_fork = media_dir / "._photo.jpg"
    resource_fork.write_bytes(b"\x00\x05\x16\x07")

    now = datetime.now(timezone.utc).isoformat()
    real_id = str(uuid.uuid4())
    fork_id = str(uuid.uuid4())
    with get_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO files (id, path, size_bytes, indexed_at, mime_type, extension, status) VALUES (?,?,?,?,?,?,?)",
            (real_id, str(real_jpg), real_jpg.stat().st_size, now, "image/jpeg", "jpg", "present"),
        )
        conn.execute(
            "INSERT INTO files (id, path, size_bytes, indexed_at, mime_type, extension, status) VALUES (?,?,?,?,?,?,?)",
            (fork_id, str(resource_fork), 4, now, "image/jpeg", "jpg", "present"),
        )
        conn.commit()

    targets = _find_face_targets(media_dir, tmp_db, limit=None, force=False)
    ids = {t["id"] for t in targets}
    assert real_id in ids
    assert fork_id not in ids


def test_find_face_targets_skips_trash_labeled(tmp_db: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """Files with a pending trash label are excluded from face analysis."""
    media_dir = tmp_path / "media2"
    media_dir.mkdir()

    keep_jpg = media_dir / "keep.jpg"
    Image.new("RGB", (64, 48), color="green").save(keep_jpg, format="JPEG")
    trash_jpg = media_dir / "trash.jpg"
    Image.new("RGB", (64, 48), color="red").save(trash_jpg, format="JPEG")

    now = datetime.now(timezone.utc).isoformat()
    keep_id = str(uuid.uuid4())
    trash_id = str(uuid.uuid4())
    with get_connection(tmp_db) as conn:
        for fid, p in ((keep_id, keep_jpg), (trash_id, trash_jpg)):
            conn.execute(
                "INSERT INTO files (id, path, size_bytes, indexed_at, mime_type, extension, status) VALUES (?,?,?,?,?,?,?)",
                (fid, str(p), p.stat().st_size, now, "image/jpeg", "jpg", "present"),
            )
        conn.execute(
            "INSERT INTO labels (file_id, action, source, labeled_at) VALUES (?,?,?,?)",
            (trash_id, "trash", "user", now),
        )
        conn.commit()

    targets = _find_face_targets(media_dir, tmp_db, limit=None, force=False)
    ids = {t["id"] for t in targets}
    assert keep_id in ids
    assert trash_id not in ids
