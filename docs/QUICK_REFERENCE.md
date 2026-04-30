# CLI Quick Reference

## General

```bash
uv run magpie --help
uv run magpie version
```

---

## Drives

```bash
# List all registered drives
uv run magpie drives list

# Register a drive (auto-detects volume name + serial)
uv run magpie drives add /Volumes/MyDrive --label "My Drive"
```

---

## Index — Local Drives

Walk a directory and populate the database with file metadata, BLAKE3 content hashes, EXIF data, and pHashes. Safe to re-run — unchanged files are skipped.

Hashes are stored in `files.content_hash` with a prefix:
- **`b3f:`** — full-file BLAKE3 (default for files under the partial threshold)
- **`b3p:`** — partial fingerprint (first + last 4 MiB + size) for large files — faster but not byte-for-byte proof

```bash
# Basic index
uv run magpie index /Volumes/MyDrive

# Index and generate thumbnails
uv run magpie index /Volumes/MyDrive --with-thumbs

# Auto-label macOS resource fork files (._*) as trash
uv run magpie index /Volumes/MyDrive --label-mac-junk

# Files larger than 128 MiB use partial b3p: hash (default threshold is 64 MiB)
uv run magpie index /Volumes/MyDrive --partial-hash-above-mib 128

# Always read the entire file for b3f: (slowest; strongest identity)
uv run magpie index /Volumes/MyDrive --partial-hash-above-mib 0

# Remove DB records for files no longer present on disk
uv run magpie index /Volumes/MyDrive --prune-missing

# Skip the initial file count scan (faster start, no ETA)
uv run magpie index /Volumes/MyDrive --no-prescan

# Force recompute pHashes and regenerate thumbnails
uv run magpie index /Volumes/MyDrive --with-thumbs --force

# Associate with a specific drive ID
uv run magpie index /Volumes/MyDrive --drive-id <uuid>
```

---

## Index — Google Drive

See [GDRIVE_SETUP.md](GDRIVE_SETUP.md) for OAuth credential setup. Supports cross-source deduplication with local files.

```bash
# Register and authorize your Google Drive
uv run magpie gdrive add --label "My Drive"

# Index files (downloads and BLAKE3-hashes each file)
uv run magpie gdrive index "My Drive"

# Test mode (limit to first 50 files)
uv run magpie gdrive index "My Drive" --limit 50 --dry-run

# Recompute file paths after folders have been renamed or moved
uv run magpie gdrive recompute-paths "My Drive"
```

Google Workspace files (Docs, Sheets, Slides) are indexed with `content_hash=NULL`.

---

## Index — Dropbox

#### Setup

