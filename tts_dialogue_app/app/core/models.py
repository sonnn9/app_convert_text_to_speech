"""Data models for the TTS Dialogue App.

We use plain ``dataclasses`` (stdlib only) to keep dependencies light and the
``.exe`` small. Each model knows how to serialize to / from a plain ``dict`` so
that projects can be saved as JSON (see ``project_manager.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# Voice settings (maps directly to ElevenLabs ``voice_settings`` payload)
# --------------------------------------------------------------------------- #
@dataclass
class VoiceSettings:
    """ElevenLabs voice settings.

    ``speed`` is kept here for convenience. Note that ElevenLabs does NOT
    support ``speed`` for every model/endpoint. When the API rejects it we fall
    back to changing the playback speed locally with pydub (see
    ``audio_processor.change_speed``).
    """

    stability: float = 0.5
    similarity_boost: float = 0.75
    style: float = 0.0
    use_speaker_boost: bool = True
    speed: float = 1.0  # 1.0 == normal speed

    def to_api_dict(self, include_speed: bool = True) -> dict[str, Any]:
        """Build the ``voice_settings`` dict sent to the ElevenLabs API.

        ``speed`` is only included when ``include_speed`` is True. Some models
        reject unknown keys, so the client decides whether to send it.
        """
        data: dict[str, Any] = {
            "stability": self.stability,
            "similarity_boost": self.similarity_boost,
            "style": self.style,
            "use_speaker_boost": self.use_speaker_boost,
        }
        if include_speed:
            data["speed"] = self.speed
        return data

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VoiceSettings":
        return cls(
            stability=float(data.get("stability", 0.5)),
            similarity_boost=float(data.get("similarity_boost", 0.75)),
            style=float(data.get("style", 0.0)),
            use_speaker_boost=bool(data.get("use_speaker_boost", True)),
            speed=float(data.get("speed", 1.0)),
        )


# --------------------------------------------------------------------------- #
# A voice as returned by GET /v1/voices
# --------------------------------------------------------------------------- #
@dataclass
class Voice:
    voice_id: str
    name: str
    category: Optional[str] = None
    preview_url: Optional[str] = None
    # ElevenLabs ``labels`` (gender, age, accent, use_case, description, ...)
    labels: dict[str, str] = field(default_factory=dict)
    # For Voice Library (shared) voices: the owner id needed to add the voice
    # to the account before it can be used for TTS. None for account voices.
    public_owner_id: Optional[str] = None

    @property
    def is_shared(self) -> bool:
        return bool(self.public_owner_id)

    def language(self) -> str:
        return (self.labels.get("language") or "").lower()

    # ------------------ gender / age helpers (for the UI) ------------------ #
    def gender(self) -> str:
        """Raw gender label, lowercased ('male' / 'female' / 'neutral' / '')."""
        return (self.labels.get("gender") or "").lower()

    def age(self) -> str:
        """Raw age label ('young' / 'middle_aged' / 'old' / '')."""
        return (self.labels.get("age") or "").lower()

    def is_child(self) -> bool:
        """Heuristic: detect a child / kid voice from the labels, name or
        description (ElevenLabs has no dedicated 'child' gender)."""
        blob = " ".join([
            self.name or "",
            self.labels.get("age", "") or "",
            self.labels.get("description", "") or "",
            self.labels.get("use_case", "") or "",
        ]).lower()
        return any(k in blob for k in ("child", "kid", "children", "toddler", "baby"))

    def descriptor(self) -> str:
        """Human-friendly Vietnamese descriptor for the dropdown, e.g.
        'Nữ · trung niên' or 'Trẻ em' or 'Nam'."""
        if self.is_child():
            return "Trẻ em"  # don't append an age for child voices
        g = self.gender()
        base = {"male": "Nam", "female": "Nữ"}.get(g, "Khác" if g else "")
        a = self.age()
        age_vi = {
            "young": "trẻ",
            "middle_aged": "trung niên",
            "middle aged": "trung niên",
            "old": "lớn tuổi",
        }.get(a, "")
        parts = [p for p in (base, age_vi) if p]
        return " · ".join(parts) if parts else "Không rõ"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Voice":
        labels = data.get("labels") or {}
        if not isinstance(labels, dict):
            labels = {}
        return cls(
            voice_id=data.get("voice_id", ""),
            name=data.get("name", "Unknown"),
            category=data.get("category"),
            preview_url=data.get("preview_url"),
            labels={str(k): str(v) for k, v in labels.items()},
        )

    @classmethod
    def from_shared_api(cls, data: dict[str, Any]) -> "Voice":
        """Build from a /v1/shared-voices entry. Unlike /v1/voices, gender / age /
        language / accent are TOP-LEVEL fields here, so we fold them into
        ``labels`` for a consistent UI/descriptor."""
        labels = {
            "gender": str(data.get("gender", "") or ""),
            "age": str(data.get("age", "") or ""),
            "language": str(data.get("language", "") or ""),
            "accent": str(data.get("accent", "") or ""),
            "descriptive": str(data.get("descriptive", "") or ""),
            "use_case": str(data.get("use_case", "") or ""),
            "description": str(data.get("description", "") or ""),
        }
        return cls(
            voice_id=data.get("voice_id", ""),
            name=data.get("name", "Unknown"),
            category=data.get("category", "library"),
            preview_url=data.get("preview_url"),
            labels={k: v for k, v in labels.items() if v},
            public_owner_id=data.get("public_owner_id"),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Voice":
        labels = data.get("labels") or {}
        if not isinstance(labels, dict):
            labels = {}
        return cls(
            voice_id=data.get("voice_id", ""),
            name=data.get("name", "Unknown"),
            category=data.get("category"),
            preview_url=data.get("preview_url"),
            labels={str(k): str(v) for k, v in labels.items()},
            public_owner_id=data.get("public_owner_id"),
        )


# --------------------------------------------------------------------------- #
# A TTS model as returned by GET /v1/models
# --------------------------------------------------------------------------- #
@dataclass
class TTSModel:
    model_id: str
    name: str = ""
    can_do_text_to_speech: bool = True
    can_use_style: bool = True
    can_use_speaker_boost: bool = True
    can_do_text_to_dialogue: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "TTSModel":
        return cls(
            model_id=data.get("model_id", ""),
            name=data.get("name", data.get("model_id", "")),
            can_do_text_to_speech=bool(data.get("can_do_text_to_speech", True)),
            can_use_style=bool(data.get("can_use_style", True)),
            can_use_speaker_boost=bool(data.get("can_use_speaker_boost", True)),
            can_do_text_to_dialogue=bool(data.get("can_do_text_to_dialogue", False)),
        )


# --------------------------------------------------------------------------- #
# Per-character voice configuration (one row in the config table)
# --------------------------------------------------------------------------- #
@dataclass
class CharacterVoiceConfig:
    character: str
    voice_id: str = ""
    voice_name: str = ""
    model_id: str = "eleven_multilingual_v2"
    preset: str = "Neutral"
    settings: VoiceSettings = field(default_factory=VoiceSettings)
    color: str = "#cccccc"  # used as a color tag in the queue view

    def to_dict(self) -> dict[str, Any]:
        return {
            "character": self.character,
            "voice_id": self.voice_id,
            "voice_name": self.voice_name,
            "model_id": self.model_id,
            "preset": self.preset,
            "settings": self.settings.to_dict(),
            "color": self.color,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CharacterVoiceConfig":
        return cls(
            character=data.get("character", ""),
            voice_id=data.get("voice_id", ""),
            voice_name=data.get("voice_name", ""),
            model_id=data.get("model_id", "eleven_multilingual_v2"),
            preset=data.get("preset", "Neutral"),
            settings=VoiceSettings.from_dict(data.get("settings", {})),
            color=data.get("color", "#cccccc"),
        )


# --------------------------------------------------------------------------- #
# Dialogue line + processing status
# --------------------------------------------------------------------------- #
class LineStatus(str, Enum):
    PENDING = "Pending"
    PROCESSING = "Processing"
    DONE = "Done"
    ERROR = "Error"


@dataclass
class DialogueLine:
    index: int
    character: str
    text: str  # original text (used for subtitles/export)
    raw_line: str = ""

    # scene grouping for video workflows (1 == first scene). 0 means "unset".
    scene: int = 0

    # runtime fields (filled during conversion)
    status: LineStatus = LineStatus.PENDING
    output_file: Optional[str] = None
    duration: float = 0.0  # seconds
    error: Optional[str] = None

    # text actually sent to the API after pronunciation substitution. When
    # empty, ``text`` is used as-is. The original ``text`` is always preserved
    # for subtitles / timeline export.
    processed_text: str = ""

    # True when the last conversion was served from the local cache (no API call).
    from_cache: bool = False

    def api_text(self) -> str:
        """Text to send to the TTS API (processed if present, else original)."""
        return self.processed_text or self.text

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "character": self.character,
            "text": self.text,
            "raw_line": self.raw_line,
            "scene": self.scene,
            "status": self.status.value,
            "output_file": self.output_file,
            "duration": self.duration,
            "error": self.error,
            "processed_text": self.processed_text,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DialogueLine":
        return cls(
            index=int(data.get("index", 0)),
            character=data.get("character", ""),
            text=data.get("text", ""),
            raw_line=data.get("raw_line", ""),
            scene=int(data.get("scene", 0)),
            status=LineStatus(data.get("status", "Pending")),
            output_file=data.get("output_file"),
            duration=float(data.get("duration", 0.0)),
            error=data.get("error"),
            processed_text=data.get("processed_text", ""),
        )


# --------------------------------------------------------------------------- #
# Project-wide settings (output, silence, format, ...)
# --------------------------------------------------------------------------- #
@dataclass
class ProjectSettings:
    output_folder: str = ""
    project_name: str = "my_project"
    output_format: str = "mp3"  # "mp3" or "wav"

    silence_between_lines_ms: int = 300
    speaker_change_silence_ms: int = 500

    auto_merge_after_convert: bool = True
    save_each_line: bool = True
    save_grouped_by_character: bool = False

    normalize_volume: bool = False

    # how to treat a line that has no ``:`` separator
    # "narrator"  -> assign to Narrator character
    # "append"    -> append to the previous line's text
    unknown_line_mode: str = "narrator"

    # ------- new advanced settings (v2) ------- #
    # convert mode: "line" (line-by-line TTS) | "dialogue" (Text-to-Dialogue API) | "auto"
    convert_mode: str = "line"
    default_model_id: str = "eleven_multilingual_v2"

    # batching
    max_chars_per_batch: int = 1500

    # cache
    cache_enabled: bool = True

    # scene grouping: "per_line" | "per_speaker_change" | "per_n_lines" | "manual"
    scene_mode: str = "per_line"
    scene_n_lines: int = 1

    # pronunciation
    apply_pronunciation_to_conversion: bool = True
    apply_pronunciation_to_preview: bool = True

    # audio post-processing
    trim_silence: bool = False
    fade_in_ms: int = 0
    fade_out_ms: int = 0
    export_sample_rate: int = 44100  # 44100 | 48000
    export_bitrate: str = "192k"     # 128k | 192k | 320k

    # rate-limit retry
    max_retries: int = 4
    rate_limit_base_delay: float = 2.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectSettings":
        s = cls()
        for k, v in data.items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s
