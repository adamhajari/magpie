# Magpie — Developer Guide

## Project Overview

Local-first tool for indexing, analyzing, and pruning multi-TB file collections across multiple external drives and cloud storage providers. See [docs/PRD.md](docs/PRD.md) for full requirements.

## Layout

```
├── db/
│   ├── schema.sql          # All table definitions (idempotent, IF NOT EXISTS)
│   └── migrate.py          # Applies schema.sql to ~/.magpie/magpie.db
├── docs/
│   ├── PRD.md              # Full product requirements
│   ├── GDRIVE_SETUP.md     # Google Drive OAuth setup guide
│   ├── COLD_STORAGE_SETUP.md  # AWS S3/Glacier setup guide
│   └── QUICK_REFERENCE.md  # CLI quick reference
├── magpie/
│   ├── cli.py              # Typer app; entry point for the `magpie` CLI
│   ├── ui/app.py           # FastAPI web UI
│   ├── indexer.py          # Local drive indexer
│   ├── gdrive_*.py         # Google Drive support
│   ├── dropbox_*.py        # Dropbox support
│   ├── analyzer.py         # Document/photo analysis via local LLM
│   ├── face_*.py           # Face detection, clustering, matching
│   ├── dupes.py            # Duplicate detection (exact + near)
│   └── executor.py         # File action executor
├── scripts/
│   └── backfill_gdrive_phash.py  # One-off migration utilities
├── tests/
├── pyproject.toml
└── CLAUDE.md
```

## Stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Package manager | `uv` |
| Database | SQLite at `~/.magpie/magpie.db` |
| CLI | `typer` |
| Web UI | FastAPI + HTMX + TailwindCSS (CDN) |
| Testing | `pytest` |

## Setup

```bash
uv sync
```

## Running the CLI

```bash
uv run magpie --help
uv run magpie version
```

Or after `uv sync`, the `magpie` script is on the PATH inside the venv:

```bash
.venv/bin/magpie --help
```

## Database Migration

```bash
# Apply schema to ~/.magpie/magpie.db (creates if not exists)
python db/migrate.py

# Apply to a custom path (useful in tests)
python db/migrate.py /path/to/custom.db
```

Migration is idempotent — safe to run multiple times.

## Running Tests

```bash
uv run pytest
# or
uv run pytest -v tests/
```

Tests use a temp database; they never touch `~/.magpie/`.

## Data Model Summary

- **drives** — registered physical drives with stable UUIDs
- **files** — every indexed file; status tracks lifecycle (`present` / `missing` / `archived` / `trashed`)
- **file_analysis** — LLM summaries, tags, OCR output per file
- **duplicate_groups** — exact (same ``content_hash`` / ``b3f:`` or ``b3p:``) and near (pHash) duplicate clusters
- **labels** — user or classifier decisions (`keep_hot` / `keep_cold` / `trash`), with approval state
- **executions** — log of actions taken by the executor process

## Key Conventions

- All IDs are UUIDs stored as TEXT.
- `PRAGMA foreign_keys=ON` and `PRAGMA journal_mode=WAL` are set at connection time.
- The executor never runs automatically — only on explicit `magpie execute` invocation.
- Files are never hard-deleted; they move to `~/.magpie/trash/` first.

## The "Trash" Label

**Important concept:** The "Trash" label is a *decision marker*, not a destructive action. When a file has `labels.action = 'trash'`:

- The **file remains on its original drive** — nothing is deleted or moved yet
- The **system ignores it from most operations**: it won't be analyzed, uploaded to cloud, or counted in folder statistics (unless the trash checkbox is toggled)
- It stays exactly where it is, just flagged as "not relevant" to the workflow
- **Files labeled "Trash" will never be uploaded to the cloud**, even if the user runs the executor

This is conceptually similar to putting a file in macOS Trash: it's "out of the way" but still exists on disk. The difference is that we *never intend to empty* the trash. Instead, trashed files are left on their original drives while actively-managed files get organized and moved to cloud storage.

**For development:** When filtering folders or files, always check `labels.action = 'trash'` to exclude trash-marked files unless the user explicitly wants to see them (e.g., via a "show trash" toggle). Don't rely on `files.status`.
