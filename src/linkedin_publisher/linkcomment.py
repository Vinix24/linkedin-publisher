"""``linkedin-publisher comment``: a comment on a post, or an answer under a comment, from a link.

Only with the digest of the approved text (see textdigest), checked before the link is read or any network
call, so a changed text never leaves this machine. The normalized text goes out with the community token.
Stdout gets one JSON line on every path; the exit code says the same (EXIT_CODES). An unclear answer is never
sent again by itself: ``--verify`` reads the comments back, and only ``--retry-unclear`` sends again."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import requests

from . import api, claims, links
from . import postqueue as queue
from .api import PublisherError, PublishUncertain, refuse_in_a_test_run
from .config import Config
from .textdigest import normalize_text, text_digest

EXIT_CODES = {"placed": 0, "found": 0, "dry-run": 0, "error": 1, "refused": 2, "uncertain": 3, "not-found": 4,
              "unverifiable": 5}
SENDS = ("comment-attempt", "comment-retry")


class Unverifiable(PublisherError):
    """LinkedIn refused to list the comments, so nobody can say from here whether the comment is there."""


def say(message: str) -> None:
    print(message, file=sys.stderr)


def run(cfg: Config, on: str, text: str | None, text_file: str | None, digest: str | None,
        reply_to: str | None = None, dry_run: bool = False, verify: bool = False, retry_unclear: bool = False,
        fetch: links.Fetch | None = None) -> int:
    approved = (digest or "").strip().lower()
    out: dict[str, Any] = {"status": "refused", "comment_urn": None, "target": None, "digest": approved or None}
    try:
        if not dry_run:
            refuse_in_a_test_run()      # before the token, a refresh, a receipt or any network call
        body = approved_text(text, text_file, approved)
        target = find_target(on, reply_to, None if dry_run else fetch or links.follow_redirect)
        out["target"] = target.urn
        if dry_run:
            out.update(preview(cfg, target, body))
        elif verify:
            out.update(read_back(cfg, target, approved))
        else:
            out.update(place(cfg, target, body, approved, retry_unclear))
    except PublisherError as exc:
        status = ("unverifiable" if isinstance(exc, Unverifiable) else
                  "uncertain" if isinstance(exc, PublishUncertain) else "refused")
        out.update(status=status, error=str(exc))
    except Exception as exc:        # every path ends in the JSON line, a failure nobody foresaw too
        out.update(status="error", error=f"{type(exc).__name__}: {exc}")
    if "error" in out:
        say(f"{out['status'].upper()}: {out['error']}")
    print(json.dumps(out))
    return EXIT_CODES[out["status"]]


def error_line(message: str) -> None:
    """The JSON line of a run that could not start: a configuration that cannot be read."""
    print(json.dumps({"status": "error", "comment_urn": None, "target": None, "digest": None, "error": message}))


def approved_text(text: str | None, text_file: str | None, digest: str) -> str:
    """The normalized text, once its digest is the approved one. Nothing here touches the network."""
    if not digest:
        raise PublisherError("--text-digest is missing. A comment goes out only with the digest of the approved text.")
    if text_file is not None:
        try:
            text = Path(text_file).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PublisherError(f"cannot read {text_file}: {exc}") from exc
    body = normalize_text(text or "")
    if not body:
        raise PublisherError("the text is empty")
    if text_digest(body) != digest:
        raise PublisherError(f"the text has digest {text_digest(body)}, not the approved {digest}. Nothing was sent.")
    return body


def find_target(on: str, reply_to: str | None, fetch: links.Fetch | None) -> links.Target:
    """Where the comment goes. With ``reply_to`` it answers that comment, which must be under the post in
    ``on``. Without ``fetch`` (a dry run) a lnkd.in link is refused: resolving it is a network call."""
    def read(value: str) -> links.Target:
        if links.is_short_link(value):
            if fetch is None:
                raise links.LinkError(f"{value.strip()} is a short link, and a dry run makes no network call. "
                                      "Pass the link it leads to.")
            value = links.resolve_short_link(value, fetch)
        return links.parse_target(value)

    target = read(on)
    if reply_to is None:
        return target
    parent = read(reply_to)
    if parent.kind != "comment":
        raise links.LinkError(f"--reply-to names a post, not a comment: {reply_to}")
    if target == parent or (target.kind == "post" and target.activity == parent.activity):
        return parent
    raise links.LinkError(f"the comment in --reply-to is not under {target.urn}. Give the activity of its post "
                          "in --on, or the comment alone.")


def community_token(cfg: Config) -> tuple[dict[str, str], dict[str, Any]]:
    problem = api.community_problem(cfg)
    if problem:
        raise PublisherError(f"the community token cannot be used: {problem}")
    try:
        creds = api.load_credentials(cfg, "community")
        tokens = api.ensure_access_token(cfg, creds, api.read_tokens(cfg, "community"), say, "community")
    except KeyError as exc:             # a refresh answer without an access token
        raise PublisherError(f"the community token cannot be used: {exc} is missing") from exc
    except (PublisherError, requests.RequestException, ValueError, OSError) as exc:
        raise PublisherError(f"the community token cannot be used: {exc}") from exc
    if not tokens.get("access_token") or not tokens.get("author_urn"):
        raise PublisherError(f"the community token file has no access_token or author_urn. "
                             f"Run: {api.auth_command('community')}")
    return creds, tokens


def urn_of(comment_id: str, target: links.Target) -> str:
    """The URN of a comment from the id LinkedIn gives: built on the activity of the post, unless it is a URN
    already. Without a known activity (a post given as a ugcPost or a share) it is the id as LinkedIn gave it."""
    if comment_id.startswith("urn:li:comment:") or not target.activity:
        return comment_id
    return links.comment_urn(target.activity, comment_id)


def preview(cfg: Config, target: links.Target, body: str) -> dict[str, Any]:
    """What would be sent, from the token file alone: no refresh and no other network call."""
    problem = api.community_problem(cfg)
    if problem:
        raise PublisherError(f"the community token cannot be used: {problem}")
    actor = api.read_tokens(cfg, "community").get("author_urn")
    payload = (api.build_reply_payload(actor, target.activity, target.urn, body) if target.kind == "comment"
               else api.build_comment_payload(actor, target.urn, body))
    return {"status": "dry-run", "url": api.comments_url(target.urn), "payload": payload}


def read_back(cfg: Config, target: links.Target, digest: str) -> dict[str, Any]:
    """A comment of our own actor with this digest under the target. It sends nothing. A comment it finds gets a
    comment-placed receipt marked verified, so no later run sends it again. A 4xx on the list is Unverifiable."""
    creds, tokens = community_token(cfg)
    try:
        elements = api.read_comments(cfg, creds, tokens, target.urn)
    except PublishUncertain:
        raise
    except PublisherError as exc:
        raise Unverifiable(f"{exc} Whether the comment is there cannot be checked from here.") from exc
    for element in elements:
        if not isinstance(element, dict) or element.get("actor") != tokens["author_urn"]:
            continue
        message = element.get("message")
        text = message.get("text") if isinstance(message, dict) else None
        if isinstance(text, str) and text_digest(text) == digest:
            ident = element.get("commentUrn") or element.get("id")
            urn = urn_of(str(ident), target) if ident else None
            if queue.load_comment_states(cfg).get(receipt_key(target, digest)) != "comment-placed":
                receipt(cfg, "comment-placed", target, digest, comment_urn=urn, verified=True)
            return {"status": "found", "comment_urn": urn}
    return {"status": "not-found"}


def sticky(stages: list[str]) -> bool:
    """Whether the history may hold a comment that went out. Clear is only: nothing, or attempts that each got
    their own comment-failed right after them. A comment-failed with no attempt before it was a refusal before the
    request and changes nothing. Any retry, uncertain or placed, or an attempt without its own outcome right after
    it, makes the key sticky for good. Notes do not count."""
    waiting = False                       # an attempt whose outcome must come next
    for stage in stages:
        if stage not in SENDS and stage not in queue.COMMENT_OUTCOMES:
            continue
        if waiting:
            if stage != "comment-failed":
                return True
            waiting = False
        elif stage == "comment-attempt":
            waiting = True
        elif stage != "comment-failed":
            return True
    return waiting


def refusal(stages: list[str], retry_unclear: bool) -> str | None:
    """Why this run may not send, decided on the whole history of this text on this target, never on its last
    receipt (see sticky). None when it may send."""
    if "comment-placed" in stages:
        return "a receipt shows this text was placed here already. Nothing was sent."
    if retry_unclear and not sticky(stages):
        return "--retry-unclear is only for an unclear earlier attempt, and there is none here. Nothing was sent."
    if not retry_unclear and sticky(stages):
        return ("an earlier attempt may have gone through. Nothing was sent. Read it back with --verify. "
                "If a person checked it is not there, add --retry-unclear.")
    return None


def stages_of(cfg: Config, target: links.Target, digest: str) -> list[str]:
    return [row.get("stage") for row in queue.rows_of(cfg, receipt_key(target, digest))]


def place(cfg: Config, target: links.Target, body: str, digest: str, retry_unclear: bool = False) -> dict[str, Any]:
    """One request, under a claim, which only one of two overlapping runs gets. The claim covers the step from
    the receipt check to the attempt receipt. After an outcome receipt the receipts guard the comment, so the
    claim is removed. A run that dies leaves it (see take_over)."""
    key, ckey = receipt_key(target, digest), claim_key(target, digest)
    if not claims.take(cfg, ckey, key):
        take_over(cfg, target, digest, ckey, retry_unclear)
        if not claims.take(cfg, ckey, key):
            raise PublisherError("the comment is not sent. Another run claimed it first.")
    try:
        return place_claimed(cfg, target, body, digest, retry_unclear)
    finally:
        try:
            claims.release(cfg, ckey)
        except OSError as exc:
            say(f"WARNING: the claim could not be removed ({exc}). The receipts still guard this comment.")


def take_over(cfg: Config, target: links.Target, digest: str, ckey: str, retry_unclear: bool) -> None:
    """A claim is there: a run that is sending, or one that died. This is decided before the claim is touched,
    so a refused run leaves it as it is. A plain run takes over a dead claim when the history is clear (see
    sticky), and so everything written after the claim as well: nothing went out, and a comment-claim-recovered
    note says so. --retry-unclear takes over one whose history is sticky. A claim whose process still runs is
    never taken over."""
    holder = claims.read(cfg, ckey)
    if holder is None:
        return

    def reason() -> str | None:
        return refusal(stages_of(cfg, target, digest), retry_unclear)

    why = reason()
    if why:
        raise PublisherError(f"another run holds the claim for this comment, or one ended without an outcome: {why}")
    try:
        claims.supersede(cfg, ckey, lambda: reason() is None)
    except claims.ClaimHeld as exc:
        raise PublisherError(f"the comment is not sent. {exc}") from exc
    if not retry_unclear:
        receipt(cfg, "comment-claim-recovered", target, digest,
                error=f"the run that claimed it at {holder.claimed_at.isoformat()} ended before it sent anything")


def place_claimed(cfg: Config, target: links.Target, body: str, digest: str, retry_unclear: bool) -> dict[str, Any]:
    """Asks refusal again, now that the claim is held. --retry-unclear is a person's word that an unclear
    comment is not there."""
    why = refusal(stages_of(cfg, target, digest), retry_unclear)
    if why:
        raise PublisherError(why)
    if retry_unclear:
        receipt(cfg, "comment-retry", target, digest, strict=True)
    try:
        creds, tokens = community_token(cfg)
    except PublisherError as exc:
        receipt(cfg, "comment-failed", target, digest, error=str(exc))
        raise
    receipt(cfg, "comment-attempt", target, digest, strict=True)
    try:
        if target.kind == "comment":
            comment_id = api.place_reply(cfg, creds, tokens, target.activity, target.urn, body)
        else:
            comment_id = api.place_comment(cfg, creds, tokens, target.urn, body)
    except PublishUncertain as exc:
        receipt(cfg, "comment-uncertain", target, digest, error=str(exc))
        raise PublishUncertain(f"{exc} It is not sent again by itself. Read it back with --verify.") from exc
    except PublisherError as exc:
        receipt(cfg, "comment-failed", target, digest, error=str(exc))
        raise
    urn = urn_of(comment_id, target)
    receipt(cfg, "comment-placed", target, digest, comment_urn=urn)
    return {"status": "placed", "comment_urn": urn}


def receipt_key(target: links.Target, digest: str) -> str:
    """Its own content key: never the key of a post, so it cannot touch the first comment of one."""
    return f"link-comment:{digest}@{target.urn}"


def claim_key(target: links.Target, digest: str) -> str:
    """The receipt key hashed: a plain file name. The claim names the receipt key itself as its file, so a
    scheduled run can find the receipts of a claim that outlives its run."""
    return "link-comment-" + hashlib.sha256(receipt_key(target, digest).encode("utf-8")).hexdigest()


def receipt(cfg: Config, stage: str, target: links.Target, digest: str, comment_urn: str | None = None,
            error: str | None = None, strict: bool = False, verified: bool = False) -> None:
    """With the digest, the target and the comment URN, never a text. Only the attempt must be written before
    the request. An outcome that cannot be written is told on stderr; the JSON line still says what happened."""
    try:
        queue.write_receipt(cfg, stage, key=receipt_key(target, digest), post_urn=target.activity or target.urn,
                            ref=comment_urn, error=error,
                            fields={"digest": digest, "target": target.urn, "comment_urn": comment_urn}
                            | ({"verified": True} if verified else {}))
    except OSError as exc:
        if strict:
            raise PublisherError(f"the receipt could not be written ({exc}). Nothing was sent.") from exc
        say(f"WARNING: the {stage} receipt could not be written: {exc}")
