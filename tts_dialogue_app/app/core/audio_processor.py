"""Audio post-processing built on pydub (which shells out to ffmpeg).

Responsibilities:
    * change_speed   -> local speed change when the API didn't support ``speed``
    * merge_dialogue -> concatenate lines in order with configurable silences
    * export_by_character -> one combined file per character
    * normalize      -> loudness normalization across clips
    * get_duration   -> read clip length (used to fill the queue + build .srt)
    * generate_srt   -> simple subtitle file based on merged durations

ffmpeg must be available (on PATH, or ffmpeg.exe next to the app). See README.
"""

from __future__ import annotations

import os
from typing import Optional

from pydub import AudioSegment
from pydub.effects import normalize as _pydub_normalize

from .models import DialogueLine


# --------------------------------------------------------------------------- #
# ffmpeg discovery: if an ffmpeg.exe sits next to the app, point pydub at it.
# --------------------------------------------------------------------------- #
def configure_ffmpeg(app_dir: str) -> None:
    """If ``ffmpeg.exe`` / ``ffprobe.exe`` are bundled next to the app, tell
    pydub to use them. Otherwise pydub falls back to PATH."""
    ffmpeg = os.path.join(app_dir, "ffmpeg.exe")
    ffprobe = os.path.join(app_dir, "ffprobe.exe")
    if os.path.exists(ffmpeg):
        AudioSegment.converter = ffmpeg
        # also expose to environment for child tools
        os.environ["PATH"] = app_dir + os.pathsep + os.environ.get("PATH", "")
    if os.path.exists(ffprobe):
        AudioSegment.ffprobe = ffprobe


class AudioProcessor:
    """Stateless helpers around pydub."""

    # ----------------------------- load / save ---------------------------- #
    @staticmethod
    def load(path: str) -> AudioSegment:
        return AudioSegment.from_file(path)

    @staticmethod
    def get_duration(path: str) -> float:
        """Return duration in seconds (0.0 on failure)."""
        try:
            seg = AudioSegment.from_file(path)
            return round(len(seg) / 1000.0, 3)
        except Exception:
            return 0.0

    @staticmethod
    def export(seg: AudioSegment, path: str, fmt: str) -> str:
        """Export a segment to ``path`` in ``fmt`` ('mp3' or 'wav')."""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        seg.export(path, format=fmt)
        return path

    # ------------------------------- speed -------------------------------- #
    @staticmethod
    def change_speed(path: str, speed: float, output_path: Optional[str] = None) -> str:
        """Change playback speed by ``speed`` factor WITHOUT changing pitch
        is not trivial with pure pydub; we use frame-rate manipulation which
        is fast and good enough for dialogue. ``speed`` 1.0 == no change.

        This is the fallback used when the ElevenLabs model didn't accept the
        ``speed`` parameter.
        """
        if abs(speed - 1.0) < 1e-3:
            return path  # no-op

        seg = AudioSegment.from_file(path)
        # Speed up / slow down by changing the sample rate, then reset the
        # declared frame rate so players play at normal rate -> tempo changes.
        new_frame_rate = int(seg.frame_rate * speed)
        if new_frame_rate <= 0:
            return path
        sped = seg._spawn(seg.raw_data, overrides={"frame_rate": new_frame_rate})
        sped = sped.set_frame_rate(seg.frame_rate)

        out = output_path or path
        fmt = os.path.splitext(out)[1].lstrip(".").lower() or "mp3"
        sped.export(out, format=fmt)
        return out

    # ----------------------------- normalize ------------------------------ #
    @staticmethod
    def normalize_segment(seg: AudioSegment) -> AudioSegment:
        """Normalize loudness of a single segment."""
        try:
            return _pydub_normalize(seg)
        except Exception:
            return seg

    # ------------------------------- merge -------------------------------- #
    @staticmethod
    def merge_dialogue(
        lines: list[DialogueLine],
        output_path: str,
        fmt: str = "mp3",
        silence_between_lines_ms: int = 300,
        speaker_change_silence_ms: int = 500,
        normalize: bool = False,
    ) -> str:
        """Concatenate the per-line audio in dialogue order.

        Adds ``silence_between_lines_ms`` between consecutive lines, and the
        longer ``speaker_change_silence_ms`` when the speaker changes.
        Only lines that have a valid ``output_file`` are included.
        """
        merged = AudioSegment.empty()
        prev_character: Optional[str] = None

        valid = [ln for ln in lines if ln.output_file and os.path.exists(ln.output_file)]
        if not valid:
            raise RuntimeError("No converted audio lines available to merge.")

        for i, line in enumerate(valid):
            seg = AudioSegment.from_file(line.output_file)
            if normalize:
                seg = AudioProcessor.normalize_segment(seg)

            if i > 0:
                gap = (
                    speaker_change_silence_ms
                    if line.character != prev_character
                    else silence_between_lines_ms
                )
                if gap > 0:
                    merged += AudioSegment.silent(duration=gap)

            merged += seg
            prev_character = line.character

        return AudioProcessor.export(merged, output_path, fmt)

    # -------------------------- export by character ------------------------ #
    @staticmethod
    def export_by_character(
        lines: list[DialogueLine],
        output_dir: str,
        fmt: str = "mp3",
        silence_between_lines_ms: int = 300,
        normalize: bool = False,
    ) -> dict[str, str]:
        """Create one combined file per character. Returns {character: path}."""
        os.makedirs(output_dir, exist_ok=True)
        by_char: dict[str, list[DialogueLine]] = {}
        for ln in lines:
            if ln.output_file and os.path.exists(ln.output_file):
                by_char.setdefault(ln.character, []).append(ln)

        results: dict[str, str] = {}
        for character, char_lines in by_char.items():
            combined = AudioSegment.empty()
            for i, line in enumerate(char_lines):
                seg = AudioSegment.from_file(line.output_file)
                if normalize:
                    seg = AudioProcessor.normalize_segment(seg)
                if i > 0 and silence_between_lines_ms > 0:
                    combined += AudioSegment.silent(duration=silence_between_lines_ms)
                combined += seg

            # sanitize file name
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in character)
            out_path = os.path.join(output_dir, f"{safe}_all.{fmt}")
            AudioProcessor.export(combined, out_path, fmt)
            results[character] = out_path

        return results

    # -------------------------------- srt --------------------------------- #
    @staticmethod
    def generate_srt(
        lines: list[DialogueLine],
        output_path: str,
        silence_between_lines_ms: int = 300,
        speaker_change_silence_ms: int = 500,
    ) -> str:
        """Generate a simple .srt subtitle file using each line's duration,
        accounting for the same silences used while merging (so timings line
        up with full_dialogue audio)."""

        def fmt_ts(seconds: float) -> str:
            ms = int(round(seconds * 1000))
            h, ms = divmod(ms, 3_600_000)
            m, ms = divmod(ms, 60_000)
            s, ms = divmod(ms, 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        valid = [ln for ln in lines if ln.output_file and os.path.exists(ln.output_file)]
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        cursor = 0.0
        prev_character: Optional[str] = None
        entries: list[str] = []

        for i, line in enumerate(valid):
            if i > 0:
                gap = (
                    speaker_change_silence_ms
                    if line.character != prev_character
                    else silence_between_lines_ms
                )
                cursor += gap / 1000.0

            duration = line.duration or AudioProcessor.get_duration(line.output_file)
            start = cursor
            end = cursor + duration
            entries.append(
                f"{i + 1}\n{fmt_ts(start)} --> {fmt_ts(end)}\n"
                f"{line.character}: {line.text}\n"
            )
            cursor = end
            prev_character = line.character

        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(entries))
        return output_path
