"""Where a comment goes, read from a LinkedIn link or a bare URN.

``parse_target`` makes no network call. It knows these forms and nothing else:

- ``/feed/update/urn:li:activity:<id>/``, and the same with ``ugcPost`` or ``share``
- ``/posts/<slug>-share-<id>-<x>/``, and the same with ``activity`` or ``ugcPost``
- ``?commentUrn=urn:li:comment:(activity:<post>,<comment>)``, also with ``urn:li:activity:<post>``
- ``?dashCommentUrn=urn:li:fsd_comment:(<comment>,urn:li:activity:<post>)``
- the bare URNs of all of these, URL-encoded or not

An answer to a comment is placed on the activity of its post, so only a comment whose URN is built on an
activity is known. A link to an answer is refused: LinkedIn has one level of answers. Any other form is an
error. The parser never guesses. A ``lnkd.in`` link is resolved first, by ``resolve_short_link``."""
from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

import requests

from .api import PublisherError

POST_URN = re.compile(r"urn:li:(activity|ugcPost|share):([0-9]+)")
COMMENT_URN = re.compile(r"urn:li:comment:\((?:urn:li:)?activity:([0-9]+),([0-9]+)\)")
FSD_COMMENT_URN = re.compile(r"urn:li:fsd_comment:\(([0-9]+),urn:li:activity:([0-9]+)\)")
POSTS_SLUG = re.compile(r".+-(activity|ugcPost|share)-([0-9]+)-[A-Za-z0-9_-]+")
SHORT_HOSTS = ("lnkd.in", "www.lnkd.in")
REDIRECT_HOPS = 5

Fetch = Callable[[str], "str | None"]


class LinkError(PublisherError):
    """The link or URN names no post or comment this tool knows. Nothing was sent."""


@dataclass(frozen=True)
class Target:
    """A post (kind "post") or a comment to answer (kind "comment"). ``activity`` is the activity of the
    post: always known for a comment, and for a post only when it was given as an activity."""
    kind: str
    urn: str
    activity: str | None


def comment_urn(activity_urn: str, comment_id: str) -> str:
    return f"urn:li:comment:({activity_urn},{comment_id})"


def _post(kind: str, number: str) -> Target:
    urn = f"urn:li:{kind}:{number}"
    return Target("post", urn, urn if kind == "activity" else None)


def _comment(post: str, comment: str) -> Target:
    activity = f"urn:li:activity:{post}"
    return Target("comment", comment_urn(activity, comment), activity)


def parse_urn(urn: str) -> Target:
    if match := POST_URN.fullmatch(urn):
        return _post(*match.groups())
    if match := COMMENT_URN.fullmatch(urn):
        return _comment(*match.groups())
    if match := FSD_COMMENT_URN.fullmatch(urn):
        return _comment(match.group(2), match.group(1))
    if urn.startswith(("urn:li:comment:", "urn:li:fsd_comment:")):
        raise LinkError(f"only a comment on an activity can be answered, as in "
                        f"urn:li:comment:(urn:li:activity:<post>,<comment>): {urn}")
    raise LinkError(f"unknown URN: {urn}")


def _comment_in_query(urn: str) -> Target:
    target = parse_urn(urn)
    if target.kind != "comment":
        raise LinkError(f"not a comment: {urn}")
    return target


def _post_in_path(path: str) -> Target | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) == 3 and parts[:2] == ["feed", "update"] and POST_URN.fullmatch(parts[2]):
        return parse_urn(parts[2])
    if len(parts) == 2 and parts[0] == "posts" and (match := POSTS_SLUG.fullmatch(parts[1])):
        return _post(*match.groups())
    return None


def is_short_link(value: str) -> bool:
    return (urllib.parse.urlsplit(value.strip()).hostname or "").lower() in SHORT_HOSTS


def parse_target(value: str) -> Target:
    """The post or the comment a link or a URN names. Raises LinkError for every other form."""
    raw = value.strip()
    if urllib.parse.unquote(raw).startswith("urn:"):
        return parse_urn(urllib.parse.unquote(raw))
    if is_short_link(raw):
        raise LinkError(f"{raw} is a short link: resolve it first")
    parts = urllib.parse.urlsplit(raw)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        raise LinkError(f"not a LinkedIn link or URN: {value!r}")
    post = _post_in_path(urllib.parse.unquote(parts.path))
    if post is None:
        raise LinkError(f"no post in this link: {raw}. Known: /feed/update/<urn>/ and /posts/<slug>-share-<id>-<x>/")
    query = urllib.parse.parse_qs(parts.query)
    if {"replyUrn", "dashReplyUrn"} & query.keys():
        raise LinkError("this link points at an answer. LinkedIn has one level of answers: "
                        "use the link of the comment it answers")
    named = {_comment_in_query(urn) for key in ("commentUrn", "dashCommentUrn") for urn in query.get(key, [])}
    if not named:
        return post
    if len(named) > 1:
        raise LinkError(f"this link names two different comments: {raw}")
    comment = named.pop()
    if post.activity and post.activity != comment.activity:
        raise LinkError(f"the comment in this link belongs to {comment.activity}, not to {post.urn}")
    return comment


def resolve_short_link(value: str, fetch: Fetch) -> str:
    """Where a lnkd.in link leads. ``fetch(url)`` makes the network call and returns the address the URL
    redirects to, or None. Any other link comes back as it is, without a call."""
    link, hops = value.strip(), 0
    while is_short_link(link):
        if hops == REDIRECT_HOPS:
            raise LinkError(f"{value.strip()} still redirects after {REDIRECT_HOPS} steps")
        location = fetch(link)
        if not location:
            raise LinkError(f"{link} did not redirect. Open it and pass the link it leads to")
        link, hops = urllib.parse.urljoin(link, location), hops + 1
    return link


def follow_redirect(url: str) -> str | None:
    """The fetcher for resolve_short_link: one GET that does not follow the redirect. It sends nothing."""
    try:
        resp = requests.get(url, allow_redirects=False, timeout=15)
    except requests.RequestException as exc:
        raise LinkError(f"could not open {url}: {exc}") from exc
    return resp.headers.get("Location") if resp.is_redirect else None
