"""
Scans P:\\TRABAJOS <year> (the crawled "trabajos" root — see
config.example.yaml's roots[].root_path/year_folder_pattern) and appends
any job/site combination that isn't already in that year's project_info
NOMBRE column, plus any "subproject" folders found alongside a job/site's
own "00.-PLANOS" marker. A completely separate, opt-in action from the
"Actualizar" button (src/webapp/pipeline.py) — that one rebuilds the
crawled P: index (db_path); this one only ever appends new rows (and, for
subprojects, sets a "flag" cell on an existing row) in the hand-
maintained {year}.xlsx via src/project_info/writer.py, never touches the
crawled index at all, and never modifies or removes any OTHER row/cell.

Combination rules (per explicit spec):
  - A job folder directly under TRABAJOS <year> whose name matches
    src/crawler/crawl.py's JOB_CODE_RE ("YY-NNN NAME", e.g.
    "26-001 AYTO TORRENT") is a candidate. Its job_code must start with
    the current year's 2-digit prefix (so a stray/misfiled folder from a
    different year sitting under the wrong TRABAJOS folder is ignored),
    and any job folder whose own code ends in "-000" (e.g. "26-000
    MAILS") is skipped outright — never a real project.
  - "part one" of the combination is the job name (e.g. "AYTO TORRENT").
  - If the job folder has an immediate subfolder that looks like the
    "00.-PLANOS" category folder (starts with "00", contains "PLANOS",
    case-insensitive), there's no location: the combination is just the
    job name on its own ("unit_dir" = the job folder itself), and the
    code written to NUMERO DE PROYECTO is the job code itself (e.g.
    "26-001").
  - Otherwise, every immediate subfolder that matches the job's own
    site-code pattern (crawl.py's parse_site_folder — must literally
    start with the job's own code, e.g. "26-001-01 URB-VENTETA Y PANTA"
    under job "26-001") contributes one combination "<job name> - <site
    name>" ("unit_dir" = that site subfolder), with that subfolder's own
    code (e.g. "26-001-01") written to NUMERO DE PROYECTO. A job folder
    with neither a PLANOS marker nor any matching site subfolder still
    gets ONE job-only combination ("unit_dir" = the job folder), same as
    the PLANOS case, so it's never silently skipped entirely.

  For each combination: if a row with that exact NOMBRE (whitespace-
  collapsed, case-insensitive) already exists in this year's file, it's
  left alone. Otherwise a brand-new row is appended with
  NOMBRE=combination and NUMERO DE PROYECTO=<code> — every other column
  left blank.

Subproject detection (runs for EVERY combination above, whether it's a
job-only or a job-site one — the same check, just against a different
unit_dir each time):
  - If "unit_dir" itself contains a "00.-PLANOS"-style marker folder
    (this is always true for the job-only case, since that's what put it
    in that branch in the first place; for a site folder it's checked
    independently — some sites have their own "00.-PLANOS", some don't),
    look at unit_dir's other immediate subfolders whose name does NOT
    start with a digit (so "00.-PLANOS", "01.-DOC DE REFERENCIA", etc.
    are excluded; a differently-named folder like "MEMORIA URBANIZACION"
    qualifies).
  - If one or more exist: set the "flag" column to "TRUE" on the
    combination's own row (whether that row was just newly appended
    above, or already existed from a previous run — either way, via
    writer.update_project_info_row this time, since the row may not be
    brand new).
  - For each such folder, append ANOTHER new row (same "already exists"
    idempotency check as above): NOMBRE = "<combination> - <folder
    name>", NUMERO DE PROYECTO = the SAME code as the parent combination
    (these folders don't have their own separate number), and
    "subprojects" = the folder's own name.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

from src.crawler.crawl import parse_job_folder, parse_site_folder
from src.project_info.reader import load_project_info
from src.project_info.writer import append_project_info_row, update_project_info_row

# Matches the "00.-PLANOS" category folder in its various real-world
# spellings ("00.-PLANOS", "00. -PLANOS", "00 PLANOS", "00.-PLANOS
# PREVIOS", ...) without being tripped up by a genuine site-code folder
# that happens to start "00" (those are still required to start with the
# job's own full code first, via parse_site_folder, so they never even
# reach this check).
_PLANOS_MARKER_RE = re.compile(r"^00\s*[.\-]*\s*.*PLANOS", re.IGNORECASE)

# A subproject-candidate folder's name must NOT start with a digit — this
# is what separates a genuine "extra" folder (e.g. "MEMORIA URBANIZACION")
# from the routine numbered category structure ("00.-PLANOS",
# "01.-DOC DE REFERENCIA", "02.-GESTIÓN", ...) that sits in every job/site
# folder and should never itself be treated as a subproject.
_STARTS_WITH_DIGIT_RE = re.compile(r"^\d")


def _normalize_name(value: str) -> str:
    return " ".join((value or "").split()).casefold()


def _trabajos_root(config: dict, year: int) -> Path | None:
    """The absolute path to TRABAJOS <year> (e.g. P:\\TRABAJOS 2026),
    derived from the "trabajos" entry in config["roots"] rather than
    hard-coded, so this stays correct if root_path is ever reconfigured
    for a different drive letter/mount. Returns None if that root isn't
    configured at all (config.yaml missing the "trabajos" entry)."""
    for root in config.get("roots", []):
        if root.get("name") == "trabajos":
            return Path(root["root_path"]) / f"TRABAJOS {year}"
    return None


def _has_planos_marker(unit_dir: Path) -> bool:
    try:
        entries = list(unit_dir.iterdir())
    except OSError:
        return False
    return any(
        entry.is_dir() and _PLANOS_MARKER_RE.match(entry.name.strip())
        for entry in entries
    )


def _iter_subproject_folder_names(unit_dir: Path):
    """Yields the names of unit_dir's own immediate subfolders whose name
    does not start with a digit — see the module docstring's "Subproject
    detection" section. Only ever called after _has_planos_marker(unit_dir)
    already confirmed a "00.-PLANOS"-style folder is present there."""
    try:
        entries = sorted(unit_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_dir() and not _STARTS_WITH_DIGIT_RE.match(entry.name.strip()):
            yield entry.name.strip()


def _iter_job_folders(trabajos_dir: Path, year: int):
    """Yields (job_dir, job_code, job_name) for every real project folder
    directly under trabajos_dir — see this module's docstring for the
    "YY-" prefix + "-000" exclusion rules."""
    year_prefix = f"{year % 100:02d}-"
    try:
        entries = sorted(trabajos_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.is_dir():
            continue
        parsed = parse_job_folder(entry.name)
        if not parsed:
            continue
        job_code, job_name = parsed
        if not job_code.startswith(year_prefix):
            continue
        if job_code.endswith("-000"):
            continue  # e.g. "26-000 MAILS" — not a real project folder
        yield entry, job_code, job_name.strip()


def _iter_combinations(job_dir: Path, job_code: str, job_name: str):
    """Yields (code, combination_text, unit_dir) for one job folder — one
    "<job name> - <site name>" triple per recognizable site subfolder
    (unit_dir=that site folder), or a single job-only triple
    (unit_dir=job_dir) if there's a PLANOS marker directly in the job
    folder or no recognizable site subfolders at all."""
    if _has_planos_marker(job_dir):
        yield job_code, job_name, job_dir
        return

    try:
        entries = sorted(job_dir.iterdir())
    except OSError:
        entries = []

    found_any = False
    for entry in entries:
        if not entry.is_dir():
            continue
        parsed = parse_site_folder(entry.name, job_code)
        if not parsed:
            continue
        site_code, site_name = parsed
        found_any = True
        yield site_code, f"{job_name} - {site_name.strip()}", entry

    if not found_any:
        yield job_code, job_name, job_dir


def sync_new_projects_from_trabajos(
    config: dict, project_info_dir: str, year: int | None = None
) -> dict:
    """Scan TRABAJOS <year> (year defaults to today's calendar year) and
    append any missing job/site combination to {project_info_dir}/
    {year}.xlsx's NOMBRE column (with NUMERO DE PROYECTO also filled in),
    plus one row per detected subproject folder and a "flag" on its
    parent row — see the module docstring for the exact rules. Never
    modifies or removes an existing row's OTHER cells — the only writes
    are brand-new rows, and "flag" being set to "TRUE" on a combination's
    own row. Safe to run repeatedly: an already-present combination (job/
    site or subproject) is simply left alone, and "flag" being written
    "TRUE" again on a row that's already "TRUE" is a harmless no-op.

    Returns a summary dict:
        {"year": int, "trabajos_dir": str | None, "checked": int,
         "already_present": int,
         "added": [{"nombre": str, "numero_de_proyecto": str}, ...],
         "flagged": [{"nombre": str, "numero_de_proyecto": str}, ...]}
    "checked" and "already_present" count subproject combinations too,
    not just job/site ones. "flagged" lists every combination whose row
    got (or already had) "flag" set this run, whether or not any of its
    subproject rows were newly added just now.

    Raises FileNotFoundError if {project_info_dir}/{year}.xlsx doesn't
    exist (same as writer.append_project_info_row) — this only ever adds
    rows to an already-existing year file, it never creates one from
    scratch. A missing/unreachable TRABAJOS <year> folder on P: is NOT an
    error (a network drive can be slow or briefly unreachable) — it's
    reported back as checked=0/added=[] instead, so the caller can show
    that as a clear message rather than a crash."""
    if year is None:
        year = datetime.date.today().year

    trabajos_dir = _trabajos_root(config, year)

    # NOMBRE (normalized) -> row_number — doubles as the "already exists"
    # check AND as how "flag" gets written onto a combination's row
    # regardless of whether that row already existed before this run or
    # was just appended a moment ago in the loop below.
    row_number_by_name: dict[str, int] = {}
    for row in load_project_info(project_info_dir):
        if row["year"] == year and row.get("project_name"):
            row_number_by_name[_normalize_name(row["project_name"])] = row["row_number"]

    result = {
        "year": year,
        "trabajos_dir": str(trabajos_dir) if trabajos_dir else None,
        "checked": 0,
        "already_present": 0,
        "added": [],
        "flagged": [],
    }

    if trabajos_dir is None or not trabajos_dir.exists():
        return result

    for job_dir, job_code, job_name in _iter_job_folders(trabajos_dir, year):
        for code, combination, unit_dir in _iter_combinations(job_dir, job_code, job_name):
            result["checked"] += 1
            normalized = _normalize_name(combination)
            row_number = row_number_by_name.get(normalized)
            if row_number is None:
                row_number = append_project_info_row(
                    project_info_dir,
                    year,
                    {"project_name": combination, "project_number": code},
                )
                row_number_by_name[normalized] = row_number
                result["added"].append({"nombre": combination, "numero_de_proyecto": code})
            else:
                result["already_present"] += 1

            if not _has_planos_marker(unit_dir):
                continue
            subproject_names = list(_iter_subproject_folder_names(unit_dir))
            if not subproject_names:
                continue

            update_project_info_row(project_info_dir, year, row_number, {"flag": "TRUE"})
            result["flagged"].append({"nombre": combination, "numero_de_proyecto": code})

            for sub_name in subproject_names:
                sub_combination = f"{combination} - {sub_name}"
                result["checked"] += 1
                sub_normalized = _normalize_name(sub_combination)
                if sub_normalized in row_number_by_name:
                    result["already_present"] += 1
                    continue
                sub_row_number = append_project_info_row(
                    project_info_dir,
                    year,
                    {
                        "project_name": sub_combination,
                        "project_number": code,
                        "subprojects": sub_name,
                    },
                )
                row_number_by_name[sub_normalized] = sub_row_number
                result["added"].append({"nombre": sub_combination, "numero_de_proyecto": code})

    return result
