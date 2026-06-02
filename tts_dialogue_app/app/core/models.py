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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Voice":
        return cls(
            voice_id=data.get("voice_id", ""),
            name=data.get("name", "Unknown"),
            category=data.get("category"),
            preview_url=data.get("preview_url"),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Voice":
        return cls(
            voice_id=data.get("voice_id", ""),
            name=data.get("name", "Unknown"),
            category=data.get("category"),
            preview_url=data.get("preview_url"),
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
