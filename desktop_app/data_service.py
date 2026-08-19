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

import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

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
from src.project_info.sync_trabajos import (  # noqa: E402
    sync_new_projects_from_trabajos as _sync_new_projects_from_trabajos,
)
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


def _resolve_path(path: str) -> str:
    return path if Path(path).is_absolute() else str(BASE_DIR / path)


def _shared_db_path() -> str | None:
    """The shared-index path from config.yaml (see config.example.yaml's
    "SHARED INDEX MODE" section), resolved to an absolute path — or None
    when it's unset, which is the default fully-local-per-PC behavior."""
    shared = load_config().get("shared_db_path")
    return _resolve_path(shared) if shared else None


def _db_path() -> str:
    """The sqlite file THIS process actually queries. Once shared_db_path
    is configured, every PC (including the one PC that's the builder)
    queries that shared file — db_path is then only ever used as the
    builder's own private local staging file during a crawl (see
    src/webapp/pipeline.py), never queried directly, so a builder PC's own
    screens always show exactly what every other PC sees too."""
    shared = _shared_db_path()
    if shared:
        return shared
    return _resolve_path(load_config()["db_path"])


def _as_bool(value) -> bool:
    """See src/webapp/pipeline.py's identical helper for why this exists
    instead of a plain bool(value) — config.yaml's is_index_builder is
    meant to be a real YAML boolean, but this tolerates it being a
    hand-typed or installer-written quoted string too, since bool("false")
    would otherwise be True."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "si", "sí", "on")
    return bool(value)


def is_index_builder() -> bool:
    """Whether THIS PC is allowed to actually crawl and publish the
    shared index — always True when shared_db_path isn't configured at
    all (the fully-local default, where every PC "builds" its own copy)."""
    config = load_config()
    if not config.get("shared_db_path"):
        return True
    return _as_bool(config.get("is_index_builder", False))


def shared_index_info() -> dict | None:
    """{"built_at": <unix ts>, "built_by_host": <str>} from the shared
    index's own .meta.json sidecar (written by pipeline._publish_db each
    time the builder PC publishes) — or None when shared_db_path isn't
    configured, or the sidecar doesn't exist yet (nothing published so
    far). Used by the UI to show readers when the shared index was last
    actually updated, since their own "Actualizar" button can't tell them
    that by refreshing it themselves."""
    shared = _shared_db_path()
    if not shared:
        return None
    meta_path = Path(shared + ".meta.json")
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


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
    every single search keystroke.

    In shared index mode this deliberately does NOT call open_db() at
    all — open_db() creates the file (and sets it up with WAL, a mode
    that isn't reliable over a network share) if it doesn't already
    exist, which would be actively harmful here: a reader PC opening the
    app before the builder has ever published anything would otherwise
    silently create/initialize an empty database AT the shared path
    itself. Readers only ever get a clear error telling them nothing's
    been published yet."""
    global _schema_ensured
    if _schema_ensured:
        return
    shared = _shared_db_path()
    if shared:
        if not Path(shared).exists():
            raise RuntimeError(
                "El indice compartido todavia no existe en "
                f"{shared}. Pide a quien gestione el PC generador que pulse "
                "'Actualizar' alli al menos una vez."
            )
        _schema_ensured = True
        return
    open_db(_db_path()).close()
    _schema_ensured = True


def get_connection() -> sqlite3.Connection:
    _ensure_schema()
    shared = _shared_db_path()
    if shared:
        # Read-only, immutable URI connection — a reader PC must never be
        # able to write to, lock, or (per _ensure_schema above) create
        # the shared file. immutable=1 additionally tells SQLite this
        # exact file will not change for as long as this connection stays
        # open (true here: pipeline._publish_db always writes a brand new
        # file via os.replace() rather than mutating this one in place),
        # so SQLite skips the locking/change-detection it would otherwise
        # still do even for a plain read-only open.
        #
        # That locking machinery matters here because pipeline.py also
        # deliberately publishes in plain rollback-journal mode, not WAL:
        # a WAL-mode database's read-only opens still need to create/
        # access its -shm shared-memory sidecar for coordination, which a
        # reader that only has read access to the shared folder may not
        # be able to do at all — non-WAL avoids that requirement
        # entirely, and immutable=1 here is belt-and-suspenders on top of
        # that. quote(..., safe="/:") percent-encodes anything that would
        # otherwise be ambiguous in a file: URI (spaces, #, ?, ...) while
        # leaving the path separators and the drive-letter colon (e.g.
        # "P:") untouched.
        uri = "file:" + quote(Path(shared).as_posix(), safe="/:") + "?mode=ro&immutable=1"
        return sqlite3.connect(uri, uri=True)
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


