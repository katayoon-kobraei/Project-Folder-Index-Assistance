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
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
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
            self.table.setItem(row_idx, 1, QTableWidgetItem(str(project.get("first_seen_year") or "?")))
            self.table.setItem(row_idx, 2, QTableWidgetItem(str(project.get("last_seen_year") or "?")))
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
    ("Cliente", "client"),
    ("Ubicación", "location"),
    ("Año", "year"),
    ("Empleados asignados actualmente", "employees_assigned"),
    ("Fecha del proyecto", "start_date"),
    ("Comienzo de la obra", "work_start_date"),
    ("Fecha límite (fin de la obra)", "deadline"),
    ("Categoría de tipo de trabajo", "work_type"),
    ("Categoría de tipo de proyecto", "project_type"),
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

# Colored pill for the PLANNING column — shown as a badge next to each
# project (in the list AND on its detail page), not as a plain text row
# among the other fields, since it's the one status every single project
# in the 2024-2026 data actually has filled in (confirmed by hand).
_PLANNING_PILL_STYLES = {
    "Acabado": "SuccessPill",
    "Cancelado": "ErrorPill",
    "Proceso": "WarningPill",
    "DO": "NeutralPill",
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
    """Year -> project hierarchy read from the hand-maintained 'LISTADO
    PROYECTOS POR AÑOS' workbook (project_info_path in config.yaml) — a
    completely separate data source from the crawled P: index the rest
    of the app uses. Only 2024, 2025, and 2026 currently have real data
    in that file (see src/project_info/reader.py), so those are the only
    years that can ever appear here. The list shows each project's raw
    NOMBRE value exactly as it reads in the workbook (no derived company
    prefix), plus its PLANNING status as a small colored pill (every
    2024-2026 row has this filled in) — clicking a row opens its own
    detail card (ProyectosInfoDetailPage), not a table."""

    project_info_opened = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 22, 26, 26)
        outer.setSpacing(14)

        heading = QLabel("Proyectos Info")
        heading.setObjectName("SectionTitle")
        outer.addWidget(heading)

        subtitle = QLabel(
            "Datos del archivo Excel de proyectos (2024, 2025 y 2026 — únicos años "
            "con datos disponibles). Haz doble clic en un proyecto para ver su ficha."
        )
        subtitle.setObjectName("MutedText")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("Filtrar por nombre de proyecto...")
        self.filter_box.textChanged.connect(self._render)
        outer.addWidget(self.filter_box)

        self.count_label = QLabel("")
        self.count_label.setObjectName("MutedText")
        outer.addWidget(self.count_label)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Proyecto", "Estado"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(True)
        # Full NOMBRE value, not truncated to the visible width — same
        # idea as Ruta/Carpeta elsewhere in the app: let the column grow
        # past the window edge and rely on the tree's own scrollbar. The
        # Estado column stays a small fixed width for its colored pill.
        self.tree.header().setStretchLastSection(False)
        self.tree.setColumnWidth(1, 110)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        outer.addWidget(self.tree, 1)

        self._rows: list[dict] = []
        self._error: str | None = None

    def refresh(self) -> None:
        try:
            self._rows = data_service.project_info_rows()
            self._error = None
        except Exception as exc:
            self._rows = []
            self._error = str(exc)
        self._render()

    def _render(self) -> None:
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()

        if self._error:
            self.count_label.setText(self._error)
            self.tree.setUpdatesEnabled(True)
            return

        filter_text = self.filter_box.text().strip().lower()
        by_year: dict[int, list[dict]] = {}
        for row in self._rows:
            if filter_text and filter_text not in (row["project_name"] or "").lower():
                continue
            by_year.setdefault(row["year"], []).append(row)

        total = sum(len(rows) for rows in by_year.values())
        self.count_label.setText(f"{total} proyecto(s)")

        for year in sorted(by_year.keys(), reverse=True):
            year_item = QTreeWidgetItem([str(year)])
            bold_font = year_item.font(0)
            bold_font.setBold(True)
            year_item.setFont(0, bold_font)
            self.tree.addTopLevelItem(year_item)
            # NOT re-sorted — kept in the same order the rows appear in
            # the source spreadsheet (by_year[year] is already in that
            # order, since reader.py appends rows as it reads them and
            # never sorts). The sheet's own order is meaningful: several
            # rows can share one project name, each representing a
            # different document/task for it, in a specific sequence.
            for row in by_year[year]:
                child = QTreeWidgetItem([row["project_name"] or ""])
                child.setData(0, Qt.UserRole, row["id"])
                year_item.addChild(child)
                self.tree.setItemWidget(child, 1, _make_status_pill(row.get("status")))
            year_item.setExpanded(True)

        self.tree.resizeColumnToContents(0)
        self.tree.setUpdatesEnabled(True)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        item_id = item.data(0, Qt.UserRole)
        if item_id is not None:
            self.project_info_opened.emit(item_id)


