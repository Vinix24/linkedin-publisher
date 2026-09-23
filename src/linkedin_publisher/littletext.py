"""Turn a Markdown post into text that LinkedIn will not silently cut short.

LinkedIn's ``commentary`` field is neither Markdown nor plain text. It is
"Little Text Format", in which ``\\ | { } @ [ ] ( ) < > * _ ~`` are reserved.
An unescaped reserved character is read as formatting, and everything after it
can disappear from the post. The API still answers HTTP 201, so nothing tells
you it happened.

Two steps, in this order:

1. normalize Markdown to plain text (LinkedIn does not render Markdown anyway)
2. escape whatever reserved characters remain

The other order would escape Markdown asterisks and underscores instead of
removing them.
"""
from __future__ import annotations

import re

MD_HEADER = (r"^#{1,6}[ \t]+", "")
MD_LINK = (r"\[([^\]\n]+)\]\(([^)\n]+)\)", r"\1 \2")
MD_BULLET = (r"^(\s*)[-*+][ \t]+", r"\1- ")
MD_BOLD_STAR = (r"\*\*([^\n]+?)\*\*", r"\1")
MD_BOLD_UNDERSCORE = (r"__([^\n]+?)__", r"\1")
# (?<!\w) and (?!\w) keep intraword underscores intact: without them
# SOME_ENV_VAR_NAME would be read as italic around "ENV".
MD_ITALIC_STAR = (r"(?<!\w)\*([^*\n]+?)\*(?!\w)", r"\1")
MD_ITALIC_UNDERSCORE = (r"(?<!\w)_([^_\n]+?)_(?!\w)", r"\1")

# The backslash is escaped separately and first, see escape().
# '#' is deliberately not reserved here, so hashtags keep working.
RESERVED = "|{}@[]()<>*_~"


def _sub_count(pattern: str, repl: str, text: str, flags: int = 0) -> tuple[str, int]:
    """Like re.sub, but only counts matches that actually change the text."""
    count = 0

    def apply(match: re.Match[str]) -> str:
        nonlocal count
        new = match.expand(repl)
        if new != match.group(0):
            count += 1
        return new

    return re.sub(pattern, apply, text, flags=flags), count


def normalize_markdown(text: str) -> tuple[str, dict[str, int]]:
    """Markdown to plain text, plus a count of what changed per kind.

    Bold runs before italic, otherwise the italic pattern would read the inner
    asterisks of **bold** as a nested italic span.
    """
    counts: dict[str, int] = {}
    text, counts["headers"] = _sub_count(*MD_HEADER, text, flags=re.M)
    text, counts["links"] = _sub_count(*MD_LINK, text)
    text, counts["bullets"] = _sub_count(*MD_BULLET, text, flags=re.M)
    text, bold_star = _sub_count(*MD_BOLD_STAR, text)
    text, bold_underscore = _sub_count(*MD_BOLD_UNDERSCORE, text)
    counts["bold"] = bold_star + bold_underscore
    text, italic_star = _sub_count(*MD_ITALIC_STAR, text)
    text, italic_underscore = _sub_count(*MD_ITALIC_UNDERSCORE, text)
    counts["italic"] = italic_star + italic_underscore
    return text, counts


def escape(text: str) -> str:
    """Escape reserved characters. Backslash goes first, otherwise the escape
    backslashes added in the second step would be escaped a second time."""
    text = text.replace("\\", "\\\\")
    return re.sub(r"([|{}@\[\]()<>*_~])", r"\\\1", text)


def prepare(text: str) -> str:
    """Exactly what goes into the commentary field."""
    normalized, _ = normalize_markdown(text)
    return escape(normalized)


def find_unescaped(text: str) -> list[str]:
    """Preflight: every reserved character that is not preceded by a backslash.

    On output of prepare() this is always empty. It is the last check before
    sending, not the first."""
    found: list[str] = []
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char in RESERVED:
            found.append(char)
        index += 1
    return found
