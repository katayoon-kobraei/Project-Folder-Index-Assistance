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

Incremental rescans (config['incremental'], default True): a folder's own
"modified date" only changes when something is directly added, removed,
or renamed INSIDE it — not when a file several levels deeper changes. So
if a folder's mtime matches what's already stored from the last crawl,
its own list of files can safely be skipped this run (nothing was
added/removed/renamed there since). Every folder still gets walked and
checked individually every run — a change deep in the tree doesn't
"bubble up" an mtime change to its ancestors, so skipping recursion based
on a parent's unchanged mtime would miss it. Only the (expensive, because
it's per-file network stat calls) file-listing step is skipped, not the
folder walk itself.

Caveat: this catches files being added/removed/renamed. It does NOT catch
a file being edited in place (re-saved) without the folder's own entry
list changing, since that only updates the file's own mtime, not its
parent folder's. Run occasionally with incremental: false (or
`--full` on scripts/run_crawl.py) for a guaranteed fully fresh scan.
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

# Site code (trabajos): a subfolder directly under a job folder is a
# "site/location" only if its name starts by repeating that job's own
# code, followed by a separator and a number, e.g. job "14-002" contains
# site folder "14-002.001 CONSUM LA ZENIA" (dot + 3-digit counter) or the
# older "22-007-02 CALLE SAN LUIS BELTRAN" (dash + 2-digit counter) style.
# Deliberately NOT used: some companies (e.g. TOKHEIM) number their site
# folders "01.- NAME", "02.- NAME" instead of repeating the job code —
# identical in shape to a plain category folder ("00.-PLANOS", "01.-DOC
# DE REFERENCIA"), so there's no reliable way to tell those apart from the
# name alone. Per explicit instruction, those are left unhandled for now
# (site/location stays NULL) and will be fixed up manually later.
SITE_SUFFIX_RE = re.compile(r"^([.\-]\s*\d{1,4})[.\-\s]+(.+)$")

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
    Return (site_code, site_name) if `name` starts with the job's own
    `job_code`, followed by a separator and a number, followed by the
    actual site name — e.g. job_code="14-002" matches
    "14-002.001 CONSUM LA ZENIA" -> ("14-002.001", "CONSUM LA ZENIA").

    Requiring the literal job_code as a prefix (rather than just any
    "YY-NNN-SS"-shaped string) is what rejects dated filenames like
    "16-01-29 (ver) 15039 caravanas puzol" or "27-10-15 MJESUS DOÑATE
    (ELEVAL) plantas" — they happen to look like codes, but they don't
    start with the job they're actually sitting under, so they're
    correctly left unmatched.
    """
    if not job_code or not name.startswith(job_code):
        return None
    remainder = name[len(job_code):]
    m = SITE_SUFFIX_RE.match(remainder)
    if not m:
        return None
    counter_segment, site_name = m.groups()
    site_code = job_code + counter_segment.strip()
    return site_code, site_name.strip()


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


def _record_files(conn, folder_path, folder_id, ignore_names: set, stats,
                   company_project, file_year, location_site):
    """Record every file sitting directly inside `folder_path` (not
    recursive — subfolders get their own call when the walk reaches them).

    `company_project` and `file_year` come from the enclosing folder's own
    row (already inherited down from the depth=1 job/offer folder and the
    depth=0 year folder respectively by the walk) — every file gets tagged
    with whichever company/project and year its containing folder belongs
    to, however deep it's nested. `location_site` is the same kind of
    inheritance from whichever site folder (if any) matched
    parse_site_folder() further up the path — NULL for anything above or
    outside a recognized site folder."""
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
            "company_project": company_project,
            "file_year": file_year,
            "location_site": location_site,
        })
        stats["files"] += 1


def _load_existing_modified_map(conn) -> dict:
    """path -> stored modified_at, loaded once so the incremental check is
    an in-memory dict lookup rather than one SQL query per folder."""
    return dict(conn.execute("SELECT path, modified_at FROM folders").fetchall())


def _mtime_to_second(iso_str: str | None) -> str | None:
    """Truncate an ISO timestamp to whole-second precision, dropping the
    fractional-second part but keeping the timezone suffix — e.g.
    "2026-08-04T11:27:58.572845+00:00" -> "2026-08-04T11:27:58+00:00".

    Windows/NTFS can report a folder's Last Write Time as still settling
    for a brief moment after a change — two reads of a genuinely unchanged
    folder, seconds apart, have been observed to differ by under a
    millisecond. Comparing at whole-second resolution absorbs that noise.
    The tradeoff: a real change that happens in the same one-second window
    as a refresh could in theory be missed until the next refresh — the
    same accepted limitation already noted for in-place file edits in the
    module docstring above, and not something that comes up in practice."""
    if iso_str is None:
        return None
    if "." not in iso_str:
        return iso_str
    date_part, rest = iso_str.split(".", 1)
    tz_index = max(rest.find("+"), rest.find("-"))
    tz_suffix = rest[tz_index:] if tz_index != -1 else ""
    return date_part + tz_suffix


def _maybe_record_files(conn, path, folder_id, ignore_names, stats,
                         row, existing_modified: dict, incremental: bool):
    """Record this folder's files, unless incremental mode is on and its
    mtime (compared to whole-second precision, see _mtime_to_second)
    matches what was already stored — in which case nothing directly
    inside it has been added/removed/renamed since last crawl."""
    previous_mtime = existing_modified.get(row["path"])
    unchanged = (
        incremental
        and previous_mtime is not None
        and row["modified_at"] is not None
        and _mtime_to_second(previous_mtime) == _mtime_to_second(row["modified_at"])
    )
    if unchanged:
        stats["folders_skipped"] += 1
        return
    _record_files(conn, path, folder_id, ignore_names, stats,
                  company_project=row["job_name"], file_year=row["year"],
                  location_site=row["site_name"])
    stats["folders_rescanned"] += 1


def _walk_children(conn, parent_path, parent_depth, year, job_code, job_name,
                    site_code, site_name, ignore_names, stats, code_kind, source,
                    existing_modified: dict, incremental: bool):
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
        _maybe_record_files(conn, entry.path, folder_id, ignore_names, stats,
                             row, existing_modified, incremental)

        if stats["folders"] % 500 == 0:
            print(f"  ...{stats['folders']} folders, {stats['files']} files indexed "
                  f"({stats['folders_skipped']} folders skipped, unchanged)")

        # entry.path already carries the \\?\ prefix if the parent scan did,
        # so long-path support propagates automatically down the recursion.
        # Every folder is still walked and checked individually every run —
        # only the file-listing step inside it is what gets skipped.
        _walk_children(conn, entry.path, this_depth, year, new_job_code, new_job_name,
                        new_site_code, new_site_name, ignore_names, stats, code_kind, source,
                        existing_modified, incremental)


def _crawl_root(conn, root_spec: dict, ignore_names: set, stats: dict,
                 existing_modified: dict, incremental: bool):
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
        _maybe_record_files(conn, entry.path, folder_id, ignore_names, stats,
                             year_row, existing_modified, incremental)

        _walk_children(conn, entry.path, 0, year, None, None, None, None,
                        ignore_names, stats, code_kind, source,
                        existing_modified, incremental)


def crawl(conn, config: dict) -> dict:
    """
    Walk every root defined in config['roots'], each with its own
    root_path / year_folder_pattern / code_kind ('job' or 'offer'), and
    write every folder and file found beneath them into the database.

    config['incremental'] (default True) skips re-listing files for
    folders whose mtime hasn't changed since the last crawl — see the
    module docstring for exactly what this does and doesn't catch.

    Returns a small stats dict.
    """
    ignore_names = set(config.get("ignore_names", []))
    incremental = config.get("incremental", True)
    existing_modified = _load_existing_modified_map(conn) if incremental else {}

    stats = {
        "years": 0, "folders": 0, "files": 0, "revision_hints": 0,
        "folders_skipped": 0, "folders_rescanned": 0,
    }

    for root_spec in config["roots"]:
        _crawl_root(conn, root_spec, ignore_names, stats, existing_modified, incremental)

    conn.commit()
    print("Rebuilding search index...")
    rebuild_fts(conn)
    mode = "incremental" if incremental else "full"
    print(f"Done ({mode}). {stats['years']} year folders, {stats['folders']} folders, "
          f"{stats['files']} files written, "
          f"{stats['folders_skipped']} folders skipped as unchanged, "
          f"{stats['revision_hints']} flagged as possible nested revisions.")
    return stats