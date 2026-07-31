"""
Entry point: `python scripts/run_crawl.py`

Loads config/config.yaml, opens (or creates) the SQLite database, and runs
the crawler against root_path. Not implemented yet — placeholder until
src/crawler/crawl.py is built out (step 3).
"""

import sys
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
    conn = open_db(config["db_path"])
    crawl(config["root_path"], config)
    conn.close()


if __name__ == "__main__":
    main()
