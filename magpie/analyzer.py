"""Content analyzer — documents (Sumy / Ollama) and photos (LLaVA via Ollama)."""

from __future__ import annotations

import json
import pathlib
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Optional, TypeVar

from rich.console import Console
from rich.panel import Panel
from rich.progress import track
from rich.table import Table

from magpie.db import DB_PATH, get_connection

console = Console()

# ---------------------------------------------------------------------------
# Analyzer identifiers (stored in file_analysis.analyzer)
# ---------------------------------------------------------------------------

ANALYZER_LOCAL_LLAVA = "local-llava"
ANALYZER_LOCAL_SUMY = "local-sumy"
ANALYZER_LOCAL_OLLAMA = "local-ollama"
ANALYZER = ANALYZER_LOCAL_LLAVA

OLLAMA_VISION_MODEL = "llava"
OLLAMA_TEXT_MODEL = "llama2"

# Image quality mapping (categorical to numeric)
QUALITY_MAPPING = {
    "low": 1,
    "medium": 2,
    "high": 3,
}

# ~3000 tokens ≈ 12000 chars (rough 4 chars/token estimate)
MAX_TEXT_CHARS = 12_000

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
SUPPORTED_MIMES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/markdown",
    "text/x-markdown",
}

_BUILTIN_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "as", "by",
    "with", "from", "is", "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "will", "would", "could", "should", "may", "might", "must", "shall",
    "this", "that", "these", "those", "it", "its", "they", "them", "their", "we", "our", "you",
    "your", "he", "she", "his", "her", "not", "no", "so", "if", "than", "then", "there", "when",
    "which", "who", "what", "can", "into", "about", "over", "such", "also", "only", "just",
})


def ollama_setup_instructions() -> str:
    return """Ollama is not reachable (expected at http://127.0.0.1:11434 by default).

To use local image analysis (default for analyze photos, or photo comparison):
  1. Install Ollama: https://ollama.com/download
  2. Start the Ollama app or run: ollama serve
  3. Pull a vision model, for example: ollama pull llava

Then retry the command."""


def check_ollama_available() -> None:
    """Exit with a clear message if the Ollama daemon is not reachable."""
    import ollama

    try:
        ollama.list()
    except Exception:
        console.print(f"[red]{ollama_setup_instructions()}[/red]")
        raise SystemExit(1)


def _is_document(ext: str, mime: Optional[str]) -> bool:
    ext = ext.lower() if ext else ""
    if ext and f".{ext}" in SUPPORTED_EXTENSIONS:
        return True
    if mime and mime.lower() in SUPPORTED_MIMES:
        return True
    return False


def _ocr_pdf(path: pathlib.Path) -> str:
    """OCR fallback: render PDF pages and run pytesseract."""
    import pytesseract
    from pdf2image import convert_from_path

    images = convert_from_path(path, dpi=150)
    return "\n".join(pytesseract.image_to_string(img) for img in images)


def extract_text_from_pdf(path: pathlib.Path) -> tuple[str, bool]:
    """Return (text, used_ocr). Falls back to pytesseract if no text extracted."""
    import pdfplumber

    text_parts: list[str] = []
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    text_parts.append(t)
    except Exception as exc:
        console.print(f"[yellow]pdfplumber failed on {path.name}: {exc}[/yellow]")

    text = "\n".join(text_parts).strip()
    if text:
        return text, False

    try:
        return _ocr_pdf(path).strip(), True
    except Exception as exc:
        console.print(f"[yellow]OCR fallback failed on {path.name}: {exc}[/yellow]")
        return "", True


def extract_text_from_docx(path: pathlib.Path) -> str:
    from docx import Document

    doc = Document(str(path))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n".join(paragraphs)


def extract_text(path: pathlib.Path, extension: str, mime_type: Optional[str]) -> tuple[str, bool]:
    """Return (text, used_ocr)."""
    ext = (extension or path.suffix.lstrip(".")).lower()
    if ext == "pdf" or (mime_type and "pdf" in mime_type):
        return extract_text_from_pdf(path)
    if ext == "docx" or (mime_type and "wordprocessingml" in (mime_type or "")):
        return extract_text_from_docx(path), False
    return path.read_text(errors="replace"), False


