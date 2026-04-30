"""CLI commands for Dropbox indexing."""

import pathlib
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

import typer
from rich.console import Console

from magpie.db import DB_PATH, get_connection
from magpie.dropbox_auth import get_dropbox_client
from magpie.dropbox_indexer import run_dropbox_index, run_dropbox_preview, run_dropbox_thumbs

console = Console()

dropbox_app = typer.Typer(
    name="dropbox",
    help="Index and manage files from Dropbox.",
    no_args_is_help=True,
)


@dropbox_app.command()
def add(
    label: Optional[str] = typer.Option(
        None,
        "--label",
        "-l",
        help="Human-readable label for this Dropbox account (default: 'My Dropbox').",
    ),
) -> None:
    """Register a Dropbox account and authorize indexing.

    Runs an OAuth2 browser flow. Credentials are cached at ~/.magpie/dropbox_token.json.
    """
    if label is None:
        label = "My Dropbox"

    console.log("[blue]Authenticating with Dropbox...[/blue]")

    try:
        dbx = get_dropbox_client()
    except FileNotFoundError as e:
        console.log(f"[red]{e}[/red]")
        raise typer.Exit(1)

    # Fetch the account info (which we'll use as the drive serial)
    try:
        account_info = dbx.users_get_current_account()
        account_id = account_info.account_id
        email = account_info.email
        console.log(f"[green]✓ Authenticated as {email}[/green]")
    except Exception as e:
        console.log(f"[red]Failed to authenticate: {e}[/red]")
        raise typer.Exit(1)

    # Register the drive in the database (or update if it already exists)
    with get_connection(DB_PATH) as conn:
        now = datetime.now(timezone.utc).isoformat()

        # Check if a drive with this label already exists
        existing = conn.execute(
            "SELECT id FROM drives WHERE label = ? AND source = 'dropbox'",
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
                (label, account_id, now, drive_id),
            )
            console.log(f"[green]✓ Updated Dropbox: {label} ({drive_id})[/green]")
        else:
            # Create new drive
            drive_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO drives (id, label, volume_name, source, serial, last_seen_at)
                VALUES (?, ?, ?, 'dropbox', ?, ?)
                """,
                (drive_id, label, label, account_id, now),
            )
            console.log(f"[green]✓ Registered Dropbox: {label} ({drive_id})[/green]")

        conn.commit()

    console.log(f"[blue]Next, run: magpie dropbox index {drive_id}[/blue]")


@dropbox_app.command()
def index(
    drive_id: Optional[str] = typer.Argument(
        None,
        help="Drive ID (UUID) or label. Use 'magpie drives list' to see registered drives.",
    ),
    db: pathlib.Path = typer.Option(DB_PATH, help="Path to the magpie database."),
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
    local_dropbox_path: Optional[pathlib.Path] = typer.Option(
        None,
        "--local-dropbox-path",
        help="Path to local Dropbox folder (e.g., ~/Dropbox). Files will be read locally instead of downloading via API.",
    ),
    skip_missing_local: bool = typer.Option(
        False,
        "--skip-missing-local",
        help="Only index files that exist in the local Dropbox folder. Requires --local-dropbox-path.",
    ),
    local_only: bool = typer.Option(
        False,
        "--local-only",
        help="Walk the local Dropbox folder directly — no API calls. Requires --local-dropbox-path.",
    ),
) -> None:
    """Index files from a registered Dropbox.

    Safe to re-run — files with matching path, size, and modification time are skipped.
    """
    if not drive_id:
        console.log("[red]Error: drive_id is required[/red]")
        raise typer.Exit(1)

    # Resolve drive_id (could be UUID or label)
    with get_connection(db) as conn:
        # Try UUID first
        row = conn.execute(
            "SELECT id FROM drives WHERE id = ? AND source = 'dropbox'", (drive_id,)
        ).fetchone()

        if not row:
            # Try by label
            row = conn.execute(
                "SELECT id FROM drives WHERE label = ? AND source = 'dropbox'", (drive_id,)
            ).fetchone()

        if not row:
            console.log(
                f"[red]Dropbox not found: {drive_id}[/red]\n"
                "[blue]Use 'magpie drives list' to see registered drives.[/blue]"
            )
            raise typer.Exit(1)

        actual_drive_id = row[0]

    if local_only and not local_dropbox_path:
        console.log("[red]--local-only requires --local-dropbox-path[/red]")
        raise typer.Exit(1)

    # Run the indexer
    try:
        if local_only:
            from magpie.dropbox_indexer import run_dropbox_index_local
            run_dropbox_index_local(
                actual_drive_id,
                db,
                local_dropbox_root=local_dropbox_path,
                limit=limit,
                dry_run=dry_run,
                prune_missing=prune_missing,
            )
        else:
            run_dropbox_index(
                actual_drive_id,
                db,
                limit=limit,
                dry_run=dry_run,
                no_prescan=no_prescan,
                commit_every=commit_every,
                prune_missing=prune_missing,
                with_thumbs=not no_thumbs,
                local_dropbox_root=local_dropbox_path,
                skip_missing_local=skip_missing_local,
            )
    except Exception as e:
        console.log(f"[red]Indexing failed: {e}[/red]")
        raise typer.Exit(1)


@dropbox_app.command()
def preview(
    drive_id: Optional[str] = typer.Argument(
        None,
        help="Drive ID (UUID) or label. Use 'magpie drives list' to see registered drives.",
    ),
    db: pathlib.Path = typer.Option(DB_PATH, help="Path to the magpie database."),
    limit: Optional[int] = typer.Option(
        None,
        "--limit",
        "-n",
        help="Maximum number of files to preview (for testing).",
    ),
) -> None:
    """Build a lightweight preview of files in Dropbox.

    Fetches file metadata without downloading or hashing. Populates the dropbox_preview
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
            "SELECT id FROM drives WHERE id = ? AND source = 'dropbox'", (drive_id,)
        ).fetchone()

        if not row:
            # Try by label
            row = conn.execute(
                "SELECT id FROM drives WHERE label = ? AND source = 'dropbox'", (drive_id,)
            ).fetchone()

        if not row:
            console.log(
                f"[red]Dropbox not found: {drive_id}[/red]\n"
                "[blue]Use 'magpie drives list' to see registered drives.[/blue]"
            )
            raise typer.Exit(1)

        actual_drive_id = row[0]

    # Run the preview
    try:
        run_dropbox_preview(
            actual_drive_id,
            db,
            limit=limit,
        )
    except Exception as e:
        console.log(f"[red]Preview failed: {e}[/red]")
        raise typer.Exit(1)


