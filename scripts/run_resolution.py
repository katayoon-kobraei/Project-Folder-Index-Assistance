"""
Entry point: `python scripts/run_resolution.py`

Reads config/config.yaml for db_path, groups job/offer names into resolved
projects (exact matches, phase-aware), and writes a CSV of fuzzy near-match
suggestions for manual review. Requires the crawl to have already been run.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from src.db.db import open_db
from src.resolution.match_projects import run


def main():
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    if not config_path.exists():
        print("Missing config/config.yaml — copy config/config.example.yaml first.")
        sys.exit(1)

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    conn = open_db(config["db_path"])

    result = run(conn)
    conn.close()

    print(f"Folders considered: {result['folders_considered']}")
    print(f"Projects created (exact-match groups, auto-confirmed): {result['projects_created']}")
    print(f"Fuzzy near-match suggestions needing review: {len(result['fuzzy_suggestions'])}")

    review_path = Path(config["db_path"]).parent / "fuzzy_review.csv"
    from src.resolution.match_projects import write_review_csv
    write_review_csv(result["fuzzy_suggestions"], str(review_path))
    print(f"Review list written to: {review_path}")
    print("Open it and check for genuine near-duplicates vs. false positives "
          "(e.g. different plant numbers) before deciding whether to merge any manually.")


if __name__ == "__main__":
    main()
