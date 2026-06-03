"""Crash & error logging.

A ``--windowed`` PyInstaller app has no console, so an uncaught Python exception
or a hard C/Qt fault makes the app vanish with no message. This module:

* enables :mod:`faulthandler` (captures hard crashes / segfaults),
* installs a ``sys.excepthook`` that logs Python tracebacks and shows a dialog,
* installs a Qt message handler (captures qWarning / qCritical / qFatal),

all written to ``error.log`` next to the app so problems are diagnosable.
"""

from __future__ import annotations

import datetime
import faulthandler
import os
import sys
import traceback
from typing import Optional

_log_file = None  # keep a module-level ref so the fd isn't closed/GC'd


def _timestamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def setup_crash_logging(app_dir: str) -> str:
    """Initialise crash/error logging. Returns the log file path."""
    global _log_file
    log_path = os.path.join(app_dir, "error.log")
    try:
        _log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return log_path

    _log_file.write(f"\n===== session start {_timestamp()} =====\n")
    _log_file.flush()

    # 1) hard crashes (segfault, abort) -> traceback of all threads
    try:
        faulthandler.enable(_log_file)
    except Exception:
        pass

    # 2) uncaught Python exceptions
    def _excepthook(exctype, value, tb) -> None:
        text = "".join(traceback.format_exception(exctype, value, tb))
        _log_file.write(f"\n[{_timestamp()}] UNCAUGHT EXCEPTION:\n{text}\n")
        _log_file.flush()
        _show_dialog(text)

    sys.excepthook = _excepthook

    # 3) Qt-side messages (warnings / fatal)
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        def _qt_handler(mode, context, message) -> None:
            level = {
                QtMsgType.QtDebugMsg: "QtDebug",
                QtMsgType.QtInfoMsg: "QtInfo",
                QtMsgType.QtWarningMsg: "QtWarning",
                QtMsgType.QtCriticalMsg: "QtCritical",
                QtMsgType.QtFatalMsg: "QtFatal",
            }.get(mode, "Qt")
            _log_file.write(f"[{_timestamp()}] {level}: {message}\n")
            _log_file.flush()

        qInstallMessageHandler(_qt_handler)
    except Exception:
        pass

    return log_path


def _show_dialog(text: str) -> None:
    """Best-effort error dialog (never raises)."""
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is None:
            return
        box = QMessageBox()
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("Đã xảy ra lỗi (app vẫn tiếp tục)")
        box.setText("Một lỗi vừa xảy ra. Chi tiết đã được ghi vào error.log.")
        box.setDetailedText(text[-3000:])
        box.exec()
    except Exception:
        pass


def log_error(message: str) -> None:
    """Write an arbitrary message to the error log (no-op if unavailable)."""
    if _log_file is not None:
        _log_file.write(f"[{_timestamp()}] {message}\n")
        _log_file.flush()
