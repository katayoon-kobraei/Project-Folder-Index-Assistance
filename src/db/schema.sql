-- Raw catalog of every folder the crawler finds, one row per folder.
CREATE TABLE IF NOT EXISTS folders (
    id            INTEGER PRIMARY KEY,
    path          TEXT UNIQUE NOT NULL,
    source        TEXT NOT NULL DEFAULT 'trabajos', -- which archive root this came from, e.g. 'trabajos' or 'ofertas'
    depth         INTEGER NOT NULL,        -- 0 = year, 1 = job/offer, 2 = site, 3 = category, ...
    name          TEXT NOT NULL,
    year          INTEGER,                 -- parsed from the year folder
    job_code      TEXT,                    -- e.g. "22-007" (trabajos) or "001" (ofertas)
    job_name      TEXT,                    -- e.g. "AYTO TORRENT" or "JUVACAR BENETUSSER"
    site_code     TEXT,                    -- e.g. "22-007-02" (trabajos only; ofertas has no site level)
    site_name     TEXT,                    -- e.g. "CALLE SAN LUIS BELTRAN - MEJORA PEATONAL"
    modified_at   TEXT,
    file_count    INTEGER,
    is_revision_hint INTEGER DEFAULT 0     -- 1 if name looks like a dated update nested in an older folder
);

-- Every individual file found inside any indexed folder.
CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    path          TEXT UNIQUE NOT NULL,
    folder_id     INTEGER REFERENCES folders(id),
    name          TEXT NOT NULL,           -- full filename including extension
    extension     TEXT,                    -- lowercase, no leading dot, e.g. "pdf"
    modified_at   TEXT,
    size_bytes    INTEGER
);

-- Resolved real-world projects, one row per project regardless of how many
-- job codes it has across years. Built by src/resolution/match_projects.py.
CREATE TABLE IF NOT EXISTS projects (
    id              INTEGER PRIMARY KEY,
    canonical_name  TEXT NOT NULL,
    first_seen_year INTEGER,
    last_seen_year  INTEGER,
    status          TEXT DEFAULT 'unknown'  -- active / dormant / completed / unknown
);

-- Links folders (job-level rows in `folders`) to a resolved project.
CREATE TABLE IF NOT EXISTS project_links (
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    folder_id   INTEGER NOT NULL REFERENCES folders(id),
    confidence  REAL,                       -- match score from resolution step
    confirmed   INTEGER DEFAULT 0,          -- 1 once a human has confirmed the link
    PRIMARY KEY (project_id, folder_id)
);

-- Full-text search index over folder names for instant lookup.
CREATE VIRTUAL TABLE IF NOT EXISTS folders_fts USING fts5(
    name, job_name, site_name, content='folders', content_rowid='id'
);

-- Full-text search index over individual filenames.
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    name, content='files', content_rowid='id'
);
