"""
Runs the full crawl -> resolve projects -> detect locations pipeline in a
background thread, so the "Refresh" button in the UI can kick it off
without blocking the request for the ~25+ minutes a full crawl takes over
the network drive.

The UI polls GET /api/refresh/status while this runs. Only one refresh can
run at a time on a given PC — a second click while one is in progress is
rejected, not queued or stacked.

The three step functions are injectable (default to the real
crawl/resolution/location functions) purely so tests can swap in fast
fakes instead of running a real multi-minute crawl.

SHARED INDEX MODE (config['shared_db_path'] set — see config.example.yaml)
----------------------------------------------------------------------------
By default (shared_db_path unset) every PC crawls into its own local
db_path and that's the end of it — the original, fully local design.

When shared_db_path IS set, every PC's reads go against that one shared
file instead (see desktop_app/data_service.py and src/webapp/app.py), but
crawling stays restricted to whichever single PC has is_index_builder set
to true in its own config.yaml. That's a deliberate, hard restriction, not
just a UI nicety: SQLite's own file-locking is not reliable for multiple
independent processes writing to the same file over a Windows network
share (SMB) — allowing two PCs to crawl the same shared_db_path at once is
a real corruption risk, not just a race that "probably" resolves cleanly.

The one allowed builder still crawls into its OWN local db_path first,
exactly like the fully-local design — nothing about the crawl itself
changes. Only once that finishes successfully does _publish_db() copy the
finished file over to shared_db_path, as a whole-file copy-then-atomic-
rename rather than SQLite performing live writes against the network path.
Readers on every other PC therefore only ever see either the complete
PREVIOUS shared file or the complete NEW one, never a partially-written
one. A small lock file (shared_db_path + ".lock") prevents two PCs from
publishing at the same time even if both are (mis)configured as builder;
it's created with an exclusive/atomic file-create (not a check-then-write),
and treated as abandoned (safe to take over) if it's older than
_STALE_LOCK_SECONDS, so a builder PC crashing mid-crawl can't permanently
wedge indexing for everyone else.
"""

import json
import os
import shutil
import socket
import threading
import time
import traceback
from pathlib import Path

from src.crawler.crawl import crawl as _real_crawl
from src.db.db import open_db
from src.resolution.detect_locations import run as _real_run_locations
from src.resolution.match_projects import run as _real_run_resolution

_lock = threading.Lock()
_state = {
    "status": "idle",       # idle | running | done | error
    "stage": None,          # crawling | resolving_projects | detecting_locations | publishing
    "started_at": None,
    "finished_at": None,
    "stats": None,
    "error": None,
}

# How old a shared-index lock file has to be before a new crawl is allowed
# to take it over anyway. Long enough to comfortably cover a real full
# crawl (~25+ minutes, see src/crawler/crawl.py's module docstring), short
# enough that a builder PC crashing or losing power mid-crawl doesn't block
# indexing for everyone else for days.
_STALE_LOCK_SECONDS = 3 * 60 * 60  # 3 hours


