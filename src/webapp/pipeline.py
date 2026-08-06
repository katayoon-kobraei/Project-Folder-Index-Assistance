"""
Runs the full crawl -> resolve projects -> detect locations pipeline in a
background thread, so the "Refresh" button in the UI can kick it off
without blocking the request for the ~25+ minutes a full crawl takes over
the network drive.

The UI polls GET /api/refresh/status while this runs. Only one refresh can
run at a time — a second click while one is in progress is rejected, not
queued or stacked.

The three step functions are injectable (default to the real
crawl/resolution/location functions) purely so tests can swap in fast
fakes instead of running a real multi-minute crawl.
"""

import threading
import time
import traceback

from src.crawler.crawl import crawl as _real_crawl
from src.db.db import open_db
from src.resolution.detect_locations import run as _real_run_locations
from src.resolution.match_projects import run as _real_run_resolution

_lock = threading.Lock()
_state = {
    "status": "idle",       # idle | running | done | error
    "stage": None,          # crawling | resolving_projects | detecting_locations
    "started_at": None,
    "finished_at": None,
    "stats": None,
    "error": None,
}


def get_status() -> dict:
    with _lock:
        return dict(_state)


def _set(**kwargs) -> None:
    with _lock:
        _state.update(kwargs)


def start_refresh(
    config: dict,
    crawl_fn=_real_crawl,
    run_resolution_fn=_real_run_resolution,
    run_locations_fn=_real_run_locations,
) -> bool:
    """Returns False without starting anything if a refresh is already
    running. Returns True once the background thread has been launched."""
    with _lock:
        if _state["status"] == "running":
            return False
        _state.update(
            status="running", stage="crawling", started_at=time.time(),
            finished_at=None, stats=None, error=None,
        )

    thread = threading.Thread(
        target=_run_pipeline,
        args=(config, crawl_fn, run_resolution_fn, run_locations_fn),
        daemon=True,
    )
    thread.start()
    return True


def _run_pipeline(config, crawl_fn, run_resolution_fn, run_locations_fn) -> None:
    conn = None
    try:
        conn = open_db(config["db_path"])

        _set(stage="crawling")
        crawl_stats = crawl_fn(conn, config)

        _set(stage="resolving_projects")
        resolution_result = run_resolution_fn(conn)

        _set(stage="detecting_locations")
        location_result = run_locations_fn(conn)

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
        _set(status="error", stage=None, finished_at=time.time(), error=str(e))
    finally:
        if conn is not None:
            conn.close()
