"""Custom pronunciation rules for the TTS Dialogue App.

A :class:`PronunciationRule` is a simple find/replace directive that is applied
to a line's text *before* it is sent to the ElevenLabs TTS engine. This lets a
user fix mispronounced words/names (e.g. brand names, foreign words, acronyms)
without altering the original subtitle text.

The :class:`PronunciationManager` owns an ordered collection of rules and knows
how to:

* apply them to a string (case-insensitive, Unicode-aware, longest-first);
* serialize/deserialize for project save/load (``to_list`` / ``from_list``);
* import/export rules as CSV (Excel-friendly, ``utf-8-sig`` so Vietnamese and
  other non-ASCII text survive the round trip).

Only the standard library is used here -- no audio is involved, so ``pydub`` is
not required.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from typing import Optional


# --------------------------------------------------------------------------- #
# A single find/replace rule
# --------------------------------------------------------------------------- #
@dataclass
class PronunciationRule:
    """One pronunciation override.

    Attributes:
        original: The text to look for in the source line.
        replacement: The text to substitute in (a phonetic spelling, etc.).
        notes: Optional free-form note explaining the rule (not used by TTS).
        enabled: When ``False`` the rule is kept but skipped during
            :meth:`PronunciationManager.apply`.
    """

    original: str
    replacement: str
    notes: str = ""
    enabled: bool = True

    def to_dict(self) -> dict:
        """Serialize this rule to a plain JSON-friendly ``dict``."""
        return {
            "original": self.original,
            "replacement": self.replacement,
            "notes": self.notes,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PronunciationRule":
        """Build a :class:`PronunciationRule` from a ``dict``.

        Missing keys fall back to sensible defaults so partially-formed data
        (e.g. older project files or hand-edited CSVs) still load cleanly.
        """
        return cls(
            original=str(d.get("original", "")),
            replacement=str(d.get("replacement", "")),
            notes=str(d.get("notes", "")),
            enabled=_coerce_bool(d.get("enabled", True)),
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _coerce_bool(value: object) -> bool:
    """Best-effort conversion of arbitrary input to ``bool``.

    Needed because CSV cells are always strings ("true"/"1"/"yes"/...), and
    project files might store either real booleans or strings.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "on", "enabled"}:
        return True
    if text in {"false", "0", "no", "n", "off", "disabled", ""}:
        return False
    # Any other non-empty string is treated as truthy.
    return True


# A rule's ``original`` is treated as a "simple word" only when it consists
# purely of Unicode word characters (letters/digits/underscore). For those we
# can safely apply whole-word matching via lookarounds. Anything containing
# spaces or punctuation falls back to a plain (case-insensitive) substring
# replace, because word boundaries are ambiguous there.
_SIMPLE_WORD_RE = re.compile(r"^\w+$", re.UNICODE)