def _as_bool(value) -> bool:
    """config.yaml's is_index_builder is meant to be a real YAML boolean
    (true/false, unquoted), but this tolerates it being hand-typed as a
    quoted string too — plain bool("false") would otherwise be True (any
    non-empty string is truthy in Python), which would silently turn a
    reader PC into a second builder. Not just theoretical: the portable
    installer has to write this value into a plain-text YAML file itself
    (see installer/_installer/install.ps1's Set-YamlBoolean), so it's
    worth being defensive here rather than trusting that path forever."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "si", "sí", "on")
    return bool(value)


def get_status() -> dict:
    with _lock:
        return dict(_state)


def _set(**kwargs) -> None:
    with _lock:
        _state.update(kwargs)


def _lock_path(shared_db_path: str) -> Path:
    return Path(str(shared_db_path) + ".lock")


def _meta_path(shared_db_path: str) -> Path:
    return Path(str(shared_db_path) + ".meta.json")


def _read_lock_info(lock_path: Path) -> dict:
    try:
        return json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}


def acquire_shared_lock(shared_db_path: str) -> None:
    """Raises RuntimeError, with a message meant to be shown directly to
    the user, if another PC already appears to be publishing to
    shared_db_path right now. Uses an exclusive/atomic file create (not a
    check-then-write) as the actual mutual-exclusion primitive, so two PCs
    racing to start a build at almost the same instant can't both succeed
    — only one os.open(..., O_CREAT | O_EXCL) can ever win."""
    lock_path = _lock_path(shared_db_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"host": socket.gethostname(), "started_at": time.time()}).encode("utf-8")

    def try_create() -> bool:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True

    if try_create():
        return

    info = _read_lock_info(lock_path)
    age = time.time() - info.get("started_at", 0) if info else _STALE_LOCK_SECONDS + 1
    if age < _STALE_LOCK_SECONDS:
        host = info.get("host") or "otro PC"
        raise RuntimeError(
            f"Ya hay una actualizacion del indice compartido en curso desde {host}. "
            "Intentalo de nuevo mas tarde."
        )

    # The existing lock is old enough that whoever created it almost
    # certainly crashed or lost power mid-crawl rather than still being
    # genuinely in progress (a real crawl finishes, and releases it, well
    # before _STALE_LOCK_SECONDS) — safe to take over.
    lock_path.unlink(missing_ok=True)
    if not try_create():
        # Someone else replaced it in the exact same instant — extremely
        # unlikely, but the safe move is to refuse this run rather than
        # risk two builders proceeding at once.
        raise RuntimeError(
            "No se pudo reservar la actualizacion del indice compartido justo ahora. Intentalo de nuevo."
        )


def release_shared_lock(shared_db_path: str) -> None:
    _lock_path(shared_db_path).unlink(missing_ok=True)


def _publish_db(local_db_path: str, shared_db_path: str) -> None:
    """Atomically replace shared_db_path with the just-finished
    local_db_path — a plain whole-file copy into a temp name on the SAME
    directory, then os.replace() (atomic rename), never SQLite writing
    live against the network path itself. That's what actually keeps this
    safe for readers on other PCs: os.replace() means a reader either
    opens the complete previous file or the complete new one, never
    something half-written mid-copy.

    Also writes a small sidecar shared_db_path + ".meta.json" with when
    and by which PC this copy was published, so a reader can show that
    (see data_service.shared_index_info)."""
    shared = Path(shared_db_path)
    shared.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = shared.with_name(shared.name + f".tmp-{os.getpid()}")
    shutil.copyfile(local_db_path, tmp_path)
    os.replace(tmp_path, shared)

    meta = {"built_at": time.time(), "built_by_host": socket.gethostname()}
    _meta_path(shared_db_path).write_text(json.dumps(meta), encoding="utf-8")


def start_refresh(
    config: dict,
    crawl_fn=_real_crawl,
    run_resolution_fn=_real_run_resolution,
    run_locations_fn=_real_run_locations,
) -> bool:
    """Returns False without starting anything if a refresh is already
    running on this PC, or (in shared index mode) if this PC isn't the
    configured builder, or another PC currently holds the shared-index
    lock. Returns True once the background thread has been launched.
    Callers that get False back should read get_status()["error"] for the
    specific reason — it's always set in that case, not just for the
    generic "already running locally" outcome."""
    shared_db_path = config.get("shared_db_path")
    is_builder = bool(shared_db_path) and _as_bool(config.get("is_index_builder", False))

    with _lock:
        if _state["status"] == "running":
            return False

        if shared_db_path and not is_builder:
            _state.update(
                status="error",
                stage=None,
                started_at=time.time(),
                finished_at=time.time(),
                stats=None,
                error=(
                    "Este PC solo lee el indice compartido y no puede actualizarlo. "
                    "Pide a quien gestione el PC generador que ejecute la actualizacion alli."
                ),
            )
            return False

        if shared_db_path:
            try:
                acquire_shared_lock(shared_db_path)
            except RuntimeError as exc:
                _state.update(
                    status="error", stage=None, started_at=time.time(),
                    finished_at=time.time(), stats=None, error=str(exc),
                )
                return False

        _state.update(
            status="running", stage="crawling", started_at=time.time(),
            finished_at=None, stats=None, error=None,
        )

    thread = threading.Thread(
        target=_run_pipeline,
        args=(config, crawl_fn, run_resolution_fn, run_locations_fn, shared_db_path),
        daemon=True,
    )
    thread.start()
    return True


def _run_pipeline(config, crawl_fn, run_resolution_fn, run_locations_fn, shared_db_path=None) -> None:
    conn = None
    try:
        conn = open_db(config["db_path"])

        _set(stage="crawling")
        crawl_stats = crawl_fn(conn, config)

        _set(stage="resolving_projects")
        resolution_result = run_resolution_fn(conn)

        _set(stage="detecting_locations")
        location_result = run_locations_fn(conn)

        if shared_db_path:
            # Force everything committed in this WAL-mode connection back
            # into the main db file (see src/db/db.py's open_db) and drop
            # the -wal/-shm sidecar files before copying — _publish_db
            # only copies the single main .db file, so anything still
            # sitting in a separate -wal file at copy time would silently
            # be missing from what gets published.
            #
            # Then switch OFF WAL mode entirely (back to SQLite's default
            # rollback-journal) before publishing. This isn't just
            # cleanup: a WAL-mode database's read-only connections still
            # need to create/access its -shm shared-memory sidecar for
            # locking coordination, which readers on other PCs opening a
            # READ-ONLY connection to a file they don't have write access
            # to (the whole point of shared_db_path — see
            # desktop_app.data_service.get_connection) may not be able to
            # do at all, hanging or failing to open. A plain rollback-
            # journal database has no such requirement — a read-only open
            # of one just works. The local BUILDER copy at db_path stays
            # in WAL mode for its own next crawl (open_db always sets it
            # again); this only affects the copy that gets published.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
        conn.close()
        conn = None

        if shared_db_path:
            _set(stage="publishing")
            _publish_db(config["db_path"], shared_db_path)
            # Released BEFORE the state flips to "done" below, not after
            # (see the except branch for why this ordering matters) — so
            # the instant any caller polling get_status() sees "done",
            # the shared index is already both published AND unlocked,
            # ready for the next run.
            release_shared_lock(shared_db_path)

        _set(
            status="done",
            stage=None,
            finished_at=time.time(),
            stats={
                "folders": crawl_stats.get("folders"),
                "files": crawl_stats.get("files"),
                "revision_hints": crawl_stats.get("revision_hints"),
                "projects": resolution_result.get("projects_created"),
                "locations": location_result.get("locations_detected"),
            },
        )
    except Exception as e:
        traceback.print_exc()
        if conn is not None:
            conn.close()
            conn = None
        # Released BEFORE the state flips to "error", same reasoning as
        # the success path above — otherwise a caller that reacts to
        # status=="error" by immediately retrying (a real, expected UI
        # flow after a failed refresh) could still find the lock held
        # for a brief window and be told "otro PC" is building it, which
        # would be actively misleading right after ITS OWN failed run.
        if shared_db_path:
            release_shared_lock(shared_db_path)
        _set(status="error", stage=None, finished_at=time.time(), error=str(e))
    finally:
        if conn is not None:
            conn.close()
