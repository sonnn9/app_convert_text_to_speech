"""Reusable GUI widgets.

* :class:`SearchableComboBox` — a combo box with type-to-search (used for the
  voice dropdown which can contain dozens of voices).
* :class:`CharacterConfigTable` — the per-character voice configuration table
  (Section 3.3 of the spec). Each row exposes voice/model/preset selectors and
  sliders for the ElevenLabs voice settings.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from app.config.settings import AVAILABLE_MODELS, PRESETS
from app.core.models import CharacterVoiceConfig, Voice, VoiceSettings


def _voice_label(v: Voice) -> str:
    """Dropdown label like 'Rachel — Nữ · trung niên [premade]'.

    The gender/age descriptor is embedded in the text so the searchable combo
    lets the user type 'Nam' / 'Nữ' / 'Trẻ em' to filter the list."""
    desc = v.descriptor()
    lang = v.language()
    cat = v.category or "voice"
    lang_part = f" · {lang}" if lang else ""
    return f"{v.name} — {desc}{lang_part} [{cat}]"


class SearchableComboBox(QComboBox):
    """An editable combo box that filters items as you type but still behaves
    like a selector (the final value must be one of the items)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.completer().setCompletionMode(QCompleter.PopupCompletion)
        self.completer().setFilterMode(Qt.MatchContains)
        self.completer().setCaseSensitivity(Qt.CaseInsensitive)

    def set_items(self, items: list[tuple[str, str]]) -> None:
        """``items`` is a list of (label, userData) tuples."""
        self.clear()
        for label, data in items:
            self.addItem(label, data)

    def select_by_data(self, data: str) -> None:
        idx = self.findData(data)
        if idx >= 0:
            self.setCurrentIndex(idx)


# Column indices for the config table.
COL_CHARACTER = 0
COL_VOICE = 1
COL_MODEL = 2
COL_PRESET = 3
COL_SPEED = 4
COL_STABILITY = 5
COL_SIMILARITY = 6
COL_STYLE = 7
COL_SPEAKER_BOOST = 8
COL_PREVIEW = 9
COL_DUPLICATE = 10

HEADERS = [
    "Character", "Voice", "Model", "Preset", "Speed",
    "Stability", "Similarity", "Style", "Spk Boost", "Preview", "Duplicate",
]


