"""
Visual language copied from the INGEVIA Email Assistant desktop app
(dark sidebar, colored metric cards, rounded panels), adapted for
Indice de Proyectos: object names and structure match that reference
app 1:1 so the two look and feel like siblings, with a teal accent
(#0F6E56) added as a fourth metric-card tone to match this project's
existing timeline color from the web version.
"""

APP_STYLESHEET = r"""
* {
    font-family: "Segoe UI";
    font-size: 13px;
    color: #172033;
}
QMainWindow, QWidget#AppRoot {
    background: #f4f7fb;
}

/* QScrollArea (Proyectos Info detail/Editar/Nuevo proyecto's scrollable
   body) — without an explicit rule here, the scroll area's internal
   viewport widget doesn't inherit the page's background and instead
   falls back to a dark OS-theme default, showing up as a black band
   wherever it peeks between/around the panels sitting inside it. Same
   root cause as the earlier QTreeWidget dark-background issue. */
QScrollArea {
    background: #f4f7fb;
    border: none;
}
QScrollArea > QWidget > QWidget {
    background: #f4f7fb;
}

/* Sidebar */
QFrame#Sidebar {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #102a43,
        stop: 0.55 #173b67,
        stop: 1 #0b2239
    );
    border: none;
}
QLabel#BrandTitle {
    color: white;
    font-size: 19px;
    font-weight: 800;
    letter-spacing: 1px;
}
QLabel#BrandSubtitle {
    color: #b8d6f2;
    font-size: 11px;
}
QPushButton#NavButton {
    color: #d8e7f5;
    background: transparent;
    border: none;
    border-left: 4px solid transparent;
    border-radius: 8px;
    padding: 11px 14px;
    text-align: left;
    font-weight: 650;
}
QPushButton#NavButton:hover {
    background: rgba(255, 255, 255, 0.10);
    color: white;
}
QPushButton#NavButton:checked {
    background: #0F6E56;
    color: white;
    border-left: 4px solid #7dd3fc;
}
QLabel#SidebarStatus {
    color: #b8d6f2;
    padding: 6px 12px;
}

/* Header and page titles */
QFrame#TopBar {
    background: white;
    border-bottom: 1px solid #e2e8f0;
}
QLabel#PageTitle {
    font-size: 23px;
    font-weight: 800;
    color: #102a43;
}
QLabel#PageSubtitle {
    color: #62748a;
}
QLabel#SectionTitle {
    font-size: 16px;
    font-weight: 750;
    color: #183b56;
}
QLabel#MutedText { color: #718096; }

/* Buttons */
QPushButton#PrimaryButton {
    background: #0F6E56;
    color: white;
    border: none;
    border-radius: 9px;
    padding: 10px 16px;
    font-weight: 750;
}
QPushButton#PrimaryButton:hover { background: #0c5a46; }
QPushButton#PrimaryButton:pressed { background: #094836; }
QPushButton#PrimaryButton:disabled { background: #a3c9bf; }

QPushButton#SecondaryButton {
    background: white;
    color: #253b53;
    border: 1px solid #cbd7e5;
    border-radius: 9px;
    padding: 9px 14px;
    font-weight: 650;
}
QPushButton#SecondaryButton:hover {
    background: #edf5ff;
    border-color: #8dbcf3;
    color: #1f6fd1;
}
QPushButton#LinkButton {
    background: transparent;
    color: #0F6E56;
    border: none;
    font-weight: 700;
    text-align: left;
    padding: 4px 0px;
}
QPushButton#LinkButton:hover { color: #094836; text-decoration: underline; }

QPushButton#DangerButton {
    background: white;
    color: #b3261e;
    border: 1px solid #f3c6c2;
    border-radius: 9px;
    padding: 9px 14px;
    font-weight: 650;
}
QPushButton#DangerButton:hover {
    background: #fdecea;
    border-color: #e1897f;
}

/* Panels */
QFrame#Panel {
    background: white;
    border: 1px solid #e0e7ef;
    border-radius: 13px;
}
QFrame#HeroPanel {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 #eaf7f3,
        stop: 0.55 #f5faff,
        stop: 1 #eefbf5
    );
    border: 1px solid #cfe8de;
    border-radius: 14px;
}

/* Metric cards */
QFrame#MetricCard {
    background: white;
    border: 1px solid #e0e7ef;
    border-radius: 13px;
}
QFrame#MetricCard[tone="teal"] {
    background: #eaf7f3;
    border: 1px solid #a9dcc9;
    border-left: 5px solid #0F6E56;
}
QFrame#MetricCard[tone="blue"] {
    background: #edf5ff;
    border: 1px solid #b9d7fb;
    border-left: 5px solid #2f80ed;
}
QFrame#MetricCard[tone="purple"] {
    background: #f5f0ff;
    border: 1px solid #d8c8ff;
    border-left: 5px solid #534AB7;
}
QFrame#MetricCard[tone="orange"] {
    background: #fff8e8;
    border: 1px solid #f7d58a;
    border-left: 5px solid #f59e0b;
}
QLabel#MetricIcon {
    border-radius: 9px;
    font-size: 18px;
    font-weight: 800;
    padding: 6px;
}
QLabel#MetricIcon[tone="teal"] { background: #cdeee1; color: #0c5a46; }
QLabel#MetricIcon[tone="blue"] { background: #dbeafe; color: #1d4ed8; }
QLabel#MetricIcon[tone="purple"] { background: #ede9fe; color: #534AB7; }
QLabel#MetricIcon[tone="orange"] { background: #ffedc2; color: #b45309; }
QLabel#MetricValue {
    font-size: 30px;
    font-weight: 800;
    color: #102a43;
}
QLabel#MetricValue[tone="teal"] { color: #0c5a46; }
QLabel#MetricValue[tone="blue"] { color: #1f6fd1; }
QLabel#MetricValue[tone="purple"] { color: #534AB7; }
QLabel#MetricValue[tone="orange"] { color: #b76100; }
QLabel#MetricTitle {
    color: #42566f;
    font-weight: 700;
}
QLabel#MetricHint {
    color: #74869b;
    font-size: 11px;
}

/* Status pills */
QLabel#SuccessPill {
    color: #087443;
    background: #e8f7ef;
    border: 1px solid #b9e8cf;
    border-radius: 10px;
    padding: 4px 9px;
    font-weight: 700;
}
QLabel#WarningPill {
    color: #9a5300;
    background: #fff2d8;
    border: 1px solid #f5d496;
    border-radius: 10px;
    padding: 4px 9px;
    font-weight: 700;
}
QLabel#ErrorPill {
    color: #b42318;
    background: #ffebe9;
    border: 1px solid #fecaca;
    border-radius: 10px;
    padding: 4px 9px;
    font-weight: 700;
}
QLabel#NeutralPill {
    color: #3a4b5c;
    background: #eef2f6;
    border: 1px solid #dbe3ea;
    border-radius: 10px;
    padding: 4px 9px;
    font-weight: 700;
}
QLabel#InfoPill {
    color: #1d4ed8;
    background: #dbeafe;
    border: 1px solid #bfdbfe;
    border-radius: 10px;
    padding: 4px 9px;
    font-weight: 700;
}

/* Timeline dots (project detail page) */
QFrame#TimelineDotMain {
    background: #0F6E56;
    border-radius: 5px;
}
QFrame#TimelineDotPhase {
    background: #534AB7;
    border-radius: 5px;
}
QFrame#TimelineDotEmpty {
    background: #e5e7eb;
    border-radius: 4px;
}
QLabel#TimelineYear {
    color: #9aa7b5;
    font-size: 10px;
}
QLabel#TimelineCode {
    color: #9aa7b5;
    font-size: 10px;
}

/* Location/site pill on project detail */
QPushButton#LocationPill {
    background: #eef2f6;
    color: #34506c;
    border: 1px solid #dbe3ea;
    border-radius: 10px;
    padding: 5px 10px;
    font-weight: 650;
}
QPushButton#LocationPill:hover {
    background: #eaf7f3;
    border-color: #a9dcc9;
    color: #0c5a46;
}

/* Inputs */
QLineEdit, QComboBox {
    background: white;
    border: 1px solid #cfd9e6;
    border-radius: 9px;
    padding: 8px 10px;
    min-height: 20px;
    selection-background-color: #cdeee1;
}
QLineEdit:hover, QComboBox:hover { border-color: #8fc9b6; }
QLineEdit:focus, QComboBox:focus {
    border: 2px solid #0F6E56;
    padding: 7px 9px;
}
QTextEdit {
    background: white;
    border: 1px solid #cfd9e6;
    border-radius: 9px;
    padding: 6px 9px;
    selection-background-color: #cdeee1;
}
QTextEdit:focus { border: 2px solid #0F6E56; }

/* Radio buttons (Editar form's Planning/Tipo/Subtipo1/Subtipo2 pickers)
   — the indicator is drawn explicitly rather than left to the OS theme.
   Without this, some Windows setups render the native radio glyph as a
   tiny illegible smudge next to the label at this font size. */
QRadioButton {
    spacing: 10px;
    padding: 4px 0;
    color: #172033;
    font-size: 13px;
}
QRadioButton::indicator {
    width: 16px;
    height: 16px;
    border-radius: 8px;
    border: 2px solid #cfd9e6;
    background: white;
}
QRadioButton::indicator:hover { border-color: #8fc9b6; }
QRadioButton::indicator:checked {
    border: 2px solid #0F6E56;
    background: qradialgradient(
        cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0 #0F6E56, stop:0.55 #0F6E56, stop:0.65 white, stop:1 white
    );
}

/* Dotted separator between the Editar form's category pickers */
QFrame#DottedSeparator {
    border: none;
    border-top: 1px dotted #cfd9e6;
    background: transparent;
}

/* Tables */
QTableWidget {
    background: white;
    border: 1px solid #e0e7ef;
    border-radius: 11px;
    gridline-color: #edf1f6;
    selection-background-color: #d7f0e6;
    selection-color: #102a43;
    alternate-background-color: #f8fbff;
}
QHeaderView::section {
    background: #eaf2fb;
    color: #34506c;
    border: none;
    border-bottom: 1px solid #cfdceb;
    padding: 9px;
    font-weight: 750;
}
QTableWidget::item { padding: 7px; }
QTableWidget::item:hover { background: #f0f9f6; }

/* Tree (Proyectos Info hierarchy) — without this, QTreeWidget falls back
   to the OS's own theme instead of this stylesheet's colors, which on a
   system with Windows dark mode on means a near-black background with
   the app's dark navy text on top of it: unreadable. Same white/teal
   language as QTableWidget above, just for the tree instead. */
QTreeWidget {
    background: white;
    border: 1px solid #e0e7ef;
    border-radius: 11px;
    alternate-background-color: #f8fbff;
    selection-background-color: #d7f0e6;
    selection-color: #102a43;
    outline: none;
}
QTreeWidget::item {
    padding: 6px;
    border: none;
}
QTreeWidget::item:hover { background: #f0f9f6; }
QTreeWidget::branch { background: white; }

/* Progress and scrollbars */
QProgressBar {
    background: #dbe7f2;
    border: none;
    border-radius: 4px;
    min-height: 8px;
    max-height: 8px;
}
QProgressBar::chunk {
    background: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 0, stop: 0 #0F6E56, stop: 1 #2f80ed);
    border-radius: 4px;
}
QScrollBar:vertical {
    width: 10px;
    background: transparent;
}
QScrollBar::handle:vertical {
    background: #b8c6d8;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover { background: #8ea6c2; }
QStatusBar {
    background: white;
    color: #68758c;
    border-top: 1px solid #e1e7f0;
}
QToolTip {
    background: #17324d;
    color: white;
    border: none;
    padding: 6px;
}
"""