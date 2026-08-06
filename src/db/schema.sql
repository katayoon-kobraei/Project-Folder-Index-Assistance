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
    is_revision_hint INTEGER DEFAULT 0,    -- 1 if name looks like a dated update nested in an older folder
    company_project TEXT,                  -- same value as job_name, kept as its own named
                                            -- column for manual checking: the company/project
                                            -- this folder belongs to (e.g. "DINAMIAL", "CONSUM"),
                                            -- inherited down from the depth=1 job folder
    location_site TEXT                     -- same value as site_name, kept as its own named
                                            -- column: the site/location this folder belongs to
                                            -- (e.g. "CONSUM LA ZENIA"), only set when a subfolder
                                            -- name starts with its job's own job_code followed by
                                            -- a counter (see parse_site_folder) — NULL otherwise,
                                            -- including for company folders with no recognized
                                            -- site layer at all
);

-- Every individual file found inside any indexed folder.
CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    path          TEXT UNIQUE NOT NULL,
    folder_id     INTEGER REFERENCES folders(id),
    name          TEXT NOT NULL,           -- full filename including extension
    extension     TEXT,                    -- lowercase, no leading dot, e.g. "pdf"
    modified_at   TEXT,
    size_bytes    INTEGER,
    company_project TEXT,                  -- the company/project this file's folder belongs
                                            -- to, e.g. a file under "14-001 DINAMIAL\..." gets
                                            -- "DINAMIAL" here, whatever its own subfolder depth
    file_year     INTEGER,                 -- the year from its TRABAJOS <year> / OFERTAS Y
                                            -- CONCURSOS\<year> root, e.g. 2008 for anything
                                            -- under "TRABAJOS 2008"
    location_site TEXT                     -- the site/location this file's folder belongs to,
                                            -- e.g. a file under "...\14-002.001 CONSUM LA
                                            -- ZENIA\..." gets "CONSUM LA ZENIA" here. NULL for
                                            -- files under a company with no recognized site
                                            -- layer (see folders.location_site)
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
    phase       TEXT,                       -- e.g. 'direccion_obra' for a "DO ..." folder; NULL for the main phase
    PRIMARY KEY (project_id, folder_id)
);

-- A detected location (town/municipality) that recurs across multiple
-- otherwise-unrelated projects. Built by src/resolution/detect_locations.py.
CREATE TABLE IF NOT EXISTS locations (
    id    INTEGER PRIMARY KEY,
    name  TEXT UNIQUE NOT NULL          -- e.g. "SAGUNTO", "TORRENT"
);

-- Links a project to a location detected in its name.
CREATE TABLE IF NOT EXISTS location_links (
    location_id  INTEGER NOT NULL REFERENCES locations(id),
    project_id   INTEGER NOT NULL REFERENCES projects(id),
    PRIMARY KEY (location_id, project_id)
);

-- Full-text search index over folder names for instant lookup.
CREATE VIRTUAL TABLE IF NOT EXISTS folders_fts USING fts5(
    name, job_name, site_name, content='folders', content_rowid='id'
);

-- Full-text search index over individual filenames.
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    name, content='files', content_rowid='id'
);

-- Manual-check view: raw folder name next to what the crawler parsed as
-- the company/project name, so it's easy to scroll through in DB Browser
-- and spot any folder where the parsing looks wrong. No new data is
-- computed here — this view only makes the existing columns easier to
-- scan side by side.
CREATE VIEW IF NOT EXISTS verificacion_crawler AS
SELECT
    id,
    year          AS anio,
    depth,
    name          AS carpeta_real,
    company_project AS empresa_proyecto,
    job_code      AS codigo,
    location_site AS ubicacion_sede,
    source        AS archivo_origen,
    path
FROM folders
ORDER BY source, year, path;

-- Same idea, one row per file: which company/project, year, and
-- site/location the crawler assigned to it, next to the actual file
-- path, for spot-checking at the file level.
CREATE VIEW IF NOT EXISTS verificacion_archivos AS
SELECT
    f.id,
    f.file_year      AS anio,
    f.company_project AS empresa_proyecto,
    f.location_site  AS ubicacion_sede,
    f.name           AS archivo,
    f.extension,
    fo.source        AS archivo_origen,
    f.path
FROM files f
JOIN folders fo ON fo.id = f.folder_id
ORDER BY fo.source, f.file_year, f.path;