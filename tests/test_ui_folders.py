"""Tests for folder browser routes and path filter logic."""

from __future__ import annotations

import pathlib
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from db.migrate import migrate
from magpie.ui import app as app_module
from magpie.ui.app import _build_breadcrumbs, _fetch_folder_children, _find_db_root


@pytest.fixture()
def tmp_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db_path = tmp_path / "magpie.db"
    migrate(db_path)
    return db_path


@pytest.fixture()
def populated_db(tmp_db: pathlib.Path) -> pathlib.Path:
    """DB seeded with files in a 3-level hierarchy."""
    drive_id = str(uuid.uuid4())
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO drives (id, label, last_seen_at) VALUES (?, ?, datetime('now'))",
        (drive_id, "TestDrive"),
    )
    files = [
        # Directly in /Volumes/TD
        ("/Volumes/TD/root_file.txt", 100),
        # In /Volumes/TD/Music
        ("/Volumes/TD/Music/song1.mp3", 1000),
        ("/Volumes/TD/Music/song2.mp3", 2000),
        # In /Volumes/TD/Music/Rock
        ("/Volumes/TD/Music/Rock/track.mp3", 500),
        # In /Volumes/TD/Docs
        ("/Volumes/TD/Docs/readme.txt", 200),
        ("/Volumes/TD/Docs/report.pdf", 3000),
        # In /Volumes/TD/Docs/2024
        ("/Volumes/TD/Docs/2024/jan.pdf", 400),
    ]
    for path, size in files:
        conn.execute(
            """
            INSERT INTO files (id, path, drive_id, indexed_at, mime_type, extension, status, size_bytes)
            VALUES (?, ?, ?, datetime('now'), 'text/plain', 'txt', 'present', ?)
            """,
            (str(uuid.uuid4()), path, drive_id, size),
        )
    conn.commit()
    conn.close()
    return tmp_db


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


def test_build_breadcrumbs_empty():
    assert _build_breadcrumbs("") == []


def test_build_breadcrumbs():
    crumbs = _build_breadcrumbs("/Volumes/SP/user/Music")
    assert len(crumbs) == 4
    assert crumbs[0] == {"name": "Volumes", "path": "/Volumes"}
    assert crumbs[1] == {"name": "SP", "path": "/Volumes/SP"}
    assert crumbs[2] == {"name": "user", "path": "/Volumes/SP/user"}
    assert crumbs[3] == {"name": "Music", "path": "/Volumes/SP/user/Music"}


def test_fetch_folder_children(populated_db: pathlib.Path):
    conn = sqlite3.connect(populated_db)
    conn.row_factory = sqlite3.Row
    children = _fetch_folder_children(conn, "/Volumes/TD")
    conn.close()

    names = [c["name"] for c in children]
    assert "Music" in names
    assert "Docs" in names
    # root_file.txt should NOT appear (it's a file, not a subfolder)
    assert "root_file.txt" not in names


def test_fetch_folder_children_nested(populated_db: pathlib.Path):
    conn = sqlite3.connect(populated_db)
    conn.row_factory = sqlite3.Row
    children = _fetch_folder_children(conn, "/Volumes/TD/Music")
    conn.close()

    names = [c["name"] for c in children]
    assert "Rock" in names
    # song1.mp3 / song2.mp3 are direct files, should not appear
    assert "song1.mp3" not in names


def test_fetch_folder_children_counts(populated_db: pathlib.Path):
    conn = sqlite3.connect(populated_db)
    conn.row_factory = sqlite3.Row
    children = _fetch_folder_children(conn, "/Volumes/TD")
    conn.close()

    by_name = {c["name"]: c for c in children}
    # Music has: song1, song2 (direct) + Rock/track (sub) = 3 total
    assert by_name["Music"]["file_count"] == 3
    # Docs has: readme, report (direct) + 2024/jan (sub) = 3 total
    assert by_name["Docs"]["file_count"] == 3


