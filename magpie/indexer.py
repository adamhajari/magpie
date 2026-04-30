"""File indexer — walk a directory tree and populate the files table."""

from __future__ import annotations

import mimetypes
import os
import pathlib
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional, TypeVar

import blake3
import typer
from rich.console import Console
from rich.filesize import decimal as _decimal_bytes
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text

from magpie.db import DB_PATH, get_connection

console = Console()

# After this many files fully processed (indexed or skipped), commit the SQLite transaction.
# Survives interrupt: work already committed is kept; the rest is redone on the next index run.
INDEX_COMMIT_INTERVAL = 50

T = TypeVar("T")


def _with_retry(
    fn: Callable[[], T],
    max_retries: int = 5,
    initial_delay: float = 0.1,
) -> T:
    """Execute fn with exponential backoff retry on database lock errors."""
    for attempt in range(max_retries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "database is locked" not in str(e):
                raise
            if attempt == max_retries - 1:
                raise
            delay = initial_delay * (2 ** attempt)
            time.sleep(delay)


class _FilesProgressColumn(ProgressColumn):
    """Files completed / pre-scan total (or running count if no pre-scan)."""

    def render(self, task: Task) -> Text:
        done = int(task.fields.get("files_done", 0))
        total = task.fields.get("files_total")
        if total is not None:
            return Text(f" {done:,}/{int(total):,} files", style="progress.download")
        return Text(f" {done:,} files", style="progress.download")

# ---------------------------------------------------------------------------
# Optional dependencies — degrade gracefully if missing
# ---------------------------------------------------------------------------

try:
    import magic as _magic  # python-magic

    def _detect_mime(path: pathlib.Path) -> str:
        return _magic.from_file(str(path), mime=True)

except ImportError:
    def _detect_mime(path: pathlib.Path) -> str:  # type: ignore[misc]
        mime, _ = mimetypes.guess_type(str(path))
        return mime or "application/octet-stream"


try:
    import exifread as _exifread  # type: ignore[import-untyped]

    def _read_exif_datetime(path: pathlib.Path) -> Optional[str]:
        """Return ISO-8601 datetime from EXIF DateTimeOriginal, or None."""
        try:
            with open(path, "rb") as fh:
                tags = _exifread.process_file(fh, stop_tag="EXIF DateTimeOriginal", details=False)
            raw = tags.get("EXIF DateTimeOriginal") or tags.get("Image DateTime")
            if raw:
                # EXIF format: '2023:06:15 14:22:01'
                dt_str = str(raw).strip()
                dt = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
                return dt.isoformat()
        except Exception:
            pass
        return None

except ImportError:
    def _read_exif_datetime(path: pathlib.Path) -> Optional[str]:  # type: ignore[misc]
        return None


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".webp", ".heic", ".heif", ".raw", ".cr2", ".nef", ".arw",
}

_AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".flac", ".wav", ".aac", ".ogg", ".aiff", ".aif",
    ".opus", ".wma", ".alac",
}


def _is_probably_audio(filepath: pathlib.Path, mime_type: str) -> bool:
    """True if we should run the audio metadata pipeline."""
    if filepath.suffix.lower() in _AUDIO_EXTENSIONS:
        return True
    return (mime_type or "").lower().startswith("audio/")


def _is_probably_image(filepath: pathlib.Path, mime_type: str) -> bool:
    """True if we run the image pipeline (pHash / thumbnails). Extension or sniffed MIME."""
    if filepath.suffix.lower() in _IMAGE_EXTENSIONS:
        return True
    return (mime_type or "").lower().startswith("image/")


_HASH_CHUNK = 1 << 20  # 1 MiB

# BLAKE3 content fingerprints stored in ``files.content_hash``:
#   ``b3f:`` + 64 hex — full file (exact byte identity vs other ``b3f:`` rows).
#   ``b3p:`` + 64 hex — sampled head/tail + size (fast for huge files; not byte-exact).
PREFIX_B3_FULL = "b3f:"
PREFIX_B3_PARTIAL = "b3p:"


def _is_indexed_content_hash(value: Optional[str]) -> bool:
    """True if *value* is a fingerprint written by this indexer (``b3f:`` / ``b3p:``)."""
    if not value:
        return False
    return value.startswith(PREFIX_B3_FULL) or value.startswith(PREFIX_B3_PARTIAL)
