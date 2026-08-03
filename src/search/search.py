"""
Query helpers over folders_fts for the web UI.

Not implemented yet — placeholder for step 4 of the build plan.
Planned responsibilities:
  - search(conn, query, filters): return matching folders ranked by FTS5
    relevance, with optional year/status filters.
"""


def search(conn, query: str, year: int | None = None, status: str | None = None):
    raise NotImplementedError("search() not implemented yet")
