# Digital Magpie — RC Presentation Outline

### 1. The Problem (1 slide)
- dozens of hard drives, thumbdrives, and SD cards -- multi-TB, accumulated over years
- Massive duplication from copying drives to computer to drives
- No unified view, no way to know what's safe to delete vs. irreplaceable
- Goal: confidently decide where every file belongs — hot cloud, cold archive, or trash — without touching the originals until you're sure

### 2. Core Idea: Persistent Metadata Shadow (1 slide)
- A SQLite database that outlives the files themselves
- Thumbnails, AI summaries, tags, hashes, duplicate scores all stored separately from the files
- You can disconnect a drive and still browse, search, and reason about its contents

### 3. Architecture Overview (1 slide)
- Four components: **Indexer → Analyzer → UI → Executor**
- No file is ever touched until explicitly approved and the executor is run by hand
- No files are ever deleted from an external drive

### 4. Indexing (1 slide)
- BLAKE3 content fingerprinting (full `b3f:` or partial `b3p:` for big files)
- EXIF extraction, perceptual hashing (pHash) for photos
- Supports local drives, **Google Drive** (OAuth2), and **Dropbox** (PKCE)

### 5. Duplicate Detection (1 slide)
- Exact dupes: same BLAKE3 hash
- Near-dupes: pHash similarity (catches re-compressed social media copies)
- Cross-source deduplication: finds the same file whether it lives on a USB drive or in Google Drive
- "Preferred folder" resolution: automatically decide which copy to keep based on drive/folder rank hierarchy

### 6. AI Analysis (1 slide)
- Local LLM via Ollama (LLaVA) for photo captions — no data leaves the machine
- Face detection with CoreML (GPU-accelerated, ~0.3s/image)
- Document OCR + local LLM summarization
- Results stored in in db; accessible even if the drive is disconnected

### 7. The Trash Label — A Design Decision (1 slide)
- "Trash" is a *decision marker*, not a destructive action
- Files stay exactly where they are on disk; they're just excluded from cloud upload and analysis
- Conceptually: macOS Trash, but you never intend to empty it — you just move *everything else* to the cloud

### 9. Cold Storage Executor (1 slide)
- AWS S3 + Glacier Deep Archive backend (multipart upload, dry-run mode). Very cheap ($1/TB/month), Very slow (24 - 48 hours before you can access)
- Pluggable: switching providers is a config change

### 10. The Web UI (demo)
- FastAPI + HTMX + TailwindCSS
- Mostly used for duplicate resolution and bulk labelling (`keep_hot`, `keep_cold`, or `trash`)

### 11. LLM analysis (demo)

### 12. What I Learned / Interesting Problems (1 slide)
- Lot's of time spent optimizing web application responsiveness
- SQLite at scale
- Partial hashing and perceptual hashing
- Local AI model selection: benchmarked LLaVA vs. gemma4 vs. llava-phi3; gemma4 was 3.4× slower with no quality gain
- How to plan out a large project and parallize with Claude

### 13. What's Next (1 slide)
- add local LLM analysis as part of the UI
- Classifier trained on manual labels to auto-score unlabeled files with explanations
- Video scene analysis
- Execution audit log in the UI
- Possibly: make it generic enough for others with the same problem
