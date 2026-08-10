"""
Query helpers backing the web UI: project search, project detail
(timeline), and location detail (graph). Pure functions over a sqlite3
connection — no Flask here, so these are independently testable.
"""


def search_projects(conn, query: str = "", limit: int | None = 50):
    """List projects, optionally filtered by name. Most recently active
    first. `query` empty returns everything (capped at `limit`). Pass
    `limit=None` for no cap at all — used by Buscar's project search,
    which (unlike this default) shows nothing for an empty query and
    every match once you type, so a cap would just hide real results."""
    where = "WHERE canonical_name LIKE ?" if query else ""
    params: list = [f"%{query}%"] if query else []
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT id, canonical_name, first_seen_year, last_seen_year, status
        FROM projects
        {where}
        ORDER BY last_seen_year DESC, canonical_name
        {limit_sql}
        """,
        params,
    ).fetchall()
    return [
        {
            "id": r[0],
            "canonical_name": r[1],
            "first_seen_year": r[2],
            "last_seen_year": r[3],
            "status": r[4],
        }
        for r in rows
    ]


def _fts_prefix_query(text: str) -> str | None:
    """Turn free-typed text into an FTS5 MATCH expression: each word
    becomes a quoted prefix match, ANDed together (FTS5's default). Every
    token is double-quoted (with internal quotes doubled per FTS5's
    escaping rule) before the trailing '*' — real filenames are full of
    characters that break bareword FTS5 syntax otherwise (a hyphenated
    job code like '14-002' raises 'no such column: 002', and something
    like '(obra)' raises a straight syntax error). Returns None for an
    empty/whitespace-only query."""
    tokens = text.split()
    if not tokens:
        return None
    return " ".join('"' + t.replace('"', '""') + '"*' for t in tokens)


def search_files(
    conn,
    query: str = "",
    limit: int = 300,
    year: int | None = None,
    company_query: str = "",
):
    """Full-text search over every indexed FILE name (not just project
    names), via the files_fts FTS5 index that's rebuilt after every
    crawl — this is what lets Buscar find an individual file instead of
    only a company/project, and stays fast even across ~600k files
    (single-digit milliseconds, vs. a plain LIKE '%...%' table scan).
    Optionally narrowed further by an exact `year` (file_year) and/or a
    `company_query` substring against company_project — same filters
    Buscar's Proyectos-style filter row offers, applied on top of the
    text search here rather than replacing it.

    Deliberately NOT ordered by year/name: for a broad term like "pdf"
    that matches 100k+ files, sorting the full matching set before
    applying LIMIT was the single slowest step measured anywhere in this
    app (a few hundred ms, vs. under 20ms without it) — SQLite has to
    materialize and sort every match, not just the ones returned, since
    the join columns aren't something FTS5 can pre-sort by. Results come
    back in whatever order SQLite finds them; a narrower query (which is
    also just more useful) naturally comes back near-instant either way.

    Returns nothing if query/year/company_query are ALL empty — unlike
    search_projects, dumping the first `limit` of ~600k files in no
    meaningful order isn't useful."""
    match_expr = _fts_prefix_query(query)
    if match_expr is None and year is None and not company_query:
        return []

    conditions = []
    params: list = []
    if match_expr is not None:
        # A subquery, not a JOIN against files_fts directly: combining an
        # FTS5 MATCH with an indexed equality filter (year, once
        # idx_files_file_year exists) can make SQLite's planner drive the
        # query off the year index instead of the FTS index, falling
        # back to an FTS SCAN per candidate row — 7+ seconds for a query
        # that resolves in ~2ms on its own. Computing the MATCH as its
        # own subquery first keeps it on the fast, dedicated FTS index
        # no matter what other filters get combined with it.
        conditions.append("f.id IN (SELECT rowid FROM files_fts WHERE files_fts MATCH ?)")
        params.append(match_expr)
    if year is not None:
        conditions.append("f.file_year = ?")
        params.append(year)
    if company_query:
        conditions.append("f.company_project LIKE ?")
        params.append(f"%{company_query}%")
    where_sql = " AND ".join(conditions)
    params.append(limit)

    rows = conn.execute(
        f"""
        SELECT f.id, f.name, f.extension, f.path, f.company_project,
               f.file_year, f.location_site, fo.source
        FROM files f
        JOIN folders fo ON fo.id = f.folder_id
        WHERE {where_sql}
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [
        {
            "id": r[0],
            "name": r[1],
            "extension": r[2],
            "path": r[3],
            "company_project": r[4],
            "file_year": r[5],
            "location_site": r[6],
            "source": r[7],
        }
        for r in rows
    ]


def search_folders(
    conn,
    query: str = "",
    limit: int = 300,
    year: int | None = None,
    company_query: str = "",
):
    """Full-text search over every indexed FOLDER's own name — via
    folders_fts, rebuilt after every crawl — instead of the resolved
    projects table. This is what Buscar's top results use now: a
    site/address folder like '25-003-02 CL BENITO PEREZ GALDÓS 68
    ALZIRA' never appears in a resolved project's canonical name (that
    project might just be called 'PLENOIL'), so searching projects.
    canonical_name alone missed it entirely — searching every folder's
    own name finds it directly, at every depth. Optionally narrowed
    further by an exact `year` and/or a `company_query` substring
    against company_project, same as search_files.

    Deliberately restricted to the `name` column only, via FTS5's
    `name: ...` column filter — folders_fts also indexes job_name and
    site_name, but those are inherited down to every descendant folder
    during the crawl (see crawl.py), so matching against them would pull
    in every single subfolder under a matching site/company (e.g. a
    plain "00.-PLANOS" folder just because it happens to sit under a
    site named after the searched street) even though that subfolder's
    own name has nothing to do with the query. Job/site context for a
    match is still shown via the company_project/location_site columns
    below — just not used to decide whether something matches at all.

    Returns nothing if query/year/company_query are ALL empty, and
    results are deliberately unordered — same reasoning as search_files
    (sorting the full matching set before LIMIT was the actual slow part
    for a broad term, not the FTS match itself)."""
    match_expr = _fts_prefix_query(query)
    if match_expr is None and year is None and not company_query:
        return []

    conditions = []
    params: list = []
    if match_expr is not None:
        # Subquery, not a direct JOIN — same reasoning as search_files:
        # combining an FTS5 MATCH with the year index directly in one
        # query let SQLite's planner drive off the year index and fall
        # back to a slow per-row FTS scan instead of using the FTS
        # index properly (measured 0.73s vs. under 1ms restructured this
        # way for the exact same query).
        conditions.append("f.id IN (SELECT rowid FROM folders_fts WHERE folders_fts MATCH 'name: ' || ?)")
        params.append(match_expr)
    if year is not None:
        conditions.append("f.year = ?")
        params.append(year)
    if company_query:
        conditions.append("f.company_project LIKE ?")
        params.append(f"%{company_query}%")
    where_sql = " AND ".join(conditions)
    params.append(limit)

    rows = conn.execute(
        f"""
        SELECT f.id, f.name, f.path, f.company_project, f.year, f.location_site, f.source
        FROM folders f
        WHERE {where_sql}
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [
        {
            "id": r[0],
            "name": r[1],
            "path": r[2],
            "company_project": r[3],
            "year": r[4],
            "location_site": r[5],
            "source": r[6],
        }
        for r in rows
    ]


def list_all_projects(conn, name_query: str = "", year: int | None = None):
    """Every project matching the given filters, A-Z by name, with NO row
    cap — unlike search_projects() (which is capped at `limit` and sorted
    by recency, meant for a quick-search box), this backs a full browsable
    list. When `year` is given, a project only matches if it actually has
    a linked folder in that exact year — NOT just if the year falls
    somewhere between first_seen_year and last_seen_year, since a project
    active in 2014 and 2019 but with nothing in between should not show
    up as "active" for 2016."""
    conditions = []
    params: list = []
    if name_query:
        conditions.append("p.canonical_name LIKE ?")
        params.append(f"%{name_query}%")
    if year is not None:
        conditions.append(
            "EXISTS (SELECT 1 FROM project_links pl JOIN folders f ON f.id = pl.folder_id "
            "WHERE pl.project_id = p.id AND f.year = ?)"
        )
        params.append(year)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = conn.execute(
        f"""
        SELECT p.id, p.canonical_name, p.first_seen_year, p.last_seen_year, p.status
        FROM projects p
        {where_clause}
        ORDER BY p.canonical_name COLLATE NOCASE
        """,
        params,
    ).fetchall()
    return [
        {
            "id": r[0],
            "canonical_name": r[1],
            "first_seen_year": r[2],
            "last_seen_year": r[3],
            "status": r[4],
        }
        for r in rows
    ]


def list_available_years(conn):
    """Every distinct year that actually has at least one linked project
    folder, ascending — used to populate the year filter so it only ever
    offers years that will actually return results."""
    rows = conn.execute(
        """
        SELECT DISTINCT f.year
        FROM project_links pl
        JOIN folders f ON f.id = pl.folder_id
        WHERE f.year IS NOT NULL
        ORDER BY f.year
        """
    ).fetchall()
    return [r[0] for r in rows]


def get_project_location_graph(conn, project_id: int):
    """Cytoscape-ready graph for the project detail page: every location
    linked to this project, plus every OTHER project each of those
    locations is also linked to — so you can see this project's place in
    the location graph without leaving its own page. Each project node
    carries an `is_current` flag (True only for `project_id` itself) so
    the UI can highlight it. Returns {"elements": []} if this project has
    no detected locations (not None — an empty graph is a normal,
    expected state, not an error)."""
    locations = conn.execute(
        """
        SELECT l.id, l.name
        FROM location_links ll
        JOIN locations l ON l.id = ll.location_id
        WHERE ll.project_id = ?
        ORDER BY l.name
        """,
        (project_id,),
    ).fetchall()
    if not locations:
        return {"elements": []}

    nodes: dict[str, dict] = {}
    edges = []
    for location_id, location_name in locations:
        location_node_id = f"location-{location_id}"
        nodes[location_node_id] = {
            "data": {"id": location_node_id, "label": location_name, "type": "location"}
        }
        linked_projects = conn.execute(
            """
            SELECT p.id, p.canonical_name
            FROM location_links ll
            JOIN projects p ON p.id = ll.project_id
            WHERE ll.location_id = ?
            ORDER BY p.canonical_name
            """,
            (location_id,),
        ).fetchall()
        for pid, pname in linked_projects:
            project_node_id = f"project-{pid}"
            nodes[project_node_id] = {
                "data": {
                    "id": project_node_id,
                    "label": pname,
                    "type": "project",
                    "project_id": pid,
                    "is_current": pid == project_id,
                }
            }
            edges.append({"data": {"source": location_node_id, "target": project_node_id}})

    return {"elements": list(nodes.values()) + edges}


def get_projects_location_graph(conn, name_query: str = "", year: int | None = None):
    """Combined location graph for the Proyectos page: every location
    connected to at least one project matching the current name/year
    filters (same filter semantics as list_all_projects), plus every
    OTHER project each of those locations is also linked to for context.
    Projects that match the filter get is_current=True; projects pulled
    in only for context get is_current=False. Returns {"elements": []}
    when nothing matches or none of the matches have a detected location
    — this is what keeps the graph in sync with the list above it."""
    conditions = []
    params: list = []
    if name_query:
        conditions.append("p.canonical_name LIKE ?")
        params.append(f"%{name_query}%")
    if year is not None:
        conditions.append(
            "EXISTS (SELECT 1 FROM project_links pl JOIN folders f ON f.id = pl.folder_id "
            "WHERE pl.project_id = p.id AND f.year = ?)"
        )
        params.append(year)
    where_sql = " AND ".join(conditions) if conditions else "1=1"

    location_rows = conn.execute(
        f"""
        SELECT DISTINCT l.id, l.name
        FROM location_links ll
        JOIN locations l ON l.id = ll.location_id
        JOIN projects p ON p.id = ll.project_id
        WHERE {where_sql}
        ORDER BY l.name
        """,
        params,
    ).fetchall()
    if not location_rows:
        return {"elements": []}

    # Which projects actually match the filter — scoped to just the
    # projects connected to these locations (bounded/small) rather than
    # the whole projects table, so this stays cheap even when the filter
    # matches most of the database.
    location_ids = [r[0] for r in location_rows]
    placeholders = ",".join("?" for _ in location_ids)
    matching_project_ids = {
        r[0]
        for r in conn.execute(
            f"""
            SELECT DISTINCT p.id
            FROM location_links ll
            JOIN projects p ON p.id = ll.project_id
            WHERE ll.location_id IN ({placeholders}) AND {where_sql}
            """,
            location_ids + params,
        ).fetchall()
    }

    nodes: dict[str, dict] = {}
    edges = []
    for location_id, location_name in location_rows:
        location_node_id = f"location-{location_id}"
        nodes[location_node_id] = {
            "data": {"id": location_node_id, "label": location_name, "type": "location"}
        }
        linked_projects = conn.execute(
            """
            SELECT p.id, p.canonical_name
            FROM location_links ll
            JOIN projects p ON p.id = ll.project_id
            WHERE ll.location_id = ?
            ORDER BY p.canonical_name
            """,
            (location_id,),
        ).fetchall()
        for pid, pname in linked_projects:
            project_node_id = f"project-{pid}"
            nodes[project_node_id] = {
                "data": {
                    "id": project_node_id,
                    "label": pname,
                    "type": "project",
                    "project_id": pid,
                    "is_current": pid in matching_project_ids,
                }
            }
            edges.append({"data": {"source": location_node_id, "target": project_node_id}})

    return {"elements": list(nodes.values()) + edges}


def get_project_detail(conn, project_id: int):
    """Everything needed to render one project's timeline: its linked
    folders (year, code, phase) and its detected locations. Returns None
    if the project doesn't exist."""
    project = conn.execute(
        "SELECT id, canonical_name, first_seen_year, last_seen_year, status "
        "FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    if project is None:
        return None

    links = conn.execute(
        """
        SELECT f.year, f.job_code, f.job_name, f.path, pl.phase, pl.confirmed, f.source
        FROM project_links pl
        JOIN folders f ON f.id = pl.folder_id
        WHERE pl.project_id = ?
        ORDER BY f.year, f.job_code
        """,
        (project_id,),
    ).fetchall()

    locations = conn.execute(
        """
        SELECT l.id, l.name
        FROM location_links ll
        JOIN locations l ON l.id = ll.location_id
        WHERE ll.project_id = ?
        ORDER BY l.name
        """,
        (project_id,),
    ).fetchall()

    return {
        "id": project[0],
        "canonical_name": project[1],
        "first_seen_year": project[2],
        "last_seen_year": project[3],
        "status": project[4],
        "links": [
            {
                "year": l[0],
                "job_code": l[1],
                "job_name": l[2],
                "path": l[3],
                "phase": l[4],
                "confirmed": bool(l[5]),
                "source": l[6],
            }
            for l in links
        ],
        "locations": [{"id": l[0], "name": l[1]} for l in locations],
    }


def _glob_escape(path: str) -> str:
    """Escape a literal path for safe use inside a GLOB pattern — GLOB
    treats *, ?, and [ as wildcards, all of which show up in real folder
    names in this archive (e.g. a folder literally named with brackets or
    a question mark). Each gets wrapped in its own single-character
    character class so it's matched literally."""
    return path.replace("[", "[[]").replace("*", "[*]").replace("?", "[?]")


_OFERTAS_SUBFOLDER_NAMES = {"facturacion", "ingevia"}


def get_ofertas(conn, year: int | None = None, company_query: str = ""):
    """OFERTAS page: every 'Firmado' or 'Pedido' document found inside a
    'FACTURACION' or 'INGEVIA' subfolder (any case) that is a DIRECT
    child of a '02.-GESTIÓN' folder — e.g.
    '...\\02.-GESTIÓN\\FACTURACION\\...' or
    '...\\02.-GESTIÓN\\INGEVIA\\...' — including everything nested any
    number of levels further beneath that FACTURACION/INGEVIA folder
    itself. Files that sit directly in '02.-GESTIÓN' or in some other
    subfolder of it (e.g. 'Industria', 'AYTO') are deliberately excluded
    — only these two specific subfolders count.

    Firmado = filename ends in _signed.pdf / _f.pdf / _fda.pdf (any case).
    Pedido  = filename starts with 'pedido' and ends in .pdf (any case).
    A filename matching both (e.g. a signed pedido, 'PEDIDO ..._f.pdf')
    is classified Firmado — the suffix is the more specific, final-state
    signal.

    Matching is done with plain Python string methods, not SQL LIKE:
    LIKE's '_' is itself a single-character wildcard, so a naive
    `LIKE '%_f.pdf'` silently over-matches (e.g. any 'Xf.pdf') — this
    was caught during a spot check before at all.

    Optionally narrowed by an exact `year` (the file's own file_year,
    since a FACTURACION/INGEVIA folder's own subfolder could in principle
    carry a different revision year) and/or a `company_query` substring
    against company_project, same filter semantics as
    search_files/search_folders. These are applied in Python AFTER the
    path GLOB match, not folded into the same SQL WHERE clause —
    combining a GLOB path-prefix filter with an indexed equality (year,
    once idx_files_file_year exists) hit the exact same SQLite planner
    pathology already worked around in search_files/search_folders: the
    planner drove off the year index instead of the path index, turning a
    fast query into hundreds of near-full table scans (measured 23.9s for
    `year=2024` alone before this fix). The final result set here is at
    most a couple thousand rows regardless, so filtering it in Python
    after the (fast) GLOB pass costs nothing noticeable.

    Returns a list of dicts sorted by year (most recent first), each with
    path, name, company_project, location_site, year, and status."""
    gestion_folders = conn.execute(
        "SELECT path FROM folders WHERE name = '02.-GESTIÓN'"
    ).fetchall()

    target_paths = []
    for (gestion_path,) in gestion_folders:
        prefix = _glob_escape(gestion_path) + "\\*"
        children = conn.execute(
            "SELECT path, name FROM folders WHERE path GLOB ?", (prefix,)
        ).fetchall()
        prefix_len = len(gestion_path) + 1  # +1 for the separating backslash
        for child_path, child_name in children:
            # DIRECT child only: everything after the '02.-GESTIÓN\' prefix
            # must be just this one folder's own name, no further '\' —
            # a folder named FACTURACION nested two levels deeper doesn't
            # count, only immediately under GESTIÓN.
            if "\\" in child_path[prefix_len:]:
                continue
            if child_name.strip().lower() in _OFERTAS_SUBFOLDER_NAMES:
                target_paths.append(child_path)

    results = []
    for folder_path in target_paths:
        prefix = _glob_escape(folder_path) + "\\*"
        rows = conn.execute(
            """
            SELECT path, name, company_project, location_site, file_year
            FROM files
            WHERE path GLOB ?
            """,
            (prefix,),
        ).fetchall()
        for path, name, company_project, location_site, file_year in rows:
            if year is not None and file_year != year:
                continue
            if company_query and (not company_project or company_query.lower() not in company_project.lower()):
                continue
            low = name.lower()
            if low.endswith("_signed.pdf") or low.endswith("_f.pdf") or low.endswith("_fda.pdf"):
                status = "Firmado"
            elif low.startswith("pedido") and low.endswith(".pdf"):
                status = "Pedido"
            else:
                continue
            results.append(
                {
                    "path": path,
                    "name": name,
                    "company_project": company_project,
                    "location_site": location_site,
                    "year": file_year,
                    "status": status,
                }
            )

    results.sort(key=lambda r: (r["year"] is not None, r["year"] or 0), reverse=True)
    return results


def list_locations(conn):
    """All detected locations, most-connected first."""
    rows = conn.execute(
        """
        SELECT l.id, l.name, COUNT(ll.project_id) AS project_count
        FROM locations l
        JOIN location_links ll ON ll.location_id = l.id
        GROUP BY l.id
        ORDER BY project_count DESC, l.name
        """
    ).fetchall()
    return [{"id": r[0], "name": r[1], "project_count": r[2]} for r in rows]


def get_location_graph(conn, location_id: int):
    """Cytoscape-ready nodes/edges: one location node fanning out to every
    project linked to it. Returns None if the location doesn't exist."""
    location = conn.execute(
        "SELECT id, name FROM locations WHERE id = ?", (location_id,)
    ).fetchone()
    if location is None:
        return None

    projects = conn.execute(
        """
        SELECT p.id, p.canonical_name, p.first_seen_year, p.last_seen_year
        FROM location_links ll
        JOIN projects p ON p.id = ll.project_id
        WHERE ll.location_id = ?
        ORDER BY p.canonical_name
        """,
        (location_id,),
    ).fetchall()

    location_node_id = f"location-{location[0]}"
    nodes = [{"data": {"id": location_node_id, "label": location[1], "type": "location"}}]
    edges = []
    for p in projects:
        project_node_id = f"project-{p[0]}"
        nodes.append({
            "data": {
                "id": project_node_id,
                "label": p[1],
                "type": "project",
                "project_id": p[0],
            }
        })
        edges.append({"data": {"source": location_node_id, "target": project_node_id}})

    return {
        "location": {"id": location[0], "name": location[1]},
        "elements": nodes + edges,
    }