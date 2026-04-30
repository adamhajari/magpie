"""Tests for the document analyzer (Task 3.1)."""

from __future__ import annotations

import json
import pathlib
import uuid
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from db.migrate import migrate
from magpie.analyzer import (
    ANALYZER,
    ANALYZER_LOCAL_OLLAMA,
    ANALYZER_LOCAL_SUMY,
    _find_documents,
    _is_document,
    call_ollama_text,
    extract_keyword_tags,
    extract_text,
    extract_text_from_docx,
    run_analyze_compare,
    run_analyze_docs,
    summarize_local_doc,
)
from magpie.cli import app
from magpie.db import get_connection


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db_path = tmp_path / "magpie.db"
    migrate(db_path)
    return db_path


@pytest.fixture()
def db_with_files(tmp_db: pathlib.Path, tmp_path: pathlib.Path):
    """Create a set of indexed files in the DB and on disk."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    files = [
        ("report.txt", "text/plain", "txt", b"This is a text report about finance."),
        ("notes.md", "text/markdown", "md", b"# Notes\n\nSome markdown notes here."),
        ("image.jpg", "image/jpeg", "jpg", b"\xff\xd8\xff fake jpeg data"),
        ("data.csv", "text/csv", "csv", b"col1,col2\nval1,val2"),
    ]
    inserted = []
    with get_connection(tmp_db) as conn:
        for name, mime, ext, content in files:
            p = docs_dir / name
            p.write_bytes(content)
            fid = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """INSERT INTO files (id, path, size_bytes, indexed_at, mime_type, extension, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'present')""",
                (fid, str(p), len(content), now, mime, ext),
            )
            inserted.append({"id": fid, "path": str(p), "mime": mime, "ext": ext})
        conn.commit()
    return docs_dir, tmp_db, inserted


runner = CliRunner()


# ---------------------------------------------------------------------------
# _is_document
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ext,mime,expected", [
    ("pdf", "application/pdf", True),
    ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", True),
    ("txt", "text/plain", True),
    ("md", "text/markdown", True),
    ("jpg", "image/jpeg", False),
    ("csv", "text/csv", False),
    ("", "application/pdf", True),
    ("pdf", None, True),
    ("", None, False),
])
def test_is_document(ext: str, mime: Optional[str], expected: bool) -> None:
    assert _is_document(ext, mime) is expected


# ---------------------------------------------------------------------------
# Text extraction — TXT and MD
# ---------------------------------------------------------------------------

def test_extract_text_txt(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "hello.txt"
    f.write_text("Hello, world!\nSecond line.")
    text, used_ocr = extract_text(f, "txt", "text/plain")
    assert "Hello, world!" in text
    assert not used_ocr


def test_extract_text_md(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "readme.md"
    f.write_text("# Title\n\nSome content here.")
    text, used_ocr = extract_text(f, "md", "text/markdown")
    assert "Title" in text
    assert not used_ocr


# ---------------------------------------------------------------------------
# Text extraction — DOCX (mocked)
# ---------------------------------------------------------------------------

def test_extract_text_docx(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "doc.docx"
    f.write_bytes(b"fake docx")  # won't be read directly; we mock python-docx

    mock_para = MagicMock()
    mock_para.text = "Paragraph content"
    mock_doc = MagicMock()
    mock_doc.paragraphs = [mock_para]

    with patch("magpie.analyzer.extract_text_from_docx") as mock_fn:
        mock_fn.return_value = "Paragraph content"
        text, used_ocr = extract_text(f, "docx", None)
    assert "Paragraph content" in text
    assert not used_ocr


# ---------------------------------------------------------------------------
# Text extraction — PDF with pdfplumber (mocked)
# ---------------------------------------------------------------------------

def test_extract_text_pdf_with_text(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF fake")

    with patch("pdfplumber.open") as mock_open:
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Extracted PDF text"
        mock_pdf = MagicMock()
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)
        mock_pdf.pages = [mock_page]
        mock_open.return_value = mock_pdf

        text, used_ocr = extract_text(f, "pdf", "application/pdf")

    assert "Extracted PDF text" in text
    assert not used_ocr


def test_extract_text_pdf_ocr_fallback(tmp_path: pathlib.Path) -> None:
    """PDF with no extractable text should fall back to pytesseract."""
    f = tmp_path / "scan.pdf"
    f.write_bytes(b"%PDF fake scan")

    with patch("pdfplumber.open") as mock_open, \
         patch("magpie.analyzer._ocr_pdf") as mock_ocr:

        mock_page = MagicMock()
        mock_page.extract_text.return_value = None  # no text
        mock_pdf = MagicMock()
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)
        mock_pdf.pages = [mock_page]
        mock_open.return_value = mock_pdf

        mock_ocr.return_value = "OCR extracted text"

        text, used_ocr = extract_text(f, "pdf", "application/pdf")

    assert "OCR extracted text" in text
    assert used_ocr


# ---------------------------------------------------------------------------
# _find_documents
# ---------------------------------------------------------------------------

def test_find_documents_filters_to_supported_types(
    db_with_files: tuple,
) -> None:
    docs_dir, tmp_db, inserted = db_with_files
    docs = _find_documents(docs_dir, tmp_db, limit=None, force=False, analyzer=ANALYZER)
    exts = {d["extension"] for d in docs}
    assert "txt" in exts
    assert "md" in exts
    assert "jpg" not in exts
    assert "csv" not in exts


def test_find_documents_skips_already_analyzed(
    db_with_files: tuple,
) -> None:
    docs_dir, tmp_db, inserted = db_with_files

    # Pre-insert analysis for the txt file
    txt_record = next(r for r in inserted if r["ext"] == "txt")
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO file_analysis (file_id, summary, tags, analyzer, analyzed_at) VALUES (?,?,?,?,?)",
            (txt_record["id"], "existing summary", "[]", ANALYZER, now),
        )
        conn.commit()

    docs = _find_documents(docs_dir, tmp_db, limit=None, force=False, analyzer=ANALYZER)
    ids = {d["id"] for d in docs}
    assert txt_record["id"] not in ids


def test_find_documents_force_includes_analyzed(
    db_with_files: tuple,
) -> None:
    docs_dir, tmp_db, inserted = db_with_files

    txt_record = next(r for r in inserted if r["ext"] == "txt")
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO file_analysis (file_id, summary, tags, analyzer, analyzed_at) VALUES (?,?,?,?,?)",
            (txt_record["id"], "existing", "[]", ANALYZER, now),
        )
        conn.commit()

    docs = _find_documents(docs_dir, tmp_db, limit=None, force=True, analyzer=ANALYZER)
    ids = {d["id"] for d in docs}
    assert txt_record["id"] in ids


def test_find_documents_respects_limit(db_with_files: tuple) -> None:
    docs_dir, tmp_db, _ = db_with_files
    docs = _find_documents(docs_dir, tmp_db, limit=1, force=False, analyzer=ANALYZER)
    assert len(docs) <= 1


# ---------------------------------------------------------------------------
# run_analyze_docs — dry run
# ---------------------------------------------------------------------------

def test_dry_run_prints_table_local_no_api_cost(db_with_files: tuple) -> None:
    docs_dir, tmp_db, _ = db_with_files

    result = runner.invoke(
        app,
        ["analyze", "docs", str(docs_dir), "--db", str(tmp_db), "--dry-run"],
    )
    assert result.exit_code == 0
    assert "Sumy" in result.output or "local" in result.output.lower()
    assert "Estimated API cost" not in result.output
    assert "ANTHROPIC_API_KEY" not in result.output


def test_dry_run_no_db_writes(db_with_files: tuple) -> None:
    docs_dir, tmp_db, _ = db_with_files

    runner.invoke(
        app,
        ["analyze", "docs", str(docs_dir), "--db", str(tmp_db), "--dry-run"],
    )

    with get_connection(tmp_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM file_analysis").fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# Local document analysis (Sumy / keyword tags)
# ---------------------------------------------------------------------------

def test_extract_keyword_tags_basic() -> None:
    text = "The algorithm processes financial transactions and banking records daily."
    tags = extract_keyword_tags(text, max_tags=5)
    assert "financial" in tags or "algorithm" in tags
    assert "the" not in tags


def test_summarize_local_doc_non_empty(tmp_path: pathlib.Path) -> None:
    long_text = " ".join(
        f"This is sentence number {i} about astronomy and telescopes." for i in range(15)
    )
    summary, tags = summarize_local_doc(long_text)
    assert summary
    assert isinstance(tags, list)


def test_run_analyze_docs_local_stores_local_sumy(db_with_files: tuple) -> None:
    docs_dir, tmp_db, _ = db_with_files
    run_analyze_docs(docs_dir, db_path=tmp_db, dry_run=False, sumy=True)
    with get_connection(tmp_db) as conn:
        rows = conn.execute(
            "SELECT analyzer, summary, tags FROM file_analysis ORDER BY file_id"
        ).fetchall()
    assert rows
    for row in rows:
        assert row["analyzer"] == ANALYZER_LOCAL_SUMY
        assert row["summary"]
        assert json.loads(row["tags"])


def test_run_analyze_docs_ollama_stores_local_ollama(
    db_with_files: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test Ollama path stores analyzer=local-ollama."""
    docs_dir, tmp_db, _ = db_with_files
    mock_response = MagicMock()
    mock_response.message.content = json.dumps({
        "summary": "Test document about something.",
        "tags": ["test", "document", "summary"]
    })
    monkeypatch.setattr(
        "magpie.analyzer.call_ollama_text",
        MagicMock(return_value=("Test document about something.", ["test", "document", "summary"]))
    )
    monkeypatch.setattr("magpie.analyzer.check_ollama_available", MagicMock())

    run_analyze_docs(docs_dir, db_path=tmp_db, dry_run=False, ollama_model="llama2")
    with get_connection(tmp_db) as conn:
        rows = conn.execute(
            "SELECT analyzer, summary, tags FROM file_analysis ORDER BY file_id"
        ).fetchall()
    assert rows
    for row in rows:
        assert row["analyzer"] == ANALYZER_LOCAL_OLLAMA
        assert row["summary"]
        assert json.loads(row["tags"])


