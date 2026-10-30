"""Approval: a person signs off on one exact version of a post, in the post file.

``approve`` writes three frontmatter fields: who, when, and the approval key. The key is a
hash over everything that goes out, so changing the text, the format, the alt text, the
title or the image, video or PDF after the sign-off makes the approval worthless. A scheduled
run with ``[approval] required = true`` only posts when the key in the file is the key of
the post as it is now.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import shutil
from pathlib import Path

from . import frontmatter, littletext
from . import postqueue as queue
from .api import PublisherError
from .config import Config

APPROVED, MISSING, MISMATCH = "approved", "missing", "mismatch"
KEY_VERSION = b"linkedin-publisher approval key v1\n"
CHUNK = 1024 * 1024


def approval_key(post: queue.Post, cfg: Config) -> str:
    """sha256 over what goes out, nothing else.

    Not in it: the slot, the status, any other frontmatter field, the private notes and the
    three approval fields. That is why a changed slot keeps the approval and writing the
    approval does not undo it. A file next to the post that its format does not use is not in
    it either, because it is never sent. Everything is length-prefixed, so two different posts
    cannot produce the same bytes by moving text from one part to the next."""
    out = queue.outgoing(post)
    text = queue.post_text(post.body, cfg.notes_headings)
    digest = hashlib.sha256(KEY_VERSION)

    def part(label: str, data: bytes) -> None:
        digest.update(f"{label}:{len(data)}\n".encode() + data + b"\n")

    part("text", text.encode("utf-8"))
    part("commentary", littletext.prepare(text).encode("utf-8"))   # the exact bytes of the commentary field
    part("format", post.meta.get("format", "text").encode("utf-8"))
    if out.image:
        part("alt_text", (out.alt_text or "").encode("utf-8"))
    if out.video or out.document:
        part("title", (out.title or "").encode("utf-8"))
    for label, media in (("image", out.image), ("video", out.video), ("document", out.document)):
        if media:
            with media.open("rb") as fh:
                digest.update(f"{label}:{os.fstat(fh.fileno()).st_size}\n".encode())
                while chunk := fh.read(CHUNK):
                    digest.update(chunk)
                digest.update(b"\n")
    return digest.hexdigest()


def check(post: queue.Post, cfg: Config) -> tuple[str, str]:
    """(APPROVED | MISSING | MISMATCH, the key of the post as it is now).

    An approval counts only when all three fields are there: a key without a name and a
    time is not a record of anyone having approved anything."""
    current = approval_key(post, cfg)
    by = post.meta.get(cfg.approved_by_field)
    at = post.meta.get(cfg.approved_at_field)
    stored = post.meta.get(cfg.approved_key_field)
    if not (by and at and stored):
        return MISSING, current
    return (APPROVED if stored == current else MISMATCH), current


def _clean_name(by: str) -> str:
    name = by.strip()
    if not name:
        raise PublisherError("--by needs the name of the person who approves this post.")
    if any(ord(char) < 32 or char in '"\\' for char in name):
        raise PublisherError("--by cannot contain quotes, backslashes or line breaks.")
    return name


def approve(cfg: Config, path: Path, by: str, now: dt.datetime | None = None) -> str:
    """Write the approval into the file and return the key. The rest of the file is not touched."""
    name = _clean_name(by)
    try:
        original = path.read_bytes()
        raw = original.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PublisherError(f"{path.name}: cannot read the file as UTF-8 text: {exc}") from exc
    post = queue.parse_post(path, raw)
    problem = queue.content_problem(post, cfg)
    if problem:
        raise PublisherError(f"{path.name}: {problem}")
    if queue.content_key(queue.post_text(post.body, cfg.notes_headings)) in queue.load_placed_keys(cfg):
        raise PublisherError(f"{path.name}: a receipt shows this was already posted.")
    try:
        key = approval_key(post, cfg)
        stamp = (now or dt.datetime.now()).astimezone().isoformat(timespec="seconds")
        updated = frontmatter.set_fields(raw, {
            cfg.approved_by_field: f'"{name}"',
            cfg.approved_at_field: stamp,
            cfg.approved_key_field: f'"{key}"',
        })
    except ValueError as exc:
        raise PublisherError(f"{path.name}: {exc}. Approval is written into the frontmatter.") from exc
    except OSError as exc:
        raise PublisherError(f"{path.name}: cannot read the media of this post: {exc}") from exc
    _replace(path, original, updated.encode("utf-8"))
    return key


def _replace(path: Path, original: bytes, content: bytes) -> None:
    """Temp file next to the original, then a rename. Nothing is written if the file changed
    since it was read, because the approval would then cover text the person never saw."""
    temp = path.with_name(f".{path.name}.approving-{os.getpid()}")
    try:
        with temp.open("wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        shutil.copymode(path, temp)
        if path.read_bytes() != original:
            raise PublisherError(f"{path.name}: the file changed while it was being approved. "
                                 "Nothing was written. Run approve again.")
        os.replace(temp, path)
    except OSError as exc:
        raise PublisherError(f"{path.name}: could not write the approval: {exc}") from exc
    finally:
        temp.unlink(missing_ok=True)
