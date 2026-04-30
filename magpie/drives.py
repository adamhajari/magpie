"""Drive registry — detect and register physical drives."""

from __future__ import annotations

import pathlib
import platform
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from magpie.db import DB_PATH, get_connection

console = Console()
drives_app = typer.Typer(help="Manage registered drives.")


# ---------------------------------------------------------------------------
# Drive detection helpers
# ---------------------------------------------------------------------------

def _mount_point_for(path: pathlib.Path) -> pathlib.Path:
    """Return the mount point that contains *path*."""
    path = path.resolve()
    # Walk up until we hit a mount point (different device than parent)
    import os
    current = path if path.is_dir() else path.parent
    while True:
        parent = current.parent
        if parent == current:
            return current  # filesystem root
        if os.stat(current).st_dev != os.stat(parent).st_dev:
            return current
        current = parent


def _volume_name_for(mount: pathlib.Path) -> str:
    """Best-effort volume label."""
    system = platform.system()
    if system == "Darwin":
        # On macOS, /Volumes/<name> gives us the label directly
        parts = mount.parts
        if len(parts) >= 3 and parts[1] == "Volumes":
            return parts[2]
        if mount == pathlib.Path("/"):
            return "Macintosh HD"
    # Fallback: use the final path component
    return mount.name or str(mount)


def _diskutil_info(mount: pathlib.Path) -> dict[str, str]:
    """Parse `diskutil info` output on macOS. Returns {} on other platforms."""
    if platform.system() != "Darwin":
        return {}
    try:
        result = subprocess.run(
            ["diskutil", "info", str(mount)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        info: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                info[key.strip()] = value.strip()
        return info
    except Exception:
        return {}


def detect_drive_info(path: pathlib.Path) -> dict:
    """Detect volume name, serial number, and total bytes for the drive containing *path*."""
    mount = _mount_point_for(path)
    volume_name = _volume_name_for(mount)

    usage = shutil.disk_usage(mount)
    total_bytes = usage.total

    serial: Optional[str] = None
    info = _diskutil_info(mount)
    # diskutil uses different keys depending on macOS version
    for key in ("Volume UUID", "Disk / Partition UUID", "Media UUID"):
        if key in info:
            serial = info[key]
            break
    # Also try the device identifier to get disk serial via another call
    if not serial:
        device = info.get("Device Identifier") or info.get("Device Node")
        if device:
            # strip partition suffix to get physical disk (disk2s1 -> disk2)
            import re
            physical = re.sub(r"s\d+$", "", device.replace("/dev/", ""))
            phys_info = _diskutil_info(pathlib.Path(f"/dev/{physical}"))
            serial = phys_info.get("Volume UUID") or phys_info.get("Media UUID")

    return {
        "mount": mount,
        "volume_name": volume_name,
        "serial": serial,
        "total_bytes": total_bytes,
    }


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

@drives_app.command("list")
def drives_list(
    db: pathlib.Path = typer.Option(DB_PATH, help="Path to the magpie database."),
) -> None:
    """Show all registered drives."""
    with get_connection(db) as conn:
        rows = conn.execute(
            "SELECT id, label, volume_name, serial, total_bytes, last_seen_at FROM drives ORDER BY label"
        ).fetchall()

    if not rows:
        console.print("[dim]No drives registered yet. Use [bold]magpie drives add /path[/bold] to register one.[/dim]")
        return

    table = Table(title="Registered Drives")
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Label")
    table.add_column("Volume")
    table.add_column("Serial", style="dim")
    table.add_column("Total", justify="right")
    table.add_column("Last Seen")

    for row in rows:
        total = f"{row['total_bytes'] / 1e12:.2f} TB" if row["total_bytes"] else "?"
        table.add_row(
            row["id"][:8] + "…",
            row["label"] or "",
            row["volume_name"] or "",
            (row["serial"] or "")[:16],
            total,
            row["last_seen_at"] or "",
        )

    console.print(table)


@drives_app.command("add")
def drives_add(
    path: pathlib.Path = typer.Argument(..., help="Any path on the drive to register."),
    label: Optional[str] = typer.Option(None, "--label", "-l", help="Human-readable label for this drive."),
    db: pathlib.Path = typer.Option(DB_PATH, help="Path to the magpie database."),
) -> None:
    """Register the drive that contains PATH, detecting volume name and serial."""
    if not path.exists():
        console.print(f"[red]Path does not exist:[/red] {path}")
        raise typer.Exit(1)

    info = detect_drive_info(path)
    now = datetime.now(timezone.utc).isoformat()

    with get_connection(db) as conn:
        # Check for an existing drive by serial (preferred) or volume name
        existing = None
        if info["serial"]:
            existing = conn.execute(
                "SELECT id, label FROM drives WHERE serial = ?", (info["serial"],)
            ).fetchone()
        if not existing and info["volume_name"]:
            existing = conn.execute(
                "SELECT id, label FROM drives WHERE volume_name = ?", (info["volume_name"],)
            ).fetchone()

        if existing:
            drive_id = existing["id"]
            conn.execute(
                "UPDATE drives SET last_seen_at = ?, total_bytes = ? WHERE id = ?",
                (now, info["total_bytes"], drive_id),
            )
            if label:
                conn.execute("UPDATE drives SET label = ? WHERE id = ?", (label, drive_id))
            console.print(f"[green]Drive already registered[/green] (id={drive_id[:8]}…). Updated [cyan]last_seen_at[/cyan].")
        else:
            drive_id = str(uuid.uuid4())
            effective_label = label or info["volume_name"] or "Unnamed Drive"
            conn.execute(
                """INSERT INTO drives (id, label, volume_name, serial, total_bytes, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    drive_id,
                    effective_label,
                    info["volume_name"],
                    info["serial"],
                    info["total_bytes"],
                    now,
                ),
            )
            console.print(
                f"[green]Drive registered[/green]\n"
                f"  id:          {drive_id}\n"
                f"  label:       {effective_label}\n"
                f"  volume:      {info['volume_name']}\n"
                f"  serial:      {info['serial'] or 'unknown'}\n"
                f"  total:       {info['total_bytes'] / 1e9:.1f} GB\n"
                f"  mount:       {info['mount']}"
            )
