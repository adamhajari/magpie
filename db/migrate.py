"""
Apply db/schema.sql to the magpie SQLite database.

Usage:
    python db/migrate.py

The database is created at ~/.magpie/magpie.db if it does not exist.
Running this script multiple times is safe — all DDL uses IF NOT EXISTS.
"""

import pathlib
import sqlite3
import sys

DB_DIR = pathlib.Path.home() / ".magpie"
DB_PATH = DB_DIR / "magpie.db"
SCHEMA_PATH = pathlib.Path(__file__).parent / "schema.sql"


def _ensure_files_face_detection_column(conn: sqlite3.Connection) -> None:
    """Add face_detection_at to existing databases (CREATE TABLE IF NOT EXISTS does not alter)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    if "face_detection_at" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN face_detection_at DATETIME")


def _ensure_file_metadata_table(conn: sqlite3.Connection) -> None:
    """Create file_metadata if it doesn't exist yet (for databases predating the table)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS file_metadata (
            file_id TEXT NOT NULL REFERENCES files(id),
            key     TEXT NOT NULL,
            value   TEXT,
            PRIMARY KEY (file_id, key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_metadata_file_id ON file_metadata(file_id)"
    )


def _ensure_files_audio_columns(conn: sqlite3.Connection) -> None:
    """Add audio-specific columns to the files table if they don't exist yet."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    if "duration_seconds" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN duration_seconds REAL")
    if "sample_rate" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN sample_rate INTEGER")
    if "bit_rate" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN bit_rate INTEGER")


def _ensure_duplicate_groups_resolution_column(conn: sqlite3.Connection) -> None:
    """Add all_labels_approved_at column to duplicate_groups if it doesn't exist yet."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(duplicate_groups)")}
    if "all_labels_approved_at" not in cols:
        conn.execute("ALTER TABLE duplicate_groups ADD COLUMN all_labels_approved_at DATETIME")
        # Create the index for efficient queries
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_duplicate_groups_resolution ON duplicate_groups(all_labels_approved_at)"
        )
        # Create the trigger that resets resolution when new members are added
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_reset_resolution_on_new_member
            AFTER INSERT ON duplicate_groups
            FOR EACH ROW
            BEGIN
              UPDATE duplicate_groups
              SET all_labels_approved_at = NULL
              WHERE group_id = NEW.group_id;
            END
            """
        )


def _ensure_preferred_duplicate_paths_table(conn: sqlite3.Connection) -> None:
    """Store folder prefixes used to auto-pick duplicate keepers (in-folder vs out-of-folder)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS preferred_duplicate_paths (
            path_prefix TEXT NOT NULL PRIMARY KEY,
            rank        INTEGER NOT NULL UNIQUE,
            created_at  DATETIME NOT NULL
        )
        """
    )


def _ensure_preferred_duplicate_paths_rank_column(conn: sqlite3.Connection) -> None:
    """Add rank column to preferred_duplicate_paths if it doesn't exist yet.

    For existing rows, assign ranks based on creation order (earliest = rank 1).
    Also removes UNIQUE constraint on rank if it exists (from old migrations).
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(preferred_duplicate_paths)")}
    if "rank" not in cols:
        # Add the rank column without UNIQUE
        conn.execute("ALTER TABLE preferred_duplicate_paths ADD COLUMN rank INTEGER")

        # Get existing rows ordered by created_at (earliest first = highest priority)
        rows = conn.execute(
            "SELECT path_prefix FROM preferred_duplicate_paths ORDER BY created_at ASC"
        ).fetchall()

        # Assign ranks 1, 2, 3, ...
        for idx, (path_prefix,) in enumerate(rows, start=1):
            conn.execute(
                "UPDATE preferred_duplicate_paths SET rank = ? WHERE path_prefix = ?",
                (idx, path_prefix),
            )

    # Check if table has UNIQUE constraint on rank (from old migration) and remove it
    create_table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='preferred_duplicate_paths'"
    ).fetchone()

    if create_table_sql and create_table_sql[0] and "UNIQUE" in create_table_sql[0]:
        # Recreate table without UNIQUE constraint
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            """
            CREATE TABLE preferred_duplicate_paths_new (
                path_prefix TEXT NOT NULL PRIMARY KEY,
                rank        INTEGER NOT NULL,
                created_at  DATETIME NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO preferred_duplicate_paths_new
            SELECT path_prefix, rank, created_at FROM preferred_duplicate_paths
            """
        )
        conn.execute("DROP TABLE preferred_duplicate_paths")
        conn.execute("ALTER TABLE preferred_duplicate_paths_new RENAME TO preferred_duplicate_paths")
        conn.execute("PRAGMA foreign_keys=ON")


def _reset_face_data_for_insightface(conn: sqlite3.Connection) -> None:
    """Drop all face_labels and people rows when migrating from dlib (128-d) to InsightFace (512-d).

    Embeddings are incompatible across backends so a clean slate is required.
    Only runs once: skipped if face_labels is already empty.
    """
    count = conn.execute("SELECT COUNT(*) FROM face_labels").fetchone()[0]
    if count == 0:
        return
    # Check if any stored embedding is 128-d (512 bytes) vs 512-d (2048 bytes).
    # If so, wipe and reset face_detection_at so images are re-processed.
    row = conn.execute("SELECT embedding FROM face_labels LIMIT 1").fetchone()
    if row and len(row[0]) == 128 * 4:  # 128 float32 = 512 bytes → old dlib data
        conn.execute("DELETE FROM face_labels")
        conn.execute("DELETE FROM people")
        conn.execute("UPDATE files SET face_detection_at = NULL")


