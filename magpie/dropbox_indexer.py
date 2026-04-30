"""Dropbox file indexer — fetch file metadata, download, hash, and populate the files table."""

from __future__ import annotations

import io
import pathlib
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import blake3
import dropbox
import typer
from dropbox.exceptions import ApiError
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text

from magpie.db import get_connection
from magpie.dropbox_auth import get_dropbox_client
from magpie.indexer import content_fingerprint, _is_probably_image, _is_probably_audio, _detect_mime

console = Console()

# BLAKE3 prefixes (same as indexer.py)
PREFIX_B3_FULL = "b3f:"
PREFIX_B3_PARTIAL = "b3p:"
DEFAULT_PARTIAL_HASH_ABOVE_BYTES = 64 * 1024 * 1024

# Batch size for API list requests
LIST_PAGE_SIZE = 2000  # Dropbox default is 2000

# Rate limit: Dropbox API allows ~60 calls/minute per user
# We'll use exponential backoff on 429s
MAX_RETRIES = 5
INITIAL_BACKOFF = 0.5  # seconds


class _FilesProgressColumn(ProgressColumn):
    """Files indexed / total."""

    def render(self, task: Task) -> Text:
        done = int(task.fields.get("files_done", 0))
        total = task.fields.get("files_total", 0)
        if total:
            return Text(f" {done:,}/{total:,} files", style="progress.download")
        return Text(f" {done:,} files", style="progress.download")


def _with_backoff(fn, max_retries=MAX_RETRIES, initial_backoff=INITIAL_BACKOFF):
    """Execute fn with exponential backoff retry on rate limit (429)."""
    for attempt in range(max_retries):
        try:
            return fn()
        except ApiError as e:
            # Dropbox returns RateLimitError for rate limits
            if not hasattr(e, "error") or not hasattr(e.error, "is_rate_limit"):
                raise
            if not e.error.is_rate_limit():
                raise
            if attempt == max_retries - 1:
                raise
            delay = initial_backoff * (2 ** attempt)
            console.log(f"[yellow]Rate limited, waiting {delay:.1f}s[/yellow]")
            time.sleep(delay)


