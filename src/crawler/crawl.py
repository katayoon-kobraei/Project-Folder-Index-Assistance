"""
Walks the archive and writes both folder and file metadata into SQLite.

The archive has two separate root trees, each configured in config.yaml
under "roots":
    trabajos -> P:\\TRABAJOS <year>\\YY-NNN NAME\\YY-NNN-SS NAME\\...
    ofertas  -> P:\\OFERTAS Y CONCURSOS\\<year>\\NNN.- NAME\\...

Folder depth convention (matches src/db/schema.sql), relative to each root:
    0 = year folder          e.g. TRABAJOS 2022, or OFERTAS Y CONCURSOS\\2025
    1 = job/offer folder     e.g. 22-007 AYTO TORRENT, or 001.- JUVACAR BENETUSSER
    2 = site folder (trabajos only) e.g. 22-007-02 CALLE SAN LUIS BELTRAN
    3+ = category / subfolders, and anything nested deeper, including
         "revision" folders that hold a later year's update inside an
         older job/site folder.

Every folder visited also has its own files recorded into the `files`
table (name, extension, modified date, size) — not recursively, just the
files sitting directly inside that folder.
"""

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.db.db import upsert_folder, upsert_file, rebuild_fts

# Job code (trabajos): YY-N to YY-NNNN, followed by at least one separator
# (space, dash, or dot) — e.g. "22-007 NAME", "14-001.- NAME", "14-023-NAME".
JOB_CODE_RE = re.compile(r"^(\d{2}-\d{2,4})[.\-\s]+(.+)$")

# Site code (trabajos): YY-NNN-SS, same flexible separator.
SITE_CODE_RE = re.compile(r"^(\d{2}-\d{2,4}-\d{1,2})[.\-\s]+(.+)$")

# Offer code (ofertas): a plain sequential number, no year/dash — e.g.
# "001.- NAME", "037.NAME", "013.-NAME".
OFFER_CODE_RE = re.compile(r"^(\d{2,4})[.\-\s]+(.+)$")

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


def parse_offer_folder(name: str):
    """Return (offer_code, offer_name) if `name` matches 'NNN.- NAME'
    (or 'NNN.NAME', 'NNN NAME'), else None. Used for the OFERTAS Y
    CONCURSOS tree, which numbers offers sequentially with no year/dash
    embedded, unlike trabajos job codes."""
    m = OFFER_CODE_RE.match(name)
    return m.groups() if m else None


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


def _scan_entries(path, ignore_names: set):
    """Yield all entries (files and dirs) of `path`, skipping ignored names
    and anything that errors out (permission issues, or — on Windows —
    paths that still exceed even the long-path limit)."""
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.name in ignore_names:
                    continue
                yield entry
    except (PermissionError, OSError) as e:
        print(f"  [skip] {_strip_long_path_prefix(str(path))}: {e}")


def _scan_dirs(path, ignore_names: set):
    for entry in _scan_entries(path, ignore_names):
        try:
            if entry.is_dir(follow_symlinks=False):
                yield entry
        except OSError as e:
            print(f"  [skip] {_strip_long_path_prefix(entry.path)}: {e}")


def _file_count(path) -> int:
    try:
        with os.scandir(path) as it:
            return sum(1 for e in it if e.is_file(follow_symlinks=False))
    except OSError:
        return 0


