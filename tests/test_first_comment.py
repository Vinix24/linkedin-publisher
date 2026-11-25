"""The first comment: a sidecar file, the approval hash, the payload, the receipts and the retry."""
import datetime as dt
import hashlib
import json
import urllib.parse
from types import SimpleNamespace

import pytest
from conftest import CREDS, TOKENS, Setup

from linkedin_publisher import api, approval, claims, cli, firstcomment, littletext
from linkedin_publisher import postqueue as queue

POST_URN = "urn:li:share:1"
COMMENT = "Link to the source in the first comment."
COMMUNITY_TOKENS = {"access_token": "c", "author_urn": "urn:li:person:x", "access_expires_at": "2999-01-01T00:00:00"}
EVENING_BEFORE = dt.datetime(2026, 9, 22, 17, 0)


def answer(status, headers=None, text=""):
    return SimpleNamespace(status_code=status, headers=headers or {}, text=text)


@pytest.fixture
def comments(tmp_path, monkeypatch):
    return community_setup(tmp_path, monkeypatch)


def community_setup(tmp_path, monkeypatch, extra=""):
    """A setup with a community token and a fake LinkedIn. `sent` lists the comments placed."""
    setup = Setup(tmp_path, '[community]\ntokens_file = "community.json"\n' + extra)
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


def test_place_comment_sends_headers_and_reads_the_id(cfg, monkeypatch, allow_linkedin_send_under_test):
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
def test_place_comment_answers(cfg, monkeypatch, reply, uncertain, allow_linkedin_send_under_test):
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


def age_claim(setup, ckey, minutes):
    path = claims.claim_path(setup.cfg, ckey)
    row = json.loads(path.read_text())
    path.write_text(json.dumps(row | {"claimed_at": (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes)).isoformat()}))


def test_the_claim_of_a_placed_first_comment_is_not_reported_as_unresolved(comments, monkeypatch, capsys):
    """Existing behaviour that was wrong: the claim stays after a placed comment, and the next scheduled run
    reported it as a post claimed without an outcome, with the advice to post it again."""
    monkeypatch.setattr(api, "place_comment", raising(api.PublisherError("503")))
    comments.run("publish", "--post", "--file", str(with_comment(comments)))
    published = comments.cfg.published_dir / "a.md"
    monkeypatch.setattr(api, "place_comment", lambda *_a, **_k: "888")
    assert comments.run("publish", "--comment-retry", "--file", str(published)) == 0
    ckey = firstcomment.claim_key(queue.post_key(queue.read_post(published), comments.cfg))
    assert claims.exists(comments.cfg, ckey)
    age_claim(comments, ckey, 120)
    capsys.readouterr()
    assert comments.run("publish", "--notify") == 0
    assert "claim-unresolved" not in comments.receipt_stages() and "claimed" not in capsys.readouterr().out
    assert not any("claimed but no outcome" in title for title in comments.titles())


def test_a_first_comment_claim_without_an_outcome_gets_the_advice_of_a_comment(comments, capsys):
    ckey = firstcomment.claim_key("c" * 64)
    assert claims.take(comments.cfg, ckey, "a.md#comment")
    age_claim(comments, ckey, 120)
    assert comments.run("publish", "--notify") == 3
    out = capsys.readouterr().out
    assert "--comment-retry" in out and "--retry" not in out.replace("--comment-retry", "")
    assert comments.titles() == ["LinkedIn first comment: claimed but no outcome"]


def test_a_receipt_time_without_a_zone_is_utc():
    moment = dt.datetime(2026, 11, 25, 12, 0, tzinfo=dt.UTC)
    assert queue.written_since({"timestamp": "2026-11-25T12:00:01"}, moment)
    assert not queue.written_since({"timestamp": "2026-11-25T11:59:59"}, moment)
    assert queue.written_since({"timestamp": "2026-11-25T13:30:00+01:00"}, moment)
    assert not queue.written_since({"timestamp": "not a time"}, moment) and not queue.written_since({}, moment)


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


# ------------------------------------------------------------------ review round: what a comment must survive

def failed_meta(setup, name="a.md"):
    return queue.read_post(setup.cfg.published_dir / name).meta


def test_a_network_error_while_refreshing_the_community_token_fails_only_the_comment(comments, monkeypatch):
    def refresh(_cfg, _creds, tokens, *_a):
        if tokens is COMMUNITY_TOKENS:
            raise api.requests.ConnectionError("no route")
        return tokens

    monkeypatch.setattr(api, "ensure_access_token", refresh)
    assert comments.run("publish", "--post", "--file", str(with_comment(comments))) == 0
    assert comments.receipt_stages() == ["attempt", "placed", "comment-failed"]
    assert published_meta(comments)["comment_status"] == "failed"
    assert "LinkedIn first comment not placed" in comments.titles()


