"""The first comment of a post: placed with the community token, after the post is live.

A comment that fails or is unclear never changes the status of the post. It leaves receipts with
their own stages (``comment-*``), a record in the frontmatter of the post file and a notification.
An unclear comment is never sent again by itself: ``publish --comment-retry`` is the person's decision."""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from . import api, approval, claims, frontmatter
from . import postqueue as queue
from .api import PublisherError, PublishUncertain
from .config import Config

Alert = Callable[[Config, str, str], None]
PLACED, FAILED, UNCERTAIN, SCHEDULED = "placed", "failed", "uncertain", "scheduled"
# Statuses in the receipts after which a scheduled comment is never placed by a scheduled run.
FINISHED_STAGES = {"comment-placed", "comment-uncertain", "comment-attempt"}


def _one_line(text: str) -> str:
    return " ".join(text.replace('"', "'").split())


def record(path: Path, status: str, comment_id: str = "", error: str = "", due: str = "", log=None) -> None:
    """Write the outcome into the frontmatter of the post file. The rest of the file is not touched.

    Best effort: the receipt is the record that counts, and a file that cannot be written (moved, locked,
    an iCloud error) must never turn a placed comment into an error of the run."""
    values = {"comment_status": status}
    if due:
        values["comment_due"] = due
    if comment_id:
        values["comment_urn"] = comment_id
    if error:
        values["comment_error"] = f'"{_one_line(error)}"'
    try:
        path.write_text(frontmatter.set_fields(path.read_text(encoding="utf-8"), values), encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        if log:
            log(f"Could not write the comment status into {path.name}: {exc}. The receipts hold the outcome.")


def claim_key(key: str) -> str:
    """The claim of a comment is not the claim of its post: that one stays for good after the post is placed."""
    return f"comment-{key}"


def place(cfg: Config, post: queue.Post, post_urn: str, record_path: Path, log, alert: Alert) -> str | None:
    """Place the first comment of a post that is live. Returns PLACED, FAILED or UNCERTAIN, or None
    when the post has no comment."""
    if not post.comment:
        return None
    name = post.path.name
    problem = api.community_problem(cfg)
    if problem:
        return _failed(cfg, post, record_path, log, alert, f"the comment was not sent: {problem}")
    try:
        creds = api.load_credentials(cfg, "community")
        tokens = api.ensure_access_token(cfg, creds, api.read_tokens(cfg, "community"), log, "community")
    except (PublisherError, requests.RequestException, ValueError, OSError) as exc:
        return _failed(cfg, post, record_path, log, alert, f"the comment was not sent: {exc}")
    queue.write_receipt(cfg, "comment-attempt", post)
    try:
        comment_id = api.place_comment(cfg, creds, tokens, post_urn, post.comment)
    except PublishUncertain as exc:
        queue.write_receipt(cfg, "comment-uncertain", post, error=str(exc))
        record(record_path, UNCERTAIN, error=str(exc), log=log)
        log(f"UNCLEAR: first comment of {name}: {exc} It will not be tried again automatically. Check "
            f"LinkedIn. If the comment is not there: linkedin-publisher publish --comment-retry --file {record_path}")
        alert(cfg, "LinkedIn first comment: unclear if it went out", f"Check LinkedIn for {post.path.stem}.")
        return UNCERTAIN
    except PublisherError as exc:
        return _failed(cfg, post, record_path, log, alert, str(exc))
    queue.write_receipt(cfg, "comment-placed", post, post_urn=post_urn, ref=comment_id)
    record(record_path, PLACED, comment_id, log=log)
    log(f"First comment placed on {name} ({comment_id}).")
    return PLACED


def _failed(cfg: Config, post: queue.Post, record_path: Path, log, alert: Alert, reason: str) -> str:
    queue.write_receipt(cfg, "comment-failed", post, error=reason)
    record(record_path, FAILED, error=reason, log=log)
    log(f"First comment of {post.path.name} is not placed: {reason}. The post itself is live. After fixing "
        f"it: linkedin-publisher publish --comment-retry --file {record_path}")
    alert(cfg, "LinkedIn first comment not placed", f"{post.path.stem}: {reason}")
    return FAILED


def retry(cfg: Config, path: Path, log, alert: Alert) -> str:
    """Place the first comment of a post that is already live, once a person decided to.

    It takes the claim of the comment, the same one a scheduled sweep takes, so the two never both send.
    A claim left by an earlier unclear run is taken over only when its process is gone and no receipt
    shows the comment as placed: the person checked LinkedIn, as with ``--retry`` for a post."""
    post = queue.read_post(path)
    if not post.comment:
        raise PublisherError(f"{path.name}: there is no {queue.comment_path_for(path).name} next to it.")
    key = queue.content_key(queue.post_text(post.body, cfg.notes_headings))
    urn = queue.placed_urn(cfg, key)
    if not urn:
        raise PublisherError(f"{path.name}: no receipt shows this post was placed, so there is nothing to comment on.")
    if queue.load_comment_states(cfg).get(key) == "comment-placed":
        raise PublisherError(f"{path.name}: a receipt shows the first comment was already placed.")
    if cfg.approval_required:
        state, _current = approval.check(post, cfg)
        if state != approval.APPROVED:
            raise PublisherError(f"{path.name}: approval is required and this version of the post and its comment "
                                 f"is not approved ({state}). Read it again, then run: linkedin-publisher approve "
                                 f"--file {path} --by NAME")
    ckey = claim_key(key)
    if not claims.take(cfg, ckey, f"{path.name}#comment"):
        try:
            claims.supersede(cfg, ckey, lambda: queue.load_comment_states(cfg).get(key) != "comment-placed")
        except claims.ClaimHeld as exc:
            raise PublisherError(f"{path.name}: the comment is not sent. {exc}") from exc
        if not claims.take(cfg, ckey, f"{path.name}#comment"):
            raise PublisherError(f"{path.name}: the comment is not sent. Another run claimed it first.")
    status = place(cfg, post, urn, path, log, alert) or FAILED
    if status == FAILED:
        claims.release(cfg, ckey)       # nothing went out
    return status


# ------------------------------------------------------------------ a delayed comment

def after_post(cfg: Config, post: queue.Post, post_urn: str, target: Path | None, now: dt.datetime,
               log, alert: Alert) -> str | None:
    """What happens to the comment right after the post is placed. With a delay it is scheduled in the
    published file: a later run places it. Without one, or when the post file could not be moved
    (so there is no published file to find later), it is placed now."""
    delay = queue.comment_delay(post, cfg) if post.comment else 0
    if not delay or target is None:
        if delay:
            log(f"{post.path.name}: the post file was not moved, so its first comment is placed now, not after {delay} minutes.")
        return place(cfg, post, post_urn, target or post.path, log, alert)
    due = (now.astimezone() + dt.timedelta(minutes=delay)).isoformat(timespec="seconds")
    queue.write_receipt(cfg, "comment-scheduled", post, post_urn=post_urn, ref=due)
    record(target, SCHEDULED, due=due, log=log)
    log(f"First comment of {post.path.name} is scheduled for {due}. A scheduled run places it from then on.")
    return SCHEDULED


@dataclass
class Scheduled:
    path: Path
    post: queue.Post
    key: str
    due: dt.datetime | None     # timezone-aware; None when the file holds no readable time


def _aware(moment: dt.datetime) -> dt.datetime:
    return moment.astimezone()


def scheduled(cfg: Config) -> list[Scheduled]:
    """The posts in the published folder whose comment is waiting for its time, soonest first.
    A comment a receipt shows as placed, unclear or attempted is not waiting, whatever the file says."""
    try:
        names = sorted(os.listdir(cfg.published_dir))
    except OSError:
        return []
    finished = {key for key, stage in queue.load_comment_states(cfg).items() if stage in FINISHED_STAGES}
    found = []
    for name in names:
        if not name.endswith(".md") or name.endswith(queue.COMMENT_SUFFIX):
            continue
        try:
            post = queue.read_post(cfg.published_dir / name)
        except OSError:
            continue
        if post.meta.get("comment_status") != SCHEDULED:
            continue
        key = queue.content_key(queue.post_text(post.body, cfg.notes_headings))
        if key in finished:
            continue
        try:
            due = _aware(dt.datetime.fromisoformat(post.meta.get("comment_due", "")))
        except ValueError:
            due = None
        found.append(Scheduled(post.path, post, key, due))
    found.sort(key=lambda item: (item.due is None, item.due, item.path.name))
    return found


def sweep(cfg: Config, now: dt.datetime, log, alert: Alert) -> int:
    """Place every scheduled comment whose time has come. Returns how many were placed.

    Two runs can pass the checks above together, so the claim decides: exactly one gets it. It stays
    after a placed or unclear comment and is removed after a failure where nothing was sent. One
    comment that cannot be handled never stops the others."""
    moment, placed = _aware(now), 0
    for item in scheduled(cfg):
        name = item.path.name
        try:
            if item.due is None:
                _failed(cfg, item.post, item.path, log, alert, "the comment_due field is missing or not a time")
                continue
            if item.due > moment:
                continue
            ckey = claim_key(item.key)
            if not claims.take(cfg, ckey, f"{name}#comment"):
                _report_held_claim(cfg, item, ckey, moment, log, alert)
                continue
            urn = queue.placed_urn(cfg, item.key)
            if not urn:
                result = _failed(cfg, item.post, item.path, log, alert,
                                 "no receipt shows the post was placed, so there is nothing to comment on")
            else:
                result = place(cfg, item.post, urn, item.path, log, alert)
            if result == FAILED:
                claims.release(cfg, ckey)       # nothing went out
            placed += result == PLACED
        except (PublisherError, OSError) as exc:
            log(f"First comment of {name} could not be handled: {exc}")
    return placed


def _report_held_claim(cfg: Config, item: Scheduled, ckey: str, moment: dt.datetime, log, alert: Alert) -> None:
    """A claim next to a waiting comment: another run is placing it, or one ended without an outcome.
    Told once per claim, after the same limit the posts use."""
    claim = claims.read(cfg, ckey)
    if claim is None or moment.astimezone(dt.UTC) - claim.claimed_at <= dt.timedelta(minutes=cfg.claim_unresolved_minutes):
        return
    ref = f"comment:{claim.claimed_at.isoformat()}"
    if (item.key, ref) in queue.load_notes(cfg, "claim-unresolved"):
        return
    queue.write_receipt(cfg, "claim-unresolved", key=item.key, file_name=item.path.name, ref=ref)
    log(f"The first comment of {item.path.name} was claimed a while ago and has no outcome. It will not be placed "
        f"again by itself. Check LinkedIn. If it is not there: linkedin-publisher publish --comment-retry --file {item.path}")
    alert(cfg, "LinkedIn first comment: claimed but no outcome", f"Check LinkedIn for {item.path.stem}.")
