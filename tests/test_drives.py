"""Tests for the drive registry (Task 1.2)."""

import pathlib
import sqlite3
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from db.migrate import migrate
from magpie.cli import app
from magpie.db import get_connection
from magpie.drives import detect_drive_info


@pytest.fixture()
def tmp_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db_path = tmp_path / "magpie.db"
    migrate(db_path)
    return db_path


runner = CliRunner()


# ---------------------------------------------------------------------------
# Unit tests for detect_drive_info
# ---------------------------------------------------------------------------

def test_detect_drive_info_returns_expected_keys(tmp_path: pathlib.Path) -> None:
    info = detect_drive_info(tmp_path)
    assert "mount" in info
    assert "volume_name" in info
    assert "total_bytes" in info
    assert isinstance(info["total_bytes"], int)
    assert info["total_bytes"] > 0


def test_detect_drive_info_mount_is_ancestor(tmp_path: pathlib.Path) -> None:
    info = detect_drive_info(tmp_path)
    mount = info["mount"]
    # The mount point must be a prefix of tmp_path
    assert str(tmp_path).startswith(str(mount))


# ---------------------------------------------------------------------------
# CLI: drives list
# ---------------------------------------------------------------------------

def test_drives_list_empty(tmp_db: pathlib.Path) -> None:
    result = runner.invoke(app, ["drives", "list", "--db", str(tmp_db)])
    assert result.exit_code == 0
    assert "No drives registered" in result.output


def test_drives_list_shows_registered(tmp_db: pathlib.Path) -> None:
    drive_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO drives (id, label, volume_name, serial, total_bytes, last_seen_at) VALUES (?,?,?,?,?,?)",
            (drive_id, "Test Drive", "TestVol", "SN123", 1_000_000_000, now),
        )
    result = runner.invoke(app, ["drives", "list", "--db", str(tmp_db)])
    assert result.exit_code == 0
    assert "Test Drive" in result.output
    assert "TestVol" in result.output


# ---------------------------------------------------------------------------
# CLI: drives add
# ---------------------------------------------------------------------------

def test_drives_add_registers_new_drive(tmp_path: pathlib.Path, tmp_db: pathlib.Path) -> None:
    fake_info = {
        "mount": tmp_path,
        "volume_name": "FakeVol",
        "serial": "FAKE-SERIAL-001",
        "total_bytes": 500_000_000_000,
    }
    with patch("magpie.drives.detect_drive_info", return_value=fake_info):
        result = runner.invoke(
            app,
            ["drives", "add", str(tmp_path), "--label", "My Test Drive", "--db", str(tmp_db)],
        )
    assert result.exit_code == 0
    assert "registered" in result.output.lower()

    with get_connection(tmp_db) as conn:
        row = conn.execute("SELECT * FROM drives WHERE serial = ?", ("FAKE-SERIAL-001",)).fetchone()
    assert row is not None
    assert row["label"] == "My Test Drive"
    assert row["volume_name"] == "FakeVol"


def test_drives_add_updates_existing_drive(tmp_path: pathlib.Path, tmp_db: pathlib.Path) -> None:
    """Re-registering a drive with the same serial updates last_seen_at rather than inserting."""
    drive_id = str(uuid.uuid4())
    old_time = "2020-01-01T00:00:00+00:00"
    with get_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO drives (id, label, volume_name, serial, total_bytes, last_seen_at) VALUES (?,?,?,?,?,?)",
            (drive_id, "Old Label", "FakeVol", "FAKE-SERIAL-002", 100, old_time),
        )

    fake_info = {
        "mount": tmp_path,
        "volume_name": "FakeVol",
        "serial": "FAKE-SERIAL-002",
        "total_bytes": 200_000_000_000,
    }
    with patch("magpie.drives.detect_drive_info", return_value=fake_info):
        result = runner.invoke(
            app,
            ["drives", "add", str(tmp_path), "--db", str(tmp_db)],
        )
    assert result.exit_code == 0
    assert "already registered" in result.output.lower()

    with get_connection(tmp_db) as conn:
        rows = conn.execute("SELECT * FROM drives WHERE serial = ?", ("FAKE-SERIAL-002",)).fetchall()
    assert len(rows) == 1
    assert rows[0]["last_seen_at"] != old_time


def test_drives_add_invalid_path(tmp_db: pathlib.Path) -> None:
    result = runner.invoke(app, ["drives", "add", "/nonexistent/path/xyz", "--db", str(tmp_db)])
    assert result.exit_code != 0
