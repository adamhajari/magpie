# Digital Magpie — Product Requirements Document

## Overview

Digital Magpie is a local-first tool for indexing, analyzing, and pruning several terabytes of files spread across 4–6 external drives. Its core value is a **Persistent Metadata Shadow**: a lightweight database that stays searchable even after source files are deleted, archived to cold storage, or moved. The system surfaces enough context (thumbnails, summaries, tags, duplicate status, classifier scores) for the user to make fast, confident decisions in bulk about where each file belongs: hot cloud, cold cloud, or trash.

---

## Problem

- Files are scattered across 4–6 physical drives (multi-TB total) with no unified view.
- Significant duplication exists from copying drives to drives; some near-duplicates exist from social media re-downloads of original photos.
- No easy way to know what is "safe to delete" vs. unique/important content.
- Moving terabytes to cloud requires categorization that doesn't currently exist.
- The user needs to manually label files, then train a classifier to score unlabeled files and present batch recommendations for approval.

---

## Goals

1. Build a searchable metadata index that outlives the files themselves.
2. Detect exact duplicates (priority) and near-duplicate photos (secondary).
3. Summarize and classify file content (photos and documents primarily; video is future scope).
4. Provide a local web UI to browse files with their metadata, thumbnails, and scores — and mark each file's destination.
5. Run a separate executor process that acts on approved decisions.
6. Train and iterate on a classifier using manually labeled examples.
7. Keep the tool local-first — no mandatory cloud dependency for indexing/analysis.

---

## Non-Goals (v1)

- Video content analysis (indexing metadata/thumbnails is fine; scene analysis is future scope).
- Real-time file watching / continuous sync.
- Full file content search (metadata + AI-generated summaries only).
- Mobile app.
- Multi-user / collaborative features.
- Automatic decision execution without user approval.

---

## Cloud Storage Recommendation

The user currently uses Google Cloud. Options compared:

| Provider | Hot Storage | Cold/Archive | Notes |
|---|---|---|---|
| **Google Cloud Storage** | $0.020/GB/mo (Standard) | $0.004/GB/mo (Archive) | Already in use. Archive retrieval has latency + fee. |
| **Backblaze B2** | $0.006/GB/mo | $0.006/GB/mo (no cold tier) | 3× cheaper than GCS for hot. Free egress to Cloudflare. Simple API. |
| **AWS S3 + Glacier** | $0.023/GB/mo (S3) | $0.004/GB/mo (Glacier) | Most mature. Glacier retrieval takes hours and costs money. |
| **Cloudflare R2** | $0.015/GB/mo | no cold tier | Zero egress fees. Good for files you access frequently. |

