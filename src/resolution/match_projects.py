"""
Groups job/offer names across years into resolved `projects` rows.

Two-tier approach:
  1. Exact matching (after stripping known phase prefixes like "DO ", which
     marks a Direccion de Obra / supervision phase of the SAME project, not
     a duplicate). These are auto-confirmed — matching the exact same name
     used in different years is very unlikely to be a coincidence.
  2. Fuzzy matching between the exact-match groups (e.g. "SEINZA" vs
     "SEINZA-INGEVIA"), which get written with confirmed=0 for a human to
     review — see write_review_csv(). Never auto-merged, since fuzzy
     matching also produces false positives (e.g. "PLASTIC PLANTA IV" vs
     "PLASTIC PLANTA I" are DIFFERENT plants, not a typo).

Rerunning this is idempotent: it clears and rebuilds `projects` and
`project_links` from the current contents of `folders` each time.
"""

import csv
import re
import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from rapidfuzz import fuzz

# Matches a bare number or a small Roman numeral (I, II, III, IV, V, VI...,
# X) — used to catch "PLANTA IV" vs "PLANTA I" as genuinely DIFFERENT
# plants, not a typo, even though they score as near-identical strings.
NUMBERING_RE = re.compile(r"^(?:\d+|I{1,3}|IV|VI{0,3}|IX|X)$", re.IGNORECASE)

# Known prefixes that mark a distinct contract PHASE of the same project,
# not a different project. Confirmed with the client: "DO" = Direccion de
# Obra (construction supervision), a later phase after the design job.
PHASE_PREFIXES = {
    "DO": "direccion_obra",
}


def strip_phase_prefix(name: str):
    """Return (base_name, phase) — phase is None if no known prefix matches."""
    stripped = name.strip()
    upper = stripped.upper()
    for prefix, phase in PHASE_PREFIXES.items():
        if upper.startswith(prefix + " "):
            return stripped[len(prefix):].strip(), phase
    return stripped, None


def load_job_rows(conn):
    """One row per job/offer-level folder (depth=1) that has a job_name."""
    rows = conn.execute(
        """
        SELECT id, job_code, job_name, source, year
        FROM folders
        WHERE depth = 1 AND job_name IS NOT NULL
        """
    ).fetchall()
    return [
        {"folder_id": r[0], "job_code": r[1], "job_name": r[2], "source": r[3], "year": r[4]}
        for r in rows
    ]


def group_exact(rows):
    """Group rows by phase-stripped, uppercased base name. Mutates each row
    to add 'base_name' and 'phase'. Returns dict: base_name_upper -> [rows]."""
    groups = {}
    for row in rows:
        base, phase = strip_phase_prefix(row["job_name"])
        row["base_name"] = base
        row["phase"] = phase
        key = base.upper()
        groups.setdefault(key, []).append(row)
    return groups


def _differs_only_by_trailing_numbering(a: str, b: str) -> bool:
    """True if `a` and `b` are identical except for their last word, and
    that last word looks like a number or Roman numeral on at least one
    side — e.g. "PLASTIC PLANTA IV" vs "PLASTIC PLANTA I". High string
    similarity here means DIFFERENT plants/sites, not a typo, so these
    must never be suggested as a merge no matter how high they score."""
    tokens_a, tokens_b = a.split(), b.split()
    if not tokens_a or not tokens_b or len(tokens_a) != len(tokens_b):
        return False
    if tokens_a[:-1] != tokens_b[:-1]:
        return False
    last_a, last_b = tokens_a[-1], tokens_b[-1]
    if last_a == last_b:
        return False
    return bool(NUMBERING_RE.match(last_a) or NUMBERING_RE.match(last_b))


def _glued_containment(shorter: str, longer: str) -> bool:
    """True if `shorter` appears in `longer` with no space at the join —
    e.g. "SEINZA" -> "SEINZA-INGEVIA" (hyphen glued directly on). False if
    there's a space at the boundary, meaning `longer` adds a whole extra
    WORD — e.g. "CONSUM" -> "CONSUM CANYADA" is a different specific site
    that happens to share the client name "CONSUM", not a near-duplicate."""
    idx = longer.upper().find(shorter.upper())
    if idx == -1:
        return False
    prefix, suffix = longer[:idx], longer[idx + len(shorter):]
    if prefix and prefix[-1] == " ":
        return False
    if suffix and suffix[0] == " ":
        return False
    return True


