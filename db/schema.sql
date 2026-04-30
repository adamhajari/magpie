-- Magpie — SQLite Schema
-- Apply via db/migrate.py. All migrations are idempotent.

CREATE TABLE IF NOT EXISTS drives (
    id          TEXT PRIMARY KEY,           -- stable UUID assigned at first scan
    label       TEXT NOT NULL,              -- human-readable name
    source      TEXT NOT NULL DEFAULT 'local' CHECK (source IN ('local', 'gdrive', 'dropbox')),  -- 'local', 'gdrive', or 'dropbox'
    volume_name TEXT,                       -- OS-reported volume name (local drives)
    serial      TEXT,                       -- drive serial number or Google Drive root folder ID
    total_bytes INTEGER,                    -- drive capacity in bytes
    last_seen_at DATETIME                   -- last time this drive was connected
);

CREATE TABLE IF NOT EXISTS files (
    id              TEXT PRIMARY KEY,       -- stable UUID
    path            TEXT NOT NULL,          -- last known absolute path
    drive_id        TEXT REFERENCES drives(id),
    size_bytes      INTEGER,
    created_at      DATETIME,              -- file creation time (FS or EXIF)
    modified_at     DATETIME,              -- last modified time
    indexed_at      DATETIME NOT NULL,     -- when we indexed it
    content_hash    TEXT,                  -- b3f: full-file / b3p: partial BLAKE3 fingerprint
    perceptual_hash TEXT,                  -- pHash for images
    mime_type       TEXT,                  -- detected MIME type
    extension       TEXT,                  -- file extension (without dot)
    status          TEXT NOT NULL DEFAULT 'present'
                    CHECK (status IN ('present', 'missing', 'archived', 'trashed')),
    face_detection_at DATETIME            -- set when `analyze faces` finished (incl. 0 faces)
);

CREATE INDEX IF NOT EXISTS idx_files_drive_id      ON files(drive_id);
CREATE INDEX IF NOT EXISTS idx_files_content_hash  ON files(content_hash);
CREATE INDEX IF NOT EXISTS idx_files_status        ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_mime_type     ON files(mime_type);
CREATE INDEX IF NOT EXISTS idx_files_path          ON files(path);
CREATE INDEX IF NOT EXISTS idx_files_path_size     ON files(path, size_bytes);
CREATE INDEX IF NOT EXISTS idx_files_size_bytes    ON files(size_bytes DESC);
CREATE INDEX IF NOT EXISTS idx_files_modified_at   ON files(modified_at DESC);

CREATE TABLE IF NOT EXISTS file_analysis (
    file_id      TEXT NOT NULL REFERENCES files(id),
    summary      TEXT,                     -- LLM-generated description (one sentence for photos)
    tags         TEXT,                     -- JSON array of auto-tags
    settings     TEXT,                     -- JSON array of setting categories (photos only)
    people       INTEGER,                  -- count of people visible (photos only, NULL if not applicable)
    text_content TEXT,                     -- OCR output
    quality      INTEGER,                  -- image quality: 1=low, 2=medium, 3=high (photos only, NULL if not applicable)
    analyzer     TEXT NOT NULL,            -- 'local-llava' | 'local-sumy' | 'local-ollama' | 'tesseract'
    analyzed_at  DATETIME NOT NULL,
    PRIMARY KEY (file_id, analyzer)
);

CREATE TABLE IF NOT EXISTS duplicate_groups (
    group_id              TEXT NOT NULL,        -- shared group UUID
    file_id               TEXT NOT NULL REFERENCES files(id),
    match_type            TEXT NOT NULL CHECK (match_type IN ('exact', 'near')),
    confidence            REAL NOT NULL,        -- 0.0–1.0
    recommended_keep      INTEGER NOT NULL DEFAULT 0 CHECK (recommended_keep IN (0, 1)),
    all_labels_approved_at DATETIME,            -- set when all non-keeper files have approved trash labels
    PRIMARY KEY (group_id, file_id)
);

CREATE INDEX IF NOT EXISTS idx_duplicate_groups_file_id ON duplicate_groups(file_id);
-- Index created in migration (not here, since the column may not exist on old databases yet)

