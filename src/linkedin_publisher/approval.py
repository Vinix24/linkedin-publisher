"""Approval: a person signs off on one exact version of a post, in the post file.

``approve`` writes three frontmatter fields: who, when, and the approval key. The key is a
hash over everything that goes out, so changing the text, the format, the alt text, the
title or the image, video or PDF after the sign-off makes the approval worthless. A scheduled
run with ``[approval] required = true`` only posts when the key in the file is the key of
the post as it is now.

A post with a measure line (see ``measures``) has one number in it that is measured shortly
before it goes out. The key masks exactly those digits and covers the measure line itself, so
the number may change after the approval and nothing else may.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import shutil
from pathlib import Path

from . import frontmatter, littletext, measures
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
    cannot produce the same bytes by moving text from one part to the next.

    A post with a measure line is hashed with the digits of the measured number replaced by a
    fixed token, and the measure line and the place of the number are parts of their own. A
    post without one is hashed exactly as before. Raises MeasureLineError when the measure
    line does not match the text exactly once: such a post cannot be approved."""
    out = queue.outgoing(post)
    text = queue.post_text(post.body, cfg.notes_headings)
    line, found = measures.line_of(post, cfg), None
    if line:
        found = line.find(text)
        text = measures.masked(text, found)
    digest = hashlib.sha256(KEY_VERSION)

    def part(label: str, data: bytes) -> None:
        digest.update(f"{label}:{len(data)}\n".encode() + data + b"\n")

    part("text", text.encode("utf-8"))
    part("commentary", littletext.prepare(text).encode("utf-8"))   # the exact bytes of the commentary field
    if line:
        part("measure_line", line.template.encode("utf-8"))
        part("measure_at", str(found.start(1)).encode("ascii"))
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
    time is not a record of anyone having approved anything. A measure line that does not match
    the text exactly once is a mismatch, and its ``measures.MeasureLineError.ref`` stands in for
    the key."""
    try:
        current = approval_key(post, cfg)
    except measures.MeasureLineError as exc:
        return MISMATCH, exc.ref
    by = post.meta.get(cfg.approved_by_field)
    at = post.meta.get(cfg.approved_at_field)
    stored = post.meta.get(cfg.approved_key_field)
    if not (by and at and stored):
        return MISSING, current
    return (APPROVED if stored == current else MISMATCH), current


def is_external(post: queue.Post, cfg: Config) -> bool:
    """A post runs somewhere else, outside this tool, when its route field holds the configured
    text and the file carries no approval key.

    An approval in the file means this tool is the route: the post is checked in full and a scheduled
    run sends it, also when that approval is for a different version (a mismatch is news, not a
    reason to look away). Without the configured text nothing is external."""
    if not cfg.external_route_field or not cfg.external_route_text:
        return False
    if post.meta.get(cfg.approved_key_field):
        return False
    return cfg.external_route_text in post.meta.get(cfg.external_route_field, "")


def measure_line_problem(post: queue.Post, cfg: Config) -> str | None:
    """Why the measure line of this post cannot take part in an approval, or None."""
    try:
        line = measures.line_of(post, cfg)
        if line:
            line.find(queue.post_text(post.body, cfg.notes_headings))
    except measures.MeasureLineError as exc:
        return str(exc)
    return None


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
    except measures.MeasureLineError as exc:
        raise PublisherError(f"{path.name}: {exc}") from exc
    except ValueError as exc:
        raise PublisherError(f"{path.name}: {exc}. Approval is written into the frontmatter.") from exc
    except OSError as exc:
        raise PublisherError(f"{path.name}: cannot read the media of this post: {exc}") from exc
    replace_file(path, original, updated.encode("utf-8"))
    return key


def replace_file(path: Path, original: bytes, content: bytes, *, what: str = "approval",
             command: str = "approve") -> None:
    """Temp file next to the original, then a rename. Nothing is written if the file changed
    since it was read, because the ``what`` would then cover text the person never saw."""
    temp = path.with_name(f".{path.name}.{what}-{os.getpid()}")
    try:
        with temp.open("wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        shutil.copymode(path, temp)
        if path.read_bytes() != original:
            raise PublisherError(f"{path.name}: the file changed while the {what} was being written. "
                                 f"Nothing was written. Run {command} again.")
        os.replace(temp, path)
    except OSError as exc:
        raise PublisherError(f"{path.name}: could not write the {what}: {exc}") from exc
    finally:
        temp.unlink(missing_ok=True)
