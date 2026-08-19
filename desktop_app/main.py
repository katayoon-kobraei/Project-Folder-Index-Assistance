from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from desktop_app.styles import APP_STYLESHEET

_ICON_PATH = Path(__file__).parent / "assets" / "app.ico"


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Indice de Proyectos")
    app.setOrganizationName("INGEVIA")
    app.setStyleSheet(APP_STYLESHEET)
    # Guarded by existence, not just try/except QIcon(...) — QIcon silently
    # accepts a bad path and just renders blank, so this is what actually
    # tells a dev running from a checkout without the generated icon (or
    # any future stripped-down install) apart from a real asset problem.
    if _ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(_ICON_PATH)))

    from desktop_app.main_window import MainWindow

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
