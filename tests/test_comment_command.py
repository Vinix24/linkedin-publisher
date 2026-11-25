"""The comment command: the digest first, then the link, then one request. Nothing leaves this machine."""
import json
import re
from pathlib import Path

import pytest
from conftest import CREDS, Setup

from linkedin_publisher import api, linkcomment, links
from linkedin_publisher.textdigest import text_digest

P, C = "7400000000000000001", "7400000000000000002"
ACTIVITY = f"urn:li:activity:{P}"
COMMENT = f"urn:li:comment:({ACTIVITY},{C})"
POST_LINK = f"https://www.linkedin.com/feed/update/{ACTIVITY}/"
COMMENT_LINK = f"{POST_LINK}?commentUrn=urn%3Ali%3Acomment%3A%28activity%3A{P}%2C{C}%29"
ME = "urn:li:person:me"
TEXT = "Dank je, goed punt."
DIGEST = text_digest(TEXT)
TOKENS = {"access_token": "c", "author_urn": ME, "access_expires_at": "2999-01-01T00:00:00"}
README = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")


def community(tmp_path, monkeypatch):
    setup = Setup(tmp_path, '[community]\ntokens_file = "community.json"\n', hook=False)
    setup.cfg.community_tokens_file.write_text(json.dumps(TOKENS))
    monkeypatch.setattr(api, "load_credentials", lambda _cfg, app="main": CREDS)
    return setup


@pytest.fixture
def lp(tmp_path, monkeypatch):
    """A community setup in which placing and reading are fakes that record what they were given."""
    setup = community(tmp_path, monkeypatch)
    setup.calls = []
    monkeypatch.setattr(api, "place_comment", lambda _c, _cr, _t, urn, text, url=None:
                        setup.calls.append(("comment", urn, text)) or "901")
    monkeypatch.setattr(api, "place_reply", lambda _c, _cr, _t, activity, parent, text:
                        setup.calls.append(("reply", activity, parent, text)) or "902")
    monkeypatch.setattr(api, "read_comments", lambda *_a: pytest.fail("nothing is read back"))
    return setup


def run(setup, capsys, *args):
    code = setup.run("comment", *args)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1, lines          # stdout is one JSON line and nothing else
    return code, json.loads(lines[0])


def receipts(setup):
    return [json.loads(line) for f in sorted(setup.cfg.receipts_dir.glob("*.ndjson")) for line in f.read_text().splitlines()]


def raising(exc):
    return lambda *_a, **_k: (_ for _ in ()).throw(exc)


def test_a_comment_on_a_post_goes_out_as_the_normalized_text(lp, capsys):
    code, out = run(lp, capsys, "--on", POST_LINK, "--text", f"  {TEXT}\r\n", "--text-digest", DIGEST.upper())
    urn = f"urn:li:comment:({ACTIVITY},901)"
    assert code == 0 and out == {"status": "placed", "comment_urn": urn, "target": ACTIVITY, "digest": DIGEST}
    assert lp.calls == [("comment", ACTIVITY, TEXT)]
    rows = receipts(lp)
    assert [row["stage"] for row in rows] == ["comment-attempt", "comment-placed"]
    assert {k: rows[-1][k] for k in ("digest", "target", "comment_urn", "post_urn")} == {
        "digest": DIGEST, "target": ACTIVITY, "comment_urn": urn, "post_urn": ACTIVITY}
    assert TEXT not in "".join(f.read_text() for f in lp.cfg.receipts_dir.glob("*.ndjson"))


@pytest.mark.parametrize("target", [("--on", COMMENT_LINK), ("--on", POST_LINK, "--reply-to", f"urn:li:fsd_comment:({C},{ACTIVITY})"),
                                    ("--on", COMMENT, "--reply-to", COMMENT_LINK)])
def test_a_comment_as_target_gets_an_answer_under_it(lp, capsys, tmp_path, target):
    (tmp_path / "answer.txt").write_text(TEXT + "\n", encoding="utf-8")
    code, out = run(lp, capsys, *target, "--text-file", str(tmp_path / "answer.txt"), "--text-digest", DIGEST)
    assert (code, out["target"], out["comment_urn"]) == (0, COMMENT, f"urn:li:comment:({ACTIVITY},902)")
    assert lp.calls == [("reply", ACTIVITY, COMMENT, TEXT)]


