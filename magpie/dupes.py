"""Exact and near-duplicate detection and duplicate group management."""

import pathlib
import sqlite3
import uuid
from typing import Iterator, Optional

import typer

from magpie.db import DB_PATH, get_connection


# ---------------------------------------------------------------------------
# Preferred-folder duplicate resolution
# ---------------------------------------------------------------------------


def normalize_path_prefix(path: str) -> str:
    """Normalize a folder path for storage and matching (directory boundary safe)."""
    p = (path or "").strip()
    if not p:
        return p
    if p != "/" and p.endswith("/"):
        p = p.rstrip("/")
    return p


def file_path_under_prefix(file_path: str, prefix: str) -> bool:
    """True if *file_path* is *prefix* or a file/dir under it."""
    if not file_path or not prefix:
        return False
    if file_path == prefix:
        return True
    return file_path.startswith(prefix + "/")


def longest_matching_preferred_prefix(file_path: str, prefixes_longest_first: list[str]) -> Optional[str]:
    for pref in prefixes_longest_first:
        if file_path_under_prefix(file_path, pref):
            return pref
    return None


def fetch_preferred_duplicate_prefixes(conn: sqlite3.Connection) -> list[str]:
    """Return preferred path prefixes, ordered by rank (1 = highest priority).

    When multiple prefixes match a file, the rank determines priority. Within the same
    rank bucket, longer paths take precedence (e.g., /Photos/Selected over /Photos).
    """
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='preferred_duplicate_paths' LIMIT 1"
    ).fetchone():
        return []
    rows = conn.execute(
        "SELECT path_prefix FROM preferred_duplicate_paths ORDER BY rank ASC"
    ).fetchall()
    prefs = [str(r["path_prefix"]) for r in rows]
    return prefs


