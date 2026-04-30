"""Tests for exact and near-duplicate detection."""

import pathlib
import sqlite3
import uuid
from datetime import datetime

import pytest

from db.migrate import migrate
from magpie.dupes import (
    _pick_keeper,
    find_exact_duplicates,
    find_near_duplicates,
    hamming_distance,
    populate_exact_duplicate_groups,
    populate_near_duplicate_groups,
)


@pytest.fixture()
def tmp_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db_path = tmp_path / "magpie.db"
    migrate(db_path)
    return db_path


def _insert_file(
    conn: sqlite3.Connection,
    *,
    file_id: str | None = None,
    path: str = "/fake/file.txt",
    content_hash: str = "abc123",
    status: str = "present",
    size_bytes: int = 1000,
    created_at: str | None = None,
    perceptual_hash: str | None = None,
) -> str:
    fid = file_id or str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO files (id, path, indexed_at, content_hash, status, size_bytes,
                           created_at, perceptual_hash)
        VALUES (?, ?, datetime('now'), ?, ?, ?, ?, ?)
        """,
        (fid, path, content_hash, status, size_bytes, created_at, perceptual_hash),
    )
    return fid


def test_find_exact_duplicates_returns_groups(tmp_db: pathlib.Path) -> None:
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    hash_a = "aaa"
    f1 = _insert_file(conn, path="/a/1.txt", content_hash=hash_a)
    f2 = _insert_file(conn, path="/a/2.txt", content_hash=hash_a)
    _insert_file(conn, path="/a/3.txt", content_hash="unique")
    conn.commit()
    conn.close()

    groups = find_exact_duplicates(tmp_db)
    assert hash_a in groups
    assert len(groups[hash_a]) == 2
    ids = {m["id"] for m in groups[hash_a]}
    assert ids == {f1, f2}
    # unique file should not appear
    assert "unique" not in groups


def test_find_exact_duplicates_ignores_non_present(tmp_db: pathlib.Path) -> None:
    conn = sqlite3.connect(tmp_db)
    h = "dup_hash"
    _insert_file(conn, path="/a/1.txt", content_hash=h, status="present")
    _insert_file(conn, path="/a/2.txt", content_hash=h, status="trashed")
    conn.commit()
    conn.close()

    groups = find_exact_duplicates(tmp_db)
    assert h not in groups  # only one present copy → not a duplicate


def test_populate_exact_creates_rows_and_marks_keeper(tmp_db: pathlib.Path) -> None:
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    h = "shared"
    f1 = _insert_file(conn, path="/a/1.txt", content_hash=h, created_at="2020-01-01")
    f2 = _insert_file(conn, path="/a/2.txt", content_hash=h, created_at="2022-06-01")
    conn.commit()
    conn.close()

    num = populate_exact_duplicate_groups(tmp_db)
    assert num == 1

    conn2 = sqlite3.connect(tmp_db)
    conn2.row_factory = sqlite3.Row
    rows = conn2.execute(
        "SELECT file_id, recommended_keep FROM duplicate_groups WHERE match_type='exact'"
    ).fetchall()
    conn2.close()

    assert len(rows) == 2
    keepers = [r["file_id"] for r in rows if r["recommended_keep"] == 1]
    assert keepers == [f1]  # earliest created_at wins


def test_populate_exact_is_idempotent(tmp_db: pathlib.Path) -> None:
    conn = sqlite3.connect(tmp_db)
    h = "dup"
    _insert_file(conn, path="/a/1.txt", content_hash=h)
    _insert_file(conn, path="/a/2.txt", content_hash=h)
    conn.commit()
    conn.close()

    populate_exact_duplicate_groups(tmp_db)
    populate_exact_duplicate_groups(tmp_db)

    conn2 = sqlite3.connect(tmp_db)
    count = conn2.execute(
        "SELECT COUNT(*) FROM duplicate_groups WHERE match_type='exact'"
    ).fetchone()[0]
    conn2.close()
    assert count == 2  # still exactly 2 rows, not 4


def test_pick_keeper_prefers_earliest_created_at() -> None:
    members = [
        {"id": "a", "created_at": "2022-01-01", "perceptual_hash": None, "size_bytes": 500},
        {"id": "b", "created_at": "2020-01-01", "perceptual_hash": None, "size_bytes": 500},
    ]
    assert _pick_keeper(members) == "b"


def test_pick_keeper_prefers_phash_presence_when_dates_equal() -> None:
    members = [
        {"id": "a", "created_at": "2021-01-01", "perceptual_hash": None, "size_bytes": 500},
        {"id": "b", "created_at": "2021-01-01", "perceptual_hash": "0xdeadbeef", "size_bytes": 500},
    ]
    assert _pick_keeper(members) == "b"


def test_pick_keeper_prefers_larger_file_as_tiebreaker() -> None:
    members = [
        {"id": "a", "created_at": None, "perceptual_hash": None, "size_bytes": 100},
        {"id": "b", "created_at": None, "perceptual_hash": None, "size_bytes": 9999},
    ]
    assert _pick_keeper(members) == "b"


def test_populate_exact_no_duplicates_returns_zero(tmp_db: pathlib.Path) -> None:
    conn = sqlite3.connect(tmp_db)
    _insert_file(conn, path="/a/1.txt", content_hash="unique1")
    _insert_file(conn, path="/a/2.txt", content_hash="unique2")
    conn.commit()
    conn.close()

    assert populate_exact_duplicate_groups(tmp_db) == 0


# ---------------------------------------------------------------------------
# Near-duplicate tests
# ---------------------------------------------------------------------------

def _phash_with_distance(base: int, distance: int) -> str:
    """Return a hex pHash that differs from *base* by exactly *distance* bits."""
    # Flip the lowest *distance* bits
    mask = (1 << distance) - 1
    return format(base ^ mask, "016x")


def test_hamming_distance_identical() -> None:
    assert hamming_distance(0xFFFF, 0xFFFF) == 0


def test_hamming_distance_one_bit() -> None:
    assert hamming_distance(0b0000, 0b0001) == 1


def test_hamming_distance_all_bits() -> None:
    # 64-bit all-ones XOR all-zeros = 64
    assert hamming_distance(0, (1 << 64) - 1) == 64


def test_find_near_duplicates_detects_close_pair(tmp_db: pathlib.Path) -> None:
    base = 0xFFFF_FFFF_0000_0000
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    f1 = _insert_file(conn, path="/img/1.jpg", perceptual_hash=format(base, "016x"))
    f2 = _insert_file(
        conn, path="/img/2.jpg", perceptual_hash=_phash_with_distance(base, 5)
    )
    conn.commit()
    conn.close()

    groups = find_near_duplicates(tmp_db, threshold=10)
    assert len(groups) == 1
    ids = {fid for fid, _ in groups[0]}
    assert ids == {f1, f2}


def test_find_near_duplicates_respects_threshold(tmp_db: pathlib.Path) -> None:
    base = 0xAAAA_AAAA_AAAA_AAAA
    conn = sqlite3.connect(tmp_db)
    # distance=15 > threshold=10 → should NOT match
    _insert_file(conn, path="/img/1.jpg", perceptual_hash=format(base, "016x"))
    _insert_file(
        conn, path="/img/2.jpg", perceptual_hash=_phash_with_distance(base, 15)
    )
    conn.commit()
    conn.close()

    groups = find_near_duplicates(tmp_db, threshold=10)
    assert len(groups) == 0


def test_find_near_duplicates_confidence_calculation(tmp_db: pathlib.Path) -> None:
    base = 0x1234_5678_9ABC_DEF0
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    f1 = _insert_file(conn, path="/img/1.jpg", perceptual_hash=format(base, "016x"))
    # distance = 8 → confidence = 1.0 - 8/64 = 0.875
    f2 = _insert_file(
        conn, path="/img/2.jpg", perceptual_hash=_phash_with_distance(base, 8)
    )
    conn.commit()
    conn.close()

    groups = find_near_duplicates(tmp_db, threshold=10)
    assert len(groups) == 1
    conf_map = {fid: conf for fid, conf in groups[0]}
    # Both files should have confidence ≈ 0.875 (within rounding)
    assert abs(conf_map[f1] - 0.875) < 0.001
    assert abs(conf_map[f2] - 0.875) < 0.001


def test_find_near_duplicates_skips_exact_group_members(tmp_db: pathlib.Path) -> None:
    base = 0xDEAD_BEEF_CAFE_BABE
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    # Two files that are byte-identical (exact dupes) AND visually similar
    f1 = _insert_file(
        conn, path="/img/1.jpg",
        content_hash="samehash",
        perceptual_hash=format(base, "016x"),
    )
    f2 = _insert_file(
        conn, path="/img/2.jpg",
        content_hash="samehash",
        perceptual_hash=_phash_with_distance(base, 3),
    )
    conn.commit()
    conn.close()

    # Populate exact groups first so f1 and f2 are excluded from near search
    populate_exact_duplicate_groups(tmp_db)
    groups = find_near_duplicates(tmp_db, threshold=10)
    assert len(groups) == 0  # both skipped because they're in an exact group


def test_populate_near_duplicate_groups_writes_db(tmp_db: pathlib.Path) -> None:
    base = 0x0F0F_0F0F_0F0F_0F0F
    conn = sqlite3.connect(tmp_db)
    _insert_file(conn, path="/img/a.jpg", perceptual_hash=format(base, "016x"))
    _insert_file(
        conn, path="/img/b.jpg", perceptual_hash=_phash_with_distance(base, 2)
    )
    conn.commit()
    conn.close()

    num = populate_near_duplicate_groups(tmp_db, threshold=10)
    assert num == 1

    conn2 = sqlite3.connect(tmp_db)
    rows = conn2.execute(
        "SELECT * FROM duplicate_groups WHERE match_type='near'"
    ).fetchall()
    conn2.close()
    assert len(rows) == 2


def test_populate_near_duplicate_groups_is_idempotent(tmp_db: pathlib.Path) -> None:
    base = 0x1111_2222_3333_4444
    conn = sqlite3.connect(tmp_db)
    _insert_file(conn, path="/img/a.jpg", perceptual_hash=format(base, "016x"))
    _insert_file(
        conn, path="/img/b.jpg", perceptual_hash=_phash_with_distance(base, 4)
    )
    conn.commit()
    conn.close()

    populate_near_duplicate_groups(tmp_db, threshold=10)
    populate_near_duplicate_groups(tmp_db, threshold=10)

    conn2 = sqlite3.connect(tmp_db)
    count = conn2.execute(
        "SELECT COUNT(*) FROM duplicate_groups WHERE match_type='near'"
    ).fetchone()[0]
    conn2.close()
    assert count == 2  # still 2 rows, not 4


def test_find_near_duplicates_transitive_grouping(tmp_db: pathlib.Path) -> None:
    """A→B and B→C within threshold should form a single group {A,B,C}."""
    # A and C are distance 8 apart via B (each 4 bits from B)
    base = 0x0000_0000_0000_0000
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    f_a = _insert_file(conn, path="/img/a.jpg", perceptual_hash=format(base, "016x"))
    # B is 4 bits from A
    hash_b = base ^ 0b1111
    f_b = _insert_file(conn, path="/img/b.jpg", perceptual_hash=format(hash_b, "016x"))
    # C is 4 bits from B (8 bits from A total, still within threshold=10)
    hash_c = hash_b ^ (0b1111 << 4)
    f_c = _insert_file(conn, path="/img/c.jpg", perceptual_hash=format(hash_c, "016x"))
    conn.commit()
    conn.close()

    groups = find_near_duplicates(tmp_db, threshold=10)
    assert len(groups) == 1
    ids = {fid for fid, _ in groups[0]}
    assert ids == {f_a, f_b, f_c}


def test_find_near_duplicates_ignores_missing_files(tmp_db: pathlib.Path) -> None:
    base = 0xABCD_EF01_2345_6789
    conn = sqlite3.connect(tmp_db)
    _insert_file(conn, path="/img/a.jpg", perceptual_hash=format(base, "016x"), status="present")
    _insert_file(
        conn, path="/img/b.jpg",
        perceptual_hash=_phash_with_distance(base, 3),
        status="missing",
    )
    conn.commit()
    conn.close()

    groups = find_near_duplicates(tmp_db, threshold=10)
    assert len(groups) == 0  # only one present file → no pair


def test_find_near_duplicates_batch_size_one(tmp_db: pathlib.Path) -> None:
    """batch_size=1 forces cross-batch comparisons; results must match batch_size=500."""
    base = 0xF0F0_F0F0_F0F0_F0F0
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    f1 = _insert_file(conn, path="/img/1.jpg", perceptual_hash=format(base, "016x"))
    f2 = _insert_file(
        conn, path="/img/2.jpg", perceptual_hash=_phash_with_distance(base, 6)
    )
    conn.commit()
    conn.close()

    groups_small = find_near_duplicates(tmp_db, threshold=10, batch_size=1)
    groups_large = find_near_duplicates(tmp_db, threshold=10, batch_size=500)
    ids_small = {fid for fid, _ in groups_small[0]}
    ids_large = {fid for fid, _ in groups_large[0]}
    assert ids_small == ids_large == {f1, f2}


# ---------------------------------------------------------------------------
# Preferred folder tests
# ---------------------------------------------------------------------------


def test_apply_preferred_folder_multiple_grandchildren(tmp_db: pathlib.Path) -> None:
    """Test that preferred folder logic keeps ALL files inside the preferred folder
    and only trashes files outside.

    When multiple files are inside a preferred folder but have different subdirectories,
    they should all be kept (no keeper selection). Files outside the preferred folder
    should be trashed.
    """
    from magpie.dupes import apply_preferred_folder_duplicate_resolution
    from datetime import datetime, timezone

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row

    # Create 3 files: 2 inside preferred folder (different grandchildren), 1 outside
    f1 = _insert_file(conn, path="/Photos/Selected/2024/01/photo.jpg", created_at="2020-01-01")
    f2 = _insert_file(conn, path="/Photos/Selected/2024/02/photo.jpg", created_at="2020-01-02")
    f3 = _insert_file(conn, path="/Photos/Backup/photo.jpg", created_at="2020-01-03")
    conn.commit()
    conn.close()

    # Populate exact duplicate groups
    populate_exact_duplicate_groups(tmp_db)

    # Mark /Photos/Selected as preferred
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        "INSERT INTO preferred_duplicate_paths (path_prefix, rank, created_at) VALUES (?, ?, ?)",
        ("/Photos/Selected", 1, now),
    )
    conn.commit()

    # Apply preferred folder logic
    apply_preferred_folder_duplicate_resolution(conn, now)

    # Verify: no keeper marked (all inside files are kept equally)
    keepers = conn.execute(
        "SELECT COUNT(*) as cnt FROM duplicate_groups WHERE recommended_keep = 1"
    ).fetchone()
    assert keepers["cnt"] == 0, "Should have no keeper (all inside files kept equally)"

    # Verify: f1 and f2 (inside) have NO trash label
    f1_trash = conn.execute(
        "SELECT action FROM labels WHERE file_id = ?", (f1,)
    ).fetchone()
    assert f1_trash is None, "f1 (inside) should not be trashed"

    f2_trash = conn.execute(
        "SELECT action FROM labels WHERE file_id = ?", (f2,)
    ).fetchone()
    assert f2_trash is None, "f2 (inside) should not be trashed"

    # Verify: f3 (outside) has trash label queued
    f3_trash = conn.execute(
        "SELECT action FROM labels WHERE file_id = ?", (f3,)
    ).fetchone()
    assert f3_trash and f3_trash["action"] == "trash", "f3 (outside) should be trashed"

    conn.close()


def test_apply_preferred_folder_all_inside_different_prefixes(tmp_db: pathlib.Path) -> None:
    """Test that preferred folder logic works when ALL duplicates are inside preferred folders
    but match different preferred paths (one more specific/prioritized than the other).

    This is the bug reported: two preferred folders with different ranks, two duplicate files
    both inside preferred folders but matching different preferred paths.
    The copy in the higher-priority (more specific) folder should be kept.
    """
    from magpie.dupes import apply_preferred_folder_duplicate_resolution
    from datetime import datetime, timezone

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row

    # Create 2 files: both under /Volumes/MyDrive/ (broader), but one also under
    # /Volumes/MyDrive/Photos/person2 (more specific, higher priority)
    f1 = _insert_file(
        conn,
        path="/Volumes/MyDrive/Photos/person1/2017/Trip_person2",
        created_at="2020-01-01",
    )
    f2 = _insert_file(
        conn,
        path="/Volumes/MyDrive/Photos/person2/2017/Trip",
        created_at="2020-01-02",
    )
    conn.commit()
    conn.close()

    # Populate exact duplicate groups
    populate_exact_duplicate_groups(tmp_db)

    # Mark both folders as preferred, with the more specific one having higher priority (rank 1)
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        "INSERT INTO preferred_duplicate_paths (path_prefix, rank, created_at) VALUES (?, ?, ?)",
        ("/Volumes/MyDrive/Photos/person2", 1, now),
    )
    conn.execute(
        "INSERT INTO preferred_duplicate_paths (path_prefix, rank, created_at) VALUES (?, ?, ?)",
        ("/Volumes/MyDrive", 2, now),
    )
    conn.commit()

    # Apply preferred folder logic
    apply_preferred_folder_duplicate_resolution(conn, now)

    # Verify: f2 (rank 1, higher priority) is marked as keeper
    keepers = conn.execute(
        "SELECT COUNT(*) as cnt FROM duplicate_groups WHERE recommended_keep = 1"
    ).fetchone()
    assert keepers["cnt"] == 1, "Should have exactly one keeper (f2 with rank 1)"

    f2_keeper = conn.execute(
        "SELECT recommended_keep FROM duplicate_groups WHERE file_id = ?", (f2,)
    ).fetchone()
    assert f2_keeper["recommended_keep"] == 1, "f2 should be marked as keeper (rank 1)"

    # Verify: f1 (rank 2, lower priority) is trashed
    f1_trash = conn.execute(
        "SELECT action FROM labels WHERE file_id = ?", (f1,)
    ).fetchone()
    assert f1_trash is not None and f1_trash["action"] == "trash", "f1 should be trashed (lower rank)"

    # Verify: f2 has no trash label
    f2_trash = conn.execute(
        "SELECT action FROM labels WHERE file_id = ?", (f2,)
    ).fetchone()
    assert f2_trash is None, "f2 should not be trashed (higher rank keeper)"

    conn.close()


def test_apply_preferred_folder_same_path_duplicates(tmp_db: pathlib.Path) -> None:
    """Test that duplicate files with identical paths (e.g., Google Drive duplicates)
    are NOT auto-trashed when both are inside the same preferred folder.

    When two files with the exact same path are exact duplicates and both are under
    a preferred folder with no hierarchy to distinguish them, both should be kept
    for manual review (neither should be automatically trashed).

    This handles the case reported: /gdrive/Google Drive/sunset1.psd appearing twice.
    """
    from magpie.dupes import apply_preferred_folder_duplicate_resolution
    from datetime import datetime, timezone

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row

    # Create 2 files with IDENTICAL paths (like Google Drive duplicates)
    f1 = _insert_file(
        conn,
        path="/gdrive/Google Drive/sunset1.psd",
        created_at="2020-01-01",
    )
    f2 = _insert_file(
        conn,
        path="/gdrive/Google Drive/sunset1.psd",
        created_at="2020-01-02",
    )
    conn.commit()
    conn.close()

    # Populate exact duplicate groups
    populate_exact_duplicate_groups(tmp_db)

    # Mark /gdrive/Google Drive as preferred
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        "INSERT INTO preferred_duplicate_paths (path_prefix, rank, created_at) VALUES (?, ?, ?)",
        ("/gdrive/Google Drive", 1, now),
    )
    conn.commit()

    # Apply preferred folder logic
    apply_preferred_folder_duplicate_resolution(conn, now)

    # Verify: NO keeper should be set (both files left for manual review)
    keepers = conn.execute(
        "SELECT COUNT(*) as cnt FROM duplicate_groups WHERE recommended_keep = 1"
    ).fetchone()
    assert keepers["cnt"] == 0, "Should have no keeper (identical paths = ambiguous)"

    # Verify: NO trash labels should be queued (both files kept)
    trash_labels = conn.execute(
        "SELECT COUNT(*) as cnt FROM labels WHERE action = 'trash'"
    ).fetchone()
    assert trash_labels["cnt"] == 0, "Should have no trash labels for identical-path duplicates"

    conn.close()
