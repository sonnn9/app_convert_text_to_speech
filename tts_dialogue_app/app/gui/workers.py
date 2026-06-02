"""Background worker threads (QThread) so the GUI never freezes during network
calls or audio processing.

Workers communicate with the UI exclusively through Qt signals (thread-safe).

v2 adds: model loading, local cache, pronunciation substitution, exponential
backoff on rate limits, Dialogue-API / Auto convert modes (with automatic
fallback to line-by-line), and a richer post-processing pipeline (normalize,
trim, fades, sample rate, bitrate) plus timeline / subtitle export.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from PySide6.QtCore import QThread, Signal

from app.core.audio_postprocessor import AudioPostProcessor, PostProcessOptions
from app.core.audio_processor import AudioProcessor
from app.core.batch_splitter import BatchSplitter
from app.core.cache_manager import CacheManager
from app.core.dialogue_api_client import DialogueAPIClient, build_inputs
from app.core.elevenlabs_client import (
    ElevenLabsClient,
    ElevenLabsError,
    call_with_backoff,
)
from app.core.models import (
    CharacterVoiceConfig,
    DialogueLine,
    LineStatus,
)
from app.core.pronunciation_manager import PronunciationManager
from app.core.subtitle_exporter import SubtitleExporter
from app.core.timeline_exporter import TimelineExporter

# Dialogue-API limits used by Auto mode to decide line-by-line vs dialogue.
AUTO_MAX_CHARS = 1200      # total characters considered "short"
AUTO_MAX_VOICES = 6        # max distinct voices the dialogue endpoint handles well


# --------------------------------------------------------------------------- #
# Generic one-shot workers
# --------------------------------------------------------------------------- #
class TestApiWorker(QThread):
    success = Signal()
    failed = Signal(str)

    def __init__(self, api_key: str) -> None:
        super().__init__()
        self.api_key = api_key

    def run(self) -> None:
        try:
            ElevenLabsClient(self.api_key).test_api()
            self.success.emit()
        except ElevenLabsError as exc:
            self.failed.emit(exc.message)
        except Exception as exc:  # pragma: no cover
            self.failed.emit(str(exc))


class LoadVoicesWorker(QThread):
    success = Signal(list)  # list[Voice]
    failed = Signal(str)

    def __init__(self, api_key: str) -> None:
        super().__init__()
        self.api_key = api_key

    def run(self) -> None:
        try:
            voices = ElevenLabsClient(self.api_key).get_voices()
            self.success.emit(voices)
        except ElevenLabsError as exc:
            self.failed.emit(exc.message)
        except Exception as exc:  # pragma: no cover
            self.failed.emit(str(exc))


class LoadSharedVoicesWorker(QThread):
    """Browse the public Voice Library with filters (background)."""

    success = Signal(list, bool)  # list[Voice], has_more
    failed = Signal(str)

    def __init__(self, api_key: str, filters: dict, page: int = 0) -> None:
        super().__init__()
        self.api_key = api_key
        self.filters = filters
        self.page = page

    def run(self) -> None:
        try:
            client = ElevenLabsClient(self.api_key)
            voices, has_more = client.get_shared_voices(page=self.page, **self.filters)
            self.success.emit(voices, has_more)
        except ElevenLabsError as exc:
            self.failed.emit(exc.message)
        except Exception as exc:  # pragma: no cover
            self.failed.emit(str(exc))


class AddSharedVoiceWorker(QThread):
    """Add a Voice Library voice to the account so it becomes usable for TTS."""

    success = Signal(str, object)  # new_voice_id, original Voice
    failed = Signal(str)

    def __init__(self, api_key: str, voice, new_name: str) -> None:
        super().__init__()
        self.api_key = api_key
        self.voice = voice
        self.new_name = new_name

    def run(self) -> None:
        try:
            client = ElevenLabsClient(self.api_key)
            new_id = client.add_shared_voice(
                self.voice.public_owner_id, self.voice.voice_id, self.new_name
            )
            self.success.emit(new_id, self.voice)
        except ElevenLabsError as exc:
            self.failed.emit(exc.message)
        except Exception as exc:  # pragma: no cover
            self.failed.emit(str(exc))


class LoadModelsWorker(QThread):
    """Fetch the account's TTS models (model manager)."""

    success = Signal(list)  # list[TTSModel]
    failed = Signal(str)

    def __init__(self, api_key: str) -> None:
        super().__init__()
        self.api_key = api_key

    def run(self) -> None:
        try:
            models = ElevenLabsClient(self.api_key).get_models(only_tts=True)
            self.success.emit(models)
        except ElevenLabsError as exc:
            self.failed.emit(exc.message)
        except Exception as exc:  # pragma: no cover
            self.failed.emit(str(exc))


