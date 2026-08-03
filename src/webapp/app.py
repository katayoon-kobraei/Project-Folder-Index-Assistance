"""
Local Flask app: search view + graph/timeline view.

Not implemented yet — placeholder for steps 8-9 of the build plan.
Planned routes:
  GET /              -> search page
  GET /api/search    -> JSON results from src/search/search.py
  GET /project/<id>  -> project timeline / graph page
  GET /api/project/<id>/graph -> JSON nodes/edges for Cytoscape.js
"""

from flask import Flask

app = Flask(__name__)


@app.route("/")
def index():
    return "Project Folder Index — not implemented yet"


if __name__ == "__main__":
    app.run(debug=True)
