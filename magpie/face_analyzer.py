"""Face detection and 512-d embeddings via InsightFace (buffalo_l / ONNX Runtime).

Supports both local files and Google Drive files (via on-demand download cache).
The buffalo_l model (~300 MB) is downloaded automatically on first run to ~/.insightface/.
Inference runs via ONNX Runtime with CoreML execution provider on Apple Silicon (Metal GPU).
"""

from __future__ import annotations

import json
import math
import pathlib
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional, TypeVar

import numpy as np
from PIL import Image
from rich.console import Console
from rich.progress import track
from rich.table import Table

from magpie.db import DB_DIR, get_connection
from magpie.photo_analyzer import (
    RAW_EXTENSIONS,
    _load_raw_as_pil,
    _register_heif_opener,
    _is_photo,
    _to_rgb,
)

console = Console()

FACES_CACHE_DIR = DB_DIR / "faces"

T = TypeVar("T")


def _with_retry(
    fn: Callable[[], T],
    max_retries: int = 10,
    initial_delay: float = 0.2,
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _insightface_bbox_to_xywh(bbox: np.ndarray) -> list[int]:
    """InsightFace returns [x1, y1, x2, y2] → convert to [x, y, w, h]."""
    x1, y1, x2, y2 = (int(v) for v in bbox)
    return [x1, y1, x2 - x1, y2 - y1]


# Keep the old name as an alias used in tests (top/right/bottom/left → [x, y, w, h])
def _location_to_bbox(top: int, right: int, bottom: int, left: int) -> list[int]:
    """face_recognition order (top, right, bottom, left) → [x, y, w, h]."""
    x, y = left, top
    w, h = right - left, bottom - top
    return [x, y, w, h]


def _embedding_to_blob(encoding: np.ndarray) -> bytes:
    return np.asarray(encoding, dtype=np.float32).reshape(512).tobytes()


def load_rgb_numpy(path: pathlib.Path, extension: str) -> np.ndarray:
    """Load image as H×W×3 uint8 RGB."""
    ext = (extension or path.suffix.lstrip(".")).lower()
    if ext in RAW_EXTENSIONS:
        img = _load_raw_as_pil(path)
    else:
        if ext in ("heic", "heif"):
            _register_heif_opener()
        img = Image.open(path)
        img = _to_rgb(img)
    return np.asarray(img, dtype=np.uint8)


def _face_thumbnail(
    rgb: np.ndarray,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    pad_ratio: float = 0.35,
    max_side: int = 256,
) -> Image.Image:
    h, w = rgb.shape[:2]
    face_w = max(1, x2 - x1)
    face_h = max(1, y2 - y1)
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0
    side = max(face_w, face_h) * (1.0 + 2.0 * pad_ratio)
    half = side / 2.0
    rx1 = max(0, int(math.floor(cx - half)))
    ry1 = max(0, int(math.floor(cy - half)))
    rx2 = min(w, int(math.ceil(cx + half)))
    ry2 = min(h, int(math.ceil(cy + half)))
    crop = rgb[ry1:ry2, rx1:rx2]
    pil = Image.fromarray(crop)
    pil.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return pil


def _reset_file_faces(conn, file_id: str, faces_dir: pathlib.Path) -> None:
    conn.execute("DELETE FROM face_labels WHERE file_id = ?", (file_id,))
    conn.execute("UPDATE files SET face_detection_at = NULL WHERE id = ?", (file_id,))
    for p in faces_dir.glob(f"{file_id}_*.jpg"):
        p.unlink(missing_ok=True)


def _find_face_targets(
    path: pathlib.Path,
    db_path: pathlib.Path,
    limit: Optional[int],
    force: bool,
) -> list[dict]:
    path_prefix = str(path).rstrip("/") + "%"
    with get_connection(db_path) as conn:
        query = """
            SELECT f.id, f.path, f.extension, f.mime_type, f.face_detection_at
            FROM files f
            WHERE f.status = 'present'
              AND f.path LIKE ?
        """
        params: list = [path_prefix]
        if not force:
            query += " AND f.face_detection_at IS NULL"
        query += """
          AND NOT EXISTS (
              SELECT 1 FROM labels l
              WHERE l.file_id = f.id AND l.action = 'trash'
          )
        """
        query += " ORDER BY f.path"
        rows = conn.execute(query, params).fetchall()

    out: list[dict] = []
    for row in rows:
        r = dict(row)
        if pathlib.Path(r["path"]).name.startswith("._"):
            continue
        if not _is_photo(r["extension"] or "", r["mime_type"]):
            continue
        out.append(r)
        if limit is not None and len(out) >= limit:
            break
    return out


def _process_one_image(
    row: dict,
    db_path: pathlib.Path,
    faces_dir: pathlib.Path,
    force: bool,
    app,  # InsightFace FaceAnalysis instance (or mock with .get())
    gdrive_service=None,  # Google Drive service for gdrive files
) -> tuple[str, bool, Optional[str], int, float, float, float]:
    """Returns (file_id, ok, error_message, face_count)."""
    file_id = row["id"]
    p = pathlib.Path(row["path"])
    ext = row["extension"] or ""
    start_time = time.time()
    try:
        # Handle gdrive files: download to cache if not on local disk
        if not p.is_file():
            path_str = str(p)
            if path_str.startswith("/gdrive/"):
                if gdrive_service is None:
                    return file_id, False, "gdrive file but no service available", 0
                # Look up the Google Drive file ID and download to cache
                from magpie.gdrive_cache import lookup_gdrive_file_id, get_gdrive_file
                with get_connection(db_path) as conn:
                    gdrive_file_id = lookup_gdrive_file_id(conn, file_id)
                if not gdrive_file_id:
                    return file_id, False, "could not find gdrive file id in metadata", 0
                cached_path = get_gdrive_file(
                    gdrive_service,
                    gdrive_file_id,
                    ext,
                    cache_dir=faces_dir.parent / "gdrive_cache",
                )
                if cached_path is None:
                    return file_id, False, "failed to download from gdrive", 0
                p = cached_path
            else:
                return file_id, False, f"not found: {p}", 0

        rgb = load_rgb_numpy(p, ext)
        load_time = time.time()
        # InsightFace expects BGR
        bgr = rgb[:, :, ::-1]
        faces = app.get(bgr)
        inference_time = time.time()

        labeled_at = _now_iso()
        faces_dir.mkdir(parents=True, exist_ok=True)

        def _store_faces() -> None:
            with get_connection(db_path) as conn:
                if force:
                    _reset_file_faces(conn, file_id, faces_dir)
                for i, face in enumerate(faces):
                    bbox = _insightface_bbox_to_xywh(face.bbox)
                    blob = _embedding_to_blob(face.embedding)
                    fl_id = str(uuid.uuid4())
                    conn.execute(
                        """
                        INSERT INTO face_labels (
                            id, file_id, person_id, face_index, bbox, embedding,
                            source, confidence, labeled_at
                        ) VALUES (?, ?, NULL, ?, ?, ?, 'user', NULL, ?)
                        """,
                        (fl_id, file_id, i, json.dumps(bbox), blob, labeled_at),
                    )
                conn.execute(
                    "UPDATE files SET face_detection_at = ? WHERE id = ?",
                    (labeled_at, file_id),
                )
                conn.commit()

        _with_retry(_store_faces)

        for i, face in enumerate(faces):
            x1, y1, x2, y2 = (int(v) for v in face.bbox)
            thumb = _face_thumbnail(rgb, x1, y1, x2, y2)
            thumb_path = faces_dir / f"{file_id}_{i}.jpg"
            thumb.save(thumb_path, format="JPEG", quality=90)

        total_time = time.time() - start_time
        return file_id, True, None, len(faces), total_time, load_time - start_time, inference_time - load_time
    except Exception as exc:
        return file_id, False, str(exc), 0, time.time() - start_time, 0, 0


def run_analyze_faces(
    path: pathlib.Path,
    db_path: pathlib.Path,
    *,
    dry_run: bool = False,
    limit: Optional[int] = None,
    force: bool = False,
    faces_dir: Optional[pathlib.Path] = None,
    _app=None,  # injectable for tests
) -> None:
    """Entry point for `magpie analyze faces`."""
    path_str = str(path)
    if not path_str.startswith("/gdrive/") and not path.exists():
        console.print(f"[red]Path does not exist: {path}[/red]")
        raise SystemExit(1)

    cache = faces_dir if faces_dir is not None else FACES_CACHE_DIR
    targets = _find_face_targets(path, db_path, limit, force)

    # Initialize gdrive service if any targets are gdrive files
    gdrive_service = None
    if any(row["path"].startswith("/gdrive/") for row in targets):
        try:
            from magpie.gdrive_auth import get_credentials
            from googleapiclient.discovery import build
            creds = get_credentials()
            gdrive_service = build("drive", "v3", credentials=creds)
        except Exception as e:
            console.print(f"[red]Failed to initialize Google Drive service: {e}[/red]")
            raise SystemExit(1) from None

    if not targets:
        console.print("[green]No images need face detection.[/green]")
        return

    if dry_run:
        title = f"[bold]Dry run — {len(targets)} image(s) would be processed[/bold]"
        table = Table(title=title)
        table.add_column("Path", style="cyan", no_wrap=False)
        table.add_column("Ext", style="dim")
        for row in targets[:50]:
            rel = pathlib.Path(row["path"])
            try:
                rel_s = str(rel.relative_to(path))
            except ValueError:
                rel_s = str(rel)
            table.add_row(rel_s, row["extension"] or "?")
        if len(targets) > 50:
            table.add_row(f"... and {len(targets) - 50} more", "")
        console.print(table)
        console.print(
            "\n[dim]Uses InsightFace buffalo_l (ONNX / CoreML). "
            "Thumbnails: ~/.magpie/faces/{file_id}_{face_index}.jpg[/dim]"
        )
        return

    if _app is None:
        try:
            from insightface.app import FaceAnalysis  # type: ignore[import-untyped]
            import onnxruntime as ort  # type: ignore[import-untyped]
        except ImportError:
            console.print(
                "[red]insightface is not installed.[/red] "
                "Install dependencies with [bold]uv sync[/bold]."
            )
            raise SystemExit(1) from None

        # Check available ONNX Runtime providers
        available_providers = ort.get_available_providers()
        console.print(f"[dim]Available ONNX Runtime providers: {available_providers}[/dim]")

        fa = FaceAnalysis(
            name="buffalo_l",
            providers=["CoreMLExecutionProvider", "CPUExecutionProvider"],
        )
        fa.prepare(ctx_id=0, det_size=(640, 640))
    else:
        fa = _app

    console.print(f"Detecting faces in [bold]{len(targets)}[/bold] image(s) using [bold]InsightFace buffalo_l[/bold]…")
    ok = 0
    errors = 0
    total_faces = 0
    for row in track(targets, description="Processing…"):
        result = _process_one_image(row, db_path, cache, force, fa, gdrive_service=gdrive_service)
        _fid, success, err, face_count = result[:4]
        timing = result[4:] if len(result) > 4 else (0, 0, 0)
        if not success:
            console.print(f"[red]Error {row['path']}: {err}[/red]")
            errors += 1
        else:
            ok += 1
            total_faces += face_count
            # Show progress: current file and running total
            rel_path = pathlib.Path(row["path"])
            try:
                rel_path = rel_path.relative_to(path)
            except ValueError:
                pass
            total_t, load_t, infer_t = timing
            console.print(f"  [dim]{ok}/{len(targets)}[/dim] {rel_path} → {face_count} face(s) [dim]({total_faces} total) [{total_t:.2f}s: load {load_t:.2f}s + inference {infer_t:.2f}s][/dim]")

    console.print(f"\n[green]Done.[/green] {ok} processed, {errors} errors, {total_faces} faces detected.")


__all__ = [
    "FACES_CACHE_DIR",
    "load_rgb_numpy",
    "run_analyze_faces",
    "_find_face_targets",
    "_location_to_bbox",
    "_embedding_to_blob",
]