class ProyectosInfoDetailPage(QWidget):
    """Card-style detail view for one row from the project info workbook
    — a label/value grid, not a table. Only fields that actually have a
    value in the workbook are shown; a field that's blank for this
    particular project (e.g. most rows have no TIPO, and NONE currently
    have Empleados/Fecha límite — see reader.py's module docstring) is
    left out of the card entirely rather than shown as empty, per the
    explicit request. The card is rebuilt from scratch on every load()
    since which fields are present varies row to row."""

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
        self.title_label.setWordWrap(True)
        outer.addWidget(self.title_label)

        self.status_pill = QLabel("")
        self.status_pill.setVisible(False)
        outer.addWidget(self.status_pill, alignment=Qt.AlignLeft)

        self.empty_label = QLabel("Este proyecto no tiene ningún otro dato en el archivo.")
        self.empty_label.setObjectName("MutedText")
        self.empty_label.setVisible(False)
        outer.addWidget(self.empty_label)

        self.card = QFrame()
        self.card.setObjectName("Panel")
        self.card_layout = QGridLayout(self.card)
        self.card_layout.setContentsMargins(22, 20, 22, 20)
        self.card_layout.setHorizontalSpacing(18)
        self.card_layout.setVerticalSpacing(14)
        self.card_layout.setColumnStretch(1, 1)
        outer.addWidget(self.card)
        outer.addStretch()

        self._item_id: int | None = None

    def _clear_card(self) -> None:
        while self.card_layout.count():
            item = self.card_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def load(self, item_id: int) -> None:
        self._item_id = item_id
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

        self._clear_card()
        row_idx = 0
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

        self.card.setVisible(row_idx > 0)
        self.empty_label.setVisible(row_idx == 0)


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
        for page in (
            self.projects_page,
            self.proyectos_info_page,
            self.search_page,
            self.ofertas_page,
            self.project_page,
            self.proyectos_info_detail_page,
        ):
            self.stack.addWidget(page)
        main.addWidget(self.stack, 1)
        root_layout.addLayout(main, 1)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Listo")

        # Proyectos/Proyectos Info each open their own detail page, so
        # each back button returns wherever the user actually came from
        # (set right before navigating to the detail page) rather than
        # always going to one fixed page. Buscar doesn't open project
        # pages at all — its folder/file results open directly in
        # Explorer instead.
        self._project_return_index = 0
        self._project_info_return_index = 1
        self.projects_page.project_opened.connect(self.open_project)
        self.project_page.back_button.clicked.connect(
            lambda: self._navigate(self._project_return_index)
        )
        self.proyectos_info_page.project_info_opened.connect(self.open_project_info)
        self.proyectos_info_detail_page.back_button.clicked.connect(
            lambda: self._navigate(self._project_info_return_index)
        )

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
        1: ("Proyectos Info", "Ficha de cada proyecto según el Excel (2024-2026)"),
        2: ("Buscar", "Buscar por nombre de carpeta, dirección o archivo"),
        3: ("Ofertas", "Documentos Firmado y Pedido dentro de FACTURACION/INGEVIA en 02.-GESTIÓN"),
        4: ("Proyecto", ""),
        5: ("Proyecto Info", ""),
    }

    def _navigate(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        title, subtitle = self._PAGE_TITLES[index]
        self.top_title.setText(title)
        self.top_subtitle.setText(subtitle)
        for button, button_index in zip(self.nav_buttons, (0, 1, 2, 3)):
            button.setChecked(button_index == index)
        if index == 0:
            self.projects_page.refresh()
        elif index == 1:
            self.proyectos_info_page.refresh()
        elif index == 3:
            self.ofertas_page.refresh()

    def open_project(self, project_id: int) -> None:
        # Remember which page this was opened from, BEFORE switching away
        # from it, so the project page's back button returns there.
        self._project_return_index = self.stack.currentIndex()
        self.project_page.load(project_id)
        self._navigate(4)

    def open_project_info(self, item_id: int) -> None:
        self._project_info_return_index = self.stack.currentIndex()
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