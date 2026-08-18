"""
Desktop UI shell for Indice de Proyectos.

Structured the same way as the reference INGEVIA Email Assistant desktop
app (dark sidebar nav, colored metric cards, QThread for background work,
QStackedWidget page switching) but wired to this project's own data via
data_service.py. There is no Flask server anywhere in this file — every
page calls data_service functions directly, which in turn call the exact
same src/search/search.py and src/webapp/pipeline.py code the browser
version uses, against the same config/config.yaml + database.

The one piece of web technology kept as-is is the location graph: it
still renders with Cytoscape.js (same library, same visual style as the
browser version), but instead of Flask serving that page over HTTP, this
generates a small self-contained HTML string with the graph data AND the
Cytoscape.js library itself embedded inline (vendor/cytoscape.min.js,
bundled with the app rather than fetched from a CDN) and loads it into a
QWebEngineView — a genuinely offline desktop widget: opening a project's
graph never depends on network speed or connectivity.
"""

from __future__ import annotations

import datetime
import json
import re
import time
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtWebEngineWidgets import QWebEngineView

    _HAS_WEBENGINE = True
except ImportError:  # pragma: no cover - depends on the machine's PySide6 build
    _HAS_WEBENGINE = False

from desktop_app import data_service
from src.project_info.reader import CATEGORY_OPTIONS, YEARS_WITH_DATA  # noqa: E402

STAGE_LABELS = {
    "crawling": "Recorriendo carpetas...",
    "resolving_projects": "Agrupando proyectos...",
    "detecting_locations": "Detectando ubicaciones...",
    # Only reached in shared index mode (see config.example.yaml) — the
    # builder PC copying its just-finished local index to the shared
    # server path so every other PC picks it up.
    "publishing": "Publicando índice compartido...",
}
PHASE_LABELS = {"direccion_obra": "Dirección de obra"}
STATUS_LABELS = {
    "unknown": "Desconocido",
    "active": "Activo",
    "dormant": "Inactivo",
    "completed": "Completado",
}
SOURCE_LABELS = {"trabajos": "Trabajos", "ofertas": "Ofertas"}
FILE_SEARCH_LIMIT = 300


def open_local_path(parent: QWidget, path: str, what: str) -> None:
    """QDesktopServices-open a local file/folder path (launches whatever
    app Windows has associated with it — Excel for .xlsx), showing a
    Spanish warning dialog on `parent` if it fails (file no longer
    exists, no default app registered, etc.) instead of silently doing
    nothing. Shared by every "Abrir Excel"/"Abrir carpeta"/"Abrir
    archivo" button added after this helper existed; a few earlier
    buttons (SearchPage) still inline the same two statements directly
    rather than being refactored to call this, purely to avoid touching
    already-working code without a reason to."""
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
        QMessageBox.warning(
            parent,
            f"No se pudo abrir {what}",
            f"No se pudo abrir:\n{path}\n\n¿Sigue existiendo en esa ubicación?",
        )


def containing_folder(path: str) -> str:
    """The folder a file lives in, given its full path. Plain rsplit on
    the Windows separator, not os.path.dirname — these paths are always
    Windows-style ("P:\\...") regardless of what OS this app itself
    happens to run on."""
    idx = path.rfind("\\")
    return path[:idx] if idx != -1 else path


def configure_table(table: QTableWidget) -> None:
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setShowGrid(False)
    table.verticalHeader().setVisible(False)
    # Interactive (not ResizeToContents): with ResizeToContents, Qt
    # recomputes every column's width on EVERY row insertion once the
    # table is visible, which is fine for a handful of rows but turns
    # populating the full ~600-row Proyectos list into a multi-second (or
    # worse) stall. Callers do one resizeColumnsToContents() after
    # populating instead — same initial sizing, no per-row cost.
    table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    table.horizontalHeader().setStretchLastSection(True)


class RefreshWorker(QThread):
    """Runs data_service.start_refresh() and then polls its status every
    second until it finishes — the polling loop happens off the GUI
    thread, so the window stays responsive the whole time, same idea as
    the browser version's setInterval poll against /api/refresh/status."""

    finished_with_result = Signal(bool, str, dict)
    stage_changed = Signal(str)

    def run(self) -> None:
        try:
            started = data_service.start_refresh()
            if not started:
                # start_refresh always sets a specific reason for why it
                # refused — a real "already running" on this PC, or (in
                # shared index mode, see config.example.yaml) this PC
                # isn't the designated builder, or another PC currently
                # holds the shared-index lock. Read it back instead of
                # assuming it's always the "already running" case.
                reason = data_service.refresh_status().get("error") or "Ya hay una actualización en curso."
                self.finished_with_result.emit(False, reason, {})
                return
            last_stage = None
            while True:
                status = data_service.refresh_status()
                if status.get("stage") != last_stage:
                    last_stage = status.get("stage")
                    if last_stage:
                        self.stage_changed.emit(last_stage)
                if status["status"] in ("done", "error"):
                    break
                time.sleep(1)
            if status["status"] == "error":
                self.finished_with_result.emit(False, status.get("error") or "Error desconocido", {})
            else:
                self.finished_with_result.emit(True, "Actualización completada.", status.get("stats") or {})
        except Exception as exc:  # pragma: no cover - defensive
            self.finished_with_result.emit(False, str(exc), {})


class SyncTrabajosWorker(QThread):
    """Runs data_service.sync_new_projects_from_trabajos() off the GUI
    thread — a plain folder scan + a handful of xlsx appends, nowhere
    near as slow as a full P: crawl, but still worth not blocking the
    window on since TRABAJOS <year> lives on the network drive too.
    Unlike RefreshWorker there's no polling: sync_new_projects_from_
    trabajos() runs to completion and returns its own result dict
    directly, no background-thread/status-polling machinery involved."""

    finished_with_result = Signal(bool, str, dict)

    def __init__(self, year: int, parent=None) -> None:
        super().__init__(parent)
        self._year = year

    def run(self) -> None:
        try:
            result = data_service.sync_new_projects_from_trabajos(year=self._year)
            self.finished_with_result.emit(True, "", result)
        except Exception as exc:
            self.finished_with_result.emit(False, str(exc), {})


class ProjectsPage(QWidget):
    """Full, browsable A-Z list of every company/project (the depth=1
    job/offer folders directly inside TRABAJOS <year>), with a year
    filter and a name filter above it — no row cap, unlike the quick
    Buscar box, and sorted alphabetically rather than by recency, so a
    project from 2008 shows up exactly where its name belongs instead of
    sinking below a flood of current-year entries."""

    project_opened = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 22, 26, 26)
        outer.setSpacing(14)

        heading = QLabel("Proyectos")
        heading.setObjectName("SectionTitle")
        outer.addWidget(heading)

        self.status_pill = QLabel("")
        self.status_pill.setObjectName("NeutralPill")
        self.status_pill.setVisible(False)
        outer.addWidget(self.status_pill, alignment=Qt.AlignLeft)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        outer.addWidget(self.progress)

        filters_row = QHBoxLayout()
        filters_row.setSpacing(10)

        filters_label = QLabel("Filtros:")
        filters_label.setObjectName("MetricTitle")
        filters_row.addWidget(filters_label)

        self.year_filter = QComboBox()
        self.year_filter.addItem("Todos los años", None)
        self.year_filter.currentIndexChanged.connect(self._on_filters_changed)
        filters_row.addWidget(self.year_filter)

        self.name_filter = QLineEdit()
        self.name_filter.setPlaceholderText("Filtrar por nombre de empresa o proyecto...")
        self.name_filter.textChanged.connect(self._on_text_changed)
        filters_row.addWidget(self.name_filter, 1)

        outer.addLayout(filters_row)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._run_filter)

        self.count_label = QLabel("")
        self.count_label.setObjectName("MutedText")
        outer.addWidget(self.count_label)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Nombre", "Año inicio", "Año fin"])
        configure_table(self.table)
        self.table.doubleClicked.connect(self._open_selected)
        outer.addWidget(self.table, 1)

        self._rows: list[dict] = []
        self._years_loaded = False

    def _load_years(self) -> None:
        if self._years_loaded:
            return
        try:
            years = data_service.available_years()
        except Exception:
            years = []
        for year in years:
            self.year_filter.addItem(str(year), year)
        self._years_loaded = True

    def _on_text_changed(self) -> None:
        self._debounce.start()

    def _on_filters_changed(self) -> None:
        self._run_filter()

    def _run_filter(self) -> None:
        name_query = self.name_filter.text().strip()
        year = self.year_filter.currentData()
        try:
            self._rows = data_service.all_projects(name_query, year)
        except Exception as exc:
            QMessageBox.critical(self, "Error al cargar proyectos", str(exc))
            self._rows = []
        self._render()

    def _render(self) -> None:
        self.count_label.setText(f"{len(self._rows)} proyecto(s)")
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(self._rows))
        for row_idx, project in enumerate(self._rows):
            self.table.setItem(row_idx, 0, QTableWidgetItem(project["canonical_name"]))
            start_item = QTableWidgetItem(str(project.get("first_seen_year") or "?"))
            start_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row_idx, 1, start_item)
            end_item = QTableWidgetItem(str(project.get("last_seen_year") or "?"))
            end_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row_idx, 2, end_item)
        self.table.resizeColumnsToContents()
        self.table.setUpdatesEnabled(True)

    def refresh(self) -> None:
        self._load_years()
        self._run_filter()

    def _open_selected(self, _index) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self._rows):
            self.project_opened.emit(self._rows[row]["id"])

    def set_processing(self, active: bool, stage_text: str = "") -> None:
        self.progress.setVisible(active)
        self.status_pill.setVisible(active or bool(stage_text))
        if active:
            self.status_pill.setText(stage_text or "Actualizando...")
            self.status_pill.setObjectName("WarningPill")
        self.status_pill.style().unpolish(self.status_pill)
        self.status_pill.style().polish(self.status_pill)


class SearchPage(QWidget):
    """One search box plus a year/company filter row (same idea as
    Proyectos), two result sets — both over the raw filesystem, not the
    resolved projects table: matching FOLDERS (by the folder's own
    name/address, e.g. a site folder named after a street, via
    folders_fts) and matching individual FILES (via files_fts). Neither
    searches project canonical names — that's what the Proyectos page's
    own filter is for. All three inputs (text, year, company) narrow the
    same two result sets together; both stay empty until at least one of
    them is actually set.

    Each folder row also has a "Proyectos Info" button — the crawler
    already parses a job_code/site_code per folder (e.g. "22-007" /
    "22-007-02", same "YY-NNN..." numbering as NUMERO DE PROYECTO in the
    Proyectos Info workbook), so clicking it looks up the matching
    row(s) there (see data_service.find_project_info_by_job_code) and
    opens that project's detail card directly — the same page with the
    category radio pickers — without having to go hunt for it by
    company name on the Proyectos Info page."""

    project_info_requested = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 22, 26, 26)
        outer.setSpacing(14)

        heading = QLabel("Buscar carpetas y archivos")
        heading.setObjectName("SectionTitle")
        outer.addWidget(heading)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar por nombre de carpeta, dirección o archivo...")
        self.search.textChanged.connect(self._on_text_changed)
        outer.addWidget(self.search)

        filters_row = QHBoxLayout()
        filters_row.setSpacing(10)

        filters_label = QLabel("Filtros:")
        filters_label.setObjectName("MetricTitle")
        filters_row.addWidget(filters_label)

        self.year_filter = QComboBox()
        self.year_filter.addItem("Todos los años", None)
        self.year_filter.currentIndexChanged.connect(self._on_filters_changed)
        filters_row.addWidget(self.year_filter)

        self.company_filter = QLineEdit()
        self.company_filter.setPlaceholderText("Filtrar por empresa/proyecto...")
        self.company_filter.textChanged.connect(self._on_text_changed)
        filters_row.addWidget(self.company_filter, 1)

        outer.addLayout(filters_row)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._run_search)

        self._years_loaded = False
        self._load_years()

        folders_title = QLabel("Carpetas")
        folders_title.setObjectName("SectionTitle")
        outer.addWidget(folders_title)

        self.count_label = QLabel("")
        self.count_label.setObjectName("MutedText")
        outer.addWidget(self.count_label)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["", "Carpeta", "Proyecto", "Año", "Ubicación", "Ruta"]
        )
        configure_table(self.table)
        self._set_fixed_result_column_widths(self.table)
        # Ruta needs to show the FULL path, even past the edge of the
        # window — stretchLastSection (on by default from configure_table)
        # caps the last column at the visible width, which is exactly
        # what was truncating it. Turning it off lets the column grow to
        # its real content width instead, with the table's own
        # horizontal scrollbar handling anything wider than the window.
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.doubleClicked.connect(self._open_selected_folder)
        outer.addWidget(self.table, 1)

        files_title = QLabel("Archivos")
        files_title.setObjectName("SectionTitle")
        outer.addWidget(files_title)

        self.files_count_label = QLabel("")
        self.files_count_label.setObjectName("MutedText")
        outer.addWidget(self.files_count_label)

        self.files_table = QTableWidget(0, 6)
        self.files_table.setHorizontalHeaderLabels(
            ["", "Archivo", "Proyecto", "Año", "Ubicación", "Carpeta"]
        )
        configure_table(self.files_table)
        # Same fixed widths as the Carpetas table, shifted one column
        # right to make room for the "open containing folder" button.
        self.files_table.setColumnWidth(0, 40)
        self.files_table.setColumnWidth(1, 260)
        self.files_table.setColumnWidth(2, 200)
        self.files_table.setColumnWidth(3, 60)
        self.files_table.setColumnWidth(4, 160)
        # Carpeta (the file's containing path) needs to show in FULL too,
        # same reasoning as Ruta on the Carpetas table above — turn off
        # stretchLastSection so the column can grow past the window
        # edge, with the table's own horizontal scrollbar taking over.
        self.files_table.horizontalHeader().setStretchLastSection(False)
        self.files_table.doubleClicked.connect(self._open_selected_file)
        outer.addWidget(self.files_table, 1)

        self._rows: list[dict] = []
        self._file_rows: list[dict] = []
        self._run_search()

    @staticmethod
    def _set_fixed_result_column_widths(table: QTableWidget) -> None:
        # Fixed widths for the icon-button column and the middle
        # columns, set ONCE rather than recomputed from cell contents on
        # every keystroke — the one column each table needs shown in
        # FULL (Ruta / Archivo) is separately sized to its actual
        # content in _render()/_render_files() instead, which is cheap
        # for a single column.
        table.setColumnWidth(0, 40)
        table.setColumnWidth(1, 260)
        table.setColumnWidth(2, 200)
        table.setColumnWidth(3, 60)
        table.setColumnWidth(4, 160)

    def _load_years(self) -> None:
        if self._years_loaded:
            return
        try:
            years = data_service.available_years()
        except Exception:
            years = []
        for year in years:
            self.year_filter.addItem(str(year), year)
        self._years_loaded = True

    def _on_text_changed(self) -> None:
        self._debounce.start()

    def _on_filters_changed(self) -> None:
        self._run_search()

    def _run_search(self) -> None:
        query = self.search.text().strip()
        year = self.year_filter.currentData()
        company_query = self.company_filter.text().strip()
        try:
            self._rows = data_service.folders(query, FILE_SEARCH_LIMIT, year, company_query)
        except Exception as exc:
            QMessageBox.critical(self, "Error al buscar carpetas", str(exc))
            self._rows = []
        try:
            self._file_rows = data_service.files(query, FILE_SEARCH_LIMIT, year, company_query)
        except Exception as exc:
            QMessageBox.critical(self, "Error al buscar archivos", str(exc))
            self._file_rows = []
        self._render()
        self._render_files()

    def _has_any_filter(self) -> bool:
        return bool(
            self.search.text().strip()
            or self.year_filter.currentData() is not None
            or self.company_filter.text().strip()
        )

    def _render(self) -> None:
        total = len(self._rows)
        if not self._has_any_filter():
            self.count_label.setText("Escribe o filtra para buscar carpetas.")
        elif total >= FILE_SEARCH_LIMIT:
            self.count_label.setText(
                f"Mostrando las primeras {total} carpetas — afina la búsqueda para ver menos."
            )
        else:
            self.count_label.setText(f"{total} carpeta(s)")

        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(total)
        for row_idx, folder in enumerate(self._rows):
            # Icon button first, same column-0 placement and style as
            # the Archivos table's "open containing folder" button below.
            pi_button = QPushButton("ℹ️")
            pi_button.setObjectName("LinkButton")
            pi_button.setCursor(Qt.PointingHandCursor)
            pi_button.setToolTip(
                "Buscar y abrir la ficha de este proyecto en Proyectos Info"
            )
            pi_button.clicked.connect(
                lambda checked=False, i=row_idx: self._open_project_info_for_folder(i)
            )
            self.table.setCellWidget(row_idx, 0, pi_button)

            self.table.setItem(row_idx, 1, QTableWidgetItem(folder["name"]))
            self.table.setItem(row_idx, 2, QTableWidgetItem(folder.get("company_project") or ""))
            self.table.setItem(row_idx, 3, QTableWidgetItem(str(folder.get("year") or "?")))
            self.table.setItem(row_idx, 4, QTableWidgetItem(folder.get("location_site") or ""))
            self.table.setItem(row_idx, 5, QTableWidgetItem(folder["path"]))
        # Only this one column, not resizeColumnsToContents() for all
        # six — measuring just the path column is a few ms even at 300
        # rows (confirmed), vs. the near-hang measuring every column
        # caused earlier when it ran on every keystroke.
        self.table.resizeColumnToContents(5)
        self.table.setUpdatesEnabled(True)

    def _render_files(self) -> None:
        total = len(self._file_rows)
        if not self._has_any_filter():
            self.files_count_label.setText("Escribe o filtra para buscar archivos.")
        elif total >= FILE_SEARCH_LIMIT:
            self.files_count_label.setText(
                f"Mostrando los primeros {total} resultados — afina la búsqueda para ver menos."
            )
        else:
            self.files_count_label.setText(f"{total} archivo(s)")

        self.files_table.setUpdatesEnabled(False)
        self.files_table.setRowCount(total)
        for row_idx, f in enumerate(self._file_rows):
            open_button = QPushButton("📂")
            open_button.setObjectName("LinkButton")
            open_button.setCursor(Qt.PointingHandCursor)
            open_button.setToolTip("Abrir la carpeta que contiene este archivo")
            open_button.clicked.connect(lambda checked=False, i=row_idx: self._open_folder_for_file(i))
            self.files_table.setCellWidget(row_idx, 0, open_button)

            self.files_table.setItem(row_idx, 1, QTableWidgetItem(f["name"]))
            self.files_table.setItem(row_idx, 2, QTableWidgetItem(f.get("company_project") or ""))
            self.files_table.setItem(row_idx, 3, QTableWidgetItem(str(f.get("file_year") or "?")))
            self.files_table.setItem(row_idx, 4, QTableWidgetItem(f.get("location_site") or ""))
            self.files_table.setItem(row_idx, 5, QTableWidgetItem(f["path"]))
        # Only these two columns, same reasoning as the Carpetas table
        # above — cheap for a couple of columns, not for all five.
        self.files_table.resizeColumnToContents(1)
        self.files_table.resizeColumnToContents(5)
        self.files_table.setUpdatesEnabled(True)

    def refresh(self) -> None:
        self._load_years()
        self._run_search()

    def _open_path(self, path: str, what: str) -> None:
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            QMessageBox.warning(
                self,
                f"No se pudo abrir {what}",
                f"No se pudo abrir:\n{path}\n\n¿Sigue existiendo en esa ubicación?",
            )

    def _open_selected_folder(self, _index) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self._rows):
            self._open_path(self._rows[row]["path"], "la carpeta")

    def _open_selected_file(self, _index) -> None:
        row = self.files_table.currentRow()
        if 0 <= row < len(self._file_rows):
            self._open_path(self._file_rows[row]["path"], "el archivo")

    def _open_folder_for_file(self, row_idx: int) -> None:
        if 0 <= row_idx < len(self._file_rows):
            folder = containing_folder(self._file_rows[row_idx]["path"])
            self._open_path(folder, "la carpeta")

    def _open_project_info_for_folder(self, row_idx: int) -> None:
        if not (0 <= row_idx < len(self._rows)):
            return
        folder = self._rows[row_idx]
        # Prefer site_code (more specific, e.g. "22-007-02") when this
        # folder is itself a specific site; fall back to job_code (the
        # whole project, e.g. "22-007") otherwise.
        code = folder.get("site_code") or folder.get("job_code")
        if not code:
            QMessageBox.information(
                self,
                "Sin código de proyecto",
                "Esta carpeta no tiene un código de proyecto reconocido (formato "
                "AA-NNN), así que no se puede relacionar automáticamente con "
                "Proyectos Info.",
            )
            return
        try:
            matches = data_service.find_project_info_by_job_code(code, folder.get("year"))
        except Exception as exc:
            QMessageBox.critical(self, "Error al buscar en Proyectos Info", str(exc))
            return
        if not matches:
            QMessageBox.information(
                self,
                "Sin coincidencias",
                f'No se encontró ningún proyecto con el código "{code}" en Proyectos Info.',
            )
            return
        if len(matches) > 1:
            QMessageBox.information(
                self,
                "Varias coincidencias",
                f'{len(matches)} filas de Proyectos Info coinciden con el código "{code}". '
                "Se abre la primera — usa el botón Volver y la página Proyectos Info "
                "para ver las demás.",
            )
        self.project_info_requested.emit(matches[0]["id"])


