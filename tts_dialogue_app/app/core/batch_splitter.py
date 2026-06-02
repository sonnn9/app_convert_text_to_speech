"""Smart batching for long dialogues.

This module groups dialogue lines into *batches* so that very long scripts can
be processed in chunks (for example when calling the ElevenLabs
``Text-to-Dialogue`` endpoint, which has a per-request character budget).

Core guarantees:
    * Lines are **never** split mid-line. A single :class:`DialogueLine` always
      stays whole inside one :class:`Batch`.
    * Lines keep their original order; only *consecutive* lines are grouped.
    * If a single line on its own exceeds ``max_chars`` it becomes its own
      batch (and :meth:`BatchSplitter.find_oversized` will flag it so the GUI
      can warn the user or call :meth:`BatchSplitter.split_long_text`).

Only the standard library is used here (no audio, so ``pydub`` is not needed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Import the existing model — do NOT redefine it.
from app.core.models import DialogueLine


# --------------------------------------------------------------------------- #
# Sentence-boundary detection
# --------------------------------------------------------------------------- #
# Punctuation marks treated as sentence terminators. Includes the common ASCII
# marks plus the full-width / CJK variants frequently seen in Vietnamese and
# East-Asian text. ``split_long_text`` keeps the terminator attached to the
# sentence it ends.
_SENTENCE_TERMINATORS = ".!?;:。！？；："

# Match a run of one or more terminators followed by any whitespace. The
# terminators are captured so we can re-attach them to the preceding sentence.
_SENTENCE_SPLIT_RE = re.compile(
    r"([" + re.escape(_SENTENCE_TERMINATORS) + r"]+)"
)


# --------------------------------------------------------------------------- #
# Batch dataclass
# --------------------------------------------------------------------------- #
@dataclass
class Batch:
    """A contiguous group of dialogue lines under a character budget.

    Attributes:
        index: 1-based position of this batch within the produced list.
        lines: The :class:`DialogueLine` objects in this batch, in order.
        char_count: Sum of ``len(line.api_text())`` over ``lines``.
    """

    index: int
    lines: list[DialogueLine] = field(default_factory=list)
    char_count: int = 0


# --------------------------------------------------------------------------- #
# Splitter
# --------------------------------------------------------------------------- #
class BatchSplitter:
    """Stateless helpers for grouping/splitting dialogue by character count.

    All methods are ``@staticmethod`` — the class is just a namespace.
    """

    @staticmethod
    def split(lines: list[DialogueLine], max_chars: int) -> list[Batch]:
        """Group consecutive ``lines`` into batches under ``max_chars``.

        A line is appended to the current batch while
        ``current_chars + len(line.api_text()) <= max_chars``. When adding the
        next line would exceed the budget, the current batch is closed and a new
        one is started. A single line that alone exceeds ``max_chars`` becomes
        its own batch (it is never split here — see :meth:`split_long_text`).

        Args:
            lines: Dialogue lines in the order they should be spoken.
            max_chars: Maximum total characters per batch. Values ``<= 0`` are
                treated as ``1`` to avoid an infinite/degenerate split.

        Returns:
            A list of :class:`Batch` objects with 1-based ``index`` values.
            An empty input yields an empty list.
        """
        # Guard against non-positive budgets which would otherwise make it
        # impossible to ever place a line.
        budget = max(1, int(max_chars))

        batches: list[Batch] = []
        current: list[DialogueLine] = []
        current_chars = 0

        for line in lines:
            line_len = len(line.api_text())

            # If the current batch is non-empty and adding this line would
            # overflow the budget, close the current batch first.
            if current and (current_chars + line_len) > budget:
                batches.append(
                    Batch(index=len(batches) + 1, lines=current, char_count=current_chars)
                )
                current = []
                current_chars = 0

            # Add the line to the (possibly fresh) current batch. A single
            # oversized line lands here on its own and will form its own batch.
            current.append(line)
            current_chars += line_len

        # Flush any remaining accumulated lines.
        if current:
            batches.append(
                Batch(index=len(batches) + 1, lines=current, char_count=current_chars)
            )

        return batches

    @staticmethod
    def find_oversized(lines: list[DialogueLine], max_chars: int) -> list[DialogueLine]:
        """Return the lines whose API text exceeds ``max_chars``.

        These lines cannot fit in any single batch and typically need to be
        broken up first via :meth:`split_long_text` (or surfaced to the user).

        Args:
            lines: Dialogue lines to inspect.
            max_chars: The per-batch character budget.

        Returns:
            The subset of ``lines`` (original order) with
            ``len(api_text()) > max_chars``.
        """
        budget = max(1, int(max_chars))
        return [line for line in lines if len(line.api_text()) > budget]

    @staticmethod
    def split_long_text(text: str, max_chars: int) -> list[str]:
        """Break a too-long string into pieces no longer than ``max_chars``.

        Strategy, applied in order of preference:
            1. Split on sentence punctuation (``. ! ? ; :`` and the full-width
               variants ``。 ！ ？ ； ：``), keeping terminators attached, then
               greedily accumulate whole sentences up to ``max_chars``.
            2. If a single sentence is still too long, hard-split it on word
               boundaries (whitespace).
            3. As a last resort (e.g. a single enormous word, or text with no
               whitespace), slice raw character chunks of ``max_chars``.

        Empty pieces are never produced.

        Args:
            text: The string to split.
            max_chars: Maximum length of each returned piece. Values ``<= 0``
                are treated as ``1``.

        Returns:
            A list of non-empty string pieces. An empty/whitespace-only input
            returns an empty list.
        """
        budget = max(1, int(max_chars))

        stripped = text.strip()
        if not stripped:
            return []

        # Short-circuit: already fits.
        if len(stripped) <= budget:
            return [stripped]

        sentences = BatchSplitter._split_into_sentences(stripped)

        pieces: list[str] = []
        buffer = ""

        for sentence in sentences:
            # A single sentence longer than the budget must be hard-split on
            # its own; flush whatever is buffered first.
            if len(sentence) > budget:
                if buffer:
                    pieces.append(buffer)
                    buffer = ""
                pieces.extend(BatchSplitter._hard_split(sentence, budget))
                continue

            if not buffer:
                buffer = sentence
            elif len(buffer) + 1 + len(sentence) <= budget:
                # Re-join sentences with a single space (terminators are already
                # attached to each sentence by the splitter).
                buffer = f"{buffer} {sentence}"
            else:
                pieces.append(buffer)
                buffer = sentence

        if buffer:
            pieces.append(buffer)

        # Defensive: drop any empty pieces (should not happen, but keeps the
        # public contract — "never return empty pieces").
        return [p for p in pieces if p]

    @staticmethod
    def preview(batches: list[Batch]) -> str:
        """Build a human-readable multi-line summary of ``batches``.

        Each line looks like::

            Batch 1: 3 lines, 142 chars (lines 1-3)

        The ``(lines a-b)`` range uses the 1-based position of the lines within
        their batch when ``DialogueLine.index`` is unavailable; otherwise it
        reflects the actual line indices for easier cross-referencing.

        Args:
            batches: The batches to describe (typically from :meth:`split`).

        Returns:
            A newline-joined string (empty string for an empty list).
        """
        rows: list[str] = []
        for batch in batches:
            count = len(batch.lines)
            line_word = "line" if count == 1 else "lines"

            if batch.lines:
                first = batch.lines[0].index
                last = batch.lines[-1].index
                line_range = f"{first}" if first == last else f"{first}-{last}"
                range_part = f" (lines {line_range})"
            else:
                range_part = ""

            rows.append(
                f"Batch {batch.index}: {count} {line_word}, "
                f"{batch.char_count} chars{range_part}"
            )
        return "\n".join(rows)

    # ----------------------------------------------------------------- #
    # Private helpers
    # ----------------------------------------------------------------- #
    @staticmethod
    def _split_into_sentences(text: str) -> list[str]:
        """Split ``text`` into sentences, keeping terminators attached.

        The regex captures runs of terminator characters; we stitch each run
        back onto the text that precedes it so punctuation is preserved.
        """
        parts = _SENTENCE_SPLIT_RE.split(text)

        sentences: list[str] = []
        # ``parts`` alternates: [chunk, terminators, chunk, terminators, ...].
        i = 0
        while i < len(parts):
            chunk = parts[i]
            terminator = parts[i + 1] if i + 1 < len(parts) else ""
            sentence = (chunk + terminator).strip()
            if sentence:
                sentences.append(sentence)
            i += 2

        # If no terminator ever matched, the whole text is one "sentence".
        if not sentences:
            stripped = text.strip()
            if stripped:
                sentences.append(stripped)
        return sentences

    @staticmethod
    def _hard_split(text: str, budget: int) -> list[str]:
        """Split a too-long chunk by words, falling back to raw char slices.

        First tries to keep words intact (splitting on whitespace). Any single
        word longer than ``budget`` is sliced into raw ``budget``-sized chunks.
        """
        words = text.split()

        # No whitespace at all -> go straight to raw character chunking.
        if len(words) <= 1:
            return BatchSplitter._raw_chunks(text, budget)

        pieces: list[str] = []
        buffer = ""

        for word in words:
            if len(word) > budget:
                # Oversized single word: flush buffer, then raw-chunk the word.
                if buffer:
                    pieces.append(buffer)
                    buffer = ""
                pieces.extend(BatchSplitter._raw_chunks(word, budget))
                continue

            if not buffer:
                buffer = word
            elif len(buffer) + 1 + len(word) <= budget:
                buffer = f"{buffer} {word}"
            else:
                pieces.append(buffer)
                buffer = word

        if buffer:
            pieces.append(buffer)

        return [p for p in pieces if p]

    @staticmethod
    def _raw_chunks(text: str, budget: int) -> list[str]:
        """Slice ``text`` into consecutive raw chunks of at most ``budget`` chars.

        Used as the final fallback when no sentence or word boundary helps.
        Whitespace-only slices are dropped so empty pieces are never returned.
        """
        chunks: list[str] = []
        for start in range(0, len(text), budget):
            chunk = text[start : start + budget].strip()
            if chunk:
                chunks.append(chunk)
        return chunks
