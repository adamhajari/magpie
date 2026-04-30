"""Google Drive file indexer — fetch file metadata, download, hash, and populate the files table."""

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
import typer
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
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
from magpie.gdrive_auth import get_credentials
from magpie.indexer import content_fingerprint, _is_probably_image, _is_probably_audio

console = Console()

# BLAKE3 prefixes (same as indexer.py)
PREFIX_B3_FULL = "b3f:"
PREFIX_B3_PARTIAL = "b3p:"
DEFAULT_PARTIAL_HASH_ABOVE_BYTES = 64 * 1024 * 1024

# Google Drive API fields to fetch
GDRIVE_FIELDS = "files(id,name,parents,mimeType,size,modifiedTime,trashed,thumbnailLink)"

# Batch size for API list requests
LIST_PAGE_SIZE = 1000

# Rate limit: Google Drive API allows ~10 requests/second per user
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
        except HttpError as e:
            if e.resp.status != 429:
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


def _build_paths_from_files(files_list: list[dict], service=None) -> dict[str, str]:
    """Build a map of {file_id: relative_path} from a flat file list.

    Args:
        files_list: List of file dicts with 'id', 'name', 'parents' keys
        service: Google Drive API service (optional, used to fetch missing parents)

    Returns:
        Dict mapping file_id to relative path (e.g., 'Photos/vacation.jpg')
    """
    paths = {}
    parents_map = {}
    name_map = {}
    missing_parents = set()

    # Build maps from files
    for file in files_list:
        fid = file["id"]
        name_map[fid] = file.get("name", "")
        parents_map[fid] = file.get("parents", [])

    # Reconstruct paths by walking up parent chain
    def get_path(fid: str) -> str:
        if fid in paths:
            return paths[fid]

        parents = parents_map.get(fid, [])
        if not parents or parents[0] == "root":
            # Top-level file
            path = name_map.get(fid, "")
        else:
            parent_id = parents[0]
            # If parent isn't in our map but we have the service, fetch it
            if parent_id not in name_map and service:
                try:
                    parent_meta = _with_backoff(
                        lambda: service.files().get(
                            fileId=parent_id,
                            fields="id,name,parents",
                        ).execute()
                    )
                    name_map[parent_id] = parent_meta.get("name", "")
                    parents_map[parent_id] = parent_meta.get("parents", [])
                except HttpError:
                    # Parent not found or inaccessible
                    name_map[parent_id] = ""
                    parents_map[parent_id] = ["root"]

            parent_path = get_path(parent_id)
            if parent_path:
                path = f"{parent_path}/{name_map.get(fid, '')}"
            else:
                path = name_map.get(fid, "")

        paths[fid] = path
        return path

    # Compute paths for all files (iterate over a copy to handle dict mutations from get_path)
    for fid in list(name_map.keys()):
        get_path(fid)

    return paths


def _download_and_hash(
    service,
    file_id: str,
    size_bytes: int,
    mime_type: str,
) -> Optional[str]:
    """Download a file from Google Drive, compute BLAKE3 hash, return the hash.

    For Google Workspace files (Docs/Sheets/Slides), returns None since they have no
    binary representation.

    Args:
        service: Google Drive API service instance
        file_id: File ID
        size_bytes: File size in bytes
        mime_type: File MIME type

    Returns:
        Hash string (b3f:... or b3p:...) or None for Google Workspace files
    """
    # Skip Google Workspace native files
    if mime_type and mime_type.startswith("application/vnd.google-apps."):
        return None

    try:
        # Download file content
        request = service.files().get_media(fileId=file_id)
        file_bytes = _with_backoff(lambda: request.execute())

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

    except HttpError as e:
        if e.resp.status == 404:
            return None  # File deleted between list and download
        elif e.resp.status == 403:
            # File cannot be downloaded (shared file, restricted access, etc.)
            console.log(f"[yellow]⚠ Skipped (403): {file_id} — cannot download[/yellow]")
            return None
        raise