STATUS_COLORS = {"Firmado": "#0F6E56", "Pedido": "#B45309"}


class OfertasPage(QWidget):
    """Every 'Firmado' (contracts/certificates whose filename ends in
    _signed/_f/_fda.pdf) or 'Pedido' (filename starts with 'pedido') PDF
    found inside a FACTURACION or INGEVIA subfolder (any case) of a
    '02.-GESTIÓN' folder — including everything nested further beneath
    that subfolder, not just its own direct children (see get_ofertas'
    docstring for why). Files elsewhere in '02.-GESTIÓN' don't count.
    Same year/company filter row as Buscar; a single result table with
    the full file path always shown, same treatment as Buscar's Ruta/
    Carpeta columns."""

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 22, 26, 26)
        outer.setSpacing(14)

        heading = QLabel("Ofertas: documentos Firmado y Pedido")
        heading.setObjectName("SectionTitle")
        outer.addWidget(heading)

        subtitle = QLabel(
            "Archivos dentro de una carpeta 'FACTURACION' o 'INGEVIA' que esté "
            "directamente dentro de '02.-GESTIÓN' (y sus subcarpetas), con nombre "
            "terminado en _signed/_f/_fda.pdf (Firmado) o que empieza por 'pedido' (Pedido)."
        )
        subtitle.setObjectName("MutedText")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        filters_row = QHBoxLayout()
        filters_row.setSpacing(10)

        filters_label = QLabel("Filtros:")
        filters_label.setObjectName("MetricTitle")
        filters_row.addWidget(filters_label)

        self.year_filter = QComboBox()
        self.year_filter.addItem("Todos los años", None)
        self.year_filter.currentIndexChanged.connect(self._on_filters_changed)
        filters_row.addWidget(self.year_filter)

        self.company_filter = QLineEdit()
        self.company_filter.setPlaceholderText("Filtrar por empresa/proyecto...")
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._run_query)
        self.company_filter.textChanged.connect(lambda _text: self._debounce.start())
        filters_row.addWidget(self.company_filter, 1)

        outer.addLayout(filters_row)

        self._years_loaded = False
        self._load_years()

        self.count_label = QLabel("")
        self.count_label.setObjectName("MutedText")
        outer.addWidget(self.count_label)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["", "Estado", "Proyecto", "Año", "Ubicación", "Ruta"]
        )
        configure_table(self.table)
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 220)
        self.table.setColumnWidth(3, 60)
        self.table.setColumnWidth(4, 180)
        # Ruta shown in full, same reasoning as Buscar's Ruta/Carpeta
        # columns — turn off stretchLastSection so it can grow past the
        # window edge, with the table's own scrollbar taking over.
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.doubleClicked.connect(self._open_selected_file)
        outer.addWidget(self.table, 1)

        self._rows: list[dict] = []

    def _load_years(self) -> None:
        if self._years_loaded:
            return
        try:
            years = data_service.available_years()
        except Exception:
            years = []
        for year in years:
            self.year_filter.addItem(str(year), year)
        self._years_loaded = True

    def _on_filters_changed(self) -> None:
        self._run_query()

    def _run_query(self) -> None:
        year = self.year_filter.currentData()
        company_query = self.company_filter.text().strip()
        try:
            self._rows = data_service.ofertas(year, company_query)
        except Exception as exc:
            QMessageBox.critical(self, "Error al cargar ofertas", str(exc))
            self._rows = []
        self._render()

    def _render(self) -> None:
        total = len(self._rows)
        firmado = sum(1 for r in self._rows if r.get("status") == "Firmado")
        pedido = total - firmado
        self.count_label.setText(f"{total} documento(s) — {firmado} Firmado, {pedido} Pedido")

        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(total)
        for row_idx, r in enumerate(self._rows):
            open_button = QPushButton("📂")
            open_button.setObjectName("LinkButton")
            open_button.setCursor(Qt.PointingHandCursor)
            open_button.setToolTip("Abrir la carpeta que contiene este archivo")
            open_button.clicked.connect(lambda checked=False, i=row_idx: self._open_folder(i))
            self.table.setCellWidget(row_idx, 0, open_button)

            status = r.get("status") or ""
            status_item = QTableWidgetItem(status)
            color = STATUS_COLORS.get(status)
            if color:
                status_item.setForeground(QColor(color))
            self.table.setItem(row_idx, 1, status_item)
            self.table.setItem(row_idx, 2, QTableWidgetItem(r.get("company_project") or ""))
            self.table.setItem(row_idx, 3, QTableWidgetItem(str(r.get("year") or "?")))
            self.table.setItem(row_idx, 4, QTableWidgetItem(r.get("location_site") or ""))
            self.table.setItem(row_idx, 5, QTableWidgetItem(r["path"]))
        # Only the Ruta column, same cheap-resize reasoning used
        # throughout the rest of the app.
        self.table.resizeColumnToContents(5)
        self.table.setUpdatesEnabled(True)

    def refresh(self) -> None:
        self._load_years()
        self._run_query()

    def _open_selected_file(self, _index) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self._rows):
            path = self._rows[row]["path"]
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
                QMessageBox.warning(
                    self,
                    "No se pudo abrir el archivo",
                    f"No se pudo abrir:\n{path}\n\n¿Sigue existiendo en esa ubicación?",
                )

    def _open_folder(self, row_idx: int) -> None:
        if not (0 <= row_idx < len(self._rows)):
            return
        folder = containing_folder(self._rows[row_idx]["path"])
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(folder)):
            QMessageBox.warning(
                self,
                "No se pudo abrir la carpeta",
                f"No se pudo abrir:\n{folder}\n\n¿Sigue existiendo en esa ubicación?",
            )


PROJECT_INFO_FIELDS = [
    # (Spanish label shown on the detail card, key in the dict returned
    # by data_service.project_info_detail). Every field the source
    # workbook has, in a sensible reading order — a field simply doesn't
    # appear on the card at all for a project/row where it's blank (see
    # ProyectosInfoDetailPage.load()), so listing all of them here costs
    # nothing when most rows only have a handful filled in.
    #
    # NOTE: "work_type"/"project_type" (TIPO/SUBTIPO1/SUBTIPO2) are
    # deliberately NOT listed here — they're covered by the live radio
    # pickers in categories_panel instead (see EDIT_CATEGORY_FIELDS
    # below), which show them individually rather than pre-joined and
    # let you change them directly, so repeating them as plain text here
    # too would just be a stale-looking duplicate.
    ("Cliente", "client"),
    ("Ubicación", "location"),
    ("Año", "year"),
    ("Comienzo de la obra", "work_start_date"),
    ("Fecha límite (fin de la obra)", "deadline"),
    ("Número de proyecto", "project_number"),
    ("Trabajos a realizar", "work_description"),
    ("Título completo", "full_title"),
    ("Presupuesto (ejecución material)", "budget_execution"),
    ("Presupuesto de las obras", "budget_works"),
    ("Importe del contrato", "contract_amount"),
    ("m² suelo desarrollado", "m2_suelo"),
    ("m² urbanizado / edificabilidad", "m2_urbanizado"),
    ("Firmado", "signed"),
    ("Fecha de la firma", "signed_date"),
    ("Visado", "visa"),
    ("Número de expediente", "file_number"),
    ("Fecha del visado", "visa_date"),
    ("Certificado de solvencia", "solvency_certificate"),
    ("Administración contratante", "contracting_admin"),
    ("Asistencia técnica", "technical_assistance"),
    ("Copias impresas / CD", "copies"),
    ("Web 1", "web1"),
    ("Web 2 (comunicación online)", "web2"),
    ("KPI concurso", "kpi_concurso"),
    ("KPI m² desarrollados", "kpi_m2"),
    ("KPI dirección de obra", "kpi_do"),
    ("Cert. repr. BI", "cert_repr_bi"),
    ("Cert. repr. IVA", "cert_repr_iva"),
    ("Cert. repr. totales", "cert_repr_totales"),
    ("Notas", "notes"),
]

# "Lista de empleados" and "Fecha del proyecto" are NOT in
# PROJECT_INFO_FIELDS above — unlike every other field, these two are
# always shown on the card (with a placeholder when empty), never hidden
# for being blank, per explicit request: every project should have both
# sections visible. Fecha del proyecto in particular gets auto-filled
# with today's date the moment "Inicio Proyecto" is picked in the
# Planning radios (see _on_category_toggled), so it needs to be visible
# even before that happens, not just after. See reader.py's module
# docstring for the exact source column names.
EMPLOYEE_LIST_FIELD = ("Lista de empleados", "employee_list")
START_DATE_FIELD = ("Fecha del proyecto", "start_date")
# Always rendered first on the card, in this order, before the
# hide-when-blank PROJECT_INFO_FIELDS below.
ALWAYS_SHOWN_FIELDS = [EMPLOYEE_LIST_FIELD, START_DATE_FIELD]

# The company's employee roster — full name, a short "match" name (see
# _row_has_employee), and a small photo file under desktop_app/assets/
# employees/. Backs the read-only avatar row on the project card
# (ProyectosInfoDetailPage.load), the tick/untick checklist in
# ProyectosInfoDetailPage/ProyectosInfoNewPage, AND ConfiguracionPage,
# where the roster itself is managed (add/rename/change photo/delete).
#
# Persisted as JSON at _ROSTER_FILE, NOT a hardcoded constant — it used
# to be, but that meant adding/removing an employee required editing
# source code. load_employees_roster() re-reads that file fresh on
# every call (no in-memory cache): it's a handful of small dict entries,
# cheap enough to not need one, and always reflects whatever
# ConfiguracionPage just saved without any extra "please refresh"
# wiring between pages.
#
# "Lista de empleados" in the source workbook is STILL just one
# free-text cell per project row (see reader.py/writer.py — this roster
# doesn't change that storage format at all). It's only used to
# interpret and produce that text: on load, an employee counts as "on
# this project" if their match-name shows up as whole word(s) somewhere
# in the cell (see _row_has_employee); on save, the ticked employees'
# full names are joined back into one comma-separated string. A project
# whose employee_list was typed by hand before this feature existed
# (e.g. just "Fernando, Milagros") still auto-ticks the right people —
# matching only needs the short first-name form, not the full name with
# surnames, since that's what's realistically been typed in by hand.
_EMPLOYEES_DIR = Path(__file__).parent / "assets" / "employees"
_ROSTER_FILE = _EMPLOYEES_DIR / "roster.json"

# Seeds _ROSTER_FILE the very first time the app runs against it (e.g.
# right after upgrading from the old hardcoded-EMPLOYEES version) — not
# read from again afterward, since load_employees_roster() only falls
# back to this when the JSON file is missing or unreadable.
_DEFAULT_EMPLOYEES = [
    {"name": "Guillermo Gea Marco", "match_name": "Guillermo", "photo": "guillermo_gea_marco.png"},
    {"name": "Fernando García Boullón", "match_name": "Fernando", "photo": "fernando_garcia_boullon.png"},
    {"name": "Stephania Terrosa Sayago", "match_name": "Stephania", "photo": "stephania_terrosa_sayago.png"},
    {"name": "Juan Carlos Anderson Morata", "match_name": "Juan Carlos", "photo": "juan_carlos_anderson_morata.png"},
    {"name": "Katerine Serrano Barbosa", "match_name": "Katerine", "photo": "katerine_serrano_barbosa.png"},
    {"name": "Francisco Vargas Zamora", "match_name": "Francisco", "photo": "francisco_vargas_zamora.png"},
    {"name": "Milagros Llopis Pérez", "match_name": "Milagros", "photo": "milagros_llopis_perez.png"},
    {"name": "Consuelo", "match_name": "Consuelo", "photo": "consuelo.png"},
    {"name": "Daniel", "match_name": "Daniel", "photo": "daniel.png"},
]


