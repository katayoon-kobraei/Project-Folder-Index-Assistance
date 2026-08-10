"""
Reads the "LISTADO PROYECTOS POR AÑOS" workbook that backs the Proyectos
Info page — a hand-maintained Excel file, NOT the crawled P: index.

The real source is a single multi-sheet workbook (one sheet per year),
but the app reads from a FOLDER of separate single-sheet files instead —
one per year, named "2024.xlsx", "2025.xlsx", "2026.xlsx" — produced by
split_workbook_by_year() below (also runnable as
scripts/split_project_info_by_year.py). Only 2024, 2025, and 2026
currently have real data, so those are the only files ever read; a
folder missing one of the three still works fine with whichever are
present.

Column mapping (Spanish header in the sheet -> field key), confirmed
against the real file — a NOMBRE value is NOT unique: the same project
can have several rows (e.g. one per document/task), each row is kept as
its own separate entry, in the same order they appear in the sheet:
    NOMBRE                              -> project_name (also the source
                                            for `company`: everything
                                            before " - ")
    PLANNING                            -> status
    TRABAJOS A REALIZAR                 -> work_description
    TITULO ENTERO DEL PROYECTO          -> full_title
    CIUDAD + PROVINCIA                  -> location ("CIUDAD (PROVINCIA)")
    FECHA DEL PROYECTO                  -> start_date
    PROMOTOR                            -> client
    TIPO                                -> work_type
    SUBTIPO1 + SUBTIPO2                 -> project_type
    PRESUPUESTO EN Nº EJECUCIÓN MATERIAL -> budget_execution
    m2 SUELO DESARROLLADO               -> m2_suelo
    m2 URBANIZADO. EDIFICABILIDAD       -> m2_urbanizado
    NOTAS                               -> notes
    COPIAS IMPRESAS EN PAPEL O COPIAS EN CD -> copies
    FIRMADO                             -> signed
    FECHA DE LA FIRMA                   -> signed_date
    VISADO                              -> visa
    NUMERO DE EXPEDIENTE                -> file_number
    FECHA DEL VISADO                    -> visa_date
    WEB 1                               -> web1
    WEB 2 COM ONLINE                    -> web2
    KPI- CONCURSO                       -> kpi_concurso
    KPI-M2 DESARROLLADOS                -> kpi_m2
    KPI-DO                              -> kpi_do
    ASISTENCIA TÉCNICA                  -> technical_assistance
    COMIENZO DE LA OBRA                 -> work_start_date
    FIN DE LA OBRA                      -> deadline
    CERTIFICADO DE SOLVENCIA            -> solvency_certificate
    PRESUPUESTO DE LAS OBRAS            -> budget_works
    IMPORTE DEL CONTRATO                -> contract_amount
    ADMINISTRACIÓN CONTRATANTE          -> contracting_admin
    CERT REPR BI                        -> cert_repr_bi
    CERT REPR IVA                       -> cert_repr_iva
    CERT REPR TOTALES                   -> cert_repr_totales
    NUMERO DE PROYECTO                  -> project_number
    (none)                              -> employees_assigned: there is
                                            NO employee count anywhere in
                                            this workbook. Always None.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import openpyxl

YEARS_WITH_DATA = ("2024", "2025", "2026")

_REQUIRED_HEADERS = ("NOMBRE",)

# (header text in the sheet, key in the returned row dict). Order here
# doesn't matter for parsing (headers are matched by name, not
# position) — it's just documentation-by-proximity to the mapping above.
_COLUMN_MAP = [
    ("TRABAJOS A REALIZAR", "work_description"),
    ("TITULO ENTERO DEL PROYECTO", "full_title"),
    ("PROMOTOR", "client"),
    ("TIPO", "work_type"),
    ("PRESUPUESTO EN Nº\nEJECUCIÓN MATERIAL", "budget_execution"),
    ("m2 SUELO DESARROLLADO", "m2_suelo"),
    ("m2 URBANIZADO. EDIFICABILIDAD", "m2_urbanizado"),
    ("NOTAS", "notes"),
    ("COPIAS IMPRESAS EN PAPEL O COPIAS EN CD", "copies"),
    ("FIRMADO", "signed"),
    ("FECHA DE LA FIRMA", "signed_date"),
    ("VISADO", "visa"),
    ("NUMERO DE EXPEDIENTE", "file_number"),
    ("FECHA DEL VISADO", "visa_date"),
    ("WEB 1", "web1"),
    ("WEB 2 COM ONLINE", "web2"),
    ("KPI- CONCURSO", "kpi_concurso"),
    ("KPI-M2 DESARROLLADOS", "kpi_m2"),
    ("KPI-DO", "kpi_do"),
    ("ASISTENCIA TÉCNICA", "technical_assistance"),
    ("COMIENZO DE LA OBRA", "work_start_date"),
    ("CERTIFICADO DE SOLVENCIA", "solvency_certificate"),
    ("PRESUPUESTO DE LAS OBRAS", "budget_works"),
    ("IMPORTE DEL CONTRATO", "contract_amount"),
    ("ADMINISTRACIÓN CONTRATANTE", "contracting_admin"),
    ("CERT REPR BI", "cert_repr_bi"),
    ("CERT REPR IVA", "cert_repr_iva"),
    ("CERT REPR TOTALES", "cert_repr_totales"),
]
# Headers whose raw value is a real date and should go through the
# dd/mm/yyyy formatter, same as start_date/deadline.
_DATE_COLUMN_MAP = [
    ("FECHA DE LA FIRMA", "signed_date"),
    ("FECHA DEL VISADO", "visa_date"),
]


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


def _row_dict_from_raw(headers: list, raw_row: list, year: int, next_id: int) -> dict | None:
    """Build one normalized row dict from a raw (headers, row) pair, or
    None if this row has no NOMBRE (a blank spacer row)."""
    row_values = {
        headers[i]: raw_row[i]
        for i in range(min(len(headers), len(raw_row)))
        if headers[i]
    }
    project_name = _cell(row_values, "NOMBRE")
    if not project_name:
        return None

    city = _cell(row_values, "CIUDAD")
    province = _cell(row_values, "PROVINCIA")
    location = f"{city} ({province})" if city and province else (city or province)

    result = {
        "id": next_id,
        "year": year,
        "company": _company_from_name(project_name),
        "project_name": project_name,
        "project_number": _cell(row_values, "NUMERO DE PROYECTO"),
        "location": location,
        "status": _cell(row_values, "PLANNING"),
        "project_type": _join_nonempty(
            _cell(row_values, "SUBTIPO1"), _cell(row_values, "SUBTIPO2")
        ),
        "start_date": _format_date(row_values.get("FECHA DEL PROYECTO")),
        "deadline": _format_date(row_values.get("FIN DE LA OBRA")),
        "work_start_date": _format_date(row_values.get("COMIENZO DE LA OBRA")),
        "employees_assigned": None,
    }
    for header, key in _COLUMN_MAP:
        result[key] = _cell(row_values, header)
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

            for raw_row in data_rows:
                row_dict = _row_dict_from_raw(headers, raw_row, year, next_id)
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