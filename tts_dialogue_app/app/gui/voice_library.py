"""Inline Voice Finder widget.

Embedded directly in the Setup tab so the user can — right there — filter the
ElevenLabs Voice Library by **language / gender / age / region (miền Bắc/Trung/
Nam) / category / keyword**, **preview** voices, **Add** them to the account,
and **Save** favorites that persist across sessions (stored in ``config.json``).

Emits :pyattr:`voice_added` (a usable account :class:`Voice`) so the main window
merges it into the per-character voice dropdowns.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from typing import Callable, Optional

from PySide6.QtCore import QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QComboBox,
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
    LIBRARY_REGIONS,
    voice_matches_region,
)
from app.core.models import Voice
from app.gui.workers import (
    AddSharedVoiceWorker,
    LoadSharedVoicesWorker,
    PreviewDownloadWorker,
)

# result columns
C_NAME = 0
C_DESC = 1
C_LANG = 2
C_ACCENT = 3
C_CATEGORY = 4
C_PREVIEW = 5
C_ACTION = 6
HEADERS = ["Name", "Gender · Age", "Lang", "Accent / Region", "Category", "Preview", "Save / Add"]

# Keywords searched (in turn) when looking for child voices, and the tokens used
# to keep only genuinely child-like results (ElevenLabs has no 'child' age).
CHILD_KEYWORDS = ["child", "kid", "children", "cartoon", "childish", "young girl", "young boy"]
CHILD_TOKENS = ["child", "kid", "children", "toddler", "baby", "cartoon", "anime", "childish"]


class VoiceFinderWidget(QWidget):
    voice_added = Signal(object)  # Voice usable for TTS (account voice_id)

    def __init__(
        self,
        get_api_key: Callable[[], str],
        config,
        log: Optional[Callable[[str], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._get_api_key = get_api_key
        self.config = config
        self._log = log or (lambda *_: None)

        self._page = 0
        self._has_more = False
        self._mode = "search"  # "search" | "favorites"
        self._results: list[Voice] = []
        self._seen: set[str] = set()
        self._child_queue: list[str] = []
        self._load_worker: Optional[LoadSharedVoicesWorker] = None
        self._add_workers: list[AddSharedVoiceWorker] = []
        self._preview_worker: Optional[PreviewDownloadWorker] = None
        self._preview_dir = os.path.join(tempfile.gettempdir(), "tts_voice_previews")
        # Retain every worker thread so a still-running QThread is never garbage
        # collected (that would abort the whole process).
        self._workers: list = []

        self._player = QMediaPlayer()
        self._audio = QAudioOutput()
        self._audio.setVolume(1.0)  # make sure preview is audible
        self._player.setAudioOutput(self._audio)
        self._player.errorOccurred.connect(
            lambda err, msg: self._log(f"Preview player error: {msg}")
        )

        self._build_ui()

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # filter row 1: search + language + region
        f1 = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search keyword (name / style)...")
        self.search_edit.returnPressed.connect(self.on_search)
        f1.addWidget(self.search_edit, 1)
        self.lang_combo = self._combo(LIBRARY_LANGUAGES)
        self.region_combo = self._combo(LIBRARY_REGIONS)
        f1.addWidget(QLabel("Language:"))
        f1.addWidget(self.lang_combo)
        f1.addWidget(QLabel("Region:"))
        f1.addWidget(self.region_combo)
        layout.addLayout(f1)

        # filter row 2: gender + age + category + buttons
        f2 = QHBoxLayout()
        self.gender_combo = self._combo(LIBRARY_GENDERS)
        self.age_combo = self._combo(LIBRARY_AGES)
        self.cat_combo = self._combo(LIBRARY_CATEGORIES)
        f2.addWidget(QLabel("Gender:"))
        f2.addWidget(self.gender_combo)
        f2.addWidget(QLabel("Age:"))
        f2.addWidget(self.age_combo)
        f2.addWidget(QLabel("Category:"))
        f2.addWidget(self.cat_combo)
        f2.addStretch(1)
        self.search_btn = QPushButton("🔍 Search Library")
        self.search_btn.clicked.connect(self.on_search)
        f2.addWidget(self.search_btn)
        self.child_btn = QPushButton("👶 Tìm giọng trẻ em")
        self.child_btn.setToolTip(
            "Tự tìm theo nhiều từ khóa (child, kid, cartoon...) và lọc ra giọng trẻ em"
        )
        self.child_btn.clicked.connect(self.find_child_voices)
        f2.addWidget(self.child_btn)
        self.fav_btn = QPushButton("★ Show Favorites")
        self.fav_btn.clicked.connect(self.show_favorites)
        f2.addWidget(self.fav_btn)
        layout.addLayout(f2)

        # results table
        self.table = QTableWidget()
        self.table.setColumnCount(len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(C_NAME, QHeaderView.Stretch)
        self.table.setMinimumHeight(180)
        layout.addWidget(self.table, 1)

        # footer
        footer = QHBoxLayout()
        self.status_label = QLabel("Filter and click Search, or Show Favorites.")
        footer.addWidget(self.status_label, 1)
        self.load_more_btn = QPushButton("Load more")
        self.load_more_btn.setEnabled(False)
        self.load_more_btn.clicked.connect(self.on_load_more)
        footer.addWidget(self.load_more_btn)
        layout.addLayout(footer)

    def _combo(self, options: list[tuple[str, str]]) -> QComboBox:
        c = QComboBox()
        for label, value in options:
            c.addItem(label, value)
        return c

    def _track(self, worker) -> None:
        """Keep a reference until the thread finishes so it isn't GC'd mid-run."""
        self._workers.append(worker)
        worker.finished.connect(lambda w=worker: self._workers.remove(w) if w in self._workers else None)

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
        key = self._get_api_key()
        if not key:
            QMessageBox.warning(self, "API Key", "Enter your API key first.")
            return
        self._mode = "search"
        self._page = 0
        self._results = []
        self.table.setRowCount(0)
        self._fetch()

    def on_load_more(self) -> None:
        self._page += 1
        self._fetch()

    # ------------------------------------------------------------------ #
    # Child-voice finder: run several keywords in turn, merge & filter
    # ------------------------------------------------------------------ #
    def find_child_voices(self) -> None:
        key = self._get_api_key()
        if not key:
            QMessageBox.warning(self, "API Key", "Enter your API key first.")
            return
        self._mode = "search"
        self.table.setRowCount(0)
        self._results = []
        self._seen = set()
        self._child_queue = list(CHILD_KEYWORDS)
        self.load_more_btn.setEnabled(False)
        self.search_btn.setEnabled(False)
        self.child_btn.setEnabled(False)
        self.status_label.setText("Đang tìm giọng trẻ em (chạy nhiều từ khóa)...")
        self._log("Voice Library: searching for child voices...")
        self._run_next_child()

    def _run_next_child(self) -> None:
        if not self._child_queue:
            self.search_btn.setEnabled(True)
            self.child_btn.setEnabled(True)
            n = self.table.rowCount()
            self.status_label.setText(
                f"Tìm thấy {n} giọng trẻ em." if n else
                "Không thấy giọng trẻ em khớp bộ lọc — thử bỏ lọc Language/Region rồi tìm lại."
            )
            self._log(f"Child-voice search done: {n} voice(s).")
            return
        kw = self._child_queue.pop(0)
        filters = self._filters()
        filters["search"] = kw
        filters["gender"] = ""  # search both genders for kids
        self.status_label.setText(f"Đang tìm '{kw}'... ({self.table.rowCount()} đã thấy)")
        self._load_worker = LoadSharedVoicesWorker(self._get_api_key(), filters, 0)
        self._load_worker.success.connect(self._on_child_results)
        self._load_worker.failed.connect(self._on_child_failed)
        self._track(self._load_worker)
        self._load_worker.start()

    def _on_child_results(self, voices: list, has_more: bool) -> None:
        region = self.region_combo.currentData()
        for v in voices:
            if v.voice_id in self._seen:
                continue
            blob = " ".join([
                v.labels.get("accent", ""), v.labels.get("description", ""),
                v.labels.get("descriptive", ""), v.labels.get("use_case", ""), v.name,
            ]).lower()
            # keep only genuinely child-like voices
            if not (v.is_child() or any(t in blob for t in CHILD_TOKENS)):
                continue
            if not voice_matches_region(blob, region):
                continue
            self._seen.add(v.voice_id)
            self._results.append(v)
            self._add_result_row(v)
        self._run_next_child()

    def _on_child_failed(self, msg: str) -> None:
        # don't abort the whole sweep on one keyword failure — log and continue
        self._log("Child search (one keyword) failed: " + msg)
        self._run_next_child()

    def _fetch(self) -> None:
        if self._load_worker and self._load_worker.isRunning():
            return
        self.search_btn.setEnabled(False)
        self.load_more_btn.setEnabled(False)
        self.status_label.setText("Searching the Voice Library...")
        self._load_worker = LoadSharedVoicesWorker(self._get_api_key(), self._filters(), self._page)
        self._load_worker.success.connect(self._on_results)
        self._load_worker.failed.connect(self._on_failed)
        self._track(self._load_worker)
        self._load_worker.start()

    def _on_results(self, voices: list, has_more: bool) -> None:
        self.search_btn.setEnabled(True)
        self._has_more = has_more
        self.load_more_btn.setEnabled(has_more)

        # client-side region filter (accent/description text)
        region = self.region_combo.currentData()
        shown = 0
        for v in voices:
            blob = " ".join([
                v.labels.get("accent", ""), v.labels.get("description", ""),
                v.labels.get("descriptive", ""), v.name,
            ])
            if not voice_matches_region(blob, region):
                continue
            self._results.append(v)
            self._add_result_row(v)
            shown += 1

        note = ""
        if region and shown == 0 and voices:
            note = " — no voices matched the region filter on this page (try 'Load more')"
        self.status_label.setText(
            f"{self.table.rowCount()} voice(s) shown" +
            (" — more available" if has_more else "") + note
        )
        self._log(f"Voice Library: page {self._page}, {len(voices)} fetched, {shown} after region filter.")

    def _on_failed(self, msg: str) -> None:
        self.search_btn.setEnabled(True)
        self.status_label.setText("Search failed.")
        self._log("Voice Library error: " + msg)
        QMessageBox.critical(self, "Voice Library failed", msg)

    # ------------------------------------------------------------------ #
    # Favorites view
    # ------------------------------------------------------------------ #
    def show_favorites(self) -> None:
        self._mode = "favorites"
        self.table.setRowCount(0)
        self._results = []
        self.load_more_btn.setEnabled(False)
        favs = [Voice.from_dict(d) for d in self.config.favorite_voices()]
        for v in favs:
            self._results.append(v)
            self._add_result_row(v)
        self.status_label.setText(f"{len(favs)} saved favorite voice(s).")

    # ------------------------------------------------------------------ #
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

        action = QPushButton()
        if self._mode == "favorites":
            action.setText("🗑 Remove")
        elif v.is_shared:
            action.setText("➕ Add & Save")
        else:
            action.setText("★ Saved" if self.config.is_favorite(v.voice_id) else "★ Save")
        action.clicked.connect(lambda _=False, voice=v, b=action: self._action(voice, b))
        self.table.setCellWidget(row, C_ACTION, action)

    # ------------------------------------------------------------------ #
    # Preview & actions
    # ------------------------------------------------------------------ #
    def _preview(self, url: Optional[str]) -> None:
        """Download the preview to a local temp file, then play it (reliable),
        caching by URL hash so repeats are instant."""
        if not url:
            self._log("This voice has no preview audio.")
            return
        name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16] + ".mp3"
        dest = os.path.join(self._preview_dir, name)
        if os.path.exists(dest):
            self._play_local(dest)
            return
        self.status_label.setText("Loading preview...")
        self._preview_worker = PreviewDownloadWorker(url, dest)
        self._preview_worker.success.connect(self._play_local)
        self._preview_worker.failed.connect(
            lambda m: (self._log("Preview download failed: " + m),
                       self._preview_stream(url))
        )
        self._track(self._preview_worker)
        self._preview_worker.start()

    def _play_local(self, path: str) -> None:
        self.status_label.setText("")
        self._player.stop()
        self._player.setSource(QUrl.fromLocalFile(os.path.abspath(path)))
        self._player.play()

    def _preview_stream(self, url: str) -> None:
        """Fallback: stream the URL directly if the download failed."""
        self._player.stop()
        self._player.setSource(QUrl(url))
        self._player.play()

    def _action(self, voice: Voice, button: QPushButton) -> None:
        # favorites view -> remove
        if self._mode == "favorites":
            self.config.remove_favorite_voice(voice.voice_id)
            button.setText("Removed")
            button.setEnabled(False)
            self._log(f"Removed favorite voice '{voice.name}'.")
            return

        # library voice -> add to account, then save as favorite
        if voice.is_shared:
            name, ok = QInputDialog.getText(
                self, "Add & Save voice",
                "Name for this voice in your account:", text=voice.name
            )
            if not ok:
                return
            name = name.strip() or voice.name
            button.setEnabled(False)
            button.setText("Adding...")
            worker = AddSharedVoiceWorker(self._get_api_key(), voice, name)
            worker.success.connect(lambda nid, v, b=button, nm=name: self._on_added(nid, v, b, nm))
            worker.failed.connect(lambda msg, b=button: self._on_add_failed(msg, b))
            self._add_workers.append(worker)
            worker.start()
            return

        # already an account voice -> just favorite it
        self.config.add_favorite_voice(voice.to_dict())
        button.setText("★ Saved")
        self.voice_added.emit(voice)
        self._log(f"Saved favorite voice '{voice.name}'.")

    def _on_added(self, new_id: str, original: Voice, button: QPushButton, name: str) -> None:
        added = Voice(
            voice_id=new_id,
            name=name,
            category="library",
            preview_url=original.preview_url,
            labels=dict(original.labels),
        )
        self.config.add_favorite_voice(added.to_dict())  # persist for next session
        button.setText("✔ Added & Saved")
        self._log(f"Added & saved voice '{name}' (id {new_id}).")
        self.voice_added.emit(added)

    def _on_add_failed(self, msg: str, button: QPushButton) -> None:
        button.setEnabled(True)
        button.setText("➕ Add & Save")
        self._log("Add voice failed: " + msg)
        QMessageBox.critical(self, "Add voice failed", msg)