@dropbox_app.command()
def thumbs(
    drive_id: Optional[str] = typer.Argument(
        None,
        help="Drive ID (UUID) or label. Use 'magpie drives list' to see registered drives.",
    ),
    db: pathlib.Path = typer.Option(DB_PATH, help="Path to the magpie database."),
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
    """Download and cache thumbnails for image files from Dropbox.

    Fetches thumbnails from Dropbox API without re-indexing.
    """
    if not drive_id:
        console.log("[red]Error: drive_id is required[/red]")
        raise typer.Exit(1)

    # Resolve drive_id (could be UUID or label)
    with get_connection(db) as conn:
        # Try UUID first
        row = conn.execute(
            "SELECT id FROM drives WHERE id = ? AND source = 'dropbox'", (drive_id,)
        ).fetchone()

        if not row:
            # Try by label
            row = conn.execute(
                "SELECT id FROM drives WHERE label = ? AND source = 'dropbox'", (drive_id,)
            ).fetchone()

        if not row:
            console.log(
                f"[red]Dropbox not found: {drive_id}[/red]\n"
                "[blue]Use 'magpie drives list' to see registered drives.[/blue]"
            )
            raise typer.Exit(1)

        actual_drive_id = row[0]

    # Run thumbnail download
    try:
        run_dropbox_thumbs(
            actual_drive_id,
            db,
            force=force,
            limit=limit,
        )
    except Exception as e:
        console.log(f"[red]Thumbnail download failed: {e}[/red]")
        raise typer.Exit(1)


@dropbox_app.command(name="normalize-paths")
def normalize_paths(
    db: pathlib.Path = typer.Option(DB_PATH, "--db", help="Path to the database."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change without modifying the database."),
) -> None:
    """Normalize Dropbox file paths to lowercase.

    Fixes duplicate folder entries caused by casing inconsistencies between the
    API indexer (which used path_display) and the local indexer (which used filesystem paths).
    """
    with get_connection(db) as conn:
        rows = conn.execute(
            "SELECT id, path FROM files WHERE path LIKE '/dropbox/%'"
        ).fetchall()

    to_update: list[tuple[str, str, str]] = []
    for file_id, path in rows:
        # Normalize: lowercase everything after "/dropbox/<label>"
        parts = path.split("/", 3)  # ['', 'dropbox', '<label>', 'rest...']
        if len(parts) == 4:
            normalized = f"/dropbox/{parts[2]}/{parts[3].lower()}"
        else:
            normalized = path.lower()
        if normalized != path:
            to_update.append((normalized, file_id, path))

    if not to_update:
        console.log("[green]All Dropbox paths are already normalized.[/green]")
        return

    console.log(f"[blue]{len(to_update)} paths need normalization.[/blue]")
    if dry_run:
        for normalized, file_id, original in to_update[:20]:
            console.log(f"  {original!r} → {normalized!r}")
        if len(to_update) > 20:
            console.log(f"  ... and {len(to_update) - 20} more")
        return

    with get_connection(db) as conn:
        for normalized, file_id, _original in to_update:
            conn.execute("UPDATE files SET path = ? WHERE id = ?", (normalized, file_id))

    console.log(f"[green]Normalized {len(to_update)} Dropbox file paths.[/green]")
