"""The digest of a text a person approved. A comment from a link goes out only when its text has it.

The text is normalized first: Unicode NFC, every line ending a ``\\n``, and no white space at the start
or the end (Python's ``str.strip()``). Nothing inside the text changes. The text that goes to LinkedIn
is always the normalized one. ``tests/data/text_digest_vectors.json`` pins the rules for other projects."""
from __future__ import annotations

import hashlib
import unicodedata


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def text_digest(text: str) -> str:
    """sha256 as hex over the UTF-8 bytes of the normalized text."""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()