# --------------------------------------------------------------------------- #
# Preview worker
# --------------------------------------------------------------------------- #
class PreviewWorker(QThread):
    success = Signal(str)  # output path
    failed = Signal(str)

    def __init__(
        self,
        api_key: str,
        text: str,
        config: CharacterVoiceConfig,
        output_path: str,
        output_format: str,
        pronunciation: Optional[PronunciationManager] = None,
        apply_pronunciation: bool = True,
    ) -> None:
        super().__init__()
        self.api_key = api_key
        self.text = text
        self.config = config
        self.output_path = output_path
        self.output_format = output_format
        self.pronunciation = pronunciation
        self.apply_pronunciation = apply_pronunciation

    def run(self) -> None:
        try:
            text = self.text
            if self.apply_pronunciation and self.pronunciation:
                text = self.pronunciation.apply(text)

            client = ElevenLabsClient(self.api_key)
            _, speed_applied = client.text_to_speech(
                text=text,
                voice_id=self.config.voice_id,
                model_id=self.config.model_id,
                voice_settings=self.config.settings,
                output_path=self.output_path,
            )
            if not speed_applied and abs(self.config.settings.speed - 1.0) > 1e-3:
                AudioProcessor.change_speed(self.output_path, self.config.settings.speed)

            final = self._maybe_convert_format(self.output_path)
            self.success.emit(final)
        except ElevenLabsError as exc:
            self.failed.emit(exc.message)
        except Exception as exc:  # pragma: no cover
            self.failed.emit(str(exc))

    def _maybe_convert_format(self, path: str) -> str:
        if self.output_format == "wav" and path.lower().endswith(".mp3"):
            seg = AudioProcessor.load(path)
            wav_path = os.path.splitext(path)[0] + ".wav"
            AudioProcessor.export(seg, wav_path, "wav")
            return wav_path
        return path