def _sentences_simple(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def summarize_with_sumy_lsa(text: str, sentences_count: int = 3) -> str:
    """LSA summarization via sumy; falls back to simple sentence extraction if NLTK data is missing."""
    text = text.strip()
    if not text:
        return ""
    if len(text) < 60:
        return text[:2000]

    try:
        from sumy.nlp.stemmers import Stemmer
        from sumy.nlp.tokenizers import Tokenizer
        from sumy.parsers.plaintext import PlaintextParser
        from sumy.summarizers.lsa import LsaSummarizer
        from sumy.utils import get_stop_words

        language = "english"
        parser = PlaintextParser.from_string(text, Tokenizer(language))
        stemmer = Stemmer(language)
        summarizer = LsaSummarizer(stemmer)
        summarizer.stop_words = get_stop_words(language)
        sentences = summarizer(parser.document)
        out = " ".join(str(s) for s in sentences)
        if out.strip():
            return out.strip()
    except (LookupError, ValueError, ZeroDivisionError):
        pass

    sents = _sentences_simple(text)
    if not sents:
        return text[:2000]
    return " ".join(sents[:sentences_count])


def extract_keyword_tags(text: str, max_tags: int = 10) -> list[str]:
    """Lowercase hyphenated tags from word frequencies (no NLTK)."""
    words = re.findall(r"[a-zA-Z][a-zA-Z\-]{2,}", text.lower())
    filtered = [w for w in words if w not in _BUILTIN_STOPWORDS and not w.isdigit()]
    counts = Counter(filtered)
    tags: list[str] = []
    for word, _ in counts.most_common(max_tags * 2):
        t = word.strip("-")
        if len(t) < 3 or t in tags:
            continue
        tags.append(t)
        if len(tags) >= max_tags:
            break
    return tags


def summarize_local_doc(text: str) -> tuple[str, list[str]]:
    summary = summarize_with_sumy_lsa(text)
    tags = extract_keyword_tags(text)
    return summary, tags


def call_ollama_text(text: str, model: str = OLLAMA_TEXT_MODEL) -> tuple[str, list[str]]:
    """Call Ollama with the given text model and return (summary, tags)."""
    import ollama

    prompt = _build_prompt(text)
    response = ollama.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        format="json",
    )
    raw = response.message.content or ""
    return _parse_llm_json_response(raw)


def _build_prompt(text: str) -> str:
    truncated = text[:MAX_TEXT_CHARS]
    if len(text) > MAX_TEXT_CHARS:
        truncated += "\n\n[... text truncated ...]"
    return f"""Analyze the following document and respond with valid JSON only.

Return a JSON object with exactly two fields:
- "summary": a 2-3 sentence description of the document's content and purpose
- "tags": a JSON array of 5-10 short descriptive tags (lowercase, no spaces, use hyphens)

Document text:
---
{truncated}
---

Respond with only the JSON object, no additional text."""


def _parse_llm_json_response(raw: str) -> dict:
    """Parse JSON object from LLM output (strips optional markdown fences).

    Returns dict with keys: summary, tags, settings (optional), people (optional), quality (optional).
    """
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    data = json.loads(raw)

    result = {
        "summary": str(data.get("summary", "")),
        "tags": [],
        "settings": None,
        "people": None,
        "quality": None,
    }

    tags = data.get("tags", [])
    if isinstance(tags, list):
        result["tags"] = [str(t) for t in tags]

    settings = data.get("settings")
    if isinstance(settings, list):
        result["settings"] = [str(s) for s in settings]

    people = data.get("people")
    if people is not None:
        try:
            result["people"] = int(people)
        except (ValueError, TypeError):
            pass

    quality = data.get("quality")
    if quality is not None:
        quality_str = str(quality).lower().strip()
        result["quality"] = QUALITY_MAPPING.get(quality_str)

    return result


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


def _photo_analysis_prompt() -> str:
    return """Look at this image and respond with valid JSON only.

Return a JSON object with exactly five fields:
- "summary": a single sentence describing what is shown (15-25 words)
- "settings": a JSON array of 1-3 applicable settings from: outdoors, nature, beach, water, desert, city, park, yard, concert, party, indoors, home, kitchen, office, restaurant, retail, document, screenshot, other, unknown
- "people": integer count of people visible, or null if none/unclear
- "quality": image quality as "low", "medium", or "high"
- "tags": a JSON array of 5-10 short descriptive tags (lowercase, hyphens instead of spaces)

Respond with only the JSON object, no additional text."""


def call_ollama_llava(image_path: pathlib.Path, model: str = OLLAMA_VISION_MODEL) -> dict:
    import ollama

    prompt = _photo_analysis_prompt()
    response = ollama.chat(
        model=model,
        messages=[
            {
                "role": "user",
                "content": prompt,
                "images": [str(image_path)],
            }
        ],
        format="json",
    )
    raw = response.message.content or ""
    return _parse_llm_json_response(raw)