class CharacterConfigTable(QTableWidget):
    """Table where each row configures the voice for one character."""

    preview_requested = Signal(str)       # character name
    duplicate_requested = Signal(str)     # source character name

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setColumnCount(len(HEADERS))
        self.setHorizontalHeaderLabels(HEADERS)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setSectionResizeMode(COL_VOICE, QHeaderView.Stretch)

        self._voices: list[Voice] = []
        # available model ids + capability map (model_id -> TTSModel)
        self._models: list[str] = list(AVAILABLE_MODELS)
        self._model_caps: dict = {}
        # row index -> dict of widgets for that row
        self._rows: dict[int, dict] = {}
        # character -> row index
        self._char_to_row: dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # Voices
    # ------------------------------------------------------------------ #
    def set_voices(self, voices: list[Voice]) -> None:
        """Update the voice list. Re-populates every row's voice dropdown,
        preserving the current selection where possible."""
        self._voices = voices
        items = [(_voice_label(v), v.voice_id) for v in voices]
        for row, widgets in self._rows.items():
            combo: SearchableComboBox = widgets["voice"]
            current = combo.currentData()
            combo.set_items(items)
            if current:
                combo.select_by_data(current)

    # ------------------------------------------------------------------ #
    # Models (model manager)
    # ------------------------------------------------------------------ #
    def set_models(self, model_ids: list[str], caps: Optional[dict] = None) -> None:
        """Update available models and their capability map. Re-populates every
        row's model dropdown and re-applies capability-based enabling."""
        self._models = list(model_ids) if model_ids else list(AVAILABLE_MODELS)
        self._model_caps = caps or {}
        for row, widgets in self._rows.items():
            combo: QComboBox = widgets["model"]
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(self._models)
            if current in self._models:
                combo.setCurrentText(current)
            elif current:
                combo.insertItem(0, current)
                combo.setCurrentIndex(0)
            combo.blockSignals(False)
            self._apply_model_caps(row)

    def _apply_model_caps(self, row: int) -> None:
        """Enable/disable the style slider and speaker-boost checkbox based on
        the selected model's capabilities (from the API)."""
        widgets = self._rows.get(row)
        if not widgets:
            return
        model_id = widgets["model"].currentText()
        cap = self._model_caps.get(model_id)
        # default to enabled when capabilities are unknown
        can_style = getattr(cap, "can_use_style", True) if cap else True
        can_boost = getattr(cap, "can_use_speaker_boost", True) if cap else True
        widgets["style"].setEnabled(can_style)
        widgets["style"].setToolTip("" if can_style else "Model does not support 'style'")
        widgets["boost"].setEnabled(can_boost)
        widgets["boost"].setToolTip("" if can_boost else "Model does not support speaker boost")

    # ------------------------------------------------------------------ #
    # Build rows from a list of character configs
    # ------------------------------------------------------------------ #
    def set_characters(self, configs: list[CharacterVoiceConfig]) -> None:
        self.setRowCount(0)
        self._rows.clear()
        self._char_to_row.clear()
        for cfg in configs:
            self._add_row(cfg)
        # apply capability-based enabling for the freshly built rows
        for row in list(self._rows):
            self._apply_model_caps(row)

    def _make_slider(self, value: float, maximum: float = 1.0) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, maximum)
        spin.setSingleStep(0.05)
        spin.setDecimals(2)
        spin.setValue(value)
        return spin

    def _add_row(self, cfg: CharacterVoiceConfig) -> None:
        row = self.rowCount()
        self.insertRow(row)

        # Character (read-only text)
        item = QTableWidgetItem(cfg.character)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        self.setItem(row, COL_CHARACTER, item)

        # Voice dropdown (searchable). The label includes the Vietnamese
        # gender/age descriptor so users can type "Nữ"/"Trẻ em" to filter.
        voice_combo = SearchableComboBox()
        items = [(_voice_label(v), v.voice_id) for v in self._voices]
        voice_combo.set_items(items)
        if cfg.voice_id:
            voice_combo.select_by_data(cfg.voice_id)
        self.setCellWidget(row, COL_VOICE, voice_combo)

        # Model dropdown (populated from API-loaded models, fallback to defaults)
        model_combo = QComboBox()
        model_combo.addItems(self._models)
        if cfg.model_id in self._models:
            model_combo.setCurrentText(cfg.model_id)
        else:
            model_combo.insertItem(0, cfg.model_id)
            model_combo.setCurrentIndex(0)
        self.setCellWidget(row, COL_MODEL, model_combo)

        # Preset dropdown
        preset_combo = QComboBox()
        preset_combo.addItems(list(PRESETS.keys()))
        if cfg.preset in PRESETS:
            preset_combo.setCurrentText(cfg.preset)
        self.setCellWidget(row, COL_PRESET, preset_combo)

        # Sliders
        speed = self._make_slider(cfg.settings.speed, maximum=2.0)
        speed.setRange(0.25, 2.0)
        stability = self._make_slider(cfg.settings.stability)
        similarity = self._make_slider(cfg.settings.similarity_boost)
        style = self._make_slider(cfg.settings.style)
        self.setCellWidget(row, COL_SPEED, speed)
        self.setCellWidget(row, COL_STABILITY, stability)
        self.setCellWidget(row, COL_SIMILARITY, similarity)
        self.setCellWidget(row, COL_STYLE, style)

        # Speaker boost checkbox (centered)
        boost = QCheckBox()
        boost.setChecked(cfg.settings.use_speaker_boost)
        boost_wrap = QWidget()
        bl = QHBoxLayout(boost_wrap)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setAlignment(Qt.AlignCenter)
        bl.addWidget(boost)
        self.setCellWidget(row, COL_SPEAKER_BOOST, boost_wrap)

        # Preview button
        preview_btn = QPushButton("▶ Preview")
        preview_btn.clicked.connect(
            lambda _=False, c=cfg.character: self.preview_requested.emit(c)
        )
        self.setCellWidget(row, COL_PREVIEW, preview_btn)

        # Duplicate button
        dup_btn = QPushButton("Copy →")
        dup_btn.setToolTip("Duplicate this voice config to other characters")
        dup_btn.clicked.connect(
            lambda _=False, c=cfg.character: self.duplicate_requested.emit(c)
        )
        self.setCellWidget(row, COL_DUPLICATE, dup_btn)

        # When the preset changes, push its values into the sliders.
        def on_preset_changed(_index: int, r=row) -> None:
            self._apply_preset_to_row(r)

        preset_combo.currentIndexChanged.connect(on_preset_changed)

        # When the model changes, enable/disable style + speaker boost per caps.
        def on_model_changed(_index: int, r=row) -> None:
            self._apply_model_caps(r)

        model_combo.currentIndexChanged.connect(on_model_changed)

        self._rows[row] = {
            "character": cfg.character,
            "voice": voice_combo,
            "model": model_combo,
            "preset": preset_combo,
            "speed": speed,
            "stability": stability,
            "similarity": similarity,
            "style": style,
            "boost": boost,
        }
        self._char_to_row[cfg.character] = row

    def _apply_preset_to_row(self, row: int) -> None:
        widgets = self._rows.get(row)
        if not widgets:
            return
        preset_name = widgets["preset"].currentText()
        preset = PRESETS.get(preset_name)
        if not preset:
            return
        widgets["stability"].setValue(preset["stability"])
        widgets["similarity"].setValue(preset["similarity_boost"])
        widgets["style"].setValue(preset["style"])
        widgets["boost"].setChecked(preset["use_speaker_boost"])
        widgets["speed"].setValue(preset["speed"])

    # ------------------------------------------------------------------ #
    # Read current config out of the table
    # ------------------------------------------------------------------ #
    def get_config_for(self, character: str) -> Optional[CharacterVoiceConfig]:
        row = self._char_to_row.get(character)
        if row is None:
            return None
        return self._read_row(row)

    def _read_row(self, row: int) -> CharacterVoiceConfig:
        w = self._rows[row]
        voice_id = w["voice"].currentData() or ""
        voice_name = ""
        for v in self._voices:
            if v.voice_id == voice_id:
                voice_name = v.name
                break
        settings = VoiceSettings(
            stability=w["stability"].value(),
            similarity_boost=w["similarity"].value(),
            style=w["style"].value(),
            use_speaker_boost=w["boost"].isChecked(),
            speed=w["speed"].value(),
        )
        return CharacterVoiceConfig(
            character=w["character"],
            voice_id=voice_id,
            voice_name=voice_name,
            model_id=w["model"].currentText(),
            preset=w["preset"].currentText(),
            settings=settings,
        )

    def get_all_configs(self) -> dict[str, CharacterVoiceConfig]:
        return {self._rows[r]["character"]: self._read_row(r) for r in self._rows}

    def character_names(self) -> list[str]:
        return [self._rows[r]["character"] for r in sorted(self._rows)]

    # ------------------------------------------------------------------ #
    # Auto-assign distinct voices, and duplicate config
    # ------------------------------------------------------------------ #
    def auto_assign_voices(self) -> None:
        """Assign a different voice to each character (cycles if there are more
        characters than voices)."""
        if not self._voices:
            return
        for i, row in enumerate(sorted(self._rows)):
            voice = self._voices[i % len(self._voices)]
            self._rows[row]["voice"].select_by_data(voice.voice_id)

    def apply_config_to(self, target_character: str, source: CharacterVoiceConfig) -> None:
        """Copy ``source`` settings onto ``target_character``'s row (keeps the
        target's own character name)."""
        row = self._char_to_row.get(target_character)
        if row is None:
            return
        w = self._rows[row]
        w["voice"].select_by_data(source.voice_id)
        w["model"].setCurrentText(source.model_id)
        if source.preset in PRESETS:
            w["preset"].setCurrentText(source.preset)
        w["stability"].setValue(source.settings.stability)
        w["similarity"].setValue(source.settings.similarity_boost)
        w["style"].setValue(source.settings.style)
        w["boost"].setChecked(source.settings.use_speaker_boost)
        w["speed"].setValue(source.settings.speed)