# Lookarounds that approximate Unicode-aware word boundaries.
# ``[^\W\d_]`` matches a Unicode *letter* (a word char that is not a digit and
# not underscore). The negative lookbehind/lookahead ensure the match is not
# glued to surrounding letters, giving us standalone-word semantics that work
# for accented Vietnamese text where ``\b`` is unreliable.
_WORD_LOOKBEHIND = r"(?<![^\W\d_])"
_WORD_LOOKAHEAD = r"(?![^\W\d_])"


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #
class PronunciationManager:
    """Owns and applies an ordered list of :class:`PronunciationRule`."""

    def __init__(self, rules: Optional[list[PronunciationRule]] = None) -> None:
        """Create a manager, optionally seeded with ``rules``.

        The provided list is copied so the caller's list is not mutated when
        rules are added or removed later.
        """
        self.rules: list[PronunciationRule] = list(rules) if rules else []

    # -- mutation ---------------------------------------------------------- #
    def add_rule(self, rule: PronunciationRule) -> None:
        """Append a rule to the end of the list."""
        self.rules.append(rule)

    def delete_rule(self, index: int) -> None:
        """Remove the rule at ``index``.

        Out-of-range indices are ignored so a stale UI selection cannot crash
        the app.
        """
        if 0 <= index < len(self.rules):
            del self.rules[index]

    def clear(self) -> None:
        """Remove all rules."""
        self.rules.clear()

    # -- core behavior ----------------------------------------------------- #
    def apply(self, text: str) -> str:
        """Apply every enabled rule to ``text`` and return the result.

        Matching is case-insensitive and Unicode-aware. Rules are applied in
        order of decreasing ``original`` length so that longer phrases are
        substituted before any shorter overlapping fragment, preventing partial
        replacements (e.g. "New York" wins over "York").

        For a single-word ``original`` we match standalone occurrences only
        (via Unicode-aware lookarounds). For multi-word or punctuated
        ``original`` values we fall back to a plain case-insensitive substring
        replacement, since word boundaries are ill-defined in that case.
        """
        if not text:
            return text

        # Sort enabled rules longest-first. ``sorted`` is stable, so rules of
        # equal length keep their relative insertion order.
        active = [r for r in self.rules if r.enabled and r.original]
        active.sort(key=lambda r: len(r.original), reverse=True)

        result = text
        for rule in active:
            if _SIMPLE_WORD_RE.match(rule.original):
                # Whole-word, standalone match.
                pattern = (
                    _WORD_LOOKBEHIND
                    + re.escape(rule.original)
                    + _WORD_LOOKAHEAD
                )
            else:
                # Plain substring match (still case-insensitive).
                pattern = re.escape(rule.original)
            # ``lambda`` avoids backreference interpretation of "\1" etc. in the
            # replacement string -- the replacement is inserted verbatim.
            result = re.sub(
                pattern,
                lambda _m, repl=rule.replacement: repl,
                result,
                flags=re.IGNORECASE | re.UNICODE,
            )
        return result

    # -- project serialization -------------------------------------------- #
    def to_list(self) -> list[dict]:
        """Serialize all rules to a list of dicts (for project save)."""
        return [rule.to_dict() for rule in self.rules]

    @classmethod
    def from_list(cls, data: list[dict]) -> "PronunciationManager":
        """Rebuild a manager from data produced by :meth:`to_list`."""
        rules = [PronunciationRule.from_dict(d) for d in (data or [])]
        return cls(rules)

    # -- CSV import / export ----------------------------------------------- #
    def import_csv(self, path: str) -> int:
        """Append rules read from the CSV file at ``path``.

        The CSV is expected to have a header row with the columns
        ``original,replacement,notes,enabled``. Extra columns are ignored and
        missing optional columns fall back to defaults. Rows without an
        ``original`` value are skipped.

        Uses ``utf-8-sig`` encoding so files saved from Excel (which prepends a
        BOM) are read correctly, preserving Vietnamese/accented characters.

        Returns:
            The number of rules actually added.
        """
        added = 0
        # ``newline=""`` is required by the csv module to handle quoted
        # newlines within fields correctly.
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                # ``DictReader`` keys come from the header; normalize them so a
                # stray header like " Original " still matches.
                normalized = {
                    (k or "").strip().lower(): (v if v is not None else "")
                    for k, v in row.items()
                }
                original = str(normalized.get("original", "")).strip()
                if not original:
                    continue
                rule = PronunciationRule(
                    original=original,
                    replacement=str(normalized.get("replacement", "")),
                    notes=str(normalized.get("notes", "")),
                    enabled=_coerce_bool(normalized.get("enabled", True)),
                )
                self.rules.append(rule)
                added += 1
        return added

    def export_csv(self, path: str) -> str:
        """Write all rules to a CSV file at ``path``.

        The file uses the ``original,replacement,notes,enabled`` header and
        ``utf-8-sig`` encoding for Excel compatibility.

        Returns:
            The ``path`` that was written (for caller convenience).
        """
        fieldnames = ["original", "replacement", "notes", "enabled"]
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for rule in self.rules:
                writer.writerow(
                    {
                        "original": rule.original,
                        "replacement": rule.replacement,
                        "notes": rule.notes,
                        # Write a stable lowercase token that round-trips
                        # cleanly through ``_coerce_bool`` on re-import.
                        "enabled": "true" if rule.enabled else "false",
                    }
                )
        return path