def _find_files_by_kind(
    path: pathlib.Path,
    db_path: pathlib.Path,
    limit: Optional[int],
    force: bool,
    analyzer: str,
    predicate,
) -> list[dict]:
    with get_connection(db_path) as conn:
        query = """
            SELECT f.id, f.path, f.extension, f.mime_type
            FROM files f
            WHERE f.status = 'present'
              AND f.path LIKE ?
        """
        params: list = [str(path).rstrip("/") + "%"]

        if not force:
            query += """
              AND NOT EXISTS (
                  SELECT 1 FROM file_analysis fa
                  WHERE fa.file_id = f.id AND fa.analyzer = ?
              )
            """
            params.append(analyzer)

        query += """
              AND NOT EXISTS (
                  SELECT 1 FROM labels l
                  WHERE l.file_id = f.id AND l.action = 'trash'
              )
            """

        query += " ORDER BY f.path"
        if limit is not None:
            query += f" LIMIT {int(limit)}"

        rows = conn.execute(query, params).fetchall()
        return [
            dict(row)
            for row in rows
            if not pathlib.Path(row["path"]).name.startswith("._")
            and predicate(row["extension"] or "", row["mime_type"])
        ]


def _find_documents(
    path: pathlib.Path,
    db_path: pathlib.Path,
    limit: Optional[int],
    force: bool,
    analyzer: str,
) -> list[dict]:
    return _find_files_by_kind(path, db_path, limit, force, analyzer, _is_document)


def _list_compare_docs(path: pathlib.Path, db_path: pathlib.Path, limit: int) -> list[dict]:
    """Documents under path (ignores existing analysis) for compare mode."""
    exts = ", ".join(f"'{e.lstrip('.')}'" for e in sorted(SUPPORTED_EXTENSIONS))
    mimes = ", ".join(f"'{m}'" for m in sorted(SUPPORTED_MIMES))
    prefix = str(path).rstrip("/") + "%"
    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT f.id, f.path, f.extension, f.mime_type
            FROM files f
            WHERE f.status = 'present'
              AND f.path LIKE ?
              AND (
                lower(f.extension) IN ({exts})
                OR lower(f.mime_type) IN ({mimes})
              )
              AND NOT EXISTS (
                  SELECT 1 FROM labels l
                  WHERE l.file_id = f.id AND l.action = 'trash'
              )
            ORDER BY f.path
            LIMIT ?
            """,
            (prefix, limit),
        ).fetchall()
    return [dict(r) for r in rows if not pathlib.Path(r["path"]).name.startswith("._")]


def _list_compare_photos(
    path: pathlib.Path,
    db_path: pathlib.Path,
    limit: int,
    min_size_bytes: int = 0,
) -> list[dict]:
    from magpie.indexer import _IMAGE_EXTENSIONS

    img_exts = ", ".join(f"'{e.lstrip('.').lower()}'" for e in sorted(_IMAGE_EXTENSIONS))
    prefix = str(path).rstrip("/") + "%"
    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT f.id, f.path, f.extension, f.mime_type
            FROM files f
            WHERE f.status = 'present'
              AND f.path LIKE ?
              AND f.size_bytes >= ?
              AND (
                lower(f.extension) IN ({img_exts})
                OR lower(f.mime_type) LIKE 'image/%'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM labels l
                  WHERE l.file_id = f.id AND l.action = 'trash'
              )
            ORDER BY f.path
            LIMIT ?
            """,
            (prefix, min_size_bytes, limit),
        ).fetchall()
    return [dict(r) for r in rows if not pathlib.Path(r["path"]).name.startswith("._")]


