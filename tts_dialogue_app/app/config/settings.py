"""App-level configuration: voice presets, available models, and local storage
of the API key.

The API key is stored locally (never hard-coded). We support both a ``.env``
file and a ``config.json`` file living next to the app. ``config.json`` is the
primary store; ``.env`` is read as a fallback (e.g. ELEVENLABS_API_KEY).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

try:
    from dotenv import load_dotenv  # python-dotenv
except Exception:  # pragma: no cover - dotenv is optional at runtime
    load_dotenv = None  # type: ignore


# --------------------------------------------------------------------------- #
# Available TTS models (editable: add new model IDs here as ElevenLabs ships
# them). The first entry is the default.
# --------------------------------------------------------------------------- #
AVAILABLE_MODELS: list[str] = [
    "eleven_multilingual_v2",
    "eleven_turbo_v2_5",
    "eleven_flash_v2_5",
    "eleven_v3",
]

DEFAULT_MODEL_ID: str = AVAILABLE_MODELS[0]


# --------------------------------------------------------------------------- #
# Voice style presets. Each maps to ElevenLabs voice_settings (+ a local speed).
# Users may still tweak the sliders manually after picking a preset.
# --------------------------------------------------------------------------- #
PRESETS: dict[str, dict[str, Any]] = {
    "Neutral": {
        "stability": 0.5, "similarity_boost": 0.75, "style": 0.0,
        "use_speaker_boost": True, "speed": 1.0,
    },
    "Fast": {
        "stability": 0.45, "similarity_boost": 0.75, "style": 0.2,
        "use_speaker_boost": True, "speed": 1.15,
    },
    "Slow": {
        "stability": 0.6, "similarity_boost": 0.8, "style": 0.1,
        "use_speaker_boost": True, "speed": 0.85,
    },
    "Strong / Powerful": {
        "stability": 0.35, "similarity_boost": 0.8, "style": 0.45,
        "use_speaker_boost": True, "speed": 1.0,
    },
    "Soft / Gentle": {
        "stability": 0.65, "similarity_boost": 0.8, "style": 0.25,
        "use_speaker_boost": True, "speed": 0.95,
    },
    "Emotional": {
        "stability": 0.35, "similarity_boost": 0.85, "style": 0.65,
        "use_speaker_boost": True, "speed": 0.95,
    },
    "Happy": {
        "stability": 0.4, "similarity_boost": 0.8, "style": 0.5,
        "use_speaker_boost": True, "speed": 1.05,
    },
    "Calm": {
        "stability": 0.7, "similarity_boost": 0.8, "style": 0.1,
        "use_speaker_boost": True, "speed": 0.95,
    },
    "Childlike": {
        "stability": 0.4, "similarity_boost": 0.7, "style": 0.4,
        "use_speaker_boost": True, "speed": 1.05,
    },
    "Narration": {
        "stability": 0.6, "similarity_boost": 0.85, "style": 0.15,
        "use_speaker_boost": True, "speed": 1.0,
    },
}

DEFAULT_PRESET = "Neutral"

# Distinct colors auto-assigned to characters (used as color tags in the queue).
CHARACTER_COLORS: list[str] = [
    "#4FC3F7", "#FF8A65", "#81C784", "#BA68C8", "#FFD54F",
    "#4DB6AC", "#F06292", "#9575CD", "#A1887F", "#90A4AE",
]


def get_app_dir() -> str:
    """Return the directory the app runs from.

    Works both when running from source and when frozen by PyInstaller
    (``--onefile`` extracts to a temp dir; we want the .exe's own folder for
    persistent config, so we use the executable path when frozen).
    """
    if getattr(sys, "frozen", False):  # PyInstaller
        return os.path.dirname(sys.executable)
    # project root = two levels up from this file (app/config/settings.py)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


CONFIG_PATH = os.path.join(get_app_dir(), "config.json")
ENV_PATH = os.path.join(get_app_dir(), ".env")
RECENT_PROJECTS_KEY = "recent_projects"
MAX_RECENT = 10


def get_cache_dir() -> str:
    """Directory holding the local audio cache (shared across projects)."""
    return os.path.join(get_app_dir(), "cache")


class AppConfig:
    """Loads / saves local configuration (API key, recent projects, ...)."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self.load()

    # ----------------------------- load / save ----------------------------- #
    def load(self) -> None:
        # 1) config.json (primary)
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except (OSError, json.JSONDecodeError):
                self._data = {}

        # 2) .env fallback for the API key
        if not self._data.get("api_key"):
            if load_dotenv is not None and os.path.exists(ENV_PATH):
                load_dotenv(ENV_PATH)
            env_key = os.environ.get("ELEVENLABS_API_KEY")
            if env_key:
                self._data["api_key"] = env_key

    def save(self) -> None:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
        except OSError:
            pass  # non-fatal: app still works without persistence

    # ------------------------------ accessors ------------------------------ #
    @property
    def api_key(self) -> str:
        return self._data.get("api_key", "")

    @api_key.setter
    def api_key(self, value: str) -> None:
        self._data["api_key"] = value
        self.save()

    @property
    def default_model(self) -> str:
        return self._data.get("default_model", DEFAULT_MODEL_ID)

    @default_model.setter
    def default_model(self, value: str) -> None:
        self._data["default_model"] = value
        self.save()

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.save()

    # -------------------------- recent projects ---------------------------- #
    def add_recent_project(self, path: str) -> None:
        recent: list[str] = self._data.get(RECENT_PROJECTS_KEY, [])
        if path in recent:
            recent.remove(path)
        recent.insert(0, path)
        self._data[RECENT_PROJECTS_KEY] = recent[:MAX_RECENT]
        self.save()

    def recent_projects(self) -> list[str]:
        return self._data.get(RECENT_PROJECTS_KEY, [])