def test_fetch_folder_children_excludes_trash_by_default(populated_db: pathlib.Path):
    """By default, files labeled for trash should not be counted in folder children."""
    drive_id = str(uuid.uuid4())
    conn = sqlite3.connect(populated_db)
    conn.row_factory = sqlite3.Row
    # Add a file to the Music folder and label it for trash
    file_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO files (id, path, drive_id, indexed_at, mime_type, extension, status, size_bytes)
        VALUES (?, ?, ?, datetime('now'), 'audio/mpeg', 'mp3', 'present', ?)
        """,
        (file_id, "/Volumes/TD/Music/to_delete.mp3", drive_id, 1500),
    )
    conn.execute(
        """
        INSERT INTO labels (file_id, action, source, labeled_at)
        VALUES (?, 'trash', 'user', datetime('now'))
        """,
        (file_id,),
    )
    conn.commit()

    # Fetch without include_trash (default)
    children = _fetch_folder_children(conn, "/Volumes/TD", include_trash=False)
    by_name = {c["name"]: c for c in children}
    # Music should have 3 files (not 4, because the trash-labeled one is excluded)
    assert by_name["Music"]["file_count"] == 3

    conn.close()


def test_fetch_folder_children_includes_trash_when_requested(populated_db: pathlib.Path):
    """When include_trash=True, files labeled for trash should be counted."""
    drive_id = str(uuid.uuid4())
    conn = sqlite3.connect(populated_db)
    conn.row_factory = sqlite3.Row
    # Add a file to the Music folder and label it for trash
    file_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO files (id, path, drive_id, indexed_at, mime_type, extension, status, size_bytes)
        VALUES (?, ?, ?, datetime('now'), 'audio/mpeg', 'mp3', 'present', ?)
        """,
        (file_id, "/Volumes/TD/Music/to_delete.mp3", drive_id, 1500),
    )
    conn.execute(
        """
        INSERT INTO labels (file_id, action, source, labeled_at)
        VALUES (?, 'trash', 'user', datetime('now'))
        """,
        (file_id,),
    )
    conn.commit()

    # Fetch with include_trash=True
    children = _fetch_folder_children(conn, "/Volumes/TD", include_trash=True)
    by_name = {c["name"]: c for c in children}
    # Music should have 4 files (including the trash-labeled one)
    assert by_name["Music"]["file_count"] == 4

    conn.close()


def test_find_db_root(populated_db: pathlib.Path):
    app_module._db_root_cache = None
    conn = sqlite3.connect(populated_db)
    conn.row_factory = sqlite3.Row
    root = _find_db_root(conn)
    conn.close()

    # All test files are under /Volumes/TD — root should be the parent of the
    # common prefix, i.e. /Volumes (parent of /Volumes/TD).
    assert root == "/Volumes"


def test_find_db_root_empty(tmp_db: pathlib.Path):
    app_module._db_root_cache = None
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    root = _find_db_root(conn)
    conn.close()
    assert root == ""


# ---------------------------------------------------------------------------
# Recursive path filter (via /files route)
# ---------------------------------------------------------------------------


