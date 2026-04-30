"""Tests for the file indexer (Task 1.3)."""

import pathlib
import sqlite3
import time

import blake3
import pytest
from typer.testing import CliRunner

from db.migrate import migrate
from magpie.cli import app
from magpie.db import get_connection
from magpie.indexer import (
    DEFAULT_PARTIAL_HASH_ABOVE_BYTES,
    PREFIX_B3_FULL,
    PREFIX_B3_PARTIAL,
    content_fingerprint,
    index_directory,
    scan_index_tree,
)


@pytest.fixture()
def tmp_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db_path = tmp_path / "magpie.db"
    migrate(db_path)
    return db_path


runner = CliRunner()


class _CommitCountingConnection:
    """Delegates to a sqlite3.Connection but counts ``commit()`` calls."""

    __slots__ = ("_conn", "commit_calls")

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.commit_calls = 0

    def commit(self) -> None:
        self.commit_calls += 1
        self._conn.commit()

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


# ---------------------------------------------------------------------------
# Unit tests for content_fingerprint
# ---------------------------------------------------------------------------

def test_content_fingerprint_small_file_full_prefix(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "hello.txt"
    f.write_bytes(b"hello world")
    expected = PREFIX_B3_FULL + blake3.blake3(b"hello world").hexdigest()
    assert content_fingerprint(f, f.stat().st_size, DEFAULT_PARTIAL_HASH_ABOVE_BYTES) == expected


def test_content_fingerprint_partial_for_large_file(tmp_path: pathlib.Path) -> None:
    """Above threshold uses b3p: without reading the full file."""
    f = tmp_path / "big.bin"
    body = b"A" * 5000 + b"B" * 5000
    f.write_bytes(body)
    st = f.stat()
    h = content_fingerprint(f, st.st_size, partial_hash_above_bytes=1024)
    assert h is not None
    assert h.startswith(PREFIX_B3_PARTIAL)
    h2 = content_fingerprint(f, st.st_size, partial_hash_above_bytes=1024)
    assert h == h2


def test_content_fingerprint_always_full_when_threshold_zero(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 10_000)
    st = f.stat()
    h = content_fingerprint(f, st.st_size, partial_hash_above_bytes=0)
    assert h == PREFIX_B3_FULL + blake3.blake3(f.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Unit tests for index_directory
# ---------------------------------------------------------------------------

def _make_tree(base: pathlib.Path) -> list[pathlib.Path]:
    """Create a small file tree and return the file paths."""
    files = [
        base / "doc.txt",
        base / "subdir" / "photo.jpg",
        base / "subdir" / "notes.md",
    ]
    (base / "subdir").mkdir()
    for i, f in enumerate(files):
        f.write_bytes(f"content {i}".encode())
    return files


def test_index_periodic_commits(tmp_path: pathlib.Path, tmp_db: pathlib.Path) -> None:
    """index_directory should call conn.commit every commit_interval files."""
    tree_root = tmp_path / "drive"
    tree_root.mkdir()
    for i in range(5):
        (tree_root / f"f{i}.txt").write_bytes(b"x" * (i + 1))

    raw = sqlite3.connect(tmp_db)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA journal_mode=WAL;")
    raw.execute("PRAGMA foreign_keys=ON;")
    proxy = _CommitCountingConnection(raw)
    try:
        index_directory(tree_root, drive_id=None, conn=proxy, commit_interval=2)  # type: ignore[arg-type]
    finally:
        raw.close()

    # Mid-run commits (after 2nd and 4th file) + tail flush
    assert proxy.commit_calls >= 3

    with get_connection(tmp_db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert n == 5


def test_index_commit_interval_zero_no_mid_commits(tmp_path: pathlib.Path, tmp_db: pathlib.Path) -> None:
    tree_root = tmp_path / "drive"
    tree_root.mkdir()
    (tree_root / "a.txt").write_bytes(b"a")

    raw = sqlite3.connect(tmp_db)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA journal_mode=WAL;")
    raw.execute("PRAGMA foreign_keys=ON;")
    proxy = _CommitCountingConnection(raw)
    try:
        index_directory(tree_root, drive_id=None, conn=proxy, commit_interval=0)  # type: ignore[arg-type]
        assert proxy.commit_calls == 0
        raw.commit()
    finally:
        raw.close()

    with get_connection(tmp_db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert n == 1


def test_scan_index_tree_matches_indexed_files(tmp_path: pathlib.Path, tmp_db: pathlib.Path) -> None:
    """Pre-scan file count and byte total should match what the indexer visits."""
    tree_root = tmp_path / "drive"
    tree_root.mkdir()
    files = _make_tree(tree_root)
    file_count, total_bytes = scan_index_tree(tree_root)
    assert file_count == len(files)
    assert total_bytes == sum(f.stat().st_size for f in files)

    with get_connection(tmp_db) as conn:
        index_directory(tree_root, drive_id=None, conn=conn)

    with get_connection(tmp_db) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS b FROM files"
        ).fetchone()
    assert row["n"] == file_count
    assert row["b"] == total_bytes


def test_index_creates_file_records(tmp_path: pathlib.Path, tmp_db: pathlib.Path) -> None:
    tree_root = tmp_path / "drive"
    tree_root.mkdir()
    files = _make_tree(tree_root)

    with get_connection(tmp_db) as conn:
        counts = index_directory(tree_root, drive_id=None, conn=conn)

    assert counts["indexed"] == len(files)
    assert counts["skipped"] == 0
    assert counts["missing"] == 0

    with get_connection(tmp_db) as conn:
        rows = conn.execute("SELECT path, status, content_hash, mime_type FROM files").fetchall()

    assert len(rows) == len(files)
    for row in rows:
        assert row["status"] == "present"
        assert row["content_hash"] is not None
        assert row["content_hash"].startswith(PREFIX_B3_FULL)
        assert len(row["content_hash"]) == len(PREFIX_B3_FULL) + 64


def test_index_recomputes_when_content_hash_not_prefixed(tmp_path: pathlib.Path, tmp_db: pathlib.Path) -> None:
    """Bare hex or other values must not skip; only ``b3f:`` / ``b3p:`` counts as indexed."""
    tree_root = tmp_path / "drive"
    tree_root.mkdir()
    f = tree_root / "a.txt"
    f.write_bytes(b"unchanged")

    with get_connection(tmp_db) as conn:
        index_directory(tree_root, drive_id=None, conn=conn)
        conn.execute(
            "UPDATE files SET content_hash = ? WHERE path = ?",
            ("a" * 64, str(f)),
        )
        conn.commit()

    with get_connection(tmp_db) as conn:
        counts = index_directory(tree_root, drive_id=None, conn=conn)

    assert counts["skipped"] == 0
    assert counts["indexed"] == 1
    with get_connection(tmp_db) as conn:
        h = conn.execute("SELECT content_hash FROM files WHERE path = ?", (str(f),)).fetchone()["content_hash"]
    assert h.startswith(PREFIX_B3_FULL)


def test_index_idempotent(tmp_path: pathlib.Path, tmp_db: pathlib.Path) -> None:
    """Running index twice on unchanged files should skip on second run."""
    tree_root = tmp_path / "drive"
    tree_root.mkdir()
    _make_tree(tree_root)

    with get_connection(tmp_db) as conn:
        counts1 = index_directory(tree_root, drive_id=None, conn=conn)

    with get_connection(tmp_db) as conn:
        counts2 = index_directory(tree_root, drive_id=None, conn=conn)

    assert counts1["indexed"] == 3
    assert counts2["indexed"] == 0
    assert counts2["skipped"] == 3

    # Only one record per file
    with get_connection(tmp_db) as conn:
        total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert total == 3


def test_index_marks_missing(tmp_path: pathlib.Path, tmp_db: pathlib.Path) -> None:
    """Files removed between runs should be marked status='missing'."""
    tree_root = tmp_path / "drive"
    tree_root.mkdir()
    files = _make_tree(tree_root)

    with get_connection(tmp_db) as conn:
        index_directory(tree_root, drive_id=None, conn=conn)

    # Delete one file
    files[0].unlink()

    with get_connection(tmp_db) as conn:
        index_directory(tree_root, drive_id=None, conn=conn)

    with get_connection(tmp_db) as conn:
        missing = conn.execute(
            "SELECT path FROM files WHERE status = 'missing'"
        ).fetchall()

    assert len(missing) == 1
    assert missing[0]["path"] == str(files[0])


def test_index_rehashes_modified_file(tmp_path: pathlib.Path, tmp_db: pathlib.Path) -> None:
    """A file whose mtime changed should be re-hashed."""
    tree_root = tmp_path / "drive"
    tree_root.mkdir()
    f = tree_root / "file.txt"
    f.write_bytes(b"original content")

    with get_connection(tmp_db) as conn:
        index_directory(tree_root, drive_id=None, conn=conn)

    with get_connection(tmp_db) as conn:
        orig_hash = conn.execute("SELECT content_hash FROM files WHERE path = ?", (str(f),)).fetchone()["content_hash"]

    # Modify the file (ensure mtime actually changes)
    time.sleep(0.01)
    f.write_bytes(b"new content")
    # Force mtime update
    f.touch()

    with get_connection(tmp_db) as conn:
        index_directory(tree_root, drive_id=None, conn=conn)

    with get_connection(tmp_db) as conn:
        new_hash = conn.execute("SELECT content_hash FROM files WHERE path = ?", (str(f),)).fetchone()["content_hash"]

    assert new_hash == PREFIX_B3_FULL + blake3.blake3(b"new content").hexdigest()
    assert new_hash != orig_hash


def test_index_assigns_drive_id(tmp_path: pathlib.Path, tmp_db: pathlib.Path) -> None:
    import uuid
    drive_id = str(uuid.uuid4())
    tree_root = tmp_path / "drive"
    tree_root.mkdir()
    (tree_root / "a.txt").write_bytes(b"hi")

    # Insert a fake drive so FK constraint is satisfied
    from datetime import datetime, timezone
    with get_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO drives (id, label, last_seen_at) VALUES (?, ?, ?)",
            (drive_id, "Test", datetime.now(timezone.utc).isoformat()),
        )
        index_directory(tree_root, drive_id=drive_id, conn=conn)

    with get_connection(tmp_db) as conn:
        row = conn.execute("SELECT drive_id FROM files").fetchone()
    assert row["drive_id"] == drive_id


# ---------------------------------------------------------------------------
# CLI: magpie index
# ---------------------------------------------------------------------------

def test_cli_index_runs(tmp_path: pathlib.Path, tmp_db: pathlib.Path) -> None:
    tree_root = tmp_path / "drive"
    tree_root.mkdir()
    (tree_root / "sample.txt").write_bytes(b"sample")

    result = runner.invoke(app, ["index", str(tree_root), "--db", str(tmp_db)])
    assert result.exit_code == 0
    assert "indexed" in result.output.lower()


def test_cli_index_nonexistent_path(tmp_db: pathlib.Path) -> None:
    result = runner.invoke(app, ["index", "/nonexistent/xyz", "--db", str(tmp_db)])
    assert result.exit_code != 0