def _download_gdrive_thumbnail(
    thumbnail_url: str,
    file_id: str,
    size_bytes: int = 0,
) -> Optional[pathlib.Path]:
    """Download a Google Drive thumbnail and cache it locally.

    Checks for existing cached thumbnail first; only downloads if not cached.

    Args:
        thumbnail_url: The thumbnailLink from Google Drive API
        file_id: File ID for naming the thumbnail
        size_bytes: File size (used to determine if we try to thumbnail)

    Returns:
        Path to cached thumbnail or None on failure
    """
    if not thumbnail_url or size_bytes == 0:
        return None

    try:
        import requests
        from PIL import Image

        # Create thumbs directory
        thumbs_dir = pathlib.Path.home() / ".magpie" / "thumbs"
        thumbs_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = thumbs_dir / f"{file_id}.jpg"

        # Use cached thumbnail if it already exists
        if thumb_path.exists():
            return thumb_path

        # Download thumbnail with backoff
        response = _with_backoff(lambda: requests.get(thumbnail_url, timeout=10))
        response.raise_for_status()

        # Save thumbnail
        img = Image.open(io.BytesIO(response.content))
        img = img.convert("RGB")
        img.thumbnail((256, 256))
        img.save(thumb_path, "JPEG", quality=85)

        return thumb_path
    except Exception as e:
        console.log(f"[yellow]⚠ Failed to save thumbnail for {file_id}: {e}[/yellow]")
        return None


