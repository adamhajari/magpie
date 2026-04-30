"""Photo analyzer — local Ollama/LLaVA via Ollama."""

from __future__ import annotations

import io
import os
import pathlib
import tempfile
from typing import Optional

from PIL import Image
from rich.console import Console
from rich.progress import track

from magpie.analyzer import (
    ANALYZER_LOCAL_LLAVA,
    _store_analysis,
    call_ollama_llava,
    check_ollama_available,
)
from magpie.db import DB_PATH, get_connection
from magpie.gdrive_cache import get_gdrive_file, lookup_gdrive_file_id

console = Console()

PHOTO_EXTENSIONS = {
    "jpg",
    "jpeg",
    "png",
    "webp",
    "heic",
    "heif",
    "cr2",
    "arw",
    "nef",
}
PHOTO_MIMES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
    "image/x-adobe-dng",
}
RAW_EXTENSIONS = {"cr2", "arw", "nef"}

MAX_IMAGE_SIDE = 2048
JPEG_QUALITY = 88

_heif_registered = False


def _register_heif_opener() -> None:
    global _heif_registered
    if _heif_registered:
        return
    from pillow_heif import register_heif_opener

    register_heif_opener()
    _heif_registered = True


def _is_photo(extension: str, mime: Optional[str]) -> bool:
    ext = (extension or "").lower().lstrip(".")
    if ext in PHOTO_EXTENSIONS:
        return True
    if mime:
        m = mime.lower()
        if m in PHOTO_MIMES:
            return True
        if m.startswith("image/") and "raw" in m:
            return True
    return False


def _resize_max_side(img: Image.Image, max_side: int = MAX_IMAGE_SIDE) -> Image.Image:
    w, h = img.size
    if max(w, h) <= max_side:
        return img
    scale = max_side / max(w, h)
    new_w, new_h = int(w * scale), int(h * scale)
    return img.resize((new_w, new_h), Image.Resampling.LANCZOS)


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        return bg
    if img.mode == "P":
        img = img.convert("RGBA")
        return _to_rgb(img)
    return img.convert("RGB")


def _load_raw_as_pil(path: pathlib.Path) -> Image.Image:
    import rawpy

    with rawpy.imread(str(path)) as raw:
        rgb = raw.postprocess()
    return Image.fromarray(rgb)


def prepare_image_jpeg_bytes(path: pathlib.Path, extension: str) -> bytes:
    """Decode file to RGB, optionally via rawpy for RAW, then JPEG bytes for the API."""
    ext = (extension or path.suffix.lstrip(".")).lower()
    if ext in RAW_EXTENSIONS:
        img = _load_raw_as_pil(path)
    else:
        if ext in ("heic", "heif"):
            _register_heif_opener()
        img = Image.open(path)
        img = _to_rgb(img)
    img = _resize_max_side(img)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def _get_photo_path(
    row: dict,
    db_path: pathlib.Path,
    gdrive_service: Optional[object] = None,
) -> Optional[pathlib.Path]:
    """Get the file path for a photo, using Google Drive cache if needed.

    Args:
        row: Photo row dict with id, path, extension
        db_path: Database path
        gdrive_service: Google Drive service instance (optional, used to download if not cached)

    Returns:
        Path to the file (local or cached), or None if unavailable
    """
    p = pathlib.Path(row["path"])

    # Check if it's a Google Drive file
    if "/gdrive/" in str(p):
        # Get the Google Drive file ID from metadata
        with get_connection(db_path) as conn:
            gdrive_file_id = lookup_gdrive_file_id(conn, row["id"])
        if not gdrive_file_id:
            return None

        # Check if it's cached locally (filenames are: {gdrive_file_id}{extension} with no dot)
        from magpie.gdrive_cache import GDRIVE_CACHE_DIR
        cache_path = GDRIVE_CACHE_DIR / f"{gdrive_file_id}{(row['extension'] or 'jpg').lower()}"
        if cache_path.exists():
            return cache_path

        # Try to download if service available
        if gdrive_service:
            cache_path = get_gdrive_file(
                gdrive_service,
                gdrive_file_id,
                ext="." + (row["extension"] or "jpg"),
                stream=False,
            )
            return cache_path

        # No cache and no service to download
        return None

    return p if p.is_file() else None


