"""Rich audio post-processing pipeline built on :mod:`pydub`.

This module supersedes the simpler :mod:`app.core.audio_processor` for the
*merging* stage: it offers per-line normalization, leading/trailing silence
trimming, fade-in/out, sample-rate conversion and mp3 bitrate control, all
driven by a single :class:`PostProcessOptions` value object.

Responsibilities:
    * trim_silence       -> strip leading & trailing silence from one segment
    * process_segment    -> apply the full per-segment chain (trim/normalize/
                            sample-rate/fades)
    * export             -> write a segment with the chosen format/bitrate/rate
    * merge_dialogue     -> concatenate per-line clips (in order) with gaps
    * export_by_character -> one combined file per character

IMPORTANT: pydub shells out to **ffmpeg** at runtime for any decode/encode
operation (loading mp3, exporting mp3/wav, resampling, etc.). ``ffmpeg`` (and
``ffprobe``) must be available on PATH, or configured via
``app.core.audio_processor.configure_ffmpeg``. Without ffmpeg these calls raise
at runtime even though this module imports cleanly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# pydub is the only permitted third-party dependency for audio work.
from pydub import AudioSegment
from pydub.effects import normalize as _normalize
from pydub.silence import detect_leading_silence

# Reuse the shared data model rather than redefining it.
from app.core.models import DialogueLine


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #
@dataclass
class PostProcessOptions:
    """Tunable knobs for the post-processing pipeline.

    Attributes:
        normalize: Apply pydub loudness normalization to each segment.
        trim_silence: Strip leading & trailing silence from each segment.
        fade_in_ms: Fade-in duration in milliseconds (0 disables).
        fade_out_ms: Fade-out duration in milliseconds (0 disables).
        sample_rate: Target sample rate in Hz (typically 44100 or 48000).
        bitrate: Target mp3 bitrate string passed to ffmpeg (e.g. "192k").
        silence_between_lines_ms: Gap inserted between consecutive lines spoken
            by the *same* character.
        speaker_change_silence_ms: Gap inserted when the speaker changes.
    """

    normalize: bool = False
    trim_silence: bool = False
    fade_in_ms: int = 0
    fade_out_ms: int = 0
    sample_rate: int = 44100          # 44100 | 48000
    bitrate: str = "192k"             # mp3 bitrate
    silence_between_lines_ms: int = 300
    speaker_change_silence_ms: int = 500


# --------------------------------------------------------------------------- #
# Processor
# --------------------------------------------------------------------------- #
class AudioPostProcessor:
    """Stateless helpers implementing the post-processing pipeline.

    All methods are ``@staticmethod`` because there is no shared state; the
    behaviour is fully determined by the passed-in :class:`PostProcessOptions`.
    """

    @staticmethod
    def trim_silence(seg: "AudioSegment", silence_thresh_db: int = -45) -> "AudioSegment":
        """Trim leading and trailing silence from ``seg``.

        Uses :func:`pydub.silence.detect_leading_silence` on the segment to find
        the leading silence, then on its reverse to find the trailing silence,
        and slices the segment to the remaining audible region.

        Args:
            seg: The audio segment to trim.
            silence_thresh_db: Threshold (in dBFS) below which audio counts as
                silence. More negative == stricter (only very quiet counts).

        Returns:
            A new, trimmed :class:`AudioSegment`. If the whole clip is below the
            threshold (i.e. effectively silent), the original segment is
            returned unchanged to avoid producing an empty clip.
        """
        # Number of leading milliseconds that are silent.
        start_trim = detect_leading_silence(seg, silence_threshold=silence_thresh_db)
        # Reverse the segment and reuse the same detector for trailing silence.
        end_trim = detect_leading_silence(seg.reverse(), silence_threshold=silence_thresh_db)

        duration = len(seg)
        # Guard against fully-silent clips where start_trim >= the audible end.
        if start_trim >= duration - end_trim:
            return seg
        return seg[start_trim:duration - end_trim]

    @staticmethod
    def process_segment(seg: "AudioSegment", opts: PostProcessOptions) -> "AudioSegment":
        """Apply the per-segment processing chain.

        Order of operations (each step is conditional on ``opts``):
            1. trim leading/trailing silence (``opts.trim_silence``)
            2. loudness normalization (``opts.normalize``)
            3. resample to ``opts.sample_rate``
            4. fade-in (``opts.fade_in_ms`` > 0)
            5. fade-out (``opts.fade_out_ms`` > 0)

        Args:
            seg: Source segment.
            opts: Processing options.

        Returns:
            The processed :class:`AudioSegment`.
        """
        if opts.trim_silence:
            seg = AudioPostProcessor.trim_silence(seg)

        if opts.normalize:
            # pydub's normalize maximizes headroom without clipping.
            seg = _normalize(seg)

        # Resampling is always applied so merged output has a consistent rate.
        seg = seg.set_frame_rate(opts.sample_rate)

        if opts.fade_in_ms > 0:
            seg = seg.fade_in(opts.fade_in_ms)
        if opts.fade_out_ms > 0:
            seg = seg.fade_out(opts.fade_out_ms)

        return seg

    @staticmethod
    def export(seg: "AudioSegment", path: str, fmt: str, opts: PostProcessOptions) -> str:
        """Export ``seg`` to ``path`` in ``fmt`` honouring rate/bitrate options.

        Creates parent directories as needed. For mp3 the bitrate is passed to
        ffmpeg via ``bitrate=opts.bitrate``; the sample rate is enforced for any
        format via the ffmpeg ``-ar`` output parameter (and the segment has
        usually already been resampled by :meth:`process_segment`).

        Args:
            seg: Segment to write.
            path: Destination file path.
            fmt: Container/codec, e.g. "mp3" or "wav".
            opts: Options carrying ``bitrate`` and ``sample_rate``.

        Returns:
            The ``path`` written (for convenient chaining).

        Note:
            Requires ffmpeg at runtime.
        """
        # Ensure the output directory exists (abspath handles bare filenames).
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

        fmt = fmt.lower().lstrip(".")
        # Force the output sample rate via ffmpeg regardless of container.
        parameters = ["-ar", str(opts.sample_rate)]

        if fmt == "mp3":
            seg.export(path, format="mp3", bitrate=opts.bitrate, parameters=parameters)
        else:
            # wav and other PCM containers: bitrate is irrelevant, only rate.
            seg.export(path, format=fmt, parameters=parameters)

        return path

    @staticmethod
    def merge_dialogue(
        lines: list["DialogueLine"],
        output_path: str,
        fmt: str,
        opts: PostProcessOptions,
    ) -> str:
        """Concatenate per-line audio into a single dialogue file.

        Only lines with an existing ``output_file`` are included, kept in their
        original order. Each line's segment is run through
        :meth:`process_segment` (so per-line trim/normalize/fades apply), then a
        silence gap is inserted *before* each subsequent line: the longer
        ``speaker_change_silence_ms`` when the speaker differs from the previous
        line, otherwise ``silence_between_lines_ms``.

        Args:
            lines: Dialogue lines (any subset/order produced by the app).
            output_path: Destination file path for the merged audio.
            fmt: Output format ("mp3" or "wav").
            opts: Processing + spacing options.

        Returns:
            The written ``output_path``.

        Raises:
            RuntimeError: If no line has a valid, existing ``output_file``.

        Note:
            Requires ffmpeg at runtime.
        """
        valid = [ln for ln in lines if ln.output_file and os.path.exists(ln.output_file)]
        if not valid:
            raise RuntimeError("No converted audio lines available to merge.")

        merged = AudioSegment.empty()
        prev_character: str | None = None

        for i, line in enumerate(valid):
            # Load and apply the full per-segment chain.
            seg = AudioSegment.from_file(line.output_file)
            seg = AudioPostProcessor.process_segment(seg, opts)

            # Insert the appropriate gap before every line except the first.
            if i > 0:
                gap = (
                    opts.speaker_change_silence_ms
                    if line.character != prev_character
                    else opts.silence_between_lines_ms
                )
                if gap > 0:
                    merged += AudioSegment.silent(duration=gap)

            merged += seg
            prev_character = line.character

        # Export once at the end with the requested bitrate/sample rate.
        return AudioPostProcessor.export(merged, output_path, fmt, opts)

    @staticmethod
    def export_by_character(
        lines: list["DialogueLine"],
        output_dir: str,
        fmt: str,
        opts: PostProcessOptions,
    ) -> dict:
        """Export one combined audio file per character.

        Lines are grouped by ``character`` (only those with an existing
        ``output_file``). Each character's clips are processed via
        :meth:`process_segment` and joined with ``silence_between_lines_ms``
        gaps. Files are named ``"<safe_char>_all.<fmt>"`` inside ``output_dir``,
        where ``<safe_char>`` is the character name with non-alphanumeric
        characters replaced by underscores.

        Args:
            lines: Dialogue lines.
            output_dir: Directory to write the per-character files into.
            fmt: Output format ("mp3" or "wav").
            opts: Processing + spacing options.

        Returns:
            Mapping of ``{character: output_path}`` for every character that had
            at least one valid clip. Empty dict if there were none.

        Note:
            Requires ffmpeg at runtime.
        """
        os.makedirs(output_dir, exist_ok=True)
        fmt = fmt.lower().lstrip(".")

        # Group valid lines by character, preserving insertion order.
        by_char: dict[str, list["DialogueLine"]] = {}
        for ln in lines:
            if ln.output_file and os.path.exists(ln.output_file):
                by_char.setdefault(ln.character, []).append(ln)

        results: dict[str, str] = {}
        for character, char_lines in by_char.items():
            combined = AudioSegment.empty()
            for i, line in enumerate(char_lines):
                seg = AudioSegment.from_file(line.output_file)
                seg = AudioPostProcessor.process_segment(seg, opts)
                # Same-speaker spacing between consecutive clips.
                if i > 0 and opts.silence_between_lines_ms > 0:
                    combined += AudioSegment.silent(duration=opts.silence_between_lines_ms)
                combined += seg

            # Sanitize the character name into a filesystem-safe stem.
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in character)
            out_path = os.path.join(output_dir, f"{safe}_all.{fmt}")
            AudioPostProcessor.export(combined, out_path, fmt, opts)
            results[character] = out_path

        return results