def run_gdrive_index(
    drive_id: str,
    db_path: pathlib.Path,
    credentials_path: Optional[pathlib.Path] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    no_prescan: bool = False,
    commit_every: int = 50,
    prune_missing: bool = False,
    with_thumbs: bool = True,
) -> None:
    """Index files from a registered Google Drive.

    Args:
        drive_id: Registered drive UUID (from drives table)
        db_path: Path to the magpie database
        credentials_path: Path to OAuth credentials JSON
        limit: Max files to index (for testing)
        dry_run: If True, don't write to database
        no_prescan: Skip reporting total file count upfront
        commit_every: Commit to database after this many files (0 = end only)
        prune_missing: Mark files not found in this scan as missing
        with_thumbs: Generate thumbnails for image files (default: True)
    """
    # Get OAuth credentials
    creds = get_credentials(credentials_path=credentials_path)
    service = build("drive", "v3", credentials=creds)

    # Look up drive in DB
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT label, serial FROM drives WHERE id = ? AND source = 'gdrive'",
            (drive_id,)
        ).fetchone()

        if not row:
            typer.echo(f"[red]Drive {drive_id} not found or not a Google Drive[/red]")
            raise typer.Exit(1)

        drive_label, root_folder_id = row

    console.log(f"[blue]Indexing Google Drive: {drive_label}[/blue]")

    # Fetch all files with metadata (single API call sequence with pagination)
    console.log("[blue]Fetching file list...[/blue]")
    all_files = []
    page_token = None
    while True:
        results = _with_backoff(
            lambda: service.files().list(
                spaces="drive",
                q="trashed=false",  # spaces=drive already limits to My Drive; exclude trashed files
                fields="files(id,name,parents,size,modifiedTime,mimeType),nextPageToken",
                pageSize=LIST_PAGE_SIZE,
                pageToken=page_token,
            ).execute()
        )

        all_files.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break

    if limit:
        all_files = all_files[:limit]

    total_files = len(all_files)
    if not no_prescan:
        console.log(f"[blue]Found {total_files:,} files to index[/blue]")

    # Check which files are already indexed to avoid unnecessary path reconstruction
    console.log("[blue]Checking for already-indexed files...[/blue]")
    gdrive_id_to_file_id = {}  # Map of Google Drive file ID -> local file ID
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT value, file_id FROM file_metadata WHERE key = 'gdrive_file_id' AND file_id IN "
            "(SELECT id FROM files WHERE drive_id = ?)",
            (drive_id,)
        ).fetchall()
        gdrive_id_to_file_id = {row[0]: row[1] for row in rows}

    # Filter files to only those not already indexed, then reconstruct paths
    files_to_process = [f for f in all_files if f["id"] not in gdrive_id_to_file_id]
    console.log(f"[blue]Need to process {len(files_to_process):,} new/updated files[/blue]")

    # Reconstruct file paths for all files (needed for path matching in skip logic)
    console.log("[blue]Reconstructing paths...[/blue]")
    paths = _build_paths_from_files(all_files, service)

    # Index files
    indexed = 0
    skipped = 0
    seen_file_ids = set()  # Track which files we've seen in this scan (for prune_missing)
    # Pre-populate seen_file_ids with already-indexed files
    seen_file_ids.update(gdrive_id_to_file_id.values())

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        _FilesProgressColumn(),
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task(
            "Indexing",
            files_total=len(files_to_process) if not no_prescan else None,
            files_done=0,
        )

        with get_connection(db_path) as conn:
            for gdfile in files_to_process:
                file_id = gdfile["id"]
                name = gdfile.get("name", "")
                size_bytes = int(gdfile.get("size", 0)) if gdfile.get("size") else 0
                modified_time_str = gdfile.get("modifiedTime")
                mime_type = gdfile.get("mimeType", "")

                # Skip Google Workspace types (non-downloadable: folders, docs, sheets, etc.)
                if mime_type and mime_type.startswith("application/vnd.google-apps."):
                    skipped += 1
                    progress.update(task, files_done=indexed + skipped)
                    continue

                # Parse modified time
                try:
                    modified_at = datetime.fromisoformat(modified_time_str.replace("Z", "+00:00")).isoformat()
                except:
                    modified_at = datetime.now(timezone.utc).isoformat()

                # Build virtual path
                rel_path = paths.get(file_id, name)
                virt_path = f"/gdrive/{drive_label}/{rel_path}"

                # Skip logic: if file exists with same path, size, modified_at, and hash, skip
                existing = conn.execute(
                    "SELECT id, content_hash FROM files WHERE path = ? AND modified_at = ?",
                    (virt_path, modified_at)
                ).fetchone()

                if existing and existing[1] and existing[1].startswith(("b3f:", "b3p:")):
                    # File already indexed and matches; track it for prune_missing logic
                    existing_file_id = existing[0]
                    seen_file_ids.add(existing_file_id)

                    # Ensure gdrive_file_id metadata is recorded (fixes prune_missing for pre-existing files)
                    conn.execute(
                        "INSERT OR IGNORE INTO file_metadata (file_id, key, value) VALUES (?, ?, ?)",
                        (existing_file_id, "gdrive_file_id", file_id),
                    )

                    skipped += 1
                    progress.update(task, files_done=indexed + skipped)
                    continue

                # Update progress with current file name
                progress.update(task, description=f"[progress.description]Indexing: {rel_path[:70]}")

                # Download and hash
                file_uuid = str(uuid.uuid4())
                content_hash = _download_and_hash(service, file_id, size_bytes or 0, mime_type)

                # Download thumbnail for images (if enabled)
                phash_for_insert: Optional[str] = None
                if with_thumbs:
                    thumbnail_url = gdfile.get("thumbnailLink")
                    if thumbnail_url and _is_probably_image(mime_type, pathlib.Path(name).suffix):
                        thumb_path = _download_gdrive_thumbnail(thumbnail_url, file_uuid, size_bytes or 0)
                        if thumb_path:
                            from magpie.thumbs import compute_phash
                            phash_for_insert = compute_phash(thumb_path)

                if not dry_run:
                    # Upsert record — ON CONFLICT keeps the original UUID stable
                    created_at = datetime.now(timezone.utc).isoformat()
                    indexed_at = created_at

                    conn.execute(
                        """
                        INSERT INTO files (
                            id, path, drive_id, size_bytes, created_at, modified_at,
                            indexed_at, content_hash, mime_type, extension, status,
                            perceptual_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(path, drive_id) DO UPDATE SET
                            size_bytes=excluded.size_bytes,
                            created_at=excluded.created_at,
                            modified_at=excluded.modified_at,
                            indexed_at=excluded.indexed_at,
                            content_hash=excluded.content_hash,
                            mime_type=excluded.mime_type,
                            extension=excluded.extension,
                            status='present',
                            perceptual_hash=COALESCE(excluded.perceptual_hash, perceptual_hash)
                        """,
                        (
                            file_uuid,
                            virt_path,
                            drive_id,
                            size_bytes,
                            created_at,
                            modified_at,
                            indexed_at,
                            content_hash,
                            mime_type,
                            pathlib.Path(name).suffix.lstrip(".") if name else "",
                            "present",
                            phash_for_insert,
                        ),
                    )

                    # Re-fetch the stable UUID (may differ from file_uuid if row already existed)
                    file_uuid = conn.execute(
                        "SELECT id FROM files WHERE path = ? AND drive_id = ?",
                        (virt_path, drive_id),
                    ).fetchone()[0]

                    # Store Google Drive file ID in metadata for later use
                    conn.execute(
                        "INSERT OR REPLACE INTO file_metadata (file_id, key, value) VALUES (?, ?, ?)",
                        (file_uuid, "gdrive_file_id", file_id),
                    )

                    # Track that we've seen this file (for prune_missing)
                    seen_file_ids.add(file_uuid)

                    # Commit periodically (if commit_every > 0)
                    if commit_every > 0 and (indexed + 1) % commit_every == 0:
                        conn.commit()

                indexed += 1
                progress.update(task, files_done=indexed + skipped)

            # Prune missing files if requested (mark files not seen in this scan as missing)
            if prune_missing and not dry_run and seen_file_ids:
                # Safety check: ensure we have metadata for previously-indexed files
                # If metadata is missing, warn the user instead of pruning (to avoid false positives)
                previously_indexed_count = len(gdrive_id_to_file_id)
                all_files_count = conn.execute(
                    "SELECT COUNT(*) FROM files WHERE drive_id = ? AND status = 'present'",
                    (drive_id,)
                ).fetchone()[0]

                if previously_indexed_count == 0 and all_files_count > 0:
                    console.log(
                        f"[yellow]⚠️  WARNING: --prune-missing requires metadata from previous indexing.[/yellow]\n"
                        f"[yellow]   Found 0 files with gdrive_file_id metadata, but {all_files_count:,} files exist in the database.[/yellow]\n"
                        f"[yellow]   Skipping prune to avoid accidentally marking all files as missing.[/yellow]\n"
                        f"[yellow]   To fix: clear the database and re-index from scratch.[/yellow]"
                    )
                else:
                    seen_list = list(seen_file_ids)
                    # Strategy: mark all files as missing, then restore the ones we've seen (in chunks)
                    # First, mark all files as missing
                    conn.execute(
                        "UPDATE files SET status = 'missing' WHERE drive_id = ? AND status = 'present'",
                        (drive_id,)
                    )

                    # Then, restore files we've seen (in safe chunks to avoid SQL parameter limits)
                    chunk_size = 999
                    total_restored = 0
                    for i in range(0, len(seen_list), chunk_size):
                        chunk = seen_list[i:i + chunk_size]
                        placeholders = ','.join(['?'] * len(chunk))
                        restored_count = conn.execute(
                            f"""
                            UPDATE files
                            SET status = 'present'
                            WHERE drive_id = ? AND id IN ({placeholders})
                            """,
                            [drive_id] + chunk
                        ).rowcount
                        total_restored += restored_count

                    # Files not restored are the ones marked as missing
                    total_missing = len(conn.execute(
                        "SELECT COUNT(*) FROM files WHERE drive_id = ? AND status = 'missing'",
                        (drive_id,)
                    ).fetchone()) - len(seen_list)  # This is just informational, actual count from DB

                    # Get actual count of files marked missing in this operation
                    all_files_count = conn.execute(
                        "SELECT COUNT(*) FROM files WHERE drive_id = ?",
                        (drive_id,)
                    ).fetchone()[0]
                    total_missing = all_files_count - total_restored

                    if total_missing > 0:
                        console.log(f"[yellow]Marked {total_missing:,} files as missing[/yellow]")

            if not dry_run:
                conn.commit()

    console.log(f"[green]✓ Indexed {indexed:,} files, skipped {skipped:,}[/green]")


