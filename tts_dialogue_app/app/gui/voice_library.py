"""Voice Library browser dialog.

Lets the user search the huge public ElevenLabs Voice Library
(``/v1/shared-voices``) filtered by language / gender / age / category / text,
preview a voice, and **Add** it to the account so it becomes usable for TTS.

Added voices are emitted via the :pyattr:`voice_added` signal (carrying a
:class:`Voice` with the new account ``voice_id``) so the main window can append
them to its voice list and the per-character dropdowns.
"""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import (
    LIBRARY_AGES,
    LIBRARY_CATEGORIES,
    LIBRARY_GENDERS,
    LIBRARY_LANGUAGES,
)
from app.core.models import Voice
from app.gui.workers import AddSharedVoiceWorker, LoadSharedVoicesWorker

# result columns
C_NAME = 0
C_DESC = 1
C_LANG = 2
C_ACCENT = 3
C_CATEGORY = 4
C_PREVIEW = 5
C_ADD = 6
HEADERS = ["Name", "Gender · Age", "Language", "Accent", "Category", "Preview", "Add"]


class VoiceLibraryDialog(QDialog):
    voice_added = Signal(object)  # emits a Voice with the new account voice_id

    def __init__(self, api_key: str, log=None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Voice Library — browse & add voices")
        self.resize(940, 620)
        self.api_key = api_key
        self._log = log or (lambda *_: None)

        self._page = 0
        self._has_more = False
        self._results: list[Voice] = []
        self._load_worker: Optional[LoadSharedVoicesWorker] = None
        self._add_workers: list[AddSharedVoiceWorker] = []

        # preview player
        self._player = QMediaPlayer()
        self._audio = QAudioOutput()
        self._player.setAudioOutput(self._audio)

        self._build_ui()

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ---- filter row ----
        filt = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search (name / style / keyword)...")
        self.search_edit.returnPressed.connect(self.on_search)
        filt.addWidget(self.search_edit, 1)

        self.lang_combo = self._combo(LIBRARY_LANGUAGES)
        self.gender_combo = self._combo(LIBRARY_GENDERS)
        self.age_combo = self._combo(LIBRARY_AGES)
        self.cat_combo = self._combo(LIBRARY_CATEGORIES)
        for label, combo in (
            ("Language:", self.lang_combo), ("Gender:", self.gender_combo),
            ("Age:", self.age_combo), ("Category:", self.cat_combo),
        ):
            filt.addWidget(QLabel(label))
            filt.addWidget(combo)

        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self.on_search)
        filt.addWidget(self.search_btn)
        layout.addLayout(filt)

        # ---- results table ----
        self.table = QTableWidget()
        self.table.setColumnCount(len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(C_NAME, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        # ---- footer ----
        footer = QHBoxLayout()
        self.status_label = QLabel("Choose filters and click Search.")
        footer.addWidget(self.status_label, 1)
        self.load_more_btn = QPushButton("Load more")
        self.load_more_btn.setEnabled(False)
        self.load_more_btn.clicked.connect(self.on_load_more)
        footer.addWidget(self.load_more_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        layout.addLayout(footer)

    def _combo(self, options: list[tuple[str, str]]) -> QComboBox:
        c = QComboBox()
        for label, value in options:
            c.addItem(label, value)
        return c

    # ------------------------------------------------------------------ #
    # Search / pagination
    # ------------------------------------------------------------------ #
    def _filters(self) -> dict:
        return {
            "search": self.search_edit.text().strip(),
            "language": self.lang_combo.currentData(),
            "gender": self.gender_combo.currentData(),
            "age": self.age_combo.currentData(),
            "category": self.cat_combo.currentData(),
        }

    def on_search(self) -> None:
        self._page = 0
        self._results = []
        self.table.setRowCount(0)
        self._fetch()

    def on_load_more(self) -> None:
        self._page += 1
        self._fetch()

    def _fetch(self) -> None:
        if self._load_worker and self._load_worker.isRunning():
            return
        self.search_btn.setEnabled(False)
        self.load_more_btn.setEnabled(False)
        self.status_label.setText("Searching the Voice Library...")
        self._load_worker = LoadSharedVoicesWorker(self.api_key, self._filters(), self._page)
        self._load_worker.success.connect(self._on_results)
        self._load_worker.failed.connect(self._on_failed)
        self._load_worker.start()

    def _on_results(self, voices: list, has_more: bool) -> None:
        self.search_btn.setEnabled(True)
        self._has_more = has_more
        self.load_more_btn.setEnabled(has_more)
        for v in voices:
            self._results.append(v)
            self._add_result_row(v)
        self.status_label.setText(
            f"{len(self._results)} voice(s) shown" + (" — more available" if has_more else "")
        )
        self._log(f"Voice Library: loaded {len(voices)} voice(s) (page {self._page}).")

    def _on_failed(self, msg: str) -> None:
        self.search_btn.setEnabled(True)
        self.status_label.setText("Search failed.")
        self._log("Voice Library error: " + msg)
        QMessageBox.critical(self, "Voice Library failed", msg)

    def _add_result_row(self, v: Voice) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, C_NAME, QTableWidgetItem(v.name))
        self.table.setItem(row, C_DESC, QTableWidgetItem(v.descriptor()))
        self.table.setItem(row, C_LANG, QTableWidgetItem(v.language() or "—"))
        self.table.setItem(row, C_ACCENT, QTableWidgetItem(v.labels.get("accent", "—")))
        self.table.setItem(row, C_CATEGORY, QTableWidgetItem(v.category or "—"))

        prev = QPushButton("▶")
        prev.setEnabled(bool(v.preview_url))
        prev.clicked.connect(lambda _=False, url=v.preview_url: self._preview(url))
        self.table.setCellWidget(row, C_PREVIEW, prev)

        add = QPushButton("➕ Add")
        add.clicked.connect(lambda _=False, voice=v, b=add: self._add_voice(voice, b))
        self.table.setCellWidget(row, C_ADD, add)

    # ------------------------------------------------------------------ #
    # Preview & add
    # ------------------------------------------------------------------ #
    def _preview(self, url: Optional[str]) -> None:
        if not url:
            return
        self._player.stop()
        # preview_url is a public https link — QMediaPlayer can stream it directly
        self._player.setSource(QUrl(url))
        self._player.play()

    def _add_voice(self, voice: Voice, button: QPushButton) -> None:
        name, ok = QInputDialog.getText(
            self, "Add voice", "Name for this voice in your account:", text=voice.name
        )
        if not ok:
            return
        name = name.strip() or voice.name
        button.setEnabled(False)
        button.setText("Adding...")
        worker = AddSharedVoiceWorker(self.api_key, voice, name)
        worker.success.connect(lambda new_id, v, b=button: self._on_added(new_id, v, b))
        worker.failed.connect(lambda msg, b=button: self._on_add_failed(msg, b))
        self._add_workers.append(worker)  # keep ref
        worker.start()

    def _on_added(self, new_id: str, original: Voice, button: QPushButton) -> None:
        button.setText("✔ Added")
        # build a usable account Voice (new id) carrying the same labels
        added = Voice(
            voice_id=new_id,
            name=original.name,
            category="library",
            preview_url=original.preview_url,
            labels=dict(original.labels),
        )
        self._log(f"Added voice '{original.name}' to account (id {new_id}).")
        self.voice_added.emit(added)

    def _on_add_failed(self, msg: str, button: QPushButton) -> None:
        button.setEnabled(True)
        button.setText("➕ Add")
        self._log("Add voice failed: " + msg)
        QMessageBox.critical(self, "Add voice failed", msg)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._player.stop()
        event.accept()
