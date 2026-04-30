"""Tests for the photo analyzer (Task 3.2)."""

from __future__ import annotations

import io
import json
import pathlib
import uuid
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
from typer.testing import CliRunner

from db.migrate import migrate
from magpie.analyzer import ANALYZER_LOCAL_LLAVA
from magpie.cli import app
from magpie.db import get_connection
from magpie.photo_analyzer import (
    _find_photos,
    _is_photo,
    prepare_image_jpeg_bytes,
    run_analyze_photos,
)


@pytest.fixture()
def tmp_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db_path = tmp_path / "magpie.db"
    migrate(db_path)
    return db_path


@pytest.fixture()
def db_with_photos(tmp_db: pathlib.Path, tmp_path: pathlib.Path):
    """Indexed files: photos + one text file (not a photo)."""
    media_dir = tmp_path / "media"
    media_dir.mkdir()

    def save_png(name: str) -> pathlib.Path:
        p = media_dir / name
        img = Image.new("RGB", (32, 24), color=(100, 150, 200))
        img.save(p, format="PNG")
        return p

    png_path = save_png("shot.png")
    jpg_path = media_dir / "snap.jpg"
    Image.new("RGB", (10, 10), color="red").save(jpg_path, format="JPEG")

    txt_path = media_dir / "readme.txt"
    txt_path.write_text("not a photo")

    raw_path = media_dir / "fake.nef"
    raw_path.write_bytes(b"not real raw")

    rows_spec = [
        (png_path, "image/png", "png"),
        (jpg_path, "image/jpeg", "jpg"),
        (txt_path, "text/plain", "txt"),
        (raw_path, "application/octet-stream", "nef"),
    ]

    inserted: list[dict] = []
    with get_connection(tmp_db) as conn:
        for p, mime, ext in rows_spec:
            fid = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            content = p.read_bytes() if p.exists() else b""
            conn.execute(
                """INSERT INTO files (id, path, size_bytes, indexed_at, mime_type, extension, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'present')""",
                (fid, str(p), len(content), now, mime, ext),
            )
            inserted.append({"id": fid, "path": str(p), "ext": ext})
        conn.commit()
    return media_dir, tmp_db, inserted


runner = CliRunner()


@pytest.mark.parametrize("ext,mime,expected", [
    ("jpg", "image/jpeg", True),
    ("jpeg", None, True),
    ("png", "image/png", True),
    ("webp", "image/webp", True),
    ("heic", "image/heic", True),
    ("cr2", None, True),
    ("arw", "application/octet-stream", True),
    ("nef", None, True),
    ("txt", "text/plain", False),
    ("pdf", "application/pdf", False),
    ("", None, False),
])
def test_is_photo(ext: str, mime: Optional[str], expected: bool) -> None:
    assert _is_photo(ext, mime) is expected


def test_find_photos_filters_types(db_with_photos: tuple) -> None:
    media_dir, tmp_db, _ = db_with_photos
    photos = _find_photos(media_dir, tmp_db, limit=None, force=False)
    exts = {p["extension"] for p in photos}
    assert "png" in exts
    assert "jpg" in exts
    assert "nef" in exts
    assert "txt" not in exts


def test_find_photos_skips_already_analyzed(db_with_photos: tuple) -> None:
    media_dir, tmp_db, inserted = db_with_photos
    png = next(x for x in inserted if x["ext"] == "png")
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO file_analysis (file_id, summary, tags, analyzer, analyzed_at) VALUES (?,?,?,?,?)",
            (png["id"], "done", "[]", ANALYZER_LOCAL_LLAVA, now),
        )
        conn.commit()

    photos = _find_photos(media_dir, tmp_db, limit=None, force=False)
    ids = {p["id"] for p in photos}
    assert png["id"] not in ids


def test_find_photos_force_includes_analyzed(db_with_photos: tuple) -> None:
    media_dir, tmp_db, inserted = db_with_photos
    png = next(x for x in inserted if x["ext"] == "png")
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO file_analysis (file_id, summary, tags, analyzer, analyzed_at) VALUES (?,?,?,?,?)",
            (png["id"], "done", "[]", ANALYZER_LOCAL_LLAVA, now),
        )
        conn.commit()

    photos = _find_photos(media_dir, tmp_db, limit=None, force=True)
    ids = {p["id"] for p in photos}
    assert png["id"] in ids


def test_prepare_image_jpeg_bytes_png(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "x.png"
    Image.new("RGBA", (40, 30), color=(255, 0, 0, 128)).save(p, format="PNG")
    data = prepare_image_jpeg_bytes(p, "png")
    assert data[:2] == b"\xff\xd8"
    reopened = Image.open(io.BytesIO(data))
    assert reopened.format == "JPEG"


def test_prepare_image_raw_uses_rawpy(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "x.nef"
    p.write_bytes(b"dummy")

    fake_img = Image.new("RGB", (8, 8), color="blue")

    class FakeRaw:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def postprocess(self):
            import numpy as np

            return np.array(fake_img)

    with patch("rawpy.imread", return_value=FakeRaw()):
        out = prepare_image_jpeg_bytes(p, "nef")
    assert out[:2] == b"\xff\xd8"


def test_dry_run_photos_cli(db_with_photos: tuple) -> None:
    media_dir, tmp_db, _ = db_with_photos
    result = runner.invoke(
        app,
        ["analyze", "photos", str(media_dir), "--db", str(tmp_db), "--dry-run"],
    )
    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert "local" in result.output.lower() or "ollama" in result.output.lower()
    assert "Estimated API cost" not in result.output


def test_dry_run_no_db_writes(db_with_photos: tuple) -> None:
    media_dir, tmp_db, _ = db_with_photos
    runner.invoke(
        app,
        ["analyze", "photos", str(media_dir), "--db", str(tmp_db), "--dry-run"],
    )
    with get_connection(tmp_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM file_analysis").fetchone()[0]
    assert count == 0


def test_find_photos_skips_dot_underscore(tmp_db: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """._-prefixed files (macOS resource forks) are silently excluded from photo analysis."""
    media_dir = tmp_path / "media"
    media_dir.mkdir()

    real_jpg = media_dir / "real.jpg"
    Image.new("RGB", (10, 10), color="green").save(real_jpg, format="JPEG")

    resource_fork = media_dir / "._real.jpg"
    resource_fork.write_bytes(b"\x00\x05\x16\x07")  # fake resource fork bytes

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

    photos = _find_photos(media_dir, tmp_db, limit=None, force=False)
    ids = {p["id"] for p in photos}
    assert real_id in ids
    assert fork_id not in ids


def test_find_photos_skips_trash_labeled(tmp_db: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """Files with a pending trash label are excluded from photo analysis."""
    media_dir = tmp_path / "media2"
    media_dir.mkdir()

    keep_jpg = media_dir / "keep.jpg"
    Image.new("RGB", (10, 10), color="blue").save(keep_jpg, format="JPEG")
    trash_jpg = media_dir / "trash.jpg"
    Image.new("RGB", (10, 10), color="red").save(trash_jpg, format="JPEG")

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

    photos = _find_photos(media_dir, tmp_db, limit=None, force=False)
    ids = {p["id"] for p in photos}
    assert keep_id in ids
    assert trash_id not in ids