_B3P_DOMAIN = b"LIBRARIAN_B3P_V1\x00"
# Bytes read from start and end when fingerprinting files larger than ``partial_hash_above``.
PARTIAL_SAMPLE_BYTES = 4 * 1024 * 1024
# Files at or below this size (unless threshold is 0) get a full-file ``b3f:`` hash.
DEFAULT_PARTIAL_HASH_ABOVE_BYTES = 64 * 1024 * 1024


def content_fingerprint(
    path: pathlib.Path,
    size_bytes: int,
    partial_hash_above_bytes: int = DEFAULT_PARTIAL_HASH_ABOVE_BYTES,
) -> Optional[str]:
    """BLAKE3 fingerprint for *path* using *size_bytes* from ``stat`` (avoids extra stat).

    If ``partial_hash_above_bytes`` is ``0``, always reads the entire file (``b3f:``).
    Otherwise files larger than that threshold use head+tail sampling (``b3p:``).
    """
    try:
        if partial_hash_above_bytes == 0 or size_bytes <= partial_hash_above_bytes:
            h = blake3.blake3()
            with path.open("rb") as fh:
                while chunk := fh.read(_HASH_CHUNK):
                    h.update(chunk)
            return PREFIX_B3_FULL + h.hexdigest()

        sample = min(PARTIAL_SAMPLE_BYTES, size_bytes)
        h = blake3.blake3()
        h.update(_B3P_DOMAIN)
        h.update(size_bytes.to_bytes(8, "little", signed=False))
        with path.open("rb") as fh:
            head = fh.read(sample)
            h.update(head)
            remainder = size_bytes - len(head)
            if remainder > 0:
                tail_len = min(PARTIAL_SAMPLE_BYTES, remainder)
                fh.seek(size_bytes - tail_len)
                h.update(fh.read(tail_len))
        return PREFIX_B3_PARTIAL + h.hexdigest()
    except OSError:
        return None


def _fs_times(stat: os.stat_result) -> tuple[str, str]:
    """Return (created_at, modified_at) as ISO-8601 strings."""
    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    # st_birthtime is macOS / BSD; fall back to st_ctime
    birth_ts = getattr(stat, "st_birthtime", None) or stat.st_ctime
    created = datetime.fromtimestamp(birth_ts, tz=timezone.utc).isoformat()
    return created, modified


def scan_index_tree(root: pathlib.Path) -> tuple[int, int]:
    """Count regular files and sum sizes under *root* (same eligibility rules as indexing).

    Returns ``(file_count, total_bytes)``. Used for progress totals before the main walk.
    """
    root = root.resolve()
    n = 0
    total_bytes = 0
    for dirpath, _dirs, filenames in os.walk(root):
        for filename in filenames:
            filepath = pathlib.Path(dirpath) / filename
            try:
                st = filepath.stat()
            except OSError:
                continue
            if not filepath.is_file():
                continue
            n += 1
            total_bytes += st.st_size
    return n, total_bytes


# ---------------------------------------------------------------------------
# Indexer logic
# ---------------------------------------------------------------------------

_MAC_JUNK_THRESHOLD = 5 * 1024  # 5 kB

# Exact filenames that are always OS/tool noise, regardless of size.
_JUNK_EXACT_NAMES: frozenset[str] = frozenset({
    ".DS_Store",       # macOS folder view settings
    ".localized",      # macOS locale marker
    "Thumbs.db",       # Windows thumbnail cache
    "ehthumbs.db",     # Windows media thumbnail cache
    "Desktop.ini",     # Windows folder configuration
})


def _junk_reason(name: str) -> str:
    reasons = {
        ".DS_Store": "macOS folder metadata (.DS_Store)",
        ".localized": "macOS locale marker (.localized)",
        "Thumbs.db": "Windows thumbnail cache (Thumbs.db)",
        "ehthumbs.db": "Windows media thumbnail cache (ehthumbs.db)",
        "Desktop.ini": "Windows folder config (Desktop.ini)",
    }
    if name.startswith("._"):
        return "macOS resource-fork sidecar (._* < 5 kB)"
    return reasons.get(name, "known OS junk file")


def _is_junk_file(name: str, size: int) -> bool:
    if name in _JUNK_EXACT_NAMES:
        return True
    if name.startswith("._") and size < _MAC_JUNK_THRESHOLD:
        return True
    return False