def get_next_available_rank(conn: sqlite3.Connection) -> int:
    """Return the next available rank for a new preferred path (count + 1)."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='preferred_duplicate_paths' LIMIT 1"
    ).fetchone():
        return 1
    row = conn.execute("SELECT COUNT(*) as cnt FROM preferred_duplicate_paths").fetchone()
    return (row["cnt"] or 0) + 1


def set_preferred_path_rank(conn: sqlite3.Connection, path_prefix: str, new_rank: int) -> None:
    """Update a preferred path's rank, shifting others as needed.

    If a folder at rank 2 is moved to rank 5, folders at ranks 3–5 shift up to 2–4.
    All changes happen in a transaction.
    """
    # Normalize the path
    path_prefix = normalize_path_prefix(path_prefix)

    # Get current rank for this path
    row = conn.execute(
        "SELECT rank FROM preferred_duplicate_paths WHERE path_prefix = ?",
        (path_prefix,)
    ).fetchone()
    if not row:
        raise ValueError(f"Path {path_prefix} is not in preferred_duplicate_paths")

    old_rank = row["rank"]
    if old_rank == new_rank:
        return  # No change needed

    # Shift folders between old and new rank
    if old_rank < new_rank:
        # Moving down: folders at old_rank+1 to new_rank shift up by 1
        conn.execute(
            "UPDATE preferred_duplicate_paths SET rank = rank - 1 WHERE rank > ? AND rank <= ?",
            (old_rank, new_rank)
        )
    else:
        # Moving up: folders at new_rank to old_rank-1 shift down by 1
        conn.execute(
            "UPDATE preferred_duplicate_paths SET rank = rank + 1 WHERE rank >= ? AND rank < ?",
            (new_rank, old_rank)
        )

    # Set the new rank for this path
    conn.execute(
        "UPDATE preferred_duplicate_paths SET rank = ? WHERE path_prefix = ?",
        (new_rank, path_prefix)
    )


def upsert_pending_user_trash_label(conn: sqlite3.Connection, file_id: str, labeled_at: str) -> None:
    conn.execute(
        """
        INSERT INTO labels (file_id, action, source, labeled_at)
        VALUES (?, 'trash', 'user', ?)
        ON CONFLICT(file_id) DO UPDATE SET
            action = 'trash',
            source = 'user',
            confidence = NULL,
            reasoning = NULL,
            labeled_at = excluded.labeled_at,
            approved_at = NULL
        """,
        (file_id, labeled_at),
    )


def queue_trash_nonkeeper_unless_already_trash(
    conn: sqlite3.Connection, file_id: str, labeled_at: str
) -> None:
    row = conn.execute("SELECT action FROM labels WHERE file_id = ?", (file_id,)).fetchone()
    if row and row["action"] == "trash":
        return
    upsert_pending_user_trash_label(conn, file_id, labeled_at)


def set_duplicate_group_keeper_and_queue_trash(
    conn: sqlite3.Connection,
    group_id: str,
    keeper_id: str,
    labeled_at: str,
) -> None:
    """Set *keeper_id* as recommended_keep=1 and queue pending trash for all other members.

    Skips upsert for files that already have a trash label (pending or approved) so
    approved queue entries are not reset.
    """
    member_rows = conn.execute(
        "SELECT file_id FROM duplicate_groups WHERE group_id = ?",
        (group_id,),
    ).fetchall()
    member_ids = [r["file_id"] for r in member_rows]
    if keeper_id not in member_ids:
        raise ValueError("keeper_id is not a member of this duplicate group")

    conn.execute(
        "UPDATE duplicate_groups SET recommended_keep = 0 WHERE group_id = ?",
        (group_id,),
    )
    conn.execute(
        """
        UPDATE duplicate_groups SET recommended_keep = 1
        WHERE group_id = ? AND file_id = ?
        """,
        (group_id, keeper_id),
    )
    conn.execute(
        "DELETE FROM labels WHERE file_id = ? AND action = 'trash'",
        (keeper_id,),
    )
    for mid in member_ids:
        if mid == keeper_id:
            continue
        queue_trash_nonkeeper_unless_already_trash(conn, mid, labeled_at)


def _get_preferred_path_rank(conn: sqlite3.Connection, path_prefix: str) -> int | None:
    """Return the rank of a preferred path, or None if not found."""
    row = conn.execute(
        "SELECT rank FROM preferred_duplicate_paths WHERE path_prefix = ?",
        (path_prefix,)
    ).fetchone()
    return row["rank"] if row else None


def apply_preferred_folder_duplicate_resolution(conn: sqlite3.Connection, labeled_at: str) -> int:
    """Resolve exact-duplicate groups using preferred path prefixes.

    Only applies to exact duplicates (byte-identical files). Near duplicates are excluded.

    * Files in lower-ranked preferred folders (higher rank number) are marked trash.
    * Files in the highest-ranked (lowest rank number) preferred folder are kept.
    * Files outside all preferred prefixes are marked trash.
    * If multiple copies tie for the highest rank, all are kept.

    Returns the number of groups that were changed (trash queued or flags cleared).
    """
    prefixes = fetch_preferred_duplicate_prefixes(conn)
    if not prefixes:
        return 0

    group_rows = conn.execute(
        """
        SELECT DISTINCT group_id
        FROM duplicate_groups
        WHERE match_type = 'exact'
          AND all_labels_approved_at IS NULL
        """
    ).fetchall()

    modified = 0
    for (gid,) in group_rows:
        members = conn.execute(
            """
            SELECT dg.file_id, dg.recommended_keep, f.path, f.created_at,
                   f.perceptual_hash, f.size_bytes
            FROM duplicate_groups dg
            JOIN files f ON f.id = dg.file_id
            WHERE dg.group_id = ?
            """,
            (gid,),
        ).fetchall()
        if len(members) < 2:
            continue

        inside: list[sqlite3.Row] = []
        outside: list[sqlite3.Row] = []
        for r in members:
            p = r["path"] or ""
            if longest_matching_preferred_prefix(p, prefixes):
                inside.append(r)
            else:
                outside.append(r)

        if not inside:
            continue

        group_changed = False

        # Among inside files, determine which have the best (lowest) rank
        inside_with_rank = []
        for r in inside:
            matched_prefix = longest_matching_preferred_prefix(r["path"] or "", prefixes)
            rank = _get_preferred_path_rank(conn, matched_prefix) if matched_prefix else None
            inside_with_rank.append((r, rank))

        # Find the best rank among inside files
        best_rank = min(
            (rank for _, rank in inside_with_rank if rank is not None),
            default=None
        )

        # Classify inside files by rank
        true_keepers = [r for r, rank in inside_with_rank if rank == best_rank]
        lower_ranked_inside = [r for r, rank in inside_with_rank if rank is not None and rank != best_rank]

        # Trash all files outside preferred folders
        for r in outside:
            fid = r["file_id"]
            lbl = conn.execute(
                "SELECT action FROM labels WHERE file_id = ?",
                (fid,),
            ).fetchone()
            if lbl and lbl["action"] == "trash":
                continue
            queue_trash_nonkeeper_unless_already_trash(conn, fid, labeled_at)
            group_changed = True

        # Trash lower-ranked inside files (same logic as outside files)
        for r in lower_ranked_inside:
            fid = r["file_id"]
            lbl = conn.execute(
                "SELECT action FROM labels WHERE file_id = ?",
                (fid,),
            ).fetchone()
            if lbl and lbl["action"] == "trash":
                continue
            queue_trash_nonkeeper_unless_already_trash(conn, fid, labeled_at)
            group_changed = True

        # If exactly one true keeper, mark it as recommended_keep=1 and trash everything else
        if len(true_keepers) == 1:
            keeper_fid = true_keepers[0]["file_id"]
            set_duplicate_group_keeper_and_queue_trash(conn, gid, keeper_fid, labeled_at)
            group_changed = True
        elif len(true_keepers) > 1:
            # Multiple tied keepers: clear any existing keeper flag (don't auto-pick a winner)
            conn.execute(
                "UPDATE duplicate_groups SET recommended_keep = 0 WHERE group_id = ?",
                (gid,),
            )
            group_changed = True
        elif group_changed:
            # No true keepers but other files were trashed: clear recommended_keep
            conn.execute(
                "UPDATE duplicate_groups SET recommended_keep = 0 WHERE group_id = ?",
                (gid,),
            )

        if group_changed:
            modified += 1

    return modified


# ---------------------------------------------------------------------------
# Hamming distance helpers
# ---------------------------------------------------------------------------

def _phash_to_int(hex_str: str) -> int:
    """Convert a hex pHash string (e.g. 'ff3a…') to an integer for bit ops."""
    return int(hex_str, 16)


def hamming_distance(a: int, b: int) -> int:
    """Return the number of differing bits between two integers."""
    return bin(a ^ b).count("1")


# ---------------------------------------------------------------------------
# Union-Find (path-compressed, union-by-rank)
# ---------------------------------------------------------------------------

class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._rank: dict[str, int] = {}

    def find(self, x: str) -> str:
        if x not in self._parent:
            self._parent[x] = x
            self._rank[x] = 0
        if self._parent[x] != x:
            self._parent[x] = self.find(self._parent[x])
        return self._parent[x]

    def union(self, x: str, y: str) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self._rank[rx] < self._rank[ry]:
            rx, ry = ry, rx
        self._parent[ry] = rx
        if self._rank[rx] == self._rank[ry]:
            self._rank[rx] += 1

    def groups(self) -> dict[str, list[str]]:
        """Return mapping of root → [member, …] for groups with >1 member."""
        buckets: dict[str, list[str]] = {}
        for x in self._parent:
            root = self.find(x)
            buckets.setdefault(root, []).append(x)
        return {root: members for root, members in buckets.items() if len(members) > 1}


# ---------------------------------------------------------------------------
# Near-duplicate core
# ---------------------------------------------------------------------------

def _iter_phash_batches(
    db_path: pathlib.Path,
    batch_size: int,
) -> Iterator[list[tuple[str, int]]]:
    """Yield batches of (file_id, phash_int) for images with a pHash, in pages."""
    conn = get_connection(db_path)
    offset = 0
    while True:
        rows = conn.execute(
            """
            SELECT id, perceptual_hash
            FROM files
            WHERE status = 'present'
              AND perceptual_hash IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM labels l
                  WHERE l.file_id = files.id AND l.action = 'trash'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM duplicate_groups dg
                  WHERE dg.file_id = files.id AND dg.match_type = 'exact'
              )
            ORDER BY id
            LIMIT ? OFFSET ?
            """,
            (batch_size, offset),
        ).fetchall()
        if not rows:
            break
        yield [(row["id"], _phash_to_int(row["perceptual_hash"])) for row in rows]
        offset += batch_size
    conn.close()


def find_near_duplicates(
    db_path: pathlib.Path,
    threshold: int = 10,
    batch_size: int = 500,
) -> list[list[tuple[str, float]]]:
    """Return near-duplicate groups as lists of (file_id, confidence) pairs.

    Uses a batched streaming approach: each new page of pHashes is compared
    against the accumulated list of previously seen hashes. Memory use at any
    step is O(seen + batch_size) in pHash ints (8 bytes each) — no image data
    is loaded.

    Args:
        db_path: Path to the SQLite database.
        threshold: Maximum Hamming distance to consider a near-match (0–64).
        batch_size: Number of hashes to fetch per DB page.

    Returns:
        List of groups; each group is a list of (file_id, confidence) tuples
        where confidence = 1.0 − distance/64 for the closest pairing found.
    """
    uf = _UnionFind()
    # Track the best (minimum) distance seen for each pair so we can compute
    # confidence accurately when writing groups.
    best_dist: dict[frozenset[str], int] = {}

    # accumulated: list of (file_id, phash_int) for all pages processed so far
    accumulated: list[tuple[str, int]] = []

    for batch in _iter_phash_batches(db_path, batch_size):
        # Compare every item in this batch against all accumulated items
        for bid, bval in batch:
            for aid, aval in accumulated:
                dist = hamming_distance(aval, bval)
                if dist <= threshold:
                    uf.union(aid, bid)
                    key = frozenset((aid, bid))
                    if key not in best_dist or dist < best_dist[key]:
                        best_dist[key] = dist

        # Compare within this batch (pairs only, not self)
        for i in range(len(batch)):
            for j in range(i + 1, len(batch)):
                aid, aval = batch[i]
                bid, bval = batch[j]
                dist = hamming_distance(aval, bval)
                if dist <= threshold:
                    uf.union(aid, bid)
                    key = frozenset((aid, bid))
                    if key not in best_dist or dist < best_dist[key]:
                        best_dist[key] = dist

        accumulated.extend(batch)

    # Build result: for each group, compute per-file confidence as the minimum
    # distance that connects it to any group member (best match).
    raw_groups = uf.groups()
    result: list[list[tuple[str, float]]] = []
    for members in raw_groups.values():
        member_set = set(members)
        # Per-file minimum distance to any other group member
        file_min_dist: dict[str, int] = {fid: threshold for fid in members}
        for key, dist in best_dist.items():
            pair = list(key)
            if pair[0] in member_set and pair[1] in member_set:
                for fid in pair:
                    if dist < file_min_dist[fid]:
                        file_min_dist[fid] = dist
        group = [(fid, round(1.0 - file_min_dist[fid] / 64, 4)) for fid in members]
        result.append(group)

    return result


def populate_near_duplicate_groups(
    db_path: pathlib.Path,
    threshold: int = 10,
    batch_size: int = 500,
) -> int:
    """Detect and store near-duplicate image groups.

    Clears existing near-match entries first. Returns the number of groups written.
    """
    groups = find_near_duplicates(db_path, threshold=threshold, batch_size=batch_size)
    conn = get_connection(db_path)

    with conn:
        conn.execute("DELETE FROM duplicate_groups WHERE match_type = 'near'")
        for members in groups:
            group_id = str(uuid.uuid4())
            # Determine keeper: reuse _pick_keeper logic — fetch rows for these IDs
            file_ids = [fid for fid, _ in members]
            placeholders = ",".join("?" * len(file_ids))
            file_rows = conn.execute(
                f"SELECT id, created_at, perceptual_hash, size_bytes FROM files WHERE id IN ({placeholders})",
                file_ids,
            ).fetchall()
            keeper_id = _pick_keeper([dict(r) for r in file_rows])

            for fid, confidence in members:
                conn.execute(
                    """
                    INSERT INTO duplicate_groups
                        (group_id, file_id, match_type, confidence, recommended_keep)
                    VALUES (?, ?, 'near', ?, ?)
                    """,
                    (group_id, fid, confidence, 1 if fid == keeper_id else 0),
                )

    conn.close()
    return len(groups)

dupes_app = typer.Typer(
    name="dupes",
    help="Detect and review duplicate files.",
    no_args_is_help=True,
)


def _pick_keeper(group_members: list[dict]) -> str:
    """Return the file_id of the recommended keeper from a duplicate group.

    Priority:
    1. Earliest created_at (None sorts last)
    2. Most EXIF fields populated (mime_type image/* with non-null perceptual_hash as proxy)
    3. Largest size_bytes
    """
    def sort_key(m: dict) -> tuple:
        # created_at: earlier is better → use as-is (None → sorts last with a sentinel)
        created = m["created_at"] or "9999-99-99"
        # perceptual_hash presence as EXIF completeness proxy (1 = has it, 0 = doesn't)
        has_phash = 0 if m["perceptual_hash"] is None else 1
        # size: larger is better → negate for ascending sort
        size = -(m["size_bytes"] or 0)
        return (created, -has_phash, size)

    return min(group_members, key=sort_key)["id"]


def find_exact_duplicates(db_path: pathlib.Path) -> dict[str, list[dict]]:
    """Return a mapping of content_hash → list of file rows for all exact duplicate groups."""
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT id, path, size_bytes, created_at, perceptual_hash, content_hash
        FROM files
        WHERE status = 'present'
          AND content_hash IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM labels l
              WHERE l.file_id = files.id AND l.action = 'trash'
          )
        ORDER BY content_hash, created_at
        """
    ).fetchall()
    conn.close()

    groups: dict[str, list[dict]] = {}
    for row in rows:
        h = row["content_hash"]
        groups.setdefault(h, []).append(dict(row))

    return {h: members for h, members in groups.items() if len(members) > 1}


def populate_exact_duplicate_groups(db_path: pathlib.Path) -> int:
    """Populate duplicate_groups for rows sharing the same ``content_hash`` (``b3f:`` / ``b3p:``).

    Clears existing exact-match entries first, then re-inserts. Returns number
    of groups written.
    """
    groups = find_exact_duplicates(db_path)
    conn = get_connection(db_path)

    with conn:
        conn.execute("DELETE FROM duplicate_groups WHERE match_type = 'exact'")

        for members in groups.values():
            group_id = str(uuid.uuid4())
            keeper_id = _pick_keeper(members)
            for m in members:
                conn.execute(
                    """
                    INSERT INTO duplicate_groups
                        (group_id, file_id, match_type, confidence, recommended_keep)
                    VALUES (?, ?, 'exact', 1.0, ?)
                    """,
                    (group_id, m["id"], 1 if m["id"] == keeper_id else 0),
                )

    conn.close()
    return len(groups)


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} PB"


@dupes_app.command("find")
def find_cmd(
    exact: bool = typer.Option(False, "--exact", help="Find duplicates by identical content_hash (b3f:/b3p:)."),
    near: bool = typer.Option(False, "--near", help="Find near-duplicate images via pHash Hamming distance."),
    threshold: int = typer.Option(
        10, "--threshold", help="Max Hamming distance for near-duplicate matching (0–64)."
    ),
    batch_size: int = typer.Option(
        500, "--batch-size", hidden=True,
        help="Number of pHashes to load per DB page (memory tuning).",
    ),
    db: pathlib.Path = typer.Option(DB_PATH, help="Path to the magpie database."),
) -> None:
    """Scan for duplicates and populate duplicate_groups. Prints a summary."""
    if not exact and not near:
        typer.echo("Specify a detection mode: --exact and/or --near")
        raise typer.Exit(1)

    if exact:
        typer.echo("Scanning for exact duplicates…")
        num_groups = populate_exact_duplicate_groups(db)

        if num_groups == 0:
            typer.echo("No exact duplicates found.")
        else:
            conn = get_connection(db)
            rows = conn.execute(
                """
                SELECT dg.group_id,
                       COUNT(*) AS file_count,
                       SUM(f.size_bytes) AS total_bytes,
                       MIN(f.size_bytes) AS keeper_bytes
                FROM duplicate_groups dg
                JOIN files f ON f.id = dg.file_id
                WHERE dg.match_type = 'exact'
                GROUP BY dg.group_id
                """
            ).fetchall()
            conn.close()

            total_files = sum(r["file_count"] for r in rows)
            reclaimable = sum(r["total_bytes"] - r["keeper_bytes"] for r in rows)

            typer.echo(f"\n{'Groups':<12} {'Files':<12} {'Reclaimable'}")
            typer.echo("-" * 40)
            typer.echo(f"{num_groups:<12} {total_files:<12} {_fmt_bytes(reclaimable)}")

    if near:
        typer.echo(f"Scanning for near-duplicate images (threshold={threshold})…")
        num_groups = populate_near_duplicate_groups(db, threshold=threshold, batch_size=batch_size)

        if num_groups == 0:
            typer.echo("No near-duplicate image groups found.")
        else:
            conn = get_connection(db)
            rows = conn.execute(
                """
                SELECT dg.group_id,
                       COUNT(*) AS file_count,
                       AVG(dg.confidence) AS avg_confidence
                FROM duplicate_groups dg
                WHERE dg.match_type = 'near'
                GROUP BY dg.group_id
                """
            ).fetchall()
            conn.close()

            total_files = sum(r["file_count"] for r in rows)
            avg_conf = sum(r["avg_confidence"] for r in rows) / len(rows) if rows else 0.0

            typer.echo(f"\n{'Groups':<12} {'Files':<12} {'Avg confidence'}")
            typer.echo("-" * 40)
            typer.echo(f"{num_groups:<12} {total_files:<12} {avg_conf:.3f}")


@dupes_app.command("apply-preferred")
def apply_preferred_cmd(
    db: pathlib.Path = typer.Option(DB_PATH, help="Path to the magpie database."),
) -> None:
    """Trash out-of-folder copies; auto-keeper only for a unique deepest preferred path."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    conn = get_connection(db)
    try:
        n = apply_preferred_folder_duplicate_resolution(conn, now)
        conn.commit()
    finally:
        conn.close()
    typer.echo(
        f"Updated {n} duplicate group(s). "
        "Out-of-folder copies get pending trash; use the duplicate UI when several "
        "in-folder copies tie unless one matches a deeper preferred path. "
        "Approve trash in the action queue before executing."
    )


@dupes_app.command("list")
def list_cmd(
    match_type: Optional[str] = typer.Option(
        None, "--type", help="Filter by match type: exact or near."
    ),
    db: pathlib.Path = typer.Option(DB_PATH, help="Path to the magpie database."),
) -> None:
    """List all duplicate groups with their member files."""
    conn = get_connection(db)

    type_clause = ""
    params: list = []
    if match_type:
        type_clause = "AND dg.match_type = ?"
        params.append(match_type)

    rows = conn.execute(
        f"""
        SELECT dg.group_id, dg.match_type, dg.recommended_keep,
               f.id AS file_id, f.path, f.size_bytes, f.created_at
        FROM duplicate_groups dg
        JOIN files f ON f.id = dg.file_id
        WHERE 1=1 {type_clause}
        ORDER BY dg.group_id, dg.recommended_keep DESC
        """,
        params,
    ).fetchall()
    conn.close()

    if not rows:
        typer.echo("No duplicate groups found.")
        return

    current_group: Optional[str] = None
    for row in rows:
        if row["group_id"] != current_group:
            current_group = row["group_id"]
            typer.echo(f"\nGroup {current_group}  [{row['match_type']}]")
            typer.echo(f"  {'keep':<6} {'size':<12} {'created':<22} path")
            typer.echo(f"  {'-'*4}  {'-'*10}  {'-'*20}  {'-'*30}")
        keep_marker = "* " if row["recommended_keep"] else "  "
        size_str = _fmt_bytes(row["size_bytes"] or 0)
        created = (row["created_at"] or "")[:19]
        typer.echo(f"  {keep_marker}{size_str:<12} {created:<22} {row['path']}")
