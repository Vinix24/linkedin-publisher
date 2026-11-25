"""The first comment of a post: placed with the community token, after the post is live.

A comment that fails or is unclear never changes the status of the post. It leaves receipts with
their own stages (``comment-*``), a record in the frontmatter of the post file and a notification.
An unclear comment is never sent again by itself: ``publish --comment-retry`` is the person's decision."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import requests

from . import api, approval, claims, frontmatter
from . import postqueue as queue
from .api import PublisherError, PublishUncertain
from .config import Config

Alert = Callable[[Config, str, str], None]
PLACED, FAILED, UNCERTAIN = "placed", "failed", "uncertain"


def _one_line(text: str) -> str:
    return " ".join(text.replace('"', "'").split())


def record(path: Path, status: str, comment_id: str = "", error: str = "", log=None) -> None:
    """Write the outcome into the frontmatter of the post file. The rest of the file is not touched.

    Best effort: the receipt is the record that counts, and a file that cannot be written (moved, locked,
    an iCloud error) must never turn a placed comment into an error of the run."""
    values = {"comment_status": status}
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
