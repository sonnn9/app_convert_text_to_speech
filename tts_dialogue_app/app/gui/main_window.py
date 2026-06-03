"""Main application window (v2).

Adds, on top of v1: convert modes (line / dialogue / auto), smart batching,
a Pronunciation tab, a model manager (capabilities from the API), usage / cost
estimation, scene grouping for video workflows, timeline (CSV/JSON) + SRT export,
a local audio cache, exponential-backoff retries, and richer audio
post-processing — all driven from QThread workers so the UI never freezes.
"""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction, QColor
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import (
    AVAILABLE_MODELS,
    AppConfig,
    CHARACTER_COLORS,
    DEFAULT_PRESET,
    PRESETS,
    get_cache_dir,
)
from app.core.audio_player import AudioPlayer
from app.core.audio_postprocessor import PostProcessOptions
from app.core.cache_manager import CacheManager
from app.core.models import (
    CharacterVoiceConfig,
    DialogueLine,
    LineStatus,
    ProjectSettings,
    Voice,
    VoiceSettings,
)
from app.core.parser import detect_characters, parse_dialogue
from app.core.pronunciation_manager import PronunciationManager, PronunciationRule
from app.core.project_manager import Project, ProjectManager
from app.core.usage_estimator import UsageEstimator
from app.core.batch_splitter import BatchSplitter
from app.gui.voice_library import VoiceFinderWidget
from app.gui.widgets import CharacterConfigTable
from app.gui.workers import (
    ConvertWorker,
    LoadModelsWorker,
    LoadVoicesWorker,
    PostProcessWorker,
    PreviewWorker,
    TestApiWorker,
)

PLACEHOLDER_DIALOGUE = (
    "Mom: Con muốn làm móng tay thì nói sao?\n"
    "Lucy: A manicure, please.\n"
    "Mom: Đúng rồi, a manicure, please.\n"
    "Lucy: A pedicure, please."
)

# Queue columns (Scene added for the video workflow)
Q_SCENE = 0
Q_INDEX = 1
Q_CHARACTER = 2
Q_TEXT = 3
Q_VOICE = 4
Q_STATUS = 5
Q_DURATION = 6
Q_FILE = 7
Q_PLAY = 8
Q_RETRY = 9
Q_HEADERS = [
    "Scene", "#", "Character", "Text", "Voice", "Status",
    "Duration", "Audio File", "Play", "Retry",
]

