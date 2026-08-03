"""
Fuzzy-matches job names across years into resolved `projects` rows.

Not implemented yet — placeholder for step 5 of the build plan.
Planned responsibilities:
  - For each job_name in `folders`, compare against all other job_names
    using rapidfuzz (token_sort_ratio or similar).
  - Group matches above a high-confidence threshold automatically.
  - Emit a review list of medium-confidence matches for manual
    confirm/reject (writes to project_links.confirmed).
  - Also consider is_revision_hint folders as automatic links to their
    parent folder's project (same-name, nested-update case).
"""


def suggest_matches(job_names: list[str], threshold: float = 85.0):
    raise NotImplementedError("suggest_matches() not implemented yet")
