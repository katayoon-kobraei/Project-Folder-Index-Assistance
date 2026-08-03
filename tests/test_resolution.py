"""
Tests for entity resolution (grouping job/offer names into projects).
Run with: pytest
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.db import open_db
from src.resolution.match_projects import (
    strip_phase_prefix,
    load_job_rows,
    group_exact,
    suggest_fuzzy_merges,
    run,
)


def test_strip_phase_prefix():
    assert strip_phase_prefix("DO PLENOIL") == ("PLENOIL", "direccion_obra")
    assert strip_phase_prefix("PLENOIL") == ("PLENOIL", None)
    # must not strip "DO" out of a name that just happens to contain it
    assert strip_phase_prefix("DOMINGO PEREZ") == ("DOMINGO PEREZ", None)


def _seed_folders(conn, rows):
    """rows: list of (job_code, job_name, source, year) -> insert as
    depth=1 folders and return their ids in the same order."""
    ids = []
    for i, (job_code, job_name, source, year) in enumerate(rows):
        conn.execute(
            """
            INSERT INTO folders (path, source, depth, name, year, job_code, job_name)
            VALUES (?, ?, 1, ?, ?, ?, ?)
            """,
            (f"/fake/{source}/{year}/{job_code}", source, job_name, year, job_code, job_name),
        )
        ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    return ids


def test_run_groups_exact_and_phase(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    _seed_folders(conn, [
        ("18-009", "PLENOIL", "trabajos", 2018),
        ("19-001", "PLENOIL", "trabajos", 2019),
        ("18-020", "DO PLENOIL", "trabajos", 2018),
        ("22-007", "AYTO TORRENT", "trabajos", 2022),
        ("23-026", "AYTO TORRENT", "trabajos", 2023),
    ])

    result = run(conn)
    assert result["folders_considered"] == 5
    # PLENOIL and DO PLENOIL collapse into ONE project (phase-aware), plus
    # AYTO TORRENT = 2 projects total.
    assert result["projects_created"] == 2

    projects = conn.execute("SELECT id, canonical_name, first_seen_year, last_seen_year FROM projects").fetchall()
    names = {p[1]: p for p in projects}
    assert "PLENOIL" in names
    assert names["PLENOIL"][2] == 2018  # first_seen_year
    assert names["PLENOIL"][3] == 2019  # last_seen_year

    plenoil_id = names["PLENOIL"][0]
    links = conn.execute(
        "SELECT phase, confirmed, confidence FROM project_links WHERE project_id = ?",
        (plenoil_id,),
    ).fetchall()
    assert len(links) == 3  # PLENOIL x2 + DO PLENOIL x1, all linked to the same project
    phases = sorted(l[0] or "main" for l in links)
    assert phases == ["direccion_obra", "main", "main"]
    # exact-match groups are auto-confirmed
    assert all(l[1] == 1 for l in links)
    assert all(l[2] == 100.0 for l in links)

    conn.close()


def test_fuzzy_suggestions_flag_near_matches_without_merging(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    _seed_folders(conn, [
        ("16-003", "SEINZA", "trabajos", 2016),
        ("20-040", "SEINZA-INGEVIA", "trabajos", 2020),
        # a case that MUST NOT be suggested: different plant numbers.
        ("15-001", "PLASTIC PLANTA IV", "trabajos", 2015),
        ("15-002", "PLASTIC PLANTA I", "trabajos", 2015),
    ])

    result = run(conn)
    # each distinct base name is still its OWN project — fuzzy matches are
    # never auto-merged.
    assert result["projects_created"] == 4

    suggestion_pairs = {(s["name_a"], s["name_b"]) for s in result["fuzzy_suggestions"]}
    flat = {n for pair in suggestion_pairs for n in pair}
    assert "SEINZA" in flat
    assert "SEINZA-INGEVIA" in flat

    conn.close()


def test_run_is_idempotent(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    _seed_folders(conn, [
        ("18-009", "PLENOIL", "trabajos", 2018),
        ("19-001", "PLENOIL", "trabajos", 2019),
    ])
    run(conn)
    run(conn)  # rerun must not duplicate projects/links
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM project_links").fetchone()[0] == 2
    conn.close()