def test_run_analyze_docs_ollama_dry_run(
    db_with_files: tuple,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test Ollama dry-run prints model name, no DB writes."""
    docs_dir, tmp_db, _ = db_with_files
    monkeypatch.setattr("magpie.analyzer.check_ollama_available", MagicMock())

    run_analyze_docs(docs_dir, db_path=tmp_db, dry_run=True, ollama_model="mistral")
    captured = capsys.readouterr()
    assert "Ollama/mistral" in captured.out

    with get_connection(tmp_db) as conn:
        rows = conn.execute("SELECT COUNT(*) as cnt FROM file_analysis").fetchall()
    assert rows[0]["cnt"] == 0


def test_run_analyze_docs_ollama_unavailable(
    db_with_files: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test Ollama unavailable raises SystemExit."""
    docs_dir, tmp_db, _ = db_with_files
    monkeypatch.setattr(
        "magpie.analyzer.check_ollama_available",
        MagicMock(side_effect=SystemExit(1))
    )

    with pytest.raises(SystemExit):
        run_analyze_docs(docs_dir, db_path=tmp_db, dry_run=False, ollama_model="llama2")


def test_find_documents_separate_rows_per_analyzer(db_with_files: tuple) -> None:
    """Cloud and local analyzers are independent rows in file_analysis."""
    docs_dir, tmp_db, inserted = db_with_files
    txt = next(r for r in inserted if r["ext"] == "txt")
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO file_analysis (file_id, summary, tags, analyzer, analyzed_at) VALUES (?,?,?,?,?)",
            (txt["id"], "sumy done", "[]", ANALYZER_LOCAL_SUMY, now),
        )
        conn.commit()

    pending_cloud = _find_documents(docs_dir, tmp_db, None, False, ANALYZER)
    cloud_ids = {d["id"] for d in pending_cloud}
    assert txt["id"] in cloud_ids

    pending_local = _find_documents(docs_dir, tmp_db, None, False, ANALYZER_LOCAL_SUMY)
    local_ids = {d["id"] for d in pending_local}
    assert txt["id"] not in local_ids


