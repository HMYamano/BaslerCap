"""Entry point for the Basler Recorder GUI app.

Usage (Windows):

    python main.py

The app does not take CLI arguments (per spec — no argparse). Configure
everything via the GUI.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import sys

from PySide6.QtWidgets import QApplication

from basler_recorder.gui.main_window import MainWindow


def main():
    # Required on Windows for multiprocessing.Process(target=...) to work when
    # frozen or run from PyInstaller. Safe to call unconditionally.
    mp.freeze_support()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    app = QApplication(sys.argv)
    app.setApplicationName("BaslerRecorder")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
