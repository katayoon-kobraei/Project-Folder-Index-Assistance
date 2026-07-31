"""
SQLite connection + schema initialization + write helpers.
"""

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def open_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


def upsert_folder(conn: sqlite3.Connection, row: dict) -> None:
    """Insert a folder row, or overwrite it if that path is already indexed
    (path is UNIQUE — this is what makes rescans idempotent)."""
    conn.execute(
        """
        INSERT INTO folders
            (path, depth, name, year, job_code, job_name, site_code, site_name,
             modified_at, file_count, is_revision_hint)
        VALUES
            (:path, :depth, :name, :year, :job_code, :job_name, :site_code, :site_name,
             :modified_at, :file_count, :is_revision_hint)
        ON CONFLICT(path) DO UPDATE SET
            depth=excluded.depth,
            name=excluded.name,
            year=excluded.year,
            job_code=excluded.job_code,
            job_name=excluded.job_name,
            site_code=excluded.site_code,
            site_name=excluded.site_name,
            modified_at=excluded.modified_at,
            file_count=excluded.file_count,
            is_revision_hint=excluded.is_revision_hint
        """,
        row,
    )


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Repopulate the full-text index from the current contents of `folders`.
    Uses FTS5's built-in 'rebuild' command, which is the safe way to
    resync an external-content table (a manual DELETE + INSERT can corrupt
    the shadow tables)."""
    conn.execute("INSERT INTO folders_fts(folders_fts) VALUES('rebuild')")
    conn.commit()