**Recommendation:** Use **Backblaze B2** for the bulk of cold/archival storage (photos, documents you rarely access). Keep **GCS** for hot storage if already configured there. R2 is worth considering if you find yourself retrieving files often and paying egress fees. The executor will be built with a pluggable backend so switching providers is a config change.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Digital Magpie                   │
│                                                         │
│  ┌──────────┐  ┌──────────────┐  ┌───────────────────┐ │
│  │ Indexer  │  │   Analyzer   │  │    Classifier     │ │
│  │          │  │              │  │                   │ │
│  │ crawls   │  │ perceptual   │  │ trains on manual  │ │
│  │ drives,  │  │ hash, EXIF,  │  │ labels, scores    │ │
│  │ extracts │  │ OCR, LLM     │  │ unlabeled files,  │ │
│  │ metadata │  │ summaries    │  │ explains reasoning│ │
│  └────┬─────┘  └──────┬───────┘  └────────┬──────────┘ │
│       │               │                   │            │
│       └───────────────▼───────────────────┘            │
│                  ┌──────────┐                           │
│                  │  SQLite  │                           │
│                  │ Database │                           │
│                  └────┬─────┘                           │
│                       │                                 │
│         ┌─────────────┴──────────────┐                 │
│         │                            │                 │
│  ┌──────▼──────┐            ┌────────▼──────┐          │
│  │   Web UI    │            │   Executor    │          │
│  │             │            │               │          │
│  │ browse,     │            │ acts on       │          │
│  │ review,     │            │ approved      │          │
│  │ label,      │            │ decisions:    │          │
│  │ approve     │            │ upload/trash  │          │
│  └─────────────┘            └───────────────┘          │
└─────────────────────────────────────────────────────────┘
```

**Key separation:** The UI is for deciding. The executor is for doing. No file is ever touched until the user approves an action through the UI, and the executor is run explicitly.

---

## Core Data Model

### `drives` table
| Column | Type | Description |
|---|---|---|
| id | TEXT | Stable UUID assigned at first scan |
| label | TEXT | Human-readable name |
| volume_name | TEXT | OS-reported volume name |
| serial | TEXT | Drive serial number (if detectable) |
| total_bytes | INTEGER | Drive capacity |
| last_seen_at | DATETIME | Last time this drive was connected |

### `files` table
| Column | Type | Description |
|---|---|---|
| id | TEXT (UUID) | Stable internal ID |
| path | TEXT | Last known absolute path |
| drive_id | TEXT | FK to drives |
| size_bytes | INTEGER | File size |
| created_at | DATETIME | File creation time (FS or EXIF) |
| modified_at | DATETIME | Last modified time |
| indexed_at | DATETIME | When we indexed it |
| content_hash | TEXT | BLAKE3 fingerprint: `b3f:` full file, or `b3p:` partial (head/tail sample + size) above a size threshold |
| perceptual_hash | TEXT | pHash for images |
| mime_type | TEXT | Detected MIME type |
| extension | TEXT | File extension |
| status | TEXT | `present` / `missing` / `archived` / `trashed` |

### `file_analysis` table
| Column | Type | Description |
|---|---|---|
| file_id | TEXT | FK to files |
| summary | TEXT | LLM-generated description |
| tags | TEXT | JSON array of auto-tags |
| text_content | TEXT | OCR output |
| analyzer | TEXT | `local-llava` / `local-sumy` / `local-ollama` / `tesseract` |
| analyzed_at | DATETIME | When analysis ran |

### `duplicate_groups` table
| Column | Type | Description |
|---|---|---|
| group_id | TEXT | Shared group UUID |
| file_id | TEXT | Member file |
| match_type | TEXT | `exact` / `near` |
| confidence | REAL | 0–1 |
| recommended_keep | BOOLEAN | Whether this copy is the best candidate |

### `labels` table
| Column | Type | Description |
|---|---|---|
| file_id | TEXT | FK to files |
| action | TEXT | `keep_hot` / `keep_cold` / `trash` |
| source | TEXT | `user` / `classifier` |
| confidence | REAL | Classifier confidence (null if user) |
| reasoning | TEXT | Classifier explanation |
| labeled_at | DATETIME | When labeled |
| approved_at | DATETIME | When user approved (null = pending) |

### `people` table
| Column | Type | Description |
|---|---|---|
| id | TEXT | UUID |
| name | TEXT | Human-readable name |
| created_at | DATETIME | When first labeled |

### `face_labels` table
| Column | Type | Description |
|---|---|---|
| id | TEXT | UUID |
| file_id | TEXT | FK to files |
| person_id | TEXT | FK to people (null if unidentified) |
| face_index | INTEGER | Which face in the image (0-based) |
| bbox | TEXT | JSON bounding box `[x, y, w, h]` |
| embedding | BLOB | 128-d face embedding vector |
| source | TEXT | `user` / `auto` |
| confidence | REAL | Match confidence (null if user-labeled) |
| labeled_at | DATETIME | When labeled |

### `executions` table
| Column | Type | Description |
|---|---|---|
| id | TEXT | UUID |
| file_id | TEXT | FK to files |
| action | TEXT | `upload_hot` / `upload_cold` / `trash` |
| destination | TEXT | Cloud path or trash dir |
| executed_at | DATETIME | When done |
| verified | BOOLEAN | Checksum verified post-upload |

---

## Features

### F1 — Indexer
Crawl one or more local drives/directories and Google Drive, and populate the database. Fast, non-destructive, resumable.

**Local Indexing:**
- Extract EXIF from images (date taken, camera, GPS)
- Detect MIME type regardless of extension
- Compute **BLAKE3** content fingerprint (`b3f:` = full file read; `b3p:` = sampled head/tail + size for files above a configurable threshold — CLI `--partial-hash-above-mib`, default 64). Use `0` for always full-file `b3f:`.
- Compute pHash for images
- Track which physical drive each file came from
- Record files that go missing between scans (status → `missing`)
- Generate thumbnails for images and store in a local thumbnail cache

**Google Drive Indexing (F1.1):**
- Register Google Drive via OAuth2 (browser-based authorization flow)
- List all files via Google Drive API with pagination
- Reconstruct relative paths from parent-child relationships
- Download each file and compute BLAKE3 hash (same algorithm as local files → enables cross-source deduplication)
- Handle Google Workspace files (Docs/Sheets/Slides) with `content_hash=NULL` (no binary content)
- Idempotent re-scanning: skip files with matching path, modified time, and existing content hash
- Support multiple Google Drive accounts
- Rate-limit handling: exponential backoff on API 429 responses

### F2 — Duplicate Detector

**Exact duplicates** (priority): match on identical `content_hash` (`b3f:` or `b3p:`). For `b3f:` rows, matching hashes imply identical file bytes. For `b3p:` rows, matching hashes are a strong fingerprint but not a cryptographic guarantee of identical bytes (two different huge files could theoretically collide if they differ only outside the sampled regions). Group and surface total reclaimable space. Recommend keeping the copy with the most complete metadata or earliest creation date.

**Near-duplicates** (images, secondary): pHash comparison with configurable Hamming distance threshold. Catches the same photo at different resolutions or after social-media re-compression. Does not run automatically on every file — triggered explicitly or on a subset.

### F3 — Content Analyzer
Two modes available. **Local mode is the default.**

**Local mode** (default — free, offline): LLaVA via `ollama` for image captioning and tagging; `pytesseract` for OCR; `sumy` for document summarization. No API key required. Fully private and zero cost.

The `analyzer` field in `file_analysis` tracks which model was used. Files can be re-analyzed with a different model using `--force`.

Analysis scope for v1:
- Photos: EXIF, LLM caption, auto-tags, face detection (see F3b)
- Documents (PDF, DOCX, TXT, MD): text extraction, OCR if needed, LLM summary
- Video: EXIF/container metadata and thumbnail only — no content analysis

### F3b — Face Detection & Recognition
People labeling for photos, with automated matching for unlabeled faces.

**Manual labeling**: in the Photo Review UI, detected faces are shown as cropped thumbnails. The user can type a name to label each face. Labels stored in the `people` and `face_labels` tables.

**Automated matching**: a background process compares face embeddings from unlabeled photos against all manually labeled faces. If the best match exceeds a configurable confidence threshold (default: 0.80), an automatic label is written with `source = auto` and the match score recorded. Labels below the threshold are left unlabeled for manual review.

**Libraries**: `face_recognition` (dlib-based, local, no API) for detection and embedding. All processing is local.

### F4 — Classifier
Trains on the user's manual labels and produces `keep_hot / keep_cold / trash` recommendations with confidence scores and reasoning.

**Training data**: labels the user assigns through the UI (target: a few hundred labeled examples).

**Feature set per file**: file type, size, age, EXIF completeness, duplicate status, tags, summary, drive source.

**Model**: start simple — gradient boosted trees (XGBoost/LightGBM) on tabular features, optionally augmented with LLM reasoning on the summary text. Re-train incrementally as new labels are added.

**Output**: for each unlabeled file, a recommended action + confidence score + plain-English explanation. Presented in the UI for user approval. High-confidence batches can be approved in bulk.

### F5 — Web UI
Local FastAPI + HTMX app. The primary interface for reviewing and labeling files.

Views:
- **Dashboard**: total files, size by type, duplicate summary, labeled vs. unlabeled, pending executions
- **File Browser**: filterable/sortable table — by type, drive, date, status, label, score. Shows thumbnail, filename, size, date, tags, classifier score.
- **Photo Review**: full-size photo viewer with metadata panel. Label buttons (hot / cold / trash) always visible. Keyboard shortcuts for fast labeling.
- **Duplicate Review**: side-by-side or grid view of a duplicate group. One click to keep one copy and queue the rest for trash.
- **Action Queue**: all approved labels waiting to be executed. Grouped by action type. Shows total size for each group.

### F6 — Executor (separate process)
Run explicitly by the user — never runs automatically.

- `magpie execute --action trash`: move files to a local trash directory. Never hard-deletes.
- `magpie execute --action upload-hot`: upload to configured hot storage bucket. Verify checksum. Update status.
- `magpie execute --action upload-cold`: upload to configured cold storage. Optionally delete local copy after verified upload.
- All executions logged in the `executions` table.
- `--dry-run` flag on all commands shows what would happen without doing it.
- Progress bar with ETA and bytes transferred.

---

## Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.12 | Rich ecosystem for file ops, ML, and CLI |
| Package manager | `uv` | Fast, modern, lockfile-based |
| Database | SQLite (via SQLAlchemy) | Zero-dependency, local-first, portable |
| Indexer | `os.walk` + `python-magic` + `Pillow` + `blake3` | Fast, no daemon; partial hashing for large files |
| Perceptual hash | `imagehash` | Established pHash/dHash |
| EXIF | `exifread` + `Pillow` | Comprehensive |
| OCR | `pytesseract` | Local, no API key |
| Local vision LLM | LLaVA via `ollama` | Free, offline |
| Classifier | `scikit-learn` / `lightgbm` | Simple, interpretable, fast to retrain |
| Web UI | FastAPI + HTMX + TailwindCSS | No JS build step, server-owns-rendering |
| Cloud storage | `google-cloud-storage` + `boto3` (B2) | Pluggable backends |
| CLI | `typer` | Clean CLI with --help |
| Thumbnails | `Pillow` | Generate at index time, cache to disk |

---

## Task Breakdown

Each task builds on the others but later phases don't block — the database and CLI are usable before the UI exists.

---

### Phase 1 — Foundation

**Task 1.1 — Project Scaffold**
Set up repo structure, `pyproject.toml` with `uv`, `CLAUDE.md`, and a `pytest` baseline. Create the SQLite schema with plain SQL migration files (no Alembic — keep it simple). Define all tables from the data model above.
- Deliverable: `magpie/` package, `db/schema.sql`, `db/migrate.py`, passing `pytest` with one smoke test.

**Task 1.2 — Drive Registry**
Detect and register physical drives. Assign stable `drive_id`s. Detect when a previously-indexed drive is reconnected. Store in the `drives` table.
- Deliverable: `magpie drives list` and `magpie drives add /path` CLI commands.

**Task 1.3 — File Indexer (Local)**
Walk a directory tree and populate the `files` table. Compute BLAKE3 fingerprints (`b3f:` / `b3p:`) and detect MIME types. Extract basic EXIF from images. Mark files that have gone missing since the last scan.
- Deliverable: `magpie index /path/to/drive [--drive-id X]` command. Idempotent re-runs (update existing records, don't duplicate). Progress bar. Optional `--partial-hash-above-mib` / `0` for always full-file hashing.
- **Status: ✅ Complete**

**Task 1.3.1 — Google Drive Indexer** ✅ **COMPLETE**
Index files from registered Google Drive accounts. Use OAuth2 for authentication. List files via Google Drive API, reconstruct paths, download each file, and compute BLAKE3 hash. Support multiple accounts. Idempotent re-scans.
- Deliverable: `magpie drives add --source gdrive` / `magpie gdrive add`, then `magpie gdrive index <drive-id>`. Supports virtual paths `/gdrive/<label>/<path>`. Cross-source deduplication with local files via identical BLAKE3 hashes. Google Workspace files indexed with `content_hash=NULL`.
- **Status: ✅ Complete (Apr 17, 2026)**

**Task 1.4 — Perceptual Hash + Thumbnails**
During indexing (or as a separate pass), compute pHash for all images and generate 256px thumbnails saved to a local cache directory.
- Deliverable: `magpie index --with-thumbs`, thumbnails stored at `~/.magpie/thumbs/{file_id}.jpg`.
- **Status: ✅ Complete**

---

### Phase 2 — Duplicate Detection

**Task 2.1 — Exact Duplicate Finder**
Query files sharing the same `content_hash` string (`b3f:` or `b3p:`). Populate `duplicate_groups` with `match_type=exact`. For each group, mark the recommended keeper (prefer: earliest created_at, most complete EXIF, largest file). Report total reclaimable space.
- Deliverable: `magpie dupes find --exact` with summary table output.

**Task 2.2 — Near-Duplicate Finder (Images)**
Compare pHashes using Hamming distance. Configurable threshold (default: distance ≤ 10). Populate `duplicate_groups` with `match_type=near`. Report groups found.
- Deliverable: `magpie dupes find --near [--threshold 10]`. Handles large image sets without OOM.

---

### Phase 3 — Content Analysis

**Task 3.1 — Document Analyzer**
For PDFs, DOCX, TXT: extract text (with pytesseract OCR fallback for image-PDFs). Generate LLM summary and tags using local Ollama or Sumy LSA. Store in `file_analysis`.
- Deliverable: `magpie analyze docs /path [--dry-run] [--limit N] [--sumy]`.

**Task 3.2 — Photo Analyzer**
For images: generate caption and tags using LLaVA via Ollama. Store in `file_analysis`. Skip files that already have analysis unless `--force`.
- Deliverable: `magpie analyze photos /path [--dry-run] [--limit N] [--ollama-model MODEL]`.

**Task 3.4 — Face Detection & Embedding**
For all indexed images: run face detection using `face_recognition`, extract bounding boxes and 128-d embeddings for each detected face. Store in `face_labels` with `person_id = null` and `source = user` (pending). Generate cropped face thumbnails at `~/.magpie/faces/{file_id}_{face_index}.jpg`.
- Deliverable: `magpie analyze faces /path [--dry-run] [--limit N]`. Skips already-processed files unless `--force`.

**Task 3.5 — Face Matching (Automated)**
Compare embeddings of unidentified faces against all manually-labeled faces. Use Euclidean distance on 128-d embeddings. If the nearest labeled face is within a configurable threshold (default: 0.6 distance, ~0.80 confidence), write an auto label.
- Deliverable: `magpie faces match [--threshold 0.6]`. Reports: faces matched, faces left unidentified, confidence distribution.

---

### Phase 4 — Web UI

**Task 4.1 — FastAPI App Shell + Dashboard**
Set up the FastAPI app with TailwindCSS (CDN, no build step). Dashboard page with stats: total files, total size, files by type, duplicate count, labeled/unlabeled breakdown.
- Deliverable: `magpie serve`, opens at `localhost:8000/`.

**Task 4.2 — File Browser**
Filterable, sortable table of all files. Columns: thumbnail, filename, type, size, date, drive, status, label. Filters: type, drive, label status, date range. Pagination or virtual scroll for 100k+ rows.
- Deliverable: `localhost:8000/files`.

**Task 4.3 — Photo Review View**
Full-size photo viewer. Click a file in the browser to open it. Shows: full image, EXIF panel, tags, summary, duplicate status, classifier score. Label buttons (Hot / Cold / Trash) always visible. Left/right arrow keys to navigate to next/previous file.
- Deliverable: `localhost:8000/files/{id}` with keyboard nav.

**Task 4.4 — Duplicate Review View**
Grid view of a duplicate group (2–8 images). Shows file info under each. "Keep this one" button marks one as keep and queues the rest for trash. Works for both exact and near duplicates.
- Deliverable: `localhost:8000/duplicates` and `localhost:8000/duplicates/{group_id}`.

**Task 4.5b — Face Labeling in Photo Review**
Extend the photo review view (Task 4.3) to show detected faces:
- Below the main image, show a row of cropped face thumbnails (from `~/.magpie/faces/`)
- Each face has a text input pre-filled with the current `person.name` (or blank if unidentified)
- Typing a name and pressing Enter creates or matches a `people` record and updates `face_labels`
- Faces with `source = auto` show their confidence score and are visually distinguished (e.g. dashed border) so the user knows to verify them
- Deliverable: extends `localhost:8000/files/{id}` — no new route needed

**Task 4.5 — Action Queue View**
List of all files with approved labels, grouped by action (Hot / Cold / Trash). Shows total size per group. "Run executor" button triggers `magpie execute` as a subprocess and streams output to the page.
- Deliverable: `localhost:8000/queue`.

---

### Phase 5 — Classifier

**Task 5.1 — Label Export + Feature Engineering**
Export user labels + file features to a training CSV. Features: file type, size bucket, age, has EXIF, has GPS, has analysis, is duplicate, duplicate rank, tags (one-hot or TF-IDF on tag list).
- Deliverable: `magpie classifier export-features --output features.csv`.

**Task 5.2 — Train + Evaluate Classifier**
Train a LightGBM classifier on the exported features. Print classification report, feature importances, and confusion matrix. Save model to `~/.magpie/classifier.pkl`.
- Deliverable: `magpie classifier train [--features features.csv]`. Requires ≥ 50 labeled examples.

**Task 5.3 — Score Unlabeled Files**
Run the trained classifier over all unlabeled files. Write predictions to the `labels` table with `source=classifier`. Show top-N high-confidence recommendations for review.
- Deliverable: `magpie classifier score [--min-confidence 0.85]`.

---

### Phase 6 — Execution

**Task 6.1 — Trash Executor**
Move approved trash files to `~/.magpie/trash/`. Preserve relative path structure. Update `files.status = trashed`. Log to `executions`. Support `--undo` to restore a file.
- Deliverable: `magpie execute trash [--dry-run]`.

**Task 6.2 — Cloud Upload Executor**
Upload approved hot/cold files to configured GCS or B2 bucket. Pluggable via a `StorageBackend` interface. Verify integrity post-upload (e.g. against stored `content_hash` / provider checksum). Update `files.status = archived`. Progress bar.
- Deliverable: `magpie execute upload --action hot|cold [--dry-run]`. Config in `~/.magpie/config.toml`.

---

## Key Design Decisions

**Decision vs. execution are separate.** The UI is for labeling. The executor is a CLI command run explicitly. No file is ever modified by browsing or labeling.

**The "Trash" Label.** Files labeled "Trash" are excluded from most operations (they do not get analyzed, uploaded to cloud, or appear in folder counts unless explicitly toggled). However, they remain physically untouched on their original drives. Think of them like files in macOS Trash: they no longer clutter the filesystem from the system's perspective, but the physical bytes remain on disk. Unlike traditional trash, we **never intend to empty it**—instead, files labeled "Trash" simply never get uploaded to the cloud. This is a decision marker, not a destructive action. The `labels` table tracks this intent independent of file status; once labeled, a file is effectively "ignored" by the system until the label is removed.

**Trash before delete.** Files are moved to a local trash directory, never hard-deleted by the tool. True deletion only happens when the user empties trash manually.

**Classifier is advisory.** It produces recommendations with confidence scores and reasoning. The user always approves. High-confidence batches can be bulk-approved.

**Local-only analysis.** All analysis runs locally via Ollama (LLaVA for photos, configurable model for docs) or offline via Sumy. The `analyzer` field tracks which model was used. Files can be re-analyzed with a different model using `--force`.

**Pluggable storage backends.** GCS and B2 are both supported from day one behind a common interface.

**SQLite is the source of truth.** The metadata shadow persists after files are moved or deleted. The `status` field tracks where a file is in its lifecycle.

**BLAKE3 content fingerprints.** `files.content_hash` stores `b3f:` (full-file BLAKE3) for smaller files and `b3p:` (domain-separated BLAKE3 over size + head/tail samples) above a configurable size threshold, balancing indexing time on multi-TB drives against duplicate semantics. Only `b3f:`/`b3p:` values participate in the indexer’s “unchanged file” fast path.

---

## Success Metrics

- Index 1TB of files in under 2 hours on a modern laptop.
- Exact duplicate detection lists every group of files that share the same `content_hash` (100% of stored fingerprint matches; `b3f:` implies byte-identical among those files).
- UI renders 100k+ file database without noticeable lag.
- Classifier reaches >80% accuracy after 200 manual labels.
- End-to-end: first batch of files moved to cloud within one day of completing setup.