def _combined_score(a: str, b: str):
    """token_sort_ratio catches reordered words (e.g. "X CONSUM" vs
    "CONSUM X") but badly penalizes length differences; partial_ratio
    catches one name being a prefix/suffix of the other (e.g. "SEINZA" vs
    "SEINZA-INGEVIA") but ignores reordering and scores 100 for ANY full
    containment, generic-name flooding included. Returns (score, metric)
    so the caller can apply the containment guard only when partial_ratio
    is what's actually driving the match."""
    token_sort = fuzz.token_sort_ratio(a, b)
    partial = fuzz.partial_ratio(a, b)
    if partial > token_sort:
        return partial, "partial"
    return token_sort, "token_sort"


def suggest_fuzzy_merges(groups: dict, threshold: float = 88.0):
    """Suggest merges BETWEEN different exact-match groups whose base names
    are similar but not identical. Returns a list of dicts, sorted by score
    descending, for human review — never applied automatically."""
    keys = list(groups.keys())
    suggestions = []
    for key_a, key_b in combinations(keys, 2):
        if _differs_only_by_trailing_numbering(key_a, key_b):
            continue
        score, metric = _combined_score(key_a, key_b)
        if score < threshold:
            continue
        if metric == "partial":
            shorter, longer = (key_a, key_b) if len(key_a) <= len(key_b) else (key_b, key_a)
            if not _glued_containment(shorter, longer):
                continue
        suggestions.append({
            "name_a": groups[key_a][0]["base_name"],
            "name_b": groups[key_b][0]["base_name"],
            "score": round(score, 1),
            "codes_a": ", ".join(sorted({r["job_code"] or "" for r in groups[key_a]})),
            "codes_b": ", ".join(sorted({r["job_code"] or "" for r in groups[key_b]})),
        })
    suggestions.sort(key=lambda s: -s["score"])
    return suggestions


def write_projects(conn, groups: dict):
    """Clear and rebuild `projects` / `project_links` from `groups`. Every
    row within one exact-match group is auto-confirmed (confidence=100,
    confirmed=1); the DO/main phase is preserved on the link, not merged
    into a single flattened record."""
    conn.execute("DELETE FROM project_links")
    conn.execute("DELETE FROM projects")

    for base_key, rows in groups.items():
        years = [r["year"] for r in rows if r["year"] is not None]
        canonical_name = rows[0]["base_name"]
        cur = conn.execute(
            "INSERT INTO projects (canonical_name, first_seen_year, last_seen_year) VALUES (?, ?, ?)",
            (canonical_name, min(years) if years else None, max(years) if years else None),
        )
        project_id = cur.lastrowid
        for r in rows:
            conn.execute(
                """
                INSERT INTO project_links (project_id, folder_id, confidence, confirmed, phase)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, folder_id) DO NOTHING
                """,
                (project_id, r["folder_id"], 100.0, 1, r["phase"]),
            )
    conn.commit()


def write_review_csv(suggestions, path: str):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["score", "name_a", "codes_a", "name_b", "codes_b"])
        writer.writeheader()
        for s in suggestions:
            writer.writerow({
                "score": s["score"],
                "name_a": s["name_a"],
                "codes_a": s["codes_a"],
                "name_b": s["name_b"],
                "codes_b": s["codes_b"],
            })


def run(conn, fuzzy_threshold: float = 88.0):
    rows = load_job_rows(conn)
    groups = group_exact(rows)
    write_projects(conn, groups)
    suggestions = suggest_fuzzy_merges(groups, threshold=fuzzy_threshold)
    return {
        "folders_considered": len(rows),
        "projects_created": len(groups),
        "fuzzy_suggestions": suggestions,
    }
