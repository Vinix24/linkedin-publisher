"""The first comment: a sidecar file, the approval hash, the payload, the receipts and the retry."""
import datetime as dt
import hashlib
import json
import urllib.parse
from types import SimpleNamespace

import pytest
from conftest import CREDS, TOKENS, Setup

from linkedin_publisher import api, approval, cli, littletext
from linkedin_publisher import postqueue as queue

POST_URN = "urn:li:share:1"
COMMENT = "Link to the source in the first comment."
COMMUNITY_TOKENS = {"access_token": "c", "author_urn": "urn:li:person:x", "access_expires_at": "2999-01-01T00:00:00"}
EVENING_BEFORE = dt.datetime(2026, 9, 22, 17, 0)


def answer(status, headers=None, text=""):
    return SimpleNamespace(status_code=status, headers=headers or {}, text=text)


@pytest.fixture
def comments(tmp_path, monkeypatch):
    """A setup with a community token and a fake LinkedIn. `sent` lists the comments placed."""
    setup = Setup(tmp_path, '[community]\ntokens_file = "community.json"\n')
    setup.install_fake_linkedin(monkeypatch)
    setup.cfg.community_tokens_file.write_text(json.dumps(COMMUNITY_TOKENS))
    setup.sent = []
    monkeypatch.setattr(api, "load_credentials", lambda _cfg, app="main": CREDS)
    monkeypatch.setattr(api, "read_tokens", lambda _cfg, app="main": COMMUNITY_TOKENS if app == "community" else TOKENS)
    monkeypatch.setattr(api, "ensure_access_token", lambda _cfg, _creds, tokens, *_a: tokens)
    monkeypatch.setattr(api, "place_comment", lambda _c, _cr, _t, urn, text, url=None:
                        setup.sent.append((urn, text)) or "777")
    return setup


def raising(exc):
    return lambda *_a, **_k: (_ for _ in ()).throw(exc)


def with_comment(setup, text=COMMENT, name="a.md"):
    path = setup.post(name)
    queue.comment_path_for(path).write_text(text + "\n", encoding="utf-8")
    return path


def comment_stages(setup):
    return [s for s in setup.receipt_stages() if s.startswith("comment-")]


def published_meta(setup):
    return queue.read_post(setup.cfg.published_dir / "a.md").meta


# ------------------------------------------------------------------ the sidecar and the approval hash

def test_the_comment_file_is_not_a_post_and_belongs_to_its_post(comments):
    path = with_comment(comments)
    assert queue.queue_files(comments.cfg) == [path] and queue.read_post(path).comment == COMMENT


def test_a_post_without_a_comment_hashes_exactly_as_before(comments):
    """The key of a plain text post, built by hand with the v1 recipe, must not move."""
    path = comments.post()
    digest = hashlib.sha256(approval.KEY_VERSION)
    for label, data in (("text", "Hello world."), ("commentary", littletext.prepare("Hello world.")),
                        ("format", "text")):
        raw = data.encode()
        digest.update(f"{label}:{len(raw)}\n".encode() + raw + b"\n")
    assert approval.approval_key(queue.read_post(path), comments.cfg) == digest.hexdigest()


def test_the_comment_is_part_of_the_approval(comments):
    path = with_comment(comments)
    approval.approve(comments.cfg, path, "Test Person")
    assert approval.check(queue.read_post(path), comments.cfg)[0] == approval.APPROVED
    queue.comment_path_for(path).write_text("Another comment\n", encoding="utf-8")
    assert approval.check(queue.read_post(path), comments.cfg)[0] == approval.MISMATCH
    queue.comment_path_for(path).unlink()
    assert approval.check(queue.read_post(path), comments.cfg)[0] == approval.MISMATCH


# ------------------------------------------------------------------ the request

def test_payload_and_url():
    assert api.build_comment_payload("urn:li:person:x", POST_URN, "a [b] @c *d*") == {
        "actor": "urn:li:person:x", "object": POST_URN, "message": {"text": "a [b] @c *d*"}}
    assert api.comments_url(POST_URN) == "https://api.linkedin.com/rest/socialActions/urn%3Ali%3Ashare%3A1/comments"


def test_place_comment_sends_headers_and_reads_the_id(cfg, monkeypatch):
    seen = {}

    def post(url, headers, json, timeout):
        seen.update(url=url, headers=headers, body=json)
        return answer(201, {"x-restli-id": "99"})

    monkeypatch.setattr(api.requests, "post", post)
    assert api.place_comment(cfg, CREDS, COMMUNITY_TOKENS, POST_URN, "Hi") == "99"
    assert urllib.parse.unquote(seen["url"]).endswith("urn:li:share:1/comments")
    assert seen["headers"]["LinkedIn-Version"] == cfg.api_version
    assert seen["headers"]["X-Restli-Protocol-Version"] == "2.0.0"
    assert seen["headers"]["Authorization"] == "Bearer c"
    assert seen["body"]["actor"] == "urn:li:person:x"


