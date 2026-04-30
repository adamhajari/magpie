#!/usr/bin/env python3
"""Compare document summarization models: mistral:7b, llama3:8b, phi3.5:3.8b"""

import json
import os
import pathlib
import time
from typing import NamedTuple

import ollama
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Import text extraction from magpie
import sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from magpie.analyzer import extract_text

console = Console()

# Find documents to test (ignore hidden files starting with .)
TEST_DIR = pathlib.Path(os.environ.get("LIBRARIAN_TEST_DOCS", "/Volumes/YourDrive/Documents"))
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}

def find_test_docs(directory: pathlib.Path, limit: int = 5) -> list[pathlib.Path]:
    """Find documents in directory, ignoring hidden files."""
    docs = []
    if not directory.exists():
        return docs

    for ext in SUPPORTED_EXTENSIONS:
        for doc_path in directory.rglob(f"*{ext}"):
            # Skip hidden files (start with .)
            if doc_path.name.startswith("."):
                continue
            docs.append(doc_path)
            if len(docs) >= limit:
                return docs[:limit]

    return docs

TEST_DOCS = find_test_docs(TEST_DIR, limit=5)

MODELS = [
    "mistral",      # mistral:latest = 7B
    "llama2",       # llama2:latest = 7B
    "phi",          # phi:latest = 3.8B
]


class Result(NamedTuple):
    model: str
    doc_path: str
    summary: str
    tags: list[str]
    time_secs: float
    error: str | None


def summarize_with_ollama(text: str, model: str) -> tuple[str, list[str]]:
    """Call Ollama with the given model to summarize text."""
    prompt = """Analyze the following document and respond with valid JSON only.

Return a JSON object with exactly two fields:
- "summary": a 2-3 sentence description of the document's content and purpose
- "tags": a JSON array of 5-10 short descriptive tags (lowercase, no spaces, use hyphens)

Document text:
---
{text}
---

Respond with only the JSON object, no additional text.""".format(text=text[:12_000])

    response = ollama.generate(
        model=model,
        prompt=prompt,
        format="json",
        stream=False,
    )

    raw = response.response.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    data = json.loads(raw)
    summary = str(data.get("summary", ""))
    tags = data.get("tags", [])
    if isinstance(tags, list):
        tags = [str(t) for t in tags]
    else:
        tags = []

    return summary, tags


def test_document(doc_path: pathlib.Path, model: str) -> Result:
    """Test a single model on a single document."""
    console.print(f"  Testing [cyan]{model}[/cyan]...", end=" ")

    # Extract text from document
    try:
        ext = doc_path.suffix.lstrip(".")
        text, _used_ocr = extract_text(doc_path, ext, None)
        if not text.strip():
            console.print("[yellow]no text[/yellow]")
            return Result(
                model=model,
                doc_path=doc_path.name,
                summary="",
                tags=[],
                time_secs=0.0,
                error="No text extracted",
            )
    except Exception as e:
        console.print(f"[yellow]extraction failed: {e}[/yellow]")
        return Result(
            model=model,
            doc_path=doc_path.name,
            summary="",
            tags=[],
            time_secs=0.0,
            error=f"Text extraction failed: {e}",
        )

    if len(text) > 50_000:
        text = text[:50_000] + "\n\n[... truncated ...]"

    try:
        start = time.perf_counter()
        summary, tags = summarize_with_ollama(text, model)
        elapsed = time.perf_counter() - start
        console.print(f"✓ ({elapsed:.2f}s)")
        return Result(
            model=model,
            doc_path=doc_path.name,
            summary=summary,
            tags=tags,
            time_secs=elapsed,
            error=None,
        )
    except Exception as e:
        console.print(f"✗ {e}")
        return Result(
            model=model,
            doc_path=doc_path.name,
            summary="",
            tags=[],
            time_secs=0.0,
            error=str(e),
        )


