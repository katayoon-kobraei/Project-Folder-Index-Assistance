"""
Tests for the folder-name parsing logic and the crawler itself.
Run with: pytest
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.crawler.crawl import (
    parse_job_folder,
    parse_site_folder,
    parse_offer_folder,
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


def test_parse_offer_folder():
    # Real examples from OFERTAS Y CONCURSOS.
    assert parse_offer_folder("001.- JUVACAR BENETUSSER") == ("001", "JUVACAR BENETUSSER")
    assert parse_offer_folder("022.- JOSE LUIS QUESADA_UN SUR") == (
        "022",
        "JOSE LUIS QUESADA_UN SUR",
    )
    assert parse_offer_folder("037.AYTO TORRENT_VENTETA") == ("037", "AYTO TORRENT_VENTETA")
    assert parse_offer_folder("013.-AYTO QUART_APARCAMIENTO") == (
        "013",
        "AYTO QUART_APARCAMIENTO",
    )
    # Non-numbered folders alongside the offers must not match.
    assert parse_offer_folder("OFERTAS") is None
    assert parse_offer_folder("OFERTAS A ESTUDIAR") is None
    assert parse_offer_folder("TURIS") is None


def test_has_year_mismatch():
    assert has_year_mismatch("PROY MEJORA PEATONAL DIC 2024", 2022) is True
    assert has_year_mismatch("22-007 AYTO TORRENT", 2022) is False
    assert has_year_mismatch("04-FOTOS", 2022) is False


def _make_fake_archive(root: Path):
    trabajos = root / "TRABAJOS"

    # Mirrors the real structure from the screenshots, including the
    # nested-revision case (a 2024 update sitting inside a 2022 site folder).
    site = trabajos / "TRABAJOS 2022" / "22-007 AYTO TORRENT" / "22-007-02 CALLE SAN LUIS BELTRAN"
    (site / "00-PLANOS PREVIOS").mkdir(parents=True)
    (site / "04-FOTOS").mkdir(parents=True)
    (site / "PROY MEJORA PEATONAL DIC 2024").mkdir(parents=True)
    (site / "some_plan.pdf").write_text("fake pdf")
    (site / "photo.jpg").write_text("fake jpg")

    # Type 2: a job folder with NO site level — category folders sit
    # directly under the job folder (e.g. 22-019 CATASTRO TORRELLA).
    no_site_job = trabajos / "TRABAJOS 2022" / "22-019 CATASTRO TORRELLA"
    (no_site_job / "00.-PLANOS PREVIOS").mkdir(parents=True)
    (no_site_job / "03.-CORREO").mkdir(parents=True)

    # Real case: a job with multiple dash-joined (no space) sites.
    aldi_job = trabajos / "TRABAJOS 2022" / "22-013 ALDI"
    (aldi_job / "22-013-01-ALDI ALFAS BARRANCO").mkdir(parents=True)
    (aldi_job / "22-013-02-ALDI ACCESOS TEULADA").mkdir(parents=True)

    # Real case: a dated file/folder name that happens to look like a site
    # code but belongs to an unrelated job — must NOT be mistaken for a site.
    eleval_job = trabajos / "TRABAJOS 2015" / "15-010 ELEVAL"
    (eleval_job / "27-10-15 MJESUS DOÑATE (ELEVAL) plantas").mkdir(parents=True)

    # A non-year, non-project folder that must be ignored.
    (trabajos / "CARPETAS PERSONALES").mkdir(parents=True)
    (trabajos / "#recycle").mkdir(parents=True)

    # Second archive root: OFERTAS Y CONCURSOS, with its own year folders
    # and offer-numbered subfolders (no site level, no year in the code).
    ofertas = root / "OFERTAS Y CONCURSOS"
    offer = ofertas / "2025" / "001.- JUVACAR BENETUSSER"
    (offer / "CORREO").mkdir(parents=True)
    (offer / "FOTOS").mkdir(parents=True)
    (offer / "OFERTA 201_2025 SORIANO US BENETUSSER.pdf").write_text("fake")

    offer2 = ofertas / "2025" / "037.AYTO TORRENT_VENTETA"
    offer2.mkdir(parents=True)

    # Non-offer folders/files sitting alongside the offers, must not parse
    # as offers but should still get crawled/recorded.
    (ofertas / "2025" / "OFERTAS").mkdir(parents=True)
    (ofertas / "CLASIFICACION EMPRESARIAL").mkdir(parents=True)  # not a year, ignored
    (ofertas / "LOGO COMPLETO.jpg").write_text("fake logo")


def _config(root: Path):
    return {
        "roots": [
            {
                "name": "trabajos",
                "root_path": str(root / "TRABAJOS"),
                "year_folder_pattern": r"^TRABAJOS (\d{4})$",
                "code_kind": "job",
            },
            {
                "name": "ofertas",
                "root_path": str(root / "OFERTAS Y CONCURSOS"),
                "year_folder_pattern": r"^(\d{4})$",
                "code_kind": "offer",
            },
        ],
        "ignore_names": ["#recycle", "_gsdata_", "Thumbs.db", ".DS_Store"],
    }


def test_crawl_builds_expected_rows(tmp_path):
    _make_fake_archive(tmp_path)
    db_path = tmp_path / "index.db"
    conn = open_db(str(db_path))

    stats = crawl(conn, _config(tmp_path))

    assert stats["years"] == 3  # TRABAJOS 2022, TRABAJOS 2015, ofertas 2025
    assert stats["revision_hints"] == 1  # the "DIC 2024" folder
    assert stats["files"] >= 3  # some_plan.pdf, photo.jpg, the offer pdf, etc.

    rows = conn.execute(
        "SELECT depth, name, source, job_code, job_name, site_code, site_name, is_revision_hint "
        "FROM folders ORDER BY depth, name"
    ).fetchall()
    by_name = {r[1]: r for r in rows}

    assert "CARPETAS PERSONALES" not in by_name
    assert "#recycle" not in by_name

    job_row = by_name["22-007 AYTO TORRENT"]
    assert job_row[2] == "trabajos"
    assert job_row[3] == "22-007"
    assert job_row[4] == "AYTO TORRENT"

    site_row = by_name["22-007-02 CALLE SAN LUIS BELTRAN"]
    assert site_row[5] == "22-007-02"
    assert site_row[6] == "CALLE SAN LUIS BELTRAN"
    assert site_row[3] == "22-007"

    revision_row = by_name["PROY MEJORA PEATONAL DIC 2024"]
    assert revision_row[7] == 1
    assert revision_row[3] == "22-007"

    no_site_job_row = by_name["22-019 CATASTRO TORRELLA"]
    assert no_site_job_row[3] == "22-019"
    assert no_site_job_row[5] is None

    category_row = by_name["03.-CORREO"]
    assert category_row[3] == "22-019"
    assert category_row[5] is None

    aldi_site_1 = by_name["22-013-01-ALDI ALFAS BARRANCO"]
    assert aldi_site_1[3] == "22-013"
    assert aldi_site_1[5] == "22-013-01"

    eleval_row = by_name["27-10-15 MJESUS DOÑATE (ELEVAL) plantas"]
    assert eleval_row[3] == "15-010"
    assert eleval_row[5] is None

    # Ofertas tree: offer folders parsed correctly, source tagged 'ofertas',
    # no site level even though the parsing machinery supports one.
    offer_row = by_name["001.- JUVACAR BENETUSSER"]
    assert offer_row[2] == "ofertas"
    assert offer_row[3] == "001"
    assert offer_row[4] == "JUVACAR BENETUSSER"
    assert offer_row[5] is None  # no site concept for offers

    offer_row_2 = by_name["037.AYTO TORRENT_VENTETA"]
    assert offer_row_2[3] == "037"
    assert offer_row_2[4] == "AYTO TORRENT_VENTETA"

    # Non-offer folder alongside the offers must not get a job_code.
    non_offer_row = by_name["OFERTAS"]
    assert non_offer_row[3] is None

    conn.close()


def test_files_are_recorded(tmp_path):
    _make_fake_archive(tmp_path)
    db_path = tmp_path / "index.db"
    conn = open_db(str(db_path))
    crawl(conn, _config(tmp_path))

    files = conn.execute("SELECT name, extension FROM files ORDER BY name").fetchall()
    names = {f[0]: f[1] for f in files}

    assert "some_plan.pdf" in names
    assert names["some_plan.pdf"] == "pdf"
    assert "photo.jpg" in names
    assert names["photo.jpg"] == "jpg"
    assert "OFERTA 201_2025 SORIANO US BENETUSSER.pdf" in names

    # File must be correctly linked to its containing folder.
    row = conn.execute(
        """
        SELECT folders.name FROM files
        JOIN folders ON folders.id = files.folder_id
        WHERE files.name = 'some_plan.pdf'
        """
    ).fetchone()
    assert row[0] == "22-007-02 CALLE SAN LUIS BELTRAN"

    conn.close()


def test_fts_search_finds_by_partial_name(tmp_path):
    _make_fake_archive(tmp_path)
    db_path = tmp_path / "index.db"
    conn = open_db(str(db_path))
    crawl(conn, _config(tmp_path))

    folder_results = conn.execute(
        "SELECT rowid FROM folders_fts WHERE folders_fts MATCH 'torrent'"
    ).fetchall()
    assert len(folder_results) >= 1

    file_results = conn.execute(
        "SELECT rowid FROM files_fts WHERE files_fts MATCH 'plan'"
    ).fetchall()
    assert len(file_results) >= 1

    conn.close()
