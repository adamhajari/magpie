# Digital Magpie

Local-first tool for indexing, analyzing, and pruning multi-TB file collections across external drives. Builds a persistent metadata shadow that stays searchable even after files are deleted or moved to cold storage.

## Setup

```bash
uv sync
```

Apply the SQLite schema (creates `~/.magpie/magpie.db` — safe to re-run):

```bash
uv run python db/migrate.py
```

For photo captioning and face analysis, install [Ollama](https://ollama.com) and pull a vision model:

```bash
brew install ollama
ollama pull llava
```

For OCR on scanned PDFs, install Tesseract:

```bash
brew install tesseract
```

For Google Drive or Dropbox indexing, see [docs/GDRIVE_SETUP.md](docs/GDRIVE_SETUP.md) and the Dropbox section in [docs/QUICK_REFERENCE.md](docs/QUICK_REFERENCE.md).

---

## Drives

Register each physical drive before indexing it. Magpie assigns a stable UUID so the drive stays tracked even if you rename or remount it.

```bash
uv run magpie drives add /Volumes/MyDrive --label "My Drive"
uv run magpie drives list
```

---

## Indexing

Walk a directory and record file metadata, BLAKE3 content hashes, EXIF data, and perceptual hashes. Safe to re-run — unchanged files are skipped.

```bash
uv run magpie index /Volumes/MyDrive
```

See [docs/QUICK_REFERENCE.md](docs/QUICK_REFERENCE.md) for all flags (`--prune-missing`, `--force`, etc.) and Google Drive / Dropbox indexing commands.

---

## Duplicate Detection

Find exact duplicates (using the BLAKE3 hashes) and near-duplicates (using perceptual hash similarity across images). Both of these types of hashes are generated during indexing. Works across local drives, Google Drive, and Dropbox.

```bash
uv run magpie dupes find --exact --near
```

Each group gets a recommended keeper (earliest date, most complete EXIF, largest file). See [docs/QUICK_REFERENCE.md](docs/QUICK_REFERENCE.md) for threshold tuning and listing options. Duplicates are resolved in the Web UI.

---

## Analysis

### Photos

Caption and tag images using a local vision model (Ollama + LLaVA).

```bash
uv run magpie analyze photos /Volumes/MyDrive/Photos
```

### Documents

Summarize PDFs, DOCX, TXT, and Markdown files. Scanned PDFs fall back to OCR.

```bash
uv run magpie analyze docs /Volumes/MyDrive/Documents
```

### Audio

Extract metadata from MP3, FLAC, M4A, WAV, and other audio formats. Embedded cover art is saved as a thumbnail.

```bash
uv run magpie analyze audio /Volumes/MyDrive/Music
```

### Faces

Detect faces and store 512-d embeddings using InsightFace (GPU-accelerated via CoreML on Apple Silicon).

```bash
uv run magpie analyze faces /Volumes/MyDrive/Photos
```

See [docs/QUICK_REFERENCE.md](docs/QUICK_REFERENCE.md) for `--limit`, `--force`, and `--dry-run` options across all analyzers.

---

## Face Matching and Clustering

After labeling a few people in the web UI, automatically assign identities to unidentified faces:

```bash
# Match unidentified faces to labeled people by embedding distance
uv run magpie faces match

# Group remaining unknowns into clusters and generate suggestions
uv run magpie faces cluster
```

See [docs/QUICK_REFERENCE.md](docs/QUICK_REFERENCE.md) for threshold tuning.

---

## Web UI

```bash
uv run magpie serve
```

Opens at `http://localhost:8000`. Pages: dashboard, file browser, photo review, duplicate resolution, face labeling, and the action queue. Stuff will start showing up in the UI as soon as the indexer kicks off. It gets a bit more interesting once you've indexed multiple drives and run `analyze faces`, `faces match`, `faces cluster`, `dupes find`, and labelled a few people.

---

## Execute

Move approved files to their destination. Always defaults to `--dry-run` — pass `--no-dry-run` to actually execute.

```bash
# Preview what would be trashed
uv run magpie execute --action trash --dry-run

# Move approved trash files to ~/.magpie/trash/
uv run magpie execute --action trash --no-dry-run

# Upload cold-labeled files to AWS S3 Glacier Deep Archive
uv run magpie execute /Volumes/MyDrive --action cold --no-dry-run
```

For AWS credential setup, see [docs/COLD_STORAGE_SETUP.md](docs/COLD_STORAGE_SETUP.md). For all executor flags, see [docs/QUICK_REFERENCE.md](docs/QUICK_REFERENCE.md).

---

## Recommended Workflow

1. **Register drives** — `magpie drives add /Volumes/MyDrive --label "My Drive"`
2. **Index** — `magpie index /Volumes/MyDrive --with-thumbs --label-mac-junk`
3. **Find duplicates** — `magpie dupes find --exact --near`
4. **Analyze content** — `analyze photos`, `analyze docs`, `analyze audio`, `analyze faces`
5. **Match and cluster faces** — label a few people in the UI, then `faces match` and `faces cluster`
6. **Review in UI** — browse files and faces (Hot / Cold / Trash), resolve duplicates, explore face clusters
7. **Execute** — `magpie execute --action trash --no-dry-run`, then cold/hot upload commands

---

## Understanding the "Trash" Label

The **"Trash" label is a decision marker, not a destructive action.** When you label a file as "Trash":

- The file **remains on its original disk** — no data is deleted or moved
- The system **ignores it from most operations** (analysis, cloud upload, folder statistics)
- **Files labeled "Trash" will never be uploaded to the cloud**

Think of it like macOS Trash: files are out of the way but still physically exist on your drive. To actually move them, run `magpie execute --action trash --no-dry-run`.

---

## Running Tests

```bash
uv run pytest
```
