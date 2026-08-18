"""
Scans P:\\TRABAJOS <year> (the crawled "trabajos" root — see
config.example.yaml's roots[].root_path/year_folder_pattern) and appends
any job/site combination that isn't already in that year's project_info
NOMBRE column. A completely separate, opt-in action from the "Actualizar"
button (src/webapp/pipeline.py) — that one rebuilds the crawled P: index
(db_path); this one only ever appends new rows to the hand-maintained
{year}.xlsx via src/project_info/writer.py, never touches the crawled
index at all, and never modifies an existing row.

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
    job name on its own, and the code written to NUMERO DE PROYECTO is
    the job code itself (e.g. "26-001").
  - Otherwise, every immediate subfolder that matches the job's own
    site-code pattern (crawl.py's parse_site_folder — must literally
    start with the job's own code, e.g. "26-001-01 URB-VENTETA Y PANTA"
    under job "26-001") contributes one combination "<job name> - <site
    name>", with that subfolder's own code (e.g. "26-001-01") written to
    NUMERO DE PROYECTO. A job folder with neither a PLANOS marker nor any
    matching site subfolder still gets ONE job-only combination, same as
    the PLANOS case, so it's never silently skipped entirely.

For each combination: if a row with that exact NOMBRE (whitespace-
collapsed, case-insensitive) already exists in this year's file, it's
left alone. Otherwise a brand-new row is appended with NOMBRE=combination
and NUMERO DE PROYECTO=<code> — every other column left blank, exactly as
requested (see writer.append_project_info_row).
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

from src.crawler.crawl import parse_job_folder, parse_site_folder
from src.project_info.reader import load_project_info
from src.project_info.writer import append_project_info_row

# Matches the "00.-PLANOS" category folder in its various real-world
# spellings ("00.-PLANOS", "00. -PLANOS", "00 PLANOS", "00.-PLANOS
# PREVIOS", ...) without being tripped up by a genuine site-code folder
# that happens to start "00" (those are still required to start with the
# job's own full code first, via parse_site_folder, so they never even
# reach this check).
_PLANOS_MARKER_RE = re.compile(r"^00\s*[.\-]*\s*.*PLANOS", re.IGNORECASE)


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


def _has_planos_marker(job_dir: Path) -> bool:
    try:
        entries = list(job_dir.iterdir())
    except OSError:
        return False
    return any(
        entry.is_dir() and _PLANOS_MARKER_RE.match(entry.name.strip())
        for entry in entries
    )


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
    """Yields (code, combination_text) pairs for one job folder — one
    "<job name> - <site name>" pair per recognizable site subfolder, or a
    single job-only pair if there's a PLANOS marker (no location) or no
    recognizable site subfolders at all."""
    if _has_planos_marker(job_dir):
        yield job_code, job_name
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
        yield site_code, f"{job_name} - {site_name.strip()}"

    if not found_any:
        yield job_code, job_name


def sync_new_projects_from_trabajos(
    config: dict, project_info_dir: str, year: int | None = None
) -> dict:
    """Scan TRABAJOS <year> (year defaults to today's calendar year) and
    append any missing job/site combination to {project_info_dir}/
    {year}.xlsx's NOMBRE column (with NUMERO DE PROYECTO also filled in,
    everything else left blank). Never modifies or removes an existing
    row — additive only, and safe to run repeatedly (a combination
    that's already present is simply left alone, see _normalize_name).

    Returns a summary dict:
        {"year": int, "trabajos_dir": str | None, "checked": int,
         "already_present": int, "added": [{"nombre": str, "numero_de_
         proyecto": str}, ...]}

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

    existing_names: set[str] = set()
    for row in load_project_info(project_info_dir):
        if row["year"] == year and row.get("project_name"):
            existing_names.add(_normalize_name(row["project_name"]))

    result = {
        "year": year,
        "trabajos_dir": str(trabajos_dir) if trabajos_dir else None,
        "checked": 0,
        "already_present": 0,
        "added": [],
    }

    if trabajos_dir is None or not trabajos_dir.exists():
        return result

    for job_dir, job_code, job_name in _iter_job_folders(trabajos_dir, year):
        for code, combination in _iter_combinations(job_dir, job_code, job_name):
            result["checked"] += 1
            normalized = _normalize_name(combination)
            if normalized in existing_names:
                result["already_present"] += 1
                continue
            append_project_info_row(
                project_info_dir,
                year,
                {"project_name": combination, "project_number": code},
            )
            existing_names.add(normalized)
            result["added"].append({"nombre": combination, "numero_de_proyecto": code})

    return result