# ---------------------------------------------------------------------------
# compare command
# ---------------------------------------------------------------------------

def test_analyze_compare_docs_prints_both_columns(
    db_with_files: tuple,
) -> None:
    docs_dir, tmp_db, _ = db_with_files

    mock_ollama = MagicMock(return_value={"summary": "Ollama summary", "tags": ["ollama-tag"]})
    mock_local = MagicMock(return_value=("Sumy summary", ["sumy-tag"]))

    with patch("magpie.analyzer.call_ollama_text", mock_ollama), patch(
        "magpie.analyzer.summarize_local_doc", mock_local
    ), patch("magpie.analyzer.check_ollama_available"):
        run_analyze_compare(docs_dir, db_path=tmp_db, limit=3, photos=False)

    assert mock_ollama.called
    assert mock_local.called


def test_check_ollama_system_exit() -> None:
    from magpie.analyzer import check_ollama_available

    with patch("ollama.list", side_effect=ConnectionError("refused")):
        with pytest.raises(SystemExit):
            check_ollama_available()


# ---------------------------------------------------------------------------
# dot-underscore and trash-label skipping
# ---------------------------------------------------------------------------

def _insert_file(conn, docs_dir: pathlib.Path, name: str, mime: str, ext: str, content: bytes) -> str:
    p = docs_dir / name
    p.write_bytes(content)
    fid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO files (id, path, size_bytes, indexed_at, mime_type, extension, status) VALUES (?,?,?,?,?,?,?)",
        (fid, str(p), len(content), now, mime, ext, "present"),
    )
    return fid


