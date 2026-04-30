"""CLI commands for Google Drive indexing."""

import pathlib
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

import typer
from rich.console import Console

from magpie.db import DB_PATH, get_connection
from magpie.gdrive_auth import get_credentials
from magpie.gdrive_indexer import run_gdrive_index, run_gdrive_preview, run_gdrive_thumbs, run_gdrive_recompute_paths

console = Console()

gdrive_app = typer.Typer(
    name="gdrive",
    help="Index and manage files from Google Drive.",
    no_args_is_help=True,
)


@gdrive_app.command()
def add(
    label: Optional[str] = typer.Option(
        None,
        "--label",
        "-l",
        help="Human-readable label for this Google Drive (default: 'My Drive').",
    ),
    credentials_path: Optional[pathlib.Path] = typer.Option(
        None,
        "--credentials",
        help="Path to Google OAuth credentials JSON (default: ~/.magpie/gdrive_client_secret.json).",
    ),
) -> None:
    """Register a Google Drive and authorize indexing.

    Runs an OAuth2 browser flow. Credentials are cached at ~/.magpie/gdrive_token.json.
    """
    if label is None:
        label = "My Drive"

    console.log("[blue]Authenticating with Google Drive...[/blue]")

    try:
        creds = get_credentials(credentials_path=credentials_path)
    except FileNotFoundError as e:
        console.log(f"[red]{e}[/red]")
        raise typer.Exit(1)

    # Fetch the root folder ID (which we'll use as the drive serial)
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        service = build("drive", "v3", credentials=creds)
        # The "root" ID in Google Drive is always the My Drive root
        root_id = "root"
        console.log(f"[green]✓ Authenticated as {creds.client_id}[/green]")
    except Exception as e:
        console.log(f"[red]Failed to authenticate: {e}[/red]")
        raise typer.Exit(1)

    # Register the drive in the database (or update if it already exists)
    with get_connection(DB_PATH) as conn:
        now = datetime.now(timezone.utc).isoformat()

        # Check if a drive with this label already exists
        existing = conn.execute(
            "SELECT id FROM drives WHERE label = ? AND source = 'gdrive'",
            (label,),
        ).fetchone()

        if existing:
            # Update existing drive
            drive_id = existing[0]
            conn.execute(
                """
                UPDATE drives
                SET volume_name = ?, serial = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (label, root_id, now, drive_id),
            )
            console.log(f"[green]✓ Updated Google Drive: {label} ({drive_id})[/green]")
        else:
            # Create new drive
            drive_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO drives (id, label, volume_name, source, serial, last_seen_at)
                VALUES (?, ?, ?, 'gdrive', ?, ?)
                """,
                (drive_id, label, label, root_id, now),
            )
            console.log(f"[green]✓ Registered Google Drive: {label} ({drive_id})[/green]")

        conn.commit()

    console.log(f"[blue]Next, run: magpie gdrive index {drive_id}[/blue]")


@gdrive_app.command()
def index(
    drive_id: Optional[str] = typer.Argument(
        None,
        help="Drive ID (UUID) or label. Use 'magpie drives list' to see registered drives.",
    ),
    db: pathlib.Path = typer.Option(DB_PATH, help="Path to the magpie database."),
    credentials_path: Optional[pathlib.Path] = typer.Option(
        None,
        "--credentials",
        help="Path to Google OAuth credentials JSON.",
    ),
    limit: Optional[int] = typer.Option(
        None,
        "--limit",
        "-n",
        help="Maximum number of files to index (for testing).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview what would be indexed without writing to the database.",
    ),
    no_prescan: bool = typer.Option(
        False,
        "--no-prescan",
        help="Skip counting total files before indexing (faster start, progress shows ? for totals).",
    ),
    commit_every: int = typer.Option(
        50,
        "--commit-every",
        min=0,
        help="Commit to SQLite after this many files. 0 = only commit at end.",
    ),
    prune_missing: bool = typer.Option(
        False,
        "--prune-missing",
        help="Delete records for files not found in this scan from the database.",
    ),
    no_thumbs: bool = typer.Option(
        False,
        "--no-thumbs",
        help="Skip downloading thumbnails for image files.",
    ),
) -> None:
    """Index files from a registered Google Drive.

    Safe to re-run — files with matching path, size, and modification time are skipped.
    """
    if not drive_id:
        console.log("[red]Error: drive_id is required[/red]")
        raise typer.Exit(1)

    # Resolve drive_id (could be UUID or label)
    with get_connection(db) as conn:
        # Try UUID first
        row = conn.execute(
            "SELECT id FROM drives WHERE id = ? AND source = 'gdrive'", (drive_id,)
        ).fetchone()

        if not row:
            # Try by label
            row = conn.execute(
                "SELECT id FROM drives WHERE label = ? AND source = 'gdrive'", (drive_id,)
            ).fetchone()

        if not row:
            console.log(
                f"[red]Google Drive not found: {drive_id}[/red]\n"
                "[blue]Use 'magpie drives list' to see registered drives.[/blue]"
            )
            raise typer.Exit(1)

        actual_drive_id = row[0]

    # Run the indexer
    try:
        run_gdrive_index(
            actual_drive_id,
            db,
            credentials_path=credentials_path,
            limit=limit,
            dry_run=dry_run,
            no_prescan=no_prescan,
            commit_every=commit_every,
            prune_missing=prune_missing,
            with_thumbs=not no_thumbs,
        )
    except Exception as e:
        console.log(f"[red]Indexing failed: {e}[/red]")
        raise typer.Exit(1)


