"""Audio metadata extraction using mutagen.

Extracts title, artist, album, track number, year, duration, sample rate,
bit rate, channels, and embedded cover art from audio files.
"""

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

from magpie.db import DB_PATH, get_connection

console = Console()

try:
    from mutagen import File as MutagenFile  # type: ignore[import-untyped]
    _MUTAGEN_AVAILABLE = True
except ImportError:  # pragma: no cover
    MutagenFile = None  # type: ignore[assignment,misc]
    _MUTAGEN_AVAILABLE = False

THUMBS_DIR = pathlib.Path.home() / ".magpie" / "thumbs"

# MIME types we treat as audio
AUDIO_MIME_PREFIXES = ("audio/",)
AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".flac", ".wav", ".aac", ".ogg", ".aiff", ".aif",
    ".opus", ".wma", ".alac",
}


def is_audio(mime_type: Optional[str], extension: Optional[str]) -> bool:
    """True if the file should be processed by the audio pipeline."""
    if extension and f".{extension.lower()}" in AUDIO_EXTENSIONS:
        return True
    if mime_type:
        for prefix in AUDIO_MIME_PREFIXES:
            if mime_type.lower().startswith(prefix):
                return True
    return False


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def _first(tag_obj) -> Optional[str]:
    """Return the first string value from a mutagen tag object, or None."""
    if tag_obj is None:
        return None
    try:
        val = tag_obj[0] if hasattr(tag_obj, "__getitem__") else str(tag_obj)
        return str(val).strip() or None
    except (IndexError, TypeError):
        return None


def extract_audio_metadata(path: pathlib.Path) -> dict:
    """
    Return a dict of extracted fields.  Keys:

    Textual (stored in file_metadata):
        title, artist, album, track_number, year

    Numeric (also stored in files columns):
        duration_seconds (float), sample_rate (int), bit_rate (int), channels (int)

    Cover art:
        cover_art_data (bytes | None)
    """
    if not _MUTAGEN_AVAILABLE or MutagenFile is None:
        return {}

    result: dict = {
        "title": None,
        "artist": None,
        "album": None,
        "track_number": None,
        "year": None,
        "duration_seconds": None,
        "sample_rate": None,
        "bit_rate": None,
        "channels": None,
        "cover_art_data": None,
    }

    try:
        audio = MutagenFile(str(path), easy=False)  # type: ignore[misc]
    except Exception:
        return result

    if audio is None:
        return result

    # Duration from StreamInfo
    info = getattr(audio, "info", None)
    if info is not None:
        duration = getattr(info, "length", None)
        if duration is not None:
            result["duration_seconds"] = float(duration)
        sample_rate = getattr(info, "sample_rate", None)
        if sample_rate is not None:
            result["sample_rate"] = int(sample_rate)
        # mutagen uses 'bitrate' (bits/s) for some formats and 'bit_rate' for others
        for attr in ("bitrate", "bit_rate"):
            br = getattr(info, attr, None)
            if br is not None:
                result["bit_rate"] = int(br)
                break
        channels = getattr(info, "channels", None)
        if channels is not None:
            result["channels"] = int(channels)

    tags = audio.tags
    if tags is None:
        return result

    # MP3 / ID3
    try:
        from mutagen.id3 import ID3
        if isinstance(tags, ID3):
            result["title"] = _first(tags.get("TIT2"))
            result["artist"] = _first(tags.get("TPE1")) or _first(tags.get("TPE2"))
            result["album"] = _first(tags.get("TALB"))
            result["track_number"] = _first(tags.get("TRCK"))
            result["year"] = _first(tags.get("TDRC")) or _first(tags.get("TYER"))
            # Cover art — APIC frames
            for key in tags.keys():
                if key.startswith("APIC"):
                    apic = tags[key]
                    data = getattr(apic, "data", None)
                    if data:
                        result["cover_art_data"] = data
                        break
            return result
    except ImportError:
        pass

    # MP4 / M4A
    try:
        from mutagen.mp4 import MP4Tags
        if isinstance(tags, MP4Tags):
            result["title"] = _first(tags.get("\xa9nam"))
            result["artist"] = _first(tags.get("\xa9ART"))
            result["album"] = _first(tags.get("\xa9alb"))
            trk = tags.get("trkn")
            if trk:
                try:
                    result["track_number"] = str(trk[0][0])
                except (IndexError, TypeError):
                    pass
            result["year"] = _first(tags.get("\xa9day"))
            # Cover art
            covr = tags.get("covr")
            if covr:
                result["cover_art_data"] = bytes(covr[0])
            return result
    except ImportError:
        pass

    # FLAC / OGG / Vorbis comment style (dict-like with list values)
    if hasattr(tags, "get"):
        def _tag(key: str) -> Optional[str]:
            v = tags.get(key) or tags.get(key.upper()) or tags.get(key.lower())
            if v:
                return str(v[0]).strip() or None
            return None

        result["title"] = _tag("title")
        result["artist"] = _tag("artist") or _tag("albumartist")
        result["album"] = _tag("album")
        result["track_number"] = _tag("tracknumber")
        result["year"] = _tag("date") or _tag("year")

    # FLAC cover art via pictures
    pictures = getattr(audio, "pictures", None)
    if pictures:
        result["cover_art_data"] = pictures[0].data

    return result