def _apply_junk_label(conn: sqlite3.Connection, file_id: str, name: str, size: int, now: str) -> None:
    """Upsert a trash label for a known OS junk file."""
    reason = _junk_reason(name)
    existing = conn.execute(
        "SELECT file_id FROM labels WHERE file_id = ?", (file_id,)
    ).fetchone()
    if existing:
        def _update_label() -> None:
            conn.execute(
                "UPDATE labels SET action = 'trash', source = 'user', labeled_at = ? WHERE file_id = ?",
                (now, file_id),
            )
        _with_retry(_update_label)
    else:
        def _insert_label() -> None:
            conn.execute(
                """INSERT INTO labels (file_id, action, source, confidence, reasoning, labeled_at)
                   VALUES (?, 'trash', 'user', 1.0, ?, ?)""",
                (file_id, reason, now),
            )
        _with_retry(_insert_label)


def index_directory(
    path: pathlib.Path,
    drive_id: Optional[str],
    conn: sqlite3.Connection,
    progress: Optional[Progress] = None,
    with_thumbs: bool = False,
    force: bool = False,
    tree_stats: Optional[tuple[int, int]] = None,
    commit_interval: int = INDEX_COMMIT_INTERVAL,
    label_mac_junk: bool = False,
    prune_missing: bool = False,
    partial_hash_above_bytes: int = DEFAULT_PARTIAL_HASH_ABOVE_BYTES,
) -> dict[str, int]:
    """
    Walk *path* and upsert records into the files table.

    *tree_stats*, when provided, is ``(file_count, total_bytes)`` from :func:`scan_index_tree`
    and drives progress bar totals.

    *commit_interval*: commit to SQLite after this many files are fully processed (skip or index).
    Use ``0`` to disable intermediate commits (single transaction until the connection closes).

    *partial_hash_above_bytes*: files **larger** than this use ``b3p:`` head/tail sampling; others use
    full-file ``b3f:``. ``0`` means always full-file ``b3f:`` (slowest, strongest per-file identity).

    Returns counts: {indexed, skipped, missing}.
    """
    now = datetime.now(timezone.utc).isoformat()
    counts = {"indexed": 0, "skipped": 0, "missing": 0}

    # Collect all existing file IDs under this path so we can mark missing ones
    root_str = str(path.resolve())
    existing_ids: dict[str, str] = {}  # path -> id for rows currently present
    for row in conn.execute(
        "SELECT id, path FROM files WHERE path LIKE ? AND status = 'present'",
        (root_str + "%",),
    ):
        existing_ids[row["path"]] = row["id"]

    seen_paths: set[str] = set()

    files_done_counter = 0
    files_since_last_commit = 0
    task_id = None
    if progress:
        bar_total: Optional[float] = None
        files_total_field: Optional[int] = None
        if tree_stats:
            fc, bt = tree_stats
            files_total_field = fc
            if bt > 0:
                bar_total = float(bt)
            elif fc > 0:
                bar_total = float(fc)
        task_id = progress.add_task(
            "Indexing…",
            total=bar_total,
            files_done=0,
            files_total=files_total_field,
        )

    for dirpath, _dirs, filenames in os.walk(path.resolve()):
        for filename in filenames:
            filepath = pathlib.Path(dirpath) / filename
            abs_path = str(filepath)

            if progress and task_id is not None:
                progress.update(task_id, description=f"[dim]{filepath.name}[/dim]")

            try:
                stat = filepath.stat()
            except OSError:
                continue  # symlink target missing, etc.

            if not filepath.is_file():
                continue

            seen_paths.add(abs_path)
            files_done_counter += 1
            created_at, modified_at = _fs_times(stat)

            def _tick() -> None:
                if not (progress and task_id is not None):
                    return
                if tree_stats and tree_stats[1] > 0:
                    adv = float(stat.st_size)
                elif tree_stats:
                    adv = 1.0
                else:
                    adv = float(stat.st_size)
                progress.update(task_id, advance=adv, files_done=files_done_counter)

            def _after_file_processed() -> None:
                nonlocal files_since_last_commit
                _tick()
                if commit_interval <= 0:
                    return
                files_since_last_commit += 1
                if files_since_last_commit >= commit_interval:
                    _with_retry(conn.commit)
                    files_since_last_commit = 0

            # Check for unchanged file (same path + mtime) to skip hashing
            existing_row = conn.execute(
                "SELECT id, modified_at, content_hash FROM files WHERE path = ?",
                (abs_path,),
            ).fetchone()

            if (
                existing_row
                and existing_row["modified_at"] == modified_at
                and _is_indexed_content_hash(existing_row["content_hash"])
            ):
                # File unchanged — just refresh indexed_at and status
                def _update_unchanged() -> None:
                    conn.execute(
                        "UPDATE files SET indexed_at = ?, status = 'present' WHERE id = ?",
                        (now, existing_row["id"]),
                    )
                _with_retry(_update_unchanged)
                counts["skipped"] += 1
                # Still run image pipeline so --with-thumbs / pHash can catch up on re-runs
                _mime = _detect_mime(filepath)
                if with_thumbs and _is_probably_image(filepath, _mime):
                    from magpie.thumbs import process_image

                    process_image(
                        file_id=existing_row["id"],
                        path=filepath,
                        conn=conn,
                        with_thumbs=with_thumbs,
                        force=force,
                    )
                if _is_probably_audio(filepath, _mime):
                    from magpie.audio_analyzer import process_audio

                    process_audio(
                        file_id=existing_row["id"],
                        path=filepath,
                        conn=conn,
                        force=force,
                    )
                if label_mac_junk and _is_junk_file(filepath.name, stat.st_size):
                    _apply_junk_label(conn, existing_row["id"], filepath.name, stat.st_size, now)
                _after_file_processed()
                continue

            # Compute content fingerprint (BLAKE3; partial for large files)
            content_hash = content_fingerprint(
                filepath, stat.st_size, partial_hash_above_bytes
            )

            mime_type = _detect_mime(filepath)
            extension = filepath.suffix.lstrip(".").lower() or None

            exif_created: Optional[str] = None
            if _is_probably_image(filepath, mime_type):
                exif_created = _read_exif_datetime(filepath)

            effective_created = exif_created or created_at

            is_image = _is_probably_image(filepath, mime_type)

            if existing_row:
                def _update_file() -> str:
                    conn.execute(
                        """UPDATE files SET
                            drive_id = ?,
                            size_bytes = ?,
                            created_at = ?,
                            modified_at = ?,
                            indexed_at = ?,
                            content_hash = ?,
                            mime_type = ?,
                            extension = ?,
                            status = 'present'
                           WHERE id = ?""",
                        (
                            drive_id,
                            stat.st_size,
                            effective_created,
                            modified_at,
                            now,
                            content_hash,
                            mime_type,
                            extension,
                            existing_row["id"],
                        ),
                    )
                    return existing_row["id"]
                current_file_id = _with_retry(_update_file)
            else:
                current_file_id = str(uuid.uuid4())
                def _insert_file() -> None:
                    conn.execute(
                        """INSERT INTO files
                            (id, path, drive_id, size_bytes, created_at, modified_at,
                             indexed_at, content_hash, mime_type, extension, status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'present')""",
                        (
                            current_file_id,
                            abs_path,
                            drive_id,
                            stat.st_size,
                            effective_created,
                            modified_at,
                            now,
                            content_hash,
                            mime_type,
                            extension,
                        ),
                    )
                _with_retry(_insert_file)

            if is_image:
                from magpie.thumbs import process_image
                process_image(
                    file_id=current_file_id,
                    path=filepath,
                    conn=conn,
                    with_thumbs=with_thumbs,
                    force=force,
                )

            if _is_probably_audio(filepath, mime_type):
                from magpie.audio_analyzer import process_audio
                process_audio(
                    file_id=current_file_id,
                    path=filepath,
                    conn=conn,
                    force=force,
                )

            if label_mac_junk and _is_junk_file(filepath.name, stat.st_size):
                _apply_junk_label(conn, current_file_id, filepath.name, stat.st_size, now)

            counts["indexed"] += 1
            _after_file_processed()

    if commit_interval > 0 and files_since_last_commit > 0:
        _with_retry(conn.commit)
        files_since_last_commit = 0

    # Mark previously-present files that weren't seen as missing (or prune them)
    for abs_path, file_id in existing_ids.items():
        if abs_path not in seen_paths:
            if prune_missing:
                def _delete_file() -> None:
                    conn.execute("DELETE FROM face_suggestions WHERE file_id = ?", (file_id,))
                    conn.execute("DELETE FROM face_labels WHERE file_id = ?", (file_id,))
                    conn.execute("DELETE FROM file_analysis WHERE file_id = ?", (file_id,))
                    conn.execute("DELETE FROM duplicate_groups WHERE file_id = ?", (file_id,))
                    conn.execute("DELETE FROM labels WHERE file_id = ?", (file_id,))
                    conn.execute("DELETE FROM file_metadata WHERE file_id = ?", (file_id,))
                    conn.execute("DELETE FROM executions WHERE file_id = ?", (file_id,))
                    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
                _with_retry(_delete_file)
            else:
                def _mark_missing() -> None:
                    conn.execute(
                        "UPDATE files SET status = 'missing' WHERE id = ?", (file_id,)
                    )
                _with_retry(_mark_missing)
            counts["missing"] += 1

    if progress and task_id is not None:
        if tree_stats and tree_stats[1] > 0:
            progress.update(
                task_id,
                description="[green]Done[/green]",
                completed=float(tree_stats[1]),
                files_done=tree_stats[0],
            )
        elif tree_stats and tree_stats[0] > 0:
            progress.update(
                task_id,
                description="[green]Done[/green]",
                completed=float(tree_stats[0]),
                files_done=tree_stats[0],
            )
        else:
            progress.update(task_id, description="[green]Done[/green]")

    return counts


