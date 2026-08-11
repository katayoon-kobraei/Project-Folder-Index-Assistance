"""
Reads (and, via writer.py, edits) the "LISTADO PROYECTOS POR AÑOS"
workbook that backs the Proyectos Info page — a hand-maintained Excel
file, NOT the crawled P: index.

The real source is a single multi-sheet workbook (one sheet per year),
but the app reads from a FOLDER of separate single-sheet files instead —
one per year, named "2024.xlsx", "2025.xlsx", "2026.xlsx" — produced by
split_workbook_by_year() below (also runnable as
scripts/split_project_info_by_year.py). Only 2024, 2025, and 2026
currently have real data, so those are the only files ever read; a
folder missing one of the three still works fine with whichever are
present. As of the latest real files, each year sheet has a smaller set
of columns than earlier versions did (NUMERO DE PROYECTO, PLANNING,
NOMBRE, LISTADO DE EMPLEADOS, TRABAJOS A REALIZAR, TITULO ENTERO DEL
PROYECTO, CIUDAD, PROVINCIA, FECHA DEL PROYECTO, PROMOTOR, TIPO,
SUBTIPO1, SUBTIPO2) — older/fuller columns (budgets, KPIs, visado, etc.)
are still supported below for backwards compatibility with any file that
still has them, they just won't appear if the column is gone.

FIELD_MAP below is the single source of truth for both directions:
reading a cell into a row dict (load_project_info) AND writing an edited
value back into that exact cell (writer.update_project_info_row) — kept
in one place so the two can never drift out of sync. A NOMBRE value is
NOT unique: the same project can have several rows (e.g. one per
document/task), each kept as its own separate entry, in the same order
they appear in the sheet.

Four columns are constrained to a fixed set of categories in the
original master workbook (its "TIPOS" sheet) — CATEGORY_OPTIONS below
holds the exact confirmed lists, used to render these as single-choice
pickers (radio buttons) instead of free text in the edit form:
    PLANNING (-> status)     Inicio Proyecto / Proceso / Acabado /
                              Cancelado / DO — "Inicio Proyecto" is an
                              app-only addition, not one of the values
                              confirmed from the master workbook's TIPOS
                              sheet; selecting it also stamps FECHA DEL
                              PROYECTO with that moment's date (see
                              main_window.py's _on_category_toggled).
    TIPO (-> work_type)      ESTUDIO / CONSULTORÍA / PROYECTO / DO + CSS
    SUBTIPO1 (-> subtipo1)   Estación de servicio / Establecimiento /
                              Industrial / Urbanización
    SUBTIPO2 (-> subtipo2)   Certificado / Habilitación / Accesos /
                              Instalaciones / DIC

Two pairs of raw columns are additionally exposed as combined,
read-only display fields for convenience (NOT written back to directly
— editing goes through the raw city/province/subtipo1/subtipo2 fields
instead, one column each):
    CIUDAD + PROVINCIA   -> location ("CIUDAD (PROVINCIA)")
    SUBTIPO1 + SUBTIPO2  -> project_type ("SUBTIPO1 / SUBTIPO2")
"""

from __future__ import annotations

import datetime
from pathlib import Path

import openpyxl

YEARS_WITH_DATA = ("2024", "2025", "2026")

_REQUIRED_HEADERS = ("NOMBRE",)

