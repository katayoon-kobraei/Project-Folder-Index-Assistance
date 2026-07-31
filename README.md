# Project Folder Index

Turns the company's `P:\TRABAJOS <year>` archive (2008–present) into a searchable,
queryable index — and eventually a graph view of which real-world projects
span which years.

## Structure discovered in the archive

```
P:\TRABAJOS <YYYY>\
  <YY>-<NNN> <JOB NAME>\                     e.g. 22-007 AYTO TORRENT
    <YY>-<NNN>-<SS> <SITE NAME>\             e.g. 22-007-02 CALLE SAN LUIS BELTRAN
      00-PLANOS PREVIOS\
      01-DOC DE REFERENCIA\
      02-GESTION\
      03-CORREO\
      04-FOTOS\
      05-ACTAS\
      06-DOC PROMOTOR\
      07-NORMATIVA\
      08-PROVEEDORES\
      09-CERT SOLVENCIA\
      10-ESCANER\
      <possible nested revision folder with a later year in its name>
```

Job codes reset every year, so the same real-world client (e.g. "AYTO TORRENT")
gets a *different* code each time they come back — `22-007` one year,
`24-013` the next. Linking those together across years is the entity
resolution problem this project solves. Some continuations instead show up as
a dated subfolder nested inside the *original* job/site folder rather than a
new job code — the crawler needs to catch both patterns.

## Repo layout

```
project-folder-index/
├── config/
│   └── config.example.yaml     # root path + folders/patterns to ignore
├── src/
│   ├── crawler/                # walks P:\ and writes folder metadata to SQLite
│   ├── db/                     # schema + connection helpers
│   ├── resolution/             # fuzzy-matches job names into Project entities
│   ├── search/                 # FTS5 query helpers
│   └── webapp/                 # local Flask app: search view + graph view
├── scripts/
│   └── run_crawl.py            # entry point: `python scripts/run_crawl.py`
├── data/                       # SQLite db lives here — gitignored, not committed
├── tests/
└── docs/                       # architecture doc, notes
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy config\config.example.yaml config\config.yaml
# edit config.yaml: set root_path to P:\ and confirm the ignore list
python scripts\run_crawl.py
```

## Important: this repo does not contain company data

`data/*.db`, `config/config.yaml`, and any exported folder listings are
gitignored on purpose — the archive contains client names, addresses, and
municipal contract details. Only code and schema belong in version control.
If this repo is pushed to GitHub, it should be a **private** repository.

## Status

Crawler is implemented and tested (`src/crawler/crawl.py`, `src/db/db.py`).
Run it with:

```
python scripts\run_crawl.py
```

It walks every `TRABAJOS <year>` folder, parses `YY-NNN NAME` job codes and
`YY-NNN-SS NAME` site codes, flags nested-revision folders (a later year's
update sitting inside an older job/site folder), and writes everything into
the SQLite database at `data/index.db` (path set in `config.yaml`) with a
full-text search index ready to query.

Still to build: entity resolution across years (`src/resolution/`), the
search API and web UI (`src/search/`, `src/webapp/`). See `docs/` for the
full architecture plan.
