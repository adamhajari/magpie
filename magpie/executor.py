"""Executor — acts on approved labels (cold storage upload, trash, etc.)."""

from __future__ import annotations

import pathlib
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Optional

import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from rich.progress import Progress, TaskID
from rich.console import Console

from magpie.config import ConfigError, load_aws_config

console = Console()

# Maps CLI --action values to rows in labels.action
EXECUTOR_TO_LABEL_ACTION: dict[str, str] = {
    "trash": "trash",
    "hot": "keep_hot",
    "cold": "keep_cold",
    "upload-hot": "keep_hot",
    "upload-cold": "keep_cold",
}


@dataclass
class ExecutorFile:
    """Represents a file eligible for execution."""

    file_id: str
    path: str
    size_bytes: int
    content_hash: Optional[str]
    drive_label: str
    volume_name: Optional[str]
    drive_source: str = "local"

    def _rel(self) -> str:
        """Strip volume/source prefix to get a drive-relative path."""
        if self.drive_source == "dropbox":
            # Stored path is /dropbox/{drive_label}/rest/of/path
            prefix = f"/dropbox/{self.drive_label}/"
            return self.path[len(prefix):] if self.path.startswith(prefix) else self.path.lstrip("/")
        if self.volume_name:
            prefix = f"/Volumes/{self.volume_name}"
            if self.path.startswith(prefix):
                return self.path[len(prefix):].lstrip("/")
        return self.path.lstrip("/")

    def relative_path(self) -> str:
        """Compute S3 key: {drive_label}/{rel_path}."""
        return f"{self.drive_label}/{self._rel()}"

    def gdrive_relative_path(self) -> str:
        """Compute GDrive destination path under old_storage_drives/{volume_name}/..."""
        volume = self.volume_name or self.drive_label
        return f"old_storage_drives/{volume}/{self._rel()}"

    def local_path(self, dropbox_root: Optional[pathlib.Path] = None) -> pathlib.Path:
        """Resolve the actual filesystem path for this file."""
        if self.drive_source == "dropbox":
            if dropbox_root is None:
                raise ValueError(f"dropbox_root required to resolve Dropbox file: {self.path}")
            # Stored path is /dropbox/{drive_label}/rest/of/path — strip that prefix
            prefix = f"/dropbox/{self.drive_label}/"
            rel = self.path[len(prefix):] if self.path.startswith(prefix) else self.path.lstrip("/")
            return dropbox_root / rel
        return pathlib.Path(self.path)


def normalize_execute_action(action: str) -> str | None:
    key = action.strip().lower()
    return EXECUTOR_TO_LABEL_ACTION.get(key)


def _get_drive_id_from_path(conn: sqlite3.Connection, path: pathlib.Path) -> Optional[str]:
    """Look up drive ID from a mounted path.

    For local drives, matches against volume_name (e.g., /Volumes/Seagate).
    Returns the drive ID, or None if not found.
    """
    conn.row_factory = sqlite3.Row
    path_str = str(path).rstrip("/")

    # Try exact match on volume_name
    row = conn.execute(
        """
        SELECT id FROM drives
        WHERE source = 'local' AND (
          ? = '/Volumes/' || volume_name
          OR ? LIKE '/Volumes/' || volume_name || '/%'
        )
        LIMIT 1
        """,
        (path_str, path_str),
    ).fetchone()

    return row["id"] if row else None


def _fetch_cold_upload_files(conn: sqlite3.Connection, drive_id: Optional[str] = None, source: Optional[str] = None) -> list[ExecutorFile]:
    """Fetch files eligible for cold storage upload.

    Includes files that:
    - Are labeled keep_cold and approved
    - Haven't been successfully uploaded yet (no executions row with verified=1)

    Files already in S3 are filtered out later by _upload_file_to_s3 via HeadObject check.

    Args:
        conn: SQLite connection
        drive_id: If provided, limit to this drive ID. If None, process all drives.
        source: If provided, limit to this drive source ('local' or 'dropbox').
    """
    conn.row_factory = sqlite3.Row

    query = """
        SELECT
            f.id AS file_id, f.path, f.size_bytes, f.content_hash,
            d.label AS drive_label, d.volume_name, d.source AS drive_source
        FROM labels lbl
        JOIN files f ON f.id = lbl.file_id
        JOIN drives d ON d.id = f.drive_id
        WHERE lbl.action = 'keep_cold'
          AND lbl.approved_at IS NOT NULL
          AND f.status = 'present'
          AND d.source IN ('local', 'dropbox')
          AND NOT EXISTS (
            SELECT 1 FROM executions e
            WHERE e.file_id = f.id AND e.action = 'upload_cold' AND e.verified = 1
          )
    """

    params = []
    if drive_id:
        query += "  AND d.id = ?\n"
        params.append(drive_id)
    if source:
        query += "  AND d.source = ?\n"
        params.append(source)

    query += "ORDER BY f.size_bytes DESC"

    rows = conn.execute(query, params).fetchall()

    return [ExecutorFile(**dict(row)) for row in rows]


