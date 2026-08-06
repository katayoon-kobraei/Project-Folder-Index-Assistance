from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from desktop_app.styles import APP_STYLESHEET


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Indice de Proyectos")
    app.setOrganizationName("INGEVIA")
    app.setStyleSheet(APP_STYLESHEET)

    from desktop_app.main_window import MainWindow

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