def main():
    if not TEST_DOCS:
        console.print(f"[red]No documents found in {TEST_DIR}[/red]")
        console.print(f"[yellow]Looking for: {', '.join(SUPPORTED_EXTENSIONS)}[/yellow]")
        raise SystemExit(1)

    console.print(
        Panel.fit(
            f"[bold]Document Summarization Model Comparison[/bold]\n"
            f"Testing {len(MODELS)} models × {len(TEST_DOCS)} document(s)\n"
            f"[dim]Folder: {TEST_DIR}[/dim]"
        )
    )

    # Check Ollama is available
    try:
        ollama.list()
    except Exception as e:
        console.print(f"[red]Ollama not available: {e}[/red]")
        raise SystemExit(1)

    results: list[Result] = []

    for doc_path in TEST_DOCS:
        if not doc_path.exists():
            console.print(f"[yellow]Skipping {doc_path.name}: not found[/yellow]")
            continue

        size_kb = doc_path.stat().st_size / 1024
        console.print(f"\n[bold cyan]{doc_path.name}[/bold cyan] ({size_kb:.1f} KB)")

        for model in MODELS:
            result = test_document(doc_path, model)
            results.append(result)

    # Summary tables
    console.print("\n" + "=" * 100)
    console.print("[bold]TIMING COMPARISON[/bold]")
    console.print("=" * 100)

    timing_table = Table(title="Speed (seconds per document)", show_header=True)
    timing_table.add_column("Model", style="cyan", width=15)
    for doc in TEST_DOCS:
        timing_table.add_column(doc.name, width=12)
    timing_table.add_column("Avg", style="bold", width=10)

    model_times = {model: [] for model in MODELS}
    for model in MODELS:
        row = [model]
        for doc in TEST_DOCS:
            res = next((r for r in results if r.model == model and r.doc_path == doc.name), None)
            if res and res.error is None:
                row.append(f"{res.time_secs:.2f}s")
                model_times[model].append(res.time_secs)
            else:
                row.append("—")
        if model_times[model]:
            avg = sum(model_times[model]) / len(model_times[model])
            row.append(f"{avg:.2f}s")
        else:
            row.append("—")
        timing_table.add_row(*row)

    console.print(timing_table)

    # Quality comparison
    console.print("\n" + "=" * 100)
    console.print("[bold]QUALITY COMPARISON[/bold]")
    console.print("=" * 100)

    for doc in TEST_DOCS:
        console.print(f"\n[bold cyan]{doc.name}[/bold cyan]")
        quality_table = Table(show_header=True, header_style="bold")
        quality_table.add_column("Model", style="cyan", width=15)
        quality_table.add_column("Summary", ratio=2)
        quality_table.add_column("Tags", ratio=1)

        for model in MODELS:
            res = next((r for r in results if r.model == model and r.doc_path == doc.name), None)
            if res:
                if res.error:
                    quality_table.add_row(
                        model,
                        f"[red]ERROR: {res.error}[/red]",
                        "—"
                    )
                else:
                    tags_str = ", ".join(res.tags[:5]) + ("..." if len(res.tags) > 5 else "")
                    quality_table.add_row(
                        model,
                        res.summary,
                        tags_str
                    )

        console.print(quality_table)

    # Recommendations
    console.print("\n" + "=" * 100)
    console.print("[bold]RECOMMENDATIONS[/bold]")
    console.print("=" * 100)

    if model_times:
        avg_times = {m: sum(ts) / len(ts) if ts else float('inf') for m, ts in model_times.items()}
        fastest = min(avg_times.items(), key=lambda x: x[1])
        console.print(f"[green]Fastest:[/green] {fastest[0]} ({fastest[1]:.2f}s avg)")

        for model, avg_time in sorted(avg_times.items(), key=lambda x: x[1]):
            if model != fastest[0]:
                slowdown = avg_time / fastest[1]
                console.print(f"  {model}: {slowdown:.1f}x slower")


if __name__ == "__main__":
    main()
