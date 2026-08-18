"""
Tests for src/project_info/sync_trabajos.py — scanning TRABAJOS <year> on
a fake "P:" (a tmp_path tree, so no real network drive is involved) and
appending missing job/site combinations to the year's project_info xlsx.
Run with: pytest
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import openpyxl
import pytest

from src.project_info.reader import load_project_info
from src.project_info.sync_trabajos import sync_new_projects_from_trabajos

HEADERS = ["NUMERO DE PROYECTO", "NOMBRE", "PLANNING", "PROMOTOR"]


def _make_year_file(dir_path, year: str, rows: list[dict]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = year
    ws.append(HEADERS)
    for row in rows:
        ws.append([row.get(h) for h in HEADERS])
    Path(dir_path).mkdir(parents=True, exist_ok=True)
    wb.save(str(Path(dir_path) / f"{year}.xlsx"))


def _config(trabajos_root: Path) -> dict:
    return {"roots": [{"name": "trabajos", "root_path": str(trabajos_root)}]}


def _mkdirs(base: Path, *names: str) -> None:
    for name in names:
        (base / name).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------
# Combination detection
# ---------------------------------------------------------------------


def test_planos_marker_means_job_only_combination(tmp_path):
    trabajos_root = tmp_path / "P"
    trabajos_2026 = trabajos_root / "TRABAJOS 2026"
    job = trabajos_2026 / "26-001 AYTO TORRENT"
    _mkdirs(job, "00.-PLANOS")

    project_info_dir = tmp_path / "project_info"
    _make_year_file(project_info_dir, "2026", [])

    result = sync_new_projects_from_trabajos(_config(trabajos_root), str(project_info_dir), year=2026)

    assert result["added"] == [{"nombre": "AYTO TORRENT", "numero_de_proyecto": "26-001"}]
    rows = load_project_info(str(project_info_dir))
    assert rows[0]["project_name"] == "AYTO TORRENT"
    assert rows[0]["project_number"] == "26-001"
    # everything else left blank
    assert rows[0]["client"] is None
    assert rows[0]["status"] is None


def test_site_subfolders_produce_one_combination_each(tmp_path):
    trabajos_root = tmp_path / "P"
    trabajos_2026 = trabajos_root / "TRABAJOS 2026"
    job = trabajos_2026 / "26-001 AYTO TORRENT"
    _mkdirs(
        job,
        "26-001-01 URB-VENTETA Y PANTA",
        "26-001-02 CALLE MAYOR",
    )

    project_info_dir = tmp_path / "project_info"
    _make_year_file(project_info_dir, "2026", [])

    result = sync_new_projects_from_trabajos(_config(trabajos_root), str(project_info_dir), year=2026)

    added_names = {row["nombre"] for row in result["added"]}
    assert added_names == {
        "AYTO TORRENT - URB-VENTETA Y PANTA",
        "AYTO TORRENT - CALLE MAYOR",
    }
    codes = {row["numero_de_proyecto"] for row in result["added"]}
    assert codes == {"26-001-01", "26-001-02"}


def test_job_folder_with_no_planos_and_no_site_subfolders_falls_back_to_job_only(tmp_path):
    trabajos_root = tmp_path / "P"
    trabajos_2026 = trabajos_root / "TRABAJOS 2026"
    job = trabajos_2026 / "26-001 AYTO TORRENT"
    # A subfolder that does NOT match the site-code pattern (doesn't
    # start with the job's own code) and isn't a PLANOS marker either.
    _mkdirs(job, "DOCS VARIOS")

    project_info_dir = tmp_path / "project_info"
    _make_year_file(project_info_dir, "2026", [])

    result = sync_new_projects_from_trabajos(_config(trabajos_root), str(project_info_dir), year=2026)

    assert result["added"] == [{"nombre": "AYTO TORRENT", "numero_de_proyecto": "26-001"}]


# ---------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------


def test_000_mails_folder_is_skipped(tmp_path):
    trabajos_root = tmp_path / "P"
    trabajos_2026 = trabajos_root / "TRABAJOS 2026"
    _mkdirs(trabajos_2026, "26-000 MAILS")
    (trabajos_2026 / "26-000 MAILS" / "00.-PLANOS").mkdir()

    project_info_dir = tmp_path / "project_info"
    _make_year_file(project_info_dir, "2026", [])

    result = sync_new_projects_from_trabajos(_config(trabajos_root), str(project_info_dir), year=2026)

    assert result["added"] == []
    assert result["checked"] == 0


def test_non_job_folders_are_ignored(tmp_path):
    trabajos_root = tmp_path / "P"
    trabajos_2026 = trabajos_root / "TRABAJOS 2026"
    _mkdirs(trabajos_2026, "Miscellaneous notes", "26-000 MAILS")

    project_info_dir = tmp_path / "project_info"
    _make_year_file(project_info_dir, "2026", [])

    result = sync_new_projects_from_trabajos(_config(trabajos_root), str(project_info_dir), year=2026)
    assert result["added"] == []


# ---------------------------------------------------------------------
# Already-present combinations / idempotency
# ---------------------------------------------------------------------


def test_existing_combination_is_left_alone(tmp_path):
    trabajos_root = tmp_path / "P"
    trabajos_2026 = trabajos_root / "TRABAJOS 2026"
    job = trabajos_2026 / "26-001 AYTO TORRENT"
    _mkdirs(job, "26-001-01 URB-VENTETA Y PANTA")

    project_info_dir = tmp_path / "project_info"
    _make_year_file(project_info_dir, "2026", [
        {"NOMBRE": "AYTO TORRENT - URB-VENTETA Y PANTA", "PROMOTOR": "Ya existente"},
    ])

    result = sync_new_projects_from_trabajos(_config(trabajos_root), str(project_info_dir), year=2026)

    assert result["added"] == []
    assert result["already_present"] == 1
    rows = load_project_info(str(project_info_dir))
    assert len(rows) == 1
    # untouched — PROMOTOR wasn't cleared or overwritten
    assert rows[0]["client"] == "Ya existente"


def test_existing_match_is_case_and_whitespace_insensitive(tmp_path):
    trabajos_root = tmp_path / "P"
    trabajos_2026 = trabajos_root / "TRABAJOS 2026"
    job = trabajos_2026 / "26-001 AYTO TORRENT"
    _mkdirs(job, "00.-PLANOS")

    project_info_dir = tmp_path / "project_info"
    _make_year_file(project_info_dir, "2026", [
        {"NOMBRE": "  ayto   torrent  "},  # different case + extra whitespace
    ])

    result = sync_new_projects_from_trabajos(_config(trabajos_root), str(project_info_dir), year=2026)
    assert result["added"] == []
    assert result["already_present"] == 1


def test_running_twice_does_not_duplicate(tmp_path):
    trabajos_root = tmp_path / "P"
    trabajos_2026 = trabajos_root / "TRABAJOS 2026"
    job = trabajos_2026 / "26-001 AYTO TORRENT"
    _mkdirs(job, "26-001-01 URB-VENTETA Y PANTA")

    project_info_dir = tmp_path / "project_info"
    _make_year_file(project_info_dir, "2026", [])

    first = sync_new_projects_from_trabajos(_config(trabajos_root), str(project_info_dir), year=2026)
    second = sync_new_projects_from_trabajos(_config(trabajos_root), str(project_info_dir), year=2026)

    assert len(first["added"]) == 1
    assert second["added"] == []
    assert second["already_present"] == 1
    rows = load_project_info(str(project_info_dir))
    assert len(rows) == 1  # not duplicated


# ---------------------------------------------------------------------
# Multiple job folders together
# ---------------------------------------------------------------------


def test_multiple_job_folders_are_all_scanned(tmp_path):
    trabajos_root = tmp_path / "P"
    trabajos_2026 = trabajos_root / "TRABAJOS 2026"
    job_a = trabajos_2026 / "26-001 AYTO TORRENT"
    job_b = trabajos_2026 / "26-002 PLENOIL"
    _mkdirs(job_a, "00.-PLANOS")
    _mkdirs(job_b, "26-002-01 GASOLINERA NORTE")

    project_info_dir = tmp_path / "project_info"
    _make_year_file(project_info_dir, "2026", [])

    result = sync_new_projects_from_trabajos(_config(trabajos_root), str(project_info_dir), year=2026)
    names = {row["nombre"] for row in result["added"]}
    assert names == {"AYTO TORRENT", "PLENOIL - GASOLINERA NORTE"}


# ---------------------------------------------------------------------
# Edge cases: missing folder / missing config
# ---------------------------------------------------------------------


def test_missing_trabajos_year_folder_returns_empty_result_not_an_error(tmp_path):
    trabajos_root = tmp_path / "P"  # "TRABAJOS 2026" itself never created
    trabajos_root.mkdir()

    project_info_dir = tmp_path / "project_info"
    _make_year_file(project_info_dir, "2026", [])

    result = sync_new_projects_from_trabajos(_config(trabajos_root), str(project_info_dir), year=2026)
    assert result["added"] == []
    assert result["checked"] == 0


def test_missing_trabajos_root_in_config_returns_empty_result(tmp_path):
    project_info_dir = tmp_path / "project_info"
    _make_year_file(project_info_dir, "2026", [])

    result = sync_new_projects_from_trabajos({"roots": []}, str(project_info_dir), year=2026)
    assert result["trabajos_dir"] is None
    assert result["added"] == []


def test_missing_year_file_raises(tmp_path):
    trabajos_root = tmp_path / "P"
    trabajos_2026 = trabajos_root / "TRABAJOS 2026"
    job = trabajos_2026 / "26-001 AYTO TORRENT"
    _mkdirs(job, "00.-PLANOS")

    project_info_dir = tmp_path / "project_info"  # 2026.xlsx never created
    project_info_dir.mkdir()

    with pytest.raises(FileNotFoundError):
        sync_new_projects_from_trabajos(_config(trabajos_root), str(project_info_dir), year=2026)