def _upload_file_to_s3(
    s3_client,
    file_path: pathlib.Path,
    bucket: str,
    s3_key: str,
    file_size: int,
) -> bool:
    """Upload a single file to S3 Glacier Deep Archive.

    Returns True on success, False on failure.
    Prints errors but continues (no exception raised).
    """
    try:
        if not file_path.exists():
            console.print(f"[yellow]File not found (skipped): {file_path}[/yellow]")
            return False

        # Check if key already exists in S3
        try:
            s3_client.head_object(Bucket=bucket, Key=s3_key)
            console.print(f"[cyan]Already in S3 (skipped): {s3_key}[/cyan]")
            return True
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code not in ("404", "403"):
                raise
            # 403 means IAM lacks HeadObject permission — fall through and attempt upload

        # Upload file
        if file_size >= 100 * 1024 * 1024:  # 100 MB threshold for multipart
            _multipart_upload(s3_client, file_path, bucket, s3_key)
        else:
            with open(file_path, "rb") as f:
                s3_client.put_object(
                    Bucket=bucket,
                    Key=s3_key,
                    Body=f,
                    StorageClass="DEEP_ARCHIVE",
                )

        return True

    except (BotoCoreError, ClientError) as e:
        console.print(f"[red]Upload failed ({s3_key}): {e}[/red]")
        return False
    except Exception as e:
        console.print(f"[red]Unexpected error ({s3_key}): {e}[/red]")
        return False


