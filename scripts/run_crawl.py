"""
Entry point: `python scripts/run_crawl.py`
             `python scripts/run_crawl.py --full`   (force a full rescan)

Loads config/config.yaml, opens (or creates) the SQLite database, and runs
the crawler. By default this is incremental (config['incremental'], true
unless set otherwise) — folders whose modified date hasn't changed since
last time have their file listing skipped, which is the expensive part on
a network drive. Pass --full to force a complete rescan regardless of the
config setting, e.g. for a periodic sanity check.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from src.db.db import open_db
from src.crawler.crawl import crawl


def main():
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    if not config_path.exists():
        print("Missing config/config.yaml — copy config/config.example.yaml first.")
        sys.exit(1)

    config = yaml.safe_load(config_path.read_text())
    if "--full" in sys.argv:
        config["incremental"] = False
        print("Forcing a full rescan (--full) regardless of config.yaml.")

    conn = open_db(config["db_path"])

    start = time.time()
    stats = crawl(conn, config)
    conn.close()

    elapsed = time.time() - start
    print(f"Finished in {elapsed:.1f}s. Database: {config['db_path']}")
    print(f"Years: {stats['years']}  Folders: {stats['folders']}  "
          f"Files written: {stats['files']}  "
          f"Folders skipped (unchanged): {stats['folders_skipped']}  "
          f"Possible nested revisions: {stats['revision_hints']}")


if __name__ == "__main__":
    main()
