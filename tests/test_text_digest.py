"""The digest a comment from a link is bound to. The vectors file is meant to be copied by other projects."""
import hashlib
import json
from pathlib import Path

import pytest

from linkedin_publisher.textdigest import normalize_text, text_digest

RAW = (Path(__file__).parent / "data" / "text_digest_vectors.json").read_text(encoding="utf-8")
VECTORS = {vector["name"]: vector for vector in json.loads(RAW)["vectors"]}


@pytest.mark.parametrize("name", VECTORS)
def test_each_vector(name):
    vector = VECTORS[name]
    assert normalize_text(vector["text"]) == vector["normalized"]
    assert normalize_text(vector["normalized"]) == vector["normalized"]      # normalizing twice changes nothing
    assert text_digest(vector["text"]) == vector["digest"] == hashlib.sha256(vector["normalized"].encode()).hexdigest()


def test_the_vectors_hold_the_cases_that_differ_between_editors():
    assert {"empty line at the end", "crlf", "emoji", "e-acute nfc", "e-acute nfd", "spaces inside",
            "byte order mark at the start", "zero width space at the edges", "next line u+0085 at the end",
            "file separator u+001c at the start"} <= VECTORS.keys()
    assert VECTORS["zero width space inside"]["normalized"] == VECTORS["zero width space inside"]["text"]
    nfc, nfd = VECTORS["e-acute nfc"], VECTORS["e-acute nfd"]
    assert nfc["text"] != nfd["text"] and nfc["digest"] == nfd["digest"]
    assert VECTORS["spaces inside"]["normalized"] == VECTORS["spaces inside"]["text"]


def test_the_vectors_file_is_ascii_so_no_editor_can_normalize_it():
    assert RAW.isascii()