def _multipart_upload(s3_client, file_path: pathlib.Path, bucket: str, s3_key: str) -> None:
    """Multipart upload for large files."""
    chunk_size = 100 * 1024 * 1024  # 100 MB chunks

    mp = s3_client.create_multipart_upload(
        Bucket=bucket,
        Key=s3_key,
        StorageClass="DEEP_ARCHIVE",
    )
    mp_id = mp["UploadId"]
    part_num = 0
    parts = []

    try:
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break

                part_num += 1
                resp = s3_client.upload_part(
                    Bucket=bucket,
                    Key=s3_key,
                    UploadId=mp_id,
                    PartNumber=part_num,
                    Body=chunk,
                )
                parts.append(
                    {"ETag": resp["ETag"], "PartNumber": part_num}
                )

        s3_client.complete_multipart_upload(
            Bucket=bucket,
            Key=s3_key,
            UploadId=mp_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception:
        s3_client.abort_multipart_upload(
            Bucket=bucket,
            Key=s3_key,
            UploadId=mp_id,
        )
        raise


def _record_execution(
    conn: sqlite3.Connection,
    file_id: str,
    s3_key: str,
    bucket: str,
) -> None:
    """Record a successful upload in the executions table and mark file as archived."""
    execution_id = str(uuid.uuid4())
    destination = f"s3://{bucket}/{s3_key}"

    conn.execute(
        """
        INSERT INTO executions (id, file_id, action, destination, executed_at, verified)
        VALUES (?, ?, 'upload_cold', ?, datetime('now'), 1)
        """,
        (execution_id, file_id, destination),
    )

    conn.execute(
        "UPDATE files SET status = 'archived' WHERE id = ?",
        (file_id,),
    )

    conn.commit()


def _fetch_hot_upload_files(conn: sqlite3.Connection, drive_id: Optional[str] = None, source: Optional[str] = None) -> list[ExecutorFile]:
    """Fetch files eligible for Google Drive hot upload."""
    conn.row_factory = sqlite3.Row
    query = """
        SELECT
            f.id AS file_id, f.path, f.size_bytes, f.content_hash,
            d.label AS drive_label, d.volume_name, d.source AS drive_source
        FROM labels lbl
        JOIN files f ON f.id = lbl.file_id
        JOIN drives d ON d.id = f.drive_id
        WHERE lbl.action = 'keep_hot'
          AND lbl.approved_at IS NOT NULL
          AND f.status = 'present'
          AND d.source IN ('local', 'dropbox')
          AND NOT EXISTS (
            SELECT 1 FROM executions e
            WHERE e.file_id = f.id AND e.action = 'upload_hot' AND e.verified = 1
          )
    """
    params = []
    if drive_id:
        query += "  AND d.id = ?\n"
        params.append(drive_id)
    if source:
        query += "  AND d.source = ?\n"
        params.append(source)
    query += "ORDER BY f.size_bytes DESC"
    rows = conn.execute(query, params).fetchall()
    return [ExecutorFile(**dict(row)) for row in rows]


def _ensure_gdrive_folder(
    service,
    parent_id: str,
    name: str,
    cache: dict[tuple[str, str], str],
) -> str:
    """Return the Google Drive folder ID for name under parent_id, creating it if needed."""
    name = name.replace("\xa0", " ")
    key = (parent_id, name)
    if key in cache:
        return cache[key]

    # Search for existing folder
    q = (
        f"name = {name!r} and mimeType = 'application/vnd.google-apps.folder'"
        f" and '{parent_id}' in parents and trashed = false"
    )
    results = service.files().list(q=q, fields="files(id)", spaces="drive").execute()
    items = results.get("files", [])
    if items:
        folder_id = items[0]["id"]
    else:
        meta = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = service.files().create(body=meta, fields="id").execute()
        folder_id = folder["id"]

    cache[key] = folder_id
    return folder_id


def _resolve_gdrive_folder_chain(
    service,
    root_id: str,
    path_segments: list[str],
    cache: dict[tuple[str, str], str],
) -> str:
    """Walk/create each folder segment under root_id and return the leaf folder ID."""
    parent_id = root_id
    for segment in path_segments:
        parent_id = _ensure_gdrive_folder(service, parent_id, segment, cache)
    return parent_id


_GDRIVE_RETRYABLE_CODES = {408, 429, 500, 502, 503, 504}


def _gdrive_execute_with_retry(request, label: str, max_attempts: int = 5):
    """Execute a Google API request with exponential backoff on transient errors."""
    from googleapiclient.errors import HttpError

    delay = 2.0
    for attempt in range(max_attempts):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status in _GDRIVE_RETRYABLE_CODES and attempt < max_attempts - 1:
                console.print(f"[yellow]GDrive {e.resp.status} on {label}, retrying in {delay:.0f}s…[/yellow]")
                time.sleep(delay)
                delay = min(delay * 2, 60)
            else:
                raise
    raise RuntimeError("unreachable")


def _upload_file_to_gdrive(
    service,
    file_path: pathlib.Path,
    parent_folder_id: str,
    filename: str,
    gdrive_display_path: str,
) -> Optional[str]:
    """Upload a file to Google Drive. Returns the GDrive file ID, or None on skip/failure."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaIoBaseUpload

    # Normalize non-breaking spaces and other non-ASCII whitespace to regular spaces
    filename = filename.replace("\xa0", " ")

    # Check for existing file with same name in the same folder
    q = (
        f"name = {filename!r} and '{parent_folder_id}' in parents and trashed = false"
        " and mimeType != 'application/vnd.google-apps.folder'"
    )
    existing = _gdrive_execute_with_retry(
        service.files().list(q=q, fields="files(id)", spaces="drive"),
        label=gdrive_display_path,
    )
    if existing.get("files"):
        console.print(f"[yellow]Already exists in GDrive (skipped): {gdrive_display_path}[/yellow]")
        return None

    if not file_path.exists():
        console.print(f"[yellow]File not found on disk (skipped): {file_path}[/yellow]")
        return None

    import mimetypes
    mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"

    delay = 2.0
    for attempt in range(5):
        try:
            with open(file_path, "rb") as fh:
                media = MediaIoBaseUpload(fh, mimetype=mime_type, chunksize=10 * 1024 * 1024, resumable=True)
                meta = {"name": filename, "parents": [parent_folder_id]}
                request = service.files().create(body=meta, media_body=media, fields="id")
                response = None
                while response is None:
                    _, response = request.next_chunk()
            return response["id"]
        except HttpError as e:
            if e.resp.status in _GDRIVE_RETRYABLE_CODES and attempt < 4:
                console.print(f"[yellow]Upload {e.resp.status} ({gdrive_display_path}), retrying in {delay:.0f}s…[/yellow]")
                time.sleep(delay)
                delay = min(delay * 2, 60)
            else:
                console.print(f"[red]Upload failed ({gdrive_display_path}): {e}[/red]")
                return None
        except OSError as e:
            if e.errno == 32 and attempt < 4:  # Broken pipe
                console.print(f"[yellow]Upload broken pipe ({gdrive_display_path}), retrying in {delay:.0f}s…[/yellow]")
                time.sleep(delay)
                delay = min(delay * 2, 60)
            else:
                console.print(f"[red]Upload failed ({gdrive_display_path}): {e}[/red]")
                return None
        except Exception as e:
            console.print(f"[red]Upload failed ({gdrive_display_path}): {e}[/red]")
            return None
    return None


def _record_hot_execution(
    conn: sqlite3.Connection,
    file_id: str,
    gdrive_file_id: str,
    gdrive_path: str,
) -> None:
    """Record a successful hot upload and mark file as archived."""
    execution_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO executions (id, file_id, action, destination, executed_at, verified)
        VALUES (?, ?, 'upload_hot', ?, datetime('now'), 1)
        """,
        (execution_id, file_id, f"gdrive://{gdrive_path}"),
    )
    conn.execute("UPDATE files SET status = 'archived' WHERE id = ?", (file_id,))
    conn.commit()


