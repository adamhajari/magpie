from __future__ import annotations

import asyncio
import json
import os.path as osp
import sqlite3
import struct
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from magpie.dupes import (
    apply_preferred_folder_duplicate_resolution,
    fetch_preferred_duplicate_prefixes,
    get_next_available_rank,
    normalize_path_prefix,
    queue_trash_nonkeeper_unless_already_trash,
    set_duplicate_group_keeper_and_queue_trash,
    set_preferred_path_rank,
)
from magpie.thumbs import generate_thumbnail, is_indexed_image_row

THUMBS_DIR = Path.home() / ".magpie" / "thumbs"
FACES_DIR = Path.home() / ".magpie" / "faces"
PLACEHOLDER_THUMB = Path(__file__).parent / "static" / "thumb_placeholder.svg"

DB_PATH = Path.home() / ".magpie" / "magpie.db"
TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="Digital Magpie")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Cache for MIME types (rarely changes, expensive to query)
_mime_types_cache: list[str] | None = None

# Cache for DB root folder path (rarely changes)
_db_root_cache: str | None = None


def cached_with_ttl(seconds: int = 43200):
    """Cache function results with TTL (in seconds). Default: 12 hours."""
    def decorator(func: Callable) -> Callable:
        cache: dict = {}

        def wrapper(*args, **kwargs) -> Any:
            key = (args, tuple(sorted(kwargs.items())))
            now = time.time()
            if key in cache:
                cached_value, cached_time = cache[key]
                if now - cached_time < seconds:
                    return cached_value

            result = func(*args, **kwargs)
            cache[key] = (result, now)
            return result

        def invalidate():
            cache.clear()

        wrapper.invalidate = invalidate
        wrapper.cache = cache
        return wrapper

    return decorator


def _log_timing(endpoint: str, elapsed_ms: float):
    """Log endpoint timing info."""
    print(f"[{endpoint}] {elapsed_ms:.1f}ms")


def _invalidate_mime_cache():
    """Invalidate the MIME types cache when files are indexed."""
    global _mime_types_cache
    _mime_types_cache = None


def _invalidate_db_root_cache():
    """Invalidate the DB root cache when drives are added/removed."""
    global _db_root_cache
    _db_root_cache = None


def _invalidate_all_caches():
    """Invalidate all UI caches after indexing operations."""
    _invalidate_mime_cache()
    _invalidate_db_root_cache()
    if hasattr(_fetch_stats, 'invalidate'):
        _fetch_stats.invalidate()
    if hasattr(_fetch_folder_children_cached, 'invalidate'):
        _fetch_folder_children_cached.invalidate()


# ---------------------------------------------------------------------------
# Template filters
# ---------------------------------------------------------------------------

def _format_bytes(value: int | None) -> str:
    if not value:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def _format_number(value: int | None) -> str:
    if value is None:
        return "0"
    return f"{value:,}"


templates.env.filters["format_bytes"] = _format_bytes
templates.env.filters["format_number"] = _format_number
templates.env.filters["basename"] = lambda p: Path(p).name if p else ""
templates.env.filters["dirname"] = lambda p: str(Path(p).parent) if p else ""
templates.env.filters["quote"] = lambda p: quote(p) if p else ""


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.create_function("basename", 1, lambda p: Path(p).name if p else "")
    return conn


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


def _convert_distance(value: bytes | float) -> float:
    """Convert distance value from bytes (if stored as binary) to float."""
    if isinstance(value, bytes):
        try:
            return struct.unpack("f", value)[0]
        except (struct.error, ValueError):
            return 0.0
    return float(value) if value is not None else 0.0


@cached_with_ttl(seconds=43200)  # 12 hours
def _fetch_stats(drive_id: str = "") -> dict:
    """Return dashboard stats using a single comprehensive query.
    All values default to 0 if the DB is empty.
    If drive_id is provided, filter stats to that drive only."""
    if not DB_PATH.exists():
        return _empty_stats()

    drive_where = ""
    if drive_id:
        drive_where = "WHERE drive_id = ?"
    trash_drive_filter = ""
    if drive_id:
        trash_drive_filter = "AND f.drive_id = ?"

    with _get_conn() as conn:
        # Fetch by_mime (multi-row result set)
        mime_start = time.time()
        mime_params = (drive_id,) if drive_id else ()
        by_mime = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT
                    COALESCE(mime_type, 'unknown') AS mime_type,
                    COUNT(*)                        AS count,
                    COALESCE(SUM(size_bytes), 0)    AS total_bytes
                FROM files
                {drive_where}
                GROUP BY mime_type
                ORDER BY count DESC
                LIMIT 20
                """,
                mime_params
            ).fetchall()
        ]
        mime_time = time.time() - mime_start

        # Fetch all scalar stats in a single comprehensive query using CTEs
        stats_start = time.time()
        stats_sql = f"""
        WITH file_stats AS (
            SELECT
                f.id,
                f.size_bytes,
                CASE WHEN l.file_id IS NOT NULL THEN 1 ELSE 0 END as has_label,
                l.action,
                l.approved_at,
                CASE WHEN e.file_id IS NOT NULL THEN 1 ELSE 0 END as has_execution
            FROM files f
            LEFT JOIN labels l ON f.id = l.file_id
            LEFT JOIN executions e ON f.id = e.file_id
            {drive_where}
        ),
        dup_stats AS (
            SELECT
                COUNT(DISTINCT dg.group_id) as dup_group_count,
                COUNT(DISTINCT dg.file_id) as dup_file_count,
                SUM(CASE WHEN dg.match_type = 'exact' THEN 1 ELSE 0 END) as exact_count,
                SUM(CASE WHEN dg.match_type = 'near' THEN 1 ELSE 0 END) as near_count
            FROM duplicate_groups dg
            INNER JOIN files f ON dg.file_id = f.id
            {drive_where}
        ),
        dup_file_sizes AS (
            SELECT COALESCE(SUM(size_bytes), 0) AS dup_file_total_size
            FROM (
                SELECT DISTINCT f.id, f.size_bytes
                FROM duplicate_groups dg
                INNER JOIN files f ON dg.file_id = f.id
                {drive_where}
            )
        ),
        trash_dup_stats AS (
            SELECT
                COUNT(*) as trash_dup_file_count,
                COALESCE(SUM(size_bytes), 0) as trash_dup_total_size
            FROM (
                SELECT DISTINCT f.id, f.size_bytes
                FROM files f
                INNER JOIN labels l ON f.id = l.file_id AND l.action = 'trash'
                WHERE f.status = 'present'
                  {trash_drive_filter}
                  AND (
                      EXISTS (
                          SELECT 1 FROM duplicate_groups dg WHERE dg.file_id = f.id
                      )
                      OR (
                          f.content_hash IS NOT NULL
                          AND EXISTS (
                              SELECT 1 FROM files o
                              WHERE o.id != f.id
                                AND o.content_hash IS NOT NULL
                                AND o.content_hash = f.content_hash
                                AND o.status = 'present'
                          )
                      )
                  )
            )
        )
        SELECT
            COUNT(DISTINCT id) as total_files,
            COALESCE(SUM(size_bytes), 0) as total_size,
            SUM(has_label) as labeled_count,
            COALESCE(SUM(CASE WHEN has_label = 0 THEN size_bytes ELSE 0 END), 0) as unlabeled_size,
            SUM(CASE WHEN has_label = 1 AND approved_at IS NULL THEN 1 ELSE 0 END) as pending_approval_count,
            SUM(CASE WHEN has_label = 1 AND approved_at IS NOT NULL THEN 1 ELSE 0 END) as approved_count,
            SUM(CASE WHEN action = 'trash' THEN 1 ELSE 0 END) as trash_count,
            COALESCE(SUM(CASE WHEN action = 'trash' THEN size_bytes ELSE 0 END), 0) as trash_size,
            SUM(CASE WHEN action = 'trash' AND approved_at IS NOT NULL THEN 1 ELSE 0 END) as trash_labeled_count,
            COALESCE(SUM(CASE WHEN action = 'trash' AND approved_at IS NOT NULL THEN size_bytes ELSE 0 END), 0) as trash_labeled_size,
            SUM(CASE WHEN action = 'trash' AND approved_at IS NULL THEN 1 ELSE 0 END) as trash_pending_count,
            COALESCE(SUM(CASE WHEN action = 'trash' AND approved_at IS NULL THEN size_bytes ELSE 0 END), 0) as trash_pending_size,
            SUM(CASE WHEN action = 'trash' AND has_execution = 1 THEN 1 ELSE 0 END) as trash_moved_count,
            COALESCE(SUM(CASE WHEN action = 'trash' AND has_execution = 1 THEN size_bytes ELSE 0 END), 0) as trash_moved_size,
            SUM(CASE WHEN action = 'keep_cold' THEN 1 ELSE 0 END) as cold_count,
            COALESCE(SUM(CASE WHEN action = 'keep_cold' THEN size_bytes ELSE 0 END), 0) as cold_size,
            SUM(CASE WHEN action = 'keep_cold' AND approved_at IS NOT NULL THEN 1 ELSE 0 END) as cold_labeled_count,
            COALESCE(SUM(CASE WHEN action = 'keep_cold' AND approved_at IS NOT NULL THEN size_bytes ELSE 0 END), 0) as cold_labeled_size,
            SUM(CASE WHEN action = 'keep_cold' AND approved_at IS NULL THEN 1 ELSE 0 END) as cold_pending_count,
            COALESCE(SUM(CASE WHEN action = 'keep_cold' AND approved_at IS NULL THEN size_bytes ELSE 0 END), 0) as cold_pending_size,
            SUM(CASE WHEN action = 'keep_cold' AND has_execution = 1 THEN 1 ELSE 0 END) as cold_moved_count,
            COALESCE(SUM(CASE WHEN action = 'keep_cold' AND has_execution = 1 THEN size_bytes ELSE 0 END), 0) as cold_moved_size,
            SUM(CASE WHEN action = 'keep_hot' THEN 1 ELSE 0 END) as hot_count,
            COALESCE(SUM(CASE WHEN action = 'keep_hot' THEN size_bytes ELSE 0 END), 0) as hot_size,
            SUM(CASE WHEN action = 'keep_hot' AND approved_at IS NOT NULL THEN 1 ELSE 0 END) as hot_labeled_count,
            COALESCE(SUM(CASE WHEN action = 'keep_hot' AND approved_at IS NOT NULL THEN size_bytes ELSE 0 END), 0) as hot_labeled_size,
            SUM(CASE WHEN action = 'keep_hot' AND approved_at IS NULL THEN 1 ELSE 0 END) as hot_pending_count,
            COALESCE(SUM(CASE WHEN action = 'keep_hot' AND approved_at IS NULL THEN size_bytes ELSE 0 END), 0) as hot_pending_size,
            SUM(CASE WHEN action = 'keep_hot' AND has_execution = 1 THEN 1 ELSE 0 END) as hot_moved_count,
            COALESCE(SUM(CASE WHEN action = 'keep_hot' AND has_execution = 1 THEN size_bytes ELSE 0 END), 0) as hot_moved_size,
            COALESCE(dup_stats.dup_group_count, 0) as duplicate_group_count,
            COALESCE(dup_stats.dup_file_count, 0) as duplicate_file_count,
            COALESCE(dup_file_sizes.dup_file_total_size, 0) as duplicate_file_total_size,
            COALESCE(dup_stats.exact_count, 0) as exact_duplicate_count,
            COALESCE(dup_stats.near_count, 0) as near_duplicate_count,
            COALESCE(trash_dup_stats.trash_dup_file_count, 0) as trash_duplicate_file_count,
            COALESCE(trash_dup_stats.trash_dup_total_size, 0) as trash_duplicate_total_size
        FROM file_stats
        CROSS JOIN dup_stats
        CROSS JOIN dup_file_sizes
        CROSS JOIN trash_dup_stats
        """

        stats_params = (drive_id, drive_id, drive_id, drive_id) if drive_id else ()
        row = conn.execute(stats_sql, stats_params).fetchone()
        stats_time = time.time() - stats_start

        if not row:
            return _empty_stats()

        stats_dict = dict(row)
        total_files = stats_dict.get("total_files", 0)
        labeled_count = stats_dict.get("labeled_count", 0)
        unlabeled_count = max(0, total_files - labeled_count)

        # Log timing info (will appear in server logs)
        print(f"[Dashboard] mime_type query: {mime_time*1000:.1f}ms, stats query: {stats_time*1000:.1f}ms, total: {(mime_time+stats_time)*1000:.1f}ms")

        return {
            "total_files": stats_dict.get("total_files", 0),
            "total_size": stats_dict.get("total_size", 0),
            "by_mime": by_mime,
            "duplicate_group_count": stats_dict.get("duplicate_group_count", 0),
            "duplicate_file_count": stats_dict.get("duplicate_file_count", 0),
            "duplicate_file_total_size": stats_dict.get("duplicate_file_total_size", 0),
            "exact_duplicate_count": stats_dict.get("exact_duplicate_count", 0),
            "near_duplicate_count": stats_dict.get("near_duplicate_count", 0),
            "trash_duplicate_file_count": stats_dict.get("trash_duplicate_file_count", 0),
            "trash_duplicate_total_size": stats_dict.get("trash_duplicate_total_size", 0),
            "labeled_count": labeled_count,
            "unlabeled_count": unlabeled_count,
            "unlabeled_size": stats_dict.get("unlabeled_size", 0),
            "pending_approval_count": stats_dict.get("pending_approval_count", 0),
            "approved_count": stats_dict.get("approved_count", 0),
            "trash_count": stats_dict.get("trash_count", 0),
            "trash_size": stats_dict.get("trash_size", 0),
            "trash_labeled_count": stats_dict.get("trash_labeled_count", 0),
            "trash_labeled_size": stats_dict.get("trash_labeled_size", 0),
            "trash_pending_count": stats_dict.get("trash_pending_count", 0),
            "trash_pending_size": stats_dict.get("trash_pending_size", 0),
            "trash_moved_count": stats_dict.get("trash_moved_count", 0),
            "trash_moved_size": stats_dict.get("trash_moved_size", 0),
            "cold_count": stats_dict.get("cold_count", 0),
            "cold_size": stats_dict.get("cold_size", 0),
            "cold_labeled_count": stats_dict.get("cold_labeled_count", 0),
            "cold_labeled_size": stats_dict.get("cold_labeled_size", 0),
            "cold_pending_count": stats_dict.get("cold_pending_count", 0),
            "cold_pending_size": stats_dict.get("cold_pending_size", 0),
            "cold_moved_count": stats_dict.get("cold_moved_count", 0),
            "cold_moved_size": stats_dict.get("cold_moved_size", 0),
            "hot_count": stats_dict.get("hot_count", 0),
            "hot_size": stats_dict.get("hot_size", 0),
            "hot_labeled_count": stats_dict.get("hot_labeled_count", 0),
            "hot_labeled_size": stats_dict.get("hot_labeled_size", 0),
            "hot_pending_count": stats_dict.get("hot_pending_count", 0),
            "hot_pending_size": stats_dict.get("hot_pending_size", 0),
            "hot_moved_count": stats_dict.get("hot_moved_count", 0),
            "hot_moved_size": stats_dict.get("hot_moved_size", 0),
        }


def _empty_stats() -> dict:
    return {
        "total_files": 0,
        "total_size": 0,
        "by_mime": [],
        "duplicate_group_count": 0,
        "duplicate_file_count": 0,
        "duplicate_file_total_size": 0,
        "exact_duplicate_count": 0,
        "near_duplicate_count": 0,
        "trash_duplicate_file_count": 0,
        "trash_duplicate_total_size": 0,
        "labeled_count": 0,
        "unlabeled_count": 0,
        "unlabeled_size": 0,
        "pending_approval_count": 0,
        "approved_count": 0,
        "trash_count": 0,
        "trash_size": 0,
        "trash_labeled_count": 0,
        "trash_labeled_size": 0,
        "trash_pending_count": 0,
        "trash_pending_size": 0,
        "trash_moved_count": 0,
        "trash_moved_size": 0,
        "cold_count": 0,
        "cold_size": 0,
        "cold_labeled_count": 0,
        "cold_labeled_size": 0,
        "cold_pending_count": 0,
        "cold_pending_size": 0,
        "cold_moved_count": 0,
        "cold_moved_size": 0,
        "hot_count": 0,
        "hot_size": 0,
        "hot_labeled_count": 0,
        "hot_labeled_size": 0,
        "hot_pending_count": 0,
        "hot_pending_size": 0,
        "hot_moved_count": 0,
        "hot_moved_size": 0,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, drive_id: str = "", refresh: bool = False) -> HTMLResponse:
    start = time.time()
    if refresh:
        _fetch_stats.invalidate()
    stats = _fetch_stats(drive_id)
    drives = _fetch_drives()
    # Check if this is an HTMX request (e.g., from drive filter change)
    is_htmx = request.headers.get("HX-Request") == "true"
    template_name = "dashboard-content.html" if is_htmx else "dashboard.html"
    elapsed = (time.time() - start) * 1000
    _log_timing("GET /", elapsed)
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={"stats": stats, "drives": drives, "selected_drive_id": drive_id}
    )


# ---------------------------------------------------------------------------
# File browser helpers
# ---------------------------------------------------------------------------

PAGE_SIZE = 100

_SORT_COLUMNS = {
    "filename": "f.path",
    "size": "f.size_bytes",
    "date": "f.modified_at",
    "drive": "d.label",
}


def _fetch_files(
    *,
    mime: str = "",
    drive_id: str = "",
    label_status: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "date",
    order: str = "desc",
    page: int = 1,
    per_page: int = 100,
    path: str = "",
    recursive: str = "true",
    show_trash: str = "",
    filename_pattern: str = "",
) -> dict:
    if not DB_PATH.exists():
        return {"files": [], "total": 0, "page": page, "page_size": per_page}

    sort_col = _SORT_COLUMNS.get(sort, "f.modified_at")
    safe_order = "ASC" if order.lower() == "asc" else "DESC"

    label_join, where, params = _build_filter_where(
        mime=mime,
        drive_id=drive_id,
        label_status=label_status,
        date_from=date_from,
        date_to=date_to,
        path=path,
        recursive=recursive,
        show_trash=show_trash,
        filename_pattern=filename_pattern,
    )

    offset = (page - 1) * per_page

    sql = f"""
        SELECT
            f.id,
            f.path,
            f.size_bytes,
            f.modified_at,
            f.mime_type,
            f.status,
            d.label   AS drive_label,
            d.id      AS drive_id,
            COALESCE(lbl.action, '') AS label_action,
            COALESCE(lbl.source, '') AS label_source,
            lbl.approved_at   AS label_approved_at,
            (SELECT COUNT(DISTINCT dg.group_id)
             FROM duplicate_groups dg
             LEFT JOIN labels dl ON dl.file_id = dg.file_id
             WHERE dg.file_id = f.id
               AND (dl.action IS NULL OR dl.action != 'trash')) AS dupe_group_count,
            (SELECT dg2.group_id
             FROM duplicate_groups dg2
             LEFT JOIN labels dl2 ON dl2.file_id = dg2.file_id
             WHERE dg2.file_id = f.id
               AND (dl2.action IS NULL OR dl2.action != 'trash')
             LIMIT 1) AS dupe_group_id
        FROM files f
        LEFT JOIN drives d ON d.id = f.drive_id
        {label_join}
        {where}
        ORDER BY {sort_col} {safe_order}
        LIMIT ? OFFSET ?
    """

    count_sql = f"""
        SELECT COUNT(*)
        FROM files f
        {label_join}
        {where}
    """

    with _get_conn() as conn:
        total = conn.execute(count_sql, params).fetchone()[0] or 0
        rows = [dict(r) for r in conn.execute(sql, params + [per_page, offset]).fetchall()]

    return {"files": rows, "total": total, "page": page, "page_size": per_page}


def _files_rows_context(
    *,
    mime: str = "",
    drive_id: str = "",
    label_status: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "date",
    order: str = "desc",
    page: int = 1,
    per_page: int = 100,
    path: str = "",
    recursive: str = "true",
    show_trash: str = "",
    filename_pattern: str = "",
) -> dict:
    """Context dict for `_files_rows.html` (table + pagination)."""
    result = _fetch_files(
        mime=mime,
        drive_id=drive_id,
        label_status=label_status,
        date_from=date_from,
        date_to=date_to,
        sort=sort,
        order=order,
        page=page,
        per_page=per_page,
        path=path,
        recursive=recursive,
        show_trash=show_trash,
        filename_pattern=filename_pattern,
    )

    subfolders: list[dict] = []
    if path and recursive == "false" and DB_PATH.exists():
        with _get_conn() as conn:
            subfolders = _fetch_folder_children(conn, path)

    return {
        **result,
        "total_pages": max(1, (result["total"] + per_page - 1) // per_page),
        "subfolders": subfolders,
        "filter_qs": _build_filter_qs(
            {
                "mime": mime,
                "drive_id": drive_id,
                "label_status": label_status,
                "date_from": date_from,
                "date_to": date_to,
                "sort": sort,
                "order": order,
                "path": path,
                "recursive": recursive,
                "filename_pattern": filename_pattern,
            }
        ),
    }


def _fetch_drives() -> list[dict]:
    if not DB_PATH.exists():
        return []
    def _query():
        with _get_conn() as conn:
            return [dict(r) for r in conn.execute("SELECT id, label FROM drives ORDER BY label").fetchall()]
    return _with_retry(_query)


def _fetch_mime_types() -> list[str]:
    """Fetch MIME types from database, with in-memory caching."""
    global _mime_types_cache
    if _mime_types_cache is not None:
        return _mime_types_cache
    if not DB_PATH.exists():
        return []
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT mime_type FROM files WHERE mime_type IS NOT NULL ORDER BY mime_type"
        ).fetchall()
    _mime_types_cache = [r[0] for r in rows]
    return _mime_types_cache


def _fetch_mime_types_for_duplicates() -> list[dict]:
    """Fetch MIME types from unresolved duplicate groups, sorted by count (descending)."""
    if not DB_PATH.exists():
        return []
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT f.mime_type, COUNT(DISTINCT dg.group_id) as dup_count
            FROM files f
            INNER JOIN duplicate_groups dg ON dg.file_id = f.id
            WHERE f.mime_type IS NOT NULL AND dg.all_labels_approved_at IS NULL
            GROUP BY f.mime_type
            ORDER BY dup_count DESC, f.mime_type ASC
        """).fetchall()
    return [{"mime_type": r["mime_type"], "dup_count": r["dup_count"]} for r in rows]


