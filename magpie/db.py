"""Shared database connection helpers."""

import pathlib
import sqlite3

DB_DIR = pathlib.Path.home() / ".magpie"
DB_PATH = DB_DIR / "magpie.db"

_MIGRATE_HINT = (
    "The database file exists but the schema has not been applied. "
    "From the project root, run:\n\n"
    "    uv run python db/migrate.py\n\n"
    "Or: python db/migrate.py /path/to/magpie.db\n\n"
    "Migration is idempotent and safe to repeat."
)


def _core_schema_present(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='drives' LIMIT 1"
    ).fetchone()
    return row is not None


def get_connection(db_path: pathlib.Path = DB_PATH) -> sqlite3.Connection:
    """Return a sqlite3 connection with WAL mode and foreign keys enabled.

    Raises RuntimeError with migration instructions if the file is missing
    core tables (typical when db/migrate.py has never been run).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    if not _core_schema_present(conn):
        conn.close()
        raise RuntimeError(_MIGRATE_HINT)
    return conn