def test_a_broken_community_token_file_fails_only_the_comment_and_is_a_precheck_problem(comments, monkeypatch, capsys):
    comments.cfg.community_tokens_file.write_text("")
    monkeypatch.undo()
    real_read = api.read_tokens
    monkeypatch.setattr(cli, "notify", lambda title, message: None)
    path = with_comment(comments)
    assert cli.cmd_precheck(comments.cfg, EVENING_BEFORE, 1, True) == 3
    assert "cannot be read" in json.loads(capsys.readouterr().out)["posts"][0]["problems"][0]["message"]
    comments.install_fake_linkedin(monkeypatch)
    monkeypatch.setattr(api, "load_credentials", lambda _cfg, app="main": CREDS)
    monkeypatch.setattr(api, "read_tokens", lambda cfg, app="main": real_read(cfg, app) if app == "community" else TOKENS)
    assert comments.run("publish", "--post", "--file", str(path)) == 0
    assert comment_stages(comments) == ["comment-failed"]


def test_a_status_that_cannot_be_written_does_not_undo_a_placed_comment(comments, monkeypatch):
    monkeypatch.setattr(firstcomment_module().frontmatter, "set_fields", raising(OSError("iCloud said no")))
    assert comments.run("publish", "--post", "--file", str(with_comment(comments))) == 0
    assert comment_stages(comments) == ["comment-attempt", "comment-placed"]
    assert comments.sent == [(POST_URN, COMMENT)]


def firstcomment_module():
    from linkedin_publisher import firstcomment
    return firstcomment


def test_the_comment_follows_the_post_when_the_name_is_taken(comments, monkeypatch):
    comments.cfg.published_dir.mkdir(parents=True)
    (comments.cfg.published_dir / "a.md").write_text("---\nstatus: published\n---\n\nOld post.\n", encoding="utf-8")
    monkeypatch.setattr(api, "place_comment", raising(api.PublisherError("503")))
    assert comments.run("publish", "--post", "--file", str(with_comment(comments))) == 0
    published = comments.cfg.published_dir
    assert (published / "a-2.md").exists() and (published / "a-2.comment.md").exists()
    assert not (published / "a.comment.md").exists()
    monkeypatch.setattr(api, "place_comment", lambda _c, _cr, _t, urn, text, url=None:
                        comments.sent.append((urn, text)) or "888")
    assert comments.run("publish", "--comment-retry", "--file", str(published / "a-2.md")) == 0
    assert comments.sent == [(POST_URN, COMMENT)]


def test_a_comment_file_left_behind_in_published_is_not_taken_over(comments):
    published = comments.cfg.published_dir
    published.mkdir(parents=True)
    (published / "a.md").write_text("---\nstatus: published\n---\n\nOld post.\n", encoding="utf-8")
    (published / "a-2.comment.md").write_text("Someone else's comment\n", encoding="utf-8")
    assert comments.run("publish", "--post", "--file", str(with_comment(comments))) == 0
    assert (published / "a-3.md").exists()
    assert (published / "a-3.comment.md").read_text(encoding="utf-8").strip() == COMMENT
    assert (published / "a-2.comment.md").read_text(encoding="utf-8").strip() == "Someone else's comment"


@pytest.mark.parametrize("tokens, problem", [
    ({"access_token": "c", "author_urn": "x", "access_expires_at": "2026-09-22T23:00:00"}, "expires within a day"),
    ({"access_token": "c", "author_urn": "x", "refresh_token": "r", "access_expires_at": "2026-09-01T00:00:00",
      "refresh_expires_at": "2026-09-20T00:00:00"}, "refresh token has expired"),
])
def test_the_community_check_matches_what_placing_the_comment_does(tmp_path, tokens, problem):
    setup = Setup(tmp_path, '[community]\ntokens_file = "community.json"\n')
    setup.cfg.community_tokens_file.write_text(json.dumps(tokens))
    assert problem in api.community_problem(setup.cfg, EVENING_BEFORE)


def test_a_refresh_token_that_is_still_valid_makes_an_expired_access_token_fine(tmp_path):
    setup = Setup(tmp_path, '[community]\ntokens_file = "community.json"\n')
    setup.cfg.community_tokens_file.write_text(json.dumps({
        "access_token": "c", "author_urn": "x", "refresh_token": "r", "access_expires_at": "2026-09-01T00:00:00",
        "refresh_expires_at": "2027-01-01T00:00:00"}))
    assert api.community_problem(setup.cfg, EVENING_BEFORE) is None