def test_files_path_filter_recursive(
    populated_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", populated_db)
    client = TestClient(app_module.app)

    r = client.get("/files?path=/Volumes/TD/Music&recursive=true")
    assert r.status_code == 200
    content = r.text
    # Should find all 3 files under /Music (song1, song2, Rock/track)
    assert "song1.mp3" in content
    assert "song2.mp3" in content
    assert "track.mp3" in content
    # Should NOT see files outside /Music
    assert "readme.txt" not in content
    assert "root_file.txt" not in content


def test_files_path_filter_recursive_default(
    populated_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """recursive=true is the default when param is omitted."""
    monkeypatch.setattr(app_module, "DB_PATH", populated_db)
    client = TestClient(app_module.app)

    r = client.get("/files?path=/Volumes/TD/Docs")
    assert r.status_code == 200
    content = r.text
    assert "readme.txt" in content
    assert "report.pdf" in content
    assert "jan.pdf" in content
    # No music files
    assert "song1.mp3" not in content


# ---------------------------------------------------------------------------
# Non-recursive path filter
# ---------------------------------------------------------------------------


def test_files_path_filter_non_recursive(
    populated_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", populated_db)
    client = TestClient(app_module.app)

    r = client.get("/files?path=/Volumes/TD/Music&recursive=false")
    assert r.status_code == 200
    content = r.text
    # Direct files in /Music only
    assert "song1.mp3" in content
    assert "song2.mp3" in content
    # track.mp3 is in /Music/Rock — should NOT appear in the file table
    assert "track.mp3" not in content


def test_files_path_filter_non_recursive_shows_subfolders(
    populated_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-recursive mode renders subfolder cards above the file table."""
    monkeypatch.setattr(app_module, "DB_PATH", populated_db)
    client = TestClient(app_module.app)

    r = client.get("/files?path=/Volumes/TD&recursive=false")
    assert r.status_code == 200
    content = r.text
    # Subfolder section should list Music and Docs
    assert "Music" in content
    assert "Docs" in content


def test_files_no_path_shows_all(
    populated_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", populated_db)
    client = TestClient(app_module.app)

    r = client.get("/files")
    assert r.status_code == 200
    content = r.text
    assert "song1.mp3" in content
    assert "readme.txt" in content


# ---------------------------------------------------------------------------
# /folders routes
# ---------------------------------------------------------------------------


def test_folders_root(
    populated_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", populated_db)
    client = TestClient(app_module.app)

    r = client.get("/folders")
    assert r.status_code == 200
    assert b"TD" in r.content


def test_folders_with_path(
    populated_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", populated_db)
    client = TestClient(app_module.app)

    r = client.get("/folders?path=/Volumes/TD")
    assert r.status_code == 200
    assert b"Music" in r.content
    assert b"Docs" in r.content


def test_folders_children_partial(
    populated_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", populated_db)
    client = TestClient(app_module.app)

    r = client.get("/folders/children?path=/Volumes/TD")
    assert r.status_code == 200
    assert b"Music" in r.content
    assert b"Docs" in r.content


def test_folders_children_empty_path_uses_db_root(
    populated_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty path falls back to _find_db_root — should show children of /Volumes."""
    monkeypatch.setattr(app_module, "DB_PATH", populated_db)
    client = TestClient(app_module.app)

    r = client.get("/folders/children")
    assert r.status_code == 200
    # /Volumes has one child: TD
    assert b"TD" in r.content


def test_folders_children_excludes_trash_by_default(
    populated_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files labeled for trash should not be counted in folder counts by default."""
    # Add a file to Music and label it for trash
    drive_id = str(uuid.uuid4())
    conn = sqlite3.connect(populated_db)
    file_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO files (id, path, drive_id, indexed_at, mime_type, extension, status, size_bytes)
        VALUES (?, ?, ?, datetime('now'), 'audio/mpeg', 'mp3', 'present', ?)
        """,
        (file_id, "/Volumes/TD/Music/to_delete.mp3", drive_id, 1500),
    )
    conn.execute(
        """
        INSERT INTO labels (file_id, action, source, labeled_at)
        VALUES (?, 'trash', 'user', datetime('now'))
        """,
        (file_id,),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(app_module, "DB_PATH", populated_db)
    client = TestClient(app_module.app)

    r = client.get("/folders/children?path=/Volumes/TD")
    assert r.status_code == 200
    content = r.text
    # Should show Music and Docs
    assert "Music" in content
    assert "Docs" in content
    # Music should show 3 files (not 4, because trash-labeled file is excluded)
    # Check for the file count in the HTML
    import re
    # Looking for something like >Music<.*?>3<
    music_match = re.search(r'>Music<.*?>(\d+)<', content, re.DOTALL)
    assert music_match
    assert music_match.group(1) == "3"


def test_folders_children_includes_trash_when_requested(
    populated_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When show_trash=true, files labeled for trash should be included in folder counts."""
    # Add a file to Music and label it for trash
    drive_id = str(uuid.uuid4())
    conn = sqlite3.connect(populated_db)
    file_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO files (id, path, drive_id, indexed_at, mime_type, extension, status, size_bytes)
        VALUES (?, ?, ?, datetime('now'), 'audio/mpeg', 'mp3', 'present', ?)
        """,
        (file_id, "/Volumes/TD/Music/to_delete.mp3", drive_id, 1500),
    )
    conn.execute(
        """
        INSERT INTO labels (file_id, action, source, labeled_at)
        VALUES (?, 'trash', 'user', datetime('now'))
        """,
        (file_id,),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(app_module, "DB_PATH", populated_db)
    client = TestClient(app_module.app)

    r = client.get("/folders/children?path=/Volumes/TD&show_trash=true")
    assert r.status_code == 200
    content = r.text
    # Should show Music and Docs
    assert "Music" in content
    assert "Docs" in content
    # Music should now show 4 files (including trash-labeled file)
    import re
    # Looking for something like >Music<.*?>4<
    music_match = re.search(r'>Music<.*?>(\d+)<', content, re.DOTALL)
    assert music_match
    assert music_match.group(1) == "4"


# ---------------------------------------------------------------------------
# Breadcrumb in /files response
# ---------------------------------------------------------------------------


def test_files_shows_breadcrumb_when_path_set(
    populated_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", populated_db)
    client = TestClient(app_module.app)

    r = client.get("/files?path=/Volumes/TD/Music")
    assert r.status_code == 200
    content = r.text
    # Breadcrumb segments should appear as links
    assert "Volumes" in content
    assert "TD" in content
    assert "Music" in content
    # "All files" link
    assert "All files" in content
