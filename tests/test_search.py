"""
Tests for the search/query layer that backs the web UI.
Run with: pytest
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.db import open_db, rebuild_fts
from src.search.search import (
    get_location_graph,
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


def _seed(conn):
    conn.execute(
        "INSERT INTO folders (path, source, depth, name, year, job_code, job_name) "
        "VALUES ('/f/1', 'trabajos', 1, '18-009 PLENOIL', 2018, '18-009', 'PLENOIL')"
    )
    folder_id_main = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO folders (path, source, depth, name, year, job_code, job_name) "
        "VALUES ('/f/2', 'trabajos', 1, '18-020 DO PLENOIL', 2018, '18-020', 'DO PLENOIL')"
    )
    folder_id_do = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    cur = conn.execute(
        "INSERT INTO projects (canonical_name, first_seen_year, last_seen_year, status) "
        "VALUES ('PLENOIL', 2018, 2018, 'active')"
    )
    project_id = cur.lastrowid
    conn.execute(
        "INSERT INTO project_links (project_id, folder_id, confidence, confirmed, phase) "
        "VALUES (?, ?, 100.0, 1, NULL)", (project_id, folder_id_main),
    )
    conn.execute(
        "INSERT INTO project_links (project_id, folder_id, confidence, confirmed, phase) "
        "VALUES (?, ?, 100.0, 1, 'direccion_obra')", (project_id, folder_id_do),
    )

    cur = conn.execute("INSERT INTO locations (name) VALUES ('SAGUNTO')")
    location_id = cur.lastrowid
    conn.execute(
        "INSERT INTO location_links (location_id, project_id) VALUES (?, ?)",
        (location_id, project_id),
    )
    conn.commit()
    return project_id, location_id


def test_search_projects_by_name(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    _seed(conn)

    results = search_projects(conn, "plenoil")
    assert len(results) == 1
    assert results[0]["canonical_name"] == "PLENOIL"

    assert search_projects(conn, "nonexistent") == []
    assert len(search_projects(conn, "")) == 1  # empty query returns everything

    conn.close()


def test_search_projects_limit_none_returns_every_match(tmp_path):
    # The web app relies on the default limit=50 staying exactly as-is;
    # limit=None (used by Buscar's desktop project search) must return
    # every match instead, with no cap at all.
    conn = open_db(str(tmp_path / "index.db"))
    for i in range(60):
        conn.execute(
            "INSERT INTO projects (canonical_name, first_seen_year, last_seen_year, status) "
            "VALUES (?, 2020, 2020, 'active')",
            (f"CONSUM SITE {i}",),
        )
    conn.commit()

    capped = search_projects(conn, "CONSUM")
    assert len(capped) == 50

    uncapped = search_projects(conn, "CONSUM", limit=None)
    assert len(uncapped) == 60

    conn.close()


def _insert_file(conn, folder_id: int, path: str, name: str, **extra) -> None:
    row = {
        "path": path,
        "folder_id": folder_id,
        "name": name,
        "extension": name.rsplit(".", 1)[-1].lower() if "." in name else None,
        "modified_at": None,
        "size_bytes": None,
        "company_project": None,
        "file_year": None,
        "location_site": None,
        **extra,
    }
    conn.execute(
        """
        INSERT INTO files (path, folder_id, name, extension, modified_at, size_bytes,
                            company_project, file_year, location_site)
        VALUES (:path, :folder_id, :name, :extension, :modified_at, :size_bytes,
                :company_project, :file_year, :location_site)
        """,
        row,
    )


def test_search_files_empty_query_returns_nothing(tmp_path):
    # Unlike search_projects, an empty query must NOT dump everything —
    # there's no useful default order over hundreds of thousands of files.
    conn = open_db(str(tmp_path / "index.db"))
    project_id, _ = _seed(conn)
    folder_id = conn.execute("SELECT id FROM folders LIMIT 1").fetchone()[0]
    _insert_file(conn, folder_id, "/f/1/Presupuesto Final.pdf", "Presupuesto Final.pdf")
    conn.commit()
    rebuild_fts(conn)

    assert search_files(conn, "") == []
    conn.close()


def test_search_files_finds_by_partial_word(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    _seed(conn)
    folder_id = conn.execute("SELECT id FROM folders LIMIT 1").fetchone()[0]
    _insert_file(conn, folder_id, "/f/1/Presupuesto Final.pdf", "Presupuesto Final.pdf")
    _insert_file(conn, folder_id, "/f/1/Factura Enero.pdf", "Factura Enero.pdf")
    conn.commit()
    rebuild_fts(conn)

    results = search_files(conn, "presup")
    assert [r["name"] for r in results] == ["Presupuesto Final.pdf"]

    assert search_files(conn, "nonexistentword") == []

    conn.close()


def test_search_files_handles_punctuation_in_query_safely(tmp_path):
    # A hyphenated job code or parentheses in the typed query must not
    # raise an FTS5 syntax error — this is exactly the kind of text a
    # real filename/search term contains (e.g. "14-002" or "(borrador)").
    conn = open_db(str(tmp_path / "index.db"))
    _seed(conn)
    folder_id = conn.execute("SELECT id FROM folders LIMIT 1").fetchone()[0]
    _insert_file(conn, folder_id, "/f/1/14-002 Presupuesto.pdf", "14-002 Presupuesto.pdf")
    conn.commit()
    rebuild_fts(conn)

    results = search_files(conn, "14-002")
    assert [r["name"] for r in results] == ["14-002 Presupuesto.pdf"]

    # Should not raise even for characters FTS5 treats specially.
    assert search_files(conn, "(borrador)") == []

    conn.close()


def test_search_files_respects_limit(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    _seed(conn)
    folder_id = conn.execute("SELECT id FROM folders LIMIT 1").fetchone()[0]
    for i in range(5):
        _insert_file(conn, folder_id, f"/f/1/Factura {i}.pdf", f"Factura {i}.pdf")
    conn.commit()
    rebuild_fts(conn)

    assert len(search_files(conn, "factura", limit=5)) == 5
    assert len(search_files(conn, "factura", limit=2)) == 2

    conn.close()


def _insert_folder(conn, path: str, name: str, **extra) -> None:
    row = {
        "path": path,
        "source": "trabajos",
        "depth": 2,
        "name": name,
        "year": None,
        "job_code": None,
        "job_name": None,
        "site_code": None,
        "site_name": None,
        "modified_at": None,
        "file_count": None,
        "is_revision_hint": 0,
        "company_project": None,
        "location_site": None,
        **extra,
    }
    conn.execute(
        """
        INSERT INTO folders (path, source, depth, name, year, job_code, job_name,
                              site_code, site_name, modified_at, file_count,
                              is_revision_hint, company_project, location_site)
        VALUES (:path, :source, :depth, :name, :year, :job_code, :job_name,
                :site_code, :site_name, :modified_at, :file_count,
                :is_revision_hint, :company_project, :location_site)
        """,
        row,
    )


def test_search_folders_empty_query_returns_nothing(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    _seed(conn)
    conn.commit()
    rebuild_fts(conn)

    assert search_folders(conn, "") == []
    conn.close()


def test_search_folders_finds_by_own_name_not_project_name(tmp_path):
    # A site/address folder's own name ("CL BENITO PEREZ GALDOS...")
    # never appears in the resolved project's canonical name (that
    # project might just be called "PLENOIL") — search_folders must find
    # it anyway, since it searches the folder's own `name` column
    # directly, not the projects table at all.
    conn = open_db(str(tmp_path / "index.db"))
    _seed(conn)
    _insert_folder(
        conn,
        "/f/1/18-009-01 CL BENITO PEREZ GALDOS",
        "18-009-01 CL BENITO PEREZ GALDOS",
        company_project="PLENOIL",
        location_site="CL BENITO PEREZ GALDOS",
    )
    conn.commit()
    rebuild_fts(conn)

    results = search_folders(conn, "benito perez")
    assert len(results) == 1
    assert results[0]["name"] == "18-009-01 CL BENITO PEREZ GALDOS"
    assert results[0]["company_project"] == "PLENOIL"

    conn.close()


def test_search_folders_ignores_inherited_job_and_site_name(tmp_path):
    # job_name/site_name are inherited down to every descendant folder
    # during the crawl (see crawl.py's _walk_children) — a generic
    # subfolder like "00.-PLANOS" sitting under a site folder named
    # after a street must NOT match a search for that street, since the
    # subfolder's own name has nothing to do with it. This was a real
    # bug: folders_fts indexes name/job_name/site_name together, so an
    # unfiltered MATCH against all three pulled in every subfolder under
    # a matching site. Only the `name` column should ever decide a match.
    conn = open_db(str(tmp_path / "index.db"))
    _seed(conn)
    _insert_folder(
        conn,
        "/f/1/18-009-01 CL BENITO PEREZ GALDOS",
        "18-009-01 CL BENITO PEREZ GALDOS",
        job_name="PLENOIL",
        site_name="CL BENITO PEREZ GALDOS",
    )
    _insert_folder(
        conn,
        "/f/1/18-009-01 CL BENITO PEREZ GALDOS/00.-PLANOS",
        "00.-PLANOS",
        job_name="PLENOIL",
        site_name="CL BENITO PEREZ GALDOS",  # inherited from the parent
    )
    conn.commit()
    rebuild_fts(conn)

    names = {r["name"] for r in search_folders(conn, "benito perez")}
    assert "18-009-01 CL BENITO PEREZ GALDOS" in names
    assert "00.-PLANOS" not in names

    conn.close()


def test_search_folders_handles_punctuation_safely(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    _seed(conn)
    _insert_folder(conn, "/f/1/14-002.001 CONSUM LA ZENIA", "14-002.001 CONSUM LA ZENIA")
    conn.commit()
    rebuild_fts(conn)

    results = search_folders(conn, "14-002")
    assert len(results) == 1

    conn.close()


def test_get_project_detail(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    project_id, location_id = _seed(conn)

    detail = get_project_detail(conn, project_id)
    assert detail["canonical_name"] == "PLENOIL"
    assert len(detail["links"]) == 2
    phases = sorted(l["phase"] or "main" for l in detail["links"])
    assert phases == ["direccion_obra", "main"]
    assert detail["locations"] == [{"id": location_id, "name": "SAGUNTO"}]

    assert get_project_detail(conn, 9999) is None

    conn.close()


def test_list_and_graph_locations(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    project_id, location_id = _seed(conn)

    locations = list_locations(conn)
    assert locations == [{"id": location_id, "name": "SAGUNTO", "project_count": 1}]

    graph = get_location_graph(conn, location_id)
    assert graph["location"]["name"] == "SAGUNTO"
    node_ids = {el["data"]["id"] for el in graph["elements"] if "source" not in el["data"]}
    assert f"location-{location_id}" in node_ids
    assert f"project-{project_id}" in node_ids

    assert get_location_graph(conn, 9999) is None

    conn.close()


def _seed_project(conn, name: str, years: list[int], status: str = "unknown"):
    """Seed one project with a linked folder for each year in `years` —
    used to test the year filter distinguishes 'has a folder in exactly
    this year' from 'this year falls somewhere in the project's overall
    span'."""
    folder_ids = []
    for i, year in enumerate(years):
        conn.execute(
            "INSERT INTO folders (path, source, depth, name, year, job_code, job_name) "
            f"VALUES ('/f/{name}/{i}', 'trabajos', 1, ?, ?, ?, ?)",
            (f"{year}-{i:03} {name}", year, f"{year}-{i:03}", name),
        )
        folder_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    cur = conn.execute(
        "INSERT INTO projects (canonical_name, first_seen_year, last_seen_year, status) "
        "VALUES (?, ?, ?, ?)",
        (name, min(years), max(years), status),
    )
    project_id = cur.lastrowid
    for folder_id in folder_ids:
        conn.execute(
            "INSERT INTO project_links (project_id, folder_id, confidence, confirmed) "
            "VALUES (?, ?, 100.0, 1)",
            (project_id, folder_id),
        )
    conn.commit()
    return project_id


