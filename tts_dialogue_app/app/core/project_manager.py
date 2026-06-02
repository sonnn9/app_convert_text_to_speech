"""Save / load a whole project to a ``.json`` file.

A project bundles everything needed to reopen work later:
    * original text
    * parsed dialogue lines
    * character -> voice mapping
    * project settings
    * output folder
    * generated files (captured inside each dialogue line's ``output_file``)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .models import CharacterVoiceConfig, DialogueLine, ProjectSettings

PROJECT_FORMAT_VERSION = 1


@dataclass
class Project:
    """In-memory representation of a project."""

    original_text: str = ""
    lines: list[DialogueLine] = field(default_factory=list)
    character_configs: dict[str, CharacterVoiceConfig] = field(default_factory=dict)
    settings: ProjectSettings = field(default_factory=ProjectSettings)
    # pronunciation rules stored as plain dicts (see PronunciationManager.to_list)
    pronunciation_rules: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------ serialize ----------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": PROJECT_FORMAT_VERSION,
            "original_text": self.original_text,
            "lines": [ln.to_dict() for ln in self.lines],
            "character_configs": {
                name: cfg.to_dict() for name, cfg in self.character_configs.items()
            },
            "settings": self.settings.to_dict(),
            "pronunciation_rules": self.pronunciation_rules,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        return cls(
            original_text=data.get("original_text", ""),
            lines=[DialogueLine.from_dict(d) for d in data.get("lines", [])],
            character_configs={
                name: CharacterVoiceConfig.from_dict(cfg)
                for name, cfg in data.get("character_configs", {}).items()
            },
            settings=ProjectSettings.from_dict(data.get("settings", {})),
            pronunciation_rules=data.get("pronunciation_rules", []),
        )


class ProjectManager:
    """Static helpers for reading/writing :class:`Project` JSON files."""

    @staticmethod
    def save(project: Project, path: str) -> str:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(project.to_dict(), f, indent=2, ensure_ascii=False)
        return path

    @staticmethod
    def load(path: str) -> Project:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Project.from_dict(data)
