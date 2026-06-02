"""Export ``.srt`` subtitles from converted dialogue lines.

The subtitle timeline is reconstructed from the **real** audio durations that
were measured during conversion (``DialogueLine.duration``), inserting the same
silence gaps the audio merger uses between lines. The cue text always uses the
*original* ``DialogueLine.text`` (never ``processed_text``), so what the viewer
reads matches what was written rather than the pronunciation-substituted text
that was actually sent to the API.

Stdlib-only module (no third-party dependencies).
"""

from __future__ import annotations

import os
from typing import List

from app.core.models import DialogueLine


class SubtitleExporter:
    """Build SubRip (``.srt``) subtitle files from converted dialogue lines."""

    @staticmethod
    def format_timestamp(seconds: float) -> str:
        """Format ``seconds`` as an SRT timestamp ``"HH:MM:SS,mmm"``.

        Negative inputs are clamped to zero. Milliseconds are derived by
        rounding so that the textual timestamp stays close to the real value.
        """
        if seconds < 0:
            seconds = 0.0

        # Convert to integer milliseconds first to avoid floating point drift
        # when splitting into the individual time components.
        total_ms = int(round(seconds * 1000.0))

        ms = total_ms % 1000
        total_secs = total_ms // 1000
        secs = total_secs % 60
        total_mins = total_secs // 60
        mins = total_mins % 60
        hours = total_mins // 60

        return f"{hours:02d}:{mins:02d}:{secs:02d},{ms:03d}"

    @staticmethod
    def export_srt(
        lines: List["DialogueLine"],
        path: str,
        silence_between_lines_ms: int = 300,
        speaker_change_silence_ms: int = 500,
    ) -> str:
        """Write an ``.srt`` file for the converted lines and return ``path``.

        Only lines that were actually converted are included: those with an
        ``output_file`` set and a positive ``duration``. They are emitted in the
        order given. A playback cursor tracks the current position on the
        timeline; before every cue except the first, the appropriate silence
        gap is added to the cursor:

        * ``speaker_change_silence_ms`` when the speaking character differs from
          the previous converted line's character;
        * ``silence_between_lines_ms`` otherwise.

        Each cue contains a sequential index, a ``"start --> end"`` timing line,
        and a single text line ``"Character: text"`` built from the **original**
        ``DialogueLine.text``. Cues are separated by a blank line.

        Parent directories of ``path`` are created if needed and the file is
        written as UTF-8.
        """
        # Keep only lines that produced audio with a measurable duration.
        converted: List[DialogueLine] = [
            line
            for line in lines
            if line.output_file and line.duration and line.duration > 0
        ]

        cues: List[str] = []
        cursor_seconds = 0.0
        prev_character: str | None = None

        for cue_number, line in enumerate(converted, start=1):
            # Insert the silence gap before this cue (not before the first one).
            if prev_character is not None:
                if line.character != prev_character:
                    gap_ms = speaker_change_silence_ms
                else:
                    gap_ms = silence_between_lines_ms
                cursor_seconds += gap_ms / 1000.0

            start_seconds = cursor_seconds
            end_seconds = start_seconds + line.duration

            start_ts = SubtitleExporter.format_timestamp(start_seconds)
            end_ts = SubtitleExporter.format_timestamp(end_seconds)

            # Build the cue. Use the ORIGINAL text for the on-screen subtitle.
            cue_text = f"{line.character}: {line.text}"
            cues.append(
                f"{cue_number}\n{start_ts} --> {end_ts}\n{cue_text}\n"
            )

            # Advance the cursor past this line's audio for the next iteration.
            cursor_seconds = end_seconds
            prev_character = line.character

        # Cues are separated by a blank line; the trailing newline after the
        # final cue keeps players happy with a clean EOF.
        content = "\n".join(cues)

        # Ensure the destination directory exists before writing.
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)

        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

        return path