# ---------------------------------------------------------------------------
# Public entry point (called from cli.py)
# ---------------------------------------------------------------------------

def run_index(
    path: pathlib.Path,
    drive_id: Optional[str],
    db: pathlib.Path,
    with_thumbs: bool = False,
    force: bool = False,
    no_prescan: bool = False,
    commit_every: int = INDEX_COMMIT_INTERVAL,
    label_mac_junk: bool = False,
    prune_missing: bool = False,
    partial_hash_above_mib: int = 64,
) -> None:
    """Validate args, auto-detect drive, run indexing with a progress bar."""
    partial_hash_above_bytes = (
        0 if partial_hash_above_mib == 0 else partial_hash_above_mib * 1024 * 1024
    )

    if not path.exists():
        console.print(f"[red]Path does not exist:[/red] {path}")
        raise typer.Exit(1)

    if not path.is_dir():
        console.print(f"[red]Path is not a directory:[/red] {path}")
        raise typer.Exit(1)

    # Auto-detect drive if not specified
    if drive_id is None:
        from magpie.drives import detect_drive_info
        info = detect_drive_info(path)
        with get_connection(db) as conn:
            row = None
            if info["serial"]:
                row = conn.execute(
                    "SELECT id FROM drives WHERE serial = ?", (info["serial"],)
                ).fetchone()
            if not row and info["volume_name"]:
                row = conn.execute(
                    "SELECT id FROM drives WHERE volume_name = ?", (info["volume_name"],)
                ).fetchone()
            if row:
                drive_id = row["id"]
                console.print(f"[dim]Auto-detected drive id:[/dim] {drive_id}")
            else:
                console.print(
                    "[yellow]No matching drive registered. Use [bold]magpie drives add[/bold] first, "
                    "or pass [bold]--drive-id[/bold] explicitly. Indexing without drive association.[/yellow]"
                )

    tree_stats: Optional[tuple[int, int]] = None
    if not no_prescan:
        with console.status(
            "[dim]Counting files and sizes for progress (one-time tree walk)…[/dim]",
            spinner="dots",
        ):
            tree_stats = scan_index_tree(path.resolve())
        fc, bt = tree_stats
        console.print(f"[dim]{fc:,} files · {_decimal_bytes(bt)} total[/dim]")

    progress_columns = (
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(binary_units=False),
        _FilesProgressColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
    )

    stats_for_index = None if no_prescan else tree_stats
    empty_after_prescan = tree_stats is not None and tree_stats[0] == 0

    with get_connection(db) as conn:
        if empty_after_prescan:
            counts = index_directory(
                path,
                drive_id,
                conn,
                progress=None,
                with_thumbs=with_thumbs,
                force=force,
                tree_stats=tree_stats,
                commit_interval=commit_every,
                label_mac_junk=label_mac_junk,
                prune_missing=prune_missing,
                partial_hash_above_bytes=partial_hash_above_bytes,
            )

        else:
            with Progress(
                *progress_columns,
                console=console,
                transient=False,
            ) as progress:
                counts = index_directory(
                    path,
                    drive_id,
                    conn,
                    progress=progress,
                    with_thumbs=with_thumbs,
                    force=force,
                    tree_stats=stats_for_index,
                    commit_interval=commit_every,
                    label_mac_junk=label_mac_junk,
                    prune_missing=prune_missing,
                    partial_hash_above_bytes=partial_hash_above_bytes,
                )

    missing_label = "pruned from db" if prune_missing else "marked missing"
    console.print(
        f"\n[green]Indexing complete[/green]\n"
        f"  indexed:  {counts['indexed']}\n"
        f"  skipped:  {counts['skipped']}  (unchanged)\n"
        f"  missing:  {counts['missing']}  ({missing_label})"
    )
