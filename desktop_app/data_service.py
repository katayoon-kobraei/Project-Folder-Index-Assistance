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
from src.webapp import pipeline  # noqa: E402

CONFIG_PATH = BASE_DIR / "config" / "config.yaml"


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text())


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