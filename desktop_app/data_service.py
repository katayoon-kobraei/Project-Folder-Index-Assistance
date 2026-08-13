"""
Thin data layer for the desktop UI.

Unlike the browser version (src/webapp), this talks to the same
src/search/search.py query functions and src/webapp/pipeline.py refresh
logic directly as plain Python calls — there is no Flask server and no
HTTP round trip involved anywhere. The desktop app reads config/config.yaml
for db_path exactly like the web app does, so both UIs stay in sync
against the same database without any extra setup.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

import yaml  # noqa: E402

from src.db.db import open_db  # noqa: E402
from src.search.search import (  # noqa: E402
    get_location_graph,
    get_ofertas,
    get_project_detail,
    get_project_location_graph,
    get_projects_location_graph,
    list_all_projects,
    list_available_years,
    list_locations,
    search_files,
    search_folders,
    search_projects,
)
from src.project_info.reader import YEARS_WITH_DATA, load_project_info  # noqa: E402
from src.project_info import writer as project_info_writer  # noqa: E402
from src.webapp import pipeline  # noqa: E402

CONFIG_PATH = BASE_DIR / "config" / "config.yaml"


def load_config() -> dict:
    # encoding="utf-8" explicitly — without it, Python falls back to the
    # OS's locale-preferred encoding, which on Windows is often NOT
    # UTF-8 (commonly cp1252). config.yaml itself is saved as UTF-8 (it
    # has accented Spanish text like "AÑOS" in it), so reading it with
    # the wrong encoding silently mangled every accented character
    # instead of raising an error — "AÑOS" came back as "AÃ'OS", which
    # then obviously didn't match any real file on disk.
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def _db_path() -> str:
    db_path = load_config()["db_path"]
    return db_path if Path(db_path).is_absolute() else str(BASE_DIR / db_path)


_schema_ensured = False


def _ensure_schema() -> None:
    """Run schema.sql (via open_db) exactly ONCE per app run, so a
    schema change — a new table, view, or index, like the year indexes
    added for Buscar's filters — takes effect the moment the desktop app
    opens the database, without waiting for the next full crawl to
    (re)create it. Only once, not on every get_connection() call: every
    statement in schema.sql is an idempotent CREATE ... IF NOT EXISTS,
    but re-parsing and re-checking all of them still costs a few tens of
    ms — negligible once at startup, but that adds up fast repeated on
    every single search keystroke."""
    global _schema_ensured
    if _schema_ensured:
        return
    open_db(_db_path()).close()
    _schema_ensured = True


def get_connection() -> sqlite3.Connection:
    _ensure_schema()
    return sqlite3.connect(_db_path())


def search(query: str = "") -> list[dict]:
    """Backs Buscar's project results. Empty query returns nothing —
    consistent with the file results shown alongside it, and less
    confusing than a top-50-most-recent list that doesn't match the
    full, uncapped A-Z list on the Proyectos page. Once you type
    something, every matching project comes back (no cap)."""
    if not query.strip():
        return []
    conn = get_connection()
    try:
        return search_projects(conn, query, limit=None)
    finally:
        conn.close()


def files(
    query: str = "",
    limit: int = 300,
    year: int | None = None,
    company_query: str = "",
) -> list[dict]:
    """Full-text file-name search backing the file results in Buscar,
    optionally narrowed by the same year/company filters as Proyectos.
    Nothing is returned if query/year/company_query are all empty (no
    useful default order over ~600k files)."""
    conn = get_connection()
    try:
        return search_files(conn, query, limit, year, company_query)
    finally:
        conn.close()


def folders(
    query: str = "",
    limit: int = 300,
    year: int | None = None,
    company_query: str = "",
) -> list[dict]:
    """Full-text FOLDER-name search backing Buscar's top results — a
    folder's own name/address, not the resolved project's canonical
    name (see search_folders' docstring for why that distinction
    matters) — optionally narrowed by the same year/company filters as
    Proyectos. Nothing is returned if all three are empty."""
    conn = get_connection()
    try:
        return search_folders(conn, query, limit, year, company_query)
    finally:
        conn.close()


def ofertas(year: int | None = None, company_query: str = "") -> list[dict]:
    """Backs the OFERTAS page: every Firmado/Pedido document found inside
    a '02.-GESTIÓN' folder tree (see get_ofertas' docstring for the
    detection rules), optionally narrowed by the same year/company
    filters as Buscar and Proyectos."""
    conn = get_connection()
    try:
        return get_ofertas(conn, year, company_query)
    finally:
        conn.close()


def _project_info_dir() -> str:
    path = load_config().get("project_info_dir")
    if not path:
        raise FileNotFoundError(
            "No se ha configurado 'project_info_dir' en config/config.yaml."
        )
    return path if Path(path).is_absolute() else str(BASE_DIR / path)


_project_info_cache: dict = {"dir": None, "signature": None, "rows": None}


def _project_info_signature(dir_path: str) -> tuple:
    """One (filename, mtime) pair per year file that actually exists in
    dir_path, in a fixed order — used to detect whether ANY of the
    2024/2025/2026 files has changed (or appeared/disappeared) since the
    last read, without caring which specific one."""
    signature = []
    for year_str in YEARS_WITH_DATA:
        file_path = Path(dir_path) / f"{year_str}.xlsx"
        if file_path.exists():
            signature.append((year_str, file_path.stat().st_mtime))
    return tuple(signature)


def project_info_rows() -> list[dict]:
    """Every project row from the per-year workbook files inside
    project_info_dir (see src/project_info/reader.py for the column
    mapping, the 2024/2025/2026-only scope, and why per-year files
    instead of the single master workbook), backing the Proyectos Info
    page. Cached by directory + a signature of each year file's modified
    time, so opening the page repeatedly doesn't re-parse ~1,300 rows on
    every click — only when at least one of the year files has actually
    changed since the last read, or on the very first call. Raises
    FileNotFoundError (with a message meant to be shown directly to the
    user) if project_info_dir isn't set, doesn't exist, or has none of
    the three expected year files."""
    dir_path = _project_info_dir()
    signature = _project_info_signature(dir_path)
    if not signature:
        raise FileNotFoundError(
            f"No se encontró ningún archivo de año (2024.xlsx, 2025.xlsx, 2026.xlsx) en: {dir_path}\n"
            "Revisa 'project_info_dir' en config/config.yaml, o genera esos archivos con "
            "scripts/split_project_info_by_year.py."
        )
    if _project_info_cache["dir"] != dir_path or _project_info_cache["signature"] != signature:
        _project_info_cache["rows"] = load_project_info(dir_path)
        _project_info_cache["dir"] = dir_path
        _project_info_cache["signature"] = signature
    return _project_info_cache["rows"]


def project_info_detail(item_id: int) -> dict | None:
    for row in project_info_rows():
        if row["id"] == item_id:
            return row
    return None


def find_project_info_by_job_code(job_code: str, preferred_year: int | None = None) -> list[dict]:
    """Every project_info row whose NUMERO DE PROYECTO starts with
    `job_code` — the join key between a folder found in Buscar (its
    crawled job_code/site_code, e.g. "22-007" or "22-007-02", see
    search_folders) and its matching row(s) in Proyectos Info, since
    both use the same "YY-NNN..." numbering. A PREFIX match, not exact:
    the xlsx often appends a per-document suffix the folder's own code
    doesn't have (folder "24-001" should still match rows numbered
    "24-001-01", "24-001-02", ...). Rows from `preferred_year` (the
    folder's own crawled year, when known) sort first, since that's a
    strong hint for which of possibly several same-prefix matches is
    the right one — every match is still returned, not just that
    year's, so the caller can tell the two cases apart."""
    job_code = (job_code or "").strip()
    if not job_code:
        return []
    matches = [
        row for row in project_info_rows()
        if (row.get("project_number") or "").strip().startswith(job_code)
    ]
    if preferred_year is not None:
        matches.sort(key=lambda r: r["year"] != preferred_year)
    return matches


def update_project_info(item_id: int, updates: dict) -> dict:
    """Save edits from the Editar form for one project_info row straight
    into its source cell (see src/project_info/writer.py — only that one
    row/those cells are touched, nothing else in the file changes).
    Raises FileNotFoundError if item_id no longer matches any row (e.g.
    the underlying file changed since the page was opened) or if the
    year's file is missing, and ValueError if a date field is invalid.

    Returns the freshly re-read row for item_id, since editing the file
    changes its mtime — project_info_rows()'s cache is keyed on that, so
    the very next call already reflects the edit, no manual cache
    invalidation needed here."""
    row = project_info_detail(item_id)
    if row is None:
        raise FileNotFoundError("Este proyecto ya no está en el archivo de datos.")
    project_info_writer.update_project_info_row(
        _project_info_dir(), row["year"], row["row_number"], updates
    )
    return project_info_detail(item_id)


def create_project_info(year: int, values: dict) -> dict:
    """Add a brand-new project row to {year}.xlsx (see
    src/project_info/writer.py's append_project_info_row — this only
    ever writes ONE new row, nothing existing is touched) from the
    "Nuevo proyecto" form. Raises FileNotFoundError if that year's file
    doesn't exist, and ValueError if a date field is invalid or NOMBRE
    is blank.

    Returns the freshly re-read row for the new project — same
    mtime-based cache invalidation as update_project_info(), so this
    already reflects reality by the time project_info_rows() runs
    again."""
    project_info_writer.append_project_info_row(_project_info_dir(), year, values)
    rows = project_info_rows()
    # The new row is always the last one for this year in sheet order —
    # load_project_info() reads top-to-bottom and this was just appended
    # at the very bottom of the sheet.
    year_rows = [r for r in rows if r["year"] == year]
    return year_rows[-1]


def all_projects(name_query: str = "", year: int | None = None) -> list[dict]:
    """Full A-Z project list (no row cap), optionally filtered by name
    and/or by a year the project actually has a linked folder in.
    Backs the Proyectos page — distinct from search(), which is capped
    and sorted by recency for the quick-search box."""
    conn = get_connection()
    try:
        return list_all_projects(conn, name_query, year)
    finally:
        conn.close()


def available_years() -> list[int]:
    conn = get_connection()
    try:
        return list_available_years(conn)
    finally:
        conn.close()


def project_detail(project_id: int) -> dict | None:
    conn = get_connection()
    try:
        return get_project_detail(conn, project_id)
    finally:
        conn.close()


def project_location_graph(project_id: int) -> dict:
    """Combined graph of every location linked to this project, for
    inline display on the project detail page — {"elements": []} (not
    None) when the project has no detected locations, since that's a
    normal state, not an error."""
    conn = get_connection()
    try:
        return get_project_location_graph(conn, project_id)
    finally:
        conn.close()


def projects_location_graph(name_query: str = "", year: int | None = None) -> dict:
    """Combined location graph for whatever the Proyectos page's filters
    currently match — kept in sync with all_projects() by taking the
    same name_query/year arguments. {"elements": []} when nothing
    matches or none of the matches have a detected location."""
    conn = get_connection()
    try:
        return get_projects_location_graph(conn, name_query, year)
    finally:
        conn.close()


def locations() -> list[dict]:
    conn = get_connection()
    try:
        return list_locations(conn)
    finally:
        conn.close()


def location_graph(location_id: int) -> dict | None:
    conn = get_connection()
    try:
        return get_location_graph(conn, location_id)
    finally:
        conn.close()


def summary_counts() -> dict:
    """Numbers for the Resumen page's metric cards."""
    conn = get_connection()
    try:
        n_projects = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        n_folders = conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
        n_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        n_locations = conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0]
        year_min, year_max = conn.execute(
            "SELECT MIN(first_seen_year), MAX(last_seen_year) FROM projects"
        ).fetchone()
        return {
            "projects": n_projects,
            "folders": n_folders,
            "files": n_files,
            "locations": n_locations,
            "year_min": year_min,
            "year_max": year_max,
        }
    finally:
        conn.close()


def start_refresh() -> bool:
    """Returns False if a refresh is already running (mirrors
    pipeline.start_refresh — same shared background-thread state whether
    it's triggered from the browser or the desktop app)."""
    return pipeline.start_refresh(load_config())


def refresh_status() -> dict:
    return pipeline.get_status()