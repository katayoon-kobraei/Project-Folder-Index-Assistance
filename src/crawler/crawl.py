"""
Walks the TRABAJOS <year> archive and writes folder metadata into SQLite.

Folder depth convention (matches src/db/schema.sql):
    0 = year folder          e.g. TRABAJOS 2022
    1 = job folder            e.g. 22-007 AYTO TORRENT
    2 = site folder           e.g. 22-007-02 CALLE SAN LUIS BELTRAN
    3+ = category / subfolders inside a site (00-PLANOS PREVIOS, etc.),
         and anything nested deeper, including "revision" folders that
         hold a later year's update inside an older job/site folder.
"""

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.db.db import upsert_folder, rebuild_fts

# Job code: YY-N to YY-NNNN, followed by at least one separator (space,
# dash, or dot — e.g. "22-007 NAME", "14-001.- NAME", "14-023-NAME",
# "09-44 NAME"). Real folder names in the archive use all of these.
JOB_CODE_RE = re.compile(r"^(\d{2}-\d{2,4})[.\-\s]+(.+)$")
SITE_CODE_RE = re.compile(r"^(\d{2}-\d{2,4}-\d{1,2})[.\-\s]+(.+)$")
YEAR_IN_NAME_RE = re.compile(r"\b(20\d{2})\b")

# Windows' legacy MAX_PATH limit (260 chars) breaks on deeply nested
# archives (CAD model libraries, revision-inside-revision folders). The
# \\?\ prefix tells the Win32 API to bypass that limit. No-op on non-Windows.
WIN_LONG_PATH_PREFIX = "\\\\?\\"


def _to_long_path(path: str) -> str:
    if os.name != "nt":
        return path
    if path.startswith(WIN_LONG_PATH_PREFIX):
        return path
    return WIN_LONG_PATH_PREFIX + os.path.abspath(path)


def _strip_long_path_prefix(path: str) -> str:
    if path.startswith(WIN_LONG_PATH_PREFIX):
        return path[len(WIN_LONG_PATH_PREFIX):]
    return path


def parse_job_folder(name: str):
    """Return (job_code, job_name) if `name` matches 'YY-NNN NAME', else None."""
    m = JOB_CODE_RE.match(name)
    return m.groups() if m else None


def parse_site_folder(name: str, job_code: str | None = None):
    """
    Return (site_code, site_name) if `name` matches 'YY-NNN-SS NAME', else None.

    If `job_code` is given, the site code's job-prefix (everything before
    the last "-SS" segment) must exactly match it. Without this check, dated
    filenames like "16-01-29 (ver) 15039 caravanas puzol" or
    "27-10-15 MJESUS DOÑATE..." — which are just day-month-year dates, not
    site codes — get misread as sites belonging to an unrelated job.
    """
    m = SITE_CODE_RE.match(name)
    if not m:
        return None
    site_code, site_name = m.groups()
    if job_code is not None:
        job_prefix = site_code.rsplit("-", 1)[0]
        if job_prefix != job_code:
            return None
    return site_code, site_name


def has_year_mismatch(name: str, folder_year: int) -> bool:
    """
    True if `name` contains a 4-digit year different from the year folder
    it lives under — e.g. "PROY MEJORA PEATONAL DIC 2024" sitting inside
    TRABAJOS 2022. This is the "nested revision" pattern: a later-year
    update dumped inside an earlier job/site folder instead of getting a
    new job code.
    """
    years_found = YEAR_IN_NAME_RE.findall(name)
    return any(int(y) != folder_year for y in years_found)


def _scan_dirs(path, ignore_names: set):
    """Yield subdirectories of `path`, skipping ignored names and anything
    that errors out (permission issues, or — on Windows — paths that
    still exceed even the long-path limit)."""
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.name in ignore_names:
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        yield entry
                except OSError as e:
                    print(f"  [skip] {_strip_long_path_prefix(entry.path)}: {e}")
    except (PermissionError, OSError) as e:
        print(f"  [skip] {_strip_long_path_prefix(str(path))}: {e}")


def _file_count(path) -> int:
    try:
        with os.scandir(path) as it:
            return sum(1 for e in it if e.is_file(follow_symlinks=False))
    except OSError:
        return 0


def _row_from_entry(entry, depth, year, job_code, job_name, site_code, site_name):
    try:
        stat = entry.stat(follow_symlinks=False)
        modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        modified_at = None
    return {
        "path": _strip_long_path_prefix(entry.path),
        "depth": depth,
        "name": entry.name,
        "year": year,
        "job_code": job_code,
        "job_name": job_name,
        "site_code": site_code,
        "site_name": site_name,
        "modified_at": modified_at,
        "file_count": _file_count(entry.path),
        "is_revision_hint": int(has_year_mismatch(entry.name, year)),
    }


def _walk_children(conn, parent_path, parent_depth, year, job_code, job_name,
                    site_code, site_name, ignore_names, stats):
    this_depth = parent_depth + 1
    for entry in _scan_dirs(parent_path, ignore_names):
        new_job_code, new_job_name = job_code, job_name
        new_site_code, new_site_name = site_code, site_name

        if this_depth == 1 and job_code is None:
            parsed = parse_job_folder(entry.name)
            if parsed:
                new_job_code, new_job_name = parsed
        elif this_depth == 2 and job_code is not None and site_code is None:
            parsed = parse_site_folder(entry.name, job_code=job_code)
            if parsed:
                new_site_code, new_site_name = parsed

        row = _row_from_entry(entry, this_depth, year, new_job_code, new_job_name,
                               new_site_code, new_site_name)
        upsert_folder(conn, row)
        stats["folders"] += 1
        if row["is_revision_hint"]:
            stats["revision_hints"] += 1
        if stats["folders"] % 500 == 0:
            print(f"  ...{stats['folders']} folders indexed")

        # entry.path already carries the \\?\ prefix if the parent scan did,
        # so long-path support propagates automatically down the recursion.
        _walk_children(conn, entry.path, this_depth, year, new_job_code, new_job_name,
                        new_site_code, new_site_name, ignore_names, stats)


def crawl(conn, root_path: str, config: dict) -> dict:
    """
    Walk root_path, keeping only top-level folders matching
    config['year_folder_pattern'], and write every folder found beneath
    them into the `folders` table. Returns a small stats dict.
    """
    ignore_names = set(config.get("ignore_names", []))
    year_pattern = re.compile(config["year_folder_pattern"])
    root = _to_long_path(root_path)

    stats = {"years": 0, "folders": 0, "revision_hints": 0}

    year_entries = sorted(_scan_dirs(root, ignore_names), key=lambda e: e.name)
    for entry in year_entries:
        m = year_pattern.match(entry.name)
        if not m:
            continue
        year = int(m.group(1))
        stats["years"] += 1
        print(f"Crawling {entry.name} ...")

        year_row = _row_from_entry(entry, 0, year, None, None, None, None)
        upsert_folder(conn, year_row)
        stats["folders"] += 1

        _walk_children(conn, entry.path, 0, year, None, None, None, None,
                        ignore_names, stats)

    conn.commit()
    print("Rebuilding search index...")
    rebuild_fts(conn)
    print(f"Done. {stats['years']} year folders, {stats['folders']} folders total, "
          f"{stats['revision_hints']} flagged as possible nested revisions.")
    return stats