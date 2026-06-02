"""Dialogue parser.

Parses a multi-line script of the form ``Character: dialogue`` into an ordered
list of :class:`DialogueLine`. Supports arbitrary character names (A, B, Mom,
Lucy, Narrator, Mẹ, Bé, ...).
"""

from __future__ import annotations

import re

from .models import DialogueLine

# Character name = everything before the FIRST ':' on a line.
# We accept both the ASCII ':' and the full-width '：' colon (common in CJK
# keyboards). The name part must be reasonably short and not contain a newline.
#
# Group 1: character name, Group 2: dialogue text.
_LINE_RE = re.compile(r"^\s*([^:：\n]{1,60}?)\s*[:：]\s*(.*)$")

NARRATOR_NAME = "Narrator"


def parse_dialogue(
    text: str,
    unknown_line_mode: str = "narrator",
) -> list[DialogueLine]:
    """Parse ``text`` into a list of :class:`DialogueLine`.

    Args:
        text: the raw dialogue script.
        unknown_line_mode: how to handle a line without a ``:`` separator.
            - ``"narrator"`` -> create a line assigned to ``Narrator``.
            - ``"append"``   -> append the line to the previous line's text
              (falls back to ``narrator`` if there is no previous line).

    Rules:
        * Blank lines are skipped.
        * The character name is the part before the first ``:``.
        * Whitespace is trimmed.
        * Original order is preserved; ``index`` starts at 1.
    """
    lines: list[DialogueLine] = []
    index = 1

    for raw in text.splitlines():
        raw_line = raw.rstrip("\n")
        stripped = raw_line.strip()

        # skip blank lines
        if not stripped:
            continue

        match = _LINE_RE.match(stripped)
        if match:
            character = match.group(1).strip()
            dialogue = match.group(2).strip()

            # A line like "http://..." would wrongly match; but for dialogue
            # scripts that's an acceptable trade-off. If the dialogue text is
            # empty (e.g. "A:"), we still keep it so the user notices.
            lines.append(
                DialogueLine(
                    index=index,
                    character=character,
                    text=dialogue,
                    raw_line=raw_line,
                )
            )
            index += 1
        else:
            # No ':' separator -> handle per unknown_line_mode.
            if unknown_line_mode == "append" and lines:
                prev = lines[-1]
                prev.text = (prev.text + " " + stripped).strip()
                prev.raw_line = prev.raw_line + "\n" + raw_line
            else:
                lines.append(
                    DialogueLine(
                        index=index,
                        character=NARRATOR_NAME,
                        text=stripped,
                        raw_line=raw_line,
                    )
                )
                index += 1

    return lines


def detect_characters(lines: list[DialogueLine]) -> list[str]:
    """Return the unique character names, preserving first-seen order."""
    seen: dict[str, None] = {}
    for line in lines:
        if line.character not in seen:
            seen[line.character] = None
    return list(seen.keys())