# --------------------------------------------------------------------------- #
# Main conversion worker
# --------------------------------------------------------------------------- #
class ConvertWorker(QThread):
    line_status = Signal(int, str)            # index, status
    line_done = Signal(int, str, float)       # index, output_file, duration
    line_error = Signal(int, str)             # index, error
    progress = Signal(int, int)               # processed, total
    log = Signal(str)
    cache_stat = Signal(int, int)             # cache hits, cache misses
    finished_all = Signal()

    def __init__(
        self,
        api_key: str,
        lines: list[DialogueLine],
        configs: dict[str, CharacterVoiceConfig],
        lines_dir: str,
        output_format: str,
        convert_mode: str = "line",                 # "line" | "dialogue" | "auto"
        pronunciation: Optional[PronunciationManager] = None,
        apply_pronunciation: bool = True,
        cache: Optional[CacheManager] = None,
        max_chars_per_batch: int = 1500,
        max_retries: int = 4,
        base_delay: float = 2.0,
        rate_limit_delay: float = 0.4,
        force_regenerate: Optional[set[int]] = None,
    ) -> None:
        super().__init__()
        self.api_key = api_key
        self.lines = lines
        self.configs = configs
        self.lines_dir = lines_dir
        self.output_format = output_format
        self.convert_mode = convert_mode
        self.pronunciation = pronunciation
        self.apply_pronunciation = apply_pronunciation
        self.cache = cache
        self.max_chars_per_batch = max_chars_per_batch
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.rate_limit_delay = rate_limit_delay
        self.force_regenerate = force_regenerate or set()

        self._paused = False
        self._cancelled = False
        self._hits = 0
        self._misses = 0

    # --------------------------- control ---------------------------------- #
    def pause(self) -> None:
        self._paused = True
        self.log.emit("Paused.")

    def resume(self) -> None:
        self._paused = False
        self.log.emit("Resumed.")

    def cancel(self) -> None:
        self._cancelled = True
        self._paused = False
        self.log.emit("Cancel requested...")

    def _wait_if_paused(self) -> None:
        while self._paused and not self._cancelled:
            time.sleep(0.15)

    # ----------------------- mode resolution ------------------------------ #
    def _resolve_mode(self) -> str:
        """Auto mode picks dialogue for short scripts with few voices, else line."""
        if self.convert_mode != "auto":
            return self.convert_mode
        total_chars = sum(len(ln.api_text()) for ln in self.lines)
        voices = {self.configs[ln.character].voice_id for ln in self.lines
                  if ln.character in self.configs and self.configs[ln.character].voice_id}
        if total_chars <= AUTO_MAX_CHARS and len(voices) <= AUTO_MAX_VOICES:
            self.log.emit(
                f"Auto mode: short script ({total_chars} chars, {len(voices)} voices) "
                f"-> using Dialogue API."
            )
            return "dialogue"
        self.log.emit(
            f"Auto mode: long script ({total_chars} chars, {len(voices)} voices) "
            f"-> using line-by-line."
        )
        return "line"

    # -------------------------- pronunciation ----------------------------- #
    def _prepare_processed_text(self, line: DialogueLine) -> None:
        if self.apply_pronunciation and self.pronunciation:
            processed = self.pronunciation.apply(line.text)
            line.processed_text = processed if processed != line.text else ""
        else:
            line.processed_text = ""

    # ------------------------------ run ----------------------------------- #
    def run(self) -> None:
        os.makedirs(self.lines_dir, exist_ok=True)
        # pre-compute processed text for all lines
        for line in self.lines:
            self._prepare_processed_text(line)

        mode = self._resolve_mode()
        if mode == "dialogue":
            ok = self._run_dialogue()
            if not ok and not self._cancelled:
                self.log.emit("Dialogue API unavailable/failed -> falling back to line-by-line.")
                self._run_line()
        else:
            self._run_line()

        self.cache_stat.emit(self._hits, self._misses)
        self.finished_all.emit()

    # --------------------- line-by-line implementation -------------------- #
    def _run_line(self) -> None:
        try:
            client = ElevenLabsClient(self.api_key)
        except ElevenLabsError as exc:
            self.log.emit(f"ERROR: {exc.message}")
            return

        total = len(self.lines)
        processed = 0

        for line in self.lines:
            if self._cancelled:
                self.log.emit("Conversion cancelled.")
                break
            self._wait_if_paused()
            if self._cancelled:
                break

            config = self.configs.get(line.character)
            if config is None or not config.voice_id:
                msg = f"Line {line.index}: no voice configured for '{line.character}'."
                line.status = LineStatus.ERROR
                line.error = msg
                self.line_status.emit(line.index, LineStatus.ERROR.value)
                self.line_error.emit(line.index, msg)
                self.log.emit("ERROR: " + msg)
                processed += 1
                self.progress.emit(processed, total)
                continue

            self.line_status.emit(line.index, LineStatus.PROCESSING.value)

            safe_char = "".join(c if c.isalnum() or c in "-_" else "_" for c in line.character)
            filename = f"{line.index:04d}_{safe_char}.{self.output_format}"
            out_path = os.path.join(self.lines_dir, filename)

            # ---- cache lookup ----
            cache_key = None
            if self.cache and self.cache.enabled and line.index not in self.force_regenerate:
                cache_key = CacheManager.make_key(
                    line.api_text(), config.voice_id, config.model_id, config.settings
                )
                cached = self.cache.copy_to(cache_key, self.output_format, out_path)
                if cached:
                    self._hits += 1
                    line.from_cache = True
                    line.status = LineStatus.DONE
                    line.output_file = out_path
                    line.duration = AudioProcessor.get_duration(out_path)
                    line.error = None
                    self.line_done.emit(line.index, out_path, line.duration)
                    self.log.emit(f"Line {line.index}: CACHE HIT (no API call).")
                    processed += 1
                    self.progress.emit(processed, total)
                    continue

            self._misses += 1
            self.log.emit(f"Line {line.index} [{line.character}]: converting (cache miss)...")

            synth_path = (
                os.path.splitext(out_path)[0] + ".mp3"
                if self.output_format == "wav"
                else out_path
            )

            try:
                line.from_cache = False

                def _do() -> tuple:
                    return client.text_to_speech(
                        text=line.api_text(),
                        voice_id=config.voice_id,
                        model_id=config.model_id,
                        voice_settings=config.settings,
                        output_path=synth_path,
                    )

                _, speed_applied = call_with_backoff(
                    _do,
                    max_retries=self.max_retries,
                    base_delay=self.base_delay,
                    on_retry=lambda a, d, m: self.log.emit(
                        f"Line {line.index}: rate-limited, retry {a} in {d:.0f}s ({m})"
                    ),
                    should_cancel=lambda: self._cancelled,
                )

                if not speed_applied and abs(config.settings.speed - 1.0) > 1e-3:
                    self.log.emit(
                        f"Line {line.index}: model lacks 'speed', applying local "
                        f"speed x{config.settings.speed} via pydub."
                    )
                    AudioProcessor.change_speed(synth_path, config.settings.speed)

                if self.output_format == "wav" and synth_path != out_path:
                    seg = AudioProcessor.load(synth_path)
                    AudioProcessor.export(seg, out_path, "wav")
                    try:
                        os.remove(synth_path)
                    except OSError:
                        pass

                duration = AudioProcessor.get_duration(out_path)
                line.status = LineStatus.DONE
                line.output_file = out_path
                line.duration = duration
                line.error = None

                # ---- store in cache ----
                if self.cache and cache_key is None:
                    cache_key = CacheManager.make_key(
                        line.api_text(), config.voice_id, config.model_id, config.settings
                    )
                if self.cache and cache_key:
                    try:
                        self.cache.put(cache_key, out_path, self.output_format)
                    except Exception:
                        pass

                self.line_done.emit(line.index, out_path, duration)
                self.log.emit(f"Line {line.index}: done ({duration:.2f}s).")

            except ElevenLabsError as exc:
                line.status = LineStatus.ERROR
                line.error = exc.message
                self.line_status.emit(line.index, LineStatus.ERROR.value)
                self.line_error.emit(line.index, exc.message)
                self.log.emit(f"ERROR line {line.index}: {exc.message}")
            except Exception as exc:  # pragma: no cover
                line.status = LineStatus.ERROR
                line.error = str(exc)
                self.line_status.emit(line.index, LineStatus.ERROR.value)
                self.line_error.emit(line.index, str(exc))
                self.log.emit(f"ERROR line {line.index}: {exc}")

            processed += 1
            self.progress.emit(processed, total)
            if self.rate_limit_delay > 0 and not self._cancelled:
                time.sleep(self.rate_limit_delay)

    # ----------------------- dialogue implementation ---------------------- #
    def _run_dialogue(self) -> bool:
        """Convert using the Text-to-Dialogue API, one audio file per batch.

        Returns False (so the caller can fall back to line-by-line) when the
        endpoint isn't supported or every batch fails. Each batch's audio is
        attached to the FIRST line of the batch (the "carrier"); the remaining
        lines are marked Done with no separate file. Per-line subtitles are most
        accurate in line-by-line mode (documented in the README)."""
        try:
            client = DialogueAPIClient(self.api_key)
        except ElevenLabsError as exc:
            self.log.emit(f"Dialogue API init failed: {exc.message}")
            return False

        if not client.is_supported():
            self.log.emit("Dialogue API not supported on this account/endpoint.")
            return False

        # one model_id for the whole call (use the first configured model)
        model_id = next(
            (self.configs[ln.character].model_id for ln in self.lines
             if ln.character in self.configs), "eleven_v3"
        )

        batches = BatchSplitter.split(self.lines, self.max_chars_per_batch)
        self.log.emit(f"Dialogue mode: {len(batches)} batch(es).")
        total = len(batches)
        processed = 0
        any_ok = False

        for batch in batches:
            if self._cancelled:
                break
            self._wait_if_paused()
            if self._cancelled:
                break

            for ln in batch.lines:
                self.line_status.emit(ln.index, LineStatus.PROCESSING.value)

            inputs = build_inputs(batch.lines, self.configs)
            if not inputs:
                processed += 1
                self.progress.emit(processed, total)
                continue

            carrier = batch.lines[0]
            out_path = os.path.join(self.lines_dir, f"batch_{batch.index:04d}.{self.output_format}")
            synth_path = (
                os.path.splitext(out_path)[0] + ".mp3"
                if self.output_format == "wav"
                else out_path
            )

            try:
                def _do() -> str:
                    return client.text_to_dialogue(
                        inputs=inputs, model_id=model_id, output_path=synth_path
                    )

                call_with_backoff(
                    _do,
                    max_retries=self.max_retries,
                    base_delay=self.base_delay,
                    on_retry=lambda a, d, m: self.log.emit(
                        f"Batch {batch.index}: rate-limited, retry {a} in {d:.0f}s"
                    ),
                    should_cancel=lambda: self._cancelled,
                )

                if self.output_format == "wav" and synth_path != out_path:
                    seg = AudioProcessor.load(synth_path)
                    AudioProcessor.export(seg, out_path, "wav")
                    try:
                        os.remove(synth_path)
                    except OSError:
                        pass

                duration = AudioProcessor.get_duration(out_path)
                # carrier line carries the batch audio
                carrier.status = LineStatus.DONE
                carrier.output_file = out_path
                carrier.duration = duration
                carrier.error = None
                self.line_done.emit(carrier.index, out_path, duration)
                # other lines: Done, no separate file
                for ln in batch.lines[1:]:
                    ln.status = LineStatus.DONE
                    ln.output_file = None
                    ln.duration = 0.0
                    ln.error = None
                    self.line_status.emit(ln.index, LineStatus.DONE.value)
                any_ok = True
                self.log.emit(f"Batch {batch.index}: done ({duration:.2f}s).")

            except ElevenLabsError as exc:
                for ln in batch.lines:
                    ln.status = LineStatus.ERROR
                    ln.error = exc.message
                    self.line_status.emit(ln.index, LineStatus.ERROR.value)
                    self.line_error.emit(ln.index, exc.message)
                self.log.emit(f"ERROR batch {batch.index}: {exc.message}")
            except Exception as exc:  # pragma: no cover
                for ln in batch.lines:
                    ln.status = LineStatus.ERROR
                    ln.error = str(exc)
                    self.line_status.emit(ln.index, LineStatus.ERROR.value)
                self.log.emit(f"ERROR batch {batch.index}: {exc}")

            processed += 1
            self.progress.emit(processed, total)
            if self.rate_limit_delay > 0 and not self._cancelled:
                time.sleep(self.rate_limit_delay)

        return any_ok