def test_comment_retry_refuses_an_unapproved_comment_when_approval_is_required(tmp_path, monkeypatch):
    setup = community_setup(tmp_path, monkeypatch, "[approval]\nrequired = true\n")
    monkeypatch.setattr(api, "place_comment", raising(api.PublisherError("503")))
    path = with_comment(setup)
    approval.approve(setup.cfg, path, "Test Person")
    assert setup.run("publish", "--post", "--file", str(path)) == 0
    published = setup.cfg.published_dir / "a.md"
    queue.comment_path_for(published).write_text("A changed comment\n", encoding="utf-8")
    monkeypatch.setattr(api, "place_comment", lambda _c, _cr, _t, urn, text, url=None:
                        setup.sent.append((urn, text)) or "888")
    assert setup.run("publish", "--comment-retry", "--file", str(published)) == 1
    assert setup.sent == []
    queue.comment_path_for(published).write_text(COMMENT + "\n", encoding="utf-8")      # the approved version again
    assert setup.run("publish", "--comment-retry", "--file", str(published)) == 0
    assert setup.sent == [(POST_URN, COMMENT)]


def failed_post(comments, monkeypatch):
    monkeypatch.setattr(api, "place_comment", raising(api.PublisherError("503")))
    comments.run("publish", "--post", "--file", str(with_comment(comments)))
    monkeypatch.setattr(api, "place_comment", lambda _c, _cr, _t, urn, text, url=None:
                        comments.sent.append((urn, text)) or "888")
    return comments.cfg.published_dir / "a.md"


def test_comment_retry_takes_the_claim_of_the_comment_and_a_second_one_stays_out(comments, monkeypatch, capsys):
    from linkedin_publisher import claims, firstcomment
    published = failed_post(comments, monkeypatch)
    key = queue.content_key(queue.post_text(queue.read_post(published).body, comments.cfg.notes_headings))
    assert claims.take(comments.cfg, firstcomment.claim_key(key), "a.md#comment")      # a sweep holds it
    claims.claim_path(comments.cfg, firstcomment.claim_key(key)).write_text(
        json.dumps({"pid": __import__("os").getppid(), "file": "a.md#comment",
                    "claimed_at": dt.datetime.now(dt.UTC).isoformat()}))
    assert comments.run("publish", "--comment-retry", "--file", str(published)) == 1
    assert comments.sent == [] and "still holds the claim" in capsys.readouterr().err


def test_comment_retry_leaves_a_claim_after_placing_and_releases_it_after_a_failure(comments, monkeypatch):
    from linkedin_publisher import claims, firstcomment
    published = failed_post(comments, monkeypatch)
    key = queue.content_key(queue.post_text(queue.read_post(published).body, comments.cfg.notes_headings))
    ckey = firstcomment.claim_key(key)
    monkeypatch.setattr(api, "place_comment", raising(api.PublisherError("503 again")))
    assert comments.run("publish", "--comment-retry", "--file", str(published)) == 1
    assert not claims.exists(comments.cfg, ckey)
    monkeypatch.setattr(api, "place_comment", lambda _c, _cr, _t, urn, text, url=None:
                        comments.sent.append((urn, text)) or "888")
    assert comments.run("publish", "--comment-retry", "--file", str(published)) == 0
    assert claims.exists(comments.cfg, ckey)


def test_the_comment_file_is_refused_as_a_post_and_for_approval(comments, capsys):
    path = with_comment(comments)
    sidecar = queue.comment_path_for(path)
    assert comments.run("publish", "--post", "--file", str(sidecar)) == 1
    assert "first comment of a post, not a post" in capsys.readouterr().err
    assert comments.run("approve", "--file", str(sidecar), "--by", "Test Person") == 1
    assert comments.sends == [] and comments.receipt_stages() == []
    assert "approved_key" not in sidecar.read_text(encoding="utf-8")


def test_an_unreadable_comment_file_does_not_stop_the_measure_run(comments):
    path = comments.post("a.md", {"measure": "count"})
    queue.comment_path_for(path).mkdir()          # exists, but reading it raises an OSError
    comments.post("b.md")
    lines = []
    assert cli.measure_post(comments.cfg, path, EVENING_BEFORE, dt.date(2026, 9, 24), set(), lines.append) == 0
    assert any("first comment" in line for line in lines)
    assert cli.cmd_measure(comments.cfg, EVENING_BEFORE, 1) == 0
