"""Entry point for the TTS Dialogue App.

Run from source:
    python main.py

Build a Windows .exe:
    build_exe.bat
"""

from __future__ import annotations

import os
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

# Make the package importable both from source and when frozen by PyInstaller.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config.settings import get_app_dir  # noqa: E402
from app.core.audio_processor import configure_ffmpeg  # noqa: E402
from app.core.crash_log import setup_crash_logging  # noqa: E402
from app.core.win_utils import suppress_subprocess_windows  # noqa: E402
from app.gui.main_window import MainWindow  # noqa: E402


def _resource_path(rel: str) -> str:
    """Resolve a bundled resource path (works under PyInstaller --onefile)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def main() -> int:
    # Log uncaught errors / hard crashes to error.log next to the app, and keep
    # the window alive instead of vanishing silently.
    setup_crash_logging(get_app_dir())
    # Windows: stop ffmpeg/ffprobe from flashing console windows during convert.
    suppress_subprocess_windows()
    # Let pydub find a bundled ffmpeg.exe if the user dropped one next to the app.
    configure_ffmpeg(get_app_dir())

    app = QApplication(sys.argv)
    app.setApplicationName("TTS Dialogue App")

    icon_path = _resource_path(os.path.join("assets", "icon.ico"))
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