# --------------------------------------------------------------------------- #
# Post-processing worker: merge / export-by-character / srt / timeline
# --------------------------------------------------------------------------- #
class PostProcessWorker(QThread):
    success = Signal(dict)
    failed = Signal(str)
    log = Signal(str)

    def __init__(
        self,
        lines: list[DialogueLine],
        configs: dict[str, CharacterVoiceConfig],
        project_dir: str,
        output_format: str,
        options: PostProcessOptions,
        do_merge: bool = True,
        do_by_character: bool = False,
        do_srt: bool = False,
        do_timeline: bool = False,
    ) -> None:
        super().__init__()
        self.lines = lines
        self.configs = configs
        self.project_dir = project_dir
        self.output_format = output_format
        self.options = options
        self.do_merge = do_merge
        self.do_by_character = do_by_character
        self.do_srt = do_srt
        self.do_timeline = do_timeline

    def run(self) -> None:
        results: dict = {}
        try:
            if self.do_merge:
                self.log.emit("Merging full dialogue...")
                merged_path = os.path.join(
                    self.project_dir, "merged", f"full_dialogue.{self.output_format}"
                )
                AudioPostProcessor.merge_dialogue(
                    self.lines, merged_path, self.output_format, self.options
                )
                results["merged"] = merged_path
                self.log.emit(f"Merged -> {merged_path}")

            if self.do_by_character:
                self.log.emit("Exporting by character...")
                by_char_dir = os.path.join(self.project_dir, "by_character")
                mapping = AudioPostProcessor.export_by_character(
                    self.lines, by_char_dir, self.output_format, self.options
                )
                results["by_character"] = mapping
                self.log.emit(f"Exported {len(mapping)} character file(s).")

            if self.do_srt:
                self.log.emit("Generating subtitle (.srt)...")
                srt_path = os.path.join(self.project_dir, "subtitles", "full_dialogue.srt")
                SubtitleExporter.export_srt(
                    self.lines,
                    srt_path,
                    silence_between_lines_ms=self.options.silence_between_lines_ms,
                    speaker_change_silence_ms=self.options.speaker_change_silence_ms,
                )
                results["srt"] = srt_path
                self.log.emit(f"Subtitle -> {srt_path}")

            if self.do_timeline:
                self.log.emit("Exporting timeline (CSV + JSON)...")
                rows = TimelineExporter.compute(
                    self.lines,
                    self.configs,
                    silence_between_lines_ms=self.options.silence_between_lines_ms,
                    speaker_change_silence_ms=self.options.speaker_change_silence_ms,
                )
                csv_path = os.path.join(self.project_dir, "timeline", "timeline.csv")
                json_path = os.path.join(self.project_dir, "timeline", "timeline.json")
                TimelineExporter.export_csv(rows, csv_path)
                TimelineExporter.export_json(rows, json_path)
                results["timeline_csv"] = csv_path
                results["timeline_json"] = json_path
                self.log.emit(f"Timeline -> {csv_path}")

            self.success.emit(results)
        except Exception as exc:
            self.failed.emit(str(exc))
