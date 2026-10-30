"""Precheck: what would stop the posts of the coming days from going out.

It reads the queue, asks the addresses of the checks and reports. It never sends, never edits
a file and never takes a claim. Running it a day ahead, and again on the day itself, leaves
time to fix what it finds.
"""
from __future__ import annotations

import datetime as dt
import shlex
from dataclasses import dataclass, field

from . import approval, checks, claims
from . import postqueue as queue
from .config import Config

PUBLISHER, EXTERNAL = "publisher", "external"
MEDIA_MISSING = {
    "image": "no image file next to the post (same name, ending in .png, .jpg, .jpeg or .gif)",
    "video": "no .mp4 file next to the post (same name)",
    "document": "no .pdf file next to the post (same name)",
}


@dataclass(frozen=True)
class Problem:
    kind: str       # approval-missing | approval-mismatch | measure-line | media | content | expired | blocked | check-failed
    message: str    # what is wrong and what to do about it
    ref: str        # names this problem, so the same problem is not reported twice


@dataclass
class PostReport:
    post: queue.Post
    slot: dt.datetime
    route: str
    problems: list[Problem] = field(default_factory=list)

    @property
    def checked(self) -> bool:
        return self.route != EXTERNAL


def evaluate(cfg: Config, now: dt.datetime, days_ahead: int) -> list[PostReport]:
    """A report for every queued post whose slot lies between now and the end of the day
    ``days_ahead`` days from today. Posts already past their slot and posts a receipt shows as
    placed are not part of it."""
    last = now.date() + dt.timedelta(days=days_ahead)
    states = queue.load_states(cfg)
    reports = []
    for path in queue.queue_files(cfg):
        try:
            post = queue.read_post(path)
        except OSError:
            continue
        slot = queue.slot_datetime(post.meta, cfg)
        if post.meta.get("status") != cfg.ready_status or slot is None or slot <= now or slot.date() > last:
            continue
        key = queue.content_key(queue.post_text(post.body, cfg.notes_headings))
        state = states.get(key)
        if state == "placed":
            continue
        if approval.is_external(post, cfg):
            reports.append(PostReport(post, slot, EXTERNAL))
            continue
        reports.append(PostReport(post, slot, PUBLISHER, problems_of(cfg, post, slot, key, state)))
    reports.sort(key=lambda r: (r.slot, r.post.path.name))
    return reports


def problems_of(cfg: Config, post: queue.Post, slot: dt.datetime, key: str, state: str | None) -> list[Problem]:
    problems = []
    path = shlex.quote(str(post.path))
    if cfg.approval_required:
        problems += _approval_problems(cfg, post, path)

    media = _media_missing(post)
    if media:
        problems.append(Problem("media", media, "media"))
    else:
        content = queue.content_problem(post, cfg)
        if content:
            problems.append(Problem("content", content, f"content:{content}"))

    expires = post.meta.get(cfg.expires_key)
    if expires:
        try:
            if dt.date.fromisoformat(expires) < slot.date():
                problems.append(Problem(
                    "expired", f"{cfg.expires_key} {expires} is before the day of the slot "
                               f"({slot:%Y-%m-%d}), so the post would be skipped as no longer true", "expired"))
        except ValueError:
            pass

    if state in queue.UNCERTAIN_STAGES or claims.exists(cfg, key) or queue.held_by_earlier_attempt(cfg, post):
        problems.append(Problem(
            "blocked", "a scheduled run will not send it: an earlier attempt may have gone through, or a "
                       "claim exists. Check LinkedIn. If the post is not there: "
                       f"linkedin-publisher publish --post --file {path} --retry", "blocked"))

    for failure in checks.run_all(checks.parse(post.meta.get(cfg.checks_field)), cfg.check_timeout_seconds):
        problems.append(Problem("check-failed", f"check failed, {failure.describe()}", f"check:{failure.ref}"))
    return problems


def _approval_problems(cfg: Config, post: queue.Post, path: str) -> list[Problem]:
    line = approval.measure_line_problem(post, cfg)
    if line:
        return [Problem("measure-line", line, "measure-line")]
    try:
        state, _current = approval.check(post, cfg)
    except OSError as exc:
        return [Problem("media", f"the files of the post cannot be read: {exc}", "media-unreadable")]
    if state == approval.MISSING:
        return [Problem("approval-missing", f"nobody approved it. Run: linkedin-publisher approve --file {path} "
                                            "--by NAME", "approval-missing")]
    if state == approval.MISMATCH:
        return [Problem("approval-mismatch", "the approval belongs to a different version of the post. Read it "
                                             f"again, then run: linkedin-publisher approve --file {path} --by NAME",
                        "approval-mismatch")]
    return []


def _media_missing(post: queue.Post) -> str | None:
    fmt = post.meta.get("format")
    present = {"image": post.image, "video": post.video, "document": post.document}
    if fmt in present and not present[fmt]:
        return MEDIA_MISSING[fmt]
    return None

