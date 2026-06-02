"""Timeline construction and export for video-editing workflows.

This module turns a list of converted :class:`~app.core.models.DialogueLine`
objects into a flat *timeline*: an ordered list of row dicts describing, for
each converted line, exactly where it sits on a global time axis (start/end in
seconds), which character speaks it, and which audio file / voice produced it.

The timing model intentionally mirrors the audio-merge logic so that the
exported timeline lines up *exactly* with the merged audio track:

* Each converted line occupies ``line.duration`` seconds (the real measured
  length of its rendered audio).
* Between two consecutive lines a silence gap is inserted. The gap length
  depends on whether the speaker changed:
      - ``speaker_change_silence_ms`` when the character differs from the
        previous line, otherwise
      - ``silence_between_lines_ms``.
* No silence is added before the very first line.

The resulting rows can be exported as CSV (for spreadsheet / NLE import) or
JSON (for programmatic pipelines).

Only stdlib is used here (``json``, ``csv``, ``os``); no audio is decoded
because durations are already known on each line.
"""

from __future__ import annotations

import csv
import json
import os
from typing import Any

# Import existing models (do NOT redefine them).
from app.core.models import CharacterVoiceConfig, DialogueLine

# Column / key order used for both the timeline row dicts and the CSV header.
# Keep this single source of truth so CSV and JSON stay consistent.
TIMELINE_FIELDS: list[str] = [
    "scene_index",
    "line_index",
    "character",
    "text",
    "start_time",
    "end_time",
    "duration",
    "audio_file",
    "voice_name",
    "voice_id",
    "model_id",
]


class TimelineExporter:
    """Build and export a video-editing timeline from converted dialogue lines."""

    @staticmethod
    def compute(
        lines: list[DialogueLine],
        configs: dict[str, CharacterVoiceConfig],
        silence_between_lines_ms: int = 300,
        speaker_change_silence_ms: int = 500,
    ) -> list[dict]:
        """Compute timeline rows for all *converted* dialogue lines.

        A line is considered converted (and thus included) only when it has an
        ``output_file`` set and a positive ``duration``. Lines are processed in
        the order given.

        A running cursor (in seconds) tracks the current position on the global
        timeline. Before every included line except the first, a silence gap is
        added to the cursor:

        * ``speaker_change_silence_ms`` if the character differs from the
          previous included line's character, otherwise
        * ``silence_between_lines_ms``.

        Both gap values are given in milliseconds and converted to seconds.

        Args:
            lines: Dialogue lines (typically the full project queue).
            configs: Mapping of character name -> voice configuration, used to
                resolve ``voice_name`` / ``voice_id`` / ``model_id``.
            silence_between_lines_ms: Silence inserted between two lines spoken
                by the same character.
            speaker_change_silence_ms: Silence inserted when the speaker changes.

        Returns:
            A list of row dicts. Each dict has exactly these keys:
            ``scene_index, line_index, character, text, start_time, end_time,
            duration, audio_file, voice_name, voice_id, model_id``.
            ``start_time``/``end_time``/``duration`` are floats in seconds,
            rounded to 3 decimals.
        """
        rows: list[dict] = []

        # Current position on the global timeline, in seconds.
        cursor: float = 0.0
        # Character of the previously emitted line (None before the first one).
        prev_character: str | None = None

        for line in lines:
            # Skip lines that were never successfully converted to audio.
            if not line.output_file or line.duration <= 0:
                continue

            # Insert the appropriate silence gap before every line except the
            # first one we emit.
            if prev_character is not None:
                if line.character != prev_character:
                    gap_ms = speaker_change_silence_ms
                else:
                    gap_ms = silence_between_lines_ms
                cursor += gap_ms / 1000.0

            start_time = cursor
            end_time = start_time + line.duration

            # Resolve voice metadata from the per-character config (if any).
            config = configs.get(line.character)
            if config is not None:
                voice_name = config.voice_name
                voice_id = config.voice_id
                model_id = config.model_id
            else:
                voice_name = ""
                voice_id = ""
                model_id = ""

            rows.append(
                {
                    "scene_index": getattr(line, "scene", 0),
                    "line_index": line.index,
                    "character": line.character,
                    # ORIGINAL text (for subtitles), never the processed text.
                    "text": line.text,
                    "start_time": round(start_time, 3),
                    "end_time": round(end_time, 3),
                    "duration": round(line.duration, 3),
                    "audio_file": line.output_file,
                    "voice_name": voice_name,
                    "voice_id": voice_id,
                    "model_id": model_id,
                }
            )

            # Advance cursor past this line's audio and remember the speaker.
            cursor = end_time
            prev_character = line.character

        return rows

    @staticmethod
    def export_csv(rows: list[dict], path: str) -> str:
        """Write timeline rows to a UTF-8 (BOM) CSV file.

        The BOM (``utf-8-sig``) is used so Excel opens the file with correct
        encoding for non-ASCII characters. The header is the fixed field order
        in :data:`TIMELINE_FIELDS`.

        Parent directories are created as needed. Returns the written path.
        """
        _ensure_parent_dir(path)

        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=TIMELINE_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

        return path

    @staticmethod
    def export_json(rows: list[dict], path: str) -> str:
        """Write timeline rows to a pretty-printed JSON file.

        Uses ``indent=2`` and ``ensure_ascii=False`` so non-ASCII text stays
        human-readable. Parent directories are created as needed. Returns the
        written path.
        """
        _ensure_parent_dir(path)

        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, ensure_ascii=False)

        return path


def _ensure_parent_dir(path: str) -> None:
    """Create the parent directory of *path* if it does not already exist."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
