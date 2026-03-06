from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from nightfall_desktop.ui.main_window import MainWindow
from nightfall_desktop.ui.theme import TOKYO_DARK_STYLESHEET


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyleSheet(TOKYO_DARK_STYLESHEET)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

