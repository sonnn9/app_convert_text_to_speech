"""Pre-conversion usage / cost estimation.

This module produces a *static* estimate of how much work (and therefore how
many billable characters) a conversion run will cost, **before** any audio is
synthesized. It mirrors the exact text-processing pipeline used at convert time
so the numbers shown to the user match reality:

    1. Pronunciation substitution (optional ``pronunciation.apply(text)``).
    2. Otherwise the line's own :meth:`DialogueLine.api_text` (processed text
       if present, else the original text).

It also reports how many lines would be served from the local cache (no API
call, no billing) so users can see the savings of a partial re-run.

Stdlib only (no audio is touched here). Existing data models are imported from
:mod:`app.core.models` and never redefined.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from app.core.models import (
    CharacterVoiceConfig,
    DialogueLine,
    VoiceSettings,
)

# Warn when a single line exceeds this many characters. Very long lines tend to
# hit the API's per-request limits and produce unnatural prosody, so we surface
# them to the user up front.
WARN_LINE_CHARS = 2500


# --------------------------------------------------------------------------- #
# Structural typing for the optional collaborators
# --------------------------------------------------------------------------- #
# We deliberately use ``Protocol`` (structural typing) instead of importing the
# concrete ``CacheManager`` / pronunciation classes. This keeps the module
# self-contained and avoids hard import cycles: anything that *quacks* like
# these interfaces will work.
@runtime_checkable
class _PronunciationLike(Protocol):
    """Anything exposing ``apply(text) -> str``."""

    def apply(self, text: str) -> str:  # pragma: no cover - structural only
        ...


@runtime_checkable
class _CacheLike(Protocol):
    """Anything exposing ``make_key(...)`` and ``get(key, ext)``."""

    def make_key(
        self,
        text: str,
        voice_id: str,
        model_id: str,
        settings: VoiceSettings,
    ) -> str:  # pragma: no cover - structural only
        ...

    def get(self, key: str, ext: str) -> Any:  # pragma: no cover - structural only
        ...


class UsageEstimator:
    """Compute and format a pre-conversion usage / cost estimate."""

    @staticmethod
    def estimate(
        lines: list[DialogueLine],
        configs: dict[str, CharacterVoiceConfig],
        pronunciation: Optional[_PronunciationLike] = None,
        cache: Optional[_CacheLike] = None,
        output_format: str = "mp3",
    ) -> dict[str, Any]:
        """Estimate character usage / cache savings for ``lines``.

        Args:
            lines: The dialogue lines that would be converted.
            configs: Map of ``character name -> CharacterVoiceConfig``. A line
                whose character is missing here is still counted in the totals
                (and as billable), but generates a warning.
            pronunciation: Optional object with ``apply(text) -> str``. When
                provided, the processed text for each line is
                ``pronunciation.apply(line.api_text())``; otherwise the line's
                own :meth:`DialogueLine.api_text` is used.
            cache: Optional cache manager with ``make_key(text, voice_id,
                model_id, settings)`` and ``get(key, ext)``. When present and
                a line's key resolves to an existing cache entry, that line is
                considered a cache hit and is **not** billable. When ``None``
                (or unusable), every line is billable.
            output_format: ``"wav"`` or ``"mp3"`` (default). Determines the
                cache file extension used for the lookup.

        Returns:
            A dict with the keys documented in the module / public API:
            ``total_lines``, ``total_characters``, ``total_chars``,
            ``per_character``, ``billable_chars``, ``cached_lines`` and
            ``warnings``.
        """
        # Cache lookups use a different file extension depending on the chosen
        # output container. Anything that isn't explicitly "wav" defaults to mp3.
        ext = "wav" if str(output_format).lower() == "wav" else "mp3"

        total_lines = len(lines)
        total_chars = 0
        billable_chars = 0
        cached_lines = 0

        # Insertion-ordered so the report lists characters as first encountered.
        per_character: dict[str, dict[str, int]] = {}
        warnings: list[str] = []

        # Track characters we've already warned about for "no voice" so the
        # warning list stays concise even with many lines per character.
        warned_missing: set[str] = set()

        for line in lines:
            # ---- 1. Resolve the exact text that would be sent to the API ---- #
            processed = UsageEstimator._processed_text(line, pronunciation)
            n_chars = len(processed)
            total_chars += n_chars

            # ---- 2. Per-character accumulation ---- #
            character = line.character or "(unknown)"
            bucket = per_character.setdefault(character, {"chars": 0, "lines": 0})
            bucket["chars"] += n_chars
            bucket["lines"] += 1

            # ---- 3. Long-line warning ---- #
            if n_chars > WARN_LINE_CHARS:
                warnings.append(
                    f"Line {line.index} ({character}) is very long: "
                    f"{n_chars} chars (> {WARN_LINE_CHARS})."
                )

            # ---- 4. Resolve config (defensive: a missing config is allowed) ---- #
            config = configs.get(line.character)
            if config is None or not getattr(config, "voice_id", ""):
                # No usable voice: still counts in totals and remains billable
                # (we can't know it'll be cached without a voice/model key).
                if character not in warned_missing:
                    warned_missing.add(character)
                    warnings.append(
                        f"Character {character!r} has no voice assigned; "
                        f"its lines are still counted as billable."
                    )
                billable_chars += n_chars
                continue

            # ---- 5. Cache lookup to decide billability ---- #
            if UsageEstimator._is_cache_hit(cache, processed, config, ext):
                cached_lines += 1
                # Cache hit => served locally => not billed.
            else:
                billable_chars += n_chars

        # ``total_characters`` is the count of UNIQUE speakers (per spec), which
        # is distinct from ``total_chars`` (the summed text length).
        total_characters = len(per_character)

        return {
            "total_lines": total_lines,
            "total_characters": total_characters,
            "total_chars": total_chars,
            "per_character": per_character,
            "billable_chars": billable_chars,
            "cached_lines": cached_lines,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _processed_text(
        line: DialogueLine,
        pronunciation: Optional[_PronunciationLike],
    ) -> str:
        """Return the text that would actually be synthesized for ``line``.

        Mirrors the convert-time pipeline: when a pronunciation engine is given
        we apply it to the line's API text; otherwise we use the API text
        as-is. Any failure inside ``apply`` is swallowed so an estimate never
        crashes the UI — we fall back to the un-substituted API text.
        """
        base = line.api_text()
        if pronunciation is not None:
            try:
                result = pronunciation.apply(base)
                # Be tolerant of implementations returning ``None``.
                return result if isinstance(result, str) else base
            except Exception:
                return base
        return base

    @staticmethod
    def _is_cache_hit(
        cache: Optional[_CacheLike],
        text: str,
        config: CharacterVoiceConfig,
        ext: str,
    ) -> bool:
        """Return True when ``text`` for ``config`` is already cached.

        Defensive on every step: a missing/disabled/broken cache, or any
        exception from ``make_key`` / ``get``, is treated as a cache *miss*
        (i.e. the line remains billable).
        """
        if cache is None:
            return False
        # Allow a cache implementation to advertise itself as disabled.
        if getattr(cache, "enabled", True) is False:
            return False
        if not hasattr(cache, "make_key") or not hasattr(cache, "get"):
            return False
        try:
            key = cache.make_key(
                text,
                config.voice_id,
                config.model_id,
                config.settings,
            )
            return cache.get(key, ext) is not None
        except Exception:
            # Any cache error => assume not cached => count as billable.
            return False

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #
    @staticmethod
    def format_report(stats: dict[str, Any]) -> str:
        """Render :meth:`estimate` output as a human-readable multi-line string."""
        total_lines = int(stats.get("total_lines", 0))
        total_characters = int(stats.get("total_characters", 0))
        total_chars = int(stats.get("total_chars", 0))
        billable_chars = int(stats.get("billable_chars", 0))
        cached_lines = int(stats.get("cached_lines", 0))
        per_character: dict[str, dict[str, int]] = stats.get("per_character", {}) or {}
        warnings: list[str] = stats.get("warnings", []) or []

        # Characters saved by the cache, for an at-a-glance "savings" figure.
        saved_chars = max(total_chars - billable_chars, 0)
        if total_chars > 0:
            saved_pct = (saved_chars / total_chars) * 100.0
        else:
            saved_pct = 0.0

        lines_out: list[str] = []
        lines_out.append("Usage Estimate")
        lines_out.append("=" * 40)
        lines_out.append(f"Lines:               {total_lines:,}")
        lines_out.append(f"Characters (voices): {total_characters:,}")
        lines_out.append(f"Total text chars:    {total_chars:,}")
        lines_out.append(f"Cached lines:        {cached_lines:,}")
        lines_out.append(
            f"Cache savings:       {saved_chars:,} chars ({saved_pct:.1f}%)"
        )
        lines_out.append(f"Billable chars:      {billable_chars:,}")

        # Per-character breakdown, sorted by descending character count so the
        # heaviest speakers surface first.
        if per_character:
            lines_out.append("")
            lines_out.append("Per character:")
            ordered = sorted(
                per_character.items(),
                key=lambda kv: (-int(kv[1].get("chars", 0)), kv[0]),
            )
            for name, info in ordered:
                chars = int(info.get("chars", 0))
                n_lines = int(info.get("lines", 0))
                lines_out.append(
                    f"  - {name}: {chars:,} chars across {n_lines:,} line(s)"
                )

        # Warnings last so they're easy to spot at the bottom.
        if warnings:
            lines_out.append("")
            lines_out.append(f"Warnings ({len(warnings)}):")
            for w in warnings:
                lines_out.append(f"  ! {w}")

        return "\n".join(lines_out)