def test_list_all_projects_has_no_row_cap_and_sorts_az(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    _seed_project(conn, "ZONA LOGISTICA", [2026])
    _seed_project(conn, "AYTO SAGUNTO", [2020])
    _seed_project(conn, "BAMAGASA", [2015])

    results = list_all_projects(conn)
    assert [r["canonical_name"] for r in results] == ["AYTO SAGUNTO", "BAMAGASA", "ZONA LOGISTICA"]

    conn.close()


def test_list_all_projects_year_filter_requires_an_actual_folder_that_year(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    # PLENOIL has folders in 2014 and 2019 but nothing in between — it
    # must NOT show up for a 2016 filter just because 2016 falls inside
    # its first_seen_year..last_seen_year range.
    _seed_project(conn, "PLENOIL", [2014, 2019])
    _seed_project(conn, "CONSUM", [2016])

    results_2016 = list_all_projects(conn, year=2016)
    assert [r["canonical_name"] for r in results_2016] == ["CONSUM"]

    results_2014 = list_all_projects(conn, year=2014)
    assert [r["canonical_name"] for r in results_2014] == ["PLENOIL"]

    conn.close()


def test_list_all_projects_combines_name_and_year_filters(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    _seed_project(conn, "PLENOIL", [2017, 2018])
    _seed_project(conn, "PLENERGY", [2018])

    # Both filters must apply together (AND, not OR).
    results = list_all_projects(conn, name_query="PLEN", year=2017)
    assert [r["canonical_name"] for r in results] == ["PLENOIL"]

    conn.close()


def test_get_project_location_graph_empty_when_no_locations(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    project_id = _seed_project(conn, "PLENERGY", [2026])

    graph = get_project_location_graph(conn, project_id)
    assert graph == {"elements": []}

    conn.close()


def test_get_project_location_graph_includes_other_projects_and_flags_current(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    consum_id = _seed_project(conn, "CONSUM MASSAMAGRELL", [2018])
    other_id = _seed_project(conn, "SALON JUEGOS MASSAMAGRELL", [2019])

    cur = conn.execute("INSERT INTO locations (name) VALUES ('MASSAMAGRELL')")
    location_id = cur.lastrowid
    conn.execute(
        "INSERT INTO location_links (location_id, project_id) VALUES (?, ?)",
        (location_id, consum_id),
    )
    conn.execute(
        "INSERT INTO location_links (location_id, project_id) VALUES (?, ?)",
        (location_id, other_id),
    )
    conn.commit()

    graph = get_project_location_graph(conn, consum_id)
    nodes = {el["data"]["id"]: el["data"] for el in graph["elements"] if "source" not in el["data"]}

    assert f"location-{location_id}" in nodes
    assert nodes[f"project-{consum_id}"]["is_current"] is True
    assert nodes[f"project-{other_id}"]["is_current"] is False

    edges = [el["data"] for el in graph["elements"] if "source" in el["data"]]
    assert {"source": f"location-{location_id}", "target": f"project-{consum_id}"} in edges
    assert {"source": f"location-{location_id}", "target": f"project-{other_id}"} in edges

    conn.close()


def _link_location(conn, location_name: str, project_ids: list[int]) -> int:
    cur = conn.execute("INSERT INTO locations (name) VALUES (?)", (location_name,))
    location_id = cur.lastrowid
    for project_id in project_ids:
        conn.execute(
            "INSERT INTO location_links (location_id, project_id) VALUES (?, ?)",
            (location_id, project_id),
        )
    conn.commit()
    return location_id


def test_get_projects_location_graph_empty_when_nothing_matches(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    consum_id = _seed_project(conn, "CONSUM MASSAMAGRELL", [2018])
    _link_location(conn, "MASSAMAGRELL", [consum_id])

    # A name filter that matches no project at all -> no graph.
    assert get_projects_location_graph(conn, name_query="NOPE") == {"elements": []}
    conn.close()


def test_get_projects_location_graph_filters_like_the_projects_list(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    consum_id = _seed_project(conn, "CONSUM MASSAMAGRELL", [2018])
    other_id = _seed_project(conn, "SALON JUEGOS MASSAMAGRELL", [2019])
    unrelated_id = _seed_project(conn, "BAMAGASA", [2020])
    location_id = _link_location(conn, "MASSAMAGRELL", [consum_id, other_id])

    # Filtering the Proyectos list to "CONSUM" should mark only CONSUM as
    # current, but still pull in SALON JUEGOS as context since it shares
    # the same location — while BAMAGASA (no shared location, doesn't
    # match the filter either) shouldn't appear in the graph at all.
    graph = get_projects_location_graph(conn, name_query="CONSUM")
    nodes = {el["data"]["id"]: el["data"] for el in graph["elements"] if "source" not in el["data"]}

    assert f"location-{location_id}" in nodes
    assert nodes[f"project-{consum_id}"]["is_current"] is True
    assert nodes[f"project-{other_id}"]["is_current"] is False
    assert f"project-{unrelated_id}" not in nodes

    conn.close()


def test_get_projects_location_graph_year_filter_flips_which_project_is_current(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    consum_id = _seed_project(conn, "CONSUM MASSAMAGRELL", [2018])
    other_id = _seed_project(conn, "SALON JUEGOS MASSAMAGRELL", [2019])
    _link_location(conn, "MASSAMAGRELL", [consum_id, other_id])

    # Both projects share the same location, but only SALON JUEGOS has a
    # folder in 2019 — the year filter must flip is_current accordingly,
    # exactly like list_all_projects' own year filter.
    graph = get_projects_location_graph(conn, year=2019)
    nodes = {el["data"]["id"]: el["data"] for el in graph["elements"] if "source" not in el["data"]}
    assert nodes[f"project-{consum_id}"]["is_current"] is False
    assert nodes[f"project-{other_id}"]["is_current"] is True

    conn.close()


def test_list_available_years(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    _seed_project(conn, "PLENOIL", [2014, 2019])
    _seed_project(conn, "CONSUM", [2016])

    assert list_available_years(conn) == [2014, 2016, 2019]

    conn.close()