def _row_from_entry(entry, depth, year, job_code, job_name, site_code, site_name, source):
    try:
        stat = entry.stat(follow_symlinks=False)
        modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        modified_at = None
    return {
        "path": _strip_long_path_prefix(entry.path),
        "source": source,
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


def _record_files(conn, folder_path, folder_id, ignore_names: set, stats):
    """Record every file sitting directly inside `folder_path` (not
    recursive — subfolders get their own call when the walk reaches them)."""
    for entry in _scan_entries(folder_path, ignore_names):
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            stat = entry.stat(follow_symlinks=False)
            modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        except OSError as e:
            print(f"  [skip file] {_strip_long_path_prefix(entry.path)}: {e}")
            continue

        name = entry.name
        extension = os.path.splitext(name)[1].lower().lstrip(".")
        upsert_file(conn, {
            "path": _strip_long_path_prefix(entry.path),
            "folder_id": folder_id,
            "name": name,
            "extension": extension,
            "modified_at": modified_at,
            "size_bytes": stat.st_size,
        })
        stats["files"] += 1


def _walk_children(conn, parent_path, parent_depth, year, job_code, job_name,
                    site_code, site_name, ignore_names, stats, code_kind, source):
    this_depth = parent_depth + 1
    for entry in _scan_dirs(parent_path, ignore_names):
        new_job_code, new_job_name = job_code, job_name
        new_site_code, new_site_name = site_code, site_name

        if this_depth == 1 and job_code is None:
            if code_kind == "offer":
                parsed = parse_offer_folder(entry.name)
            else:
                parsed = parse_job_folder(entry.name)
            if parsed:
                new_job_code, new_job_name = parsed
        elif (this_depth == 2 and code_kind == "job"
              and job_code is not None and site_code is None):
            parsed = parse_site_folder(entry.name, job_code=job_code)
            if parsed:
                new_site_code, new_site_name = parsed

        row = _row_from_entry(entry, this_depth, year, new_job_code, new_job_name,
                               new_site_code, new_site_name, source)
        folder_id = upsert_folder(conn, row)
        stats["folders"] += 1
        if row["is_revision_hint"]:
            stats["revision_hints"] += 1
        _record_files(conn, entry.path, folder_id, ignore_names, stats)

        if stats["folders"] % 500 == 0:
            print(f"  ...{stats['folders']} folders, {stats['files']} files indexed")

        # entry.path already carries the \\?\ prefix if the parent scan did,
        # so long-path support propagates automatically down the recursion.
        _walk_children(conn, entry.path, this_depth, year, new_job_code, new_job_name,
                        new_site_code, new_site_name, ignore_names, stats, code_kind, source)


def _crawl_root(conn, root_spec: dict, ignore_names: set, stats: dict):
    root_path = root_spec["root_path"]
    year_pattern = re.compile(root_spec["year_folder_pattern"])
    code_kind = root_spec.get("code_kind", "job")
    source = root_spec.get("name", "default")
    root = _to_long_path(root_path)

    year_entries = sorted(_scan_dirs(root, ignore_names), key=lambda e: e.name)
    for entry in year_entries:
        m = year_pattern.match(entry.name)
        if not m:
            continue
        year = int(m.group(1))
        stats["years"] += 1
        print(f"[{source}] Crawling {entry.name} ...")

        year_row = _row_from_entry(entry, 0, year, None, None, None, None, source)
        folder_id = upsert_folder(conn, year_row)
        stats["folders"] += 1
        _record_files(conn, entry.path, folder_id, ignore_names, stats)

        _walk_children(conn, entry.path, 0, year, None, None, None, None,
                        ignore_names, stats, code_kind, source)


def crawl(conn, config: dict) -> dict:
    """
    Walk every root defined in config['roots'], each with its own
    root_path / year_folder_pattern / code_kind ('job' or 'offer'), and
    write every folder and file found beneath them into the database.
    Returns a small stats dict.
    """
    ignore_names = set(config.get("ignore_names", []))
    stats = {"years": 0, "folders": 0, "files": 0, "revision_hints": 0}

    for root_spec in config["roots"]:
        _crawl_root(conn, root_spec, ignore_names, stats)

    conn.commit()
    print("Rebuilding search index...")
    rebuild_fts(conn)
    print(f"Done. {stats['years']} year folders, {stats['folders']} folders, "
          f"{stats['files']} files total, "
          f"{stats['revision_hints']} flagged as possible nested revisions.")
    return stats