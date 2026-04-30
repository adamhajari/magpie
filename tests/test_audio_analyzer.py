"""Tests for audio metadata extraction (magpie/audio_analyzer.py)."""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

import pytest

from db.migrate import migrate
from magpie.audio_analyzer import (
    extract_audio_metadata,
    is_audio,
    save_cover_art,
    store_audio_metadata,
)
from magpie.db import get_connection


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db_path = tmp_path / "magpie.db"
    migrate(db_path)
    return db_path


def _insert_file(conn, file_id: str, path: str, mime_type: str = "audio/mpeg") -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO files (id, path, mime_type, extension, indexed_at, status)
           VALUES (?, ?, ?, ?, ?, 'present')""",
        (file_id, path, mime_type, path.rsplit(".", 1)[-1], now),
    )


# ---------------------------------------------------------------------------
# is_audio
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mime,ext,expected", [
    ("audio/mpeg", "mp3", True),
    ("audio/flac", "flac", True),
    ("audio/x-wav", "wav", True),
    ("audio/mp4", "m4a", True),
    (None, "mp3", True),
    (None, "flac", True),
    ("image/jpeg", "jpg", False),
    ("application/pdf", "pdf", False),
    (None, "txt", False),
])
def test_is_audio(mime, ext, expected):
    assert is_audio(mime, ext) is expected


# ---------------------------------------------------------------------------
# extract_audio_metadata — MP3 (ID3 tags)
# ---------------------------------------------------------------------------

def _make_mp3_mock() -> MagicMock:
    """Build a mock mutagen File() return value that looks like an ID3-tagged MP3."""
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, TDRC, APIC

    # Build real ID3 tag objects for realistic coverage
    tags = MagicMock(spec=ID3)
    tags.__class__ = ID3

    def _tag_get(key, default=None):
        data = {
            "TIT2": MagicMock(**{"__getitem__.return_value": "Test Song", "__str__.return_value": "TIT2"}),
            "TPE1": MagicMock(**{"__getitem__.return_value": "Test Artist"}),
            "TALB": MagicMock(**{"__getitem__.return_value": "Test Album"}),
            "TRCK": MagicMock(**{"__getitem__.return_value": "3"}),
            "TDRC": MagicMock(**{"__getitem__.return_value": "2023"}),
        }
        return data.get(key, default)

    tags.get = _tag_get
    tags.keys.return_value = ["APIC:"]
    apic = MagicMock()
    apic.data = b"\xff\xd8\xff\xe0"  # minimal JPEG-like bytes
    tags.__getitem__ = lambda self, key: apic if key.startswith("APIC") else None

    info = MagicMock()
    info.length = 213.5
    info.sample_rate = 44100
    info.bitrate = 320000
    info.channels = 2

    mock_audio = MagicMock()
    mock_audio.tags = tags
    mock_audio.info = info
    return mock_audio


def test_extract_mp3_metadata(tmp_path: pathlib.Path) -> None:
    fake_mp3 = tmp_path / "song.mp3"
    fake_mp3.write_bytes(b"\xff\xfb" + b"\x00" * 100)  # dummy bytes

    mock_audio = _make_mp3_mock()

    with patch("magpie.audio_analyzer.MutagenFile", return_value=mock_audio), \
         patch("magpie.audio_analyzer._MUTAGEN_AVAILABLE", True):
        meta = extract_audio_metadata(fake_mp3)

    assert meta["title"] == "Test Song"
    assert meta["artist"] == "Test Artist"
    assert meta["album"] == "Test Album"
    assert meta["track_number"] == "3"
    assert meta["year"] == "2023"
    assert meta["duration_seconds"] == pytest.approx(213.5)
    assert meta["sample_rate"] == 44100
    assert meta["bit_rate"] == 320000
    assert meta["channels"] == 2
    assert meta["cover_art_data"] == b"\xff\xd8\xff\xe0"


# ---------------------------------------------------------------------------
# extract_audio_metadata — FLAC (Vorbis comment style)
# ---------------------------------------------------------------------------

def _make_flac_mock() -> MagicMock:
    """Build a mock mutagen File() return value that looks like a FLAC file."""
    from mutagen.id3 import ID3  # just to confirm tags is NOT ID3

    tags = {}  # plain dict — triggers the Vorbis-comment branch
    # mutagen Vorbis tags use lowercase lists
    tags["title"] = ["Flac Track"]
    tags["artist"] = ["Flac Artist"]
    tags["album"] = ["Flac Album"]
    tags["tracknumber"] = ["7"]
    tags["date"] = ["2021"]

    # Wrap in a MagicMock that has .get() like a real mapping
    tags_mock = MagicMock()
    tags_mock.get = lambda key, default=None: tags.get(key, tags.get(key.upper(), tags.get(key.lower(), default)))
    tags_mock.__class__ = dict  # not ID3 / MP4Tags

    # FLAC pictures
    picture = MagicMock()
    picture.data = b"PNG\x89"
    pictures = [picture]

    info = MagicMock()
    info.length = 305.2
    info.sample_rate = 48000
    info.bits_per_sample = 24
    del info.bitrate  # FLAC may not have bitrate attribute

    mock_audio = MagicMock()
    mock_audio.tags = tags_mock
    mock_audio.info = info
    mock_audio.pictures = pictures
    return mock_audio


def test_extract_flac_metadata(tmp_path: pathlib.Path) -> None:
    fake_flac = tmp_path / "track.flac"
    fake_flac.write_bytes(b"fLaC" + b"\x00" * 50)

    mock_audio = _make_flac_mock()

    with patch("magpie.audio_analyzer.MutagenFile", return_value=mock_audio), \
         patch("magpie.audio_analyzer._MUTAGEN_AVAILABLE", True):
        meta = extract_audio_metadata(fake_flac)

    assert meta["title"] == "Flac Track"
    assert meta["artist"] == "Flac Artist"
    assert meta["album"] == "Flac Album"
    assert meta["track_number"] == "7"
    assert meta["year"] == "2021"
    assert meta["duration_seconds"] == pytest.approx(305.2)
    assert meta["sample_rate"] == 48000
    assert meta["cover_art_data"] == b"PNG\x89"


# ---------------------------------------------------------------------------
# extract_audio_metadata — mutagen not available
# ---------------------------------------------------------------------------

def test_extract_metadata_mutagen_missing(tmp_path: pathlib.Path) -> None:
    fake = tmp_path / "song.mp3"
    fake.write_bytes(b"\x00")
    with patch("magpie.audio_analyzer._MUTAGEN_AVAILABLE", False):
        meta = extract_audio_metadata(fake)
    assert meta == {}


# ---------------------------------------------------------------------------
# extract_audio_metadata — mutagen returns None (unrecognised format)
# ---------------------------------------------------------------------------

def test_extract_metadata_none_audio(tmp_path: pathlib.Path) -> None:
    fake = tmp_path / "weird.mp3"
    fake.write_bytes(b"\x00")
    with patch("magpie.audio_analyzer.MutagenFile", return_value=None), \
         patch("magpie.audio_analyzer._MUTAGEN_AVAILABLE", True):
        meta = extract_audio_metadata(fake)
    assert meta["title"] is None
    assert meta["duration_seconds"] is None


# ---------------------------------------------------------------------------
# store_audio_metadata / DB round-trip
# ---------------------------------------------------------------------------

def test_store_audio_metadata_writes_rows(tmp_path: pathlib.Path, tmp_db: pathlib.Path) -> None:
    file_id = "aaaaaaaa-0000-0000-0000-000000000001"
    with get_connection(tmp_db) as conn:
        _insert_file(conn, file_id, str(tmp_path / "song.mp3"))

    metadata = {
        "title": "DB Song",
        "artist": "DB Artist",
        "album": "DB Album",
        "track_number": "5",
        "year": "2020",
        "channels": 2,
        "duration_seconds": 180.0,
        "sample_rate": 44100,
        "bit_rate": 256000,
        "cover_art_data": None,
    }

    with get_connection(tmp_db) as conn:
        store_audio_metadata(file_id, metadata, conn)

    with get_connection(tmp_db) as conn:
        rows = {
            row["key"]: row["value"]
            for row in conn.execute(
                "SELECT key, value FROM file_metadata WHERE file_id = ?", (file_id,)
            ).fetchall()
        }
        file_row = conn.execute(
            "SELECT duration_seconds, sample_rate, bit_rate FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()

    assert rows["title"] == "DB Song"
    assert rows["artist"] == "DB Artist"
    assert rows["album"] == "DB Album"
    assert rows["track_number"] == "5"
    assert rows["year"] == "2020"
    assert rows["channels"] == "2"
    assert file_row["duration_seconds"] == pytest.approx(180.0)
    assert file_row["sample_rate"] == 44100
    assert file_row["bit_rate"] == 256000


def test_store_audio_metadata_idempotent(tmp_path: pathlib.Path, tmp_db: pathlib.Path) -> None:
    """Calling store twice should upsert without duplicating rows."""
    file_id = "aaaaaaaa-0000-0000-0000-000000000002"
    with get_connection(tmp_db) as conn:
        _insert_file(conn, file_id, str(tmp_path / "song.mp3"))

    metadata = {
        "title": "Same Song",
        "artist": None,
        "album": None,
        "track_number": None,
        "year": None,
        "channels": None,
        "duration_seconds": 60.0,
        "sample_rate": 44100,
        "bit_rate": 128000,
        "cover_art_data": None,
    }

    with get_connection(tmp_db) as conn:
        store_audio_metadata(file_id, metadata, conn)
        store_audio_metadata(file_id, metadata, conn)

    with get_connection(tmp_db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM file_metadata WHERE file_id = ? AND key = 'title'",
            (file_id,),
        ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# save_cover_art
# ---------------------------------------------------------------------------

def test_save_cover_art_creates_jpeg(tmp_path: pathlib.Path) -> None:
    from PIL import Image
    import io

    # Create a tiny valid JPEG in memory
    img = Image.new("RGB", (10, 10), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    jpeg_data = buf.getvalue()

    with patch("magpie.audio_analyzer.THUMBS_DIR", tmp_path / "thumbs"):
        out = save_cover_art("test-file-id", jpeg_data)

    assert out is not None
    assert out.exists()
    assert out.name == "test-file-id.jpg"


def test_save_cover_art_invalid_data_returns_none(tmp_path: pathlib.Path) -> None:
    with patch("magpie.audio_analyzer.THUMBS_DIR", tmp_path / "thumbs"):
        out = save_cover_art("bad-id", b"not an image")
    assert out is None
