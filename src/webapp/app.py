"""
Local Flask app: search, project timeline, and location graph.

Run with: python scripts/run_webapp.py (opens config.yaml for db_path,
then starts this app). Skeleton only — routes and data wiring are real
and working end to end; visual polish comes next once the layout/detail
is decided.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import yaml
from flask import Flask, jsonify, render_template, request

from src.search.search import (
    get_location_graph,
    get_project_detail,
    list_locations,
    search_projects,
)
from src.webapp import pipeline

BASE_DIR = Path(__file__).parent.parent.parent
TEMPLATE_DIR = Path(__file__).parent / "templates"

app = Flask(__name__, template_folder=str(TEMPLATE_DIR))


def _load_config() -> dict:
    config_path = BASE_DIR / "config" / "config.yaml"
    return yaml.safe_load(config_path.read_text())


def _db_path() -> str:
    return _load_config()["db_path"]


def get_db():
    db_path = _db_path()
    full_path = db_path if Path(db_path).is_absolute() else str(BASE_DIR / db_path)
    return sqlite3.connect(full_path)


@app.route("/")
def index():
    return render_template("search.html")


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "")
    conn = get_db()
    try:
        return jsonify(search_projects(conn, q))
    finally:
        conn.close()


@app.route("/project/<int:project_id>")
def project_page(project_id):
    return render_template("project.html", project_id=project_id)


@app.route("/api/project/<int:project_id>")
def api_project(project_id):
    conn = get_db()
    try:
        data = get_project_detail(conn, project_id)
    finally:
        conn.close()
    if data is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@app.route("/locations")
def locations_page():
    return render_template("locations.html")


@app.route("/api/locations")
def api_locations():
    conn = get_db()
    try:
        return jsonify(list_locations(conn))
    finally:
        conn.close()


@app.route("/location/<int:location_id>")
def location_page(location_id):
    return render_template("location.html", location_id=location_id)


@app.route("/api/location/<int:location_id>/graph")
def api_location_graph(location_id):
    conn = get_db()
    try:
        data = get_location_graph(conn, location_id)
    finally:
        conn.close()
    if data is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    config = _load_config()
    started = pipeline.start_refresh(config)
    if not started:
        return jsonify({"status": "already_running"}), 409
    return jsonify({"status": "started"})


@app.route("/api/refresh/status")
def api_refresh_status():
    return jsonify(pipeline.get_status())


if __name__ == "__main__":
    app.run(debug=True)