def save_cover_art(file_id: str, data: bytes) -> Optional[pathlib.Path]:
    """
    Save raw image *data* as a JPEG thumbnail to ~/.magpie/thumbs/{file_id}.jpg.
    Returns the path on success, None on failure.
    """
    try:
        from PIL import Image
        import io

        THUMBS_DIR.mkdir(parents=True, exist_ok=True)
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        img.thumbnail((256, 256))
        out = THUMBS_DIR / f"{file_id}.jpg"
        img.save(out, "JPEG", quality=85)
        return out
    except Exception:
        return None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def store_audio_metadata(
    file_id: str,
    metadata: dict,
    conn: sqlite3.Connection,
) -> None:
    """
    Write extracted metadata to file_metadata (key/value) and update the
    duration_seconds / sample_rate / bit_rate columns on the files row.
    """
    text_keys = ("title", "artist", "album", "track_number", "year", "channels")
    for key in text_keys:
        value = metadata.get(key)
        if value is not None:
            conn.execute(
                "INSERT OR REPLACE INTO file_metadata (file_id, key, value) VALUES (?, ?, ?)",
                (file_id, key, str(value)),
            )

    # Numeric columns on files
    updates: dict[str, object] = {}
    if metadata.get("duration_seconds") is not None:
        updates["duration_seconds"] = metadata["duration_seconds"]
    if metadata.get("sample_rate") is not None:
        updates["sample_rate"] = metadata["sample_rate"]
    if metadata.get("bit_rate") is not None:
        updates["bit_rate"] = metadata["bit_rate"]

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE files SET {set_clause} WHERE id = ?",
            (*updates.values(), file_id),
        )

    # Cover art
    cover_data = metadata.get("cover_art_data")
    if cover_data:
        save_cover_art(file_id, cover_data)


# ---------------------------------------------------------------------------
# process_audio — called from indexer for a single file
# ---------------------------------------------------------------------------

def process_audio(
    file_id: str,
    path: pathlib.Path,
    conn: sqlite3.Connection,
    force: bool = False,
) -> None:
    """Extract audio metadata for *path* and persist it.  Skips if already done unless *force*."""
    if not force:
        existing = conn.execute(
            "SELECT 1 FROM file_metadata WHERE file_id = ? LIMIT 1", (file_id,)
        ).fetchone()
        if existing:
            return

    metadata = extract_audio_metadata(path)
    store_audio_metadata(file_id, metadata, conn)


# ---------------------------------------------------------------------------
# run_analyze_audio — CLI entry point
# ---------------------------------------------------------------------------

def run_analyze_audio(
    path: pathlib.Path,
    db_path: pathlib.Path = DB_PATH,
    dry_run: bool = False,
    limit: Optional[int] = None,
    force: bool = False,
) -> None:
    """Walk already-indexed audio files under *path* and fill in missing metadata."""
    if not path.exists():
        console.print(f"[red]Path does not exist:[/red] {path}")
        return

    root_str = str(path.resolve())

    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT f.id, f.path, f.mime_type, f.extension
              FROM files f
             WHERE f.path LIKE ?
               AND f.status = 'present'
               AND NOT EXISTS (
                   SELECT 1 FROM labels l
                   WHERE l.file_id = f.id AND l.action = 'trash'
               )
            """,
            (root_str + "%",),
        ).fetchall()

    # Filter to audio files
    audio_rows = [r for r in rows if is_audio(r["mime_type"], r["extension"])]

    if not force:
        with get_connection(db_path) as conn:
            processed_ids = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT file_id FROM file_metadata"
                ).fetchall()
            }
        audio_rows = [r for r in audio_rows if r["id"] not in processed_ids]

    if limit is not None:
        audio_rows = audio_rows[:limit]

    if dry_run:
        console.print(f"[dim]Dry run — would process {len(audio_rows)} audio file(s)[/dim]")
        for r in audio_rows[:20]:
            console.print(f"  {r['path']}")
        if len(audio_rows) > 20:
            console.print(f"  … and {len(audio_rows) - 20} more")
        return

    if not audio_rows:
        console.print("[dim]No audio files to process.[/dim]")
        return

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
        task_id = progress.add_task("Extracting audio metadata…", total=len(audio_rows))

        with get_connection(db_path) as conn:
            for row in audio_rows:
                progress.update(task_id, description=f"[dim]{pathlib.Path(row['path']).name}[/dim]")
                try:
                    metadata = extract_audio_metadata(pathlib.Path(row["path"]))
                    store_audio_metadata(row["id"], metadata, conn)
                except Exception as exc:
                    console.print(f"[yellow]Warning:[/yellow] {row['path']}: {exc}")
                finally:
                    progress.advance(task_id)

    console.print(f"[green]Done.[/green] Processed {len(audio_rows)} audio file(s).")
