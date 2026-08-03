"""
Tests for location detection (grouping projects that share a town name).
Run with: pytest
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.db import open_db
from src.resolution.detect_locations import tokenize, find_location_candidates, run


def test_tokenize():
    assert tokenize("AYTO SAGUNTO") == ["AYTO", "SAGUNTO"]
    assert tokenize("FAMILY CASH SAGUNTO") == ["FAMILY", "CASH", "SAGUNTO"]


def _seed_projects(conn, names):
    ids = []
    for name in names:
        cur = conn.execute("INSERT INTO projects (canonical_name) VALUES (?)", (name,))
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def test_finds_real_location_pattern(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    _seed_projects(conn, [
        "AYTO SAGUNTO",
        "FAMILY CASH SAGUNTO",
        "CARAVANAS SAGUNTO",
        "DEVI PETROL SAGUNTO",
        "AYTO TORRENT",
        "VIVO ACCESO TORRENT",
        "PLENOIL",  # no location, single unrelated project
    ])

    result = run(conn)
    assert "SAGUNTO" in result["location_project_counts"]
    assert result["location_project_counts"]["SAGUNTO"] == 4
    assert "TORRENT" in result["location_project_counts"]
    assert result["location_project_counts"]["TORRENT"] == 2

    # a location with only ONE project isn't a "recurring" location — must
    # not appear at all.
    assert "PLENOIL" not in result["location_project_counts"]

    conn.close()


def test_stopwords_are_never_treated_as_locations(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    _seed_projects(conn, [
        "AYTO SAGUNTO",
        "AYTO VILLALONGA",
        "AYTO GANDIA",
        "EST HIDROLOGICO ARJONA",
        "EST USD PAIPORTA",
    ])
    result = run(conn)
    # "AYTO" and "EST" appear in many projects but are administrative
    # abbreviations (town hall / study), never a place name themselves.
    assert "AYTO" not in result["location_project_counts"]
    assert "EST" not in result["location_project_counts"]

    conn.close()


def test_location_links_correct_projects(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    ids = _seed_projects(conn, [
        "AYTO SAGUNTO",
        "FAMILY CASH SAGUNTO",
        "PLENOIL",
    ])
    run(conn)

    location_id = conn.execute("SELECT id FROM locations WHERE name = 'SAGUNTO'").fetchone()[0]
    linked_project_ids = {
        r[0] for r in conn.execute(
            "SELECT project_id FROM location_links WHERE location_id = ?", (location_id,)
        )
    }
    assert linked_project_ids == {ids[0], ids[1]}
    assert ids[2] not in linked_project_ids  # PLENOIL must not be linked

    conn.close()


def test_run_is_idempotent(tmp_path):
    conn = open_db(str(tmp_path / "index.db"))
    _seed_projects(conn, ["AYTO SAGUNTO", "FAMILY CASH SAGUNTO"])
    run(conn)
    run(conn)
    assert conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM location_links").fetchone()[0] == 2
    conn.close()