def _with_db_retry(fn, max_retries=MAX_RETRIES, initial_backoff=INITIAL_BACKOFF):
    """Execute fn with exponential backoff retry on database lock."""
    for attempt in range(max_retries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "database is locked" not in str(e):
                raise
            if attempt == max_retries - 1:
                raise
            delay = initial_backoff * (2 ** attempt)
            console.log(f"[yellow]Database locked, retrying in {delay:.1f}s[/yellow]")
            time.sleep(delay)


def _download_and_hash(
    dbx,
    path: str,
    size_bytes: int,
    local_dropbox_root: Optional[pathlib.Path] = None,
    skip_missing_local: bool = False,
) -> Optional[str]:
    """Download a file from Dropbox, compute BLAKE3 hash, return the hash.

    Args:
        dbx: Dropbox client instance
        path: File path in Dropbox
        size_bytes: File size in bytes
        local_dropbox_root: Optional local path to Dropbox folder (e.g., ~/Dropbox)
        skip_missing_local: If True and local_dropbox_root is set, skip files not found locally

    Returns:
        Hash string (b3f:... or b3p:...) or None on download failure or file not found locally
    """
    # Try to read from local copy first if available
    file_bytes = None
    local_path = None
    if local_dropbox_root:
        local_path = local_dropbox_root / path.lstrip("/")
        if local_path.exists() and local_path.is_file():
            try:
                with open(local_path, "rb") as f:
                    file_bytes = f.read()
            except (IOError, OSError):
                file_bytes = None
        elif skip_missing_local:
            return None  # Skip files not found locally

    # Fall back to API download if no local copy
    if file_bytes is None:
        try:
            # Download file content
            metadata, response = _with_backoff(lambda: dbx.files_download(path))
            file_bytes = response.content
        except ApiError as e:
            if hasattr(e, "error"):
                if hasattr(e.error, "is_not_found") and e.error.is_not_found():
                    return None  # File deleted between list and download
                if hasattr(e.error, "is_restricted_content") and e.error.is_restricted_content():
                    console.log(f"[yellow]⚠ Skipped: {path} — restricted content[/yellow]")
                    return None
            raise

    try:

        # Compute BLAKE3 hash
        if size_bytes <= DEFAULT_PARTIAL_HASH_ABOVE_BYTES or size_bytes == 0:
            h = blake3.blake3()
            h.update(file_bytes)
            return PREFIX_B3_FULL + h.hexdigest()
        else:
            # Partial hash for large files
            from magpie.indexer import _B3P_DOMAIN, PARTIAL_SAMPLE_BYTES
            sample = min(PARTIAL_SAMPLE_BYTES, len(file_bytes))
            h = blake3.blake3()
            h.update(_B3P_DOMAIN)
            h.update(size_bytes.to_bytes(8, "little", signed=False))
            h.update(file_bytes[:sample])
            if len(file_bytes) > sample:
                tail_len = min(PARTIAL_SAMPLE_BYTES, len(file_bytes) - sample)
                h.update(file_bytes[-tail_len:])
            return PREFIX_B3_PARTIAL + h.hexdigest()
    except Exception as e:
        return None


def _download_dropbox_thumbnail(
    dbx,
    path: str,
    file_id: str,
    size_bytes: int = 0,
) -> Optional[pathlib.Path]:
    """Download a Dropbox thumbnail and cache it locally.

    Args:
        dbx: Dropbox client instance
        path: File path in Dropbox
        file_id: File ID for naming the thumbnail
        size_bytes: File size (used to determine if we try to thumbnail)

    Returns:
        Path to cached thumbnail or None on failure
    """
    if not path or size_bytes == 0:
        return None

    try:
        # Create thumbs directory
        thumbs_dir = pathlib.Path.home() / ".magpie" / "thumbs"
        thumbs_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = thumbs_dir / f"{file_id}.jpg"

        # Download thumbnail with backoff (limit=w128h128 for small image)
        try:
            metadata, response = _with_backoff(
                lambda: dbx.files_get_thumbnail_v2(path, size=dropbox.files.ThumbnailSize("w128h128"))
            )
            img_bytes = response.content

            # Save thumbnail
            from PIL import Image
            img = Image.open(io.BytesIO(img_bytes))
            img = img.convert("RGB")
            img.thumbnail((256, 256))
            img.save(thumb_path, "JPEG", quality=85)

            return thumb_path
        except ApiError as e:
            # If thumbnail fails (e.g., unsupported file type), just skip
            return None

    except Exception as e:
        console.log(f"[yellow]⚠ Failed to save thumbnail for {file_id}: {e}[/yellow]")
        return None


def run_dropbox_index(
    drive_id: str,
    db_path: pathlib.Path,
    limit: Optional[int] = None,
    dry_run: bool = False,
    no_prescan: bool = False,
    commit_every: int = 50,
    prune_missing: bool = False,
    with_thumbs: bool = True,
    local_dropbox_root: Optional[pathlib.Path] = None,
    skip_missing_local: bool = False,
) -> None:
    """Index files from a registered Dropbox.

    Args:
        drive_id: Registered drive UUID (from drives table)
        db_path: Path to the magpie database
        limit: Max files to index (for testing)
        dry_run: If True, don't write to database
        no_prescan: Skip reporting total file count upfront
        commit_every: Commit to database after this many files (0 = end only)
        prune_missing: Mark files not found in this scan as missing
        with_thumbs: Generate thumbnails for image files (default: True)
        local_dropbox_root: Optional path to local Dropbox folder (e.g., ~/Dropbox)
        skip_missing_local: If True, skip indexing files not found in local_dropbox_root
    """
    # Get Dropbox client
    dbx = get_dropbox_client()

    # Look up drive in DB
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT label, serial FROM drives WHERE id = ? AND source = 'dropbox'",
            (drive_id,)
        ).fetchone()

        if not row:
            typer.echo(f"[red]Drive {drive_id} not found or not a Dropbox[/red]")
            raise typer.Exit(1)

        drive_label, drive_serial = row

    # Build progress bar
    progress_cols = [
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        _FilesProgressColumn(),
        TimeElapsedColumn(),
    ]

    with Progress(*progress_cols, console=console) as progress:
        task = progress.add_task(
            f"[blue]Indexing Dropbox: {drive_label}...[/blue]",
            total=None,
            files_done=0,
            files_total=0,
        )

        # Prescan: count files (optional)
        files_total = None
        if not no_prescan:
            try:
                progress.update(task, description="[blue]Counting files in Dropbox...[/blue]")
                files_total = _prescan_dropbox(dbx)
                progress.update(task, total=files_total, files_total=files_total)
            except Exception as e:
                console.log(f"[yellow]Prescan failed: {e}, continuing without total...[/yellow]")

        # Load already-indexed file IDs and paths to skip re-processing
        with get_connection(db_path) as conn:
            already_indexed = set(
                row[0] for row in conn.execute(
                    """
                    SELECT fm.value FROM file_metadata fm
                    JOIN files f ON fm.file_id = f.id
                    WHERE f.drive_id = ? AND fm.key = 'dropbox_file_id' AND f.status = 'present'
                    """,
                    (drive_id,),
                ).fetchall()
            )
            already_indexed_paths = set(
                row[0] for row in conn.execute(
                    "SELECT path FROM files WHERE drive_id = ? AND status = 'present'",
                    (drive_id,),
                ).fetchall()
            )
        console.log(f"[dim]Skipping {len(already_indexed_paths):,} already-indexed files[/dim]")

        # Index files
        seen_file_ids = set(already_indexed)
        indexed_count = 0

        for entry in _list_dropbox_files(dbx, recursive=True):
            # Skip folders
            if isinstance(entry, dropbox.files.FolderMetadata):
                continue

            if limit and indexed_count >= limit:
                break

            file_id = entry.id
            path = entry.path_display
            path_lower = entry.path_lower or path.lower()

            # Build virtual path early so we can check against path-based index
            virtual_path = f"/dropbox/{drive_label}{path_lower}"

            # Skip files already in the database (by Dropbox file ID or by path)
            if file_id in seen_file_ids or virtual_path in already_indexed_paths:
                progress.update(task, advance=1)
                continue
            size_bytes = entry.size
            modified_at = entry.server_modified.isoformat() if entry.server_modified else None

            # Determine extension and mime type
            extension = None
            if path:
                parts = path.rsplit(".", 1)
                if len(parts) > 1:
                    extension = parts[1].lower()

            mime_type = _detect_mime(pathlib.Path(path))

            # Download and hash
            content_hash = None
            try:
                content_hash = _download_and_hash(dbx, path, size_bytes, local_dropbox_root, skip_missing_local)
            except ApiError as e:
                console.log(f"[yellow]Failed to download {path}: {e}[/yellow]")

            # Skip if not hashed (either missing locally or download failed)
            if content_hash is None and skip_missing_local:
                continue

            # Download thumbnail if image (skip when using local files since we're not using API)
            thumb_path = None
            if with_thumbs and content_hash and not local_dropbox_root and _is_probably_image(pathlib.Path(path), mime_type):
                try:
                    thumb_path = _download_dropbox_thumbnail(dbx, path, file_id, size_bytes)
                except Exception as e:
                    console.log(f"[yellow]Thumbnail failed for {path}: {e}[/yellow]")

            # Write to database (with retry)
            if not dry_run:
                def _insert_file():
                    with get_connection(db_path) as conn:
                        new_uuid = str(uuid.uuid4())
                        conn.execute(
                            """
                            INSERT INTO files
                            (id, path, drive_id, size_bytes, modified_at, indexed_at, content_hash, mime_type, extension, status)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'present')
                            ON CONFLICT(path, drive_id) DO UPDATE SET
                                size_bytes=excluded.size_bytes,
                                modified_at=excluded.modified_at,
                                indexed_at=excluded.indexed_at,
                                content_hash=excluded.content_hash,
                                mime_type=excluded.mime_type,
                                extension=excluded.extension,
                                status='present'
                            """,
                            (
                                new_uuid, virtual_path, drive_id, size_bytes,
                                modified_at, datetime.now(timezone.utc).isoformat(),
                                content_hash, mime_type, extension
                            ),
                        )
                        file_uuid = conn.execute(
                            "SELECT id FROM files WHERE path = ? AND drive_id = ?",
                            (virtual_path, drive_id),
                        ).fetchone()[0]

                        conn.execute(
                            "INSERT OR REPLACE INTO file_metadata (file_id, key, value) VALUES (?, 'dropbox_file_id', ?)",
                            (file_uuid, file_id),
                        )
                        if content_hash:
                            conn.execute(
                                "INSERT OR REPLACE INTO file_metadata (file_id, key, value) VALUES (?, 'content_hash', ?)",
                                (file_uuid, content_hash),
                            )
                        conn.commit()

                _with_db_retry(_insert_file)

            seen_file_ids.add(file_id)
            indexed_count += 1
            progress.update(task, advance=1, files_done=indexed_count, description=f"[blue]{path}[/blue]")

        # Prune missing files (files from previous scans not seen in this one)
        if prune_missing and seen_file_ids:
            _prune_dropbox_missing(db_path, drive_id, seen_file_ids)

        console.log(f"[green]✓ Indexed {indexed_count} files from Dropbox: {drive_label}[/green]")


