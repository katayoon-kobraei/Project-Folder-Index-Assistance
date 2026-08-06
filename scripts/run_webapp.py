"""
Entry point: `python scripts/run_webapp.py`

Starts the local Flask app (search, project timeline, location graph)
against the database configured in config/config.yaml. Requires the
crawler, resolution, and location detection steps to have already been
run at least once.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.webapp.app import app

if __name__ == "__main__":
    print("Starting server at http://127.0.0.1:5000 — open that in your browser.")
    app.run(debug=True)