def _find_photos(
    path: pathlib.Path,
    db_path: pathlib.Path,
    limit: Optional[int],
    force: bool,
    analyzer: str = ANALYZER_LOCAL_LLAVA,
    drive_id: Optional[str] = None,
) -> list[dict]:
    with get_connection(db_path) as conn:
        query = """
            SELECT f.id, f.path, f.extension, f.mime_type, f.drive_id
            FROM files f
            WHERE f.status = 'present'
              AND f.path LIKE ?
        """
        params: list = [str(path).rstrip("/") + "%"]

        if drive_id:
            query += " AND f.drive_id = ?"
            params.append(drive_id)

        if not force:
            query += """
              AND NOT EXISTS (
                  SELECT 1 FROM file_analysis fa
                  WHERE fa.file_id = f.id AND fa.analyzer = ?
              )
            """
            params.append(analyzer)

        query += """
          AND NOT EXISTS (
              SELECT 1 FROM labels l
              WHERE l.file_id = f.id AND l.action = 'trash'
          )
        """

        # Don't apply limit in SQL; apply it after filtering for photos
        # (most files are non-photos, so limiting before filtering gives few results)
        rows = conn.execute(query, params).fetchall()
        photos = []
        for row in rows:
            if os.path.basename(row["path"]).startswith("._"):
                continue
            if _is_photo(row["extension"] or "", row["mime_type"]):
                photos.append(dict(row))
                if limit is not None and len(photos) >= limit:
                    break
    return photos


def _ollama_caption_prepared_jpeg(
    row: dict,
    ollama_model: str,
    db_path: pathlib.Path = DB_PATH,
) -> dict:
    """Resize/decode like the cloud path, then send JPEG bytes to Ollama via a temp file."""
    # Get the file path (handles Google Drive cache)
    p = _get_photo_path(row, db_path, gdrive_service=None)
    if p is None:
        raise FileNotFoundError(f"File not found: {row['path']}")

    jpeg_bytes = prepare_image_jpeg_bytes(p, row["extension"] or "")
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        tf.write(jpeg_bytes)
        tmp_path = pathlib.Path(tf.name)
    try:
        return call_ollama_llava(tmp_path, model=ollama_model)
    finally:
        tmp_path.unlink(missing_ok=True)


def run_analyze_photos(
    path: pathlib.Path,
    db_path: pathlib.Path = DB_PATH,
    dry_run: bool = False,
    limit: Optional[int] = None,
    force: bool = False,
    ollama_model: str = "llava",
    drive_id: Optional[str] = None,
    gdrive_service: Optional[object] = None,
) -> None:
    """Main entry point for `magpie analyze photos`.

    Args:
        path: Starting path for photos
        db_path: Database path
        dry_run: If True, show what would be analyzed without running
        limit: Max photos to analyze
        force: If True, re-analyze already-analyzed photos
        ollama_model: Ollama vision model to use
        drive_id: Optional drive ID to limit analysis to a specific drive
        gdrive_service: Google Drive service instance (used to download Google Drive files to cache)
    """
    is_gdrive = str(path).startswith("/gdrive/")
    if not is_gdrive and not path.exists():
        console.print(f"[red]Path does not exist: {path}[/red]")
        raise SystemExit(1)

    photos = _find_photos(path, db_path, limit, force, analyzer=ANALYZER_LOCAL_LLAVA, drive_id=drive_id)

    if not photos:
        console.print("[green]No photos to analyze.[/green]")
        return

    if dry_run:
        from rich.table import Table
        title = f"[bold]Dry run — {len(photos)} photo(s) would be analyzed[/bold] [dim](local LLaVA via Ollama)[/dim]"
        table = Table(title=title)
        table.add_column("Path", style="cyan", no_wrap=False)
        table.add_column("Type", style="dim")
        for row in photos[:50]:
            p = pathlib.Path(row["path"])
            rel = str(p.relative_to(path)) if p.is_relative_to(path) else str(p)
            table.add_row(rel, row["extension"] or "?")
        if len(photos) > 50:
            table.add_row(f"... and {len(photos) - 50} more", "")
        console.print(table)
        console.print(
            "\n[dim]Local mode: Ollama + vision model (analyzer=local-llava). "
            "Requires a running Ollama daemon.[/dim]"
        )
        return

    check_ollama_available()
    console.print(
        f"Analyzing [bold]{len(photos)}[/bold] image(s) with [bold]Ollama/{ollama_model}[/bold]…"
    )
    ok = 0
    errors = 0
    for row in track(photos, description="Captioning…"):
        filename = pathlib.Path(row["path"]).name
        file_id = row["id"]
        try:
            result = _ollama_caption_prepared_jpeg(row, ollama_model, db_path)
            _store_analysis(
                db_path,
                row["id"],
                result["summary"],
                result["tags"],
                "",
                ANALYZER_LOCAL_LLAVA,
                settings=result.get("settings"),
                people=result.get("people"),
                quality=result.get("quality"),
            )
            console.print(f"[green]✓[/green] {filename} ({file_id})")
            ok += 1
        except Exception as exc:
            console.print(f"[red]✗ {filename} ({file_id}): {exc}[/red]")
            errors += 1
    console.print(f"\n[green]Done.[/green] {ok} analyzed, {errors} errors.")


__all__ = [
    "run_analyze_photos",
    "_find_photos",
    "_is_photo",
    "prepare_image_jpeg_bytes",
]
