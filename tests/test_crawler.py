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

    # Real formatting variants found in the archive: dot before dash, no
    # space, 2-digit job number instead of 3.
    assert parse_job_folder("14-001.- DINAMIAL") == ("14-001", "DINAMIAL")
    assert parse_job_folder("14-023-USD NAQUERA") == ("14-023", "USD NAQUERA")
    assert parse_job_folder("09-44 EST PICAYO") == ("09-44", "EST PICAYO")

    # Genuinely non-coded admin folders must still not match.
    assert parse_job_folder("CONTROL DE HORAS") is None
    assert parse_job_folder("FACTURAS DE PROVEEDORES") is None
    assert parse_job_folder("14- CONSULTAS 2014") is None


def test_parse_site_folder():
    assert parse_site_folder("22-007-02 CALLE SAN LUIS BELTRAN") == (
        "22-007-02",
        "CALLE SAN LUIS BELTRAN",
    )
    assert parse_site_folder("22-007 AYTO TORRENT") is None

    # Real formatting variant: no space, dash-joined straight into the name.
    assert parse_site_folder("22-013-01-ALDI ALFAS BARRANCO", job_code="22-013") == (
        "22-013-01",
        "ALDI ALFAS BARRANCO",
    )

    # Dates that happen to look like site codes (day-month-year, e.g. a
    # file named "16-01-29 (ver) ...") must be rejected because their
    # job-code prefix doesn't match the job they're actually sitting under.
    assert parse_site_folder("16-01-29 (ver) 15039 caravanas puzol", job_code="14-047") is None
    assert parse_site_folder(
        "27-10-15 MJESUS DOÑATE (ELEVAL) plantas", job_code="15-010"
    ) is None


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

    # Type 2: a job folder with NO site level — category folders sit
    # directly under the job folder (e.g. 22-019 CATASTRO TORRELLA).
    no_site_job = root / "TRABAJOS 2022" / "22-019 CATASTRO TORRELLA"
    (no_site_job / "00.-PLANOS PREVIOS").mkdir(parents=True)
    (no_site_job / "03.-CORREO").mkdir(parents=True)

    # Real case: a job with multiple dash-joined (no space) sites.
    aldi_job = root / "TRABAJOS 2022" / "22-013 ALDI"
    (aldi_job / "22-013-01-ALDI ALFAS BARRANCO").mkdir(parents=True)
    (aldi_job / "22-013-02-ALDI ACCESOS TEULADA").mkdir(parents=True)

    # Real case: a dated file/folder name that happens to look like a site
    # code but belongs to an unrelated job — must NOT be mistaken for a site.
    eleval_job = root / "TRABAJOS 2015" / "15-010 ELEVAL"
    (eleval_job / "27-10-15 MJESUS DOÑATE (ELEVAL) plantas").mkdir(parents=True)

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

    assert stats["years"] == 2  # TRABAJOS 2022 and TRABAJOS 2015
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

    # Type 2: no-site job — category folders sit directly under the job,
    # so they must NOT be mistaken for a site and site_code/site_name must
    # stay empty.
    no_site_job_row = by_name["22-019 CATASTRO TORRELLA"]
    assert no_site_job_row[2] == "22-019"
    assert no_site_job_row[3] == "CATASTRO TORRELLA"
    assert no_site_job_row[4] is None  # site_code
    assert no_site_job_row[5] is None  # site_name

    category_row = by_name["03.-CORREO"]
    assert category_row[2] == "22-019"   # inherits job code
    assert category_row[3] == "CATASTRO TORRELLA"
    assert category_row[4] is None       # no site — never invented one
    assert category_row[5] is None

    # ALDI: dash-joined (no space) sites must parse correctly.
    aldi_site_1 = by_name["22-013-01-ALDI ALFAS BARRANCO"]
    assert aldi_site_1[2] == "22-013"
    assert aldi_site_1[4] == "22-013-01"
    assert aldi_site_1[5] == "ALDI ALFAS BARRANCO"

    aldi_site_2 = by_name["22-013-02-ALDI ACCESOS TEULADA"]
    assert aldi_site_2[4] == "22-013-02"

    # ELEVAL: a dated folder name that looks like a site code must NOT be
    # mistaken for one — it belongs to job 15-010, not job 27-10.
    eleval_row = by_name["27-10-15 MJESUS DOÑATE (ELEVAL) plantas"]
    assert eleval_row[2] == "15-010"  # correctly inherits the real job
    assert eleval_row[4] is None      # site_code must stay empty
    assert eleval_row[5] is None

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