# Pronunciation columns
P_ORIGINAL = 0
P_REPLACEMENT = 1
P_NOTES = 2
P_ENABLED = 3
P_HEADERS = ["Original word", "Pronunciation", "Notes", "Enabled"]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("TTS Dialogue App — ElevenLabs Multi-Voice Converter")
        self.resize(1280, 880)

        # state
        self.config = AppConfig()
        self.voices: list[Voice] = []
        self.models: list[str] = list(AVAILABLE_MODELS)
        self.model_caps: dict = {}
        self.lines: list[DialogueLine] = []
        self.project_path: Optional[str] = None
        self.character_colors: dict[str, str] = {}
        self.pronunciation = PronunciationManager()
        self.cache = CacheManager(get_cache_dir(), enabled=True)

        # workers (kept as refs so they aren't garbage-collected mid-run)
        self._test_worker: Optional[TestApiWorker] = None
        self._voices_worker: Optional[LoadVoicesWorker] = None
        self._models_worker: Optional[LoadModelsWorker] = None
        self._convert_worker: Optional[ConvertWorker] = None
        self._post_worker: Optional[PostProcessWorker] = None
        self._preview_worker: Optional[PreviewWorker] = None
        # Retain every worker thread so a still-running QThread is never garbage
        # collected mid-run (which would hard-crash the whole process).
        self._workers: list = []

        # audio player (shared)
        self._player = QMediaPlayer()
        self._audio_out = QAudioOutput()
        self._audio_out.setVolume(1.0)
        self._player.setAudioOutput(self._audio_out)
        self._player.errorOccurred.connect(
            lambda err, msg: self.log(f"Audio player error: {msg}")
        )
        # Robust playback (ffplay preferred, QMediaPlayer fallback) — QtMultimedia
        # is often silent inside a PyInstaller build.
        self._audio_player = AudioPlayer(self._player, log=self.log)

        self._build_menu()
        self._build_ui()

        if self.config.api_key:
            self.api_key_edit.setText(self.config.api_key)
            self.log("Loaded saved API key from config.")

        default_out = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "outputs",
        )
        self.output_folder_edit.setText(default_out)
        self._update_cache_label()
        self._load_favorite_voices()

    # ===================================================================== #
    # Menu
    # ===================================================================== #
    def _build_menu(self) -> None:
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")

        new_act = QAction("&New Project", self)
        new_act.triggered.connect(self.new_project)
        file_menu.addAction(new_act)

        save_act = QAction("&Save Project...", self)
        save_act.triggered.connect(self.save_project)
        file_menu.addAction(save_act)

        load_act = QAction("&Load Project...", self)
        load_act.triggered.connect(self.load_project)
        file_menu.addAction(load_act)

        self.recent_menu = file_menu.addMenu("Recent Projects")
        self._refresh_recent_menu()

        file_menu.addSeparator()
        quit_act = QAction("E&xit", self)
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

    def _refresh_recent_menu(self) -> None:
        self.recent_menu.clear()
        recents = self.config.recent_projects()
        if not recents:
            empty = QAction("(none)", self)
            empty.setEnabled(False)
            self.recent_menu.addAction(empty)
            return
        for path in recents:
            act = QAction(path, self)
            act.triggered.connect(lambda _=False, p=path: self._load_project_path(p))
            self.recent_menu.addAction(act)

    # ===================================================================== #
    # UI construction
    # ===================================================================== #
    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Vertical)

        tabs = QTabWidget()
        tabs.addTab(self._build_setup_tab(), "1. Setup & Voices")
        tabs.addTab(self._build_pronunciation_tab(), "2. Pronunciation")
        tabs.addTab(self._build_queue_tab(), "3. Queue & Convert")
        splitter.addWidget(tabs)

        splitter.addWidget(self._build_log_panel())
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)

        self.setCentralWidget(splitter)

    # ---------------------------- Setup tab ------------------------------- #
    def _build_setup_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        # ---- API settings ----
        api_box = QGroupBox("API Settings")
        api_layout = QHBoxLayout(api_box)
        api_layout.addWidget(QLabel("API Key:"))
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("xi-...")
        api_layout.addWidget(self.api_key_edit, 1)

        self.show_key_cb = QCheckBox("Show")
        self.show_key_cb.toggled.connect(
            lambda on: self.api_key_edit.setEchoMode(
                QLineEdit.Normal if on else QLineEdit.Password
            )
        )
        api_layout.addWidget(self.show_key_cb)

        self.test_btn = QPushButton("Test API")
        self.test_btn.clicked.connect(self.on_test_api)
        api_layout.addWidget(self.test_btn)

        self.load_voices_btn = QPushButton("Load Voices")
        self.load_voices_btn.clicked.connect(self.on_load_voices)
        self.load_voices_btn.setToolTip("Load all voices saved in your account")
        api_layout.addWidget(self.load_voices_btn)

        self.browse_library_btn = QPushButton("Browse Voice Library")
        self.browse_library_btn.clicked.connect(self.on_browse_library)
        self.browse_library_btn.setToolTip(
            "Search the huge public Voice Library by language / gender / age and add voices"
        )
        api_layout.addWidget(self.browse_library_btn)

        self.load_models_btn = QPushButton("Load Models")
        self.load_models_btn.clicked.connect(self.on_load_models)
        api_layout.addWidget(self.load_models_btn)

        self.voices_label = QLabel("Voices: 0 | Models: default")
        api_layout.addWidget(self.voices_label)
        layout.addWidget(api_box)

        # ---- model manager row ----
        model_box = QGroupBox("Model Manager")
        model_layout = QHBoxLayout(model_box)
        model_layout.addWidget(QLabel("Default model:"))
        self.default_model_combo = QComboBox()
        self.default_model_combo.addItems(self.models)
        self.default_model_combo.setCurrentText(self.config.default_model)
        model_layout.addWidget(self.default_model_combo, 1)
        self.refresh_models_btn = QPushButton("Refresh Models")
        self.refresh_models_btn.clicked.connect(self.on_load_models)
        model_layout.addWidget(self.refresh_models_btn)
        self.set_default_model_btn = QPushButton("Set Default Model")
        self.set_default_model_btn.clicked.connect(self.on_set_default_model)
        model_layout.addWidget(self.set_default_model_btn)
        layout.addWidget(model_box)

        # ---- Voice Finder (inline, collapsible) ----
        self.voice_finder_box = QGroupBox(
            "Voice Finder — lọc theo ngôn ngữ / giới tính / tuổi / miền · nghe thử · lưu favorites"
        )
        self.voice_finder_box.setCheckable(True)
        self.voice_finder_box.setChecked(False)
        vf_layout = QVBoxLayout(self.voice_finder_box)
        self.voice_finder = VoiceFinderWidget(
            get_api_key=self._current_api_key, config=self.config, log=self.log
        )
        self.voice_finder.voice_added.connect(self._on_library_voice_added)
        self.voice_finder.setVisible(False)
        vf_layout.addWidget(self.voice_finder)
        # collapse/expand by toggling the group's check
        self.voice_finder_box.toggled.connect(self.voice_finder.setVisible)
        layout.addWidget(self.voice_finder_box)

        # ---- Text input ----
        text_box = QGroupBox("Dialogue Text (format: 'Character: dialogue')")
        text_layout = QVBoxLayout(text_box)
        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText(PLACEHOLDER_DIALOGUE)
        text_layout.addWidget(self.text_edit)

        opt_row = QHBoxLayout()
        opt_row.addWidget(QLabel("Lines without ':' →"))
        self.unknown_mode_combo = QComboBox()
        self.unknown_mode_combo.addItem("Assign to Narrator", "narrator")
        self.unknown_mode_combo.addItem("Append to previous line", "append")
        opt_row.addWidget(self.unknown_mode_combo)
        opt_row.addStretch(1)

        self.detect_btn = QPushButton("Detect Characters")
        self.detect_btn.clicked.connect(self.on_detect_characters)
        opt_row.addWidget(self.detect_btn)

        self.auto_assign_btn = QPushButton("Auto-assign Voices")
        self.auto_assign_btn.clicked.connect(self.on_auto_assign)
        opt_row.addWidget(self.auto_assign_btn)
        text_layout.addLayout(opt_row)
        layout.addWidget(text_box)

        # ---- Character config table ----
        cfg_box = QGroupBox("Character → Voice Configuration")
        cfg_layout = QVBoxLayout(cfg_box)
        self.config_table = CharacterConfigTable()
        self.config_table.preview_requested.connect(self.on_preview)
        self.config_table.duplicate_requested.connect(self.on_duplicate_config)
        cfg_layout.addWidget(self.config_table)

        prev_row = QHBoxLayout()
        prev_row.addWidget(QLabel("Preview text:"))
        self.preview_text_edit = QLineEdit("Hello, this is a voice preview.")
        prev_row.addWidget(self.preview_text_edit, 1)
        cfg_layout.addLayout(prev_row)

        build_row = QHBoxLayout()
        build_row.addStretch(1)
        self.build_queue_btn = QPushButton("Build Queue ▶")
        self.build_queue_btn.clicked.connect(self.on_build_queue)
        build_row.addWidget(self.build_queue_btn)
        cfg_layout.addLayout(build_row)

        layout.addWidget(cfg_box, 1)
        return w

    # ------------------------ Pronunciation tab --------------------------- #
    def _build_pronunciation_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Add custom pronunciations (e.g. Đậu → Dow, Lucy → Loo-see). When the "
            "ElevenLabs account has uploaded pronunciation dictionaries the locator "
            "is sent to the API; otherwise the app substitutes the text before "
            "sending — the ORIGINAL text is always kept for subtitles/export."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.pron_table = QTableWidget()
        self.pron_table.setColumnCount(len(P_HEADERS))
        self.pron_table.setHorizontalHeaderLabels(P_HEADERS)
        self.pron_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.pron_table, 1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add rule")
        add_btn.clicked.connect(self.on_pron_add)
        del_btn = QPushButton("Delete rule")
        del_btn.clicked.connect(self.on_pron_delete)
        imp_btn = QPushButton("Import CSV")
        imp_btn.clicked.connect(self.on_pron_import)
        exp_btn = QPushButton("Export CSV")
        exp_btn.clicked.connect(self.on_pron_export)
        for b in (add_btn, del_btn, imp_btn, exp_btn):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        toggle_row = QHBoxLayout()
        self.pron_preview_cb = QCheckBox("Apply pronunciation to preview")
        self.pron_preview_cb.setChecked(True)
        self.pron_convert_cb = QCheckBox("Apply pronunciation to conversion")
        self.pron_convert_cb.setChecked(True)
        toggle_row.addWidget(self.pron_preview_cb)
        toggle_row.addWidget(self.pron_convert_cb)
        toggle_row.addStretch(1)
        layout.addLayout(toggle_row)

        return w

    # ---------------------------- Queue tab ------------------------------- #
    def _build_queue_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        # ---- Output + convert mode options ----
        opts_box = QGroupBox("Output & Convert Options")
        form = QFormLayout(opts_box)

        out_row = QHBoxLayout()
        self.output_folder_edit = QLineEdit()
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self.on_browse_output)
        out_row.addWidget(self.output_folder_edit, 1)
        out_row.addWidget(browse_btn)
        form.addRow("Output folder:", out_row)

        name_fmt_row = QHBoxLayout()
        self.project_name_edit = QLineEdit("my_project")
        name_fmt_row.addWidget(self.project_name_edit, 1)
        name_fmt_row.addWidget(QLabel("Format:"))
        self.format_combo = QComboBox()
        self.format_combo.addItems(["mp3", "wav"])
        name_fmt_row.addWidget(self.format_combo)
        form.addRow("Project name:", name_fmt_row)

        mode_row = QHBoxLayout()
        self.convert_mode_combo = QComboBox()
        self.convert_mode_combo.addItem("Line-by-line TTS (stable)", "line")
        self.convert_mode_combo.addItem("Dialogue API (natural, short)", "dialogue")
        self.convert_mode_combo.addItem("Auto (decide automatically)", "auto")
        mode_row.addWidget(self.convert_mode_combo, 1)
        mode_row.addWidget(QLabel("Max chars/batch:"))
        self.batch_chars_spin = QSpinBox()
        self.batch_chars_spin.setRange(100, 20000)
        self.batch_chars_spin.setValue(1500)
        mode_row.addWidget(self.batch_chars_spin)
        self.preview_batches_btn = QPushButton("Preview Batches")
        self.preview_batches_btn.clicked.connect(self.on_preview_batches)
        mode_row.addWidget(self.preview_batches_btn)
        form.addRow("Convert mode:", mode_row)

        silence_row = QHBoxLayout()
        self.silence_line_spin = QSpinBox()
        self.silence_line_spin.setRange(0, 5000)
        self.silence_line_spin.setValue(300)
        self.silence_line_spin.setSuffix(" ms")
        self.silence_speaker_spin = QSpinBox()
        self.silence_speaker_spin.setRange(0, 5000)
        self.silence_speaker_spin.setValue(500)
        self.silence_speaker_spin.setSuffix(" ms")
        silence_row.addWidget(QLabel("Between lines:"))
        silence_row.addWidget(self.silence_line_spin)
        silence_row.addWidget(QLabel("Speaker change:"))
        silence_row.addWidget(self.silence_speaker_spin)
        silence_row.addStretch(1)
        form.addRow("Silence:", silence_row)

        # audio post-processing
        post_opts_row = QHBoxLayout()
        self.normalize_cb = QCheckBox("Normalize")
        self.trim_cb = QCheckBox("Trim silence")
        post_opts_row.addWidget(self.normalize_cb)
        post_opts_row.addWidget(self.trim_cb)
        post_opts_row.addWidget(QLabel("Fade in:"))
        self.fade_in_spin = QSpinBox()
        self.fade_in_spin.setRange(0, 10000)
        self.fade_in_spin.setSuffix(" ms")
        post_opts_row.addWidget(self.fade_in_spin)
        post_opts_row.addWidget(QLabel("Fade out:"))
        self.fade_out_spin = QSpinBox()
        self.fade_out_spin.setRange(0, 10000)
        self.fade_out_spin.setSuffix(" ms")
        post_opts_row.addWidget(self.fade_out_spin)
        post_opts_row.addWidget(QLabel("Sample rate:"))
        self.sample_rate_combo = QComboBox()
        self.sample_rate_combo.addItems(["44100", "48000"])
        post_opts_row.addWidget(self.sample_rate_combo)
        post_opts_row.addWidget(QLabel("MP3 bitrate:"))
        self.bitrate_combo = QComboBox()
        self.bitrate_combo.addItems(["128k", "192k", "320k"])
        self.bitrate_combo.setCurrentText("192k")
        post_opts_row.addWidget(self.bitrate_combo)
        post_opts_row.addStretch(1)
        form.addRow("Audio post:", post_opts_row)

        checks_row = QHBoxLayout()
        self.auto_merge_cb = QCheckBox("Auto merge after convert")
        self.auto_merge_cb.setChecked(True)
        self.save_each_cb = QCheckBox("Save each line")
        self.save_each_cb.setChecked(True)
        self.save_grouped_cb = QCheckBox("Save grouped by character")
        checks_row.addWidget(self.auto_merge_cb)
        checks_row.addWidget(self.save_each_cb)
        checks_row.addWidget(self.save_grouped_cb)
        checks_row.addStretch(1)
        form.addRow("Options:", checks_row)

        # cache controls
        cache_row = QHBoxLayout()
        self.cache_cb = QCheckBox("Enable cache")
        self.cache_cb.setChecked(True)
        self.cache_cb.toggled.connect(self._on_cache_toggle)
        self.cache_size_label = QLabel("Cache: 0 B")
        self.clear_cache_btn = QPushButton("Clear cache")
        self.clear_cache_btn.clicked.connect(self.on_clear_cache)
        self.force_regen_btn = QPushButton("Force regenerate selected")
        self.force_regen_btn.clicked.connect(self.on_force_regenerate)
        cache_row.addWidget(self.cache_cb)
        cache_row.addWidget(self.cache_size_label)
        cache_row.addWidget(self.clear_cache_btn)
        cache_row.addWidget(self.force_regen_btn)
        cache_row.addStretch(1)
        form.addRow("Cache:", cache_row)

        # scene grouping
        scene_row = QHBoxLayout()
        self.scene_mode_combo = QComboBox()
        self.scene_mode_combo.addItem("Each line = 1 scene", "per_line")
        self.scene_mode_combo.addItem("New scene on speaker change", "per_speaker_change")
        self.scene_mode_combo.addItem("Every N lines = 1 scene", "per_n_lines")
        self.scene_mode_combo.addItem("Manual (edit Scene column)", "manual")
        scene_row.addWidget(self.scene_mode_combo, 1)
        scene_row.addWidget(QLabel("N:"))
        self.scene_n_spin = QSpinBox()
        self.scene_n_spin.setRange(1, 999)
        self.scene_n_spin.setValue(1)
        scene_row.addWidget(self.scene_n_spin)
        self.apply_scenes_btn = QPushButton("Apply Scenes")
        self.apply_scenes_btn.clicked.connect(self.on_apply_scenes)
        scene_row.addWidget(self.apply_scenes_btn)
        self.estimate_btn = QPushButton("Estimate Usage")
        self.estimate_btn.clicked.connect(self.on_estimate_usage)
        scene_row.addWidget(self.estimate_btn)
        form.addRow("Scenes:", scene_row)

        layout.addWidget(opts_box)

        # ---- Queue table ----
        self.queue_table = QTableWidget()
        self.queue_table.setColumnCount(len(Q_HEADERS))
        self.queue_table.setHorizontalHeaderLabels(Q_HEADERS)
        self.queue_table.verticalHeader().setVisible(False)
        # only the Scene column is editable (for manual scene assignment)
        self.queue_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked)
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.queue_table.setColumnWidth(Q_TEXT, 340)
        layout.addWidget(self.queue_table, 1)

        # ---- Convert controls ----
        ctrl_row = QHBoxLayout()
        self.convert_btn = QPushButton("Convert ▶")
        self.convert_btn.clicked.connect(self.on_convert)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.clicked.connect(self.on_pause_resume)
        self.pause_btn.setEnabled(False)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.on_cancel)
        self.cancel_btn.setEnabled(False)
        self.retry_failed_btn = QPushButton("Retry Failed")
        self.retry_failed_btn.clicked.connect(self.on_retry_failed)
        self.retry_selected_btn = QPushButton("Retry Selected")
        self.retry_selected_btn.clicked.connect(self.on_retry_selected)
        ctrl_row.addWidget(self.convert_btn)
        ctrl_row.addWidget(self.pause_btn)
        ctrl_row.addWidget(self.cancel_btn)
        ctrl_row.addWidget(self.retry_failed_btn)
        ctrl_row.addWidget(self.retry_selected_btn)
        ctrl_row.addStretch(1)
        layout.addLayout(ctrl_row)

        # ---- Post-processing / export controls ----
        post_row = QHBoxLayout()
        self.merge_btn = QPushButton("Merge Full Dialogue")
        self.merge_btn.clicked.connect(lambda: self.on_post_process(merge=True))
        self.export_char_btn = QPushButton("Export by Character")
        self.export_char_btn.clicked.connect(lambda: self.on_post_process(by_character=True))
        self.srt_btn = QPushButton("Export .srt")
        self.srt_btn.clicked.connect(lambda: self.on_post_process(srt=True))
        self.timeline_btn = QPushButton("Export Timeline (CSV+JSON)")
        self.timeline_btn.clicked.connect(lambda: self.on_post_process(timeline=True))
        self.open_folder_btn = QPushButton("Open Output Folder")
        self.open_folder_btn.clicked.connect(self.on_open_output)
        for b in (self.merge_btn, self.export_char_btn, self.srt_btn,
                  self.timeline_btn, self.open_folder_btn):
            post_row.addWidget(b)
        post_row.addStretch(1)
        layout.addLayout(post_row)

        # ---- Progress ----
        self.progress = QProgressBar()
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        return w

    # ---------------------------- Log panel ------------------------------- #
    def _build_log_panel(self) -> QWidget:
        box = QGroupBox("Log")
        layout = QVBoxLayout(box)
        self.log_panel = QPlainTextEdit()
        self.log_panel.setReadOnly(True)
        self.log_panel.setMaximumBlockCount(5000)
        layout.addWidget(self.log_panel)

        row = QHBoxLayout()
        save_log_btn = QPushButton("Save log")
        save_log_btn.clicked.connect(self.on_save_log)
        clear_log_btn = QPushButton("Clear log")
        clear_log_btn.clicked.connect(lambda: self.log_panel.clear())
        row.addStretch(1)
        row.addWidget(save_log_btn)
        row.addWidget(clear_log_btn)
        layout.addLayout(row)
        return box

    # ===================================================================== #
    # Logging
    # ===================================================================== #
    def log(self, message: str) -> None:
        self.log_panel.appendPlainText(message)

    def _track(self, worker) -> None:
        """Keep a reference until the thread finishes so a running QThread is
        never garbage-collected (that would abort the process)."""
        self._workers.append(worker)
        worker.finished.connect(
            lambda w=worker: self._workers.remove(w) if w in self._workers else None
        )

    def on_save_log(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save log", "tts_log.txt", "Text (*.txt)")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.log_panel.toPlainText())
                self.log(f"Log saved: {path}")
            except OSError as exc:
                QMessageBox.critical(self, "Save log failed", str(exc))

    # ===================================================================== #
    # API: Test / Load voices / Load models
    # ===================================================================== #
    def _current_api_key(self) -> str:
        return self.api_key_edit.text().strip()

    def on_test_api(self) -> None:
        key = self._current_api_key()
        if not key:
            QMessageBox.warning(self, "API Key", "Please enter your API key first.")
            return
        self.config.api_key = key
        self.test_btn.setEnabled(False)
        self.log("API: testing key...")
        self._test_worker = TestApiWorker(key)
        self._test_worker.success.connect(self._on_test_ok)
        self._test_worker.failed.connect(self._on_test_fail)
        self._track(self._test_worker)
        self._test_worker.start()

    def _on_test_ok(self) -> None:
        self.test_btn.setEnabled(True)
        self.log("API connected ✔ (key valid)")
        QMessageBox.information(self, "Test API", "API key is valid!")

    def _on_test_fail(self, msg: str) -> None:
        self.test_btn.setEnabled(True)
        self.log("API test failed: " + msg)
        QMessageBox.critical(self, "Test API failed", msg)

    def on_load_voices(self) -> None:
        key = self._current_api_key()
        if not key:
            QMessageBox.warning(self, "API Key", "Please enter your API key first.")
            return
        self.config.api_key = key
        self.load_voices_btn.setEnabled(False)
        self.log("API: loading voices...")
        self._voices_worker = LoadVoicesWorker(key)
        self._voices_worker.success.connect(self._on_voices_loaded)
        self._voices_worker.failed.connect(self._on_voices_failed)
        self._track(self._voices_worker)
        self._voices_worker.start()

    def _on_voices_loaded(self, voices: list) -> None:
        self.load_voices_btn.setEnabled(True)
        self.voices = voices
        self._update_status_label()
        self.config_table.set_voices(voices)
        self.log(f"Voices loaded: {len(voices)}")

    def _on_voices_failed(self, msg: str) -> None:
        self.load_voices_btn.setEnabled(True)
        self.log("Load voices failed: " + msg)
        QMessageBox.critical(self, "Load Voices failed", msg)

    def on_browse_library(self) -> None:
        """Expand the inline Voice Finder panel (and persist the key)."""
        key = self._current_api_key()
        if key:
            self.config.api_key = key
        self.voice_finder_box.setChecked(True)
        self.voice_finder.search_edit.setFocus()

    def _on_library_voice_added(self, voice) -> None:
        """A voice was added/saved — merge it into the voice list and refresh the
        per-character dropdowns (dedupe by voice_id)."""
        if any(v.voice_id == voice.voice_id for v in self.voices):
            return
        self.voices.append(voice)
        self._update_status_label()
        self.config_table.set_voices(self.voices)
        self.log(f"Voice '{voice.name}' is now selectable per character.")

    def _load_favorite_voices(self) -> None:
        """Load saved favorite voices at startup so they're immediately
        selectable per character without re-searching."""
        favs = [Voice.from_dict(d) for d in self.config.favorite_voices()]
        added = 0
        for v in favs:
            if not any(x.voice_id == v.voice_id for x in self.voices):
                self.voices.append(v)
                added += 1
        if added:
            self._update_status_label()
            self.config_table.set_voices(self.voices)
            self.log(f"Loaded {added} saved favorite voice(s).")

    def on_load_models(self) -> None:
        key = self._current_api_key()
        if not key:
            QMessageBox.warning(self, "API Key", "Please enter your API key first.")
            return
        self.config.api_key = key
        self.load_models_btn.setEnabled(False)
        self.refresh_models_btn.setEnabled(False)
        self.log("API: loading models...")
        self._models_worker = LoadModelsWorker(key)
        self._models_worker.success.connect(self._on_models_loaded)
        self._models_worker.failed.connect(self._on_models_failed)
        self._track(self._models_worker)
        self._models_worker.start()

    def _on_models_loaded(self, models: list) -> None:
        self.load_models_btn.setEnabled(True)
        self.refresh_models_btn.setEnabled(True)
        if not models:
            self.log("Models loaded: 0 (keeping defaults).")
            return
        self.model_caps = {m.model_id: m for m in models}
        self.models = [m.model_id for m in models]
        # update default-model combo + config table
        cur = self.default_model_combo.currentText()
        self.default_model_combo.blockSignals(True)
        self.default_model_combo.clear()
        self.default_model_combo.addItems(self.models)
        if cur in self.models:
            self.default_model_combo.setCurrentText(cur)
        self.default_model_combo.blockSignals(False)
        self.config_table.set_models(self.models, self.model_caps)
        self._update_status_label()
        self.log(
            "Models loaded: "
            + ", ".join(f"{m.model_id}(style={m.can_use_style},boost={m.can_use_speaker_boost})"
                        for m in models)
        )

    def _on_models_failed(self, msg: str) -> None:
        self.load_models_btn.setEnabled(True)
        self.refresh_models_btn.setEnabled(True)
        self.log("Load models failed: " + msg)
        QMessageBox.critical(self, "Load Models failed", msg)

    def on_set_default_model(self) -> None:
        model = self.default_model_combo.currentText()
        self.config.default_model = model
        self.log(f"Default model set: {model}")
        QMessageBox.information(self, "Default model", f"Default model is now: {model}")

    def _update_status_label(self) -> None:
        self.voices_label.setText(
            f"Voices: {len(self.voices)} | Models: {len(self.models)}"
        )

    # ===================================================================== #
    # Pronunciation tab
    # ===================================================================== #
    def _pron_rule_from_row(self, row: int) -> PronunciationRule:
        def text(col: int) -> str:
            item = self.pron_table.item(row, col)
            return item.text().strip() if item else ""
        enabled_item = self.pron_table.item(row, P_ENABLED)
        enabled = enabled_item.checkState() == Qt.Checked if enabled_item else True
        return PronunciationRule(
            original=text(P_ORIGINAL),
            replacement=text(P_REPLACEMENT),
            notes=text(P_NOTES),
            enabled=enabled,
        )

    def _sync_pronunciation_from_table(self) -> None:
        """Rebuild self.pronunciation from the table contents."""
        rules: list[PronunciationRule] = []
        for row in range(self.pron_table.rowCount()):
            rule = self._pron_rule_from_row(row)
            if rule.original:
                rules.append(rule)
        self.pronunciation = PronunciationManager(rules)

    def _add_pron_row(self, rule: PronunciationRule) -> None:
        row = self.pron_table.rowCount()
        self.pron_table.insertRow(row)
        self.pron_table.setItem(row, P_ORIGINAL, QTableWidgetItem(rule.original))
        self.pron_table.setItem(row, P_REPLACEMENT, QTableWidgetItem(rule.replacement))
        self.pron_table.setItem(row, P_NOTES, QTableWidgetItem(rule.notes))
        enabled_item = QTableWidgetItem()
        enabled_item.setFlags(
            (enabled_item.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable
        )
        enabled_item.setCheckState(Qt.Checked if rule.enabled else Qt.Unchecked)
        self.pron_table.setItem(row, P_ENABLED, enabled_item)

    def on_pron_add(self) -> None:
        self._add_pron_row(PronunciationRule(original="", replacement="", notes="", enabled=True))

    def on_pron_delete(self) -> None:
        rows = sorted({i.row() for i in self.pron_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.pron_table.removeRow(r)

    def on_pron_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import pronunciation CSV", "", "CSV (*.csv)")
        if not path:
            return
        try:
            self._sync_pronunciation_from_table()
            added = self.pronunciation.import_csv(path)
            self._reload_pron_table()
            self.log(f"Imported {added} pronunciation rule(s).")
        except Exception as exc:
            QMessageBox.critical(self, "Import failed", str(exc))

    def on_pron_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export pronunciation CSV", "pronunciation.csv", "CSV (*.csv)"
        )
        if not path:
            return
        try:
            self._sync_pronunciation_from_table()
            self.pronunciation.export_csv(path)
            self.log(f"Exported pronunciation rules: {path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def _reload_pron_table(self) -> None:
        self.pron_table.setRowCount(0)
        for rule_dict in self.pronunciation.to_list():
            self._add_pron_row(PronunciationRule.from_dict(rule_dict))

    # ===================================================================== #
    # Detect characters
    # ===================================================================== #
    def _unknown_mode(self) -> str:
        return self.unknown_mode_combo.currentData()

    def on_detect_characters(self) -> None:
        text = self.text_edit.toPlainText()
        if not text.strip():
            QMessageBox.warning(self, "Detect", "Please paste some dialogue first.")
            return

        self.lines = parse_dialogue(text, unknown_line_mode=self._unknown_mode())
        characters = detect_characters(self.lines)
        if not characters:
            QMessageBox.warning(self, "Detect", "No characters detected.")
            return

        default_model = self.default_model_combo.currentText()
        existing = self.config_table.get_all_configs()
        configs: list[CharacterVoiceConfig] = []
        for i, name in enumerate(characters):
            color = CHARACTER_COLORS[i % len(CHARACTER_COLORS)]
            self.character_colors[name] = color
            if name in existing:
                cfg = existing[name]
                cfg.color = color
            else:
                cfg = CharacterVoiceConfig(
                    character=name,
                    model_id=default_model,
                    preset=DEFAULT_PRESET,
                    settings=VoiceSettings.from_dict(PRESETS[DEFAULT_PRESET]),
                    color=color,
                )
            configs.append(cfg)

        self.config_table.set_models(self.models, self.model_caps)
        self.config_table.set_characters(configs)
        self.config_table.set_voices(self.voices)
        self.log(
            f"Dialogue parsed: {len(characters)} character(s) "
            f"({', '.join(characters)}), {len(self.lines)} line(s)."
        )
        if not self.voices:
            self.log("Tip: click 'Load Voices' to populate the voice dropdowns.")

    def on_auto_assign(self) -> None:
        if not self.voices:
            QMessageBox.warning(self, "Auto-assign", "Load voices first.")
            return
        self.config_table.auto_assign_voices()
        self.log("Auto-assigned distinct voices to characters.")

    def on_duplicate_config(self, source_character: str) -> None:
        names = [c for c in self.config_table.character_names() if c != source_character]
        if not names:
            return
        target, ok = QInputDialog.getItem(
            self, "Duplicate voice config",
            f"Copy '{source_character}' settings to:", names, 0, False,
        )
        if ok and target:
            source_cfg = self.config_table.get_config_for(source_character)
            if source_cfg:
                self.config_table.apply_config_to(target, source_cfg)
                self.log(f"Copied voice config from '{source_character}' to '{target}'.")

    # ===================================================================== #
    # Build queue + scenes
    # ===================================================================== #
    def _project_dir(self) -> str:
        return os.path.join(
            self.output_folder_edit.text().strip(),
            self.project_name_edit.text().strip() or "my_project",
        )

    def on_build_queue(self) -> None:
        text = self.text_edit.toPlainText()
        if not text.strip():
            QMessageBox.warning(self, "Build Queue", "Please paste some dialogue first.")
            return
        self.lines = parse_dialogue(text, unknown_line_mode=self._unknown_mode())
        if not self.lines:
            QMessageBox.warning(self, "Build Queue", "No dialogue lines found.")
            return
        self._assign_scenes()  # initial scene assignment from current mode
        configs = self.config_table.get_all_configs()
        self._populate_queue(configs)
        self.log(f"Queue built with {len(self.lines)} line(s).")

    def _assign_scenes(self) -> None:
        """Assign line.scene based on the chosen scene mode (except manual)."""
        mode = self.scene_mode_combo.currentData()
        if mode == "manual":
            return
        n = max(1, self.scene_n_spin.value())
        prev_char = None
        scene = 0
        for i, line in enumerate(self.lines):
            if mode == "per_line":
                line.scene = i + 1
            elif mode == "per_speaker_change":
                if line.character != prev_char:
                    scene += 1
                line.scene = scene
                prev_char = line.character
            elif mode == "per_n_lines":
                line.scene = (i // n) + 1
            else:
                line.scene = i + 1

    def on_apply_scenes(self) -> None:
        if not self.lines:
            QMessageBox.warning(self, "Scenes", "Build the queue first.")
            return
        mode = self.scene_mode_combo.currentData()
        if mode == "manual":
            # read scene numbers from the (editable) Scene column
            self._sync_scenes_from_table()
            self.log("Applied manual scene numbers from the table.")
        else:
            self._assign_scenes()
            # refresh the Scene column cells
            for line in self.lines:
                row = self._row_for_index(line.index)
                if row is not None:
                    self.queue_table.item(row, Q_SCENE).setText(str(line.scene))
            self.log(f"Scenes assigned using mode '{mode}'.")

    def _sync_scenes_from_table(self) -> None:
        for line in self.lines:
            row = self._row_for_index(line.index)
            if row is not None:
                item = self.queue_table.item(row, Q_SCENE)
                try:
                    line.scene = int(item.text()) if item and item.text().strip() else 0
                except ValueError:
                    line.scene = 0

    def _populate_queue(self, configs: dict[str, CharacterVoiceConfig]) -> None:
        self.queue_table.setRowCount(0)
        for line in self.lines:
            row = self.queue_table.rowCount()
            self.queue_table.insertRow(row)

            color = self.character_colors.get(line.character, "#cccccc")

            scene_item = QTableWidgetItem(str(line.scene or ""))
            scene_item.setFlags(scene_item.flags() | Qt.ItemIsEditable)  # editable for manual
            idx_item = QTableWidgetItem(str(line.index))
            idx_item.setFlags(idx_item.flags() & ~Qt.ItemIsEditable)
            char_item = QTableWidgetItem(line.character)
            char_item.setFlags(char_item.flags() & ~Qt.ItemIsEditable)
            char_item.setBackground(QColor(color))
            text_item = QTableWidgetItem(line.text)
            text_item.setFlags(text_item.flags() & ~Qt.ItemIsEditable)
            cfg = configs.get(line.character)
            voice_item = QTableWidgetItem(cfg.voice_name if cfg else "")
            status_item = QTableWidgetItem(line.status.value)
            dur_item = QTableWidgetItem(f"{line.duration:.2f}s" if line.duration else "")
            file_item = QTableWidgetItem(
                os.path.basename(line.output_file) if line.output_file else ""
            )
            for it in (voice_item, status_item, dur_item, file_item):
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)

            self.queue_table.setItem(row, Q_SCENE, scene_item)
            self.queue_table.setItem(row, Q_INDEX, idx_item)
            self.queue_table.setItem(row, Q_CHARACTER, char_item)
            self.queue_table.setItem(row, Q_TEXT, text_item)
            self.queue_table.setItem(row, Q_VOICE, voice_item)
            self.queue_table.setItem(row, Q_STATUS, status_item)
            self.queue_table.setItem(row, Q_DURATION, dur_item)
            self.queue_table.setItem(row, Q_FILE, file_item)

            play_btn = QPushButton("▶")
            play_btn.clicked.connect(lambda _=False, idx=line.index: self.on_play_line(idx))
            self.queue_table.setCellWidget(row, Q_PLAY, play_btn)

            retry_btn = QPushButton("↻")
            retry_btn.clicked.connect(lambda _=False, idx=line.index: self.on_retry_line(idx))
            self.queue_table.setCellWidget(row, Q_RETRY, retry_btn)

    def _row_for_index(self, index: int) -> Optional[int]:
        for row in range(self.queue_table.rowCount()):
            item = self.queue_table.item(row, Q_INDEX)
            if item and item.text() == str(index):
                return row
        return None

    def _line_for_index(self, index: int) -> Optional[DialogueLine]:
        for ln in self.lines:
            if ln.index == index:
                return ln
        return None

    def _selected_indices(self) -> list[int]:
        rows = sorted({i.row() for i in self.queue_table.selectedIndexes()})
        result = []
        for r in rows:
            item = self.queue_table.item(r, Q_INDEX)
            if item:
                result.append(int(item.text()))
        return result

    # ===================================================================== #
    # Usage estimate + batch preview
    # ===================================================================== #
    def on_estimate_usage(self) -> None:
        if not self.lines:
            QMessageBox.warning(self, "Estimate", "Build the queue first.")
            return
        configs = self.config_table.get_all_configs()
        pron = self.pronunciation if self.pron_convert_cb.isChecked() else None
        if self.pron_convert_cb.isChecked():
            self._sync_pronunciation_from_table()
            pron = self.pronunciation
        stats = UsageEstimator.estimate(
            self.lines, configs,
            pronunciation=pron,
            cache=self.cache if self.cache_cb.isChecked() else None,
            output_format=self.format_combo.currentText(),
        )
        report = UsageEstimator.format_report(stats)
        self.log("Usage estimate:\n" + report)
        QMessageBox.information(self, "Usage / Cost Estimate", report)

    def on_preview_batches(self) -> None:
        if not self.lines:
            QMessageBox.warning(self, "Batches", "Build the queue first.")
            return
        self._sync_pronunciation_from_table()
        for ln in self.lines:
            if self.pron_convert_cb.isChecked():
                p = self.pronunciation.apply(ln.text)
                ln.processed_text = p if p != ln.text else ""
        batches = BatchSplitter.split(self.lines, self.batch_chars_spin.value())
        oversized = BatchSplitter.find_oversized(self.lines, self.batch_chars_spin.value())
        preview = BatchSplitter.preview(batches)
        if oversized:
            preview += "\n\nWARNING — these lines exceed max chars and may need splitting:\n"
            preview += "\n".join(f"  Line {ln.index} [{ln.character}]: {len(ln.api_text())} chars"
                                 for ln in oversized)
        self.log("Batch preview:\n" + preview)
        QMessageBox.information(self, "Batch Preview", preview)

    # ===================================================================== #
    # Cache controls
    # ===================================================================== #
    def _on_cache_toggle(self, on: bool) -> None:
        self.cache.enabled = on
        self.log(f"Cache {'enabled' if on else 'disabled'}.")

    def _update_cache_label(self) -> None:
        self.cache_size_label.setText(
            f"Cache: {self.cache.size_human()} ({self.cache.count()} files)"
        )

    def on_clear_cache(self) -> None:
        removed = self.cache.clear()
        self._update_cache_label()
        self.log(f"Cache cleared ({removed} files removed).")

    def on_force_regenerate(self) -> None:
        indices = self._selected_indices()
        if not indices:
            QMessageBox.information(self, "Force regenerate", "Select line(s) in the queue first.")
            return
        lines = [self._line_for_index(i) for i in indices if self._line_for_index(i)]
        self._start_conversion(lines, force_regenerate=set(indices))

    # ===================================================================== #
    # Convert
    # ===================================================================== #
    def _validate_before_convert(self, lines: list[DialogueLine]) -> bool:
        if not self._current_api_key():
            QMessageBox.warning(self, "Convert", "Enter your API key first.")
            return False
        if not self.output_folder_edit.text().strip():
            QMessageBox.warning(self, "Convert", "Choose an output folder.")
            return False
        configs = self.config_table.get_all_configs()
        missing = sorted({
            ln.character for ln in lines
            if not (configs.get(ln.character) and configs[ln.character].voice_id)
        })
        if missing:
            QMessageBox.warning(
                self, "Convert",
                "These characters have no voice selected:\n  " + ", ".join(missing)
                + "\n\nLoad voices and pick one for each character.",
            )
            return False
        return True

    def on_convert(self) -> None:
        if not self.lines:
            QMessageBox.warning(self, "Convert", "Build the queue first.")
            return
        self._start_conversion(self.lines)

    def on_retry_failed(self) -> None:
        failed = [ln for ln in self.lines if ln.status == LineStatus.ERROR]
        if not failed:
            QMessageBox.information(self, "Retry", "No failed lines to retry.")
            return
        self._start_conversion(failed)

    def on_retry_selected(self) -> None:
        indices = self._selected_indices()
        if not indices:
            QMessageBox.information(self, "Retry", "Select line(s) in the queue first.")
            return
        lines = [self._line_for_index(i) for i in indices if self._line_for_index(i)]
        self._start_conversion(lines)

    def on_retry_line(self, index: int) -> None:
        line = self._line_for_index(index)
        if line:
            self._start_conversion([line])

    def _start_conversion(
        self, lines: list[DialogueLine], force_regenerate: Optional[set[int]] = None
    ) -> None:
        if self._convert_worker and self._convert_worker.isRunning():
            QMessageBox.warning(self, "Convert", "A conversion is already running.")
            return
        if not self._validate_before_convert(lines):
            return

        self._sync_pronunciation_from_table()
        configs = self.config_table.get_all_configs()
        project_dir = self._project_dir()
        lines_dir = os.path.join(project_dir, "lines")
        fmt = self.format_combo.currentText()

        for ln in lines:
            ln.status = LineStatus.PENDING
            self._set_row_status(ln.index, LineStatus.PENDING.value)

        self.progress.setValue(0)
        self._set_convert_running(True)
        mode = self.convert_mode_combo.currentData()
        self.log(f"Converting {len(lines)} line(s), mode='{mode}' → {lines_dir}")

        self._convert_worker = ConvertWorker(
            api_key=self._current_api_key(),
            lines=lines,
            configs=configs,
            lines_dir=lines_dir,
            output_format=fmt,
            convert_mode=mode,
            pronunciation=self.pronunciation if self.pron_convert_cb.isChecked() else None,
            apply_pronunciation=self.pron_convert_cb.isChecked(),
            cache=self.cache if self.cache_cb.isChecked() else None,
            max_chars_per_batch=self.batch_chars_spin.value(),
            force_regenerate=force_regenerate,
        )
        self._convert_worker.line_status.connect(self._set_row_status)
        self._convert_worker.line_done.connect(self._on_line_done)
        self._convert_worker.line_error.connect(self._on_line_error)
        self._convert_worker.progress.connect(self._on_progress)
        self._convert_worker.log.connect(self.log)
        self._convert_worker.cache_stat.connect(
            lambda h, m: self.log(f"Cache: {h} hit(s), {m} miss(es).")
        )
        self._convert_worker.finished_all.connect(self._on_convert_finished)
        self._track(self._convert_worker)
        self._convert_worker.start()

    def _set_convert_running(self, running: bool) -> None:
        self.convert_btn.setEnabled(not running)
        self.pause_btn.setEnabled(running)
        self.cancel_btn.setEnabled(running)
        self.pause_btn.setText("Pause")

    def _set_row_status(self, index: int, status: str) -> None:
        row = self._row_for_index(index)
        if row is not None:
            item = self.queue_table.item(row, Q_STATUS)
            if item:
                item.setText(status)
                colors = {
                    "Pending": "#eeeeee", "Processing": "#FFF59D",
                    "Done": "#A5D6A7", "Error": "#EF9A9A",
                }
                item.setBackground(QColor(colors.get(status, "#ffffff")))

    def _on_line_done(self, index: int, output_file: str, duration: float) -> None:
        line = self._line_for_index(index)
        if line:
            line.status = LineStatus.DONE
            line.output_file = output_file
            line.duration = duration
        row = self._row_for_index(index)
        if row is not None:
            self._set_row_status(index, LineStatus.DONE.value)
            self.queue_table.item(row, Q_DURATION).setText(f"{duration:.2f}s")
            self.queue_table.item(row, Q_FILE).setText(os.path.basename(output_file))

    def _on_line_error(self, index: int, message: str) -> None:
        line = self._line_for_index(index)
        if line:
            line.status = LineStatus.ERROR
            line.error = message
        self._set_row_status(index, LineStatus.ERROR.value)

    def _on_progress(self, processed: int, total: int) -> None:
        self.progress.setValue(int(processed / total * 100) if total else 0)

    def _on_convert_finished(self) -> None:
        self._set_convert_running(False)
        self._update_cache_label()
        self.log("Conversion finished.")

        done = [ln for ln in self.lines if ln.status == LineStatus.DONE]
        errors = [ln for ln in self.lines if ln.status == LineStatus.ERROR]
        self.log(f"Summary: {len(done)} done, {len(errors)} error(s).")

        if done and self.auto_merge_cb.isChecked():
            self.on_post_process(merge=True, by_character=self.save_grouped_cb.isChecked())
        elif done and self.save_grouped_cb.isChecked():
            self.on_post_process(by_character=True)

    def on_pause_resume(self) -> None:
        if not (self._convert_worker and self._convert_worker.isRunning()):
            return
        if self.pause_btn.text() == "Pause":
            self._convert_worker.pause()
            self.pause_btn.setText("Resume")
        else:
            self._convert_worker.resume()
            self.pause_btn.setText("Pause")

    def on_cancel(self) -> None:
        if self._convert_worker and self._convert_worker.isRunning():
            self._convert_worker.cancel()

    # ===================================================================== #
    # Post-processing (merge / export / srt / timeline)
    # ===================================================================== #
    def _post_options(self) -> PostProcessOptions:
        return PostProcessOptions(
            normalize=self.normalize_cb.isChecked(),
            trim_silence=self.trim_cb.isChecked(),
            fade_in_ms=self.fade_in_spin.value(),
            fade_out_ms=self.fade_out_spin.value(),
            sample_rate=int(self.sample_rate_combo.currentText()),
            bitrate=self.bitrate_combo.currentText(),
            silence_between_lines_ms=self.silence_line_spin.value(),
            speaker_change_silence_ms=self.silence_speaker_spin.value(),
        )

    def on_post_process(
        self, merge: bool = False, by_character: bool = False,
        srt: bool = False, timeline: bool = False,
    ) -> None:
        done = [ln for ln in self.lines if ln.status == LineStatus.DONE and ln.output_file]
        if not done:
            QMessageBox.warning(self, "Post-process", "No converted audio yet. Convert first.")
            return
        if self._post_worker and self._post_worker.isRunning():
            QMessageBox.warning(self, "Post-process", "Already running.")
            return

        # make sure scenes are current for timeline export
        if timeline:
            self._sync_scenes_from_table()

        self.log("Post-processing...")
        self._post_worker = PostProcessWorker(
            lines=self.lines,
            configs=self.config_table.get_all_configs(),
            project_dir=self._project_dir(),
            output_format=self.format_combo.currentText(),
            options=self._post_options(),
            do_merge=merge,
            do_by_character=by_character,
            do_srt=srt,
            do_timeline=timeline,
        )
        self._post_worker.log.connect(self.log)
        self._post_worker.success.connect(self._on_post_success)
        self._post_worker.failed.connect(self._on_post_failed)
        self._track(self._post_worker)
        self._post_worker.start()

    def _on_post_success(self, results: dict) -> None:
        self.log("Post-processing complete.")
        parts = []
        if "merged" in results:
            parts.append(f"Merged: {results['merged']}")
        if "by_character" in results:
            parts.append(f"By character: {len(results['by_character'])} file(s)")
        if "srt" in results:
            parts.append(f"Subtitle: {results['srt']}")
        if "timeline_csv" in results:
            parts.append(f"Timeline CSV: {results['timeline_csv']}")
            parts.append(f"Timeline JSON: {results['timeline_json']}")
        if parts:
            QMessageBox.information(self, "Done", "\n".join(parts))

    def _on_post_failed(self, msg: str) -> None:
        self.log("Post-process error: " + msg)
        QMessageBox.critical(
            self, "Post-process failed",
            msg + "\n\nNOTE: merging/exporting requires ffmpeg. See README.",
        )

    # ===================================================================== #
    # Preview & playback
    # ===================================================================== #
    def on_preview(self, character: str) -> None:
        key = self._current_api_key()
        if not key:
            QMessageBox.warning(self, "Preview", "Enter your API key first.")
            return
        cfg = self.config_table.get_config_for(character)
        if not cfg or not cfg.voice_id:
            QMessageBox.warning(self, "Preview", f"Pick a voice for '{character}' first.")
            return
        text = self.preview_text_edit.text().strip() or "Hello, this is a voice preview."

        tmp_dir = os.path.join(self._project_dir(), "previews")
        os.makedirs(tmp_dir, exist_ok=True)
        safe = "".join(c if c.isalnum() else "_" for c in character)
        out_path = os.path.join(tmp_dir, f"preview_{safe}.mp3")

        self._sync_pronunciation_from_table()
        self.log(f"Previewing '{character}'...")
        self._preview_worker = PreviewWorker(
            api_key=key, text=text, config=cfg, output_path=out_path,
            output_format="mp3",
            pronunciation=self.pronunciation,
            apply_pronunciation=self.pron_preview_cb.isChecked(),
        )
        self._preview_worker.success.connect(self._on_preview_ready)
        self._preview_worker.failed.connect(
            lambda m: QMessageBox.critical(self, "Preview failed", m)
        )
        self._track(self._preview_worker)
        self._preview_worker.start()

    def _on_preview_ready(self, path: str) -> None:
        self.log(f"Preview ready: {path}")
        self._play_file(path)

    def on_play_line(self, index: int) -> None:
        line = self._line_for_index(index)
        if not line or not line.output_file or not os.path.exists(line.output_file):
            QMessageBox.information(
                self, "Play",
                "This line has no separate audio file.\n(In Dialogue mode the audio "
                "lives in the batch file carried by the first line of the batch.)",
            )
            return
        self._play_file(line.output_file)

    def _play_file(self, path: str) -> None:
        self._audio_player.play(path)

    # ===================================================================== #
    # Output folder helpers
    # ===================================================================== #
    def on_browse_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select output folder")
        if folder:
            self.output_folder_edit.setText(folder)

    def on_open_output(self) -> None:
        path = self._project_dir()
        os.makedirs(path, exist_ok=True)
        try:
            os.startfile(path)  # type: ignore[attr-defined]  # Windows only
        except AttributeError:
            QMessageBox.information(self, "Output", path)

    # ===================================================================== #
    # Project save / load / new
    # ===================================================================== #
    def _gather_project(self) -> Project:
        self._sync_pronunciation_from_table()
        self._sync_scenes_from_table()
        settings = ProjectSettings(
            output_folder=self.output_folder_edit.text().strip(),
            project_name=self.project_name_edit.text().strip(),
            output_format=self.format_combo.currentText(),
            silence_between_lines_ms=self.silence_line_spin.value(),
            speaker_change_silence_ms=self.silence_speaker_spin.value(),
            auto_merge_after_convert=self.auto_merge_cb.isChecked(),
            save_each_line=self.save_each_cb.isChecked(),
            save_grouped_by_character=self.save_grouped_cb.isChecked(),
            normalize_volume=self.normalize_cb.isChecked(),
            unknown_line_mode=self._unknown_mode(),
            convert_mode=self.convert_mode_combo.currentData(),
            default_model_id=self.default_model_combo.currentText(),
            max_chars_per_batch=self.batch_chars_spin.value(),
            cache_enabled=self.cache_cb.isChecked(),
            scene_mode=self.scene_mode_combo.currentData(),
            scene_n_lines=self.scene_n_spin.value(),
            apply_pronunciation_to_conversion=self.pron_convert_cb.isChecked(),
            apply_pronunciation_to_preview=self.pron_preview_cb.isChecked(),
            trim_silence=self.trim_cb.isChecked(),
            fade_in_ms=self.fade_in_spin.value(),
            fade_out_ms=self.fade_out_spin.value(),
            export_sample_rate=int(self.sample_rate_combo.currentText()),
            export_bitrate=self.bitrate_combo.currentText(),
        )
        return Project(
            original_text=self.text_edit.toPlainText(),
            lines=self.lines,
            character_configs=self.config_table.get_all_configs(),
            settings=settings,
            pronunciation_rules=self.pronunciation.to_list(),
        )

    def save_project(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Project", self.project_name_edit.text() + ".json", "JSON (*.json)"
        )
        if not path:
            return
        try:
            ProjectManager.save(self._gather_project(), path)
            self.project_path = path
            self.config.add_recent_project(path)
            self._refresh_recent_menu()
            self.log(f"Project saved: {path}")
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))

    def load_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load Project", "", "JSON (*.json)")
        if path:
            self._load_project_path(path)

    def _load_project_path(self, path: str) -> None:
        try:
            project = ProjectManager.load(path)
        except Exception as exc:
            QMessageBox.critical(self, "Load failed", str(exc))
            return

        self.project_path = path
        self.text_edit.setPlainText(project.original_text)
        self.lines = project.lines

        s = project.settings
        self.output_folder_edit.setText(s.output_folder)
        self.project_name_edit.setText(s.project_name)
        self.format_combo.setCurrentText(s.output_format)
        self.silence_line_spin.setValue(s.silence_between_lines_ms)
        self.silence_speaker_spin.setValue(s.speaker_change_silence_ms)
        self.auto_merge_cb.setChecked(s.auto_merge_after_convert)
        self.save_each_cb.setChecked(s.save_each_line)
        self.save_grouped_cb.setChecked(s.save_grouped_by_character)
        self.normalize_cb.setChecked(s.normalize_volume)
        self._set_combo_by_data(self.unknown_mode_combo, s.unknown_line_mode)
        self._set_combo_by_data(self.convert_mode_combo, s.convert_mode)
        self.default_model_combo.setCurrentText(s.default_model_id)
        self.batch_chars_spin.setValue(s.max_chars_per_batch)
        self.cache_cb.setChecked(s.cache_enabled)
        self._set_combo_by_data(self.scene_mode_combo, s.scene_mode)
        self.scene_n_spin.setValue(s.scene_n_lines)
        self.pron_convert_cb.setChecked(s.apply_pronunciation_to_conversion)
        self.pron_preview_cb.setChecked(s.apply_pronunciation_to_preview)
        self.trim_cb.setChecked(s.trim_silence)
        self.fade_in_spin.setValue(s.fade_in_ms)
        self.fade_out_spin.setValue(s.fade_out_ms)
        self.sample_rate_combo.setCurrentText(str(s.export_sample_rate))
        self.bitrate_combo.setCurrentText(s.export_bitrate)

        # pronunciation
        self.pronunciation = PronunciationManager.from_list(project.pronunciation_rules)
        self._reload_pron_table()

        # rebuild character configs + colors
        configs = list(project.character_configs.values())
        for i, cfg in enumerate(configs):
            self.character_colors[cfg.character] = cfg.color or CHARACTER_COLORS[
                i % len(CHARACTER_COLORS)
            ]
        self.config_table.set_models(self.models, self.model_caps)
        self.config_table.set_characters(configs)
        self.config_table.set_voices(self.voices)

        self._populate_queue(project.character_configs)
        for ln in self.lines:
            self._set_row_status(ln.index, ln.status.value)
            row = self._row_for_index(ln.index)
            if row is not None:
                if ln.duration:
                    self.queue_table.item(row, Q_DURATION).setText(f"{ln.duration:.2f}s")
                if ln.output_file:
                    self.queue_table.item(row, Q_FILE).setText(os.path.basename(ln.output_file))

        self.config.add_recent_project(path)
        self._refresh_recent_menu()
        self.log(f"Project loaded: {path}")

    def _set_combo_by_data(self, combo: QComboBox, data: str) -> None:
        idx = combo.findData(data)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def new_project(self) -> None:
        if QMessageBox.question(
            self, "New Project", "Clear current project? Unsaved changes will be lost.",
        ) != QMessageBox.Yes:
            return
        self.text_edit.clear()
        self.lines = []
        self.character_colors.clear()
        self.config_table.set_characters([])
        self.queue_table.setRowCount(0)
        self.pron_table.setRowCount(0)
        self.pronunciation = PronunciationManager()
        self.progress.setValue(0)
        self.project_path = None
        self.project_name_edit.setText("my_project")
        self.log("New project.")

    # ===================================================================== #
    # Close handling
    # ===================================================================== #
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if self._convert_worker and self._convert_worker.isRunning():
            self._convert_worker.cancel()
            self._convert_worker.wait(3000)
        self._audio_player.stop()
        try:
            self.voice_finder._aplayer.stop()
        except Exception:
            pass
        # Wait briefly for any background threads so none is destroyed mid-run
        # (which would abort the process at exit).
        for pool in (list(self._workers), list(getattr(self.voice_finder, "_workers", []))):
            for w in pool:
                try:
                    if w.isRunning():
                        w.wait(2000)
                except Exception:
                    pass
        event.accept()