CREATE TABLE IF NOT EXISTS preferred_duplicate_paths (
    path_prefix TEXT NOT NULL PRIMARY KEY,    -- absolute path; no trailing slash except root
    rank        INTEGER NOT NULL UNIQUE,      -- priority rank (1 = highest); auto-assigned on insert
    created_at  DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS labels (
    file_id     TEXT NOT NULL REFERENCES files(id),
    action      TEXT NOT NULL CHECK (action IN ('keep_hot', 'keep_cold', 'trash')),
    source      TEXT NOT NULL CHECK (source IN ('user', 'classifier')),
    confidence  REAL,                      -- classifier confidence; NULL if user-assigned
    reasoning   TEXT,                      -- classifier explanation
    labeled_at  DATETIME NOT NULL,
    approved_at DATETIME,                  -- NULL = pending approval
    PRIMARY KEY (file_id)
);

CREATE INDEX IF NOT EXISTS idx_labels_action ON labels(action, file_id);

CREATE TABLE IF NOT EXISTS file_metadata (
    file_id TEXT NOT NULL REFERENCES files(id),
    key     TEXT NOT NULL,
    value   TEXT,
    PRIMARY KEY (file_id, key)
);

CREATE INDEX IF NOT EXISTS idx_file_metadata_file_id ON file_metadata(file_id);

CREATE TABLE IF NOT EXISTS executions (
    id          TEXT PRIMARY KEY,          -- UUID
    file_id     TEXT NOT NULL REFERENCES files(id),
    action      TEXT NOT NULL CHECK (action IN ('upload_hot', 'upload_cold', 'trash')),
    destination TEXT,                      -- cloud path or local trash dir
    executed_at DATETIME NOT NULL,
    verified    INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_executions_file_id ON executions(file_id);

CREATE TABLE IF NOT EXISTS people (
    id         TEXT PRIMARY KEY,            -- UUID
    name       TEXT NOT NULL,
    created_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS face_labels (
    id          TEXT PRIMARY KEY,          -- UUID
    file_id     TEXT NOT NULL REFERENCES files(id),
    person_id   TEXT REFERENCES people(id),
    face_index  INTEGER NOT NULL,
    bbox        TEXT NOT NULL,             -- JSON [x, y, w, h] in pixel coords
    embedding   BLOB NOT NULL,             -- 512 float32 values, L2-normalized (InsightFace buffalo_l)
    source      TEXT NOT NULL CHECK (source IN ('user', 'auto')),
    confidence  REAL,
    labeled_at  DATETIME NOT NULL,
    cluster_id  TEXT                       -- UUID assigned by `magpie faces cluster` for unlabeled faces
);

CREATE INDEX IF NOT EXISTS idx_face_labels_file_id ON face_labels(file_id);
CREATE INDEX IF NOT EXISTS idx_face_labels_person_id ON face_labels(person_id);
CREATE INDEX IF NOT EXISTS idx_face_labels_cluster_id ON face_labels(cluster_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_face_labels_file_face ON face_labels(file_id, face_index);

CREATE TABLE IF NOT EXISTS face_suggestions (
    file_id     TEXT NOT NULL,
    face_index  INTEGER NOT NULL,
    person_id   TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    distance    REAL NOT NULL,
    created_at  DATETIME NOT NULL,
    PRIMARY KEY (file_id, face_index, person_id)
);

CREATE INDEX IF NOT EXISTS idx_face_suggestions_person_id ON face_suggestions(person_id);

-- Temporary table for Google Drive exploration (lightweight preview before full indexing)
-- Populated by `magpie gdrive preview` to understand file distribution
-- Can be cleared and repopulated without affecting the main files table
CREATE TABLE IF NOT EXISTS gdrive_preview (
    id          TEXT PRIMARY KEY,              -- Google Drive file ID
    path        TEXT NOT NULL,                 -- relative path reconstructed from parents
    filename    TEXT NOT NULL,                 -- base filename
    extension   TEXT,                          -- file extension (without dot), NULL for directories
    size_bytes  INTEGER,                       -- file size in bytes, NULL for Google Workspace files
    mime_type   TEXT,                          -- MIME type from Google Drive
    drive_id    TEXT NOT NULL REFERENCES drives(id),  -- which Google Drive account
    scanned_at  DATETIME NOT NULL              -- when this preview was generated
);

CREATE INDEX IF NOT EXISTS idx_gdrive_preview_drive_id ON gdrive_preview(drive_id);
CREATE INDEX IF NOT EXISTS idx_gdrive_preview_extension ON gdrive_preview(extension);
CREATE INDEX IF NOT EXISTS idx_gdrive_preview_path ON gdrive_preview(path);

-- Temporary table for Dropbox exploration (lightweight preview before full indexing)
-- Populated by `magpie dropbox preview` to understand file distribution
-- Can be cleared and repopulated without affecting the main files table
CREATE TABLE IF NOT EXISTS dropbox_preview (
    id          TEXT PRIMARY KEY,              -- Dropbox file ID (id:...)
    path        TEXT NOT NULL,                 -- full path in Dropbox (/path/to/file.ext)
    filename    TEXT NOT NULL,                 -- base filename
    extension   TEXT,                          -- file extension (without dot), NULL for directories
    size_bytes  INTEGER,                       -- file size in bytes
    mime_type   TEXT,                          -- MIME type from Dropbox metadata
    drive_id    TEXT NOT NULL REFERENCES drives(id),  -- which Dropbox account
    scanned_at  DATETIME NOT NULL              -- when this preview was generated
);

CREATE INDEX IF NOT EXISTS idx_dropbox_preview_drive_id ON dropbox_preview(drive_id);
CREATE INDEX IF NOT EXISTS idx_dropbox_preview_extension ON dropbox_preview(extension);
CREATE INDEX IF NOT EXISTS idx_dropbox_preview_path ON dropbox_preview(path);