# (header text in the sheet, field key, is this a date column?). Order
# here doesn't matter for parsing (headers are matched by name, not
# position) — it's just documentation-by-proximity.
FIELD_MAP = [
    ("NUMERO DE PROYECTO", "project_number", False),
    ("NOMBRE", "project_name", False),
    ("PLANNING", "status", False),
    ("LISTADO DE EMPLEADOS", "employee_list", False),
    ("TRABAJOS A REALIZAR", "work_description", False),
    ("TITULO ENTERO DEL PROYECTO", "full_title", False),
    ("CIUDAD", "city", False),
    ("PROVINCIA", "province", False),
    ("FECHA DEL PROYECTO", "start_date", True),
    ("PROMOTOR", "client", False),
    ("TIPO", "work_type", False),
    ("SUBTIPO1", "subtipo1", False),
    ("SUBTIPO2", "subtipo2", False),
    ("COMIENZO DE LA OBRA", "work_start_date", True),
    ("FIN DE LA OBRA", "deadline", True),
    # Older/fuller-schema columns — kept for backwards compatibility
    # with files that still have them.
    ("PRESUPUESTO EN Nº\nEJECUCIÓN MATERIAL", "budget_execution", False),
    ("m2 SUELO DESARROLLADO", "m2_suelo", False),
    ("m2 URBANIZADO. EDIFICABILIDAD", "m2_urbanizado", False),
    ("NOTAS", "notes", False),
    ("COPIAS IMPRESAS EN PAPEL O COPIAS EN CD", "copies", False),
    ("FIRMADO", "signed", False),
    ("FECHA DE LA FIRMA", "signed_date", True),
    ("VISADO", "visa", False),
    ("NUMERO DE EXPEDIENTE", "file_number", False),
    ("FECHA DEL VISADO", "visa_date", True),
    ("WEB 1", "web1", False),
    ("WEB 2 COM ONLINE", "web2", False),
    ("KPI- CONCURSO", "kpi_concurso", False),
    ("KPI-M2 DESARROLLADOS", "kpi_m2", False),
    ("KPI-DO", "kpi_do", False),
    ("ASISTENCIA TÉCNICA", "technical_assistance", False),
    ("CERTIFICADO DE SOLVENCIA", "solvency_certificate", False),
    ("PRESUPUESTO DE LAS OBRAS", "budget_works", False),
    ("IMPORTE DEL CONTRATO", "contract_amount", False),
    ("ADMINISTRACIÓN CONTRATANTE", "contracting_admin", False),
    ("CERT REPR BI", "cert_repr_bi", False),
    ("CERT REPR IVA", "cert_repr_iva", False),
    ("CERT REPR TOTALES", "cert_repr_totales", False),
]

# key -> header, and key -> is_date, derived from FIELD_MAP so writer.py
# never has to duplicate the mapping.
KEY_TO_HEADER = {key: header for header, key, _is_date in FIELD_MAP}
_KEY_IS_DATE = {key: is_date for _header, key, is_date in FIELD_MAP}

# Exact categories confirmed from the master workbook's "TIPOS" sheet —
# used to render these four fields as radio-button pickers in the edit
# form rather than free text.
CATEGORY_OPTIONS = {
    "status": ["Inicio Proyecto", "Proceso", "Acabado", "Cancelado", "DO"],
    "work_type": ["ESTUDIO", "CONSULTORÍA", "PROYECTO", "DO + CSS"],
    "subtipo1": ["Estación de servicio", "Establecimiento", "Industrial", "Urbanización"],
    "subtipo2": ["Certificado", "Habilitación", "Accesos", "Instalaciones", "DIC"],
}


def _format_date(value) -> str | None:
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _cell(row_values: dict, header: str):
    """A cell's value as a plain string, or None — coerced defensively:
    a handful of rows in the real file have a stray date or number typed
    into a column that's normally text (e.g. a date accidentally entered
    in SUBTIPO1), which would otherwise crash string-joining logic
    downstream."""
    value = row_values.get(header)
    if value is None:
        return None
    if isinstance(value, (datetime.date, datetime.datetime)):
        return _format_date(value)
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return str(value)


def _company_from_name(project_name: str) -> str:
    if " - " in project_name:
        return project_name.split(" - ", 1)[0].strip()
    return project_name.strip()


def _join_nonempty(*parts: str | None, sep: str = " / ") -> str | None:
    values = [p for p in parts if p]
    return sep.join(values) if values else None


def _rows_from_sheet(ws) -> tuple[list, list[list]]:
    """(headers, raw_data_rows) — headers from row 1 (stripped strings),
    every row from row 2 on as a list of raw cell values, in sheet
    order."""
    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    headers = [h.strip() if isinstance(h, str) else h for h in header_row]
    data_rows = [list(r) for r in ws.iter_rows(min_row=2, values_only=True)]
    return headers, data_rows


