"""On-demand Dropbox file download cache for the web UI and executor."""

import pathlib
from typing import Optional, Union

import dropbox
from dropbox.exceptions import ApiError
from rich.console import Console

from magpie.db import get_connection

console = Console()

DB_DIR = pathlib.Path.home() / ".magpie"
DROPBOX_CACHE_DIR = DB_DIR / "dropbox_cache"


def lookup_dropbox_file_id(conn, file_id: str) -> Optional[str]:
    """Get the Dropbox file ID from file metadata."""
    row = conn.execute(
        "SELECT value FROM file_metadata WHERE file_id = ? AND key = 'dropbox_file_id'",
        (file_id,),
    ).fetchone()
    return row[0] if row else None


def lookup_dropbox_path(conn, file_id: str) -> Optional[str]:
    """Get the Dropbox path from the files table."""
    row = conn.execute(
        "SELECT path FROM files WHERE id = ?",
        (file_id,),
    ).fetchone()
    if not row:
        return None

    path = row[0]
    # Remove the virtual /dropbox/label prefix to get the actual Dropbox path
    if path.startswith("/dropbox/"):
        parts = path.split("/", 3)
        if len(parts) >= 4:
            return "/" + parts[3]
    return None


def get_dropbox_file(
    dbx: dropbox.Dropbox,
    file_id: str,
    ext: Optional[str] = None,
    *,
    stream: bool = False,
    cache_dir: Optional[pathlib.Path] = None,
) -> Union[pathlib.Path, bytes, None]:
    """Get a Dropbox file from cache or download it.

    Args:
        dbx: Dropbox client instance
        file_id: File UUID (will look up dropbox file ID in metadata)
        ext: File extension for cache filename
        stream: If True, download to memory and return bytes instead of path
        cache_dir: Directory to cache files (default: ~/.magpie/dropbox_cache)

    Returns:
        Path to cached file, bytes content, or None on error
    """
    cache_dir = cache_dir or DROPBOX_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Look up Dropbox file ID and path
    from magpie.db import get_connection, DB_PATH
    with get_connection(DB_PATH) as conn:
        dropbox_file_id = lookup_dropbox_file_id(conn, file_id)
        dropbox_path = lookup_dropbox_path(conn, file_id)

    if not dropbox_path:
        console.log(f"[red]Dropbox path not found for {file_id}[/red]")
        return None

    if stream:
        return _download_dropbox_bytes(dbx, dropbox_path)

    # Check cache first
    cache_path = cache_dir / f"{file_id}.{ext or 'bin'}"
    if cache_path.exists():
        return cache_path

    # Download and cache
    try:
        content = _download_dropbox_bytes(dbx, dropbox_path)
        if content:
            cache_path.write_bytes(content)
            return cache_path
    except Exception as e:
        console.log(f"[red]Failed to download {dropbox_path}: {e}[/red]")

    return None


def _download_dropbox_bytes(dbx: dropbox.Dropbox, path: str) -> Optional[bytes]:
    """Download file bytes from Dropbox path."""
    try:
        metadata, response = dbx.files_download(path)
        return response.content
    except ApiError as e:
        if hasattr(e, "error"):
            if hasattr(e.error, "is_not_found") and e.error.is_not_found():
                console.log(f"[yellow]File not found: {path}[/yellow]")
                return None
            if hasattr(e.error, "is_restricted_content") and e.error.is_restricted_content():
                console.log(f"[yellow]Restricted content: {path}[/yellow]")
                return None
        raise