def load_employees_roster() -> list[dict]:
    """Every roster entry as {"name", "match_name", "photo"}, freshly
    read from _ROSTER_FILE (see module comment above for why this isn't
    cached). Seeds the file from _DEFAULT_EMPLOYEES on first run, or
    falls back to those same defaults (without persisting them) if the
    file exists but is somehow corrupt — either way this never raises,
    since a broken roster file shouldn't take down the whole app."""
    if not _ROSTER_FILE.exists():
        save_employees_roster(_DEFAULT_EMPLOYEES)
        return [dict(e) for e in _DEFAULT_EMPLOYEES]
    try:
        return json.loads(_ROSTER_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [dict(e) for e in _DEFAULT_EMPLOYEES]


def save_employees_roster(roster: list[dict]) -> None:
    _EMPLOYEES_DIR.mkdir(parents=True, exist_ok=True)
    _ROSTER_FILE.write_text(
        json.dumps(roster, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _slugify_employee_name(name: str) -> str:
    """Filesystem-safe base filename for a new employee's photo, e.g.
    "Juan Pérez" -> "juan_perez" — used only when adding someone new or
    replacing their photo (see _save_employee_photo); existing entries
    keep whatever filename they already have in the roster JSON."""
    import unicodedata

    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = "".join(ch if ch.isalnum() else "_" for ch in normalized.lower())
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_") or "empleado"


# Stored (saved-to-disk) avatar size — deliberately bigger than any size
# actually displayed on screen (_circular_avatar_pixmap's callers all
# pass 28-40px) so a photo still looks sharp if a bigger display size is
# ever used later; downscaling for display happens on the fly from this.
_AVATAR_EXPORT_SIZE = 512


def _save_employee_photo(source_path: str, name: str, existing_photo: str | None = None) -> str:
    """Turn whatever image a QFileDialog handed back at `source_path`
    into a square-cropped, fixed-size PNG saved into _EMPLOYEES_DIR, and
    return the filename to store in the roster JSON's "photo" field.
    Pure Qt (QImage crop + scale + save) — no Pillow, which isn't a
    dependency of this app anywhere else.

    If `existing_photo` is given (replacing an existing employee's
    picture), the SAME filename is reused, so the roster entry's
    "photo" field never has to change and every other already-open page
    referencing that filename picks up the new picture next time it
    repaints. Otherwise a fresh filename is derived from `name` (via
    _slugify_employee_name), with a numeric suffix appended if that slug
    is already taken by a different employee's photo file.

    Raises ValueError if the file isn't a loadable image or can't be
    saved (e.g. a permissions problem) — callers show that to the user
    rather than silently failing."""
    image = QImage(source_path)
    if image.isNull():
        raise ValueError(f"No se pudo abrir la imagen: {source_path}")

    side = min(image.width(), image.height())
    x = (image.width() - side) // 2
    y = (image.height() - side) // 2
    cropped = image.copy(x, y, side, side)
    scaled = cropped.scaled(
        _AVATAR_EXPORT_SIZE,
        _AVATAR_EXPORT_SIZE,
        Qt.KeepAspectRatio,
        Qt.SmoothTransformation,
    )

    _EMPLOYEES_DIR.mkdir(parents=True, exist_ok=True)
    if existing_photo:
        filename = existing_photo
    else:
        base = _slugify_employee_name(name)
        taken_filenames = {employee["photo"] for employee in load_employees_roster()}
        filename = f"{base}.png"
        suffix = 2
        while filename in taken_filenames:
            filename = f"{base}_{suffix}.png"
            suffix += 1

    if not scaled.save(str(_EMPLOYEES_DIR / filename), "PNG"):
        raise ValueError(f"No se pudo guardar la foto: {filename}")

    # A replaced photo keeps its old filename (see above), so the
    # circular-avatar cache below (keyed by "filename:size") would
    # otherwise go on serving the OLD picture under that same key
    # forever — drop every cached size for this filename so the next
    # repaint re-decodes the file that was just written.
    for key in [k for k in _avatar_pixmap_cache if k.startswith(f"{filename}:")]:
        del _avatar_pixmap_cache[key]

    return filename


_avatar_pixmap_cache: dict[str, QPixmap] = {}


def _circular_mask_pixmap(source: QPixmap, size: int) -> QPixmap:
    """A `size`x`size` circularly-masked copy of an already-loaded
    QPixmap — the actual crop+mask logic shared by _circular_avatar_
    pixmap (cached, disk-backed roster photos) and the Configuración
    add/edit dialog's live preview of a photo the user just picked but
    hasn't saved into the roster yet (nothing to cache there, it's a
    different image every time the dialog opens)."""
    if source.isNull():
        result = QPixmap(size, size)
        result.fill(Qt.transparent)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor("#dbe3ea"))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(0, 0, size, size)
        painter.end()
        return result

    source = source.scaled(
        size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
    )
    # KeepAspectRatioByExpanding can leave one dimension slightly larger
    # than `size` — center-crop back down to an exact square before
    # masking, or the mask circle would clip unevenly.
    if source.width() != size or source.height() != size:
        x = max(0, (source.width() - size) // 2)
        y = max(0, (source.height() - size) // 2)
        source = source.copy(x, y, size, size)

    result = QPixmap(size, size)
    result.fill(Qt.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.Antialiasing)
    clip_path = QPainterPath()
    clip_path.addEllipse(0, 0, size, size)
    painter.setClipPath(clip_path)
    painter.drawPixmap(0, 0, source)
    painter.end()
    return result


def _circular_avatar_pixmap(photo_filename: str, size: int = 32) -> QPixmap:
    """A `size`x`size` circularly-masked QPixmap for one roster photo —
    cached by (filename, size) since this gets called for the same
    handful of employees over and over (every project card load, every
    row of the edit checklist), and re-decoding + re-masking the same
    PNG each time would be wasteful for no benefit (the roster photos
    only change when Configuración explicitly saves a new one, which
    busts this cache itself — see _save_employee_photo)."""
    cache_key = f"{photo_filename}:{size}"
    cached = _avatar_pixmap_cache.get(cache_key)
    if cached is not None:
        return cached

    source = QPixmap(str(_EMPLOYEES_DIR / photo_filename))
    result = _circular_mask_pixmap(source, size)
    _avatar_pixmap_cache[cache_key] = result
    return result


def _row_has_employee(employee_list_text: str | None, match_name: str) -> bool:
    """Whether a roster entry (identified by its short `match_name`,
    e.g. "Fernando" or "Juan Carlos") shows up in this project's
    free-text employee_list cell — matched on WHOLE words (see _words),
    same reasoning as the company search fix elsewhere in this file: a
    plain substring check would let e.g. "Ana" match inside "Mariana".
    For a two-word match_name ("Juan Carlos") every one of its words
    has to be present, not just one of them."""
    text_words = _words(employee_list_text or "")
    match_words = _words(match_name)
    return bool(match_words) and match_words.issubset(text_words)


def _build_employee_avatar_row(employee_list_text: str | None) -> QWidget:
    """Read-only row of small circular avatar icons — one per roster
    employee whose match-name is found in `employee_list_text` (see
    _row_has_employee) — replacing what used to be the raw
    employee_list text on the project card. Hovering an icon shows that
    person's full name as a tooltip. Returns a widget containing a
    single muted placeholder label instead when nobody matches, so the
    card still shows SOMETHING in that slot (same "Sin datos todavía"
    wording the old plain-text version used).

    Reads load_employees_roster() fresh (not a frozen list) so a
    project opened right after adding/renaming/deleting someone in
    Configuración already reflects that change, no restart needed."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)

    matched = [
        (employee["name"], employee["photo"]) for employee in load_employees_roster()
        if _row_has_employee(employee_list_text, employee["match_name"])
    ]
    if not matched:
        placeholder = QLabel("Sin datos todavía")
        placeholder.setObjectName("MutedText")
        layout.addWidget(placeholder)
        layout.addStretch()
        return row

    for name, photo in matched:
        avatar = QLabel()
        avatar.setPixmap(_circular_avatar_pixmap(photo, 32))
        avatar.setFixedSize(32, 32)
        avatar.setToolTip(name)
        layout.addWidget(avatar)
    layout.addStretch()
    return row


def _build_employee_checklist() -> tuple[QWidget, dict[str, QCheckBox]]:
    """One QCheckBox per current roster entry (see load_employees_
    roster — always the LATEST saved roster, not a frozen snapshot),
    each showing that person's avatar as its icon plus their full name
    as its label — the tick/untick control used in both
    ProyectosInfoDetailPage's employees panel and ProyectosInfoNewPage,
    replacing the old free-text "Lista de empleados" box in both
    places. Returns the container widget plus a {full_name: checkbox}
    dict so the caller can read/set which boxes are ticked (see
    _row_has_employee for how a saved employee_list string maps back to
    which boxes should start ticked)."""
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)

    checkboxes: dict[str, QCheckBox] = {}
    for employee in load_employees_roster():
        checkbox = QCheckBox(employee["name"])
        checkbox.setIcon(QIcon(_circular_avatar_pixmap(employee["photo"], 28)))
        checkbox.setIconSize(QSize(28, 28))
        checkbox.setCursor(Qt.PointingHandCursor)
        layout.addWidget(checkbox)
        checkboxes[employee["name"]] = checkbox
    return container, checkboxes


def _employee_list_text_from_checkboxes(checkboxes: dict[str, QCheckBox]) -> str | None:
    """The comma-separated employee_list string to save, built from
    whichever checkboxes are currently ticked — full names, e.g.
    "Guillermo Gea Marco, Daniel", not just first names, so the saved
    text is unambiguous even though _row_has_employee only needs the
    short first-name form to re-recognize it later."""
    ticked = [name for name in checkboxes if checkboxes[name].isChecked()]
    return ", ".join(ticked) or None


def _set_checkboxes_from_employee_list_text(
    checkboxes: dict[str, QCheckBox], employee_list_text: str | None
) -> None:
    """Ticks exactly the checkboxes whose roster entry matches
    `employee_list_text` (see _row_has_employee) — used to initialize
    the checklist from a project's already-saved (or hand-typed legacy)
    employee_list value when entering edit mode / opening the new-
    project form."""
    for employee in load_employees_roster():
        checkbox = checkboxes.get(employee["name"])
        if checkbox is not None:
            checkbox.setChecked(_row_has_employee(employee_list_text, employee["match_name"]))

# Fields shown in the "Editar" form — a fixed, narrower list than
# PROJECT_INFO_FIELDS above, matching the columns the real 2024/2025/
# 2026 files actually have today (see reader.py's FIELD_MAP). Unlike the
# read-only card, this doesn't hide/show fields per row — every field is
# always present in the form, blank if the row has nothing there yet.
EDIT_TEXT_FIELDS = [
    ("Número de proyecto", "project_number"),
    ("Nombre", "project_name"),
    ("Trabajos a realizar", "work_description"),
    ("Título entero del proyecto", "full_title"),
    ("Ciudad", "city"),
    ("Provincia", "province"),
    ("Fecha del proyecto (dd/mm/aaaa)", "start_date"),
    ("Promotor", "client"),
]
EDIT_MULTILINE_FIELDS = [
    # "Lista de empleados" used to be here as a free-text box — it's now
    # a dedicated tick/untick checklist (see _build_employee_checklist),
    # built and wired into the edit forms separately, not through this
    # generic per-field loop.
]
# The 4 columns constrained to a fixed set of categories in the source
# workbook (see reader.CATEGORY_OPTIONS) — rendered as radio buttons
# instead of free text, so only a valid category can ever be saved.
EDIT_CATEGORY_FIELDS = [
    ("Planning", "status"),
    ("Tipo", "work_type"),
    ("Subtipo1", "subtipo1"),
    ("Subtipo2", "subtipo2"),
]
_SIN_ESPECIFICAR = "(Sin especificar)"

# Colored pill for the PLANNING column — shown as a badge next to each
# project (in the list AND on its detail page), not as a plain text row
# among the other fields, since it's the one status every single project
# in the 2024-2026 data actually has filled in (confirmed by hand).
_PLANNING_PILL_STYLES = {
    "Acabado": "SuccessPill",
    "Cancelado": "ErrorPill",
    "Proceso": "WarningPill",
    "DO": "NeutralPill",
    "Inicio Proyecto": "InfoPill",
}


def _planning_pill_style(status: str | None) -> str:
    return _PLANNING_PILL_STYLES.get((status or "").strip(), "NeutralPill")


def _make_status_pill(status: str | None) -> QLabel:
    # About 105 of the 366 2024-2026 rows actually have NO PLANNING
    # value (mostly the "request received, not catalogued yet" first
    # row for a project — see reader.py's docstring for a real example)
    # — shown as a neutral "Sin estado" pill rather than a blank gap, so
    # every row in the list looks consistent either way.
    pill = QLabel(status or "Sin estado")
    pill.setObjectName(_planning_pill_style(status) if status else "NeutralPill")
    pill.setAlignment(Qt.AlignCenter)
    return pill


# Plain-text colors matching each pill's own text color in styles.py
# (QLabel#SuccessPill/WarningPill/etc. { color: ... }) — used ONLY for
# ProyectosInfoPage's tree, which can have up to one row-item per EVERY
# project (thousands, not the bounded ~30-per-company a real pill widget
# is fine for on ProyectosInfoCompanyPage). setItemWidget()-ing a real
# QLabel per row for that many items measured fine in isolation but
# still caused a real, user-reported freeze while filtering — Qt's
# QTreeWidget keeps every registered item-widget "live" for as long as
# its item exists, hidden or not, and operations that touch the
# model/view (resizeColumnToContents, setHidden, etc.) apparently have
# to account for all of them, not just the visible ones. Plain colored
# TEXT via setForeground has no such per-widget bookkeeping — same
# color coding, none of the cost.
_PLANNING_TEXT_COLORS = {
    "Acabado": "#087443",
    "Cancelado": "#b42318",
    "Proceso": "#9a5300",
    "DO": "#3a4b5c",
    "Inicio Proyecto": "#1d4ed8",
}


def _set_status_text(item: QTreeWidgetItem, column: int, status: str | None) -> None:
    item.setText(column, status or "Sin estado")
    color = _PLANNING_TEXT_COLORS.get((status or "").strip(), "#3a4b5c")
    item.setForeground(column, QColor(color))
    bold_font = item.font(column)
    bold_font.setBold(True)
    item.setFont(column, bold_font)


_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _words(text: str) -> set[str]:
    """Lowercased whole-word tokens in `text` — used by ProyectosInfoPage
    's search box to match a typed word against a whole word in the
    data, NOT as a substring anywhere. A plain substring check would
    let e.g. "forn" match inside "FORNES" (a surname, nothing to do
    with "Forn" the street) — real false positive seen with the search
    "c del forn": "forn" is a substring of "TONI FORNES" in an
    unrelated company's title, but not a whole word there."""
    return set(_WORD_RE.findall(text.lower()))


class ProyectosInfoPage(QWidget):
    """Year -> company hierarchy read from the hand-maintained 'LISTADO
    PROYECTOS POR AÑOS' workbook (project_info_path in config.yaml) — a
    completely separate data source from the crawled P: index the rest
    of the app uses. Whichever years reader.YEARS_WITH_DATA lists (2008
    through 2026, as of the last split from the master workbook) are the
    only ones that can ever appear here.

    Every row under a year is a company node (row["company"] — the text
    before the first "-" in NOMBRE, or the whole name if there's none),
    always, even a company with only one row — there's no more "skip the
    wrapper for a singleton" special case, so the list here is uniform:
    year -> company -> (a separate page). Double-clicking a company node
    doesn't expand it in place; it navigates to ProyectosInfoCompanyPage,
    which lists that company's rows for that year and is where
    double-clicking an individual row opens its own detail card
    (ProyectosInfoDetailPage).

    EXCEPTION to "no in-place expansion": while a search/category filter
    is active and it narrows a company down to specific matching row(s)
    (see _row_fully_matches), those matching rows themselves ARE shown
    as children under the company node, auto-expanded — so e.g.
    searching "san ramon" shows the "SAN RAMON" row right there under
    PLENERGY, not just a "(1 de 33)" badge the user still has to open
    the company page and hunt through 33 rows to find. Double-clicking
    one of these child rows opens its detail card directly
    (project_info_opened), skipping the company page entirely. When no
    filter is active, or when a match can't be traced to a single row
    (e.g. "urb torrent", see _company_matches_text), no children are
    shown and double-clicking the company still opens
    ProyectosInfoCompanyPage as before."""

    company_opened = Signal(int, str)  # (year, company)
    project_info_opened = Signal(int)  # a matched row's own id, opened directly
    new_project_requested = Signal()
    sync_trabajos_requested = Signal()

    _ALL_YEARS_LABEL = "Todos los años"
    _ALL_CATEGORY_LABEL = "Todos"

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 22, 26, 26)
        outer.setSpacing(14)

        heading_row = QHBoxLayout()
        heading = QLabel("Proyectos Info")
        heading.setObjectName("SectionTitle")
        heading_row.addWidget(heading)
        heading_row.addStretch()
        # Distinct from the topbar's "Actualizar" button (which rebuilds
        # the crawled P: index, db_path) — this one scans TRABAJOS
        # <año actual> on P: for job/site folders that don't have a
        # matching NOMBRE row yet in the current year's xlsx, and appends
        # one for each (see src/project_info/sync_trabajos.py). Never
        # touches the crawled index at all.
        self.sync_trabajos_button = QPushButton("⟳  Sincronizar carpetas nuevas")
        self.sync_trabajos_button.setObjectName("SecondaryButton")
        self.sync_trabajos_button.setCursor(Qt.PointingHandCursor)
        self.sync_trabajos_button.clicked.connect(self.sync_trabajos_requested.emit)
        heading_row.addWidget(self.sync_trabajos_button)
        self.new_project_button = QPushButton("+  Nuevo proyecto")
        self.new_project_button.setObjectName("PrimaryButton")
        self.new_project_button.setCursor(Qt.PointingHandCursor)
        self.new_project_button.clicked.connect(self.new_project_requested.emit)
        heading_row.addWidget(self.new_project_button)
        outer.addLayout(heading_row)

        subtitle = QLabel(
            f"Datos del archivo Excel de proyectos ({YEARS_WITH_DATA[0]}-{YEARS_WITH_DATA[-1]}), "
            "agrupados por empresa/cliente. Haz doble clic en una empresa para ver "
            "sus proyectos. La búsqueda también encuentra texto dentro de los "
            "proyectos de cada empresa (nombre y título), no solo en el nombre de "
            "la empresa."
        )
        subtitle.setObjectName("MutedText")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(10)
        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText(
            "Filtrar por empresa, nombre de proyecto o título..."
        )
        self.filter_box.textChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self.filter_box, 1)

        self.year_filter = QComboBox()
        self.year_filter.addItem(self._ALL_YEARS_LABEL)
        self.year_filter.addItems(list(reversed(YEARS_WITH_DATA)))  # newest first, matches the tree
        self.year_filter.setMinimumWidth(130)
        self.year_filter.currentIndexChanged.connect(self._apply_filter)
        filter_row.addWidget(self.year_filter)

        # Collapsed by default — clicking this reveals the panel below
        # with the 4 category dropdowns, rather than always taking up a
        # whole row. Its own label shows how many of those are actually
        # active (see _apply_filter), so the count is visible even while
        # the panel itself is collapsed.
        self.filters_toggle = QPushButton("▾  Filtros")
        self.filters_toggle.setObjectName("SecondaryButton")
        self.filters_toggle.setCursor(Qt.PointingHandCursor)
        self.filters_toggle.setCheckable(True)
        self.filters_toggle.toggled.connect(self._on_filters_toggle)
        filter_row.addWidget(self.filters_toggle)
        outer.addLayout(filter_row)

        # The same 4 fixed-category fields as the detail page's tick
        # boxes (Estado/Tipo/Subtipo1/Subtipo2), as a filter here
        # instead — a company matches a given category filter if ANY of
        # its rows has that value (see _apply_filter), since the
        # company itself doesn't have a single value for these.
        self.category_panel = QWidget()
        category_row = QHBoxLayout(self.category_panel)
        category_row.setContentsMargins(0, 0, 0, 0)
        category_row.setSpacing(10)
        self._category_filters: dict[str, QComboBox] = {}
        for key, spanish_label in (
            ("status", "Estado"),
            ("work_type", "Tipo"),
            ("subtipo1", "Subtipo 1"),
            ("subtipo2", "Subtipo 2"),
        ):
            field_label = QLabel(f"{spanish_label}:")
            field_label.setObjectName("MutedText")
            category_row.addWidget(field_label)
            combo = QComboBox()
            combo.addItem(self._ALL_CATEGORY_LABEL)
            combo.addItems(CATEGORY_OPTIONS[key])
            combo.setMinimumWidth(150)
            combo.currentIndexChanged.connect(self._apply_filter)
            self._category_filters[key] = combo
            category_row.addWidget(combo)

        # Employee filter — a plain dropdown of names (see
        # _rebuild_employee_filter_options), same "Todos" + one option
        # per value shape as the 4 category filters above, but backed
        # by the live roster (load_employees_roster) instead of a fixed
        # CATEGORY_OPTIONS list, and matched against a row's free-text
        # employee_list cell via _row_has_employee rather than an exact
        # field comparison — see _row_matches_active_filters.
        employee_label = QLabel("Empleado:")
        employee_label.setObjectName("MutedText")
        category_row.addWidget(employee_label)
        self.employee_filter = QComboBox()
        self.employee_filter.setMinimumWidth(170)
        self.employee_filter.currentIndexChanged.connect(self._apply_filter)
        self._employee_filter_match_names: dict[str, str] = {}
        self._rebuild_employee_filter_options()
        category_row.addWidget(self.employee_filter)

        category_row.addStretch()
        self.category_panel.setVisible(False)
        outer.addWidget(self.category_panel)

        # Filtering just show/hides existing rows (see _apply_filter) —
        # cheap — but debounced anyway so a fast typist doesn't trigger
        # it on every single keystroke.
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(150)
        self._filter_timer.timeout.connect(self._apply_filter)

        self.count_label = QLabel("")
        self.count_label.setObjectName("MutedText")
        outer.addWidget(self.count_label)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Estado", "Proyecto", "Título"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(True)
        # Estado first (small fixed width for its colored pill), then
        # Proyecto and Título — both full NOMBRE/TITULO ENTERO DEL
        # PROYECTO values, not truncated to the visible width, same idea
        # as Ruta/Carpeta elsewhere in the app: let them grow to fit
        # their full text (see resizeColumnToContents calls in _render)
        # and rely on the tree's own horizontal scrollbar past the
        # window edge.
        self.tree.header().setStretchLastSection(False)
        # Wide enough for the longest status text ("Sin estado") plus
        # room for the branch/expand indentation of a matched ROW child
        # (see _build_tree/_apply_filter), which sits two levels deep
        # (year -> company -> row) — without this extra room, that
        # indentation ate into column 0's usable width and clipped the
        # text against the column edge.
        self.tree.setColumnWidth(0, 130 + 2 * self.tree.indentation())
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        outer.addWidget(self.tree, 1)

        self._rows: list[dict] = []
        self._error: str | None = None
        # id(company_item) for every company that CURRENTLY has one or
        # more of its row children revealed (see _apply_filter) — lets
        # a filter change that reveals nothing new skip re-hiding rows
        # for the (usually large) majority of companies that never had
        # any shown in the first place, instead of looping over every
        # row of every company on every keystroke regardless of whether
        # there's anything to actually undo.
        self._companies_with_revealed_rows: set[int] = set()
        # id(row) -> frozenset of that row's own project_name/full_title
        # words (see _row_words) — a row's text never changes once
        # loaded, but _company_matches_text/_row_fully_matches used to
        # re-tokenize it with a fresh regex pass on EVERY keystroke for
        # EVERY row of EVERY company being checked (~7,800 calls for a
        # single broad search over the full dataset) — a real, measured
        # chunk of _apply_filter's cost. Computing each row's word set
        # once and reusing it removes that repeat work entirely.
        self._row_words_cache: dict[int, frozenset[str]] = {}

    def _rebuild_employee_filter_options(self) -> None:
        """Repopulates the employee filter dropdown from the CURRENT
        roster (load_employees_roster re-reads roster.json fresh every
        call) — called once in __init__ and again on every refresh()
        (every visit to this page), so someone added/renamed/removed
        via Configuración shows up here without restarting the app.
        Keeps the current selection if that exact name is still in the
        roster; otherwise resets to "Todos" rather than silently
        leaving a filter selected for a name that no longer exists."""
        previous_selection = self.employee_filter.currentText()
        self.employee_filter.blockSignals(True)
        self.employee_filter.clear()
        self.employee_filter.addItem(self._ALL_CATEGORY_LABEL)
        self._employee_filter_match_names = {}
        for employee in load_employees_roster():
            self.employee_filter.addItem(employee["name"])
            self._employee_filter_match_names[employee["name"]] = employee["match_name"]
        index = self.employee_filter.findText(previous_selection)
        self.employee_filter.setCurrentIndex(index if index >= 0 else 0)
        self.employee_filter.blockSignals(False)

    def refresh(self) -> None:
        """Called every time this page is navigated to — NOT just when
        the underlying data actually changed. data_service.
        project_info_rows() returns the exact same list object (not just
        an equal one) when nothing's changed since the last read, so
        comparing by identity (`is`) below skips rebuilding ~370+ tree
        items and status pills on every single visit to this page, only
        doing it when there's something new to show."""
        # Independent of the row-data caching below — the roster can
        # change (via Configuración) even when project_info_rows()
        # hasn't, so this always re-reads it fresh on every visit.
        self._rebuild_employee_filter_options()

        try:
            rows = data_service.project_info_rows()
            error = None
        except Exception as exc:
            rows = []
            error = str(exc)

        if rows is self._rows and error == self._error:
            return

        self._rows = rows
        self._error = error
        self._build_tree()

    def _build_tree(self) -> None:
        """Full (re)build of every year/project/pill in the tree,
        ignoring the filter box — only called from refresh() when the
        row data actually changed. Filtering itself is handled
        separately by _apply_filter(), which just shows/hides these
        already-built items instead of recreating them, since that's by
        far the more frequent operation (every keystroke).

        Every child item is built and parented to its year_item BEFORE
        that year_item is attached to the tree (via the QTreeWidgetItem
        (parent, values) constructor form, then one single
        addTopLevelItems() call at the end) — NOT year_item.addChild()
        in a loop while year_item is already live in the tree. That
        distinction is the entire reason this used to take ~3 seconds
        for only ~370 rows: adding children one at a time to a parent
        that's already part of the on-screen tree makes Qt redo its
        internal layout bookkeeping on every single insertion, which
        gets quadratically worse as the list grows. Building the whole
        subtree off-tree first and attaching it in one shot avoids that
        entirely — same end result, a small fraction of the time.

        Within each year, rows are grouped by "company" — reader.py's
        row["company"], the text before the first "-" in NOMBRE (or the
        whole name if there's no "-"). Every company becomes exactly one
        leaf item under its year, shown as "COMPANY (n)" — always, even
        when n is 1, so the list is uniform regardless of how many rows
        a company has. There's no in-place expansion any more: each
        company item carries (year, company) via setData, and double-
        clicking it navigates to ProyectosInfoCompanyPage instead (see
        _on_item_double_clicked / company_opened).

        EACH company's individual rows are ALSO built here as children
        of their company item — one per row, hidden by default — rather
        than being created on demand inside _apply_filter. A search or
        category filter that narrows a company down to specific matching
        row(s) reveals the matching ones via setHidden(False) (see
        _apply_filter), it doesn't construct them. This matters a lot
        for performance: this method only runs once per data refresh, so
        building every row item here (parented to its company item
        BEFORE that company item's own year_item is attached to the
        tree, same off-tree-then-attach-once trick as above) is a
        one-time cost. Building/destroying row items on every keystroke
        instead — which is what an earlier version of this did — was
        measured taking 3.6+ seconds for a single broad search (e.g. the
        letter "a", which narrows hundreds of companies at once): even
        batched via addChildren(), inserting into a company item that's
        already live inside an on-screen tree with ~1100+ existing items
        forces Qt to redo bookkeeping across the WHOLE tree on every
        single insertion call, not just the new item(s) — the same
        "quadratic against a live tree" trap called out above, just one
        level deeper. Pre-building once and only toggling .setHidden()
        afterwards is what actually fixes it."""
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        self._companies_with_revealed_rows.clear()
        self._row_words_cache.clear()

        if self._error:
            self.count_label.setText(self._error)
            self.tree.setUpdatesEnabled(True)
            return

        by_year: dict[int, dict[str, list[dict]]] = {}
        for row in self._rows:
            company = row.get("company") or row["project_name"] or ""
            by_year.setdefault(row["year"], {}).setdefault(company, []).append(row)

        year_items = []
        for year in sorted(by_year.keys(), reverse=True):
            year_item = QTreeWidgetItem([str(year)])
            bold_font = year_item.font(0)
            bold_font.setBold(True)
            year_item.setFont(0, bold_font)
            # NOT re-sorted — companies appear in the order their first
            # row shows up in the source spreadsheet (by_year[year] is
            # built by iterating self._rows in read order, and dicts
            # preserve insertion order).
            for company, company_rows in by_year[year].items():
                # Column 0 (Estado) is intentionally blank here — a
                # company can span several different statuses, so no
                # single pill would be accurate; per-row status is shown
                # on the company page instead. Column 1's text also
                # doubles as what _apply_filter() matches against.
                company_item = QTreeWidgetItem(
                    year_item, ["", f"{company} ({len(company_rows)})", ""]
                )
                company_item.setData(0, Qt.UserRole, (year, company))
                # The underlying rows themselves, stored on a different
                # (row, column) slot so it doesn't clash with the tuple
                # above — _apply_filter() searches into these (project
                # name, title) and checks them against the category
                # filters, not just the "COMPANY (n)" label text.
                company_item.setData(1, Qt.UserRole, company_rows)

                for row in company_rows:
                    row_item = QTreeWidgetItem(
                        company_item, ["", row.get("project_name") or "", row.get("full_title") or ""]
                    )
                    row_item.setData(0, Qt.UserRole + 1, row["id"])
                    row_item.setHidden(True)
                    # Plain colored TEXT (see _set_status_text), NOT a
                    # setItemWidget() pill — this loop can run for every
                    # single project (thousands), and a real embedded
                    # QLabel per row at that scale is what caused the
                    # freeze-while-filtering reported after an earlier
                    # version of this (see _set_status_text's docstring).
                    _set_status_text(row_item, 0, row.get("status"))
            year_items.append(year_item)

        self.tree.addTopLevelItems(year_items)
        for year_item in year_items:
            year_item.setExpanded(True)
            # setItemWidget needs the item already attached to the tree
            # (hence only done here, after addTopLevelItems above, not
            # while year_items was still being built off-tree) — but
            # only ~19 of these (one per year, see YEARS_WITH_DATA), not
            # per company/row, so none of the "embedded widget at scale"
            # performance concerns from the rest of this method apply.
            year = int(year_item.text(0))
            open_button = QPushButton("📗  Abrir Excel")
            open_button.setObjectName("LinkButton")
            open_button.setCursor(Qt.PointingHandCursor)
            open_button.clicked.connect(lambda checked=False, y=year: self._open_year_excel(y))
            self.tree.setItemWidget(year_item, 1, open_button)

        self.tree.resizeColumnToContents(1)
        self.tree.resizeColumnToContents(2)
        self.tree.setUpdatesEnabled(True)
        self._apply_filter()

    def _open_year_excel(self, year: int) -> None:
        try:
            path = data_service.project_info_file_path(year)
        except FileNotFoundError as exc:
            QMessageBox.warning(self, "Archivo no encontrado", str(exc))
            return
        open_local_path(self, path, f"el archivo {year}.xlsx")

    def _on_filter_changed(self) -> None:
        self._filter_timer.start()  # restarts the 150ms countdown on every keystroke

    def _on_filters_toggle(self, checked: bool) -> None:
        self.category_panel.setVisible(checked)
        self._refresh_filters_toggle_text()

    def _refresh_filters_toggle_text(self) -> None:
        """Keeps the "Filtros" button's own label showing how many of
        the 4 category filters are actually active, so that count is
        visible even while the panel itself is collapsed — called both
        when the panel is opened/closed and whenever a category combo
        changes (see _apply_filter)."""
        active = sum(
            1 for combo in self._category_filters.values()
            if combo.currentText() != self._ALL_CATEGORY_LABEL
        )
        if self.employee_filter.currentText() != self._ALL_CATEGORY_LABEL:
            active += 1
        arrow = "▴" if self.filters_toggle.isChecked() else "▾"
        label = f"Filtros ({active})" if active else "Filtros"
        self.filters_toggle.setText(f"{arrow}  {label}")

    def _company_matches_text(
        self, label_text: str, company_rows: list[dict], filter_words: list[str]
    ) -> bool:
        """Whether EVERY word typed in the search box is an EXACT whole
        word SOMEWHERE for this company — its own "COMPANY (n)" label,
        or any of its rows' project name/title — not necessarily all in
        the same place, and not just a substring buried inside a longer,
        unrelated word.

        Two things this has to get right at once, from two real
        examples:
          - "urb torrent" must find "AYTO TORRENT - URB-VENTETA Y
            PANTA" even though "torrent" only comes from the company
            label and "urb" only from inside a row's own project name
            — the literal phrase "urb torrent" never appears together
            anywhere (the real text is "TORRENT - URB-VENTETA",
            reversed, with punctuation in between). That's why this
            checks each word independently against one combined
            haystack instead of one single phrase/substring match.
          - "c del forn" must NOT match a company just because "forn"
            happens to be a substring of "FORNES" (a surname) in some
            unrelated row's title — a plain substring check did exactly
            that. Matching against WHOLE words (via _words()) instead
            of "is this text contained anywhere" fixes it: "fornes" is
            a whole word, "forn" isn't equal to it, so it no longer
            matches."""
        haystack_words = _words(label_text)
        for row in company_rows:
            haystack_words |= self._row_words(row)
        return all(word in haystack_words for word in filter_words)

    def _row_words(self, row: dict) -> frozenset[str]:
        """This row's own project_name/full_title words, tokenized once
        and cached by the row's own stable "id" field — see
        _row_words_cache in __init__ for why this matters (re-tokenizing
        every row on every keystroke was a real, measured chunk of
        _apply_filter's cost).

        Keyed by row["id"], NOT Python's id(row): `row` here almost
        always comes from company_item.data(1, Qt.UserRole), and PySide6
        does NOT preserve object identity for a plain Python list-of-
        dicts round-tripped through QTreeWidgetItem.setData()/.data() —
        every single .data() call reconstructs a BRAND NEW list of
        brand new dict objects with different id()s (confirmed directly:
        item.data(...) is item.data(...) is False). Caching by id(row)
        therefore almost never actually hit the SAME row twice — worse,
        because those throwaway dicts get garbage collected immediately,
        Python was free to reuse their memory addresses for the NEXT
        unrelated temporary dict, so a cache "hit" could silently return
        a completely different row's words. That was a real, confirmed
        correctness bug (searching "a" matched a different, wrong number
        of companies on every run) caught while chasing this
        performance fix — row["id"] is a plain int copied by value, so
        it survives that round-trip correctly."""
        key = row["id"]
        cached = self._row_words_cache.get(key)
        if cached is None:
            cached = frozenset(
                _words(row.get("project_name") or "") | _words(row.get("full_title") or "")
            )
            self._row_words_cache[key] = cached
        return cached

    def _row_matches_active_filters(self, row: dict) -> bool:
        """Whether this one row satisfies every ACTIVE filter at once —
        the 4 fixed categories (Estado/Tipo/Subtipo1/Subtipo2), each an
        exact-value match, PLUS the employee filter, matched the same
        way "Lista de empleados" is read everywhere else (whole-word
        match_name lookup via _row_has_employee, not an exact string
        comparison — a row's employee_list is free text). A filter left
        on "Todos" is skipped entirely, not treated as "must be
        blank"."""
        for key, combo in self._category_filters.items():
            selected = combo.currentText()
            if selected != self._ALL_CATEGORY_LABEL and (row.get(key) or "") != selected:
                return False
        selected_employee = self.employee_filter.currentText()
        if selected_employee != self._ALL_CATEGORY_LABEL:
            match_name = self._employee_filter_match_names.get(selected_employee)
            if match_name is None or not _row_has_employee(row.get("employee_list"), match_name):
                return False
        return True

    def _row_fully_matches(self, row: dict, filter_words: list[str]) -> bool:
        """Whether THIS one row's own project name/title — not the
        company label, not any of its sibling rows — contains every
        filter word. Used only to count how many of a company's rows
        specifically matched (see _apply_filter's "(n de total)" label),
        which is well-defined for the common case (e.g. "san ramon"
        landing entirely inside one row's own name) but NOT for a match
        that only exists by combining the company label with a
        DIFFERENT row (e.g. "urb torrent", see _company_matches_text) —
        there's no single row to point to in that case, so this
        correctly returns False for every row and the caller falls back
        to showing the plain total instead of a possibly-misleading
        count."""
        if not filter_words:
            return True
        return all(word in self._row_words(row) for word in filter_words)

    def _apply_filter(self) -> None:
        """Shows/hides the tree's EXISTING items to match the search
        box, year dropdown, and the 4 category filters — no items are
        created or destroyed here (see _build_tree, which now
        pre-builds every row item too, hidden by default), which is
        what keeps this fast enough to run on every keystroke (after
        the debounce) even with several thousand rows across 19 years —
        an earlier version of this DID build/destroy row items here on
        every keystroke, and that turned out to be a real, measured
        performance bug (3.6+ seconds for a single broad search) fixed
        by moving all item construction into _build_tree instead; see
        that method's docstring for the full story.

        A company is shown if year matches AND every word typed in the
        search box turns up somewhere for that company (see
        _company_matches_text) AND at least one of its rows satisfies
        every active category filter at once. The text condition and
        the category condition are checked independently — they don't
        have to be satisfied by the exact same row — which keeps this
        simple while still being right for the overwhelming majority of
        real searches (a company rarely has such wildly different rows
        that this loose combination would be misleading)."""
        self.tree.setUpdatesEnabled(False)
        # Tokenized the same way as the haystack in _company_matches_text
        # (via _words()) — not a raw .split() — so a stray "/" or "-" in
        # what's typed (e.g. "c/ del forn") doesn't stop a word from
        # being recognized as the same token it'd be in the data.
        filter_words = list(_words(self.filter_box.text()))
        year_filter = self.year_filter.currentText()
        any_category_active = any(
            combo.currentText() != self._ALL_CATEGORY_LABEL
            for combo in self._category_filters.values()
        ) or self.employee_filter.currentText() != self._ALL_CATEGORY_LABEL
        total_visible = 0

        for i in range(self.tree.topLevelItemCount()):
            year_item = self.tree.topLevelItem(i)
            if year_filter != self._ALL_YEARS_LABEL and year_item.text(0) != year_filter:
                year_item.setHidden(True)
                continue
            year_visible_count = 0
            for j in range(year_item.childCount()):
                child = year_item.child(j)
                company_rows = child.data(1, Qt.UserRole) or []
                company = child.data(0, Qt.UserRole)[1]  # (year, company) tuple

                text_ok = not filter_words or self._company_matches_text(
                    company, company_rows, filter_words
                )
                category_ok = not any_category_active or any(
                    self._row_matches_active_filters(row) for row in company_rows
                )
                matches = text_ok and category_ok

                # Whichever of this company's own rows individually
                # satisfy BOTH active conditions at once — used both for
                # the "(n de total)" badge below AND to reveal those
                # exact rows' PRE-BUILT child items (see _build_tree),
                # so the user can jump straight to the matching project
                # instead of opening the company page and hunting for it
                # among all its rows. No row is revealed (all stay
                # hidden) when nothing's filtered, or when the match
                # doesn't trace back to any single row (see
                # _row_fully_matches) — company_rows[k] and
                # child.child(k) are always the same row, same order, so
                # no separate id lookup is needed here.
                company_key = id(child)
                any_row_visible = False
                if matches and (filter_words or any_category_active):
                    matched_count = 0
                    total = len(company_rows)
                    for k, row in enumerate(company_rows):
                        row_visible = self._row_fully_matches(row, filter_words) and self._row_matches_active_filters(row)
                        child.child(k).setHidden(not row_visible)
                        if row_visible:
                            matched_count += 1
                    any_row_visible = matched_count > 0
                    if any_row_visible:
                        self._companies_with_revealed_rows.add(company_key)
                    else:
                        self._companies_with_revealed_rows.discard(company_key)
                    if any_row_visible and matched_count < total:
                        child.setText(1, f"{company} ({matched_count} de {total})")
                    else:
                        child.setText(1, f"{company} ({total})")
                else:
                    if matches:
                        child.setText(1, f"{company} ({len(company_rows)})")
                    # Filter cleared, or this company no longer matches
                    # at all — make sure no previously-revealed row stays
                    # visible underneath it, but ONLY do that (bothering
                    # to touch every one of this company's row items) for
                    # a company that's actually in
                    # _companies_with_revealed_rows — i.e. actually had
                    # something to undo. The overwhelming majority of
                    # companies never reveal a row in the first place, so
                    # skipping them here is what keeps clearing/loosening
                    # a filter cheap instead of re-touching all ~2,400
                    # row items on every single keystroke regardless of
                    # whether anything's actually visible.
                    if company_key in self._companies_with_revealed_rows:
                        for k in range(child.childCount()):
                            child.child(k).setHidden(True)
                        self._companies_with_revealed_rows.discard(company_key)

                child.setExpanded(any_row_visible)
                child.setHidden(not matches)
                if matches:
                    year_visible_count += 1
            year_item.setHidden(year_visible_count == 0)
            total_visible += year_visible_count

        self.count_label.setText(
            self._error or f"{total_visible} empresa(s)"
        )
        # Labels can grow longer than the original "(n)" (e.g. "(1 de
        # 33)") — re-measure column 1 so that doesn't get clipped. Also
        # column 2: at the last full _build_tree(), every visible item
        # was a company row with a blank Título cell, so column 2 was
        # sized to little more than its header — a now-revealed matching
        # row can have a real, much longer Título that needs the column
        # to grow to show it.
        self.tree.resizeColumnToContents(1)
        self.tree.resizeColumnToContents(2)
        self.tree.setUpdatesEnabled(True)
        self._refresh_filters_toggle_text()

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        row_id = item.data(0, Qt.UserRole + 1)
        if row_id is not None:
            self.project_info_opened.emit(row_id)
            return
        data = item.data(0, Qt.UserRole)
        if data is not None:
            year, company = data
            self.company_opened.emit(year, company)


class ProyectosInfoCompanyPage(QWidget):
    """Flat list of every project row for one company within one year —
    the page reached by double-clicking a company on ProyectosInfoPage.
    Loaded fresh each time via load(year, company) rather than kept in
    sync automatically, same pattern as ProyectosInfoDetailPage. Even a
    company with a single row lands here first (per the explicit
    request that singletons behave the same as multi-row companies);
    double-clicking that one row still opens its own detail card."""

    project_info_opened = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 22, 26, 26)
        outer.setSpacing(14)

        top_bar = QHBoxLayout()
        self.back_button = QPushButton("←  Volver")
        self.back_button.setObjectName("LinkButton")
        self.back_button.setCursor(Qt.PointingHandCursor)
        top_bar.addWidget(self.back_button, alignment=Qt.AlignLeft)
        top_bar.addStretch()
        outer.addLayout(top_bar)

        self.heading = QLabel("")
        self.heading.setObjectName("SectionTitle")
        self.heading.setWordWrap(True)
        outer.addWidget(self.heading)

        self.count_label = QLabel("")
        self.count_label.setObjectName("MutedText")
        outer.addWidget(self.count_label)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Estado", "Proyecto", "Título"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(False)
        self.tree.header().setStretchLastSection(False)
        self.tree.setColumnWidth(0, 160)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        outer.addWidget(self.tree, 1)

        self._year: int | None = None
        self._company: str | None = None

    def load(self, year: int, company: str) -> None:
        self._year, self._company = year, company
        self.heading.setText(f"{company} — {year}")

        rows = [
            row for row in data_service.project_info_rows()
            if row["year"] == year and (row.get("company") or row["project_name"] or "") == company
        ]
        self.count_label.setText(f"{len(rows)} proyecto(s)")

        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        items = []
        # Original sheet order, same as everywhere else in this page —
        # not re-sorted.
        for row in rows:
            item = QTreeWidgetItem(["", row["project_name"] or "", row.get("full_title") or ""])
            item.setData(0, Qt.UserRole, row["id"])
            items.append((item, row.get("status")))
        self.tree.addTopLevelItems([item for item, _status in items])
        for item, status in items:
            self.tree.setItemWidget(item, 0, _make_status_pill(status))

        self.tree.resizeColumnToContents(1)
        self.tree.resizeColumnToContents(2)
        self.tree.setUpdatesEnabled(True)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        item_id = item.data(0, Qt.UserRole)
        if item_id is not None:
            self.project_info_opened.emit(item_id)


class ProyectosInfoDetailPage(QWidget):
    """Card-style detail view for one row from the project info workbook
    — a label/value grid, not a table. Most fields only appear when
    they actually have a value for this project (e.g. most rows have no
    TIPO); a blank one is left out of the card entirely rather than shown
    as empty, per the explicit request. "Lista de empleados" is the one
    exception — it's always shown first, even with no data, with a "Sin
    datos todavía" placeholder (see EMPLOYEE_LIST_FIELD above). The card
    is rebuilt from scratch on every load() since which fields are
    present varies row to row.

    The 4 category fields (Planning/Tipo/Subtipo1/Subtipo2 — each backed
    by a fixed set of options, see reader.CATEGORY_OPTIONS) live directly
    on this read view, in `categories_panel`, as always-interactive radio
    buttons — NOT behind the "Editar" button. Clicking one saves that
    single field immediately (see _on_category_toggled): no separate
    Guardar step for these four. The "Editar" button instead opens a
    separate, fixed-layout form (built once in __init__, not rebuilt per
    row) for the remaining fields — a plain text field per
    EDIT_TEXT_FIELDS entry plus a small multi-line box for "Lista de
    empleados" — where Guardar/Cancelar behave as a normal batch edit.
    Both paths go through data_service.update_project_info(), which
    writes straight back into that exact row's cells in the source xlsx
    (see writer.py) — nothing else in the file is touched."""

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 22, 26, 26)
        outer.setSpacing(14)

        top_bar = QHBoxLayout()
        self.back_button = QPushButton("←  Volver")
        self.back_button.setObjectName("LinkButton")
        self.back_button.setCursor(Qt.PointingHandCursor)
        top_bar.addWidget(self.back_button, alignment=Qt.AlignLeft)
        top_bar.addStretch()
        # Opens the exact {year}.xlsx this row's data is read from AND
        # written back into (see writer.py) — the year comes from
        # self._data["year"], set fresh on every load().
        self.open_excel_button = QPushButton("📗  Abrir Excel")
        self.open_excel_button.setObjectName("SecondaryButton")
        self.open_excel_button.setCursor(Qt.PointingHandCursor)
        self.open_excel_button.clicked.connect(self._open_excel)
        top_bar.addWidget(self.open_excel_button)
        self.edit_button = QPushButton("✏️  Editar")
        self.edit_button.setObjectName("SecondaryButton")
        self.edit_button.setCursor(Qt.PointingHandCursor)
        self.edit_button.clicked.connect(self._enter_edit_mode)
        top_bar.addWidget(self.edit_button)
        outer.addLayout(top_bar)

        self.title_label = QLabel("")
        self.title_label.setObjectName("PageTitle")
        self.title_label.setWordWrap(True)
        outer.addWidget(self.title_label)

        self.status_pill = QLabel("")
        self.status_pill.setVisible(False)
        outer.addWidget(self.status_pill, alignment=Qt.AlignLeft)

        # Everything below (the fields card, the category pickers, and
        # the Editar form) lives inside ONE shared scroll area rather
        # than directly in `outer`. Without it, all of that gets squeezed
        # into whatever vertical space is left in the window — that's
        # what was making the radio buttons render as illegible slivers
        # with no room for their labels once categories_panel joined the
        # card on this page. Title/status stay outside it, always
        # visible at the top regardless of how far you've scrolled.
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(14)

        self.empty_label = QLabel("Este proyecto no tiene ningún otro dato en el archivo.")
        self.empty_label.setObjectName("MutedText")
        self.empty_label.setVisible(False)
        content_layout.addWidget(self.empty_label)

        self.card = QFrame()
        self.card.setObjectName("Panel")
        self.card_layout = QGridLayout(self.card)
        self.card_layout.setContentsMargins(22, 20, 22, 20)
        self.card_layout.setHorizontalSpacing(18)
        self.card_layout.setVerticalSpacing(14)
        self.card_layout.setColumnStretch(1, 1)
        content_layout.addWidget(self.card)

        # Always part of the read view (shown/hidden together with
        # `card`, never gated behind "Editar") — each pick saves that one
        # field immediately, see _on_category_toggled.
        self.categories_panel = self._build_categories_panel()
        content_layout.addWidget(self.categories_panel)

        # Same "always interactive, saves instantly" treatment as
        # categories_panel above — NOT the "Editar" + "Guardar" batch
        # flow the other fields use. Ticking/unticking one employee here
        # writes straight into that row's LISTADO DE EMPLEADOS cell right
        # away (see _on_employee_toggled), per explicit request: ticking
        # someone should show up in the xlsx immediately, not wait for a
        # separate save step.
        self.employees_panel = self._build_employees_panel()
        content_layout.addWidget(self.employees_panel)

        self.edit_panel = self._build_edit_panel()
        self.edit_panel.setVisible(False)
        content_layout.addWidget(self.edit_panel)

        content_layout.addStretch()

        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QFrame.NoFrame)
        self.content_scroll.setWidget(content_widget)
        outer.addWidget(self.content_scroll, 1)

        self._item_id: int | None = None
        self._data: dict | None = None

    def _build_categories_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("Panel")
        layout = QGridLayout(panel)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setHorizontalSpacing(18)
        layout.setVerticalSpacing(14)
        layout.setColumnStretch(1, 1)
        row = 0

        # value (a canonical option, or None for "sin especificar") ->
        # its QRadioButton, one dict per category field — plus the
        # QButtonGroup itself, needed to block/unblock its signals while
        # load() sets the current selection programmatically (so that
        # doesn't get mistaken for the user clicking a new option and
        # trigger a write).
        self._category_radio_buttons: dict[str, dict[str | None, QRadioButton]] = {}
        self._category_groups: dict[str, QButtonGroup] = {}
        for idx, (label_text, key) in enumerate(EDIT_CATEGORY_FIELDS):
            if idx > 0:
                separator = QFrame()
                separator.setObjectName("DottedSeparator")
                separator.setFixedHeight(1)
                layout.addWidget(separator, row, 0, 1, 2)
                row += 1

            label = QLabel(label_text + ":")
            label.setObjectName("MetricTitle")
            label.setAlignment(Qt.AlignTop | Qt.AlignLeft)

            options_widget = QWidget()
            options_layout = QVBoxLayout(options_widget)
            options_layout.setContentsMargins(0, 0, 0, 0)
            options_layout.setSpacing(4)
            group = QButtonGroup(options_widget)
            self._category_groups[key] = group

            buttons: dict[str | None, QRadioButton] = {}
            none_button = QRadioButton(_SIN_ESPECIFICAR)
            group.addButton(none_button)
            options_layout.addWidget(none_button)
            buttons[None] = none_button
            for option in CATEGORY_OPTIONS[key]:
                button = QRadioButton(option)
                group.addButton(button)
                options_layout.addWidget(button)
                buttons[option] = button
            self._category_radio_buttons[key] = buttons

            group.buttonToggled.connect(
                lambda button, checked, key=key: self._on_category_toggled(key, button, checked)
            )

            layout.addWidget(label, row, 0)
            layout.addWidget(options_widget, row, 1)
            row += 1

        return panel

    def _sync_category_radios(self, data: dict | None) -> None:
        """Select the radio matching each category field's current value
        — WITHOUT triggering _on_category_toggled (signals blocked),
        since this just reflects already-saved state, it isn't a user
        edit to write back."""
        for _label, key in EDIT_CATEGORY_FIELDS:
            current = data.get(key) if data else None
            buttons = self._category_radio_buttons[key]
            group = self._category_groups[key]
            group.blockSignals(True)
            buttons.get(current, buttons[None]).setChecked(True)
            group.blockSignals(False)

    def _on_category_toggled(self, key: str, button: QRadioButton, checked: bool) -> None:
        # QButtonGroup fires buttonToggled twice per click — once for the
        # button losing the check, once for the one gaining it. Only
        # react to the latter.
        if not checked or self._item_id is None:
            return
        value = next(
            (candidate for candidate, btn in self._category_radio_buttons[key].items() if btn is button),
            None,
        )
        updates = {key: value}
        # Picking "Inicio Proyecto" also stamps FECHA DEL PROYECTO with
        # that exact moment's date — the whole point of that option — in
        # the SAME write as the status change, not a separate save.
        if key == "status" and value == "Inicio Proyecto":
            updates["start_date"] = datetime.date.today().strftime("%d/%m/%Y")
        try:
            data_service.update_project_info(self._item_id, updates)
        except Exception as exc:
            QMessageBox.critical(self, "No se pudo guardar", str(exc))
            self._sync_category_radios(self._data)  # revert the radio to the last saved value
            return
        # Full reload — cheap at this dataset's size, and guarantees the
        # status pill, the card's derived fields, and every other
        # category radio stay in sync with what's now actually on disk.
        self.load(self._item_id)

    def _build_employees_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("Panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(10)

        title = QLabel("Lista de empleados:")
        title.setObjectName("MetricTitle")
        layout.addWidget(title)

        # Reserves the slot; the actual checkboxes are (re)built by
        # _rebuild_employee_checklist, called here once and again on
        # every load() — see that method for why.
        self._employee_checklist_slot = QVBoxLayout()
        layout.addLayout(self._employee_checklist_slot)
        self._employee_checkboxes: dict[str, QCheckBox] = {}
        self._rebuild_employee_checklist()

        return panel

    def _rebuild_employee_checklist(self) -> None:
        """Rebuilds the tick/untick checklist from the CURRENT roster
        (load_employees_roster() re-reads roster.json fresh every
        call) — called from load() every time a project is opened, so
        someone added/renamed/removed via Configuración shows up here
        without restarting the app. The previous checkbox widgets (if
        any) are torn down first; load() re-ticks the right boxes for
        this specific row right after calling this."""
        while self._employee_checklist_slot.count():
            taken = self._employee_checklist_slot.takeAt(0)
            widget = taken.widget()
            if widget is not None:
                widget.deleteLater()

        checklist_widget, self._employee_checkboxes = _build_employee_checklist()
        self._employee_checklist_slot.addWidget(checklist_widget)
        for name, checkbox in self._employee_checkboxes.items():
            checkbox.toggled.connect(
                lambda checked, name=name: self._on_employee_toggled(name, checked)
            )

    def _sync_employee_checkboxes(self, data: dict | None) -> None:
        """Ticks exactly the checkboxes matching this row's current
        employee_list value — WITHOUT triggering _on_employee_toggled
        (signals blocked), same reasoning as _sync_category_radios:
        this reflects already-saved state, it isn't a user edit to
        write back."""
        employee_list_text = data.get("employee_list") if data else None
        for checkbox in self._employee_checkboxes.values():
            checkbox.blockSignals(True)
        _set_checkboxes_from_employee_list_text(self._employee_checkboxes, employee_list_text)
        for checkbox in self._employee_checkboxes.values():
            checkbox.blockSignals(False)

    def _on_employee_toggled(self, name: str, checked: bool) -> None:
        if self._item_id is None:
            return
        # Recomputed from ALL currently ticked boxes (not just this one
        # name added/removed from the old text) — the same approach
        # _employee_list_text_from_checkboxes always uses, so the saved
        # cell is always exactly "whoever's ticked right now", never
        # drifting from what's shown on screen.
        new_text = _employee_list_text_from_checkboxes(self._employee_checkboxes)
        try:
            data_service.update_project_info(self._item_id, {"employee_list": new_text})
        except Exception as exc:
            QMessageBox.critical(self, "No se pudo guardar", str(exc))
            self._sync_employee_checkboxes(self._data)  # revert to the last saved state
            return
        # Full reload — keeps the card's avatar-icon glance row (see
        # _build_employee_avatar_row) in sync with what's now actually
        # on disk, same as every other instant-save field on this page.
        self.load(self._item_id)

    def _build_edit_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("Panel")
        layout = QGridLayout(panel)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setHorizontalSpacing(18)
        layout.setVerticalSpacing(14)
        layout.setColumnStretch(1, 1)
        row = 0

        self._edit_inputs: dict[str, QLineEdit] = {}
        for label_text, key in EDIT_TEXT_FIELDS:
            label = QLabel(label_text + ":")
            label.setObjectName("MetricTitle")
            label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            field = QLineEdit()
            layout.addWidget(label, row, 0)
            layout.addWidget(field, row, 1)
            self._edit_inputs[key] = field
            row += 1

        self._edit_multiline: dict[str, QTextEdit] = {}
        for label_text, key in EDIT_MULTILINE_FIELDS:
            label = QLabel(label_text + ":")
            label.setObjectName("MetricTitle")
            label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            field = QTextEdit()
            field.setMaximumHeight(70)
            layout.addWidget(label, row, 0)
            layout.addWidget(field, row, 1)
            self._edit_multiline[key] = field
            row += 1

        # "Lista de empleados" is NOT in this batch form — it's its own
        # always-visible, instant-save panel (see _build_employees_panel
        # / _on_employee_toggled), same treatment as the category radios
        # above it, so it's intentionally absent here.

        button_row = QHBoxLayout()
        button_row.addStretch()
        self.cancel_button = QPushButton("Cancelar")
        self.cancel_button.setObjectName("LinkButton")
        self.cancel_button.clicked.connect(self._exit_edit_mode)
        button_row.addWidget(self.cancel_button)
        self.save_button = QPushButton("Guardar")
        self.save_button.setObjectName("PrimaryButton")
        self.save_button.clicked.connect(self._save_edit)
        button_row.addWidget(self.save_button)
        layout.addLayout(button_row, row, 0, 1, 2)

        return panel

    def _clear_card(self) -> None:
        while self.card_layout.count():
            item = self.card_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def _open_excel(self) -> None:
        if self._data is None:
            return
        try:
            path = data_service.project_info_file_path(self._data["year"])
        except FileNotFoundError as exc:
            QMessageBox.warning(self, "Archivo no encontrado", str(exc))
            return
        open_local_path(self, path, f"el archivo {self._data['year']}.xlsx")

    def load(self, item_id: int) -> None:
        self._item_id = item_id
        self.edit_panel.setVisible(False)
        self.edit_button.setVisible(True)
        try:
            data = data_service.project_info_detail(item_id)
        except Exception as exc:
            QMessageBox.critical(self, "Error al cargar la información", str(exc))
            return
        if data is None:
            QMessageBox.warning(
                self, "No encontrado", "Este proyecto ya no está en el archivo de datos."
            )
            return
        self._data = data

        self.title_label.setText(data.get("project_name") or "(Sin nombre)")

        status = data.get("status")
        self.status_pill.setVisible(True)
        self.status_pill.setText(status or "Sin estado")
        self.status_pill.setObjectName(_planning_pill_style(status) if status else "NeutralPill")
        # Re-polishing is required after changing objectName on an
        # already-shown widget — Qt only applies a stylesheet rule keyed
        # by object name once, at the widget's first polish.
        self.status_pill.style().unpolish(self.status_pill)
        self.status_pill.style().polish(self.status_pill)

        self._sync_category_radios(data)
        # Rebuilt from the current roster on every load (not just once
        # in __init__) — see _rebuild_employee_checklist — so someone
        # added/renamed/removed in Configuración shows up here right
        # away; _sync_employee_checkboxes then ticks the right boxes
        # for THIS row against those freshly-built checkboxes.
        self._rebuild_employee_checklist()
        self._sync_employee_checkboxes(data)

        self._clear_card()
        self.card.setVisible(True)
        self.categories_panel.setVisible(True)
        self.employees_panel.setVisible(True)
        row_idx = 0

        # Always shown first, even with no data yet — see
        # ALWAYS_SHOWN_FIELDS' comment above.
        for label_text, key in ALWAYS_SHOWN_FIELDS:
            value = data.get(key)
            label = QLabel(label_text + ":")
            label.setObjectName("MetricTitle")
            label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            self.card_layout.addWidget(label, row_idx, 0)
            if key == "employee_list":
                # Small circular avatar icons instead of the raw
                # free-text cell — see _build_employee_avatar_row.
                self.card_layout.addWidget(_build_employee_avatar_row(value), row_idx, 1)
            else:
                value_label = QLabel(str(value) if value not in (None, "") else "Sin datos todavía")
                value_label.setWordWrap(True)
                if value in (None, ""):
                    value_label.setObjectName("MutedText")
                self.card_layout.addWidget(value_label, row_idx, 1)
            row_idx += 1

        for label_text, key in PROJECT_INFO_FIELDS:
            value = data.get(key)
            if value in (None, ""):
                continue
            label = QLabel(label_text + ":")
            label.setObjectName("MetricTitle")
            label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            value_label = QLabel(str(value))
            value_label.setWordWrap(True)
            self.card_layout.addWidget(label, row_idx, 0)
            self.card_layout.addWidget(value_label, row_idx, 1)
            row_idx += 1

        self.empty_label.setVisible(False)

    def _enter_edit_mode(self) -> None:
        if self._item_id is None:
            return
        data = self._data

        for _label, key in EDIT_TEXT_FIELDS:
            self._edit_inputs[key].setText(data.get(key) or "")
        for _label, key in EDIT_MULTILINE_FIELDS:
            self._edit_multiline[key].setPlainText(data.get(key) or "")

        # categories_panel AND employees_panel both stay out of this
        # mode entirely — they each save the instant you click/tick
        # something (see _on_category_toggled / _on_employee_toggled),
        # so they're hidden alongside `card` while there's a batch of
        # OTHER unsaved edits in progress here, to avoid any confusion
        # about what's saved and what isn't.
        self.card.setVisible(False)
        self.categories_panel.setVisible(False)
        self.employees_panel.setVisible(False)
        self.status_pill.setVisible(False)
        self.edit_button.setVisible(False)
        self.edit_panel.setVisible(True)

    def _exit_edit_mode(self) -> None:
        self.load(self._item_id)

    def _save_edit(self) -> None:
        updates: dict[str, str | None] = {}
        for _label, key in EDIT_TEXT_FIELDS:
            updates[key] = self._edit_inputs[key].text().strip() or None
        for _label, key in EDIT_MULTILINE_FIELDS:
            updates[key] = self._edit_multiline[key].toPlainText().strip() or None
        # employee_list is NOT part of this batch save — see
        # _on_employee_toggled, it's already saved by the time you get
        # here.

        try:
            data_service.update_project_info(self._item_id, updates)
        except Exception as exc:
            QMessageBox.critical(self, "No se pudo guardar", str(exc))
            return

        self.load(self._item_id)


class ProyectosInfoNewPage(QWidget):
    """Form to add a brand-new row to one of the per-year xlsx files
    (2008-2026, whichever are in reader.YEARS_WITH_DATA — see
    src/project_info/writer.py's append_project_info_row —
    only ever writes that one new row, nothing existing is touched).
    Same field set as ProyectosInfoDetailPage's Editar form (text fields
    + "Lista de empleados" + the 4 category radio pickers), plus a year
    picker up top since a new row has to go into exactly one file, and
    unlike the detail page's always-instant category pickers, everything
    here is one batch write on "Crear proyecto" — there's no existing
    row to save into until that button is pressed. Built once; reset()
    blanks every field back out each time the page is (re)opened."""

    project_created = Signal(int)  # the new row's id, so the caller can open its detail page

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 22, 26, 26)
        outer.setSpacing(14)

        self.back_button = QPushButton("←  Cancelar")
        self.back_button.setObjectName("LinkButton")
        self.back_button.setCursor(Qt.PointingHandCursor)
        outer.addWidget(self.back_button, alignment=Qt.AlignLeft)

        heading = QLabel("Nuevo proyecto")
        heading.setObjectName("PageTitle")
        outer.addWidget(heading)

        subtitle = QLabel(
            "Se añadirá como una fila nueva al final del archivo del año elegido."
        )
        subtitle.setObjectName("MutedText")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(14)

        form_panel = QFrame()
        form_panel.setObjectName("Panel")
        layout = QGridLayout(form_panel)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setHorizontalSpacing(18)
        layout.setVerticalSpacing(14)
        layout.setColumnStretch(1, 1)
        row = 0

        # A combo box, not a row of radio buttons — YEARS_WITH_DATA now
        # spans 2008-2026 (see reader.py), and ~19 radio buttons side by
        # side would overflow the form's width. A dropdown scales to
        # however many years end up in that tuple without needing
        # another UI change.
        year_label = QLabel("Año:")
        year_label.setObjectName("MetricTitle")
        self._year_combo = QComboBox()
        self._year_combo.addItems(list(YEARS_WITH_DATA))
        layout.addWidget(year_label, row, 0)
        layout.addWidget(self._year_combo, row, 1)
        row += 1

        separator = QFrame()
        separator.setObjectName("DottedSeparator")
        separator.setFixedHeight(1)
        layout.addWidget(separator, row, 0, 1, 2)
        row += 1

        self._new_inputs: dict[str, QLineEdit] = {}
        for label_text, key in EDIT_TEXT_FIELDS:
            label = QLabel(label_text + (" *:" if key == "project_name" else ":"))
            label.setObjectName("MetricTitle")
            label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            field = QLineEdit()
            layout.addWidget(label, row, 0)
            layout.addWidget(field, row, 1)
            self._new_inputs[key] = field
            row += 1

        self._new_multiline: dict[str, QTextEdit] = {}
        for label_text, key in EDIT_MULTILINE_FIELDS:
            label = QLabel(label_text + ":")
            label.setObjectName("MetricTitle")
            label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            field = QTextEdit()
            field.setMaximumHeight(70)
            layout.addWidget(label, row, 0)
            layout.addWidget(field, row, 1)
            self._new_multiline[key] = field
            row += 1

        # "Lista de empleados" — same tick/untick roster checklist as
        # ProyectosInfoDetailPage's Editar panel (see
        # _build_employee_checklist), not a generic EDIT_MULTILINE_FIELDS
        # entry.
        employees_label = QLabel("Lista de empleados:")
        employees_label.setObjectName("MetricTitle")
        employees_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(employees_label, row, 0)
        # The checklist widget itself is (re)built by
        # _rebuild_new_employee_checklist — called once right below and
        # again from reset() every time this page reopens, so roster
        # edits made via Configuración show up here without restarting
        # the app. _employees_form_layout/_employees_form_row remember
        # where to (re)place it.
        self._employees_form_layout = layout
        self._employees_form_row = row
        self._new_employee_checkboxes: dict[str, QCheckBox] = {}
        self._rebuild_new_employee_checklist()
        row += 1

        self._new_radio_buttons: dict[str, dict[str | None, QRadioButton]] = {}
        for idx, (label_text, key) in enumerate(EDIT_CATEGORY_FIELDS):
            cat_separator = QFrame()
            cat_separator.setObjectName("DottedSeparator")
            cat_separator.setFixedHeight(1)
            layout.addWidget(cat_separator, row, 0, 1, 2)
            row += 1

            label = QLabel(label_text + ":")
            label.setObjectName("MetricTitle")
            label.setAlignment(Qt.AlignTop | Qt.AlignLeft)

            options_widget = QWidget()
            options_layout = QVBoxLayout(options_widget)
            options_layout.setContentsMargins(0, 0, 0, 0)
            options_layout.setSpacing(4)
            group = QButtonGroup(options_widget)

            buttons: dict[str | None, QRadioButton] = {}
            none_button = QRadioButton(_SIN_ESPECIFICAR)
            group.addButton(none_button)
            options_layout.addWidget(none_button)
            buttons[None] = none_button
            for option in CATEGORY_OPTIONS[key]:
                button = QRadioButton(option)
                group.addButton(button)
                options_layout.addWidget(button)
                buttons[option] = button

            layout.addWidget(label, row, 0)
            layout.addWidget(options_widget, row, 1)
            self._new_radio_buttons[key] = buttons
            row += 1

        button_row = QHBoxLayout()
        button_row.addStretch()
        cancel_button = QPushButton("Cancelar")
        cancel_button.setObjectName("LinkButton")
        cancel_button.clicked.connect(lambda: self.back_button.click())
        button_row.addWidget(cancel_button)
        self.create_button = QPushButton("Crear proyecto")
        self.create_button.setObjectName("PrimaryButton")
        self.create_button.clicked.connect(self._on_create_clicked)
        button_row.addWidget(self.create_button)
        layout.addLayout(button_row, row, 0, 1, 2)

        content_layout.addWidget(form_panel)
        content_layout.addStretch()

        content_scroll = QScrollArea()
        content_scroll.setWidgetResizable(True)
        content_scroll.setFrameShape(QFrame.NoFrame)
        content_scroll.setWidget(content_widget)
        outer.addWidget(content_scroll, 1)

    def _rebuild_new_employee_checklist(self) -> None:
        """Rebuilds the tick/untick checklist from the CURRENT roster
        (see load_employees_roster) at self._employees_form_row, column
        1 of the form grid — the previous widget (if any) is removed
        first. Called once during __init__ and again from reset() every
        time this page is (re)opened, so someone added/renamed/removed
        via Configuración shows up here without restarting the app."""
        old_item = self._employees_form_layout.itemAtPosition(self._employees_form_row, 1)
        if old_item is not None:
            old_widget = old_item.widget()
            if old_widget is not None:
                self._employees_form_layout.removeWidget(old_widget)
                old_widget.deleteLater()
        employees_widget, self._new_employee_checkboxes = _build_employee_checklist()
        self._employees_form_layout.addWidget(employees_widget, self._employees_form_row, 1)

    def reset(self) -> None:
        """Blank every field, called each time the page is opened —
        otherwise the previous project's now-created data would still
        be sitting in the form."""
        self._year_combo.setCurrentText(YEARS_WITH_DATA[-1])
        for field in self._new_inputs.values():
            field.clear()
        for field in self._new_multiline.values():
            field.clear()
        self._rebuild_new_employee_checklist()
        for buttons in self._new_radio_buttons.values():
            buttons[None].setChecked(True)
        self._new_inputs["project_name"].setFocus()

    def _on_create_clicked(self) -> None:
        year_str = self._year_combo.currentText() or None
        if year_str is None:
            QMessageBox.warning(self, "Falta el año", "Elige a qué año pertenece el proyecto.")
            return

        values: dict[str, str | None] = {}
        for _label, key in EDIT_TEXT_FIELDS:
            values[key] = self._new_inputs[key].text().strip() or None
        for _label, key in EDIT_MULTILINE_FIELDS:
            values[key] = self._new_multiline[key].toPlainText().strip() or None
        values["employee_list"] = _employee_list_text_from_checkboxes(
            self._new_employee_checkboxes
        )
        for _label, key in EDIT_CATEGORY_FIELDS:
            selected = None
            for value, button in self._new_radio_buttons[key].items():
                if button.isChecked():
                    selected = value
                    break
            values[key] = selected

        try:
            new_row = data_service.create_project_info(int(year_str), values)
        except Exception as exc:
            QMessageBox.critical(self, "No se pudo crear el proyecto", str(exc))
            return

        self.project_created.emit(new_row["id"])