def _row_dict_from_raw(
    headers: list, raw_row: list, year: int, next_id: int, row_number: int
) -> dict | None:
    """Build one normalized row dict from a raw (headers, row) pair, or
    None if this row has no NOMBRE (a blank spacer row). `row_number` is
    the row's actual 1-based position in the worksheet (header is row 1,
    so the first data row is 2) — carried along so writer.py can find
    this exact cell again later without re-matching by content."""
    row_values = {
        headers[i]: raw_row[i]
        for i in range(min(len(headers), len(raw_row)))
        if headers[i]
    }
    project_name = _cell(row_values, "NOMBRE")
    if not project_name:
        return None

    result = {"id": next_id, "year": year, "row_number": row_number}
    for header, key, is_date in FIELD_MAP:
        result[key] = _format_date(row_values.get(header)) if is_date else _cell(row_values, header)

    result["company"] = _company_from_name(project_name)
    city, province = result.get("city"), result.get("province")
    result["location"] = f"{city} ({province})" if city and province else (city or province)
    result["project_type"] = _join_nonempty(result.get("subtipo1"), result.get("subtipo2"))
    return result


def split_workbook_by_year(source_path: str, output_dir: str) -> list[str]:
    """One-time conversion: read the master multi-sheet workbook at
    `source_path` and write a separate single-sheet workbook per year
    (only for years in YEARS_WITH_DATA that actually have a sheet in the
    source) into `output_dir`, named "2024.xlsx" / "2025.xlsx" /
    "2026.xlsx" — an exact copy of that year's header row + every data
    row, same order, same cell types (dates stay real dates). Returns
    the list of file paths written. Safe to re-run any time the master
    workbook is updated — each run overwrites the same 3 files."""
    if not Path(source_path).exists():
        raise FileNotFoundError(f"No se encuentra el archivo origen: {source_path}")

    wb = openpyxl.load_workbook(source_path, data_only=True, read_only=True)
    written = []
    try:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        for year_str in YEARS_WITH_DATA:
            if year_str not in wb.sheetnames:
                continue
            headers, data_rows = _rows_from_sheet(wb[year_str])

            out_wb = openpyxl.Workbook()
            out_ws = out_wb.active
            out_ws.title = year_str
            out_ws.append(headers)
            for row in data_rows:
                out_ws.append(row)

            out_path = str(Path(output_dir) / f"{year_str}.xlsx")
            out_wb.save(out_path)
            out_wb.close()
            written.append(out_path)
    finally:
        wb.close()
    return written


def load_project_info(dir_path: str) -> list[dict]:
    """Read every project row from the per-year workbook files
    ("2024.xlsx", "2025.xlsx", "2026.xlsx" — see split_workbook_by_year)
    inside `dir_path`. Whichever of the three files actually exist are
    read; a missing one is skipped rather than treated as an error,
    since the source data itself only covers these years and even that
    coverage may be incomplete at any given time. Raises
    FileNotFoundError only if the directory itself is missing, or none
    of the three expected files are present at all.

    Returns a list of dicts, each with a stable `id` (its position in
    this combined list, in the SAME order the rows appear in the sheets
    — year by year in YEARS_WITH_DATA order, top to bottom within each
    year — deliberately not re-sorted, since the sheet's own order is
    meaningful) plus every field documented in this module's docstring.

    Rows with no NOMBRE are skipped (blank spacer rows the workbook uses
    between sections)."""
    dir_p = Path(dir_path)
    if not dir_p.exists() or not dir_p.is_dir():
        raise FileNotFoundError(
            f"No se encuentra la carpeta de información de proyectos: {dir_path}"
        )

    results = []
    next_id = 0
    any_file_found = False
    for year_str in YEARS_WITH_DATA:
        file_path = dir_p / f"{year_str}.xlsx"
        if not file_path.exists():
            continue
        any_file_found = True
        year = int(year_str)

        wb = openpyxl.load_workbook(str(file_path), data_only=True, read_only=True)
        try:
            ws = wb.worksheets[0]
            headers, data_rows = _rows_from_sheet(ws)
            if not any(h in _REQUIRED_HEADERS for h in headers):
                continue  # doesn't look like a project table at all

            for offset, raw_row in enumerate(data_rows):
                row_number = offset + 2  # row 1 is the header
                row_dict = _row_dict_from_raw(headers, raw_row, year, next_id, row_number)
                if row_dict is None:
                    continue
                results.append(row_dict)
                next_id += 1
        finally:
            wb.close()

    if not any_file_found:
        raise FileNotFoundError(
            f"No se encontró ningún archivo de año (2024.xlsx, 2025.xlsx, 2026.xlsx) en: {dir_path}"
        )
    return results