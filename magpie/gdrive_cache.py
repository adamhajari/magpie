"""Google Drive file download cache with streaming and persistent modes."""

from __future__ import annotations

import pathlib
import sqlite3
import time
from typing import Optional

from googleapiclient.errors import HttpError

from magpie.db import DB_DIR, get_connection

GDRIVE_CACHE_DIR = DB_DIR / "gdrive_cache"

# Rate limit: Google Drive API allows ~10 requests/second per user
MAX_RETRIES = 5
INITIAL_BACKOFF = 0.5  # seconds


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
            time.sleep(delay)


def lookup_gdrive_file_id(conn: sqlite3.Connection, file_id: str) -> Optional[str]:
    """Return the Google Drive native file ID for a magpie file UUID.

    Args:
        conn: Database connection
        file_id: Magpie file UUID

    Returns:
        Google Drive file ID or None if not found
    """
    row = conn.execute(
        "SELECT value FROM file_metadata WHERE file_id = ? AND key = 'gdrive_file_id'",
        (file_id,)
    ).fetchone()
    return row[0] if row else None


def get_gdrive_file(
    service,
    gdrive_file_id: str,
    ext: str = "",
    *,
    stream: bool = False,
    cache_dir: Optional[pathlib.Path] = None,
) -> pathlib.Path | bytes | None:
    """Download a file from Google Drive with optional caching.

    If stream=True: download bytes into memory and return bytes (no disk write).
    If stream=False (default): check local cache, download if missing, return pathlib.Path.

    Args:
        service: Google Drive API service instance
        gdrive_file_id: Google Drive file ID
        ext: File extension with dot (e.g. '.jpg'). Used for cache filename.
        stream: If True, return bytes only (no caching). If False, cache on disk.
        cache_dir: Cache directory. Defaults to GDRIVE_CACHE_DIR if None.

    Returns:
        - If stream=True: bytes or None on error
        - If stream=False: pathlib.Path to cached file, or None on error
    """
    if cache_dir is None:
        cache_dir = GDRIVE_CACHE_DIR

    if stream:
        # Streaming mode: download to memory only
        return _download_gdrive_bytes(service, gdrive_file_id)

    # Cached mode: check local cache first, download if needed
    cache_path = cache_dir / f"{gdrive_file_id}{ext}"

    if cache_path.exists():
        return cache_path

    # Download and cache
    file_bytes = _download_gdrive_bytes(service, gdrive_file_id)
    if file_bytes is None:
        return None

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(file_bytes)
    return cache_path


def _download_gdrive_bytes(service, gdrive_file_id: str) -> Optional[bytes]:
    """Download raw bytes from Google Drive.

    Args:
        service: Google Drive API service instance
        gdrive_file_id: Google Drive file ID

    Returns:
        File bytes or None on error (403/404)
    """
    try:
        request = service.files().get_media(fileId=gdrive_file_id)
        file_bytes = _with_backoff(lambda: request.execute())
        return file_bytes
    except HttpError as e:
        if e.resp.status == 404:
            return None  # File deleted between list and download
        elif e.resp.status == 403:
            # File cannot be downloaded (shared file, restricted access, etc.)
            return None
        raise
