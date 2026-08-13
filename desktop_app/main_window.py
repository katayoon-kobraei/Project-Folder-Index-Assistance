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
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
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
                self.finished_with_result.emit(False, "Ya hay una actualización en curso.", {})
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
    them is actually set."""

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

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Carpeta", "Proyecto", "Año", "Ubicación", "Ruta"])
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
        # Fixed widths for the middle columns, set ONCE rather than
        # recomputed from cell contents on every keystroke — the one
        # column each table needs shown in FULL (Ruta / Archivo) is
        # separately sized to its actual content in _render()/
        # _render_files() instead, which is cheap for a single column.
        table.setColumnWidth(0, 260)
        table.setColumnWidth(1, 200)
        table.setColumnWidth(2, 60)
        table.setColumnWidth(3, 160)

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
            self.table.setItem(row_idx, 0, QTableWidgetItem(folder["name"]))
            self.table.setItem(row_idx, 1, QTableWidgetItem(folder.get("company_project") or ""))
            self.table.setItem(row_idx, 2, QTableWidgetItem(str(folder.get("year") or "?")))
            self.table.setItem(row_idx, 3, QTableWidgetItem(folder.get("location_site") or ""))
            self.table.setItem(row_idx, 4, QTableWidgetItem(folder["path"]))
        # Only this one column, not resizeColumnsToContents() for all
        # five — measuring just the path column is a few ms even at 300
        # rows (confirmed), vs. the near-hang measuring every column
        # caused earlier when it ran on every keystroke.
        self.table.resizeColumnToContents(4)
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
    ("Lista de empleados", "employee_list"),
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
    (ProyectosInfoDetailPage)."""

    company_opened = Signal(int, str)  # (year, company)
    new_project_requested = Signal()

    _ALL_YEARS_LABEL = "Todos los años"

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
        self.new_project_button = QPushButton("+  Nuevo proyecto")
        self.new_project_button.setObjectName("PrimaryButton")
        self.new_project_button.setCursor(Qt.PointingHandCursor)
        self.new_project_button.clicked.connect(self.new_project_requested.emit)
        heading_row.addWidget(self.new_project_button)
        outer.addLayout(heading_row)

        subtitle = QLabel(
            f"Datos del archivo Excel de proyectos ({YEARS_WITH_DATA[0]}-{YEARS_WITH_DATA[-1]}), "
            "agrupados por empresa/cliente. Haz doble clic en una empresa para ver "
            "sus proyectos."
        )
        subtitle.setObjectName("MutedText")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(10)
        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("Filtrar por nombre de proyecto...")
        self.filter_box.textChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self.filter_box, 1)

        self.year_filter = QComboBox()
        self.year_filter.addItem(self._ALL_YEARS_LABEL)
        self.year_filter.addItems(list(reversed(YEARS_WITH_DATA)))  # newest first, matches the tree
        self.year_filter.setMinimumWidth(130)
        self.year_filter.currentIndexChanged.connect(self._apply_filter)
        filter_row.addWidget(self.year_filter)
        outer.addLayout(filter_row)

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
        # Wide enough for the longest pill text ("Sin estado") without
        # Qt squeezing the pill widget to fit the column, which distorts
        # it into an illegible sliver instead of just clipping cleanly.
        self.tree.setColumnWidth(0, 160)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        outer.addWidget(self.tree, 1)

        self._rows: list[dict] = []
        self._error: str | None = None

    def refresh(self) -> None:
        """Called every time this page is navigated to — NOT just when
        the underlying data actually changed. data_service.
        project_info_rows() returns the exact same list object (not just
        an equal one) when nothing's changed since the last read, so
        comparing by identity (`is`) below skips rebuilding ~370+ tree
        items and status pills on every single visit to this page, only
        doing it when there's something new to show."""
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
        _on_item_double_clicked / company_opened)."""
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()

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
            year_items.append(year_item)

        self.tree.addTopLevelItems(year_items)
        for year_item in year_items:
            year_item.setExpanded(True)

        self.tree.resizeColumnToContents(1)
        self.tree.resizeColumnToContents(2)
        self.tree.setUpdatesEnabled(True)
        self._apply_filter()

    def _on_filter_changed(self) -> None:
        self._filter_timer.start()  # restarts the 150ms countdown on every keystroke

    def _apply_filter(self) -> None:
        """Shows/hides the tree's EXISTING items to match the filter box
        and the year dropdown — no items are created or destroyed here,
        which is what keeps this fast enough to run on every keystroke
        (after the debounce) even with several thousand rows across 19
        years."""
        self.tree.setUpdatesEnabled(False)
        filter_text = self.filter_box.text().strip().lower()
        year_filter = self.year_filter.currentText()
        total_visible = 0

        for i in range(self.tree.topLevelItemCount()):
            year_item = self.tree.topLevelItem(i)
            if year_filter != self._ALL_YEARS_LABEL and year_item.text(0) != year_filter:
                year_item.setHidden(True)
                continue
            year_visible_count = 0
            for j in range(year_item.childCount()):
                child = year_item.child(j)
                matches = not filter_text or filter_text in child.text(1).lower()
                child.setHidden(not matches)
                if matches:
                    year_visible_count += 1
            year_item.setHidden(year_visible_count == 0)
            total_visible += year_visible_count

        self.count_label.setText(
            self._error or f"{total_visible} empresa(s)"
        )
        self.tree.setUpdatesEnabled(True)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
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

        self._clear_card()
        self.card.setVisible(True)
        self.categories_panel.setVisible(True)
        row_idx = 0

        # Always shown first, even with no data yet — see
        # ALWAYS_SHOWN_FIELDS' comment above.
        for label_text, key in ALWAYS_SHOWN_FIELDS:
            value = data.get(key)
            label = QLabel(label_text + ":")
            label.setObjectName("MetricTitle")
            label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            value_label = QLabel(str(value) if value not in (None, "") else "Sin datos todavía")
            value_label.setWordWrap(True)
            if value in (None, ""):
                value_label.setObjectName("MutedText")
            self.card_layout.addWidget(label, row_idx, 0)
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

        # categories_panel stays out of this mode entirely — it saves
        # each field the instant you click it (see
        # _on_category_toggled), so it's hidden alongside `card` while
        # there's a batch of OTHER unsaved edits in progress here, to
        # avoid any confusion about what's saved and what isn't.
        self.card.setVisible(False)
        self.categories_panel.setVisible(False)
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

    def reset(self) -> None:
        """Blank every field, called each time the page is opened —
        otherwise the previous project's now-created data would still
        be sitting in the form."""
        self._year_combo.setCurrentText(YEARS_WITH_DATA[-1])
        for field in self._new_inputs.values():
            field.clear()
        for field in self._new_multiline.values():
            field.clear()
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


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Índice de Proyectos")
        self.resize(1360, 840)
        self.setMinimumSize(1080, 680)
        self.refresh_worker: RefreshWorker | None = None

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
        for page in (
            self.projects_page,
            self.proyectos_info_page,
            self.search_page,
            self.ofertas_page,
            self.project_page,
            self.proyectos_info_detail_page,
            self.proyectos_info_new_page,
            self.proyectos_info_company_page,
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
        self.proyectos_info_company_page.back_button.clicked.connect(self._go_back)
        self.proyectos_info_company_page.project_info_opened.connect(self.open_project_info)
        self.proyectos_info_detail_page.back_button.clicked.connect(self._go_back)
        self.proyectos_info_page.new_project_requested.connect(self.open_new_project)
        self.proyectos_info_new_page.back_button.clicked.connect(self._go_back)
        # A freshly created project opens straight into its own detail
        # page rather than back to the form — see open_new_project_result.
        self.proyectos_info_new_page.project_created.connect(self.open_new_project_result)

        self._navigate(0)
        self.refresh_all()

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
        buttons = [
            ("▦  Proyectos", 0),
            ("ℹ️  Proyectos Info", 1),
            ("🔍  Buscar", 2),
            ("📄  Ofertas", 3),
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
    }

    def _navigate(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        title, subtitle = self._PAGE_TITLES[index]
        self.top_title.setText(title)
        self.top_subtitle.setText(subtitle)
        if index in (0, 1, 2, 3):
            for button, button_index in zip(self.nav_buttons, (0, 1, 2, 3)):
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
            self.statusBar().showMessage("Datos actualizados", 4000)
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

        self.refresh_worker = RefreshWorker(self)
        self.refresh_worker.stage_changed.connect(self._refresh_stage_changed)
        self.refresh_worker.finished_with_result.connect(self._refresh_finished)
        self.refresh_worker.start()

    def _refresh_stage_changed(self, stage: str) -> None:
        label = STAGE_LABELS.get(stage, stage)
        self.projects_page.set_processing(True, label)
        self.statusBar().showMessage(label)

    def _refresh_finished(self, success: bool, message: str, stats: dict) -> None:
        self.refresh_button.setEnabled(True)
        self.projects_page.set_processing(False)
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