1. Go to [dropbox.com/developers/apps](https://www.dropbox.com/developers/apps) and create a Scoped Full Dropbox app
2. Save `~/.magpie/dropbox_app.json`:
   ```json
   { "app_key": "...", "app_secret": "..." }
   ```
3. `chmod 600 ~/.magpie/dropbox_app.json`

```bash
# Register and authorize your Dropbox
uv run magpie dropbox add --label "Dropbox"

# Index files (downloads and BLAKE3-hashes each file)
uv run magpie dropbox index "Dropbox"

# Preview metadata without downloading (fast)
uv run magpie dropbox preview "Dropbox" --limit 100

# Download thumbnails for image files
uv run magpie dropbox thumbs "Dropbox"

# Test mode
uv run magpie dropbox index "Dropbox" --limit 50 --dry-run
```

---

## Thumbnails

Generate pHashes and thumbnails for already-indexed files without re-indexing.

```bash
uv run magpie thumbs /Volumes/MyDrive
uv run magpie thumbs /Volumes/MyDrive --no-thumbs   # pHashes only
uv run magpie thumbs /Volumes/MyDrive --force       # regenerate existing
```

Thumbnails are saved to `~/.magpie/thumbs/{file_id}.jpg`.

---

## Duplicate Detection

```bash
# Find exact duplicates (same content_hash)
uv run magpie dupes find --exact

# Find near-duplicate images (perceptual hash, Hamming distance)
uv run magpie dupes find --near

# Adjust similarity threshold (default: 10)
uv run magpie dupes find --near --threshold 8

# Run both in one pass
uv run magpie dupes find --exact --near

# List all duplicate groups
uv run magpie dupes list
```

---

## Analyze: Photos

Caption and tag images using a local vision model (Ollama + LLaVA). Supports JPEG, PNG, WebP, HEIC, RAW.

```bash
uv run magpie analyze photos /Volumes/MyDrive/Photos

# Specific Ollama model
uv run magpie analyze photos /path --ollama-model llava:13b

# Limit, dry-run, force
uv run magpie analyze photos /path --limit 20 --dry-run
uv run magpie analyze photos /path --force
```

---

## Analyze: Documents

Summarize and tag PDFs, DOCX, TXT, and MD files. Scanned PDFs fall back to OCR.

```bash
uv run magpie analyze docs /Volumes/MyDrive/Documents

# Offline mode (no Ollama needed)
uv run magpie analyze docs /path --sumy

# Limit, dry-run, force
uv run magpie analyze docs /path --limit 50 --dry-run
uv run magpie analyze docs /path --force
```

---

## Analyze: Audio

Extract metadata from MP3, M4A, FLAC, WAV, AAC, OGG, AIFF. Embedded cover art is saved as a thumbnail.

```bash
uv run magpie analyze audio /Volumes/MyDrive/Music
uv run magpie analyze audio /path --limit 100 --dry-run --force
```

---

## Analyze: Faces

Detect faces and store 512-d embeddings using InsightFace (GPU-accelerated via CoreML on Apple Silicon). Model (~300 MB) is downloaded automatically on first run.

```bash
uv run magpie analyze faces /Volumes/MyDrive/Photos
uv run magpie analyze faces /path --limit 100 --dry-run --force
```

Face thumbnails are saved to `~/.magpie/faces/{file_id}_{face_index}.jpg`.

---

## Analyze: Compare

Compare two local models side-by-side. Does not write to the database.

```bash
uv run magpie analyze compare /path --limit 10          # documents
uv run magpie analyze compare /path --photos --limit 10 # photos
```

---

## Faces: Match

Assign identities to unidentified faces by nearest labeled prototype (Euclidean distance in 512-d embedding space).

```bash
uv run magpie faces match                    # default threshold 1.0
uv run magpie faces match --threshold 0.9   # stricter
uv run magpie faces match --threshold 1.2   # more permissive
```

Matches are written with `source = auto` and a confidence score.

---

## Faces: Cluster

Group unlabeled faces by similarity and generate suggestions for labeled people.

```bash
uv run magpie faces cluster
uv run magpie faces cluster --match-threshold 1.0 --suggest-threshold 1.4
uv run magpie faces cluster --force   # clear old results and re-cluster
```

---

## Web UI

```bash
uv run magpie serve                  # http://localhost:8000/
uv run magpie serve --port 9000
uv run magpie serve --host 0.0.0.0  # LAN access
uv run magpie serve --reload        # auto-reload on code changes
```

**Pages:**
- `/` — Dashboard: totals, size by type, duplicate summary, label status
- `/files` — File browser: filter by type, drive, label, date range
- `/files/{id}` — Photo review: image, EXIF, tags, summary, face labeling
- `/people` — Faces: named people, unlabeled clusters, similarity suggestions
- `/duplicates` — Duplicate groups: auto-resolve exact or review near-dupes
- `/queue` — Action queue: approved files grouped by destination

---

## Execute

Move approved files to their destination. Always defaults to `--dry-run`.

```bash
# Trash: move to ~/.magpie/trash/
uv run magpie execute --action trash --dry-run
uv run magpie execute --action trash --no-dry-run

# Cold: upload to AWS S3 Glacier Deep Archive (see COLD_STORAGE_SETUP.md)
uv run magpie execute --action cold --dry-run
uv run magpie execute /Volumes/MyDrive --action cold --no-dry-run

# Hot: upload to hot cloud storage
uv run magpie execute --action hot --dry-run
```

The optional `[PATH]` argument limits execution to a single mounted drive. Omit to process all drives.

---

## Cold Storage: AWS Setup

See [COLD_STORAGE_SETUP.md](COLD_STORAGE_SETUP.md) for the full AWS walkthrough (bucket creation, IAM user, access keys).

```bash
# Configure Magpie with your AWS credentials
uv run magpie configure
# Prompts for: AWS profile, region, bucket name
# Writes to: ~/.magpie/config.toml
```

```bash
# Verify credentials
aws sts get-caller-identity --profile magpie

# Check what's been uploaded
aws s3 ls s3://your-bucket/ --profile magpie --recursive
```

---

## Database

SQLite at `~/.magpie/magpie.db`. To use a custom path:

```bash
python db/migrate.py /path/to/custom.db
```

Most CLI commands accept a `--db` option to point at a different file.

---

## Running Tests

```bash
uv run pytest
uv run pytest -v tests/
```

Tests use a temporary database and never touch `~/.magpie/`.