def _run_hot_upload(
    *,
    conn: sqlite3.Connection,
    drive_id: Optional[str],
    path: Optional[pathlib.Path],
    dry_run: bool,
    line_print,
    dropbox_root: Optional[pathlib.Path] = None,
    source: Optional[str] = None,
) -> int:
    """Upload keep_hot approved files from one local or Dropbox drive to Google Drive."""
    from googleapiclient.discovery import build
    from magpie.gdrive_auth import get_credentials

    files = _fetch_hot_upload_files(conn, drive_id=drive_id, source=source)
    total_bytes = sum(f.size_bytes or 0 for f in files)

    line_print(
        f"[execute] action='hot' approved_files={len(files)} "
        f"total_bytes={total_bytes} dry_run={dry_run} path={path}"
    )

    if not files:
        line_print("[execute] No files to upload.")
        return 0

    if dry_run:
        line_print("[execute] Dry-run: planned GDrive uploads:")
        for f in files:
            size_mb = (f.size_bytes or 0) / (1024 * 1024)
            line_print(f"  {f.gdrive_relative_path()} ({size_mb:.1f} MB)")
        return 0

    creds = get_credentials()
    service = build("drive", "v3", credentials=creds)

    # Resolve My Drive root
    root = service.files().get(fileId="root", fields="id").execute()
    root_id = root["id"]

    folder_cache: dict[tuple[str, str], str] = {}
    uploaded = 0
    skipped = 0
    failed = 0

    from rich.progress import BarColumn, TimeRemainingColumn, TextColumn, DownloadColumn, TransferSpeedColumn

    class FilenameColumn(TextColumn):
        def __init__(self):
            super().__init__("{task.fields[filename]}", justify="right")

    with Progress(
        TextColumn("[cyan]Uploading"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        FilenameColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("", total=total_bytes, filename="")

        for f in files:
            gdrive_path = f.gdrive_relative_path()
            segments = gdrive_path.split("/")
            # segments[-1] is the filename; everything before is the folder chain
            folder_segments = segments[:-1]
            filename = segments[-1]

            progress.update(task, filename=f.path)

            try:
                if f.drive_source == "dropbox" and dropbox_root is None:
                    console.print(f"[yellow]Skipping Dropbox file (no --dropbox-path): {f.path}[/yellow]")
                    skipped += 1
                    progress.update(task, advance=f.size_bytes or 0)
                    continue
                file_path = f.local_path(dropbox_root)
                parent_id = _resolve_gdrive_folder_chain(service, root_id, folder_segments, folder_cache)
                gdrive_file_id = _upload_file_to_gdrive(
                    service,
                    file_path,
                    parent_id,
                    filename,
                    gdrive_path,
                )
            except Exception as e:
                console.print(f"[red]Error ({f.path}): {e}[/red]")
                failed += 1
                progress.update(task, advance=f.size_bytes or 0)
                continue

            if gdrive_file_id is None:
                skipped += 1
            else:
                try:
                    _record_hot_execution(conn, f.file_id, gdrive_file_id, gdrive_path)
                except Exception as e:
                    console.print(f"[yellow]Warning: file uploaded but DB record failed: {e}[/yellow]")
                uploaded += 1

            progress.update(task, advance=f.size_bytes or 0)

    line_print(f"\n[execute] Completed: {uploaded} uploaded, {skipped} skipped (conflict), {failed} failed")
    return 0


def run_execute(
    *,
    db_path: pathlib.Path,
    cli_action: str,
    path: Optional[pathlib.Path] = None,
    dry_run: bool,
    line_print,
    dropbox_root: Optional[pathlib.Path] = None,
    source: Optional[str] = None,
) -> int:
    """Execute approved labels. Return process exit code.

    Args:
        db_path: Path to SQLite database
        cli_action: Action to execute (trash, hot, cold, etc.)
        path: Optional mount path (e.g., /Volumes/Seagate). If provided, limits to that drive.
        dry_run: If True, report work without actually executing
        line_print: Callable to output lines
    """
    label_action = normalize_execute_action(cli_action)
    if not label_action:
        line_print(f"Unknown --action {cli_action!r}. Use: trash, hot, cold (or upload-hot, upload-cold).")
        return 1

    if not db_path.exists():
        line_print(f"Database not found: {db_path}")
        return 1

    # Hot upload to Google Drive
    if label_action == "keep_hot":
        if not path and not dropbox_root:
            line_print("[error] --path /Volumes/... is required for --action hot (or --dropbox-path for Dropbox-only)")
            return 1

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            drive_id = None
            if path:
                drive_id = _get_drive_id_from_path(conn, path)
                if not drive_id:
                    line_print(f"[error] Drive not found for path: {path}")
                    return 1
            return _run_hot_upload(conn=conn, drive_id=drive_id, path=path, dry_run=dry_run, line_print=line_print, dropbox_root=dropbox_root, source=source)
        finally:
            conn.close()

    # Unimplemented actions
    if label_action != "keep_cold":
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n, COALESCE(SUM(f.size_bytes), 0) AS total_bytes
                FROM labels lbl
                JOIN files f ON f.id = lbl.file_id
                WHERE lbl.approved_at IS NOT NULL AND lbl.action = ?
                """,
                (label_action,),
            ).fetchone()
            n = row["n"] or 0
            total_b = row["total_bytes"] or 0
            line_print(
                f"[execute] action={cli_action!r} label_filter={label_action!r} "
                f"approved_files={n} total_bytes={total_b} dry_run={dry_run}"
            )
            line_print("[execute] This action type is not yet implemented.")
            return 0
        finally:
            conn.close()

    # Cold storage upload
    try:
        config = load_aws_config()
    except ConfigError as e:
        line_print(f"[error] {e}")
        return 1

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        # Look up drive ID from path if provided
        drive_id = None
        if path:
            drive_id = _get_drive_id_from_path(conn, path)
            if not drive_id:
                line_print(f"[error] Drive not found for path: {path}")
                return 1

        files = _fetch_cold_upload_files(conn, drive_id=drive_id, source=source)
        total_bytes = sum(f.size_bytes for f in files)

        drive_filter = f" path={path}" if path else ""
        line_print(
            f"[execute] action={cli_action!r} approved_files={len(files)} "
            f"total_bytes={total_bytes} dry_run={dry_run} bucket={config.bucket}{drive_filter}"
        )

        if not files:
            line_print("[execute] No files to upload.")
            return 0

        if dry_run:
            line_print("[execute] Dry-run: planned uploads (no S3 calls):")
            for f in files:
                size_mb = f.size_bytes / (1024 * 1024)
                line_print(f"  {f.relative_path()} ({size_mb:.1f} MB)")
            return 0

        # Real upload
        s3_client = boto3.Session(profile_name=config.profile).client("s3", region_name=config.region)
        uploaded = 0
        skipped = 0

        from rich.progress import BarColumn, TimeRemainingColumn, TextColumn, DownloadColumn, TransferSpeedColumn

        class FilenameColumn(TextColumn):
            """Display filename on the right side."""
            def __init__(self):
                super().__init__("{task.fields[filename]}", justify="right")

        with Progress(
            TextColumn("[cyan]Uploading"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            FilenameColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(
                "",
                total=total_bytes,
                filename="",
            )

            for f in files:
                if f.drive_source == "dropbox" and dropbox_root is None:
                    console.print(f"[yellow]Skipping Dropbox file (no --dropbox-path): {f.path}[/yellow]")
                    skipped += 1
                    progress.update(task, advance=f.size_bytes)
                    continue
                file_path = f.local_path(dropbox_root)
                s3_key = f.relative_path()

                # Update filename for display on the right
                progress.update(task, filename=f.path)

                if _upload_file_to_s3(s3_client, file_path, config.bucket, s3_key, f.size_bytes):
                    # File is now in S3. Try to record in DB, but don't fail if insert fails —
                    # next run will see it in S3 and skip re-upload.
                    try:
                        _record_execution(conn, f.file_id, s3_key, config.bucket)
                    except Exception as e:
                        console.print(f"[yellow]Warning: file in S3 but DB record failed: {e}[/yellow]")
                    uploaded += 1
                else:
                    skipped += 1

                progress.update(task, advance=f.size_bytes)

        line_print(f"\n[execute] Completed: {uploaded} uploaded, {skipped} skipped/failed")
        return 0

    except Exception as e:
        line_print(f"[error] {e}")
        return 1
    finally:
        conn.close()
