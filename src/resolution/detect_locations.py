"""
Detects locations (towns/municipalities) that recur across multiple
otherwise-distinct projects, e.g. "SAGUNTO" appearing in "AYTO SAGUNTO",
"FAMILY CASH SAGUNTO", and "CARAVANAS SAGUNTO" — three unrelated clients,
same town. This is the "location" grouping: a fan-out from one place to
every project ever done there.

Approach: tokenize each resolved project's canonical_name, throw out
generic Spanish/admin words that show up constantly but aren't places
(see STOPWORDS below — built from what actually appears in the real
archive: "AYTO", "USD", "EST", "PROY", etc. are abbreviations, not towns),
and keep any remaining word that appears in 2+ DISTINCT projects. That
word becomes a location, linking every project that contains it.

This is intentionally simpler than match_projects.py's phase/fuzzy
matching — it's single-token exact matching only. Multi-word place names
("SAN VICENTE") aren't detected as one unit yet; each word is considered
separately. Good enough as a first pass since single-word towns dominate
the real data (SAGUNTO, TORRENT, SILLA, MONCADA, SOLLANA, DENIA, MANISES,
QUART, MUSEROS all showed up this way in a real check against the archive).

Rerunning this is idempotent: it clears and rebuilds `locations` and
`location_links` from the current contents of `projects` each time.

Known false-positive pattern: a CLIENT/COMPANY name (e.g. "PLENOIL",
"CONSUM") can recur across multiple distinct project names exactly the
same way a real town does (e.g. "PLENOIL" and "CSS PLENOIL AMBIENTAL"
both contain "PLENOIL") — structurally identical to the real thing this
module is meant to detect, so it can't be filtered by the same
token-frequency logic. COMPANY_BLOCKLIST below excludes names confirmed
to be companies, not places. See write_company_review_csv() for a
worklist of the remaining ambiguous cases (a detected "location" whose
name is also the name of one of its own linked projects) for a human to
sort out — same idea as match_projects.py's fuzzy_review.csv.
"""

import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Generic administrative/connector words and abbreviations that show up
# constantly in the real project names but are never themselves a place.
# Built from a frequency check against the actual resolved project list —
# these are exactly the tokens that appeared in many DISTINCT projects
# without being a town name (confirmed against real data, not guessed).
STOPWORDS = {
    "DE", "DEL", "LA", "EL", "LOS", "LAS", "Y", "EN", "A", "CON", "SIN",
    "AYTO", "USD", "EST", "ES", "PROY", "DIC", "SAN", "JUAN", "E.S.",
    "RI", "JV", "DO", "ACC", "MOD", "NPV", "U.S.", "U.S.C", "USC", "CC",
    "IP", "IP02", "ICIO", "TASA", "OF", "VARIOS", "OFERTA", "GESTION",
    "REVISION", "CHJ", "AMPLIA", "AMPLIACION", "NUEVA", "NUEVO",
}

# Client/company names confirmed (well-known brands) to NOT be places,
# even though they recur across multiple distinct project names the same
# way a real town would. Add to this list as more are confirmed via
# write_company_review_csv()'s output.
COMPANY_BLOCKLIST = {
    "CONSUM", "PLENOIL", "ALDI", "FERRARI", "INTERPARKING",
}

MIN_TOKEN_LEN = 4
MIN_DISTINCT_PROJECTS = 2

TOKEN_RE = re.compile(r"[A-ZÁÉÍÓÚÑÜ0-9.]+", re.IGNORECASE)


def tokenize(name: str):
    return [t.upper() for t in TOKEN_RE.findall(name)]


def load_projects(conn):
    return conn.execute("SELECT id, canonical_name FROM projects").fetchall()


def find_location_candidates(projects):
    """Returns dict: location_name -> set of project_ids that mention it."""
    token_to_projects = {}
    for project_id, name in projects:
        for tok in set(tokenize(name)):
            if tok in STOPWORDS or tok in COMPANY_BLOCKLIST or len(tok) < MIN_TOKEN_LEN:
                continue
            if tok.replace(".", "").isdigit():
                # Pure numbers/years (e.g. "2015") are never a location —
                # a project named "...2015" just means it happened in 2015.
                continue
            token_to_projects.setdefault(tok, set()).add(project_id)

    return {
        tok: ids
        for tok, ids in token_to_projects.items()
        if len(ids) >= MIN_DISTINCT_PROJECTS
    }


def find_ambiguous_candidates(candidates: dict, projects):
    """A detected 'location' is ambiguous (possibly a company name, not a
    real place) when its name exactly matches the canonical_name of one
    of ITS OWN linked projects — the same pattern a real recurring town
    name would never show (a town isn't also literally the name of one
    specific project done there). Returns a list of dicts for
    write_company_review_csv(), sorted by how many projects it affects."""
    names_by_id = {pid: name for pid, name in projects}
    ambiguous = []
    for location_name, project_ids in candidates.items():
        matching = [
            names_by_id[pid] for pid in project_ids
            if names_by_id.get(pid, "").strip().upper() == location_name
        ]
        if matching:
            other_names = sorted(
                names_by_id[pid] for pid in project_ids
                if names_by_id.get(pid, "").strip().upper() != location_name
            )
            ambiguous.append({
                "location_name": location_name,
                "project_count": len(project_ids),
                "self_named_project": matching[0],
                "other_linked_projects": ", ".join(other_names),
            })
    ambiguous.sort(key=lambda a: -a["project_count"])
    return ambiguous


def write_company_review_csv(ambiguous: list, path: str):
    """Worklist for a human: each row is a detected 'location' whose name
    is suspiciously also the name of one of its own linked projects —
    could be a real town that happens to share a name with a job done
    there, or could be a company/client name that isn't a place at all.
    Nothing in this file changes the database by itself. To exclude a
    confirmed company name from future runs, add it to COMPANY_BLOCKLIST
    above."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["location_name", "project_count", "self_named_project", "other_linked_projects"],
        )
        writer.writeheader()
        for row in ambiguous:
            writer.writerow(row)


def write_locations(conn, candidates: dict):
    conn.execute("DELETE FROM location_links")
    conn.execute("DELETE FROM locations")
    for name, project_ids in candidates.items():
        cur = conn.execute("INSERT INTO locations (name) VALUES (?)", (name,))
        location_id = cur.lastrowid
        for project_id in project_ids:
            conn.execute(
                """
                INSERT INTO location_links (location_id, project_id)
                VALUES (?, ?)
                ON CONFLICT(location_id, project_id) DO NOTHING
                """,
                (location_id, project_id),
            )
    conn.commit()


def run(conn):
    projects = load_projects(conn)
    candidates = find_location_candidates(projects)
    write_locations(conn, candidates)
    ambiguous = find_ambiguous_candidates(candidates, projects)
    return {
        "projects_considered": len(projects),
        "locations_detected": len(candidates),
        "location_project_counts": {
            name: len(ids) for name, ids in
            sorted(candidates.items(), key=lambda kv: -len(kv[1]))
        },
        "ambiguous_candidates": ambiguous,
    }