# ---------------------------------------------------------------------------
# Detail-view helpers
# ---------------------------------------------------------------------------

def _build_filter_where(
    mime: str = "",
    drive_id: str = "",
    label_status: str = "",
    date_from: str = "",
    date_to: str = "",
    path: str = "",
    recursive: str = "true",
    show_trash: str = "",
    filename_pattern: str = "",
    **_kwargs,
) -> tuple[str, str, list]:
    """Return (label_join, where_clause, params) for the filtered file set."""
    conditions: list[str] = []
    params: list = []

    if mime:
        conditions.append("f.mime_type = ?")
        params.append(mime)
    if drive_id:
        conditions.append("f.drive_id = ?")
        params.append(drive_id)
    if date_from:
        conditions.append("f.modified_at >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("f.modified_at <= ?")
        params.append(date_to + " 23:59:59")
    if filename_pattern:
        like_pat = filename_pattern.replace("*", "%").replace("?", "_")
        conditions.append("LOWER(basename(f.path)) LIKE LOWER(?)")
        params.append(like_pat)
    if path:
        p = path.rstrip("/")
        conditions.append("f.path LIKE ?")
        params.append(p + "/%")
        if recursive == "false":
            conditions.append("f.path NOT LIKE ?")
            params.append(p + "/%/%")

    if label_status in ("keep_hot", "keep_cold", "trash"):
        conditions.append("lbl.action = ?")
        params.append(label_status)
    elif label_status == "unlabeled":
        conditions.append("lbl.file_id IS NULL")
    else:
        if show_trash != "true":
            conditions.append("(lbl.action IS NULL OR lbl.action != ?)")
            params.append("trash")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    label_join = "LEFT JOIN labels lbl ON lbl.file_id = f.id"
    return label_join, where, params


def _fetch_adjacent(
    conn: sqlite3.Connection,
    file_id: str,
    sort: str = "date",
    order: str = "desc",
    **filter_kwargs,
) -> tuple[str | None, str | None]:
    """Return (prev_id, next_id) for file_id within the filtered+sorted set."""
    sort_col = _SORT_COLUMNS.get(sort, "f.modified_at")
    safe_order = "ASC" if order.lower() == "asc" else "DESC"
    reverse_order = "DESC" if safe_order == "ASC" else "ASC"

    label_join, where, params = _build_filter_where(**filter_kwargs)

    cte_sql = f"""
        WITH ranked AS (
            SELECT f.id,
                   ROW_NUMBER() OVER (ORDER BY {sort_col} {safe_order}, f.id) AS rn
            FROM files f
            LEFT JOIN drives d ON d.id = f.drive_id
            {label_join}
            {where}
            GROUP BY f.id
        ),
        cur AS (SELECT rn FROM ranked WHERE id = ?)
        SELECT id, rn FROM ranked
        WHERE rn BETWEEN (SELECT rn FROM cur) - 1 AND (SELECT rn FROM cur) + 1
        ORDER BY rn
    """
    rows = conn.execute(cte_sql, params + [file_id]).fetchall()

    cur_rn = None
    for r in rows:
        if r["id"] == file_id:
            cur_rn = r["rn"]
            break

    prev_id = next_id = None
    for r in rows:
        if r["rn"] == cur_rn - 1:
            prev_id = r["id"]
        elif r["rn"] == cur_rn + 1:
            next_id = r["id"]

    return prev_id, next_id


def _build_filter_qs(filters: dict) -> str:
    """Build a URL query string from filter params (excluding page)."""
    from urllib.parse import quote
    parts = []
    for key, val in filters.items():
        if key == "page":
            continue
        if val:
            parts.append(f"{key}={quote(str(val), safe='/')}")
    return ("?" + "&".join(parts)) if parts else ""


def _read_exif(path: Path) -> dict:
    """Read EXIF tags from an image file. Returns a dict of human-readable values."""
    result: dict = {}
    try:
        import exifread  # type: ignore[import-untyped]
        with open(path, "rb") as fh:
            tags = exifread.process_file(fh, details=False)

        wanted = {
            "EXIF DateTimeOriginal": "date_taken",
            "Image DateTime": "date_taken",
            "Image Make": "camera_make",
            "Image Model": "camera_model",
            "EXIF FocalLength": "focal_length",
            "EXIF FNumber": "f_number",
            "EXIF ISOSpeedRatings": "iso",
            "EXIF ExposureTime": "exposure",
            "GPS GPSLatitude": "gps_lat_raw",
            "GPS GPSLatitudeRef": "gps_lat_ref",
            "GPS GPSLongitude": "gps_lon_raw",
            "GPS GPSLongitudeRef": "gps_lon_ref",
            "GPS GPSAltitude": "gps_alt",
        }
        for tag_key, field in wanted.items():
            if tag_key in tags:
                result[field] = str(tags[tag_key])

        # Build GPS decimal degrees if we have all parts
        if "gps_lat_raw" in result and "gps_lon_raw" in result:
            lat = _exif_gps_to_decimal(tags.get("GPS GPSLatitude"), tags.get("GPS GPSLatitudeRef"))
            lon = _exif_gps_to_decimal(tags.get("GPS GPSLongitude"), tags.get("GPS GPSLongitudeRef"))
            if lat is not None and lon is not None:
                result["gps_decimal"] = f"{lat:.6f}, {lon:.6f}"
    except Exception:
        pass

    # Fallback: use Pillow for dimensions
    try:
        from PIL import Image as PILImage  # type: ignore[import-untyped]
        with PILImage.open(path) as img:
            result["width"], result["height"] = img.size
    except Exception:
        pass

    return result


def _exif_gps_to_decimal(coord_tag, ref_tag) -> float | None:
    """Convert exifread GPS IFDRational list to decimal degrees."""
    try:
        vals = coord_tag.values  # list of IFDRational
        degrees = float(vals[0].num) / float(vals[0].den)
        minutes = float(vals[1].num) / float(vals[1].den)
        seconds = float(vals[2].num) / float(vals[2].den)
        decimal = degrees + minutes / 60 + seconds / 3600
        if ref_tag and str(ref_tag) in ("S", "W"):
            decimal = -decimal
        return decimal
    except Exception:
        return None


def _exif_summary(exif: dict) -> str:
    """One-line summary for duplicate grid cards."""
    parts: list[str] = []
    dt = exif.get("date_taken")
    if dt:
        parts.append(str(dt)[:10])
    cam = " ".join(
        x for x in (exif.get("camera_make"), exif.get("camera_model")) if x
    ).strip()
    if cam:
        parts.append(cam[:48])
    w, h = exif.get("width"), exif.get("height")
    if w and h:
        parts.append(f"{w}×{h}")
    if exif.get("exposure"):
        parts.append(f"{exif['exposure']}s")
    if exif.get("f_number"):
        parts.append(f"ƒ/{exif['f_number']}")
    if exif.get("iso"):
        parts.append(f"ISO {exif['iso']}")
    return " · ".join(parts)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Folder tree helpers
# ---------------------------------------------------------------------------

def _common_path_prefix(paths: list[str]) -> str:
    """Return the longest common path prefix (directory boundary)."""
    if not paths:
        return ""
    try:
        return osp.commonpath(paths)
    except ValueError:
        return ""


def _build_breadcrumbs(path: str) -> list[dict]:
    """Return [{name, path}, …] for each segment of an absolute path."""
    if not path:
        return []
    parts = path.strip("/").split("/")
    crumbs: list[dict] = []
    current = ""
    for part in parts:
        current += "/" + part
        crumbs.append({"name": part, "path": current})
    return crumbs


def _fetch_folder_children(conn: sqlite3.Connection, prefix: str, include_trash: bool = False) -> list[dict]:
    """Return immediate subfolder nodes under *prefix* with file_count and total_bytes.

    Uses an f-string for the integer offset (safe — not user data).
    User-supplied *prefix* is always passed as a SQL parameter.
    If include_trash is False, excludes files with a 'trash' label (action='trash').
    """
    p = prefix.rstrip("/")
    p_len = len(p)
    # substr(path, p_len+2) strips the prefix and its trailing slash.
    offset = p_len + 2
    # Range bounds use '~' (ASCII 126) as upper sentinel — sorts above all printable chars.
    p_lo = p + "/"
    p_hi = p + "/~"
    trash_filter = ""
    if not include_trash:
        trash_filter = "AND NOT EXISTS (SELECT 1 FROM labels WHERE file_id = f.id AND action = 'trash')"
    sql = f"""
        SELECT
            ? || '/' ||
            substr(substr(f.path, {offset}), 1,
                CASE WHEN instr(substr(f.path, {offset}), '/') > 0
                     THEN instr(substr(f.path, {offset}), '/') - 1
                     ELSE length(substr(f.path, {offset}))
                END
            ) AS child_path,
            COUNT(*) AS file_count,
            COALESCE(SUM(f.size_bytes), 0) AS total_bytes
        FROM files f
        WHERE f.path >= ? AND f.path < ?
          AND instr(substr(f.path, {offset}), '/') > 0
          {trash_filter}
        GROUP BY child_path
        ORDER BY child_path
    """
    rows = conn.execute(sql, (p, p_lo, p_hi)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["name"] = d["child_path"].rpartition("/")[2]
        result.append(d)
    return result


@cached_with_ttl(seconds=43200)  # 12 hours
def _fetch_folder_children_cached(prefix: str, include_trash: bool = False) -> list[dict]:
    """Cached version of _fetch_folder_children that creates its own connection."""
    if not DB_PATH.exists():
        return []
    with _get_conn() as conn:
        return _fetch_folder_children(conn, prefix, include_trash)


@cached_with_ttl(seconds=43200)  # 12 hours
def _fetch_folder_browser_data(path: str) -> dict:
    """Fetch folder browser data (nodes and preferred paths) with caching."""
    if not DB_PATH.exists():
        return {
            "effective_path": path,
            "nodes": [],
            "preferred_paths_with_ranks": [],
            "active_is_preferred": False,
        }

    with _get_conn() as conn:
        effective_path = path if path else _find_db_root(conn)
        nodes = _fetch_folder_children(conn, effective_path)

        # Fetch preferred paths with their ranks
        rows = conn.execute(
            "SELECT path_prefix, rank FROM preferred_duplicate_paths ORDER BY rank ASC"
        ).fetchall()
        preferred_paths_with_ranks = [
            {"path": str(r["path_prefix"]), "rank": r["rank"]}
            for r in rows
        ]

        active_norm = normalize_path_prefix(effective_path)
        active_is_preferred = any(
            normalize_path_prefix(p["path"]) == active_norm
            for p in preferred_paths_with_ranks
        )

        return {
            "effective_path": effective_path,
            "nodes": nodes,
            "preferred_paths_with_ranks": preferred_paths_with_ranks,
            "active_is_preferred": active_is_preferred,
        }


def _find_db_root(conn: sqlite3.Connection) -> str:
    """Return the parent of the deepest common path prefix across all indexed files.

    This gives a sensible default starting point for the folder browser regardless
    of how deeply the files are nested. Falls back to "" (no prefix) if files are
    from multiple sources (local drives + Google Drive) or if the DB is empty.

    Results are cached since this rarely changes.
    """
    global _db_root_cache
    if _db_root_cache is not None:
        return _db_root_cache

    # Check if we have files from multiple sources
    sources = conn.execute(
        "SELECT DISTINCT source FROM drives WHERE EXISTS (SELECT 1 FROM files WHERE drive_id = drives.id)"
    ).fetchall()
    source_list = [r[0] if isinstance(r, tuple) else r["source"] for r in sources]
    if len(source_list) > 1:
        # Multiple sources (local + gdrive) — show virtual root
        _db_root_cache = ""
        return ""

    rows = conn.execute(
        "SELECT path FROM files WHERE path IS NOT NULL AND path != '' LIMIT 200"
    ).fetchall()
    paths = [r["path"] for r in rows if r["path"]]
    if not paths:
        _db_root_cache = ""
        return ""
    try:
        common = osp.commonpath(paths)
        parent = str(Path(common).parent)
        result = parent if parent != common else common
        _db_root_cache = result
        return result
    except ValueError:
        _db_root_cache = ""
        return ""


def _fetch_faces_for_file(conn: sqlite3.Connection, file_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT fl.face_index, fl.source, fl.confidence, p.name AS person_name
        FROM face_labels fl
        LEFT JOIN people p ON p.id = fl.person_id
        WHERE fl.file_id = ?
        ORDER BY fl.face_index
        """,
        (file_id,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        idx = int(d["face_index"])
        d["thumb_exists"] = (FACES_DIR / f"{file_id}_{idx}.jpg").exists()
        out.append(d)
    return out


def _fetch_one_face(conn: sqlite3.Connection, file_id: str, face_index: int) -> dict | None:
    row = conn.execute(
        """
        SELECT fl.face_index, fl.source, fl.confidence, p.name AS person_name
        FROM face_labels fl
        LEFT JOIN people p ON p.id = fl.person_id
        WHERE fl.file_id = ? AND fl.face_index = ?
        """,
        (file_id, face_index),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["thumb_exists"] = (FACES_DIR / f"{file_id}_{face_index}.jpg").exists()
    return d


def _find_or_create_person_by_name(conn: sqlite3.Connection, name: str, now_iso: str) -> str | None:
    name = name.strip()
    if not name:
        return None
    existing = conn.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone()
    if existing:
        return existing["id"]
    pid = str(uuid.uuid4())
    def _do_insert():
        conn.execute(
            "INSERT INTO people (id, name, created_at) VALUES (?, ?, ?)",
            (pid, name, now_iso),
        )
        conn.commit()
    _with_retry(_do_insert)
    return pid


def _fetch_duplicate_groups_index(conn: sqlite3.Connection, show_all: bool = False, mime: str | None = None, folder_prefix: str | None = None) -> tuple[list[dict], list[dict]]:
    """Return (exact_group_dicts, near_group_dicts) for the duplicates list page.

    If show_all is False, only return groups where all_labels_approved_at is NULL (unresolved).
    If mime is provided, filter to groups where at least one file has that MIME type.
    If folder_prefix is provided, only return groups with at least one file under that folder tree.
    Optimized to compute all group stats in SQL (no nested loops in Python).
    """
    conditions = []
    params: list = []
    if not show_all:
        conditions.append("dg.all_labels_approved_at IS NULL")
    if mime:
        conditions.append("f.mime_type = ?")
        params.append(mime)
    if folder_prefix:
        p = folder_prefix.rstrip("/")
        folder_lo = p + "/"
        folder_hi = p + "/~"  # '~' (ASCII 126) sorts above all printable chars
        conditions.append(
            "dg.group_id IN ("
            "  SELECT DISTINCT dg2.group_id FROM duplicate_groups dg2"
            "  JOIN files f2 ON f2.id = dg2.file_id"
            "  WHERE f2.path >= ? AND f2.path < ?"
            ")"
        )
        params.extend([folder_lo, folder_hi])

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = conn.execute(f"""
        SELECT
            dg.group_id,
            MAX(dg.match_type) AS match_type,
            MIN(dg.confidence) AS min_confidence,
            COUNT(DISTINCT dg.file_id) AS member_count,
            SUM(f.size_bytes) AS total_bytes,
            MIN(f.indexed_at) AS created_at,
            COALESCE(
                (SELECT f2.size_bytes FROM files f2
                 INNER JOIN duplicate_groups dg2 ON dg2.file_id = f2.id
                 WHERE dg2.group_id = dg.group_id AND dg2.recommended_keep = 1
                 LIMIT 1),
                (SELECT MAX(f3.size_bytes) FROM files f3
                 INNER JOIN duplicate_groups dg3 ON dg3.file_id = f3.id
                 WHERE dg3.group_id = dg.group_id)
            ) AS keeper_size
        FROM duplicate_groups dg
        INNER JOIN files f ON f.id = dg.file_id
        {where_clause}
        GROUP BY dg.group_id
        ORDER BY CASE MAX(dg.match_type) WHEN 'exact' THEN 0 ELSE 1 END,
                 MIN(dg.confidence) DESC,
                 dg.group_id
    """, params).fetchall()

    # Build group dicts
    groups_by_id: dict[str, dict] = {}
    exact_out: list[dict] = []
    near_out: list[dict] = []

    for r in rows:
        total = r["total_bytes"] or 0
        keeper = r["keeper_size"] or 0
        match_type = r["match_type"]
        min_conf = r["min_confidence"]
        # Near-dupes with confidence=1.0 (Hamming distance 0) are perceptually identical
        is_identical = match_type == "near" and min_conf is not None and min_conf >= 1.0
        g = {
            "group_id": r["group_id"],
            "match_type": match_type,
            "min_confidence": min_conf,
            "is_identical": is_identical,
            "member_count": r["member_count"],
            "member_ids": [],
            "total_bytes": total,
            "reclaimable_bytes": max(0, total - keeper),
            "first_filename": "",
            "created_at": r["created_at"],
        }
        groups_by_id[r["group_id"]] = g
        if match_type == "exact" or is_identical:
            exact_out.append(g)
        else:
            near_out.append(g)

    # Fetch member_ids and first filename for all groups in chunks (SQLite has a ~999 parameter limit)
    if groups_by_id:
        group_ids = list(groups_by_id.keys())
        chunk_size = 500
        for i in range(0, len(group_ids), chunk_size):
            chunk = group_ids[i : i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            member_rows = conn.execute(f"""
                SELECT group_id, file_id, ROW_NUMBER() OVER (PARTITION BY group_id ORDER BY file_id) as row_num, f.path
                FROM duplicate_groups dg
                JOIN files f ON f.id = dg.file_id
                WHERE group_id IN ({placeholders})
                ORDER BY group_id, file_id
            """, chunk).fetchall()

            for mr in member_rows:
                groups_by_id[mr["group_id"]]["member_ids"].append(mr["file_id"])
                # Set first_filename from the first member (row_num == 1)
                if mr["row_num"] == 1:
                    groups_by_id[mr["group_id"]]["first_filename"] = mr["path"]

    return exact_out, near_out


def _get_next_unresolved_duplicate_group(conn: sqlite3.Connection, current_group_id: str) -> str | None:
    """Find the next unresolved duplicate group ID after the current one.

    Unresolved groups are those where all_labels_approved_at IS NULL.
    Returns the next group_id in the sort order (exact first, then near), or None if this is the last one.
    """
    # Get all unresolved group IDs sorted by match_type (exact first) then group_id
    unresolved_rows = conn.execute(
        """
        SELECT DISTINCT dg.group_id
        FROM duplicate_groups dg
        WHERE dg.all_labels_approved_at IS NULL
        ORDER BY CASE (SELECT MAX(match_type) FROM duplicate_groups dg2 WHERE dg2.group_id = dg.group_id)
                 WHEN 'exact' THEN 0 ELSE 1 END,
                 dg.group_id
        """
    ).fetchall()

    unresolved_ids = [r["group_id"] for r in unresolved_rows]

    try:
        current_index = unresolved_ids.index(current_group_id)
        if current_index < len(unresolved_ids) - 1:
            return unresolved_ids[current_index + 1]
    except ValueError:
        pass

    return None


def _update_duplicate_group_resolution_status(conn: sqlite3.Connection, now_iso: str) -> None:
    """Check all unresolved duplicate groups and mark any as resolved if all labels are approved.

    A group is considered resolved when all non-keeper files have approved trash labels.
    """
    # Get all currently unresolved groups
    unresolved_groups = conn.execute(
        """
        SELECT DISTINCT dg.group_id
        FROM duplicate_groups dg
        WHERE dg.all_labels_approved_at IS NULL
        """
    ).fetchall()

    for (group_id,) in unresolved_groups:
        # Get the keeper file ID for this group (or find the one with recommended_keep)
        keeper = conn.execute(
            """
            SELECT file_id FROM duplicate_groups
            WHERE group_id = ? AND recommended_keep = 1
            LIMIT 1
            """,
            (group_id,),
        ).fetchone()

        if not keeper:
            # No keeper selected yet, can't be resolved
            continue

        keeper_id = keeper["file_id"]

        # Check if all non-keeper files have approved trash labels
        all_approved = True
        non_keepers = conn.execute(
            """
            SELECT file_id FROM duplicate_groups
            WHERE group_id = ? AND file_id != ?
            """,
            (group_id, keeper_id),
        ).fetchall()

        for (file_id,) in non_keepers:
            label = conn.execute(
                """
                SELECT action, approved_at FROM labels
                WHERE file_id = ?
                """,
                (file_id,),
            ).fetchone()

            if not label or label["action"] != "trash" or not label["approved_at"]:
                all_approved = False
                break

        if all_approved and non_keepers:
            # All non-keeper files have approved trash labels
            conn.execute(
                """
                UPDATE duplicate_groups
                SET all_labels_approved_at = ?
                WHERE group_id = ?
                """,
                (now_iso, group_id),
            )


# ---------------------------------------------------------------------------
# File browser routes
# ---------------------------------------------------------------------------

@app.get("/files", response_class=HTMLResponse)
async def file_browser(
    request: Request,
    mime: str = "",
    drive_id: str = "",
    label_status: str = "unlabeled",
    date_from: str = "",
    date_to: str = "",
    sort: str = "size",
    order: str = "desc",
    page: int = 1,
    per_page: int = 100,
    path: str = "",
    recursive: str = "true",
    show_trash: str = "",
    filename_pattern: str = "",
) -> HTMLResponse:
    start = time.time()
    rows_ctx = _files_rows_context(
        mime=mime,
        drive_id=drive_id,
        label_status=label_status,
        date_from=date_from,
        date_to=date_to,
        sort=sort,
        order=order,
        page=page,
        per_page=per_page,
        path=path,
        recursive=recursive,
        show_trash=show_trash,
        filename_pattern=filename_pattern,
    )
    drives = _fetch_drives()
    mime_types = _fetch_mime_types()
    breadcrumbs = _build_breadcrumbs(path)

    ctx = {
        **rows_ctx,
        "drives": drives,
        "mime_types": mime_types,
        "breadcrumbs": breadcrumbs,
        "active_path": path,
        "filters": {
            "mime": mime,
            "drive_id": drive_id,
            "label_status": label_status,
            "date_from": date_from,
            "date_to": date_to,
            "sort": sort,
            "order": order,
            "path": path,
            "recursive": recursive,
            "show_trash": show_trash,
            "filename_pattern": filename_pattern,
            "per_page": per_page,
        },
    }

    elapsed = (time.time() - start) * 1000
    _log_timing("GET /files", elapsed)

    # HTMX partial swap — return only the table region
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request, name="_files_rows.html", context=ctx
        )

    return templates.TemplateResponse(
        request=request, name="files.html", context=ctx
    )


@app.post("/files/bulk-label", response_class=HTMLResponse)
async def bulk_label_files(request: Request) -> HTMLResponse:
    """Apply a user label to many files from the files list (same semantics as single-file label)."""
    allowed = {"keep_hot", "keep_cold", "trash", "remove"}
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    form = await request.form()
    action_raw = form.get("action")
    action = str(action_raw) if action_raw is not None else ""
    if action not in allowed:
        raise HTTPException(status_code=400, detail="Invalid action")

    file_ids = [str(v) for v in form.getlist("file_ids") if v]
    if not file_ids:
        raise HTTPException(status_code=400, detail="No files selected")

    def _str_field(key: str, default: str = "") -> str:
        v = form.get(key)
        return str(v) if v is not None else default

    mime = _str_field("mime")
    drive_id = _str_field("drive_id")
    label_status = _str_field("label_status")
    date_from = _str_field("date_from")
    date_to = _str_field("date_to")
    path = _str_field("path")
    recursive = _str_field("recursive", "true") or "true"
    sort = _str_field("sort", "date") or "date"
    order = _str_field("order", "desc") or "desc"
    show_trash = _str_field("show_trash")
    filename_pattern = _str_field("filename_pattern")
    page_raw = form.get("page")
    try:
        page = int(page_raw) if page_raw is not None else 1
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1
    per_page_raw = form.get("per_page")
    try:
        per_page = int(per_page_raw) if per_page_raw is not None else 100
    except (TypeError, ValueError):
        per_page = 100
    if per_page < 1:
        per_page = 100

    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        placeholders = ",".join("?" * len(file_ids))
        existing = conn.execute(
            f"SELECT id FROM files WHERE id IN ({placeholders})",
            file_ids,
        ).fetchall()
        valid_ids = [r["id"] for r in existing]
        if not valid_ids:
            raise HTTPException(status_code=400, detail="No matching files")

        def _do_insert_labels():
            if action == "remove":
                for fid in valid_ids:
                    conn.execute("DELETE FROM labels WHERE file_id = ?", (fid,))
            else:
                for fid in valid_ids:
                    conn.execute(
                        """
                        INSERT INTO labels (file_id, action, source, labeled_at)
                        VALUES (?, ?, 'user', ?)
                        ON CONFLICT(file_id) DO UPDATE SET
                            action = excluded.action,
                            source = 'user',
                            confidence = NULL,
                            reasoning = NULL,
                            labeled_at = excluded.labeled_at,
                            approved_at = NULL
                        """,
                        (fid, action, now),
                    )
            conn.commit()

        _with_retry(_do_insert_labels)

    ctx = _files_rows_context(
        mime=mime,
        drive_id=drive_id,
        label_status=label_status,
        date_from=date_from,
        date_to=date_to,
        sort=sort,
        order=order,
        page=page,
        per_page=per_page,
        path=path,
        recursive=recursive,
        show_trash=show_trash,
        filename_pattern=filename_pattern,
    )
    return templates.TemplateResponse(
        request=request, name="_files_rows.html", context=ctx
    )


@app.get("/files/{file_id}", response_class=HTMLResponse)
async def file_detail(
    request: Request,
    file_id: str,
    mime: str = "",
    drive_id: str = "",
    label_status: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "date",
    order: str = "desc",
    page: int = 1,
    per_page: int = 100,
    filename_pattern: str = "",
) -> HTMLResponse:
    import time
    start = time.time()

    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    filters = dict(
        mime=mime,
        drive_id=drive_id,
        label_status=label_status,
        date_from=date_from,
        date_to=date_to,
        sort=sort,
        order=order,
        page=page,
        per_page=per_page,
        filename_pattern=filename_pattern,
    )

    with _get_conn() as conn:
        file_row = conn.execute(
            """
            SELECT f.*, d.label AS drive_label, d.id AS drive_id_col
            FROM files f
            LEFT JOIN drives d ON d.id = f.drive_id
            WHERE f.id = ?
            """,
            (file_id,),
        ).fetchone()

        if not file_row:
            raise HTTPException(status_code=404, detail="File not found")

        file = dict(file_row)

        # file_analysis (take the most recent by analyzed_at)
        analysis_row = conn.execute(
            """
            SELECT summary, tags, text_content, analyzer, analyzed_at
            FROM file_analysis
            WHERE file_id = ?
            ORDER BY analyzed_at DESC
            LIMIT 1
            """,
            (file_id,),
        ).fetchone()
        analysis = dict(analysis_row) if analysis_row else None
        if analysis and analysis.get("tags"):
            try:
                analysis["tags"] = json.loads(analysis["tags"])
            except (json.JSONDecodeError, TypeError):
                analysis["tags"] = []

        # label
        label_row = conn.execute(
            "SELECT action, source, confidence, reasoning, labeled_at, approved_at FROM labels WHERE file_id = ?",
            (file_id,),
        ).fetchone()
        label = dict(label_row) if label_row else None

        # duplicate groups with all members (single query instead of N+1)
        dup_all_rows = conn.execute(
            """
            SELECT dg.group_id, dg.match_type, dg.confidence, dg.recommended_keep,
                   COUNT(*) OVER (PARTITION BY dg.group_id) AS group_size,
                   dg.file_id, f.path, f.status, f.size_bytes
            FROM duplicate_groups dg
            JOIN files f ON f.id = dg.file_id
            WHERE dg.group_id IN (
              SELECT dg2.group_id FROM duplicate_groups dg2 WHERE dg2.file_id = ?
            )
            ORDER BY dg.group_id, f.path
            """,
            (file_id,),
        ).fetchall()

        # Group results by duplicate group
        duplicates = []
        seen_groups = set()
        for row in dup_all_rows:
            dup_dict = dict(row)
            group_id = dup_dict["group_id"]

            # Create group entry if first time seeing this group
            if group_id not in seen_groups:
                seen_groups.add(group_id)
                duplicates.append({
                    "group_id": group_id,
                    "match_type": dup_dict["match_type"],
                    "confidence": dup_dict["confidence"],
                    "recommended_keep": dup_dict["recommended_keep"],
                    "group_size": dup_dict["group_size"],
                    "members": []
                })

            # Add file to the group's members
            duplicates[-1]["members"].append({
                "file_id": dup_dict["file_id"],
                "path": dup_dict["path"],
                "status": dup_dict["status"],
                "size_bytes": dup_dict["size_bytes"]
            })

        faces = _fetch_faces_for_file(conn, file_id)

    # Read EXIF from disk if file is an image and present on disk
    exif = {}
    file_path = Path(file["path"]) if file.get("path") else None
    is_image = (file.get("mime_type") or "").startswith("image/")
    if is_image and file_path and file_path.exists():
        exif = _read_exif(file_path)

    elapsed = (time.time() - start) * 1000
    _log_timing("GET /files/{id}", elapsed)

    ctx = {
        "file": file,
        "file_id": file_id,
        "analysis": analysis,
        "label": label,
        "duplicates": duplicates,
        "exif": exif,
        "is_image": is_image,
        "filters": filters,
        "filter_qs": _build_filter_qs(filters),
        "faces": faces,
    }
    return templates.TemplateResponse(request=request, name="file_detail.html", context=ctx)


def _face_card_response(request: Request, file_id: str, face_index: int) -> HTMLResponse:
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")
    with _get_conn() as conn:
        face = _fetch_one_face(conn, file_id, face_index)
        if not face:
            raise HTTPException(status_code=404, detail="Face not found")
    return templates.TemplateResponse(
        request=request,
        name="_face_card.html",
        context={"file": {"id": file_id}, "face": face},
    )


@app.post("/files/{file_id}/faces/{face_index}/label", response_class=HTMLResponse)
async def label_face(
    request: Request,
    file_id: str,
    face_index: int,
    name: str = Form(""),
) -> HTMLResponse:
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    now = _utc_now_iso()
    with _get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM face_labels WHERE file_id = ? AND face_index = ?",
            (file_id, face_index),
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Face not found")

        person_id = _find_or_create_person_by_name(conn, name, now)

        def _do_update():
            # Get the cluster_id of the face being labeled
            cluster_id = conn.execute(
                "SELECT cluster_id FROM face_labels WHERE file_id = ? AND face_index = ?",
                (file_id, face_index),
            ).fetchone()
            cluster_id = cluster_id[0] if cluster_id else None

            # Update the face being labeled
            conn.execute(
                """
                UPDATE face_labels
                SET person_id = ?, source = 'user', confidence = NULL, labeled_at = ?, cluster_id = NULL
                WHERE file_id = ? AND face_index = ?
                """,
                (person_id, now, file_id, face_index),
            )

            # If this face is in a cluster, reassign all other faces in that cluster
            if cluster_id:
                conn.execute(
                    """
                    UPDATE face_labels
                    SET person_id = ?, source = 'user', confidence = NULL, labeled_at = ?, cluster_id = NULL
                    WHERE cluster_id = ? AND NOT (file_id = ? AND face_index = ?)
                    """,
                    (person_id, now, cluster_id, file_id, face_index),
                )

            conn.commit()

        _with_retry(_do_update)

    return _face_card_response(request, file_id, face_index)


@app.post("/files/{file_id}/faces/{face_index}/confirm-auto", response_class=HTMLResponse)
async def confirm_auto_face(request: Request, file_id: str, face_index: int) -> HTMLResponse:
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    now = _utc_now_iso()
    with _get_conn() as conn:
        def _do_confirm():
            cur = conn.execute(
                """
                UPDATE face_labels
                SET source = 'user', confidence = NULL, labeled_at = ?
                WHERE file_id = ? AND face_index = ? AND source = 'auto'
                """,
                (now, file_id, face_index),
            )
            conn.commit()
            return cur.rowcount

        rowcount = _with_retry(_do_confirm)
        if rowcount == 0:
            raise HTTPException(status_code=400, detail="Face is not an auto label or not found")

    return _face_card_response(request, file_id, face_index)


@app.post("/people/{person_id}/confirm-multiple")
async def confirm_multiple_faces(request: Request, person_id: str) -> Response:
    """Bulk confirm multiple auto-matched face suggestions."""
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    body = await request.json()
    faces = body.get("faces", [])  # List of {"file_id": str, "face_index": int}

    if not faces:
        raise HTTPException(status_code=400, detail="No faces provided")

    now = _utc_now_iso()
    with _get_conn() as conn:
        def _do_confirm_multiple():
            confirmed_count = 0
            for face in faces:
                file_id = face.get("file_id")
                face_index = face.get("face_index")
                if not file_id or face_index is None:
                    continue

                cur = conn.execute(
                    """
                    UPDATE face_labels
                    SET person_id = ?, confidence = NULL, labeled_at = ?
                    WHERE file_id = ? AND face_index = ? AND person_id IS NULL
                    """,
                    (person_id, now, file_id, face_index),
                )
                confirmed_count += cur.rowcount

            conn.commit()
            return confirmed_count

        confirmed_count = _with_retry(_do_confirm_multiple)
        if confirmed_count == 0:
            raise HTTPException(status_code=400, detail="No faces were confirmed")

    # Redirect back to person detail page
    return RedirectResponse(url=f"/people/{person_id}", status_code=303)


@app.post("/people/{person_id}/remove-multiple")
async def remove_multiple_faces(request: Request, person_id: str) -> Response:
    """Bulk remove faces from a person (unlink person_id)."""
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    body = await request.json()
    faces = body.get("faces", [])  # List of {"file_id": str, "face_index": int}

    if not faces:
        raise HTTPException(status_code=400, detail="No faces provided")

    import sys
    print(f"DEBUG: Removing {len(faces)} faces for person {person_id}", file=sys.stderr)
    for face in faces:
        print(f"DEBUG: face={face}, file_id={face.get('file_id')}, face_index={face.get('face_index')} (type: {type(face.get('face_index'))})", file=sys.stderr)

    now = _utc_now_iso()
    with _get_conn() as conn:
        def _do_remove_multiple():
            removed_count = 0
            for face in faces:
                file_id = face.get("file_id")
                face_index = face.get("face_index")
                if not file_id or face_index is None:
                    continue

                cur = conn.execute(
                    """
                    UPDATE face_labels
                    SET person_id = NULL, source = 'user', confidence = NULL
                    WHERE file_id = ? AND face_index = ? AND person_id = ?
                    """,
                    (file_id, face_index, person_id),
                )
                removed_count += cur.rowcount

            conn.commit()
            return removed_count

        removed_count = _with_retry(_do_remove_multiple)
        if removed_count == 0:
            raise HTTPException(status_code=400, detail="No faces were removed")

    # Redirect back to person detail page
    return RedirectResponse(url=f"/people/{person_id}", status_code=303)


@app.post("/files/{file_id}/label", response_class=HTMLResponse)
async def label_file(
    request: Request,
    file_id: str,
    action: str = Form(...),
) -> HTMLResponse:
    allowed = {"keep_hot", "keep_cold", "trash", "remove"}
    if action not in allowed:
        raise HTTPException(status_code=400, detail="Invalid action")

    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM files WHERE id = ?", (file_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="File not found")

        def _do_label():
            if action == "remove":
                conn.execute("DELETE FROM labels WHERE file_id = ?", (file_id,))
            else:
                conn.execute(
                    """
                    INSERT INTO labels (file_id, action, source, labeled_at)
                    VALUES (?, ?, 'user', ?)
                    ON CONFLICT(file_id) DO UPDATE SET
                        action = excluded.action,
                        source = 'user',
                        confidence = NULL,
                        reasoning = NULL,
                        labeled_at = excluded.labeled_at,
                        approved_at = NULL
                    """,
                    (file_id, action, now),
                )
            conn.commit()

        _with_retry(_do_label)

        label_row = conn.execute(
            "SELECT action, source, confidence, reasoning, labeled_at, approved_at FROM labels WHERE file_id = ?",
            (file_id,),
        ).fetchone()
        label = dict(label_row) if label_row else None

    return templates.TemplateResponse(
        request=request,
        name="_label_buttons.html",
        context={"file_id": file_id, "label": label},
    )


@app.get("/image/{file_id}")
async def serve_image(file_id: str) -> Response:
    """Serve the actual image file from disk (images only)."""
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT path, mime_type FROM files WHERE id = ?", (file_id,)
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="File not found")

    mime_type = row["mime_type"] or ""
    if not mime_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Not an image file")

    file_path = row["path"]

    # Handle Dropbox files
    if file_path.startswith("/dropbox/"):
        try:
            from magpie.dropbox_auth import get_dropbox_client
            from magpie.dropbox_cache import get_dropbox_file
            dbx = get_dropbox_client()
            content = get_dropbox_file(dbx, file_id, stream=True)
            if content:
                return Response(content=content, media_type=mime_type)
        except Exception as e:
            from rich.console import Console
            Console().log(f"[red]Failed to serve Dropbox image {file_id}: {e}[/red]")
        raise HTTPException(status_code=404, detail="Dropbox file not accessible")

    # Handle Google Drive files (similar pattern if needed)
    if file_path.startswith("/gdrive/"):
        raise HTTPException(status_code=404, detail="Google Drive image serving not yet implemented")

    # Handle local files
    file_path_obj = Path(file_path)
    if not file_path_obj.exists():
        raise HTTPException(status_code=404, detail="File not on disk")

    return FileResponse(str(file_path_obj), media_type=mime_type)


@app.get("/faces/{file_id}/{face_index}")
async def serve_face_crop(file_id: str, face_index: int) -> Response:
    """Serve a cropped face thumbnail from ~/.magpie/faces/{file_id}_{face_index}.jpg."""
    if face_index < 0:
        raise HTTPException(status_code=400, detail="Invalid face index")

    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM face_labels WHERE file_id = ? AND face_index = ?",
            (file_id, face_index),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Face not found")

    crop_path = FACES_DIR / f"{file_id}_{face_index}.jpg"
    if not crop_path.exists():
        raise HTTPException(status_code=404, detail="Face thumbnail not on disk")

    return FileResponse(str(crop_path), media_type="image/jpeg")


@app.get("/thumbs/{file_id}")
async def thumbnail(file_id: str) -> Response:
    thumb_path = THUMBS_DIR / f"{file_id}.jpg"
    if thumb_path.exists():
        return FileResponse(str(thumb_path), media_type="image/jpeg")

    # No cached JPEG yet — build one from the indexed path (same as `magpie index --with-thumbs`)
    if DB_PATH.exists():
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT path, mime_type, extension, status FROM files WHERE id = ?",
                (file_id,),
            ).fetchone()
        if row and row["status"] == "present":
            file_path = row["path"]

            # Handle Dropbox files: thumbnail already cached if indexing ran with --with-thumbs
            if file_path.startswith("/dropbox/"):
                if thumb_path.exists():
                    return FileResponse(str(thumb_path), media_type="image/jpeg")
                # Thumbnail not cached yet; return placeholder
                # (On-demand generation could be added here if needed)
                pass

            # Handle local files
            if not file_path.startswith("/"):  # local file path
                src = Path(file_path)
                if src.is_file() and is_indexed_image_row(row["mime_type"], row["extension"]):
                    out = generate_thumbnail(src, file_id)
                    if out is not None and out.exists():
                        return FileResponse(str(out), media_type="image/jpeg")

    # For audio files without cover art, show a music note placeholder
    if DB_PATH.exists():
        with _get_conn() as conn:
            mime_row = conn.execute(
                "SELECT mime_type, extension FROM files WHERE id = ?", (file_id,)
            ).fetchone()
        if mime_row:
            from magpie.audio_analyzer import is_audio as _is_audio
            if _is_audio(mime_row["mime_type"], mime_row["extension"]):
                music_svg = (
                    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
                    '<rect width="64" height="64" fill="#1f2937"/>'
                    # Eighth note shape
                    '<path d="M38 14 L38 40" stroke="#6366f1" stroke-width="2.5" stroke-linecap="round"/>'
                    '<path d="M38 14 L50 18 L50 26 L38 22 Z" fill="#6366f1"/>'
                    '<circle cx="32" cy="42" r="6" fill="#6366f1"/>'
                    "</svg>"
                )
                return Response(content=music_svg, media_type="image/svg+xml")

    # Serve SVG placeholder (avoid emoji — some browsers fail <img> with SVG+emoji)
    if PLACEHOLDER_THUMB.exists():
        return FileResponse(str(PLACEHOLDER_THUMB), media_type="image/svg+xml")

    # Inline minimal SVG if no placeholder file exists
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
        '<rect width="64" height="64" fill="#1f2937"/>'
        '<rect x="14" y="12" width="36" height="40" rx="3" fill="none" stroke="#4b5563" stroke-width="2"/>'
        '<line x1="20" y1="22" x2="44" y2="22" stroke="#4b5563" stroke-width="2"/>'
        '<line x1="20" y1="30" x2="40" y2="30" stroke="#4b5563" stroke-width="2"/>'
        "</svg>"
    )
    return Response(content=svg, media_type="image/svg+xml")


# ---------------------------------------------------------------------------
# Action queue (approved labels)
# ---------------------------------------------------------------------------

QUEUE_GROUP_META: tuple[dict, ...] = (
    {
        "db_action": "keep_hot",
        "title": "Hot",
        "executor_arg": "hot",
        "border": "border-amber-800/60",
        "accent": "text-amber-400",
    },
    {
        "db_action": "keep_cold",
        "title": "Cold",
        "executor_arg": "cold",
        "border": "border-indigo-800/60",
        "accent": "text-indigo-300",
    },
    {
        "db_action": "trash",
        "title": "Trash",
        "executor_arg": "trash",
        "border": "border-red-900/50",
        "accent": "text-red-400",
    },
)


def _normalize_bulk_approve_action(raw: str) -> str | None:
    key = (raw or "").strip().lower()
    aliases = {
        "trash": "trash",
        "hot": "keep_hot",
        "cold": "keep_cold",
        "keep_hot": "keep_hot",
        "keep_cold": "keep_cold",
    }
    return aliases.get(key)


def _fetch_pending_label_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {"keep_hot": 0, "keep_cold": 0, "trash": 0}
    rows = conn.execute(
        """
        SELECT action, COUNT(*) AS c
        FROM labels
        WHERE approved_at IS NULL
        GROUP BY action
        """
    ).fetchall()
    for r in rows:
        act = r["action"]
        if act in counts:
            counts[act] = int(r["c"] or 0)
    return counts


def _fetch_approved_queue_groups(
    page_hot: int = 1, page_cold: int = 1, page_trash: int = 1, page_size: int = 50
) -> tuple[list[dict], dict[str, int]]:
    """Return (group dicts with paginated files + aggregates, pending counts).

    Each group gets its own page parameter and pagination info.
    """
    empty_pending = {"keep_hot": 0, "keep_cold": 0, "trash": 0}
    if not DB_PATH.exists():
        groups = [
            {**meta, "files": [], "count": 0, "total_bytes": 0, "sample_ids": [], "page": 1, "total_pages": 1}
            for meta in QUEUE_GROUP_META
        ]
        return groups, empty_pending

    with _get_conn() as conn:
        pending = _fetch_pending_label_counts(conn)
        rows = conn.execute(
            """
            SELECT f.id, f.path, f.size_bytes, f.modified_at, f.created_at, f.mime_type,
                   lbl.action, lbl.source, lbl.confidence, lbl.approved_at
            FROM labels lbl
            JOIN files f ON f.id = lbl.file_id
            WHERE lbl.approved_at IS NOT NULL
            ORDER BY lbl.action, f.path
            """
        ).fetchall()

    by_action: dict[str, list[dict]] = {m["db_action"]: [] for m in QUEUE_GROUP_META}
    for r in rows:
        d = dict(r)
        act = d.get("action")
        if act in by_action:
            by_action[act].append(d)

    page_map = {
        "keep_hot": page_hot,
        "keep_cold": page_cold,
        "trash": page_trash,
    }

    groups: list[dict] = []
    for meta in QUEUE_GROUP_META:
        all_files = by_action[meta["db_action"]]
        total_count = len(all_files)
        total_pages = max(1, (total_count + page_size - 1) // page_size)

        # Get the current page (validate)
        current_page = page_map.get(meta["db_action"], 1)
        if current_page < 1:
            current_page = 1
        if current_page > total_pages:
            current_page = total_pages

        # Paginate
        offset = (current_page - 1) * page_size
        paginated_files = all_files[offset : offset + page_size]

        total_bytes = sum(int(f["size_bytes"] or 0) for f in all_files)
        sample_ids = [f["id"] for f in all_files[:8]]
        groups.append(
            {
                **meta,
                "files": paginated_files,
                "count": total_count,
                "total_bytes": total_bytes,
                "sample_ids": sample_ids,
                "page": current_page,
                "total_pages": total_pages,
                "page_size": page_size,
            }
        )
    return groups, pending


def _fetch_pending_queue_groups(
    page_hot_p: int = 1, page_cold_p: int = 1, page_trash_p: int = 1, page_size: int = 50
) -> list[dict]:
    """Return (group dicts with paginated pending files + aggregates).

    Similar to _fetch_approved_queue_groups but for labels WHERE approved_at IS NULL.
    Each group gets its own page parameter and pagination info.
    """
    if not DB_PATH.exists():
        groups = [
            {**meta, "files": [], "count": 0, "total_bytes": 0, "sample_ids": [], "page": 1, "total_pages": 1}
            for meta in QUEUE_GROUP_META
        ]
        return groups

    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT f.id, f.path, f.size_bytes, f.modified_at, f.created_at, f.mime_type,
                   lbl.action, lbl.source, lbl.confidence, lbl.labeled_at
            FROM labels lbl
            JOIN files f ON f.id = lbl.file_id
            WHERE lbl.approved_at IS NULL
            ORDER BY lbl.action, f.path
            """
        ).fetchall()

    by_action: dict[str, list[dict]] = {m["db_action"]: [] for m in QUEUE_GROUP_META}
    for r in rows:
        d = dict(r)
        act = d.get("action")
        if act in by_action:
            by_action[act].append(d)

    page_map = {
        "keep_hot": page_hot_p,
        "keep_cold": page_cold_p,
        "trash": page_trash_p,
    }

    groups: list[dict] = []
    for meta in QUEUE_GROUP_META:
        all_files = by_action[meta["db_action"]]
        total_count = len(all_files)
        total_pages = max(1, (total_count + page_size - 1) // page_size)

        # Get the current page (validate)
        current_page = page_map.get(meta["db_action"], 1)
        if current_page < 1:
            current_page = 1
        if current_page > total_pages:
            current_page = total_pages

        # Paginate
        offset = (current_page - 1) * page_size
        paginated_files = all_files[offset : offset + page_size]

        total_bytes = sum(int(f["size_bytes"] or 0) for f in all_files)
        sample_ids = [f["id"] for f in all_files[:8]]
        groups.append(
            {
                **meta,
                "files": paginated_files,
                "count": total_count,
                "total_bytes": total_bytes,
                "sample_ids": sample_ids,
                "page": current_page,
                "total_pages": total_pages,
                "page_size": page_size,
            }
        )
    return groups


def _build_queue_page_url(
    current_page_hot: int, current_page_cold: int, current_page_trash: int, action: str, new_page: int
) -> str:
    """Build a /queue URL preserving all page parameters."""
    params = []
    if current_page_hot > 1:
        params.append(f"page_hot={current_page_hot}")
    if current_page_cold > 1:
        params.append(f"page_cold={current_page_cold}")
    if current_page_trash > 1:
        params.append(f"page_trash={current_page_trash}")

    # Update the specific action's page
    action_key = f"page_{action}"
    params = [p for p in params if not p.startswith(f"{action_key}=")]
    if new_page > 1:
        params.append(f"{action_key}={new_page}")

    qs = "&".join(params)
    return f"/queue?{qs}" if qs else "/queue"


def _build_pending_queue_page_url(
    current_page_hot_p: int,
    current_page_cold_p: int,
    current_page_trash_p: int,
    action: str,
    new_page: int,
) -> str:
    """Build a /queue URL preserving all pending page parameters (page_*_p)."""
    params = []
    if current_page_hot_p > 1:
        params.append(f"page_hot_p={current_page_hot_p}")
    if current_page_cold_p > 1:
        params.append(f"page_cold_p={current_page_cold_p}")
    if current_page_trash_p > 1:
        params.append(f"page_trash_p={current_page_trash_p}")

    # Update the specific action's pending page
    action_key = f"page_{action}_p"
    params = [p for p in params if not p.startswith(f"{action_key}=")]
    if new_page > 1:
        params.append(f"{action_key}={new_page}")

    qs = "&".join(params)
    return f"/queue?{qs}" if qs else "/queue"


@app.get("/queue", response_class=HTMLResponse)
async def action_queue(
    request: Request,
    approved: int = 0,
    removed: int = 0,
    page_hot: int = 1,
    page_cold: int = 1,
    page_trash: int = 1,
    page_hot_p: int = 1,
    page_cold_p: int = 1,
    page_trash_p: int = 1,
) -> HTMLResponse:
    groups, pending = _fetch_approved_queue_groups(
        page_hot=page_hot, page_cold=page_cold, page_trash=page_trash
    )
    pending_groups = _fetch_pending_queue_groups(
        page_hot_p=page_hot_p, page_cold_p=page_cold_p, page_trash_p=page_trash_p
    )

    # Add URL builder to each approved group for pagination links (preserving both approved and pending params)
    for group in groups:
        action = group["executor_arg"]

        def _make_group_url(page_num, act=action, ph=page_hot, pc=page_cold, pt=page_trash, php=page_hot_p, pcp=page_cold_p, ptp=page_trash_p):
            # Preserve both approved and pending page params
            params = []
            if act == "hot":
                if page_num > 1:
                    params.append(f"page_hot={page_num}")
            elif act == "cold":
                if page_num > 1:
                    params.append(f"page_cold={page_num}")
            else:  # trash
                if page_num > 1:
                    params.append(f"page_trash={page_num}")

            # Add other approved page params
            if act != "hot" and ph > 1:
                params.append(f"page_hot={ph}")
            if act != "cold" and pc > 1:
                params.append(f"page_cold={pc}")
            if act != "trash" and pt > 1:
                params.append(f"page_trash={pt}")

            # Add all pending page params
            if php > 1:
                params.append(f"page_hot_p={php}")
            if pcp > 1:
                params.append(f"page_cold_p={pcp}")
            if ptp > 1:
                params.append(f"page_trash_p={ptp}")

            qs = "&".join(params)
            return f"/queue?{qs}" if qs else "/queue"

        group["prev_url"] = _make_group_url(group["page"] - 1) if group["page"] > 1 else None
        group["next_url"] = _make_group_url(group["page"] + 1) if group["page"] < group["total_pages"] else None

    # Add URL builder to each pending group for pagination links (preserving both approved and pending params)
    for group in pending_groups:
        action = group["executor_arg"]

        def _make_pending_group_url(page_num, act=action, php=page_hot_p, pcp=page_cold_p, ptp=page_trash_p, ph=page_hot, pc=page_cold, pt=page_trash):
            # Preserve both approved and pending page params
            params = []
            if act == "hot":
                if page_num > 1:
                    params.append(f"page_hot_p={page_num}")
            elif act == "cold":
                if page_num > 1:
                    params.append(f"page_cold_p={page_num}")
            else:  # trash
                if page_num > 1:
                    params.append(f"page_trash_p={page_num}")

            # Add other pending page params
            if act != "hot" and php > 1:
                params.append(f"page_hot_p={php}")
            if act != "cold" and pcp > 1:
                params.append(f"page_cold_p={pcp}")
            if act != "trash" and ptp > 1:
                params.append(f"page_trash_p={ptp}")

            # Add all approved page params
            if ph > 1:
                params.append(f"page_hot={ph}")
            if pc > 1:
                params.append(f"page_cold={pc}")
            if pt > 1:
                params.append(f"page_trash={pt}")

            qs = "&".join(params)
            return f"/queue?{qs}" if qs else "/queue"

        group["prev_url"] = _make_pending_group_url(group["page"] - 1) if group["page"] > 1 else None
        group["next_url"] = _make_pending_group_url(group["page"] + 1) if group["page"] < group["total_pages"] else None

    return templates.TemplateResponse(
        request=request,
        name="queue.html",
        context={
            "groups": groups,
            "pending_groups": pending_groups,
            "pending": pending,
            "approved_bulk": approved,
            "removed_bulk": removed,
        },
    )


@app.get("/queue/execute-stream")
async def queue_execute_stream(action: str) -> StreamingResponse:
    allowed = {m["executor_arg"] for m in QUEUE_GROUP_META}
    if action not in allowed:

        async def err_gen():
            yield f"data: {json.dumps({'error': 'invalid action', 'allowed': sorted(allowed)})}\n\n"

        return StreamingResponse(err_gen(), media_type="text/event-stream")

    db_abs = str(DB_PATH.resolve())

    async def event_gen():
        cmd = [
            sys.executable,
            "-m",
            "magpie.cli",
            "execute",
            "--action",
            action,
            "--db",
            db_abs,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        if proc.stdout is None:
            yield f"data: {json.dumps('executor: no stdout pipe')}\n\n"
            rc = await proc.wait()
            yield f"data: {json.dumps({'exit_code': rc})}\n\n"
            return
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip("\r\n")
            yield f"data: {json.dumps(text)}\n\n"
        rc = await proc.wait()
        yield f"data: {json.dumps({'exit_code': rc})}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.post("/labels/{file_id}/approve")
async def label_approve(request: Request, file_id: str) -> Response:
    """Approve a pending label (set approved_at = now)."""
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    now = _utc_now_iso()
    with _get_conn() as conn:
        def _do_approve():
            cur = conn.execute(
                """
                UPDATE labels
                SET approved_at = ?
                WHERE file_id = ? AND approved_at IS NULL
                """,
                (now, file_id),
            )
            conn.commit()
            return cur.rowcount

        rowcount = _with_retry(_do_approve)
        if rowcount == 0:
            raise HTTPException(status_code=404, detail="No pending label for this file")

    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": "/queue"})
    return RedirectResponse(url="/queue", status_code=303)


@app.post("/labels/{file_id}/unapprove")
async def label_unapprove(request: Request, file_id: str) -> Response:
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")
    with _get_conn() as conn:
        def _do_unapprove():
            cur = conn.execute(
                """
                UPDATE labels
                SET approved_at = NULL
                WHERE file_id = ? AND approved_at IS NOT NULL
                """,
                (file_id,),
            )
            conn.commit()
            return cur.rowcount

        rowcount = _with_retry(_do_unapprove)
        if rowcount == 0:
            raise HTTPException(status_code=404, detail="No approved label for this file")

    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": "/queue"})
    return RedirectResponse(url="/queue", status_code=303)


@app.post("/labels/approve-all")
async def labels_approve_all(request: Request, action: str) -> Response:
    db_action = _normalize_bulk_approve_action(action)
    if not db_action:
        raise HTTPException(
            status_code=400,
            detail="Invalid action. Use trash, hot, cold, keep_hot, or keep_cold.",
        )
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    now = _utc_now_iso()
    with _get_conn() as conn:
        def _do_approve():
            cur = conn.execute(
                """
                UPDATE labels
                SET approved_at = ?
                WHERE approved_at IS NULL AND action = ?
                """,
                (now, db_action),
            )
            n = cur.rowcount

            # Update duplicate group resolution status if trash labels were just approved
            if db_action == "trash":
                _update_duplicate_group_resolution_status(conn, now)

            conn.commit()
            return n

        n = _with_retry(_do_approve)

    if request.headers.get("HX-Request"):
        return Response(
            status_code=200,
            headers={"HX-Redirect": f"/queue?approved={n}"},
        )
    return RedirectResponse(url=f"/queue?approved={n}", status_code=303)


@app.post("/labels/remove-all")
async def labels_remove_all(request: Request, action: str) -> Response:
    db_action = _normalize_bulk_approve_action(action)
    if not db_action:
        raise HTTPException(
            status_code=400,
            detail="Invalid action. Use trash, hot, cold, keep_hot, or keep_cold.",
        )
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    with _get_conn() as conn:
        def _do_remove():
            cur = conn.execute(
                "DELETE FROM labels WHERE approved_at IS NULL AND action = ?",
                (db_action,),
            )
            n = cur.rowcount
            conn.commit()
            return n

        n = _with_retry(_do_remove)

    if request.headers.get("HX-Request"):
        return Response(
            status_code=200,
            headers={"HX-Redirect": f"/queue?removed={n}"},
        )
    return RedirectResponse(url=f"/queue?removed={n}", status_code=303)


# ---------------------------------------------------------------------------
# Purge missing files
# ---------------------------------------------------------------------------

@app.get("/purge-missing/check")
async def purge_missing_check() -> dict:
    """Check how many missing files are in the database."""
    if not DB_PATH.exists():
        return {"count": 0, "total_bytes": 0}

    with _get_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count, COALESCE(SUM(size_bytes), 0) AS total_bytes
            FROM files
            WHERE status = 'missing'
            """
        ).fetchone()
        return {
            "count": int(row["count"] or 0),
            "total_bytes": int(row["total_bytes"] or 0),
        }


@app.post("/purge-missing/execute")
async def purge_missing_execute(request: Request) -> dict:
    """Purge all missing files from the database."""
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    def _do_purge():
        with _get_conn() as conn:
            # Delete all records related to missing files, respecting foreign key constraints
            # Order matters: delete from child tables before parent table
            conn.execute(
                "DELETE FROM face_labels WHERE file_id IN (SELECT id FROM files WHERE status = 'missing')"
            )
            conn.execute(
                "DELETE FROM face_suggestions WHERE file_id IN (SELECT id FROM files WHERE status = 'missing')"
            )
            conn.execute(
                "DELETE FROM file_analysis WHERE file_id IN (SELECT id FROM files WHERE status = 'missing')"
            )
            conn.execute(
                "DELETE FROM file_metadata WHERE file_id IN (SELECT id FROM files WHERE status = 'missing')"
            )
            conn.execute(
                "DELETE FROM duplicate_groups WHERE file_id IN (SELECT id FROM files WHERE status = 'missing')"
            )
            conn.execute(
                "DELETE FROM labels WHERE file_id IN (SELECT id FROM files WHERE status = 'missing')"
            )
            conn.execute(
                "DELETE FROM executions WHERE file_id IN (SELECT id FROM files WHERE status = 'missing')"
            )
            cur = conn.execute("DELETE FROM files WHERE status = 'missing'")
            conn.commit()
            return cur.rowcount

    count = _with_retry(_do_purge)
    return {"deleted_count": count, "success": True}


# ---------------------------------------------------------------------------
# Duplicate review
# ---------------------------------------------------------------------------


@app.get("/duplicates", response_class=HTMLResponse)
async def duplicates_list(
    request: Request,
    preferred_applied: int = 0,
    preferred_none: int = 0,
    show_all: str = "",
    sort_by: str = "size",
    sort_order: str = "desc",
    mime: str = "",
    page: int = 1,
    folder: str = "",
    per_page: int = 100,
    bulk_resolved: int = 0,
    match_type: str = "",
) -> HTMLResponse:
    start = time.time()
    if not DB_PATH.exists():
        ctx = {
            "sections": [],
            "groups": [],
            "preferred_applied": 0,
            "preferred_none": 0,
            "preferred_folder_count": 0,
            "show_all": False,
            "groups_total": 0,
            "groups_page": 1,
            "groups_total_pages": 0,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "mime": mime,
            "mime_types": [],
            "folder": folder,
            "per_page": per_page if per_page in (25, 50, 100, 250, 500, 1000, 2000) else 100,
        }
        return templates.TemplateResponse(
            request=request, name="duplicates.html", context=ctx
        )

    show_all_bool = show_all.lower() in ("true", "1", "yes")
    groups_per_page = per_page if per_page in (25, 50, 100, 250, 500, 1000, 2000) else 100
    offset = (page - 1) * groups_per_page
    mime_filter = mime if mime else None
    folder_filter = folder.rstrip("/") if folder else None
    match_type_filter = match_type if match_type in ("exact", "identical", "near", "all") else None

    with _get_conn() as conn:
        exact_groups, near_groups = _fetch_duplicate_groups_index(conn, show_all=show_all_bool, mime=mime_filter, folder_prefix=folder_filter)
        preferred_folder_count = len(fetch_preferred_duplicate_prefixes(conn))

    if match_type_filter == "all":
        pass
    elif match_type_filter == "exact":
        near_groups = []
        exact_groups = [g for g in exact_groups if g["match_type"] == "exact"]
    elif match_type_filter == "identical":
        near_groups = []
        exact_groups = [g for g in exact_groups if g.get("is_identical")]
    elif match_type_filter == "near":
        exact_groups = []

    mime_types = _fetch_mime_types_for_duplicates()

    # Apply sorting: always keep exact groups first, then apply secondary sort within each type
    reverse = sort_order == "desc"
    if sort_by == "size":
        exact_groups.sort(key=lambda g: g["total_bytes"], reverse=reverse)
        near_groups.sort(key=lambda g: g["total_bytes"], reverse=reverse)
    elif sort_by == "added":
        exact_groups.sort(key=lambda g: g["created_at"] or "", reverse=reverse)
        near_groups.sort(key=lambda g: g["created_at"] or "", reverse=reverse)
    # else: match_type — groups already sorted by type from _fetch_duplicate_groups_index

    all_groups = exact_groups + near_groups

    groups_total = len(all_groups)
    groups_total_pages = max(1, (groups_total + groups_per_page - 1) // groups_per_page)

    # Paginate all groups
    paged_groups = all_groups[offset : offset + groups_per_page]

    # Rebuild sections from paged groups
    # "exact" section includes exact matches and near-dupes with 100% confidence (identical)
    sections: list[dict] = []
    exact_paged = [g for g in paged_groups if g["match_type"] == "exact" or g.get("is_identical")]
    near_paged = [g for g in paged_groups if g["match_type"] == "near" and not g.get("is_identical")]
    if exact_paged:
        sections.append({"match_type": "exact", "groups": exact_paged})
    if near_paged:
        sections.append({"match_type": "near", "groups": near_paged})

    elapsed = (time.time() - start) * 1000
    _log_timing("GET /duplicates", elapsed)

    return templates.TemplateResponse(
        request=request,
        name="duplicates.html",
        context={
            "sections": sections,
            "groups": paged_groups,
            "preferred_applied": preferred_applied,
            "preferred_none": preferred_none,
            "preferred_folder_count": preferred_folder_count,
            "show_all": show_all_bool,
            "groups_total": groups_total,
            "groups_page": page,
            "groups_total_pages": groups_total_pages,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "mime": mime,
            "mime_types": mime_types,
            "folder": folder,
            "per_page": groups_per_page,
            "bulk_resolved": bulk_resolved,
            "match_type": match_type_filter or "",
        },
    )


@app.get("/duplicates/{group_id}", response_class=HTMLResponse)
async def duplicate_group_detail(
    request: Request, group_id: str, updated: int = 0
) -> HTMLResponse:
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    with _get_conn() as conn:
        meta = conn.execute(
            """
            SELECT MAX(dg.match_type) AS match_type,
                   MIN(dg.confidence) AS min_confidence
            FROM duplicate_groups dg
            WHERE dg.group_id = ?
            """,
            (group_id,),
        ).fetchone()
        if not meta or not meta["match_type"]:
            raise HTTPException(status_code=404, detail="Group not found")

        match_type = meta["match_type"]
        min_confidence = meta["min_confidence"]
        rows = conn.execute(
            """
            SELECT f.id, f.path, f.size_bytes, f.modified_at, f.mime_type, f.status,
                   d.label AS drive_label,
                   dg.recommended_keep, dg.confidence,
                   lbl.action, lbl.approved_at
            FROM duplicate_groups dg
            JOIN files f ON f.id = dg.file_id
            LEFT JOIN drives d ON d.id = f.drive_id
            LEFT JOIN labels lbl ON lbl.file_id = f.id
            WHERE dg.group_id = ?
            ORDER BY dg.recommended_keep DESC, f.path
            """,
            (group_id,),
        ).fetchall()

    members: list[dict] = []
    total_bytes = 0
    keeper_size: int | None = None
    for r in rows:
        d = dict(r)
        total_bytes += d["size_bytes"] or 0
        if d["recommended_keep"]:
            keeper_size = d["size_bytes"] or 0
        members.append(d)

    if keeper_size is None and members:
        keeper_size = max(m["size_bytes"] or 0 for m in members)
    elif keeper_size is None:
        keeper_size = 0

    reclaimable_bytes = max(0, total_bytes - keeper_size)

    for m in members:
        exif_summary = ""
        path = Path(m["path"]) if m.get("path") else None
        mime = m.get("mime_type") or ""
        if path and mime.startswith("image/") and path.exists():
            exif = _read_exif(path)
            exif_summary = _exif_summary(exif)
        m["exif_summary"] = exif_summary

    is_identical = match_type == "near" and min_confidence is not None and min_confidence >= 1.0
    group = {
        "group_id": group_id,
        "match_type": match_type,
        "min_confidence": min_confidence,
        "is_identical": is_identical,
        "total_bytes": total_bytes,
        "reclaimable_bytes": reclaimable_bytes,
    }

    next_group_id = None
    with _get_conn() as conn:
        next_group_id = _get_next_unresolved_duplicate_group(conn, group_id)

    return templates.TemplateResponse(
        request=request,
        name="duplicate_group.html",
        context={
            "group": group,
            "members": members,
            "updated": updated,
            "next_group_id": next_group_id,
        },
    )


@app.post("/duplicates/{group_id}/keep/{file_id}")
async def duplicate_keep_one(group_id: str, file_id: str) -> RedirectResponse:
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    now = _utc_now_iso()
    with _get_conn() as conn:
        def _do_set_keeper():
            in_group = conn.execute(
                """
                SELECT 1 FROM duplicate_groups
                WHERE group_id = ? AND file_id = ?
                """,
                (group_id, file_id),
            ).fetchone()
            if not in_group:
                raise HTTPException(status_code=404, detail="File not in this group")

            set_duplicate_group_keeper_and_queue_trash(conn, group_id, file_id, now)
            conn.commit()

        _with_retry(_do_set_keeper)

    return RedirectResponse(
        url=f"/duplicates/{group_id}?updated=1", status_code=303
    )


@app.post("/duplicates/bulk-auto-resolve")
async def duplicates_bulk_auto_resolve(
    request: Request,
) -> RedirectResponse:
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    form = await request.form()
    group_ids = form.getlist("group_ids")
    redirect_url = str(form.get("redirect_url", "/duplicates"))

    if not group_ids:
        return RedirectResponse(url=redirect_url, status_code=303)

    now = _utc_now_iso()
    resolved = 0

    with _get_conn() as conn:
        def _do_bulk():
            nonlocal resolved
            for gid in group_ids:
                row = conn.execute(
                    "SELECT file_id FROM duplicate_groups WHERE group_id = ? AND recommended_keep = 1 LIMIT 1",
                    (gid,),
                ).fetchone()
                if not row:
                    continue
                set_duplicate_group_keeper_and_queue_trash(conn, gid, row["file_id"], now)
                resolved += 1
            conn.commit()

        _with_retry(_do_bulk)

    # Append resolved count to redirect URL as a flash param
    from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
    parsed = urlparse(redirect_url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["bulk_resolved"] = [str(resolved)]
    new_query = urlencode({k: v[0] for k, v in qs.items()})
    redirect_url = urlunparse(parsed._replace(query=new_query))

    return RedirectResponse(url=redirect_url, status_code=303)


@app.post("/duplicates/apply-preferred")
async def duplicates_apply_preferred() -> RedirectResponse:
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    now = _utc_now_iso()
    with _get_conn() as conn:
        def _do_apply():
            n = apply_preferred_folder_duplicate_resolution(conn, now)
            conn.commit()
            return n

        n = _with_retry(_do_apply)

    if n > 0:
        loc = f"/duplicates?preferred_applied={n}"
    else:
        loc = "/duplicates?preferred_none=1"
    return RedirectResponse(url=loc, status_code=303)


# ---------------------------------------------------------------------------
# Folder browser
# ---------------------------------------------------------------------------


@app.get("/folders", response_class=HTMLResponse)
async def folder_browser(
    request: Request,
    path: str = "",
    preferred_set: int = 0,
    preferred_removed: int = 0,
    rank_updated: int = 0,
    trashed: int = 0,
    refresh: bool = False,
) -> HTMLResponse:
    start = time.time()
    # Invalidate cache to pick up new sources from CLI indexing
    _invalidate_db_root_cache()

    if refresh:
        _fetch_folder_browser_data.invalidate()

    breadcrumbs = _build_breadcrumbs(path)
    folder_data = _fetch_folder_browser_data(path)
    effective_path = folder_data["effective_path"]

    ctx = {
        "breadcrumbs": breadcrumbs,
        "active_path": effective_path,
        "nodes": folder_data["nodes"],
        "preferred_paths_with_ranks": folder_data["preferred_paths_with_ranks"],
        "active_is_preferred": folder_data["active_is_preferred"],
        "preferred_set": preferred_set,
        "preferred_removed": preferred_removed,
        "rank_updated": rank_updated,
        "trashed": trashed,
    }
    elapsed = (time.time() - start) * 1000
    _log_timing("GET /folders", elapsed)
    return templates.TemplateResponse(request=request, name="folders.html", context=ctx)


@app.post("/folders/preferred/add")
async def folders_preferred_add(path: str = Form("")) -> RedirectResponse:
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")
    norm = normalize_path_prefix(path)
    if not norm:
        raise HTTPException(status_code=400, detail="path is required")

    now = _utc_now_iso()
    with _get_conn() as conn:
        def _do_add():
            # Get the next available rank
            next_rank = get_next_available_rank(conn)
            conn.execute(
                """
                INSERT INTO preferred_duplicate_paths (path_prefix, rank, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(path_prefix) DO NOTHING
                """,
                (norm, next_rank, now),
            )
            conn.commit()

        _with_retry(_do_add)

    return RedirectResponse(
        url=f"/folders?path={quote(norm, safe='')}&preferred_set=1",
        status_code=303,
    )


@app.post("/folders/preferred/remove")
async def folders_preferred_remove(path: str = Form("")) -> RedirectResponse:
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")
    norm = normalize_path_prefix(path)
    if not norm:
        raise HTTPException(status_code=400, detail="path is required")

    with _get_conn() as conn:
        def _do_delete():
            # Get the rank of the folder being deleted (for compacting)
            row = conn.execute(
                "SELECT rank FROM preferred_duplicate_paths WHERE path_prefix = ?",
                (norm,)
            ).fetchone()
            if row:
                deleted_rank = row["rank"]
                # Delete the folder
                conn.execute(
                    "DELETE FROM preferred_duplicate_paths WHERE path_prefix = ?",
                    (norm,)
                )
                # Shift all folders with higher ranks down by 1 to keep ranks compact
                conn.execute(
                    "UPDATE preferred_duplicate_paths SET rank = rank - 1 WHERE rank > ?",
                    (deleted_rank,)
                )
            conn.commit()
        _with_retry(_do_delete)

    parent = str(Path(norm).parent) if norm != "/" else ""
    dest = quote(parent, safe="") if parent else ""
    q = f"path={dest}&preferred_removed=1" if dest else "preferred_removed=1"
    return RedirectResponse(url=f"/folders?{q}", status_code=303)


@app.post("/folders/preferred/set-rank")
async def folders_preferred_set_rank(path: str = Form(""), new_rank: int = Form(...)) -> RedirectResponse:
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")
    norm = normalize_path_prefix(path)
    if not norm:
        raise HTTPException(status_code=400, detail="path is required")

    with _get_conn() as conn:
        def _do_update():
            # Validate rank
            count_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM preferred_duplicate_paths"
            ).fetchone()
            count = count_row["cnt"] or 0
            if new_rank < 1 or new_rank > count:
                raise ValueError(f"Rank must be between 1 and {count}")

            # Update the rank with shifting
            set_preferred_path_rank(conn, norm, new_rank)
            conn.commit()

        try:
            _with_retry(_do_update)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    return RedirectResponse(
        url=f"/folders?path={quote(norm, safe='')}&rank_updated=1",
        status_code=303,
    )


@app.get("/folders/children", response_class=HTMLResponse)
async def folder_children_partial(
    request: Request,
    path: str = "",
    show_trash: str = "",
) -> HTMLResponse:
    """HTMX partial: flat list of immediate subfolders at *path* (or DB root)."""
    if not DB_PATH.exists():
        return HTMLResponse("")

    include_trash = show_trash == "true"
    with _get_conn() as conn:
        effective = path or _find_db_root(conn)
        children = _fetch_folder_children(conn, effective, include_trash=include_trash)

    return templates.TemplateResponse(
        request=request,
        name="_folder_node.html",
        context={"nodes": children},
    )


@app.get("/folders/count-files")
async def folder_count_files(path: str = "") -> dict:
    """Count files in a folder (recursive)."""
    if not DB_PATH.exists() or not path:
        return {"count": 0}

    with _get_conn() as conn:
        p = path.rstrip("/")
        count = conn.execute(
            "SELECT COUNT(*) FROM files WHERE path >= ? AND path < ?",
            (p + "/", p + "/~"),
        ).fetchone()[0]

    return {"count": count}


@app.get("/folders/label-stats")
async def folder_label_stats(path: str = "") -> dict:
    """Get label distribution for files in a folder (recursive)."""
    if not DB_PATH.exists() or not path:
        return {"total": 0, "unlabeled": 0, "labeled": {}}

    with _get_conn() as conn:
        p = path.rstrip("/")
        p_lo = p + "/"
        p_hi = p + "/~"

        # Count total files
        total = conn.execute(
            "SELECT COUNT(*) FROM files WHERE path >= ? AND path < ?",
            (p_lo, p_hi),
        ).fetchone()[0]

        # Count files by label action
        label_counts = conn.execute(
            """
            SELECT l.action, COUNT(*)
            FROM files f
            LEFT JOIN labels l ON f.id = l.file_id
            WHERE f.path >= ? AND f.path < ?
            GROUP BY l.action
            """,
            (p_lo, p_hi),
        ).fetchall()

        labeled = {}
        unlabeled = 0
        for action, count in label_counts:
            if action is None:
                unlabeled = count
            else:
                labeled[action] = count

        return {
            "total": total,
            "unlabeled": unlabeled,
            "labeled": labeled,
        }


@app.post("/folders/trash")
async def folder_trash(
    path: str = Form(""),
    mode: str = Form("all"),
) -> RedirectResponse:
    """Mark all files in a folder (recursive) as trash."""
    if not DB_PATH.exists() or not path:
        raise HTTPException(status_code=400, detail="Path required")

    now = _utc_now_iso()
    p = path.rstrip("/")

    with _get_conn() as conn:
        def _do_trash():
            # Get file IDs under this path, optionally filtering to unlabeled only
            if mode == "unlabeled_only":
                file_ids = conn.execute(
                    """
                    SELECT f.id FROM files f
                    WHERE f.path LIKE ?
                    AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.file_id = f.id)
                    """,
                    (p + "/%",),
                ).fetchall()
            else:
                file_ids = conn.execute(
                    "SELECT id FROM files WHERE path LIKE ?",
                    (p + "/%",),
                ).fetchall()

            for (file_id,) in file_ids:
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
                    (file_id, now),
                )
            conn.commit()

        _with_retry(_do_trash)

    return RedirectResponse(
        url=f"/folders?path={quote(path, safe='')}&trashed=1",
        status_code=303,
    )


@app.post("/folders/label-hot")
async def folder_label_hot(
    path: str = Form(""),
    mode: str = Form("all"),
) -> RedirectResponse:
    """Label all files in a folder (recursive) for hot storage."""
    if not DB_PATH.exists() or not path:
        raise HTTPException(status_code=400, detail="Path required")

    now = _utc_now_iso()
    p = path.rstrip("/")

    with _get_conn() as conn:
        def _do_label():
            # Get file IDs under this path, optionally filtering to unlabeled only
            if mode == "unlabeled_only":
                file_ids = conn.execute(
                    """
                    SELECT f.id FROM files f
                    WHERE f.path LIKE ?
                    AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.file_id = f.id)
                    """,
                    (p + "/%",),
                ).fetchall()
            else:
                file_ids = conn.execute(
                    "SELECT id FROM files WHERE path LIKE ?",
                    (p + "/%",),
                ).fetchall()

            for (file_id,) in file_ids:
                conn.execute(
                    """
                    INSERT INTO labels (file_id, action, source, labeled_at)
                    VALUES (?, 'keep_hot', 'user', ?)
                    ON CONFLICT(file_id) DO UPDATE SET
                        action = 'keep_hot',
                        source = 'user',
                        confidence = NULL,
                        reasoning = NULL,
                        labeled_at = excluded.labeled_at,
                        approved_at = NULL
                    """,
                    (file_id, now),
                )
            conn.commit()

        _with_retry(_do_label)

    return RedirectResponse(
        url=f"/folders?path={quote(path, safe='')}&labeled_hot=1",
        status_code=303,
    )


@app.post("/folders/label-cold")
async def folder_label_cold(
    path: str = Form(""),
    mode: str = Form("all"),
) -> RedirectResponse:
    """Label all files in a folder (recursive) for cold storage."""
    if not DB_PATH.exists() or not path:
        raise HTTPException(status_code=400, detail="Path required")

    now = _utc_now_iso()
    p = path.rstrip("/")

    with _get_conn() as conn:
        def _do_label():
            # Get file IDs under this path, optionally filtering to unlabeled only
            if mode == "unlabeled_only":
                file_ids = conn.execute(
                    """
                    SELECT f.id FROM files f
                    WHERE f.path LIKE ?
                    AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.file_id = f.id)
                    """,
                    (p + "/%",),
                ).fetchall()
            else:
                file_ids = conn.execute(
                    "SELECT id FROM files WHERE path LIKE ?",
                    (p + "/%",),
                ).fetchall()

            for (file_id,) in file_ids:
                conn.execute(
                    """
                    INSERT INTO labels (file_id, action, source, labeled_at)
                    VALUES (?, 'keep_cold', 'user', ?)
                    ON CONFLICT(file_id) DO UPDATE SET
                        action = 'keep_cold',
                        source = 'user',
                        confidence = NULL,
                        reasoning = NULL,
                        labeled_at = excluded.labeled_at,
                        approved_at = NULL
                    """,
                    (file_id, now),
                )
            conn.commit()

        _with_retry(_do_label)

    return RedirectResponse(
        url=f"/folders?path={quote(path, safe='')}&labeled_cold=1",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Faces / People
# ---------------------------------------------------------------------------


@app.get("/people", response_class=HTMLResponse)
async def people_index(request: Request, page: int = 1) -> HTMLResponse:
    """Faces index: named people + unlabeled clusters."""
    start = time.time()
    if not DB_PATH.exists():
        response = templates.TemplateResponse(
            request=request,
            name="people.html",
            context={
                "named_people": [],
                "clusters": [],
                "unclustered_count": 0,
                "clusters_total": 0,
                "clusters_page": 1,
                "clusters_total_pages": 0,
            },
        )
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    clusters_per_page = 100
    offset = (page - 1) * clusters_per_page

    with _get_conn() as conn:
        # Named people with photo count and sample face
        named_people = [
            dict(r)
            for r in conn.execute(
                """
                SELECT p.id, p.name,
                       COUNT(DISTINCT fl.file_id) AS photo_count,
                       (SELECT fl2.file_id FROM face_labels fl2
                        WHERE fl2.person_id = p.id
                        ORDER BY fl2.labeled_at LIMIT 1) AS sample_file_id,
                       (SELECT fl2.face_index FROM face_labels fl2
                        WHERE fl2.person_id = p.id
                        ORDER BY fl2.labeled_at LIMIT 1) AS sample_face_index
                FROM people p
                LEFT JOIN face_labels fl ON fl.person_id = p.id
                GROUP BY p.id
                ORDER BY photo_count DESC, p.name
                """
            ).fetchall()
        ]

        # Count total unlabeled clusters
        clusters_total = conn.execute(
            "SELECT COUNT(DISTINCT cluster_id) FROM face_labels WHERE cluster_id IS NOT NULL"
        ).fetchone()[0]

        # Unlabeled clusters (paginated)
        clusters = [
            dict(r)
            for r in conn.execute(
                """
                SELECT cluster_id,
                       COUNT(*) AS face_count,
                       (SELECT file_id FROM face_labels WHERE cluster_id = fl.cluster_id LIMIT 1) AS sample_file_id,
                       (SELECT face_index FROM face_labels WHERE cluster_id = fl.cluster_id LIMIT 1) AS sample_face_index
                FROM face_labels fl
                WHERE cluster_id IS NOT NULL
                GROUP BY cluster_id
                ORDER BY face_count DESC
                LIMIT ? OFFSET ?
                """,
                (clusters_per_page, offset),
            ).fetchall()
        ]

        # Count of unclustered unlabeled faces
        unclustered_count = conn.execute(
            "SELECT COUNT(*) FROM face_labels WHERE person_id IS NULL AND cluster_id IS NULL"
        ).fetchone()[0]

    clusters_total_pages = max(1, (clusters_total + clusters_per_page - 1) // clusters_per_page)

    ctx = {
        "named_people": named_people,
        "clusters": clusters,
        "unclustered_count": unclustered_count,
        "clusters_total": clusters_total,
        "clusters_page": page,
        "clusters_total_pages": clusters_total_pages,
    }
    elapsed = (time.time() - start) * 1000
    _log_timing("GET /people", elapsed)
    response = templates.TemplateResponse(request=request, name="people.html", context=ctx)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.get("/people/{person_id}", response_class=HTMLResponse)
async def person_detail(request: Request, person_id: str, confirmed_page: int = 1, speculative_page: int = 1, limit: int = 200) -> HTMLResponse:
    """Named person detail: confirmed + speculative photos."""
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    # Validate pagination parameters
    confirmed_page = max(1, confirmed_page)
    speculative_page = max(1, speculative_page)
    limit = max(1, min(limit, 100))  # Cap at 100 per page

    with _get_conn() as conn:
        person = conn.execute(
            "SELECT id, name FROM people WHERE id = ?",
            (person_id,),
        ).fetchone()

        if not person:
            raise HTTPException(status_code=404, detail="Person not found")

        # Confirmed photos (user-labeled or auto-matched)
        all_confirmed = [
            dict(r)
            for r in conn.execute(
                """
                SELECT f.id, f.path, fl.face_index
                FROM files f
                JOIN face_labels fl ON fl.file_id = f.id
                WHERE fl.person_id = ? AND f.status != 'trashed'
                ORDER BY f.path, fl.face_index
                """,
                (person_id,),
            ).fetchall()
        ]

        # Paginate confirmed results
        total_confirmed = len(all_confirmed)
        confirmed_offset = (confirmed_page - 1) * limit
        confirmed = all_confirmed[confirmed_offset : confirmed_offset + limit]
        confirmed_total_pages = (total_confirmed + limit - 1) // limit if total_confirmed > 0 else 1

        # Speculative photos (below threshold)
        all_speculative = []
        for r in conn.execute(
            """
            SELECT DISTINCT f.id, f.path, fl.face_index, fs.distance
            FROM files f
            JOIN face_labels fl ON fl.file_id = f.id AND fl.person_id IS NULL
            JOIN face_suggestions fs ON fs.file_id = fl.file_id AND fs.face_index = fl.face_index
            WHERE fs.person_id = ? AND f.status != 'trashed'
            """,
            (person_id,),
        ).fetchall():
            row = dict(r)
            row["distance"] = _convert_distance(row["distance"])
            all_speculative.append(row)

        # Sort by distance ascending (best matches first, lowest distance = most similar)
        all_speculative.sort(key=lambda x: x["distance"])

        # Paginate speculative results
        total_speculative = len(all_speculative)
        speculative_offset = (speculative_page - 1) * limit
        speculative = all_speculative[speculative_offset : speculative_offset + limit]
        speculative_total_pages = (total_speculative + limit - 1) // limit if total_speculative > 0 else 1

    ctx = {
        "person": dict(person),
        "confirmed": confirmed,
        "confirmed_pagination": {
            "page": confirmed_page,
            "limit": limit,
            "total": total_confirmed,
            "total_pages": confirmed_total_pages,
            "has_prev": confirmed_page > 1,
            "has_next": confirmed_page < confirmed_total_pages,
            "prev_page": confirmed_page - 1,
            "next_page": confirmed_page + 1,
        },
        "speculative": speculative,
        "pagination": {
            "page": speculative_page,
            "limit": limit,
            "total": total_speculative,
            "total_pages": speculative_total_pages,
            "has_prev": speculative_page > 1,
            "has_next": speculative_page < speculative_total_pages,
            "prev_page": speculative_page - 1,
            "next_page": speculative_page + 1,
        },
    }
    response = templates.TemplateResponse(request=request, name="person_detail.html", context=ctx)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.post("/people/{person_id}/delete")
async def delete_person(request: Request, person_id: str) -> Response:
    """Delete a person and all their face labels."""
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    with _get_conn() as conn:
        def _do_delete():
            # Delete all face_labels for this person
            conn.execute(
                "DELETE FROM face_labels WHERE person_id = ?",
                (person_id,),
            )
            # Delete the person
            cur = conn.execute(
                "DELETE FROM people WHERE id = ?",
                (person_id,),
            )
            conn.commit()
            return cur.rowcount

        rowcount = _with_retry(_do_delete)
        if rowcount == 0:
            raise HTTPException(status_code=404, detail="Person not found")

    # Redirect back to people list page
    return RedirectResponse(url="/people", status_code=303)


@app.get("/people/cluster/{cluster_id}", response_class=HTMLResponse)
async def cluster_detail(request: Request, cluster_id: str) -> HTMLResponse:
    """Unlabeled cluster detail: all faces in this cluster."""
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    with _get_conn() as conn:
        # Check cluster exists
        cluster_count = conn.execute(
            "SELECT COUNT(*) FROM face_labels WHERE cluster_id = ?",
            (cluster_id,),
        ).fetchone()[0]

        if cluster_count == 0:
            raise HTTPException(status_code=404, detail="Cluster not found")

        # Fetch all faces in this cluster with their files
        faces_in_cluster = [
            dict(r)
            for r in conn.execute(
                """
                SELECT fl.file_id, fl.face_index, f.path
                FROM face_labels fl
                JOIN files f ON f.id = fl.file_id
                WHERE fl.cluster_id = ?
                ORDER BY f.path
                """,
                (cluster_id,),
            ).fetchall()
        ]

    ctx = {
        "cluster_id": cluster_id,
        "face_count": cluster_count,
        "faces": faces_in_cluster,
    }
    return templates.TemplateResponse(request=request, name="cluster_detail.html", context=ctx)


@app.post("/people/cluster/{cluster_id}/label", response_class=RedirectResponse)
async def label_cluster(
    cluster_id: str,
    name: str = Form(""),
) -> RedirectResponse:
    """Label all faces in an unknown group as a named person."""
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found")

    now = _utc_now_iso()
    with _get_conn() as conn:
        # Check cluster exists
        cluster_count = conn.execute(
            "SELECT COUNT(*) FROM face_labels WHERE cluster_id = ?",
            (cluster_id,),
        ).fetchone()[0]

        if cluster_count == 0:
            raise HTTPException(status_code=404, detail="Cluster not found")

        person_id = _find_or_create_person_by_name(conn, name, now)
        if not person_id:
            raise HTTPException(status_code=400, detail="Person name cannot be empty")

        def _do_update():
            conn.execute(
                """
                UPDATE face_labels
                SET person_id = ?, source = 'user', confidence = NULL, labeled_at = ?, cluster_id = NULL
                WHERE cluster_id = ?
                """,
                (person_id, now, cluster_id),
            )
            conn.commit()

        _with_retry(_do_update)

    return RedirectResponse(url=f"/people/{person_id}", status_code=303)
