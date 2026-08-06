"""
One-off diagnostic: crawls a fresh fake archive twice and prints exactly
which folders were re-scanned on the second pass and why, instead of just
a pass/fail count. Run with:

    python scripts/diagnose_incremental.py

Safe to run anywhere — it only touches a temp folder it creates itself,
never anything under P:.
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.crawler.crawl import crawl
from src.db.db import open_db


def make_fake_archive(root: Path) -> None:
    trabajos = root / "TRABAJOS"
    site = trabajos / "TRABAJOS 2022" / "22-007 AYTO TORRENT" / "22-007-02 CALLE SAN LUIS BELTRAN"
    (site / "00-PLANOS PREVIOS").mkdir(parents=True)
    (site / "04-FOTOS").mkdir(parents=True)
    (site / "PROY MEJORA PEATONAL DIC 2024").mkdir(parents=True)
    (site / "some_plan.pdf").write_text("fake pdf")
    (site / "photo.jpg").write_text("fake jpg")

    no_site_job = trabajos / "TRABAJOS 2022" / "22-019 CATASTRO TORRELLA"
    (no_site_job / "00.-PLANOS PREVIOS").mkdir(parents=True)
    (no_site_job / "03.-CORREO").mkdir(parents=True)

    aldi_job = trabajos / "TRABAJOS 2022" / "22-013 ALDI"
    (aldi_job / "22-013-01-ALDI ALFAS BARRANCO").mkdir(parents=True)
    (aldi_job / "22-013-02-ALDI ACCESOS TEULADA").mkdir(parents=True)

    eleval_job = trabajos / "TRABAJOS 2015" / "15-010 ELEVAL"
    (eleval_job / "27-10-15 MJESUS DOÑATE (ELEVAL) plantas").mkdir(parents=True)

    ofertas = root / "OFERTAS Y CONCURSOS"
    offer = ofertas / "2025" / "001.- JUVACAR BENETUSSER"
    (offer / "CORREO").mkdir(parents=True)
    (offer / "FOTOS").mkdir(parents=True)
    (offer / "OFERTA 201_2025 SORIANO US BENETUSSER.pdf").write_text("fake")

    (ofertas / "2025" / "037.AYTO TORRENT_VENTETA").mkdir(parents=True)
    (ofertas / "2025" / "OFERTAS").mkdir(parents=True)


def config(root: Path) -> dict:
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
        "ignore_names": [],
    }


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="diagnose_incremental_"))
    db_path = root / "index.db"
    try:
        make_fake_archive(root)

        print("Waiting 2 seconds for the freshly created folders to settle...\n")
        time.sleep(2)

        conn = open_db(str(db_path))
        cfg = config(root)

        print("=== First crawl ===")
        crawl(conn, cfg)
        before = dict(conn.execute("SELECT path, modified_at FROM folders").fetchall())

        print("\nWaiting 3 seconds...\n")
        time.sleep(3)

        print("=== Second crawl (nothing on disk changed) ===")
        crawl(conn, cfg)
        after = dict(conn.execute("SELECT path, modified_at FROM folders").fetchall())

        print("\n=== Folders whose stored modified_at changed between the two crawls ===")
        changed = 0
        for path, before_mtime in before.items():
            after_mtime = after.get(path)
            if before_mtime != after_mtime:
                changed += 1
                print(f"  {path}")
                print(f"    before: {before_mtime}")
                print(f"    after:  {after_mtime}")
        if changed == 0:
            print("  (none — every folder's mtime was identical both times)")
        conn.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()