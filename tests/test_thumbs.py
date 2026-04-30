"""Tests for pHash computation and thumbnail generation (Task 1.4)."""

from __future__ import annotations

import pathlib
import uuid

import pytest
from PIL import Image

from db.migrate import migrate
from magpie.db import get_connection
from magpie.thumbs import (
    THUMBS_DIR,
    compute_phash,
    generate_thumbnail,
    process_image,
    thumb_exists,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_image(path: pathlib.Path, size: tuple[int, int] = (100, 100), color: str = "red") -> pathlib.Path:
    """Write a minimal PNG to *path* and return it."""
    img = Image.new("RGB", size, color=color)
    img.save(path, "PNG")
    return path


def _insert_file(conn, file_id: str, path: pathlib.Path) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO files (id, path, drive_id, size_bytes, indexed_at, status)
           VALUES (?, ?, NULL, 0, ?, 'present')""",
        (file_id, str(path), now),
    )


@pytest.fixture()
def tmp_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db_path = tmp_path / "magpie.db"
    migrate(db_path)
    return db_path


@pytest.fixture(autouse=True)
def isolated_thumbs_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Redirect the thumbs directory to a temp location so tests don't touch ~/.magpie."""
    test_thumbs = tmp_path / "thumbs"
    test_thumbs.mkdir()
    monkeypatch.setattr("magpie.thumbs.THUMBS_DIR", test_thumbs)
    return test_thumbs


# ---------------------------------------------------------------------------
# compute_phash
# ---------------------------------------------------------------------------

def test_compute_phash_returns_string(tmp_path: pathlib.Path) -> None:
    img_path = _make_image(tmp_path / "test.png")
    result = compute_phash(img_path)
    assert result is not None
    assert isinstance(result, str)
    assert len(result) > 0


def test_compute_phash_same_image_deterministic(tmp_path: pathlib.Path) -> None:
    img_path = _make_image(tmp_path / "test.png")
    assert compute_phash(img_path) == compute_phash(img_path)


def test_compute_phash_different_images_differ(tmp_path: pathlib.Path) -> None:
    # Solid-color images look the same to pHash (no frequency variation).
    # Use images with distinct structural content instead.
    import random

    rng = random.Random(42)
    checkerboard = Image.new("RGB", (64, 64))
    checkerboard.putdata(
        [(255, 0, 0) if (x + y) % 2 == 0 else (0, 0, 255) for y in range(64) for x in range(64)]
    )
    gradient = Image.new("RGB", (64, 64))
    gradient.putdata([(int(x * 4), int(y * 4), 0) for y in range(64) for x in range(64)])

    p1 = tmp_path / "checker.png"
    p2 = tmp_path / "gradient.png"
    checkerboard.save(p1, "PNG")
    gradient.save(p2, "PNG")

    assert compute_phash(p1) != compute_phash(p2)


def test_compute_phash_nonexistent_returns_none(tmp_path: pathlib.Path) -> None:
    assert compute_phash(tmp_path / "missing.png") is None


def test_compute_phash_non_image_returns_none(tmp_path: pathlib.Path) -> None:
    txt = tmp_path / "file.txt"
    txt.write_bytes(b"not an image")
    assert compute_phash(txt) is None


# ---------------------------------------------------------------------------
# generate_thumbnail
# ---------------------------------------------------------------------------

def test_generate_thumbnail_creates_file(tmp_path: pathlib.Path, isolated_thumbs_dir: pathlib.Path) -> None:
    img_path = _make_image(tmp_path / "photo.png", size=(800, 600))
    file_id = str(uuid.uuid4())
    result = generate_thumbnail(img_path, file_id)
    assert result is not None
    assert result.exists()
    assert result.suffix == ".jpg"


def test_generate_thumbnail_max_256px(tmp_path: pathlib.Path, isolated_thumbs_dir: pathlib.Path) -> None:
    img_path = _make_image(tmp_path / "big.png", size=(1024, 768))
    file_id = str(uuid.uuid4())
    thumb_path = generate_thumbnail(img_path, file_id)
    assert thumb_path is not None
    with Image.open(thumb_path) as img:
        assert max(img.size) <= 256


def test_generate_thumbnail_small_image_not_upscaled(tmp_path: pathlib.Path, isolated_thumbs_dir: pathlib.Path) -> None:
    """Images smaller than 256px should not be upscaled."""
    img_path = _make_image(tmp_path / "small.png", size=(50, 50))
    file_id = str(uuid.uuid4())
    thumb_path = generate_thumbnail(img_path, file_id)
    assert thumb_path is not None
    with Image.open(thumb_path) as img:
        assert img.size == (50, 50)


def test_generate_thumbnail_nonexistent_returns_none(tmp_path: pathlib.Path, isolated_thumbs_dir: pathlib.Path) -> None:
    assert generate_thumbnail(tmp_path / "missing.png", str(uuid.uuid4())) is None


# ---------------------------------------------------------------------------
# process_image — skip logic
# ---------------------------------------------------------------------------

def test_process_image_computes_phash(tmp_path: pathlib.Path, tmp_db: pathlib.Path, isolated_thumbs_dir: pathlib.Path) -> None:
    img_path = _make_image(tmp_path / "photo.png")
    file_id = str(uuid.uuid4())

    with get_connection(tmp_db) as conn:
        _insert_file(conn, file_id, img_path)
        result = process_image(file_id, img_path, conn, with_thumbs=False, force=False)

    assert result["phash"] is True
    assert result["thumb"] is False

    with get_connection(tmp_db) as conn:
        row = conn.execute("SELECT perceptual_hash FROM files WHERE id = ?", (file_id,)).fetchone()
    assert row["perceptual_hash"] is not None


def test_process_image_generates_thumbnail_when_requested(
    tmp_path: pathlib.Path, tmp_db: pathlib.Path, isolated_thumbs_dir: pathlib.Path
) -> None:
    img_path = _make_image(tmp_path / "photo.png")
    file_id = str(uuid.uuid4())

    with get_connection(tmp_db) as conn:
        _insert_file(conn, file_id, img_path)
        result = process_image(file_id, img_path, conn, with_thumbs=True, force=False)

    assert result["phash"] is True
    assert result["thumb"] is True
    assert (isolated_thumbs_dir / f"{file_id}.jpg").exists()


def test_process_image_skips_existing_phash(
    tmp_path: pathlib.Path, tmp_db: pathlib.Path, isolated_thumbs_dir: pathlib.Path
) -> None:
    img_path = _make_image(tmp_path / "photo.png")
    file_id = str(uuid.uuid4())

    with get_connection(tmp_db) as conn:
        _insert_file(conn, file_id, img_path)
        # Pre-set pHash
        conn.execute("UPDATE files SET perceptual_hash = 'aabbccdd' WHERE id = ?", (file_id,))
        result = process_image(file_id, img_path, conn, with_thumbs=False, force=False)

    assert result["phash"] is False  # already had one, skipped


def test_process_image_force_recomputes_phash(
    tmp_path: pathlib.Path, tmp_db: pathlib.Path, isolated_thumbs_dir: pathlib.Path
) -> None:
    img_path = _make_image(tmp_path / "photo.png")
    file_id = str(uuid.uuid4())

    with get_connection(tmp_db) as conn:
        _insert_file(conn, file_id, img_path)
        conn.execute("UPDATE files SET perceptual_hash = 'aabbccdd' WHERE id = ?", (file_id,))
        result = process_image(file_id, img_path, conn, with_thumbs=False, force=True)

    assert result["phash"] is True

    with get_connection(tmp_db) as conn:
        row = conn.execute("SELECT perceptual_hash FROM files WHERE id = ?", (file_id,)).fetchone()
    # Should be the real pHash, not our fake one
    assert row["perceptual_hash"] != "aabbccdd"


def test_process_image_skips_existing_thumbnail(
    tmp_path: pathlib.Path, tmp_db: pathlib.Path, isolated_thumbs_dir: pathlib.Path
) -> None:
    img_path = _make_image(tmp_path / "photo.png")
    file_id = str(uuid.uuid4())

    # Pre-create a stub thumbnail file
    stub = isolated_thumbs_dir / f"{file_id}.jpg"
    stub.write_bytes(b"stub")

    with get_connection(tmp_db) as conn:
        _insert_file(conn, file_id, img_path)
        result = process_image(file_id, img_path, conn, with_thumbs=True, force=False)

    assert result["thumb"] is False  # skipped because file already exists
    # Stub should be untouched
    assert stub.read_bytes() == b"stub"


def test_process_image_force_regenerates_thumbnail(
    tmp_path: pathlib.Path, tmp_db: pathlib.Path, isolated_thumbs_dir: pathlib.Path
) -> None:
    img_path = _make_image(tmp_path / "photo.png", size=(200, 200))
    file_id = str(uuid.uuid4())

    stub = isolated_thumbs_dir / f"{file_id}.jpg"
    stub.write_bytes(b"stub")

    with get_connection(tmp_db) as conn:
        _insert_file(conn, file_id, img_path)
        result = process_image(file_id, img_path, conn, with_thumbs=True, force=True)

    assert result["thumb"] is True
    # Stub should now be a real JPEG
    assert stub.read_bytes() != b"stub"


# ---------------------------------------------------------------------------
# Integration: indexer computes pHash during index_directory
# ---------------------------------------------------------------------------

def test_indexer_computes_phash_for_images(
    tmp_path: pathlib.Path, tmp_db: pathlib.Path, isolated_thumbs_dir: pathlib.Path
) -> None:
    from magpie.indexer import index_directory

    tree = tmp_path / "drive"
    tree.mkdir()
    _make_image(tree / "photo.png")
    (tree / "doc.txt").write_bytes(b"text file")

    with get_connection(tmp_db) as conn:
        index_directory(tree, drive_id=None, conn=conn, with_thumbs=False)

    with get_connection(tmp_db) as conn:
        img_row = conn.execute(
            "SELECT perceptual_hash FROM files WHERE path LIKE '%photo.png'"
        ).fetchone()
        txt_row = conn.execute(
            "SELECT perceptual_hash FROM files WHERE path LIKE '%doc.txt'"
        ).fetchone()

    assert img_row["perceptual_hash"] is not None
    assert txt_row["perceptual_hash"] is None


def test_indexer_generates_thumbnails_with_flag(
    tmp_path: pathlib.Path, tmp_db: pathlib.Path, isolated_thumbs_dir: pathlib.Path
) -> None:
    from magpie.indexer import index_directory

    tree = tmp_path / "drive"
    tree.mkdir()
    _make_image(tree / "photo.png", size=(512, 512))

    with get_connection(tmp_db) as conn:
        index_directory(tree, drive_id=None, conn=conn, with_thumbs=True)

    thumbs = list(isolated_thumbs_dir.glob("*.jpg"))
    assert len(thumbs) == 1

    with Image.open(thumbs[0]) as img:
        assert max(img.size) <= 256


def test_indexer_no_thumbnails_without_flag(
    tmp_path: pathlib.Path, tmp_db: pathlib.Path, isolated_thumbs_dir: pathlib.Path
) -> None:
    from magpie.indexer import index_directory

    tree = tmp_path / "drive"
    tree.mkdir()
    _make_image(tree / "photo.png")

    with get_connection(tmp_db) as conn:
        index_directory(tree, drive_id=None, conn=conn, with_thumbs=False)

    assert list(isolated_thumbs_dir.glob("*.jpg")) == []


def test_indexer_second_run_with_thumbs_creates_jpeg_after_skip(
    tmp_path: pathlib.Path, tmp_db: pathlib.Path, isolated_thumbs_dir: pathlib.Path
) -> None:
    """Re-index hits the mtime fast-path; --with-thumbs must still create thumbnails."""
    from magpie.indexer import index_directory

    tree = tmp_path / "drive"
    tree.mkdir()
    _make_image(tree / "photo.png", size=(400, 300))

    with get_connection(tmp_db) as conn:
        index_directory(tree, drive_id=None, conn=conn, with_thumbs=False)
    assert list(isolated_thumbs_dir.glob("*.jpg")) == []

    with get_connection(tmp_db) as conn:
        index_directory(tree, drive_id=None, conn=conn, with_thumbs=True)

    thumbs = list(isolated_thumbs_dir.glob("*.jpg"))
    assert len(thumbs) == 1


def test_indexer_skips_phash_on_unchanged_file(
    tmp_path: pathlib.Path, tmp_db: pathlib.Path, isolated_thumbs_dir: pathlib.Path
) -> None:
    """Second index run on unchanged file skips re-hashing; pHash set on first run stays."""
    from magpie.indexer import index_directory

    tree = tmp_path / "drive"
    tree.mkdir()
    _make_image(tree / "photo.png")

    with get_connection(tmp_db) as conn:
        index_directory(tree, drive_id=None, conn=conn, with_thumbs=False)

    with get_connection(tmp_db) as conn:
        first_phash = conn.execute(
            "SELECT perceptual_hash FROM files WHERE path LIKE '%photo.png'"
        ).fetchone()["perceptual_hash"]

    # Second run — file unchanged, so it's skipped by the mtime check
    with get_connection(tmp_db) as conn:
        counts = index_directory(tree, drive_id=None, conn=conn, with_thumbs=False)

    assert counts["skipped"] == 1

    with get_connection(tmp_db) as conn:
        second_phash = conn.execute(
            "SELECT perceptual_hash FROM files WHERE path LIKE '%photo.png'"
        ).fetchone()["perceptual_hash"]

    assert first_phash == second_phash
