"""
Entry point: `python scripts/run_location_detection.py`

Reads config/config.yaml for db_path and groups resolved projects that
share a common location (town/municipality) name. Requires
scripts/run_resolution.py to have already been run (needs the `projects`
table populated).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from src.db.db import open_db
from src.resolution.detect_locations import run, write_company_review_csv


def main():
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    if not config_path.exists():
        print("Missing config/config.yaml — copy config/config.example.yaml first.")
        sys.exit(1)

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    conn = open_db(config["db_path"])

    result = run(conn)
    conn.close()

    print(f"Projects considered: {result['projects_considered']}")
    print(f"Locations detected: {result['locations_detected']}")
    print()
    print("Locations by number of linked projects:")
    for name, count in result["location_project_counts"].items():
        print(f"  {name:20} {count} projects")

    ambiguous = result["ambiguous_candidates"]
    if ambiguous:
        review_path = Path(__file__).parent.parent / "data" / "company_review.csv"
        write_company_review_csv(ambiguous, str(review_path))
        print()
        print(f"{len(ambiguous)} detected location(s) share a name with one of their own "
              f"linked projects — could be a real town, or could be a company/client name.")
        print(f"Review list written to: {review_path}")
        print("To exclude a confirmed company name from future runs, add it to "
              "COMPANY_BLOCKLIST in src/resolution/detect_locations.py.")


if __name__ == "__main__":
    main()