def _prescan_dropbox(dbx) -> int:
    """Count total files in Dropbox (expensive operation)."""
    count = 0
    for entry in _list_dropbox_files(dbx, recursive=True):
        if isinstance(entry, dropbox.files.FileMetadata):
            count += 1
    return count


def _list_dropbox_files(dbx, recursive: bool = True):
    """Generator that yields all FileMetadata and FolderMetadata entries from Dropbox."""
    cursor = None

    def _list():
        nonlocal cursor
        if cursor:
            return _with_backoff(lambda: dbx.files_list_folder_continue(cursor))
        else:
            return _with_backoff(lambda: dbx.files_list_folder("", recursive=recursive, limit=LIST_PAGE_SIZE))

    result = _list()
    for entry in result.entries:
        yield entry

    while result.has_more:
        cursor = result.cursor
        result = _list()
        for entry in result.entries:
            yield entry


def run_dropbox_preview(
    drive_id: str,
    db_path: pathlib.Path,
    limit: Optional[int] = None,
) -> None:
    """Build a lightweight preview of files in Dropbox.

    Fetches file metadata without downloading or hashing. Populates the dropbox_preview
    table for analysis before running the full indexer.
    """
    dbx = get_dropbox_client()

    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT label FROM drives WHERE id = ? AND source = 'dropbox'",
            (drive_id,)
        ).fetchone()

        if not row:
            typer.echo(f"[red]Dropbox not found: {drive_id}[/red]")
            raise typer.Exit(1)

        drive_label = row[0]

    # Clear previous preview
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM dropbox_preview WHERE drive_id = ?", (drive_id,))
        conn.commit()

    indexed_count = 0
    with Progress(
        SpinnerColumn(),
        BarColumn(),
        TaskProgressColumn(),
        _FilesProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            f"[blue]Previewing Dropbox: {drive_label}...[/blue]",
            files_done=0,
        )

        for entry in _list_dropbox_files(dbx, recursive=True):
            if isinstance(entry, dropbox.files.FolderMetadata):
                continue

            if limit and indexed_count >= limit:
                break

            path = entry.path_display
            size_bytes = entry.size
            extension = None
            if path:
                parts = path.rsplit(".", 1)
                if len(parts) > 1:
                    extension = parts[1].lower()

            mime_type = _detect_mime(pathlib.Path(path))
            filename = path.split("/")[-1] if path else ""

            # Insert into preview table
            def _insert_preview():
                with get_connection(db_path) as conn:
                    conn.execute(
                        """
                        INSERT INTO dropbox_preview
                        (id, path, filename, extension, size_bytes, mime_type, drive_id, scanned_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            entry.id, path, filename, extension, size_bytes,
                            mime_type, drive_id, datetime.now(timezone.utc).isoformat()
                        ),
                    )
                    conn.commit()

            _with_db_retry(_insert_preview)
            indexed_count += 1
            progress.update(task, advance=1, files_done=indexed_count)

        console.log(f"[green]✓ Preview complete: {indexed_count} files from {drive_label}[/green]")


def run_dropbox_thumbs(
    drive_id: str,
    db_path: pathlib.Path,
    force: bool = False,
    limit: Optional[int] = None,
) -> None:
    """Download and cache thumbnails for image files from Dropbox."""
    dbx = get_dropbox_client()

    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT label FROM drives WHERE id = ? AND source = 'dropbox'",
            (drive_id,)
        ).fetchone()

        if not row:
            typer.echo(f"[red]Dropbox not found: {drive_id}[/red]")
            raise typer.Exit(1)

        drive_label = row[0]

        # Get files that need thumbnails
        if force:
            files = conn.execute(
                """
                SELECT f.id, fm.value, f.size_bytes
                FROM files f
                LEFT JOIN file_metadata fm ON f.id = fm.file_id AND fm.key = 'dropbox_file_id'
                WHERE f.drive_id = ? AND f.mime_type LIKE 'image/%'
                LIMIT ?
                """,
                (drive_id, limit or 10000),
            ).fetchall()
        else:
            files = conn.execute(
                """
                SELECT f.id, fm.value, f.size_bytes
                FROM files f
                LEFT JOIN file_metadata fm ON f.id = fm.file_id AND fm.key = 'dropbox_file_id'
                WHERE f.drive_id = ? AND f.mime_type LIKE 'image/%'
                AND NOT EXISTS (SELECT 1 FROM file_metadata fm2 WHERE fm2.file_id = f.id AND fm2.key = 'thumb_cached')
                LIMIT ?
                """,
                (drive_id, limit or 10000),
            ).fetchall()

    processed = 0
    with Progress(
        SpinnerColumn(),
        BarColumn(),
        TaskProgressColumn(),
        _FilesProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            f"[blue]Downloading thumbnails from {drive_label}...[/blue]",
            total=len(files),
            files_done=0,
        )

        for file_id, dropbox_file_id, size_bytes in files:
            if not dropbox_file_id:
                progress.update(task, advance=1, files_done=processed + 1)
                processed += 1
                continue

            # Get the file path from the files table
            with get_connection(db_path) as conn:
                path_row = conn.execute(
                    "SELECT path FROM files WHERE id = ?", (file_id,)
                ).fetchone()

            if not path_row:
                progress.update(task, advance=1, files_done=processed + 1)
                processed += 1
                continue

            path = path_row[0]
            # Remove the virtual /dropbox/label prefix to get the actual Dropbox path
            if path.startswith("/dropbox/"):
                # /dropbox/Label/actual/path -> /actual/path
                parts = path.split("/", 3)
                if len(parts) >= 4:
                    dropbox_path = "/" + parts[3]
                else:
                    dropbox_path = "/"
            else:
                continue

            # Try to download thumbnail
            thumb_path = _download_dropbox_thumbnail(dbx, dropbox_path, dropbox_file_id, size_bytes)

            # Mark as processed (even if failed)
            if thumb_path:
                def _mark_thumb_cached():
                    with get_connection(db_path) as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO file_metadata (file_id, key, value) VALUES (?, 'thumb_cached', '1')",
                            (file_id,),
                        )
                        conn.commit()

                _with_db_retry(_mark_thumb_cached)

            processed += 1
            progress.update(task, advance=1, files_done=processed)

        console.log(f"[green]✓ Downloaded {processed} thumbnails from {drive_label}[/green]")


def run_dropbox_index_local(
    drive_id: str,
    db_path: pathlib.Path,
    local_dropbox_root: pathlib.Path,
    limit: Optional[int] = None,
    dry_run: bool = False,
    prune_missing: bool = False,
) -> None:
    """Index files by walking the local Dropbox folder — no API calls."""
    local_root = local_dropbox_root.expanduser().resolve()
    if not local_root.exists():
        console.log(f"[red]Local Dropbox path not found: {local_root}[/red]")
        return

    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT label FROM drives WHERE id = ? AND source = 'dropbox'",
            (drive_id,)
        ).fetchone()
        if not row:
            console.log(f"[red]Drive {drive_id} not found or not a Dropbox[/red]")
            return
        drive_label = row[0]

    # Load already-indexed paths to skip re-hashing
    with get_connection(db_path) as conn:
        already_indexed = set(
            row[0] for row in conn.execute(
                "SELECT path FROM files WHERE drive_id = ? AND status = 'present'",
                (drive_id,),
            ).fetchall()
        )
    console.log(f"[dim]Skipping {len(already_indexed):,} already-indexed files[/dim]")

    # Prescan: count files for total
    files_total = sum(
        1 for p in local_root.rglob("*")
        if p.is_file() and not p.name.startswith("._") and p.name != ".DS_Store"
    )
    console.log(f"[dim]Found {files_total:,} local files to scan[/dim]")

    progress_cols = [
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        _FilesProgressColumn(),
        TimeElapsedColumn(),
    ]

    indexed_count = 0
    seen_virtual_paths: set[str] = set(already_indexed)

    with Progress(*progress_cols, console=console) as progress:
        task = progress.add_task(
            f"[blue]Indexing local Dropbox: {drive_label}[/blue]",
            total=files_total,
            files_done=0,
        )

        for local_path in local_root.rglob("*"):
            if not local_path.is_file():
                continue
            if limit and indexed_count >= limit:
                break

            # Skip macOS metadata files
            if local_path.name.startswith("._") or local_path.name == ".DS_Store":
                continue

            # Build virtual path: /dropbox/<label>/<relative> — lowercase for consistent casing
            rel = local_path.relative_to(local_root)
            virtual_path = f"/dropbox/{drive_label}/{str(rel).lower()}"
            seen_virtual_paths.add(virtual_path)

            if virtual_path in already_indexed:
                continue

            try:
                size_bytes = local_path.stat().st_size
                modified_at = datetime.fromtimestamp(local_path.stat().st_mtime, tz=timezone.utc).isoformat()
            except OSError:
                continue

            mime_type = _detect_mime(local_path)
            extension = local_path.suffix.lstrip(".").lower() or None

            # Hash the file
            content_hash = None
            try:
                with open(local_path, "rb") as f:
                    file_bytes = f.read()
                if size_bytes <= DEFAULT_PARTIAL_HASH_ABOVE_BYTES or size_bytes == 0:
                    h = blake3.blake3()
                    h.update(file_bytes)
                    content_hash = PREFIX_B3_FULL + h.hexdigest()
                else:
                    from magpie.indexer import _B3P_DOMAIN, PARTIAL_SAMPLE_BYTES
                    sample = min(PARTIAL_SAMPLE_BYTES, len(file_bytes))
                    h = blake3.blake3()
                    h.update(_B3P_DOMAIN)
                    h.update(size_bytes.to_bytes(8, "little", signed=False))
                    h.update(file_bytes[:sample])
                    if len(file_bytes) > sample:
                        tail_len = min(PARTIAL_SAMPLE_BYTES, len(file_bytes) - sample)
                        h.update(file_bytes[-tail_len:])
                    content_hash = PREFIX_B3_PARTIAL + h.hexdigest()
            except (IOError, OSError):
                pass

            if not dry_run:
                def _insert(vp=virtual_path, sz=size_bytes, mod=modified_at, ch=content_hash, mt=mime_type, ext=extension):
                    with get_connection(db_path) as conn:
                        new_uuid = str(uuid.uuid4())
                        conn.execute(
                            """
                            INSERT INTO files
                            (id, path, drive_id, size_bytes, modified_at, indexed_at, content_hash, mime_type, extension, status)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'present')
                            ON CONFLICT(path, drive_id) DO UPDATE SET
                                size_bytes=excluded.size_bytes,
                                modified_at=excluded.modified_at,
                                indexed_at=excluded.indexed_at,
                                content_hash=excluded.content_hash,
                                mime_type=excluded.mime_type,
                                extension=excluded.extension,
                                status='present'
                            """,
                            (new_uuid, vp, drive_id, sz, mod,
                             datetime.now(timezone.utc).isoformat(), ch, mt, ext),
                        )
                        if ch:
                            file_uuid = conn.execute(
                                "SELECT id FROM files WHERE path = ? AND drive_id = ?",
                                (vp, drive_id),
                            ).fetchone()[0]
                            conn.execute(
                                "INSERT OR REPLACE INTO file_metadata (file_id, key, value) VALUES (?, 'content_hash', ?)",
                                (file_uuid, ch),
                            )
                        conn.commit()

                _with_db_retry(_insert)

            indexed_count += 1
            progress.update(task, advance=1, files_done=indexed_count, description=f"[blue]{local_path}[/blue]")

    if prune_missing:
        # Use path-based prune for local-only mode
        with get_connection(db_path) as conn:
            all_present = conn.execute(
                "SELECT path FROM files WHERE drive_id = ? AND status = 'present'",
                (drive_id,),
            ).fetchall()
            missing_paths = [r[0] for r in all_present if r[0] not in seen_virtual_paths]
            if missing_paths:
                chunk_size = 100
                for i in range(0, len(missing_paths), chunk_size):
                    chunk = missing_paths[i:i + chunk_size]
                    placeholders = ",".join("?" * len(chunk))
                    conn.execute(
                        f"UPDATE files SET status = 'missing' WHERE drive_id = ? AND path IN ({placeholders})",
                        (drive_id, *chunk),
                    )
                conn.commit()
                console.log(f"[yellow]⚠ Marked {len(missing_paths)} files as missing[/yellow]")

    console.log(f"[green]✓ Indexed {indexed_count} files from local Dropbox: {drive_label}[/green]")


def _prune_dropbox_missing(db_path: pathlib.Path, drive_id: str, seen_file_ids: set) -> None:
    """Mark files from a previous Dropbox scan as missing if not found in this scan."""
    with get_connection(db_path) as conn:
        # Get all file IDs from metadata that were in previous scans
        previous_ids = conn.execute(
            """
            SELECT DISTINCT fm.value
            FROM file_metadata fm
            JOIN files f ON fm.file_id = f.id
            WHERE f.drive_id = ? AND fm.key = 'dropbox_file_id'
            """,
            (drive_id,),
        ).fetchall()

        missing_ids = [fid[0] for fid in previous_ids if fid[0] not in seen_file_ids]

        if missing_ids:
            # Build WHERE clause with chunks to avoid SQLite's limit
            chunk_size = 100
            for i in range(0, len(missing_ids), chunk_size):
                chunk = missing_ids[i : i + chunk_size]
                placeholders = ",".join("?" * len(chunk))
                conn.execute(
                    f"""
                    UPDATE files SET status = 'missing'
                    WHERE drive_id = ? AND id IN (
                        SELECT f.id FROM files f
                        JOIN file_metadata fm ON f.id = fm.file_id
                        WHERE fm.key = 'dropbox_file_id' AND fm.value IN ({placeholders})
                    )
                    """,
                    (drive_id, *chunk),
                )

            conn.commit()
            console.log(f"[yellow]⚠ Marked {len(missing_ids)} files as missing[/yellow]")
