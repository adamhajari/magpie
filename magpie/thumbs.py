"""Perceptual hashing and thumbnail generation for image files."""

from __future__ import annotations

import pathlib
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

from magpie.db import get_connection

console = Console()

THUMBS_DIR = pathlib.Path.home() / ".magpie" / "thumbs"
THUMB_SIZE = (256, 256)

# Extensions we'll attempt to open as images for pHash/thumbnail
_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".webp", ".heic", ".heif",
}

_heif_opener_registered = False


def _ensure_heif_opener() -> None:
    """Register pillow-heif once so HEIC/HEIF open in Pillow (optional dependency)."""
    global _heif_opener_registered
    if _heif_opener_registered:
        return
    _heif_opener_registered = True
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:
        pass


def is_indexed_image_row(mime_type: str | None, extension: str | None) -> bool:
    """True if DB mime/extension indicate an image we try to thumbnail (matches indexer extensions)."""
    if (mime_type or "").lower().startswith("image/"):
        return True
    ext = (extension or "").lower()
    if not ext:
        return False
    return f".{ext}" in _IMAGE_EXTENSIONS


def _thumbs_dir() -> pathlib.Path:
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    return THUMBS_DIR


def compute_phash(path: pathlib.Path) -> Optional[str]:
    """Return the pHash hex string for an image, or None on failure."""
    try:
        import imagehash
        from PIL import Image

        _ensure_heif_opener()
        with Image.open(path) as img:
            return str(imagehash.phash(img))
    except Exception:
        return None


def generate_thumbnail(path: pathlib.Path, file_id: str) -> Optional[pathlib.Path]:
    """
    Generate a 256px thumbnail saved to ~/.magpie/thumbs/{file_id}.jpg.
    Returns the thumbnail path on success, None on failure.
    """
    try:
        from PIL import Image

        _ensure_heif_opener()
        thumb_path = _thumbs_dir() / f"{file_id}.jpg"
        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail(THUMB_SIZE)
            img.save(thumb_path, "JPEG", quality=85)
        return thumb_path
    except Exception:
        return None


def thumb_exists(file_id: str) -> bool:
    return (THUMBS_DIR / f"{file_id}.jpg").exists()


def process_image(
    file_id: str,
    path: pathlib.Path,
    conn: sqlite3.Connection,
    with_thumbs: bool,
    force: bool,
) -> dict[str, bool]:
    """
    Compute pHash and optionally generate a thumbnail for one image file.

    Skips pHash if already set (unless force=True).
    Skips thumbnail if already on disk (unless force=True).

    Returns {"phash": True/False, "thumb": True/False} indicating what was done.
    """
    done = {"phash": False, "thumb": False}

    row = conn.execute("SELECT perceptual_hash FROM files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        return done

    needs_phash = force or row["perceptual_hash"] is None
    needs_thumb = with_thumbs and (force or not thumb_exists(file_id))

    if not needs_phash and not needs_thumb:
        return done

    if needs_phash:
        phash = compute_phash(path)
        if phash is not None:
            conn.execute(
                "UPDATE files SET perceptual_hash = ? WHERE id = ?",
                (phash, file_id),
            )
            done["phash"] = True

    if needs_thumb:
        result = generate_thumbnail(path, file_id)
        done["thumb"] = result is not None

    return done


def run_thumbs(
    path: pathlib.Path,
    db: pathlib.Path,
    with_thumbs: bool = True,
    force: bool = False,
) -> None:
    """
    Entry point for `magpie thumbs /path`.

    Queries already-indexed image files under *path* and computes pHash /
    generates thumbnails without re-indexing.
    """
    if not path.exists():
        console.print(f"[red]Path does not exist:[/red] {path}")
        raise SystemExit(1)

    root_str = str(path.resolve())

    with get_connection(db) as conn:
        rows = conn.execute(
            """SELECT id, path FROM files
               WHERE path LIKE ?
                 AND status = 'present'
                 AND (extension IN ('jpg','jpeg','png','gif','bmp','tiff','tif','webp','heic','heif')
                      OR mime_type LIKE 'image/%')
                 AND NOT EXISTS (
                     SELECT 1 FROM labels l
                     WHERE l.file_id = f.id AND l.action = 'trash'
                 )""",
            (root_str + "%",),
        ).fetchall()

    if not rows:
        console.print("[yellow]No indexed image files found under that path.[/yellow]")
        return

    phash_count = 0
    thumb_count = 0
    skip_count = 0

    with get_connection(db) as conn:
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
            task_id = progress.add_task("Processing images…", total=len(rows))
            for row in rows:
                file_path = pathlib.Path(row["path"])
                progress.update(task_id, description=f"[dim]{file_path.name}[/dim]")

                if not file_path.exists():
                    progress.advance(task_id)
                    skip_count += 1
                    continue

                result = process_image(
                    file_id=row["id"],
                    path=file_path,
                    conn=conn,
                    with_thumbs=with_thumbs,
                    force=force,
                )
                if result["phash"]:
                    phash_count += 1
                if result["thumb"]:
                    thumb_count += 1
                if not result["phash"] and not result["thumb"]:
                    skip_count += 1

                progress.advance(task_id)

    console.print(
        f"\n[green]Done[/green]\n"
        f"  pHashes computed:   {phash_count}\n"
        f"  Thumbnails created: {thumb_count}\n"
        f"  Skipped:            {skip_count}  (already processed or missing)"
    )