@pytest.mark.parametrize("reply, uncertain", [
    (answer(429, text="throttled"), False), (answer(403, text="no"), False),
    (answer(503), True), (answer(201), True),
])
def test_place_comment_answers(cfg, monkeypatch, reply, uncertain):
    monkeypatch.setattr(api.requests, "post", lambda *_a, **_k: reply)
    with pytest.raises(api.PublisherError) as caught:
        api.place_comment(cfg, CREDS, COMMUNITY_TOKENS, POST_URN, "Hi")
    assert isinstance(caught.value, api.PublishUncertain) is uncertain


# ------------------------------------------------------------------ the flow after the post

def test_the_comment_is_placed_after_the_post_and_recorded(comments):
    path = with_comment(comments)
    assert comments.run("publish", "--post", "--file", str(path)) == 0
    assert comments.sent == [(POST_URN, COMMENT)]
    assert comments.receipt_stages() == ["attempt", "placed", "comment-attempt", "comment-placed"]
    assert (comments.cfg.published_dir / "a.comment.md").exists() and not queue.comment_path_for(path).exists()
    meta = published_meta(comments)
    assert meta["comment_status"] == "placed" and meta["comment_urn"] == "777" and meta["status"] == "published"


def test_a_failed_comment_leaves_the_post_placed_and_tells_a_person(comments, monkeypatch):
    monkeypatch.setattr(api, "place_comment", raising(api.PublisherError("LinkedIn refused the comment (403): no")))
    assert comments.run("publish", "--post", "--file", str(with_comment(comments))) == 0
    assert comment_stages(comments) == ["comment-attempt", "comment-failed"]
    meta = published_meta(comments)
    assert meta["status"] == "published" and meta["comment_status"] == "failed" and "403" in meta["comment_error"]
    assert comments.titles() == ["LinkedIn first comment not placed", "LinkedIn post is live"]


def test_an_uncertain_comment_is_recorded_and_never_retried_by_itself(comments, monkeypatch):
    monkeypatch.setattr(api, "place_comment", raising(api.PublishUncertain("timeout")))
    assert comments.run("publish", "--post", "--file", str(with_comment(comments))) == 0
    assert comment_stages(comments) == ["comment-attempt", "comment-uncertain"]
    assert published_meta(comments)["comment_status"] == "uncertain"
    assert "LinkedIn first comment: unclear if it went out" in comments.titles()
    assert comment_stages(comments) == ["comment-attempt", "comment-uncertain"]


def test_without_a_community_token_the_post_goes_out_and_the_comment_is_not_placed(comments):
    comments.cfg.community_tokens_file.unlink()
    assert comments.run("publish", "--post", "--file", str(with_comment(comments))) == 0
    assert comments.receipt_stages() == ["attempt", "placed", "comment-failed"]
    meta = published_meta(comments)
    assert meta["comment_status"] == "failed" and "no community token" in meta["comment_error"]


# ------------------------------------------------------------------ the manual retry

def test_comment_retry_places_a_failed_comment_and_then_refuses(comments, monkeypatch):
    monkeypatch.setattr(api, "place_comment", raising(api.PublisherError("503")))
    comments.run("publish", "--post", "--file", str(with_comment(comments)))
    published = comments.cfg.published_dir / "a.md"
    monkeypatch.setattr(api, "place_comment", lambda _c, _cr, _t, urn, text, url=None:
                        comments.sent.append((urn, text)) or "888")
    assert comments.run("publish", "--comment-retry", "--file", str(published)) == 0
    assert comments.sent == [(POST_URN, COMMENT)]
    assert comment_stages(comments)[-1] == "comment-placed"
    assert queue.read_post(published).meta["comment_status"] == "placed"
    assert comments.run("publish", "--comment-retry", "--file", str(published)) == 1
    assert len(comments.sent) == 1


def test_comment_retry_needs_a_placed_post_and_a_comment(comments, capsys):
    path = with_comment(comments)
    assert comments.run("publish", "--comment-retry", "--file", str(path)) == 1
    assert "no receipt shows this post was placed" in capsys.readouterr().err


# ------------------------------------------------------------------ the precheck

def test_precheck_reports_a_comment_without_a_community_token(comments, capsys):
    comments.cfg.community_tokens_file.unlink()
    with_comment(comments)
    assert cli.cmd_precheck(comments.cfg, EVENING_BEFORE, 1, True) == 3
    problem = json.loads(capsys.readouterr().out)["posts"][0]["problems"][0]
    assert problem["kind"] == "comment" and "no community token" in problem["message"]