class TimelineWidget(QWidget):
    """Small custom-drawn year-by-year timeline: one column per year, a
    colored dot per linked folder that year (teal for the main phase,
    purple for 'direccion_obra'), matching the dot-timeline style already
    used in the browser version's project.html."""

    def __init__(self) -> None:
        super().__init__()
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(10)
        self._layout.addStretch()

    def set_links(self, links: list[dict]) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        years = sorted({l["year"] for l in links if l.get("year") is not None})
        if not years:
            empty = QLabel("Sin fechas registradas.")
            empty.setObjectName("MutedText")
            self._layout.addWidget(empty)
            self._layout.addStretch()
            return

        by_year: dict[int, list[dict]] = {}
        for link in links:
            by_year.setdefault(link.get("year"), []).append(link)

        for year in range(years[0], years[-1] + 1):
            column = QVBoxLayout()
            column.setSpacing(4)
            column.setAlignment(Qt.AlignHCenter)

            year_label = QLabel(str(year))
            year_label.setObjectName("TimelineYear")
            year_label.setAlignment(Qt.AlignHCenter)
            column.addWidget(year_label)

            dots_row = QHBoxLayout()
            dots_row.setSpacing(3)
            dots_row.setAlignment(Qt.AlignHCenter)
            year_links = by_year.get(year, [])
            if year_links:
                for link in year_links:
                    dot = QFrame()
                    dot.setFixedSize(10, 10)
                    dot.setObjectName("TimelineDotPhase" if link.get("phase") else "TimelineDotMain")
                    tooltip = f"{link.get('job_code') or ''} {link.get('job_name') or ''}".strip()
                    dot.setToolTip(tooltip)
                    dots_row.addWidget(dot)
            else:
                dot = QFrame()
                dot.setFixedSize(8, 8)
                dot.setObjectName("TimelineDotEmpty")
                dots_row.addWidget(dot)
            column.addLayout(dots_row)

            codes = " · ".join(l["job_code"] for l in year_links if l.get("job_code"))
            code_label = QLabel(codes)
            code_label.setObjectName("TimelineCode")
            code_label.setAlignment(Qt.AlignHCenter)
            column.addWidget(code_label)

            wrapper = QWidget()
            wrapper.setLayout(column)
            wrapper.setMinimumWidth(60)
            self._layout.addWidget(wrapper)

        self._layout.addStretch()


class ProjectDetailPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 22, 26, 26)
        outer.setSpacing(14)

        self.back_button = QPushButton("←  Volver")
        self.back_button.setObjectName("LinkButton")
        self.back_button.setCursor(Qt.PointingHandCursor)
        outer.addWidget(self.back_button, alignment=Qt.AlignLeft)

        self.title_label = QLabel("")
        self.title_label.setObjectName("PageTitle")
        outer.addWidget(self.title_label)

        self.subtitle_label = QLabel("")
        self.subtitle_label.setObjectName("MutedText")
        outer.addWidget(self.subtitle_label)

        self.locations_row = QHBoxLayout()
        self.locations_row.setSpacing(6)
        outer.addLayout(self.locations_row)

        history_title = QLabel("Historial por año")
        history_title.setObjectName("SectionTitle")
        outer.addWidget(history_title)

        timeline_panel = QFrame()
        timeline_panel.setObjectName("Panel")
        timeline_layout = QVBoxLayout(timeline_panel)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(100)
        self.timeline = TimelineWidget()
        scroll.setWidget(self.timeline)
        timeline_layout.addWidget(scroll)
        outer.addWidget(timeline_panel)

        location_graph_title = QLabel("Gráfico de ubicaciones")
        location_graph_title.setObjectName("SectionTitle")
        outer.addWidget(location_graph_title)

        self.location_graph_panel = QFrame()
        self.location_graph_panel.setObjectName("Panel")
        location_graph_layout = QVBoxLayout(self.location_graph_panel)
        location_graph_layout.setContentsMargins(1, 1, 1, 1)
        self.location_graph_panel.setFixedHeight(260)

        if _HAS_WEBENGINE:
            self.location_graph_view = QWebEngineView()
            location_graph_layout.addWidget(self.location_graph_view)
        else:  # pragma: no cover - fallback for a PySide6 build without WebEngine
            self.location_graph_view = None
            fallback = QLabel(
                "No se pudo cargar el componente de gráfico (QtWebEngine no está disponible)."
            )
            fallback.setWordWrap(True)
            fallback.setObjectName("MutedText")
            location_graph_layout.addWidget(fallback)

        self.location_graph_empty_label = QLabel("Este proyecto no tiene ubicaciones detectadas.")
        self.location_graph_empty_label.setObjectName("MutedText")
        self.location_graph_empty_label.setVisible(False)
        location_graph_layout.addWidget(self.location_graph_empty_label)

        outer.addWidget(self.location_graph_panel)

        links_title = QLabel("Carpetas vinculadas")
        links_title.setObjectName("SectionTitle")
        outer.addWidget(links_title)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Año", "Código y nombre", "Fase", "Archivo"])
        configure_table(self.table)
        outer.addWidget(self.table, 1)

        self._project_id: int | None = None

    def load(self, project_id: int) -> None:
        self._project_id = project_id
        try:
            data = data_service.project_detail(project_id)
        except Exception as exc:
            QMessageBox.critical(self, "Error al cargar el proyecto", str(exc))
            return
        if data is None:
            QMessageBox.warning(self, "No encontrado", "Este proyecto ya no existe.")
            return

        self.title_label.setText(data["canonical_name"])
        status = STATUS_LABELS.get(data.get("status"), data.get("status") or "")
        self.subtitle_label.setText(
            f"{data.get('first_seen_year') or '?'} - {data.get('last_seen_year') or '?'}  ·  {status}"
        )

        while self.locations_row.count():
            item = self.locations_row.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        for loc in data.get("locations", []):
            pill = QLabel(loc["name"])
            pill.setObjectName("LocationPill")
            self.locations_row.addWidget(pill)
        self.locations_row.addStretch()

        self.timeline.set_links(data["links"])
        self._load_location_graph(project_id)

        links = data["links"]
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(links))
        for row_idx, link in enumerate(links):
            self.table.setItem(row_idx, 0, QTableWidgetItem(str(link.get("year") or "?")))
            code_name = f"{link.get('job_code') or ''} {link.get('job_name') or ''}".strip()
            self.table.setItem(row_idx, 1, QTableWidgetItem(code_name))
            phase = PHASE_LABELS.get(link.get("phase"), link.get("phase") or "")
            self.table.setItem(row_idx, 2, QTableWidgetItem(phase))
            self.table.setItem(row_idx, 3, QTableWidgetItem(SOURCE_LABELS.get(link.get("source"), link.get("source") or "")))
        self.table.resizeColumnsToContents()
        self.table.setUpdatesEnabled(True)

    def _load_location_graph(self, project_id: int) -> None:
        try:
            graph = data_service.project_location_graph(project_id)
        except Exception as exc:
            QMessageBox.critical(self, "Error al cargar el gráfico de ubicaciones", str(exc))
            graph = {"elements": []}

        has_elements = bool(graph.get("elements"))
        self.location_graph_empty_label.setVisible(not has_elements)
        if self.location_graph_view is not None:
            self.location_graph_view.setVisible(has_elements)
            if has_elements:
                self.location_graph_view.setHtml(render_graph_html(graph["elements"]))


