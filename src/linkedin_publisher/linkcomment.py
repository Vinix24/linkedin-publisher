"""``linkedin-publisher comment``: a comment on a post, or an answer under a comment, from a link.

Only with the digest of the approved text (see textdigest), checked before the link is read or any network
call, so a changed text never leaves this machine. The normalized text goes out with the community token.
Stdout gets one JSON line; the exit code says the same (EXIT_CODES). An unclear answer is never sent again
by itself: ``--verify`` reads the comments back and sends nothing."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import requests

from . import api, links
from . import postqueue as queue
from .api import PublisherError, PublishUncertain
from .config import Config
from .textdigest import normalize_text, text_digest

EXIT_CODES = {"placed": 0, "found": 0, "dry-run": 0, "refused": 2, "uncertain": 3, "not-found": 4}


def say(message: str) -> None:
    print(message, file=sys.stderr)


def run(cfg: Config, on: str, text: str | None, text_file: str | None, digest: str | None,
        reply_to: str | None = None, dry_run: bool = False, verify: bool = False,
        fetch: links.Fetch | None = None) -> int:
    approved = (digest or "").strip().lower()
    out: dict[str, Any] = {"status": "refused", "comment_urn": None, "target": None, "digest": approved or None}
    try:
        body = approved_text(text, text_file, approved)
        target = find_target(on, reply_to, None if dry_run else fetch or links.follow_redirect)
        out["target"] = target.urn
        if dry_run:
            out.update(preview(cfg, target, body))
        elif verify:
            out.update(read_back(cfg, target, approved))
        else:
            out.update(place(cfg, target, body, approved))
    except PublisherError as exc:
        out.update(status="uncertain" if isinstance(exc, PublishUncertain) else "refused", error=str(exc))
        say(f"{out['status'].upper()}: {exc}")
    print(json.dumps(out))
    return EXIT_CODES[out["status"]]


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
    except (PublisherError, requests.RequestException, ValueError, OSError) as exc:
        raise PublisherError(f"the community token cannot be used: {exc}") from exc
    if not tokens.get("author_urn"):
        raise PublisherError(f"the community token names no author. Run: {api.auth_command('community')}")
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
    """A comment of our own actor with this digest under the target. Sends nothing and writes no receipt."""
    creds, tokens = community_token(cfg)
    for element in api.read_comments(cfg, creds, tokens, target.urn):
        if not isinstance(element, dict) or element.get("actor") != tokens["author_urn"]:
            continue
        message = element.get("message")
        text = message.get("text") if isinstance(message, dict) else None
        if isinstance(text, str) and text_digest(text) == digest:
            ident = element.get("commentUrn") or element.get("id")
            return {"status": "found", "comment_urn": urn_of(str(ident), target) if ident else None}
    return {"status": "not-found"}


def place(cfg: Config, target: links.Target, body: str, digest: str) -> dict[str, Any]:
    """One request. A receipt that shows this text placed on this target already refuses a second one."""
    key = receipt_key(target, digest)
    earlier = next((ref for seen, ref in queue.load_notes(cfg, "comment-placed") if seen == key), None)
    if earlier is not None:
        raise PublisherError(f"a receipt shows this text was placed here already ({earlier}). Nothing was sent.")
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


def receipt(cfg: Config, stage: str, target: links.Target, digest: str, comment_urn: str | None = None,
            error: str | None = None, strict: bool = False) -> None:
    """With the digest, the target and the comment URN, never a text. Only the attempt must be written before
    the request. An outcome that cannot be written is told on stderr; the JSON line still says what happened."""
    try:
        queue.write_receipt(cfg, stage, key=receipt_key(target, digest), post_urn=target.activity or target.urn,
                            ref=comment_urn, error=error,
                            fields={"digest": digest, "target": target.urn, "comment_urn": comment_urn})
    except OSError as exc:
        if strict:
            raise PublisherError(f"the receipt could not be written ({exc}). Nothing was sent.") from exc
        say(f"WARNING: the {stage} receipt could not be written: {exc}")
