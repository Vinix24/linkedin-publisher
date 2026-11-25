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
from .firstcomment import FINISHED_STAGES
from .textdigest import normalize_text, text_digest

EXIT_CODES = {"placed": 0, "found": 0, "dry-run": 0, "error": 1, "refused": 2, "uncertain": 3, "not-found": 4,
              "unverifiable": 5}
UNCLEAR_STAGES = ("comment-attempt", "comment-uncertain")


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


def place(cfg: Config, target: links.Target, body: str, digest: str, retry_unclear: bool = False) -> dict[str, Any]:
    """One request, under a claim, which only one of two overlapping runs gets. The claim covers the step from
    the receipt check to the attempt receipt. After an outcome receipt the receipts guard the comment, so the
    claim is removed; a run that dies mid-request leaves it, and only --retry-unclear takes it over."""
    key, ckey = receipt_key(target, digest), claim_key(target, digest)
    if not claims.take(cfg, ckey, f"comment on {target.urn}"):
        if not retry_unclear:
            raise PublisherError("another run holds the claim for this comment, or one ended without an outcome. "
                                 "Nothing was sent. Read it back with --verify.")
        try:
            claims.supersede(cfg, ckey, lambda: queue.load_comment_states(cfg).get(key) != "comment-placed")
        except claims.ClaimHeld as exc:
            raise PublisherError(f"the comment is not sent. {exc}") from exc
        if not claims.take(cfg, ckey, f"comment on {target.urn}"):
            raise PublisherError("the comment is not sent. Another run claimed it first.")
    try:
        return place_claimed(cfg, target, body, digest, retry_unclear)
    finally:
        claims.release(cfg, ckey)


def place_claimed(cfg: Config, target: links.Target, body: str, digest: str, retry_unclear: bool) -> dict[str, Any]:
    """After an attempt, an unclear answer or a placed comment for this text and target (FINISHED_STAGES, as for a
    first comment) nothing is sent again. --retry-unclear is a person's word that an unclear one is not there."""
    state = queue.load_comment_states(cfg).get(receipt_key(target, digest))
    if retry_unclear and state not in UNCLEAR_STAGES:
        raise PublisherError(f"--retry-unclear is only for an unclear earlier attempt, and the latest receipt here "
                             f"is {state or 'none'}. Nothing was sent.")
    if state == "comment-placed":
        raise PublisherError("a receipt shows this text was placed here already. Nothing was sent.")
    if state in FINISHED_STAGES and not retry_unclear:
        raise PublisherError(f"an earlier attempt may have gone through ({state}). Nothing was sent. Read it back "
                             "with --verify. If a person checked it is not there, add --retry-unclear.")
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
    """The receipt key hashed: a plain file name."""
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