# Bundled locally (desktop_app/vendor/cytoscape.min.js) instead of a
# CDN <script src>. A CDN fetch on every graph render was the one truly
# unbounded step in the whole pipeline — every data_service call is
# single-digit milliseconds against the local sqlite database, but a
# network round trip to jsdelivr.net for a slow/flaky connection could
# stall the graph (and the perceived app) for seconds. Embedding the
# library text directly means opening a project's graph never touches
# the network at all.
_CYTOSCAPE_JS_PATH = Path(__file__).parent / "vendor" / "cytoscape.min.js"
_CYTOSCAPE_JS = _CYTOSCAPE_JS_PATH.read_text(encoding="utf-8")

_GRAPH_HTML_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<script>__CYTOSCAPE_JS__</script>
<style>
  html, body {{ margin:0; height:100%; background:#f4f7fb; font-family:'Segoe UI',sans-serif; }}
  #cy {{ width:100%; height:100%; }}
</style></head>
<body>
<div id="cy"></div>
<script>
const elements = {elements_json};
const cy = cytoscape({{
  container: document.getElementById('cy'),
  elements: elements,
  style: [
    {{ selector: 'node[type = "location"]', style: {{
        'background-color': '#0F6E56', 'label': 'data(label)', 'color': '#0c5a46',
        'font-size': 13, 'font-weight': 700, 'text-valign': 'bottom', 'text-margin-y': 6,
        'width': 42, 'height': 42
    }} }},
    {{ selector: 'node[type = "project"]', style: {{
        'background-color': '#d1d5db', 'label': 'data(label)', 'color': '#334155',
        'font-size': 11, 'text-valign': 'bottom', 'text-margin-y': 4,
        'width': 26, 'height': 26
    }} }},
    {{ selector: 'node[?is_current]', style: {{
        'background-color': '#0F6E56', 'border-width': 3, 'border-color': '#0c5a46',
        'font-weight': 700, 'width': 34, 'height': 34
    }} }},
    {{ selector: 'edge', style: {{ 'width': 2, 'line-color': '#cbd5e1', 'curve-style': 'bezier' }} }}
  ],
  layout: {{ name: 'cose', animate: false, padding: 40 }}
}});
</script>
</body></html>"""


def render_graph_html(elements: list[dict]) -> str:
    """Fill in the graph data, then splice in the cytoscape.js source as
    a plain string replacement (not part of the .format() call) — the
    minified library is full of literal `{{`/`}}` characters that would
    otherwise have to be escaped throughout a ~366KB file."""
    html = _GRAPH_HTML_TEMPLATE.format(elements_json=json.dumps(elements))
    return html.replace("__CYTOSCAPE_JS__", _CYTOSCAPE_JS)


class _EmployeeEditDialog(QDialog):
    """Add-or-rename-and-photo form used by ConfiguracionPage, for both
    "+ Añadir empleado" (existing=None) and a card's "Editar" button
    (existing={"name", "match_name", "photo"}). Doesn't touch the
    roster or write any photo file itself — the caller reads
    .result_name / .picked_photo_path after exec() == QDialog.Accepted
    and does the actual save (see ConfiguracionPage._on_add_clicked /
    _on_edit_clicked), so cancelling never leaves a half-applied edit."""

    def __init__(self, existing: dict | None = None, parent=None) -> None:
        super().__init__(parent)
        self._existing = existing
        self.picked_photo_path: str | None = None
        self.setWindowTitle("Editar empleado" if existing else "Añadir empleado")
        self.setMinimumWidth(340)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)

        self._preview_label = QLabel()
        self._preview_label.setFixedSize(96, 96)
        self._preview_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._preview_label, alignment=Qt.AlignHCenter)
        self._refresh_preview()

        photo_button = QPushButton("Cambiar foto…" if existing else "Elegir foto…")
        photo_button.setObjectName("SecondaryButton")
        photo_button.setCursor(Qt.PointingHandCursor)
        photo_button.clicked.connect(self._pick_photo)
        layout.addWidget(photo_button, alignment=Qt.AlignHCenter)

        name_label = QLabel("Nombre completo:")
        name_label.setObjectName("MetricTitle")
        layout.addWidget(name_label)
        self.name_edit = QLineEdit(existing["name"] if existing else "")
        self.name_edit.setPlaceholderText("Ej. Juan Pérez García")
        layout.addWidget(self.name_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Guardar")
        buttons.button(QDialogButtonBox.Cancel).setText("Cancelar")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _refresh_preview(self) -> None:
        if self.picked_photo_path:
            pixmap = _circular_mask_pixmap(QPixmap(self.picked_photo_path), 96)
        elif self._existing:
            pixmap = _circular_avatar_pixmap(self._existing["photo"], 96)
        else:
            pixmap = _circular_mask_pixmap(QPixmap(), 96)  # neutral placeholder circle
        self._preview_label.setPixmap(pixmap)

    def _pick_photo(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Elegir foto", "", "Imágenes (*.png *.jpg *.jpeg *.webp *.bmp)"
        )
        if not path:
            return
        if QImage(path).isNull():
            QMessageBox.warning(self, "Imagen no válida", "No se pudo abrir esa imagen.")
            return
        self.picked_photo_path = path
        self._refresh_preview()

    def _on_accept(self) -> None:
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "Falta el nombre", "Escribe el nombre del empleado.")
            return
        if self._existing is None and not self.picked_photo_path:
            QMessageBox.warning(self, "Falta la foto", "Elige una foto para el nuevo empleado.")
            return
        self.accept()

    @property
    def result_name(self) -> str:
        return self.name_edit.text().strip()


class ConfiguracionPage(QWidget):
    """Employee roster admin — add, rename, replace the photo of, or
    remove someone from the fixed employee list used by "Lista de
    empleados" everywhere else in the app (see load_employees_roster /
    save_employees_roster). Every action here saves to roster.json (and
    the photo file, when relevant) immediately, no separate Guardar
    step — same instant-save feel as the category radios and employee
    checklist elsewhere in the app.

    Cards are matched back to their roster entry by `photo` filename
    (see _save_employee_photo — guaranteed unique per employee), not by
    list position or object identity, since refresh() rebuilds the grid
    (and therefore every card's captured `employee` dict) from scratch
    on every visit.

    Rebuilt from scratch on every refresh() rather than patched in
    place — the roster is small (a handful to a few dozen people), so
    this stays simple and always correct rather than being a meaningful
    performance concern (unlike the ~2400-row Proyectos Info tree)."""

    _CARDS_PER_ROW = 3

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 22, 26, 26)
        outer.setSpacing(14)

        top_bar = QHBoxLayout()
        heading = QLabel("Empleados")
        heading.setObjectName("SectionTitle")
        top_bar.addWidget(heading)
        top_bar.addStretch()
        add_button = QPushButton("+  Añadir empleado")
        add_button.setObjectName("PrimaryButton")
        add_button.setCursor(Qt.PointingHandCursor)
        add_button.clicked.connect(self._on_add_clicked)
        top_bar.addWidget(add_button)
        outer.addLayout(top_bar)

        note = QLabel(
            "Estas son las personas que se pueden marcar en «Lista de empleados» "
            "de cada proyecto, en Proyectos Info. Eliminar a alguien de aquí no "
            "borra nada de los proyectos donde ya estaba marcado — solo deja de "
            "aparecer como opción para marcar en el futuro."
        )
        note.setObjectName("MutedText")
        note.setWordWrap(True)
        outer.addWidget(note)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 8, 0, 8)
        self._grid.setSpacing(14)
        scroll.setWidget(self._grid_host)
        outer.addWidget(scroll, 1)

    def refresh(self) -> None:
        while self._grid.count():
            taken = self._grid.takeAt(0)
            widget = taken.widget()
            if widget is not None:
                widget.deleteLater()

        roster = load_employees_roster()
        for index, employee in enumerate(roster):
            row, col = divmod(index, self._CARDS_PER_ROW)
            self._grid.addWidget(self._build_card(employee), row, col)
        self._grid.setRowStretch(len(roster) // self._CARDS_PER_ROW + 1, 1)

    def _build_card(self, employee: dict) -> QFrame:
        card = QFrame()
        card.setObjectName("Panel")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 16)
        layout.setSpacing(10)

        avatar = QLabel()
        avatar.setPixmap(_circular_avatar_pixmap(employee["photo"], 64))
        avatar.setFixedSize(64, 64)
        layout.addWidget(avatar, alignment=Qt.AlignHCenter)

        name_label = QLabel(employee["name"])
        name_label.setObjectName("SectionTitle")
        name_label.setAlignment(Qt.AlignCenter)
        name_label.setWordWrap(True)
        layout.addWidget(name_label)

        button_row = QHBoxLayout()
        edit_button = QPushButton("Editar")
        edit_button.setObjectName("SecondaryButton")
        edit_button.setCursor(Qt.PointingHandCursor)
        edit_button.clicked.connect(lambda checked=False, e=employee: self._on_edit_clicked(e))
        delete_button = QPushButton("Eliminar")
        delete_button.setObjectName("DangerButton")
        delete_button.setCursor(Qt.PointingHandCursor)
        delete_button.clicked.connect(lambda checked=False, e=employee: self._on_delete_clicked(e))
        button_row.addWidget(edit_button)
        button_row.addWidget(delete_button)
        layout.addLayout(button_row)

        return card

    def _on_add_clicked(self) -> None:
        dialog = _EmployeeEditDialog(existing=None, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        name = dialog.result_name
        roster = load_employees_roster()
        if any(e["name"].strip().lower() == name.lower() for e in roster):
            QMessageBox.warning(self, "Ya existe", f"Ya hay un empleado llamado «{name}».")
            return
        try:
            photo_filename = _save_employee_photo(dialog.picked_photo_path, name)
        except ValueError as exc:
            QMessageBox.critical(self, "No se pudo guardar la foto", str(exc))
            return
        roster.append({
            "name": name,
            "match_name": name.split()[0] if name.split() else name,
            "photo": photo_filename,
        })
        save_employees_roster(roster)
        self.refresh()

    def _on_edit_clicked(self, employee: dict) -> None:
        dialog = _EmployeeEditDialog(existing=employee, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        new_name = dialog.result_name
        roster = load_employees_roster()
        target = next((e for e in roster if e["photo"] == employee["photo"]), None)
        if target is None:
            # The roster changed elsewhere between this card being drawn
            # and the dialog being saved — shouldn't normally happen in
            # a single-user desktop app, but refresh and let the user
            # retry rather than silently doing nothing.
            self.refresh()
            return
        if any(
            e is not target and e["name"].strip().lower() == new_name.lower()
            for e in roster
        ):
            QMessageBox.warning(self, "Ya existe", f"Ya hay un empleado llamado «{new_name}».")
            return
        if dialog.picked_photo_path:
            try:
                _save_employee_photo(dialog.picked_photo_path, new_name, existing_photo=target["photo"])
            except ValueError as exc:
                QMessageBox.critical(self, "No se pudo guardar la foto", str(exc))
                return
        target["name"] = new_name
        save_employees_roster(roster)
        self.refresh()

    def _on_delete_clicked(self, employee: dict) -> None:
        answer = QMessageBox.question(
            self,
            "Eliminar empleado",
            f"¿Eliminar a «{employee['name']}» de la lista de empleados?\n\n"
            "Ya no aparecerá para marcar en nuevos proyectos, pero los "
            "proyectos que ya lo tienen marcado conservan ese texto en "
            "el Excel — no se borra nada del histórico.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        roster = [e for e in load_employees_roster() if e["photo"] != employee["photo"]]
        save_employees_roster(roster)
        self.refresh()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Índice de Proyectos")
        self.resize(1360, 840)
        self.setMinimumSize(1080, 680)
        self.refresh_worker: RefreshWorker | None = None
        self.sync_trabajos_worker: SyncTrabajosWorker | None = None
        self._refresh_started_at: float | None = None
        self._refresh_current_stage_label = ""
        # Ticks once a second while a refresh is running, purely to keep
        # the elapsed-time text in the top bar's status label current
        # (see _update_refresh_elapsed) — the stage text itself only
        # changes when RefreshWorker.stage_changed actually fires, which
        # can be minutes apart during a long crawl.
        self._refresh_elapsed_timer = QTimer(self)
        self._refresh_elapsed_timer.setInterval(1000)
        self._refresh_elapsed_timer.timeout.connect(self._update_refresh_elapsed)

        root = QWidget()
        root.setObjectName("AppRoot")
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(self._build_sidebar())

        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        main.addWidget(self._build_topbar())

        self.stack = QStackedWidget()
        self.projects_page = ProjectsPage()
        self.proyectos_info_page = ProyectosInfoPage()
        self.search_page = SearchPage()
        self.ofertas_page = OfertasPage()
        self.project_page = ProjectDetailPage()
        self.proyectos_info_detail_page = ProyectosInfoDetailPage()
        self.proyectos_info_new_page = ProyectosInfoNewPage()
        self.proyectos_info_company_page = ProyectosInfoCompanyPage()
        # Added at the end of the stack (index 8) rather than renumbering
        # every existing sub-page index above — its sidebar POSITION
        # (right below Ofertas) is set independently by where its entry
        # sits in the `buttons` list in _build_sidebar(), not by its
        # stack index.
        self.configuracion_page = ConfiguracionPage()
        for page in (
            self.projects_page,
            self.proyectos_info_page,
            self.search_page,
            self.ofertas_page,
            self.project_page,
            self.proyectos_info_detail_page,
            self.proyectos_info_new_page,
            self.proyectos_info_company_page,
            self.configuracion_page,
        ):
            self.stack.addWidget(page)
        main.addWidget(self.stack, 1)
        root_layout.addLayout(main, 1)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Listo")

        # Proyectos/Proyectos Info each open their own detail page, and
        # Proyectos Info can now go two levels deep (list -> company ->
        # project detail) — a real stack, not a single "return index"
        # variable, since a variable gets overwritten by the second
        # drill-down and breaks the FIRST page's "Volver" (list ->
        # company -> detail -> back (correct, pops to company) -> back
        # (used to silently no-op: the variable had been overwritten to
        # point at the company page itself, not the list). Buscar
        # doesn't open project pages at all — its folder/file results
        # open directly in Explorer instead.
        self._nav_stack: list[int] = []
        self.projects_page.project_opened.connect(self.open_project)
        self.project_page.back_button.clicked.connect(self._go_back)
        self.proyectos_info_page.company_opened.connect(self.open_company_projects)
        # A matched row shown directly under a company (search/category
        # filter narrowed it down — see ProyectosInfoPage._set_company_
        # children) opens its detail card straight away, same as any
        # other entry point into that page — no need to detour through
        # the company page first.
        self.proyectos_info_page.project_info_opened.connect(self.open_project_info)
        self.proyectos_info_company_page.back_button.clicked.connect(self._go_back)
        self.proyectos_info_company_page.project_info_opened.connect(self.open_project_info)
        self.proyectos_info_detail_page.back_button.clicked.connect(self._go_back)
        self.proyectos_info_page.new_project_requested.connect(self.open_new_project)
        self.proyectos_info_page.sync_trabajos_requested.connect(self.run_sync_trabajos)
        self.proyectos_info_new_page.back_button.clicked.connect(self._go_back)
        # Buscar's "Proyectos Info" button on a folder result — reuses
        # open_project_info, so "Volver" on the detail page correctly
        # returns to Buscar (index 2), same nav-stack mechanism as
        # every other entry point into that page.
        self.search_page.project_info_requested.connect(self.open_project_info)
        # A freshly created project opens straight into its own detail
        # page rather than back to the form — see open_new_project_result.
        self.proyectos_info_new_page.project_created.connect(self.open_new_project_result)

        self._navigate(0)
        self._apply_index_role_ui()
        self.refresh_all()

    def _apply_index_role_ui(self) -> None:
        """Only relevant in shared index mode (see config.example.yaml's
        "SHARED INDEX MODE" section) — everywhere else data_service.
        is_index_builder() is always True (every PC "builds" its own
        local copy) and this is a no-op. On a PC that's configured as a
        reader (shared_db_path set, is_index_builder false/unset), the
        Actualizar button would otherwise just produce a "no puede
        actualizarlo" error every time it's clicked — greying it out
        with an explanatory tooltip up front is clearer than letting
        someone click into that."""
        if data_service.is_index_builder():
            return
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("↻  Solo lectura")
        self.refresh_button.setToolTip(
            "Este PC solo lee el índice compartido; no puede actualizarlo. "
            "Pide a quien gestione el PC generador que ejecute la actualización allí."
        )

    def _shared_index_status_message(self) -> str | None:
        """"Índice compartido actualizado el <fecha> por <host>", or None
        when shared_db_path isn't configured (nothing to add to the
        default "Listo"/"Datos actualizados" status messages) — used so
        a reader PC has SOME way to see when the shared index was last
        actually refreshed, since its own Actualizar button can't tell
        it that by refreshing itself."""
        info = data_service.shared_index_info()
        if not info:
            return None
        built_at = info.get("built_at")
        host = info.get("built_by_host") or "?"
        if not built_at:
            return None
        when = datetime.datetime.fromtimestamp(built_at).strftime("%d/%m/%Y %H:%M")
        return f"Índice compartido actualizado el {when} por {host}"

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(228)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 22, 16, 18)
        layout.setSpacing(8)

        brand = QLabel("INGEVIA")
        brand.setObjectName("BrandTitle")
        brand_subtitle = QLabel("Índice de Proyectos")
        brand_subtitle.setObjectName("BrandSubtitle")
        layout.addWidget(brand)
        layout.addWidget(brand_subtitle)
        layout.addSpacing(22)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: list[QPushButton] = []
        # Stack index (second element) is NOT the same as position in
        # this list — Configuración lives at stack index 8 (added last,
        # after every other page, so nothing else had to be renumbered)
        # but its sidebar entry is placed right here, directly below
        # Ofertas, per the explicit request.
        buttons = [
            ("▦  Proyectos", 0),
            ("ℹ️  Proyectos Info", 1),
            ("🔍  Buscar", 2),
            ("📄  Ofertas", 3),
            ("⚙️  Configuración", 8),
        ]
        for text, index in buttons:
            button = QPushButton(text)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, i=index: self._navigate(i))
            self.nav_group.addButton(button)
            self.nav_buttons.append(button)
            layout.addWidget(button)
        self.nav_buttons[0].setChecked(True)

        layout.addStretch()
        status = QLabel("●  Aplicación local")
        status.setObjectName("SidebarStatus")
        status.setToolTip("Esta interfaz se ejecuta únicamente en este ordenador.")
        layout.addWidget(status)
        version = QLabel("Escritorio v1.0")
        version.setObjectName("SidebarStatus")
        layout.addWidget(version)
        return sidebar

    def _build_topbar(self) -> QFrame:
        topbar = QFrame()
        topbar.setObjectName("TopBar")
        topbar.setFixedHeight(76)
        layout = QHBoxLayout(topbar)
        layout.setContentsMargins(26, 12, 26, 12)

        title_block = QVBoxLayout()
        self.top_title = QLabel("Proyectos")
        self.top_title.setObjectName("PageTitle")
        self.top_subtitle = QLabel("")
        self.top_subtitle.setObjectName("PageSubtitle")
        title_block.addWidget(self.top_title)
        title_block.addWidget(self.top_subtitle)
        layout.addLayout(title_block)
        layout.addStretch()

        # Lives in the top bar (not on any one page) so it's visible no
        # matter which page you're on while "Actualizar" runs — Buscar
        # included, which otherwise had no progress indicator of its own
        # at all (see ProjectsPage.set_processing for the page-local one
        # that only ever showed on the Proyectos page). Indeterminate
        # (setRange(0, 0), a continuously animating bar) rather than a
        # real percentage — there's no reliable way to know the total
        # folder/file count on P: in advance, so a filled/emptying bar
        # would just be showing a fake number. The elapsed-time label
        # next to it (see _update_refresh_elapsed) is what actually
        # gives a sense of how long it's been running instead.
        self.refresh_progress = QProgressBar()
        self.refresh_progress.setRange(0, 0)
        self.refresh_progress.setFixedWidth(120)
        self.refresh_progress.setTextVisible(False)
        self.refresh_progress.setVisible(False)
        layout.addWidget(self.refresh_progress)

        self.refresh_status_label = QLabel("")
        self.refresh_status_label.setObjectName("MutedText")
        self.refresh_status_label.setVisible(False)
        layout.addWidget(self.refresh_status_label)

        self.refresh_button = QPushButton("↻  Actualizar")
        self.refresh_button.setObjectName("PrimaryButton")
        self.refresh_button.setCursor(Qt.PointingHandCursor)
        self.refresh_button.clicked.connect(self.run_refresh)
        layout.addWidget(self.refresh_button)
        return topbar

    _PAGE_TITLES = {
        0: ("Proyectos", "Lista completa de proyectos — filtra por año o por nombre"),
        1: ("Proyectos Info", "Ficha de cada proyecto según el Excel"),
        2: ("Buscar", "Buscar por nombre de carpeta, dirección o archivo"),
        3: ("Ofertas", "Documentos Firmado y Pedido dentro de FACTURACION/INGEVIA en 02.-GESTIÓN"),
        4: ("Proyecto", ""),
        5: ("Proyecto Info", ""),
        6: ("Nuevo proyecto", "Añade una fila nueva a uno de los archivos por año"),
        7: ("Proyectos de la empresa", ""),
        8: ("Configuración", "Añade, edita o elimina empleados de la lista de empleados"),
    }

    # Top-level pages — each has its own always-visible sidebar button
    # (see _build_sidebar's `buttons` list) and gets highlighted while
    # active. Every other stack index is a sub-page reached by drilling
    # in from one of these (see _open_subpage), with no sidebar entry
    # of its own.
    _TOP_LEVEL_PAGE_INDEXES = (0, 1, 2, 3, 8)

    def _navigate(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        title, subtitle = self._PAGE_TITLES[index]
        self.top_title.setText(title)
        self.top_subtitle.setText(subtitle)
        if index in self._TOP_LEVEL_PAGE_INDEXES:
            for button, button_index in zip(self.nav_buttons, self._TOP_LEVEL_PAGE_INDEXES):
                button.setChecked(button_index == index)
        else:
            # Sub-pages (Proyecto, Proyecto Info, Nuevo proyecto, the
            # company projects page...) have no sidebar entry of their
            # own. QButtonGroup in exclusive mode refuses to leave ZERO
            # buttons checked if you call setChecked(False) on the one
            # that's currently checked — it's a no-op — so without
            # dropping exclusivity first, the last-visited top-level
            # page's nav button stays highlighted even after navigating
            # into a sub-page, wrongly suggesting the app never left it.
            self.nav_group.setExclusive(False)
            for button in self.nav_buttons:
                button.setChecked(False)
            self.nav_group.setExclusive(True)
        if index == 0:
            self.projects_page.refresh()
        elif index == 1:
            self.proyectos_info_page.refresh()
        elif index == 3:
            self.ofertas_page.refresh()
        elif index == 8:
            # Re-read roster.json fresh every visit (not just once at
            # startup) — picks up edits made in THIS same session
            # without needing anything fancier, since ConfiguracionPage
            # itself is also the only place those edits can come from.
            self.configuracion_page.refresh()

    def _open_subpage(self, index: int) -> None:
        """Navigate to a page reached by drilling into something
        (project detail, company projects, Nuevo proyecto...) — pushes
        the CURRENT page onto _nav_stack first, so _go_back() can unwind
        an arbitrarily deep chain (e.g. list -> company -> detail) one
        step at a time instead of always jumping to one fixed page."""
        self._nav_stack.append(self.stack.currentIndex())
        self._navigate(index)

    def _go_back(self) -> None:
        index = self._nav_stack.pop() if self._nav_stack else 1
        self._navigate(index)

    def open_project(self, project_id: int) -> None:
        self.project_page.load(project_id)
        self._open_subpage(4)

    def open_company_projects(self, year: int, company: str) -> None:
        self.proyectos_info_company_page.load(year, company)
        self._open_subpage(7)

    def open_project_info(self, item_id: int) -> None:
        # Reached from wherever the user actually drilled in from — the
        # company page (normal path) or, after creating a project,
        # straight from open_new_project_result below — _open_subpage
        # records that automatically.
        self.proyectos_info_detail_page.load(item_id)
        self._open_subpage(5)

    def open_new_project(self) -> None:
        self.proyectos_info_new_page.reset()
        self._open_subpage(6)

    def open_new_project_result(self, item_id: int) -> None:
        # Deliberately NOT _open_subpage(5) — that would push the Nuevo
        # proyecto form (6, the current page when this fires) onto the
        # stack, so "Volver" on the detail page would land back on a
        # stale, already-submitted form. Instead, pop the entry
        # open_new_project() pushed (wherever "+ Nuevo proyecto" was
        # actually clicked from) and push that same value again — same
        # stack depth, but the detail page's "Volver" skips the form
        # entirely and returns to that original page.
        return_index = self._nav_stack.pop() if self._nav_stack else 1
        self._nav_stack.append(return_index)
        self.proyectos_info_detail_page.load(item_id)
        self._navigate(5)

    def refresh_all(self) -> None:
        try:
            self.projects_page.refresh()
            self.proyectos_info_page.refresh()
            self.search_page.refresh()
            self.ofertas_page.refresh()
            # In shared index mode (see config.example.yaml), this is the
            # one place a reader PC finds out when the shared index was
            # actually last built — its own Actualizar button is
            # disabled/read-only (see _apply_index_role_ui) so it can't
            # tell them that by refreshing itself.
            shared_message = self._shared_index_status_message()
            self.statusBar().showMessage(shared_message or "Datos actualizados", 6000 if shared_message else 4000)
        except Exception as exc:
            self.statusBar().showMessage("No se pudieron cargar los datos", 4000)
            QMessageBox.critical(self, "Error al cargar datos", str(exc))

    def run_refresh(self) -> None:
        if self.refresh_worker and self.refresh_worker.isRunning():
            return
        answer = QMessageBox.question(
            self,
            "Actualizar índice",
            "Se recorrerá de nuevo el archivo en P: y se actualizará el índice.\n"
            "Puede tardar bastante en carpetas grandes.\n\n¿Continuar?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return

        self.refresh_button.setEnabled(False)
        self.projects_page.set_processing(True, "Iniciando actualización...")
        self.statusBar().showMessage("Actualizando en segundo plano...")

        # Top-bar indicator — see _build_topbar for why this is separate
        # from projects_page's own bar: this one is visible on every
        # page (Buscar included), that one only while looking at
        # Proyectos.
        self._refresh_started_at = time.time()
        self._refresh_current_stage_label = "Iniciando actualización..."
        self.refresh_progress.setVisible(True)
        self.refresh_status_label.setVisible(True)
        self._update_refresh_elapsed()
        self._refresh_elapsed_timer.start()

        self.refresh_worker = RefreshWorker(self)
        self.refresh_worker.stage_changed.connect(self._refresh_stage_changed)
        self.refresh_worker.finished_with_result.connect(self._refresh_finished)
        self.refresh_worker.start()

    def _refresh_stage_changed(self, stage: str) -> None:
        label = STAGE_LABELS.get(stage, stage)
        self.projects_page.set_processing(True, label)
        self.statusBar().showMessage(label)
        self._refresh_current_stage_label = label
        self._update_refresh_elapsed()

    def _update_refresh_elapsed(self) -> None:
        """Refreshes the top bar's "<stage> (mm:ss)" text — called once a
        second by _refresh_elapsed_timer AND immediately whenever the
        stage itself changes, so the label never sits on a stale minute
        count for up to a full second after either kind of update."""
        if self._refresh_started_at is None:
            return
        elapsed = int(time.time() - self._refresh_started_at)
        minutes, seconds = divmod(elapsed, 60)
        self.refresh_status_label.setText(
            f"{self._refresh_current_stage_label} ({minutes}:{seconds:02d})"
        )

    def run_sync_trabajos(self) -> None:
        if self.sync_trabajos_worker and self.sync_trabajos_worker.isRunning():
            return

        # Year picker — defaults to the current calendar year (the old,
        # only-ever-current-year behavior) but lets you target any other
        # year's TRABAJOS folder/xlsx instead, e.g. to catch up 2025.xlsx
        # for something added there after the fact. Same newest-first
        # year list as every other year dropdown in the app.
        years = [str(y) for y in reversed(YEARS_WITH_DATA)]
        current_year_str = str(datetime.date.today().year)
        default_index = years.index(current_year_str) if current_year_str in years else 0
        year_str, ok = QInputDialog.getItem(
            self,
            "Sincronizar carpetas nuevas",
            "Elige el año a sincronizar:",
            years,
            default_index,
            editable=False,
        )
        if not ok:
            return
        year = int(year_str)

        answer = QMessageBox.question(
            self,
            "Sincronizar carpetas nuevas",
            f"Se recorrerá TRABAJOS {year} en P: buscando carpetas de proyecto/ubicación\n"
            f"que todavía no tengan una fila en {year}.xlsx, y se añadirá una fila nueva\n"
            "(NOMBRE + NUMERO DE PROYECTO) por cada una que falte. Las filas ya existentes\n"
            "no se tocan.\n\n¿Continuar?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return

        self.proyectos_info_page.sync_trabajos_button.setEnabled(False)
        self.proyectos_info_page.sync_trabajos_button.setText("⟳  Sincronizando...")
        self.statusBar().showMessage(f"Buscando carpetas nuevas en TRABAJOS {year}...")

        self.sync_trabajos_worker = SyncTrabajosWorker(year, self)
        self.sync_trabajos_worker.finished_with_result.connect(self._sync_trabajos_finished)
        self.sync_trabajos_worker.start()

    def _sync_trabajos_finished(self, success: bool, message: str, result: dict) -> None:
        self.proyectos_info_page.sync_trabajos_button.setEnabled(True)
        self.proyectos_info_page.sync_trabajos_button.setText("⟳  Sincronizar carpetas nuevas")
        if not success:
            self.statusBar().showMessage("La sincronización terminó con errores", 5000)
            QMessageBox.critical(self, "Error al sincronizar carpetas", message)
            return

        added = result.get("added") or []
        year = result.get("year")
        year_suffix = f" ({year})" if year else ""
        if not result.get("trabajos_dir"):
            self.statusBar().showMessage("Sincronización terminada", 4000)
            QMessageBox.warning(
                self,
                "Carpeta TRABAJOS no configurada",
                "No se encontró la carpeta 'trabajos' en la configuración (roots en "
                "config.yaml) — no se ha comprobado nada.",
            )
            return

        self.statusBar().showMessage(
            f"Sincronización completada{year_suffix}: {len(added)} fila(s) nueva(s)", 6000
        )
        if added:
            lines = "\n".join(f"  • {row['nombre']}  ({row['numero_de_proyecto']})" for row in added)
            QMessageBox.information(
                self,
                "Sincronización completada",
                f"Año: {year}\n"
                f"Carpetas revisadas: {result.get('checked')}\n"
                f"Ya existían: {result.get('already_present')}\n"
                f"Filas nuevas añadidas ({len(added)}):\n{lines}",
            )
            self.proyectos_info_page.refresh()
        else:
            QMessageBox.information(
                self,
                "Sincronización completada",
                f"Año: {year}\n"
                f"Carpetas revisadas: {result.get('checked')}\n"
                "No se encontró ninguna carpeta nueva — el Excel ya estaba al día.",
            )

    def _refresh_finished(self, success: bool, message: str, stats: dict) -> None:
        self.refresh_button.setEnabled(True)
        self.projects_page.set_processing(False)
        self._refresh_elapsed_timer.stop()
        self._refresh_started_at = None
        self.refresh_progress.setVisible(False)
        self.refresh_status_label.setVisible(False)
        if success:
            self.statusBar().showMessage("Actualización completada", 5000)
            if stats:
                QMessageBox.information(
                    self,
                    "Actualización completada",
                    f"Carpetas: {stats.get('folders')}\n"
                    f"Archivos: {stats.get('files')}\n"
                    f"Proyectos: {stats.get('projects')}\n"
                    f"Ubicaciones: {stats.get('locations')}",
                )
            self.refresh_all()
        else:
            self.statusBar().showMessage("La actualización terminó con errores", 5000)
            QMessageBox.critical(self, "Error durante la actualización", message)