@gdrive_app.command()
def preview(
    drive_id: Optional[str] = typer.Argument(
        None,
        help="Drive ID (UUID) or label. Use 'magpie drives list' to see registered drives.",
    ),
    db: pathlib.Path = typer.Option(DB_PATH, help="Path to the magpie database."),
    credentials_path: Optional[pathlib.Path] = typer.Option(
        None,
        "--credentials",
        help="Path to Google OAuth credentials JSON.",
    ),
    limit: Optional[int] = typer.Option(
        None,
        "--limit",
        "-n",
        help="Maximum number of files to preview (for testing).",
    ),
) -> None:
    """Build a lightweight preview of files in Google Drive.

    Fetches file metadata without downloading or hashing. Populates the gdrive_preview
    table for analysis before running the full indexer.

    Use this to understand file distribution, identify cleanup opportunities, etc.
    """
    if not drive_id:
        console.log("[red]Error: drive_id is required[/red]")
        raise typer.Exit(1)

    # Resolve drive_id (could be UUID or label)
    with get_connection(db) as conn:
        # Try UUID first
        row = conn.execute(
            "SELECT id FROM drives WHERE id = ? AND source = 'gdrive'", (drive_id,)
        ).fetchone()

        if not row:
            # Try by label
            row = conn.execute(
                "SELECT id FROM drives WHERE label = ? AND source = 'gdrive'", (drive_id,)
            ).fetchone()

        if not row:
            console.log(
                f"[red]Google Drive not found: {drive_id}[/red]\n"
                "[blue]Use 'magpie drives list' to see registered drives.[/blue]"
            )
            raise typer.Exit(1)

        actual_drive_id = row[0]

    # Run the preview
    try:
        run_gdrive_preview(
            actual_drive_id,
            db,
            credentials_path=credentials_path,
            limit=limit,
        )
    except Exception as e:
        console.log(f"[red]Preview failed: {e}[/red]")
        raise typer.Exit(1)


@gdrive_app.command()
def thumbs(
    drive_id: Optional[str] = typer.Argument(
        None,
        help="Drive ID (UUID) or label. Use 'magpie drives list' to see registered drives.",
    ),
    db: pathlib.Path = typer.Option(DB_PATH, help="Path to the magpie database."),
    credentials_path: Optional[pathlib.Path] = typer.Option(
        None,
        "--credentials",
        help="Path to Google OAuth credentials JSON.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Regenerate thumbnails even if already cached.",
    ),
    limit: Optional[int] = typer.Option(
        None,
        "--limit",
        help="Process only first N files (for testing).",
    ),
) -> None:
    """Download and cache thumbnails for image files from a Google Drive.

    Fetches thumbnails from Google Drive API without re-indexing.
    """
    if not drive_id:
        console.log("[red]Error: drive_id is required[/red]")
        raise typer.Exit(1)

    # Resolve drive_id (could be UUID or label)
    with get_connection(db) as conn:
        # Try UUID first
        row = conn.execute(
            "SELECT id FROM drives WHERE id = ? AND source = 'gdrive'", (drive_id,)
        ).fetchone()

        if not row:
            # Try by label
            row = conn.execute(
                "SELECT id FROM drives WHERE label = ? AND source = 'gdrive'", (drive_id,)
            ).fetchone()

        if not row:
            console.log(
                f"[red]Google Drive not found: {drive_id}[/red]\n"
                "[blue]Use 'magpie drives list' to see registered drives.[/blue]"
            )
            raise typer.Exit(1)

        actual_drive_id = row[0]

    # Run thumbnail download
    try:
        run_gdrive_thumbs(
            actual_drive_id,
            db,
            credentials_path=credentials_path,
            force=force,
            limit=limit,
        )
    except Exception as e:
        console.log(f"[red]Thumbnail download failed: {e}[/red]")
        raise typer.Exit(1)


@gdrive_app.command()
def recompute_paths(
    drive_id: Optional[str] = typer.Argument(
        None,
        help="Drive ID (UUID) or label. Use 'magpie drives list' to see registered drives.",
    ),
    db: pathlib.Path = typer.Option(DB_PATH, help="Path to the magpie database."),
    credentials_path: Optional[pathlib.Path] = typer.Option(
        None,
        "--credentials",
        help="Path to Google OAuth credentials JSON.",
    ),
    limit: Optional[int] = typer.Option(
        None,
        "--limit",
        "-n",
        help="Maximum number of files to recompute (for testing).",
    ),
) -> None:
    """Recompute and update file paths for all files from a Google Drive.

    Does NOT re-download files or re-hash them. Useful after a fix to path reconstruction
    logic, or if folder structure has changed in Google Drive.
    """
    if not drive_id:
        console.log("[red]Error: drive_id is required[/red]")
        raise typer.Exit(1)

    # Resolve drive_id (could be UUID or label)
    with get_connection(db) as conn:
        # Try UUID first
        row = conn.execute(
            "SELECT id FROM drives WHERE id = ? AND source = 'gdrive'", (drive_id,)
        ).fetchone()

        if not row:
            # Try by label
            row = conn.execute(
                "SELECT id FROM drives WHERE label = ? AND source = 'gdrive'", (drive_id,)
            ).fetchone()

        if not row:
            console.log(
                f"[red]Google Drive not found: {drive_id}[/red]\n"
                "[blue]Use 'magpie drives list' to see registered drives.[/blue]"
            )
            raise typer.Exit(1)

        actual_drive_id = row[0]

    # Run path recomputation
    try:
        run_gdrive_recompute_paths(
            actual_drive_id,
            db,
            credentials_path=credentials_path,
            limit=limit,
        )
    except Exception as e:
        console.log(f"[red]Path recomputation failed: {e}[/red]")
        raise typer.Exit(1)