@pytest.mark.parametrize("args, says", [
    (("--on", POST_LINK, "--text", TEXT), "--text-digest is missing"),
    (("--on", POST_LINK, "--text", TEXT + "!", "--text-digest", DIGEST), "not the approved"),
    (("--on", "https://lnkd.in/abc", "--text", TEXT + "!", "--text-digest", DIGEST), "not the approved"),
    (("--on", POST_LINK, "--text", " \r\n ", "--text-digest", DIGEST), "the text is empty"),
    (("--on", POST_LINK, "--text-file", "missing.txt", "--text-digest", DIGEST), "cannot read"),
    (("--on", "https://www.linkedin.com/in/someone/", "--text", TEXT, "--text-digest", DIGEST), "no post in this link"),
    (("--on", POST_LINK, "--reply-to", POST_LINK, "--text", TEXT, "--text-digest", DIGEST), "names a post"),
    (("--on", f"urn:li:ugcPost:{P}", "--reply-to", COMMENT, "--text", TEXT, "--text-digest", DIGEST), "is not under"),
    (("--on", "urn:li:activity:9", "--reply-to", COMMENT, "--text", TEXT, "--text-digest", DIGEST), "is not under"),
])
def test_refused_before_the_token_or_the_network(lp, capsys, monkeypatch, args, says):
    monkeypatch.setattr(api, "community_problem", lambda *_a: pytest.fail("the token was looked at"))
    monkeypatch.setattr(links, "follow_redirect", lambda url: pytest.fail("a short link was followed"))
    code, out = run(lp, capsys, *args)
    assert (code, out["status"]) == (2, "refused") and says in out["error"] and lp.calls == []
    assert not lp.cfg.receipts_dir.exists()


def test_a_short_link_is_followed_before_it_is_read(lp, capsys, monkeypatch):
    monkeypatch.setattr(links, "follow_redirect", lambda url: COMMENT_LINK if url == "https://lnkd.in/abc" else None)
    assert run(lp, capsys, "--on", "https://lnkd.in/abc", "--text", TEXT, "--text-digest", DIGEST)[0] == 0
    assert lp.calls == [("reply", ACTIVITY, COMMENT, TEXT)]


@pytest.mark.parametrize("failure, code, stage", [
    (api.PublishUncertain("No clear answer from LinkedIn (timeout)."), 3, "comment-uncertain"),
    (api.PublisherError("LinkedIn refused the comment (403): no"), 2, "comment-failed"),
])
def test_an_answer_that_is_not_a_success_is_sent_once(lp, capsys, monkeypatch, failure, code, stage):
    monkeypatch.setattr(api, "place_comment", lambda *_a, **_k: lp.calls.append("sent") or raising(failure)())
    got, out = run(lp, capsys, "--on", POST_LINK, "--text", TEXT, "--text-digest", DIGEST)
    assert (got, out["comment_urn"], lp.calls) == (code, None, ["sent"])
    assert [row["stage"] for row in receipts(lp)] == ["comment-attempt", stage]
    assert ("--verify" in out["error"]) is (code == 3)


def test_without_a_usable_community_token_nothing_is_sent(lp, capsys):
    lp.cfg.community_tokens_file.unlink()
    code, out = run(lp, capsys, "--on", POST_LINK, "--text", TEXT, "--text-digest", DIGEST)
    assert (code, lp.calls) == (2, []) and "no community token" in out["error"]
    assert [row["stage"] for row in receipts(lp)] == ["comment-failed"]


def test_the_same_text_is_placed_once_on_a_target(lp, capsys):
    args = ("--text", TEXT, "--text-digest", DIGEST)
    assert run(lp, capsys, "--on", POST_LINK, *args)[0] == 0
    code, out = run(lp, capsys, "--on", ACTIVITY, *args)
    assert code == 2 and "placed here already" in out["error"]
    assert run(lp, capsys, "--on", COMMENT, *args)[0] == 0
    assert [call[0] for call in lp.calls] == ["comment", "reply"]


