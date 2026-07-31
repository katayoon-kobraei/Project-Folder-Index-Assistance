"""
Tests for the folder-name parsing logic and the crawler itself.
Run with: pytest
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.crawler.crawl import (
    parse_job_folder,
    parse_site_folder,
    has_year_mismatch,
    crawl,
)
from src.db.db import open_db


def test_parse_job_folder():
    assert parse_job_folder("22-007 AYTO TORRENT") == ("22-007", "AYTO TORRENT")
    assert parse_job_folder("random name") is None


def test_parse_site_folder():
    assert parse_site_folder("22-007-02 CALLE SAN LUIS BELTRAN") == (
        "22-007-02",
        "CALLE SAN LUIS BELTRAN",
    )
    assert parse_site_folder("22-007 AYTO TORRENT") is None


def test_has_year_mismatch():
    assert has_year_mismatch("PROY MEJORA PEATONAL DIC 2024", 2022) is True
    assert has_year_mismatch("22-007 AYTO TORRENT", 2022) is False
    assert has_year_mismatch("04-FOTOS", 2022) is False


def _make_fake_archive(root: Path):
    # Mirrors the real structure from the screenshots, including the
    # nested-revision case (a 2024 update sitting inside a 2022 site folder).
    site = root / "TRABAJOS 2022" / "22-007 AYTO TORRENT" / "22-007-02 CALLE SAN LUIS BELTRAN"
    (site / "00-PLANOS PREVIOS").mkdir(parents=True)
    (site / "04-FOTOS").mkdir(parents=True)
    (site / "PROY MEJORA PEATONAL DIC 2024").mkdir(parents=True)
    # A non-year, non-project folder at the root that must be ignored.
    (root / "CARPETAS PERSONALES").mkdir(parents=True)
    (root / "#recycle").mkdir(parents=True)


def test_crawl_builds_expected_rows(tmp_path):
    _make_fake_archive(tmp_path)
    db_path = tmp_path / "index.db"
    conn = open_db(str(db_path))

    config = {
        "year_folder_pattern": r"^TRABAJOS (\d{4})$",
        "ignore_names": ["#recycle", "_gsdata_", "Thumbs.db", ".DS_Store"],
    }
    stats = crawl(conn, str(tmp_path), config)

    assert stats["years"] == 1
    assert stats["revision_hints"] == 1  # the "DIC 2024" folder

    rows = conn.execute(
        "SELECT depth, name, job_code, job_name, site_code, site_name, is_revision_hint "
        "FROM folders ORDER BY depth, name"
    ).fetchall()
    by_name = {r[1]: r for r in rows}

    # CARPETAS PERSONALES and #recycle must not appear — not a TRABAJOS year
    # folder / explicitly ignored.
    assert "CARPETAS PERSONALES" not in by_name
    assert "#recycle" not in by_name

    job_row = by_name["22-007 AYTO TORRENT"]
    assert job_row[2] == "22-007"
    assert job_row[3] == "AYTO TORRENT"

    site_row = by_name["22-007-02 CALLE SAN LUIS BELTRAN"]
    assert site_row[4] == "22-007-02"
    assert site_row[5] == "CALLE SAN LUIS BELTRAN"
    # site folder still inherits the job code/name
    assert site_row[2] == "22-007"

    revision_row = by_name["PROY MEJORA PEATONAL DIC 2024"]
    assert revision_row[6] == 1  # is_revision_hint
    assert revision_row[2] == "22-007"  # still linked to the right job

    conn.close()


def test_fts_search_finds_by_partial_name(tmp_path):
    _make_fake_archive(tmp_path)
    db_path = tmp_path / "index.db"
    conn = open_db(str(db_path))
    config = {
        "year_folder_pattern": r"^TRABAJOS (\d{4})$",
        "ignore_names": ["#recycle", "_gsdata_", "Thumbs.db", ".DS_Store"],
    }
    crawl(conn, str(tmp_path), config)

    results = conn.execute(
        "SELECT rowid FROM folders_fts WHERE folders_fts MATCH 'torrent'"
    ).fetchall()
    assert len(results) >= 1
    conn.close()