def test_find_documents_skips_dot_underscore(tmp_db: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """._-prefixed files are silently excluded from document analysis."""
    docs_dir = tmp_path / "docs2"
    docs_dir.mkdir()
    content = b"Some document text here."
    with get_connection(tmp_db) as conn:
        real_id = _insert_file(conn, docs_dir, "report.txt", "text/plain", "txt", content)
        fork_id = _insert_file(conn, docs_dir, "._report.txt", "text/plain", "txt", b"\x00\x05\x16\x07")
        conn.commit()

    docs = _find_documents(docs_dir, tmp_db, limit=None, force=False, analyzer=ANALYZER_LOCAL_SUMY)
    ids = {d["id"] for d in docs}
    assert real_id in ids
    assert fork_id not in ids


def test_find_documents_skips_trash_labeled(tmp_db: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """Files with a pending trash label are excluded from document analysis."""
    docs_dir = tmp_path / "docs3"
    docs_dir.mkdir()
    content = b"Some document text here."
    with get_connection(tmp_db) as conn:
        keep_id = _insert_file(conn, docs_dir, "keep.txt", "text/plain", "txt", content)
        trash_id = _insert_file(conn, docs_dir, "trash.txt", "text/plain", "txt", content)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO labels (file_id, action, source, labeled_at) VALUES (?,?,?,?)",
            (trash_id, "trash", "user", now),
        )
        conn.commit()

    docs = _find_documents(docs_dir, tmp_db, limit=None, force=False, analyzer=ANALYZER_LOCAL_SUMY)
    ids = {d["id"] for d in docs}
    assert keep_id in ids
    assert trash_id not in ids