def _store_analysis(
    db_path: pathlib.Path,
    file_id: str,
    summary: str,
    tags: list[str],
    text_content: str,
    analyzer: str,
    settings: Optional[list[str]] = None,
    people: Optional[int] = None,
    quality: Optional[int] = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()

    def _do_insert() -> None:
        with get_connection(db_path) as conn:
            conn.execute(
                """
                INSERT INTO file_analysis (file_id, summary, tags, settings, people, text_content, quality, analyzer, analyzed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_id, analyzer) DO UPDATE SET
                    summary      = excluded.summary,
                    tags         = excluded.tags,
                    settings     = excluded.settings,
                    people       = excluded.people,
                    text_content = excluded.text_content,
                    quality      = excluded.quality,
                    analyzed_at  = excluded.analyzed_at
                """,
                (
                    file_id,
                    summary,
                    json.dumps(tags),
                    json.dumps(settings) if settings else None,
                    people,
                    text_content[:50_000],
                    quality,
                    analyzer,
                    now,
                ),
            )
            conn.commit()

    _with_retry(_do_insert)


def run_analyze_docs(
    path: pathlib.Path,
    db_path: pathlib.Path = DB_PATH,
    dry_run: bool = False,
    limit: Optional[int] = None,
    force: bool = False,
    sumy: bool = False,
    ollama_model: Optional[str] = None,
) -> None:
    """Main entry point for `magpie analyze docs`.

    Backend selection (in order of priority):
    - sumy=True → Sumy LSA (offline, extractive)
    - ollama_model set → Ollama with specified model
    - default → Ollama with llama2
    """
    if not path.exists():
        console.print(f"[red]Path does not exist: {path}[/red]")
        raise SystemExit(1)

    # Determine analyzer and backend
    if sumy:
        analyzer = ANALYZER_LOCAL_SUMY
    else:
        analyzer = ANALYZER_LOCAL_OLLAMA
        if ollama_model is None:
            ollama_model = OLLAMA_TEXT_MODEL

    docs = _find_documents(path, db_path, limit, force, analyzer)

    if not docs:
        console.print("[green]No documents to analyze.[/green]")
        return

    if dry_run:
        title = f"[bold]Dry run — {len(docs)} document(s) would be analyzed[/bold]"
        if analyzer == ANALYZER_LOCAL_SUMY:
            title += " [dim](local Sumy LSA, offline)[/dim]"
        elif analyzer == ANALYZER_LOCAL_OLLAMA:
            title += f" [dim](Ollama/{ollama_model}, offline)[/dim]"
        table = Table(title=title)
        table.add_column("Path", style="cyan", no_wrap=False)
        table.add_column("Type", style="dim")
        for doc in docs[:50]:
            p = pathlib.Path(doc["path"])
            table.add_row(str(p.relative_to(path) if p.is_relative_to(path) else p), doc["extension"] or "?")
        if len(docs) > 50:
            table.add_row(f"... and {len(docs) - 50} more", "")
        console.print(table)

        if analyzer == ANALYZER_LOCAL_SUMY:
            console.print("\n[dim]Local mode: Sumy LSA summarization + keyword tags. No API calls.[/dim]")
        else:
            console.print(f"\n[dim]Local mode: Ollama/{ollama_model} summarization + tags. Requires Ollama running.[/dim]")
        return

    if analyzer == ANALYZER_LOCAL_SUMY:
        console.print(
            f"Analyzing [bold]{len(docs)}[/bold] document(s) with [bold]local Sumy (LSA)[/bold] (offline)…"
        )
        ok = 0
        errors = 0
        for doc in track(docs, description="Summarizing…"):
            p = pathlib.Path(doc["path"])
            try:
                text, _used_ocr = extract_text(p, doc["extension"] or "", doc["mime_type"])
                if not text.strip():
                    console.print(f"[yellow]Skipping {p.name}: no text extracted[/yellow]")
                    continue
                summary, tags = summarize_local_doc(text)
                _store_analysis(db_path, doc["id"], summary, tags, text, ANALYZER_LOCAL_SUMY)
                ok += 1
            except Exception as exc:
                console.print(f"[red]Error analyzing {p.name}: {exc}[/red]")
                errors += 1
        console.print(f"\n[green]Done.[/green] {ok} analyzed, {errors} errors.")
        return

    if analyzer == ANALYZER_LOCAL_OLLAMA:
        check_ollama_available()
        console.print(
            f"Analyzing [bold]{len(docs)}[/bold] document(s) with [bold]Ollama/{ollama_model}[/bold] (offline)…"
        )
        ok = 0
        errors = 0
        for doc in track(docs, description="Summarizing…"):
            p = pathlib.Path(doc["path"])
            try:
                text, _used_ocr = extract_text(p, doc["extension"] or "", doc["mime_type"])
                if not text.strip():
                    console.print(f"[yellow]Skipping {p.name}: no text extracted[/yellow]")
                    continue
                summary, tags = call_ollama_text(text, model=ollama_model)
                _store_analysis(db_path, doc["id"], summary, tags, text, ANALYZER_LOCAL_OLLAMA)
                ok += 1
            except Exception as exc:
                console.print(f"[red]Error analyzing {p.name}: {exc}[/red]")
                errors += 1
        console.print(f"\n[green]Done.[/green] {ok} analyzed, {errors} errors.")


def run_analyze_compare(
    path: pathlib.Path,
    db_path: pathlib.Path = DB_PATH,
    limit: int = 10,
    photos: bool = False,
    model1: str = "llava:7b",
    model2: str = "gemma4:e4b",
    min_size_mb: float = 0.0,
) -> None:
    """Run analysis on the same files with two models and print a comparison (no DB writes).

    For photos: compares two Ollama vision models side-by-side with per-photo timing.
    For documents: compares Ollama vs Sumy LSA.
    """
    if not path.exists():
        console.print(f"[red]Path does not exist: {path}[/red]")
        raise SystemExit(1)

    if photos:
        check_ollama_available()
        min_size_bytes = int(min_size_mb * 1_000_000)
        rows = _list_compare_photos(path, db_path, limit, min_size_bytes=min_size_bytes)
        if not rows:
            console.print("[yellow]No indexed images found under this path.[/yellow]")
            return
        console.print(
            Panel.fit(f"Comparing Ollama/{model1} vs Ollama/{model2} on {len(rows)} file(s)")
        )
        total_time1 = 0.0
        total_time2 = 0.0
        for row in rows:
            p = pathlib.Path(row["path"])
            label = str(p.relative_to(path)) if p.is_relative_to(path) else str(p)
            console.print(f"\n[bold cyan]{label}[/bold cyan]")
            try:
                start = time.perf_counter()
                s1, t1 = call_ollama_llava(p, model=model1)
                time1 = time.perf_counter() - start
                total_time1 += time1
            except Exception as exc:
                s1, t1 = f"(error: {exc})", []
                time1 = 0.0
            try:
                start = time.perf_counter()
                s2, t2 = call_ollama_llava(p, model=model2)
                time2 = time.perf_counter() - start
                total_time2 += time2
            except Exception as exc:
                s2, t2 = f"(error: {exc})", []
                time2 = 0.0
            _print_compare_row(
                f"Model 1 ({model1})",
                s1,
                t1,
                f"Model 2 ({model2})",
                s2,
                t2,
                time1=time1,
                time2=time2,
            )
        console.print(
            f"\n[bold]Summary:[/bold] Total time — {model1}: {total_time1:.2f}s, {model2}: {total_time2:.2f}s"
        )
        if total_time1 > 0:
            speedup = total_time2 / total_time1
            console.print(f"[cyan]{model2} is {speedup:.1f}x {'faster' if speedup < 1 else 'slower'}[/cyan]")
        return

    check_ollama_available()
    rows = _list_compare_docs(path, db_path, limit)
    if not rows:
        console.print("[yellow]No indexed documents found under this path.[/yellow]")
        return
    ollama_model = model1
    console.print(Panel.fit(f"Comparing Ollama/{ollama_model} vs Sumy (LSA) on {len(rows)} document(s)"))
    for row in rows:
        p = pathlib.Path(row["path"])
        label = str(p.relative_to(path)) if p.is_relative_to(path) else str(p)
        console.print(f"\n[bold cyan]{label}[/bold cyan]")
        text, _ = extract_text(p, row["extension"] or "", row["mime_type"])
        if not text.strip():
            console.print("[dim]No extractable text; skipping.[/dim]")
            continue
        try:
            o_result = call_ollama_text(text, model=ollama_model)
            o_sum, o_tags = o_result["summary"], o_result["tags"]
        except Exception as exc:
            o_sum, o_tags = f"(error: {exc})", []
        l_sum, l_tags = summarize_local_doc(text)
        _print_compare_row(f"Ollama/{ollama_model}", o_sum, o_tags, "Local (local-sumy)", l_sum, l_tags)


def _print_compare_row(
    left_title: str,
    left_s: str,
    left_t: list[str],
    right_title: str,
    right_s: str,
    right_t: list[str],
    time1: Optional[float] = None,
    time2: Optional[float] = None,
) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("", style="dim", width=14)
    table.add_column("Summary", ratio=1)
    table.add_column("Tags", ratio=1)
    if time1 is not None or time2 is not None:
        table.add_column("Time", style="dim", width=8)

    left_time_str = f"{time1:.2f}s" if time1 is not None else "—"
    right_time_str = f"{time2:.2f}s" if time2 is not None else "—"

    if time1 is not None or time2 is not None:
        table.add_row(left_title, left_s or "—", ", ".join(left_t) if left_t else "—", left_time_str)
        table.add_row(right_title, right_s or "—", ", ".join(right_t) if right_t else "—", right_time_str)
    else:
        table.add_row(left_title, left_s or "—", ", ".join(left_t) if left_t else "—")
        table.add_row(right_title, right_s or "—", ", ".join(right_t) if right_t else "—")
    console.print(table)
