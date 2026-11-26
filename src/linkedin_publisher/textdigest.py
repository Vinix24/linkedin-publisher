"""The digest of a text a person approved. A comment from a link goes out only when its text has it.

The text is normalized first: Unicode NFC, every line ending a ``\\n``, and no white space (Python's
``str.strip()``), U+FEFF or U+200B at the start or the end. Nothing inside the text changes. The text that
goes to LinkedIn is always the normalized one. ``tests/data/text_digest_vectors.json`` pins the rules."""
from __future__ import annotations

import hashlib
import unicodedata

EDGE = "\ufeff\u200b"      # a byte order mark and a zero width space: not white space to str.strip()


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    previous = None
    while text != previous:
        previous, text = text, text.strip().strip(EDGE)
    return text


def text_digest(text: str) -> str:
    """sha256 as hex over the UTF-8 bytes of the normalized text."""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()