def _ensure_face_labels_cluster_id_column(conn: sqlite3.Connection) -> None:
    """Add cluster_id column to face_labels if it doesn't exist yet."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(face_labels)")}
    if "cluster_id" not in cols:
        conn.execute("ALTER TABLE face_labels ADD COLUMN cluster_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_face_labels_cluster_id ON face_labels(cluster_id)")


def _ensure_face_suggestions_table(conn: sqlite3.Connection) -> None:
    """Create face_suggestions table if it doesn't exist yet."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS face_suggestions (
            file_id     TEXT NOT NULL,
            face_index  INTEGER NOT NULL,
            person_id   TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
            distance    REAL NOT NULL,
            created_at  DATETIME NOT NULL,
            PRIMARY KEY (file_id, face_index, person_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_face_suggestions_person_id ON face_suggestions(person_id)"
    )


def _ensure_drives_source_column(conn: sqlite3.Connection) -> None:
    """Add source column to drives table if it doesn't exist yet."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(drives)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE drives ADD COLUMN source TEXT NOT NULL DEFAULT 'local'")
        # Ensure all existing rows are marked as 'local' (they are)
        conn.execute("UPDATE drives SET source = 'local' WHERE source IS NULL")


def _ensure_drives_source_dropbox(conn: sqlite3.Connection) -> None:
    """Update drives.source CHECK constraint to include 'dropbox' if not present."""
    create_table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='drives'"
    ).fetchone()

    if create_table_sql and create_table_sql[0] and "'dropbox'" not in create_table_sql[0]:
        # Recreate table with updated CHECK constraint
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            """
            CREATE TABLE drives_new (
                id          TEXT PRIMARY KEY,
                label       TEXT NOT NULL,
                source      TEXT NOT NULL DEFAULT 'local' CHECK (source IN ('local', 'gdrive', 'dropbox')),
                volume_name TEXT,
                serial      TEXT,
                total_bytes INTEGER,
                last_seen_at DATETIME
            )
            """
        )
        conn.execute(
            """
            INSERT INTO drives_new
            SELECT id, label, source, volume_name, serial, total_bytes, last_seen_at
            FROM drives
            """
        )
        conn.execute("DROP TABLE drives")
        conn.execute("ALTER TABLE drives_new RENAME TO drives")
        conn.execute("PRAGMA foreign_keys=ON")


def _ensure_file_analysis_photo_columns(conn: sqlite3.Connection) -> None:
    """Add settings and people columns to file_analysis for photo analysis."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(file_analysis)")}
    if "settings" not in cols:
        conn.execute("ALTER TABLE file_analysis ADD COLUMN settings TEXT")
    if "people" not in cols:
        conn.execute("ALTER TABLE file_analysis ADD COLUMN people INTEGER")


def _ensure_file_analysis_quality_column(conn: sqlite3.Connection) -> None:
    """Add quality column to file_analysis for image quality assessment."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(file_analysis)")}
    if "quality" not in cols:
        conn.execute("ALTER TABLE file_analysis ADD COLUMN quality INTEGER")


def _ensure_files_path_drive_unique(conn: sqlite3.Connection) -> None:
    """Add UNIQUE(path, drive_id) index to prevent duplicate indexing of the same file.

    Before creating the index, removes duplicate rows caused by a race condition in
    earlier indexer versions (concurrent runs could insert the same path twice with
    different UUIDs). Keeps the row with the latest indexed_at; cascades deletes to
    dependent tables via explicit cleanup.
    """
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_files_path_drive'"
    ).fetchone()
    if existing:
        return

    # Find all duplicate (path, drive_id) groups — keep the one with latest indexed_at
    dups = conn.execute(
        """
        SELECT id FROM files
        WHERE (path, drive_id) IN (
            SELECT path, drive_id FROM files
            GROUP BY path, drive_id HAVING COUNT(*) > 1
        )
        AND id NOT IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY path, drive_id ORDER BY indexed_at DESC, id ASC
                ) AS rn
                FROM files
            ) WHERE rn = 1
        )
        """
    ).fetchall()

    if dups:
        ids_to_delete = [row[0] for row in dups]
        placeholders = ",".join("?" * len(ids_to_delete))
        # Clean up dependent rows first (FK constraints)
        for table in ("file_analysis", "file_metadata", "duplicate_groups",
                      "labels", "face_labels", "face_suggestions", "executions"):
            conn.execute(
                f"DELETE FROM {table} WHERE file_id IN ({placeholders})", ids_to_delete
            )
        conn.execute(f"DELETE FROM files WHERE id IN ({placeholders})", ids_to_delete)
        print(f"  Removed {len(ids_to_delete)} duplicate file rows")

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_files_path_drive ON files(path, drive_id)"
    )


def migrate(db_path: pathlib.Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)

    schema = SCHEMA_PATH.read_text()

    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema)
        _ensure_files_face_detection_column(conn)
        _ensure_file_metadata_table(conn)
        _ensure_files_audio_columns(conn)
        _ensure_duplicate_groups_resolution_column(conn)
        _ensure_preferred_duplicate_paths_table(conn)
        _ensure_preferred_duplicate_paths_rank_column(conn)
        _reset_face_data_for_insightface(conn)
        _ensure_face_labels_cluster_id_column(conn)
        _ensure_face_suggestions_table(conn)
        _ensure_drives_source_column(conn)
        _ensure_drives_source_dropbox(conn)
        _ensure_file_analysis_photo_columns(conn)
        _ensure_file_analysis_quality_column(conn)
        _ensure_files_path_drive_unique(conn)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.commit()

    print(f"Migration applied → {db_path}")


if __name__ == "__main__":
    target = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DB_PATH
    migrate(target)