def test_a_dry_run_shows_the_request_and_makes_none(lp, capsys, monkeypatch):
    monkeypatch.setattr(api, "ensure_access_token", lambda *_a: pytest.fail("a dry run refreshes nothing"))
    code, out = run(lp, capsys, "--on", COMMENT_LINK, "--text", TEXT, "--text-digest", DIGEST, "--dry-run")
    assert code == 0 and out == {"status": "dry-run", "comment_urn": None, "target": COMMENT, "digest": DIGEST,
                                 "url": api.comments_url(COMMENT),
                                 "payload": api.build_reply_payload(ME, ACTIVITY, COMMENT, TEXT)}
    code, out = run(lp, capsys, "--on", "https://lnkd.in/abc", "--text", TEXT, "--text-digest", DIGEST, "--dry-run")
    assert code == 2 and "no network call" in out["error"]
    assert lp.calls == [] and not lp.cfg.receipts_dir.exists()


MINE = {"actor": ME, "message": {"text": TEXT + "\r\n"}, "commentUrn": f"urn:li:comment:({ACTIVITY},5)"}


@pytest.mark.parametrize("elements, code, urn", [
    ([{"actor": "urn:li:person:other", "message": {"text": TEXT}}, MINE], 0, MINE["commentUrn"]),
    ([{"actor": ME, "message": {"text": TEXT}, "id": "6"}], 0, f"urn:li:comment:({ACTIVITY},6)"),
    ([{"actor": "urn:li:person:other", "message": {"text": TEXT}}, {"actor": ME, "message": {"text": "Iets anders."}},
      "not a comment", {"actor": ME}, {"actor": ME, "message": "x"}], 4, None),
    ([], 4, None),
])
def test_verify_reads_back_and_sends_nothing(lp, capsys, monkeypatch, elements, code, urn):
    read = []
    monkeypatch.setattr(api, "read_comments", lambda _c, _cr, _t, target: read.append(target) or elements)
    got, out = run(lp, capsys, "--on", POST_LINK, "--text", TEXT, "--text-digest", DIGEST, "--verify")
    assert (got, out["status"], out["comment_urn"]) == (code, "found" if code == 0 else "not-found", urn)
    assert read == [ACTIVITY] and lp.calls == [] and not lp.cfg.receipts_dir.exists()


def test_verify_that_cannot_read_the_list_is_unclear(lp, capsys, monkeypatch):
    monkeypatch.setattr(api, "read_comments", raising(api.PublishUncertain("no readable list")))
    assert run(lp, capsys, "--on", COMMENT, "--text", TEXT, "--text-digest", DIGEST, "--verify")[0] == 3


@pytest.mark.parametrize("on", [POST_LINK, COMMENT_LINK])
def test_without_dry_run_a_test_run_sends_nothing(tmp_path, capsys, monkeypatch, on):
    """The real place_comment and place_reply, the real token file: only the network is replaced, by a trap."""
    setup = community(tmp_path, monkeypatch)
    monkeypatch.setattr(api.requests, "post", lambda *_a, **_k: pytest.fail("a request was sent"))
    code, out = run(setup, capsys, "--on", on, "--text", TEXT, "--text-digest", DIGEST)
    assert (code, out["status"]) == (2, "refused") and "from a test run" in out["error"]
    assert [row["stage"] for row in receipts(setup)] == ["comment-attempt", "comment-failed"]


def test_the_exit_codes_are_the_ones_in_the_readme():
    assert linkcomment.EXIT_CODES == {"placed": 0, "found": 0, "dry-run": 0, "refused": 2, "uncertain": 3, "not-found": 4}
    section = README.split("## Comment from a link\n", 1)[1].split("\n## ", 1)[0]
    rows = dict(re.findall(r"^\| ([0-9]) \| (.+) \|$", section, flags=re.MULTILINE))
    assert sorted(map(int, rows)) == sorted(set(linkcomment.EXIT_CODES.values()))
    assert "placed" in rows["0"] and "found" in rows["0"] and rows["2"].startswith("refused")
    assert rows["3"].startswith("unclear") and rows["4"].endswith("not found")
