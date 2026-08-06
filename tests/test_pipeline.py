"""
Tests for the background refresh pipeline (crawl -> resolve -> detect
locations, run in a thread so the UI button doesn't block on a real
multi-minute crawl). Uses fast fake step functions injected in place of
the real crawl/resolution/location steps — no real filesystem crawl runs
in these tests.
Run with: pytest
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.webapp import pipeline


def _reset_state():
    pipeline._state.update(
        status="idle", stage=None, started_at=None, finished_at=None,
        stats=None, error=None,
    )


def _wait_until_done(timeout=2.0):
    start = time.time()
    while time.time() - start < timeout:
        if pipeline.get_status()["status"] != "running":
            return
        time.sleep(0.02)
    raise TimeoutError("pipeline did not finish in time")


def test_start_refresh_runs_all_three_steps_and_reports_stats(tmp_path):
    _reset_state()
    calls = []

    def fake_crawl(conn, config):
        calls.append("crawl")
        return {"folders": 10, "files": 20, "revision_hints": 1}

    def fake_resolution(conn):
        calls.append("resolution")
        return {"projects_created": 5}

    def fake_locations(conn):
        calls.append("locations")
        return {"locations_detected": 2}

    config = {"db_path": str(tmp_path / "index.db")}
    started = pipeline.start_refresh(config, fake_crawl, fake_resolution, fake_locations)
    assert started is True

    _wait_until_done()
    status = pipeline.get_status()
    assert status["status"] == "done"
    assert calls == ["crawl", "resolution", "locations"]
    assert status["stats"] == {
        "folders": 10, "files": 20, "revision_hints": 1,
        "projects": 5, "locations": 2,
    }
    assert status["started_at"] is not None
    assert status["finished_at"] is not None


def test_concurrent_refresh_is_rejected(tmp_path):
    _reset_state()
    release = threading.Event()

    def slow_crawl(conn, config):
        release.wait(timeout=2)
        return {"folders": 1, "files": 1, "revision_hints": 0}

    def noop_resolution(conn):
        return {"projects_created": 0}

    def noop_locations(conn):
        return {"locations_detected": 0}

    config = {"db_path": str(tmp_path / "index.db")}
    first = pipeline.start_refresh(config, slow_crawl, noop_resolution, noop_locations)
    assert first is True

    # A second call while the first is still running (blocked on the
    # event) must be rejected, not queued.
    second = pipeline.start_refresh(config, slow_crawl, noop_resolution, noop_locations)
    assert second is False

    release.set()
    _wait_until_done()


def test_error_in_pipeline_is_reported(tmp_path):
    _reset_state()

    def failing_crawl(conn, config):
        raise RuntimeError("network drive unreachable")

    config = {"db_path": str(tmp_path / "index.db")}
    pipeline.start_refresh(config, failing_crawl, lambda c: {}, lambda c: {})

    _wait_until_done()
    status = pipeline.get_status()
    assert status["status"] == "error"
    assert "network drive unreachable" in status["error"]