def project_info_file_path(year: int) -> str:
    """Full path to {project_info_dir}/{year}.xlsx — the exact file a
    given year's project_info rows are read from AND written back into
    (see writer.py). Backs the "Abrir Excel" buttons on the Proyectos
    Info page (one per year) and its detail page (one per project, for
    the year that row's own data lives in).

    Raises FileNotFoundError if project_info_dir isn't configured, or
    that year's file doesn't exist — the same failure mode as every
    other project_info_dir access in this module (see _project_info_dir/
    project_info_rows), so callers can show it as a plain error dialog
    the same way."""
    dir_path = _project_info_dir()
    file_path = Path(dir_path) / f"{year}.xlsx"
    if not file_path.exists():
        raise FileNotFoundError(f"No se encuentra el archivo de {year}: {file_path}")
    return str(file_path)


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


def pick_project_info_match(matches: list[dict], folder_name: str | None) -> dict | None:
    """When find_project_info_by_job_code() returns several rows sharing
    the exact same NUMERO DE PROYECTO — routine now that a job/site
    folder with subprojects gets one base row PLUS one extra row per
    subproject folder, all carrying that SAME code (see
    src/project_info/sync_trabajos.py) — pick the ONE row that actually
    corresponds to `folder_name` (the folder just clicked in Buscar),
    instead of blindly opening whichever happens to be first:
      - if folder_name exactly matches one row's "subprojects" value
        (see reader.py's FIELD_MAP — the subproject folder's own name,
        e.g. "MEMORIA URBANIZACION V_2026-04-16"), that row is it;
      - otherwise, if folder_name doesn't match ANY row's subprojects
        value, the folder being looked up must be the site/job folder
        itself (not one of ITS subproject folders) — the base
        combination row, the one with no subprojects value at all, is
        the right one, provided there's exactly one such row.
    Returns None (caller falls back to opening the first match and
    warning there were others) when neither rule narrows it down to
    exactly one row — e.g. folder_name is missing, or the data is
    ambiguous in some other way this doesn't anticipate."""
    if not folder_name:
        return None
    normalized = folder_name.strip().casefold()
    subproject_matches = [
        m for m in matches
        if (m.get("subprojects") or "").strip().casefold() == normalized
    ]
    if len(subproject_matches) == 1:
        return subproject_matches[0]
    if not subproject_matches:
        base_matches = [m for m in matches if not (m.get("subprojects") or "").strip()]
        if len(base_matches) == 1:
            return base_matches[0]
    return None


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


def delete_project_info(item_id: int) -> None:
    """Permanently delete one project_info row from its source xlsx (see
    src/project_info/writer.py's delete_project_info_row — an actual row
    removal, not just clearing cells). Backs the delete button on the
    company projects page (ProyectosInfoCompanyPage) — select a row
    there, delete it, and it's gone from both the UI and the Excel file.

    Raises FileNotFoundError if item_id no longer matches any row (e.g.
    the underlying file already changed since the page was opened) or
    the year's file is missing. No return value: the caller re-reads via
    project_info_rows()/refresh() afterwards, same mtime-based cache
    invalidation as update_project_info()/create_project_info()."""
    row = project_info_detail(item_id)
    if row is None:
        raise FileNotFoundError("Este proyecto ya no está en el archivo de datos.")
    project_info_writer.delete_project_info_row(
        _project_info_dir(), row["year"], row["row_number"]
    )


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


def sync_new_projects_from_trabajos(year: int | None = None) -> dict:
    """Runs src/project_info/sync_trabajos.py's scan against config.yaml's
    "trabajos" root and project_info_dir, appending any missing job/site
    combination to the given year's (defaults to the current calendar
    year) NOMBRE column. See that module's docstring for the exact
    matching rules. Backs the Proyectos Info page's second refresh
    button — distinct from start_refresh()/refresh_status() above, which
    rebuild the crawled P: index instead; this only ever appends rows to
    the hand-maintained project_info xlsx files.

    Raises FileNotFoundError if project_info_dir isn't configured, or
    that year's xlsx file doesn't exist yet."""
    result = _sync_new_projects_from_trabajos(load_config(), _project_info_dir(), year=year)
    return result


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