def run_gdrive_preview(
    drive_id: str,
    db_path: pathlib.Path,
    credentials_path: Optional[pathlib.Path] = None,
    limit: Optional[int] = None,
) -> None:
    """Generate a lightweight preview of files in a Google Drive.

    Fetches file metadata without downloading or hashing, useful for understanding
    file distribution before running the full indexer.

    Args:
        drive_id: Registered drive UUID (from drives table)
        db_path: Path to the magpie database
        credentials_path: Path to OAuth credentials JSON
        limit: Max files to preview (for testing)
    """
    # Get OAuth credentials
    creds = get_credentials(credentials_path=credentials_path)
    service = build("drive", "v3", credentials=creds)

    # Look up drive in DB
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT label, serial FROM drives WHERE id = ? AND source = 'gdrive'",
            (drive_id,)
        ).fetchone()

        if not row:
            console.log(f"[red]Drive {drive_id} not found or not a Google Drive[/red]")
            raise typer.Exit(1)

        drive_label, root_folder_id = row

    console.log(f"[blue]Building preview of: {drive_label}[/blue]")

    # Fetch all files with metadata (single API call sequence with pagination)
    console.log("[blue]Fetching file list...[/blue]")
    all_files = []
    page_token = None
    while True:
        results = _with_backoff(
            lambda: service.files().list(
                spaces="drive",
                q="trashed=false",  # spaces=drive already limits to My Drive; exclude trashed files
                fields="files(id,name,parents,size,modifiedTime,mimeType),nextPageToken",
                pageSize=LIST_PAGE_SIZE,
                pageToken=page_token,
            ).execute()
        )

        all_files.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break

    # Reconstruct file paths from parent relationships
    console.log("[blue]Reconstructing paths...[/blue]")
    paths = _build_paths_from_files(all_files, service)

    if limit:
        all_files = all_files[:limit]

    total_files = len(all_files)
    console.log(f"[blue]Found {total_files:,} files to preview[/blue]")

    # Populate preview table
    scanned_at = datetime.now(timezone.utc).isoformat()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        _FilesProgressColumn(),
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task(
            "Previewing",
            files_total=total_files,
            files_done=0,
        )

        with get_connection(db_path) as conn:
            # Clear existing preview for this drive (with retry)
            _with_db_retry(
                lambda: conn.execute("DELETE FROM gdrive_preview WHERE drive_id = ?", (drive_id,))
            )

            for idx, gdfile in enumerate(all_files):
                file_id = gdfile["id"]
                name = gdfile.get("name", "")
                size_bytes = int(gdfile.get("size", 0)) if gdfile.get("size") else None
                mime_type = gdfile.get("mimeType", "")

                # Skip Google Workspace types (non-downloadable: folders, docs, sheets, etc.)
                if mime_type and mime_type.startswith("application/vnd.google-apps."):
                    continue

                # Build relative path
                rel_path = paths.get(file_id, name)

                # Extract filename and extension
                filename = pathlib.Path(name).name if name else ""
                extension = pathlib.Path(name).suffix.lstrip(".") if name else None

                # Skip Google Workspace files from extension (they don't have real extensions)
                if mime_type and mime_type.startswith("application/vnd.google-apps."):
                    extension = None

                progress.update(task, description=f"[progress.description]{filename[:60]}")

                # Insert with retry (with lambda to capture current values)
                _with_db_retry(
                    lambda: conn.execute(
                        """
                        INSERT INTO gdrive_preview (
                            id, path, filename, extension, size_bytes, mime_type, drive_id, scanned_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            file_id,
                            rel_path,
                            filename,
                            extension,
                            size_bytes,
                            mime_type,
                            drive_id,
                            scanned_at,
                        ),
                    )
                )

                # Commit periodically (with retry)
                if (idx + 1) % 100 == 0:
                    _with_db_retry(lambda: conn.commit())

            # Final commit (with retry)
            _with_db_retry(lambda: conn.commit())

        progress.update(task, files_done=total_files)

    console.log(f"[green]✓ Preview complete: {total_files:,} files[/green]")


def run_gdrive_recompute_paths(
    drive_id: str,
    db_path: pathlib.Path,
    credentials_path: Optional[pathlib.Path] = None,
    limit: Optional[int] = None,
) -> None:
    """Recompute and update file paths for all files from a Google Drive.

    Does NOT re-download files or re-hash them. Useful after a fix to path reconstruction
    logic, or if folder structure has changed in Google Drive.

    Args:
        drive_id: Registered drive UUID (from drives table)
        db_path: Path to the magpie database
        credentials_path: Path to OAuth credentials JSON
        limit: Max files to recompute (for testing)
    """
    # Get OAuth credentials
    creds = get_credentials(credentials_path=credentials_path)
    service = build("drive", "v3", credentials=creds)

    # Look up drive in DB
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT label FROM drives WHERE id = ? AND source = 'gdrive'",
            (drive_id,)
        ).fetchone()

        if not row:
            typer.echo(f"[red]Drive {drive_id} not found or not a Google Drive[/red]")
            raise typer.Exit(1)

        drive_label = row[0]

    console.log(f"[blue]Recomputing paths for: {drive_label}[/blue]")

    # Fetch all files with metadata
    console.log("[blue]Fetching file list from Google Drive...[/blue]")
    all_files = []
    page_token = None
    while True:
        results = _with_backoff(
            lambda: service.files().list(
                spaces="drive",
                q="trashed=false",
                fields="files(id,name,parents,size,modifiedTime,mimeType),nextPageToken",
                pageSize=LIST_PAGE_SIZE,
                pageToken=page_token,
            ).execute()
        )

        all_files.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break

    console.log(f"[blue]Found {len(all_files):,} files on Google Drive[/blue]")

    # Reconstruct paths for all files (with API fallback for missing parents)
    console.log("[blue]Reconstructing paths...[/blue]")
    paths = _build_paths_from_files(all_files, service)

    # Get list of indexed files for this drive
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM files WHERE drive_id = ?",
            (drive_id,)
        ).fetchall()

    if not rows:
        console.log("[yellow]No indexed files found for this drive[/yellow]")
        return

    file_ids = [row[0] for row in rows]
    if limit:
        file_ids = file_ids[:limit]

    # Look up gdrive_file_id metadata for each file and update paths
    updated = 0
    skipped = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        _FilesProgressColumn(),
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task(
            "Recomputing",
            files_total=len(file_ids),
            files_done=0,
        )

        with get_connection(db_path) as conn:
            for file_id in file_ids:
                # Get gdrive_file_id metadata
                gdrive_id_row = conn.execute(
                    "SELECT value FROM file_metadata WHERE file_id = ? AND key = 'gdrive_file_id'",
                    (file_id,)
                ).fetchone()

                if not gdrive_id_row:
                    skipped += 1
                    progress.update(task, files_done=updated + skipped)
                    continue

                gdrive_file_id = gdrive_id_row[0]
                new_rel_path = paths.get(gdrive_file_id)

                if not new_rel_path:
                    skipped += 1
                    progress.update(task, files_done=updated + skipped)
                    continue

                # Build new virtual path
                new_virt_path = f"/gdrive/{drive_label}/{new_rel_path}"

                # Update the file's path in the database
                conn.execute(
                    "UPDATE files SET path = ? WHERE id = ?",
                    (new_virt_path, file_id),
                )

                updated += 1
                progress.update(task, files_done=updated + skipped)

            conn.commit()

    console.log(f"[green]✓ Updated {updated:,} file paths, {skipped:,} files had no metadata[/green]")


def run_gdrive_thumbs(
    drive_id: str,
    db_path: pathlib.Path,
    credentials_path: Optional[pathlib.Path] = None,
    force: bool = False,
    limit: Optional[int] = None,
) -> None:
    """Download and cache thumbnails for image files from a Google Drive.

    Args:
        drive_id: Registered drive UUID (from drives table)
        db_path: Path to the magpie database
        credentials_path: Path to OAuth credentials JSON
        force: Regenerate thumbnails even if already cached
        limit: Process only first N files (for testing)
    """
    from magpie.thumbs import is_indexed_image_row, thumb_exists

    # Get OAuth credentials
    creds = get_credentials(credentials_path=credentials_path)
    service = build("drive", "v3", credentials=creds)

    # Look up drive in DB (with retry)
    with get_connection(db_path) as conn:
        row = _with_db_retry(
            lambda: conn.execute(
                "SELECT label FROM drives WHERE id = ? AND source = 'gdrive'",
                (drive_id,)
            ).fetchone()
        )

        if not row:
            console.log(f"[red]Google Drive {drive_id} not found[/red]")
            raise typer.Exit(1)

        drive_label = row[0]

    console.log(f"[blue]Downloading thumbnails for: {drive_label}[/blue]")

    # Query for all image files from this drive (with retry)
    with get_connection(db_path) as conn:
        rows = _with_db_retry(
            lambda: conn.execute(
                """
                SELECT f.id, f.mime_type, f.extension, f.size_bytes, f.perceptual_hash,
                       fm.value as gdrive_file_id
                FROM files f
                LEFT JOIN file_metadata fm ON f.id = fm.file_id AND fm.key = 'gdrive_file_id'
                WHERE f.drive_id = ? AND f.status = 'present'
                AND (f.mime_type LIKE 'image/%' OR f.extension IN ('jpg','jpeg','png','gif','bmp','tiff','tif','webp','heic','heif'))
                AND NOT EXISTS (
                    SELECT 1 FROM labels l
                    WHERE l.file_id = f.id AND l.action = 'trash'
                )
                ORDER BY f.path
                """ + (f" LIMIT {limit}" if limit else ""),
                (drive_id,)
            ).fetchall()
        )

    if not rows:
        console.log("[yellow]No indexed image files found.[/yellow]")
        return

    thumb_count = 0
    skip_count = 0
    no_metadata_count = 0
    not_image_count = 0
    cached_count = 0
    no_url_count = 0
    download_failed_count = 0
    sample_skips = []  # Track first few skips for debugging

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Downloading thumbnails…", total=len(rows))

        for idx, row in enumerate(rows):
            file_id = row["id"]
            mime_type = row["mime_type"]
            extension = row["extension"]
            size_bytes = row["size_bytes"]
            gdrive_file_id = row["gdrive_file_id"]

            # Skip if not an image or no Google Drive file ID
            if not is_indexed_image_row(mime_type, extension) or not gdrive_file_id:
                progress.advance(task)
                skip_count += 1
                if not gdrive_file_id:
                    no_metadata_count += 1
                else:
                    not_image_count += 1
                if len(sample_skips) < 3:
                    sample_skips.append(f"No metadata/image: {file_id}")
                continue

            already_has_thumb = thumb_exists(file_id)
            already_has_phash = row["perceptual_hash"] is not None

            # Skip if already cached and pHash already computed (unless force)
            if not force and already_has_thumb and already_has_phash:
                progress.advance(task)
                skip_count += 1
                cached_count += 1
                if len(sample_skips) < 3:
                    sample_skips.append(f"Already cached: {file_id}")
                continue

            # Fetch thumbnail URL from Google Drive API
            try:
                file_meta = _with_backoff(
                    lambda: service.files().get(
                        fileId=gdrive_file_id,
                        fields="thumbnailLink",
                    ).execute()
                )
                thumbnail_url = file_meta.get("thumbnailLink")

                if thumbnail_url:
                    result = _download_gdrive_thumbnail(thumbnail_url, file_id, size_bytes or 0)
                    if result:
                        thumb_count += 1
                        if not already_has_phash or force:
                            from magpie.thumbs import compute_phash
                            phash = compute_phash(result)
                            if phash:
                                with get_connection(db_path) as wconn:
                                    wconn.execute(
                                        "UPDATE files SET perceptual_hash = ? WHERE id = ?",
                                        (phash, file_id),
                                    )
                    else:
                        download_failed_count += 1
                        skip_count += 1
                        if len(sample_skips) < 3:
                            sample_skips.append(f"Download failed: {file_id}")
                else:
                    no_url_count += 1
                    skip_count += 1
                    if len(sample_skips) < 3:
                        sample_skips.append(f"No URL returned: {file_id}")
            except Exception as e:
                console.log(f"[yellow]⚠ Failed to get thumbnail for {gdrive_file_id}: {e}[/yellow]")
                skip_count += 1
                if len(sample_skips) < 3:
                    sample_skips.append(f"API error: {gdrive_file_id}")

            progress.advance(task)

    console.log(
        f"\n[green]Done[/green]\n"
        f"  Thumbnails downloaded: {thumb_count}\n"
        f"  Skipped:              {skip_count}\n"
        f"    - Already cached: {cached_count}\n"
        f"    - No gdrive_file_id metadata: {no_metadata_count}\n"
        f"    - Not an image: {not_image_count}\n"
        f"    - No thumbnailLink from API: {no_url_count}\n"
        f"    - Download failed: {download_failed_count}"
    )
    if sample_skips:
        console.log("\n[dim]Sample skipped files:[/dim]")
        for skip in sample_skips:
            console.log(f"  {skip}")
