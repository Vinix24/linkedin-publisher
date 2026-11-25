"""The comment command: the digest first, then the link, then one request. Nothing leaves this machine."""
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import CREDS, Setup

from linkedin_publisher import api, claims, cli, linkcomment, links
from linkedin_publisher import postqueue as queue
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
    community_creds = {"LINKEDIN_COMMUNITY_CLIENT_ID": "x", "LINKEDIN_COMMUNITY_CLIENT_SECRET": "y"}
    monkeypatch.setattr(api, "load_credentials", lambda _cfg, app="main": community_creds if app == "community" else CREDS)
    return setup


@pytest.fixture
def lp(tmp_path, monkeypatch):
    """A community setup in which placing and reading are fakes that record what they were given. The refusal
    of a test run at the start of ``run`` is lifted, and only that one: the real place_comment, place_reply and
    read_comments keep their own, and here they are replaced. The last tests run with nothing lifted."""
    setup = community(tmp_path, monkeypatch)
    monkeypatch.setattr(linkcomment, "refuse_in_a_test_run", lambda: None)
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


def stages(setup):
    return [row["stage"] for row in receipts(setup)]


ARGS = ("--on", POST_LINK, "--text", TEXT, "--text-digest", DIGEST)


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
    assert read == [ACTIVITY] and lp.calls == []
    assert [(r["stage"], r.get("verified"), r["comment_urn"]) for r in receipts(lp)] == (
        [("comment-placed", True, urn)] if code == 0 else [])
    assert run(lp, capsys, *ARGS)[0] == (2 if code == 0 else 0)          # a found comment is never sent again


@pytest.mark.parametrize("failure, code, status", [
    (api.PublishUncertain("no readable list"), 3, "uncertain"),
    (api.PublisherError("LinkedIn refused to list the comments (403): no"), 5, "unverifiable"),
])
def test_verify_that_cannot_read_the_list(lp, capsys, monkeypatch, failure, code, status):
    monkeypatch.setattr(api, "read_comments", raising(failure))
    got, out = run(lp, capsys, "--on", COMMENT, "--text", TEXT, "--text-digest", DIGEST, "--verify")
    assert (got, out["status"]) == (code, status) and stages(lp) == []


@pytest.mark.parametrize("left", ["comment-attempt", "comment-uncertain"])
def test_after_an_unclear_attempt_only_retry_unclear_sends_again(lp, capsys, left):
    linkcomment.receipt(lp.cfg, left, links.parse_target(POST_LINK), DIGEST)     # died mid-request, or no clear answer
    code, out = run(lp, capsys, *ARGS)
    assert (code, lp.calls) == (2, []) and "--verify" in out["error"] and "--retry-unclear" in out["error"]
    assert run(lp, capsys, *ARGS, "--retry-unclear")[0] == 0 and lp.calls == [("comment", ACTIVITY, TEXT)]
    assert stages(lp) == [left, "comment-retry", "comment-attempt", "comment-placed"]
    code, out = run(lp, capsys, *ARGS, "--retry-unclear")
    assert code == 2 and "placed here already" in out["error"] and len(lp.calls) == 1


@pytest.mark.parametrize("flag", [(), ("--retry-unclear",)])
@pytest.mark.parametrize("history, plain, retry", [
    ([], 0, 2),
    (["comment-failed"], 0, 2),                                     # the token failed: nothing was sent
    (["comment-attempt", "comment-failed"], 0, 2),                  # a clear refusal: nothing was sent
    (["comment-attempt"], 2, 0), (["comment-retry"], 2, 0), (["comment-uncertain"], 2, 0),
    (["comment-attempt", "comment-uncertain", "comment-retry", "comment-failed"], 2, 0),       # retry: token failed
    (["comment-attempt", "comment-uncertain", "comment-retry", "comment-attempt", "comment-failed"], 2, 0),  # retry: 4xx
    (["comment-placed"], 2, 2), (["comment-uncertain", "comment-retry", "comment-attempt", "comment-placed"], 2, 2),
])
def test_the_whole_history_decides_not_the_latest_receipt(lp, capsys, history, plain, retry, flag):
    for stage in history:
        linkcomment.receipt(lp.cfg, stage, links.parse_target(POST_LINK), DIGEST)
    code = run(lp, capsys, *ARGS, *flag)[0]
    assert code == (retry if flag else plain) and len(lp.calls) == (code == 0)


@pytest.mark.parametrize("retry_fails", ["token", "4xx"])
def test_a_failed_retry_does_not_open_plain_sending(lp, capsys, monkeypatch, retry_fails):
    def answer(result):
        monkeypatch.setattr(api, "place_comment", lambda *_a, **_k: lp.calls.append("sent") or (
            raising(result)() if isinstance(result, Exception) else result))

    answer(api.PublishUncertain("timeout"))
    assert run(lp, capsys, *ARGS)[0] == 3
    if retry_fails == "token":
        lp.cfg.community_tokens_file.unlink()
    else:
        answer(api.PublisherError("LinkedIn refused the comment (400): no"))
    assert run(lp, capsys, *ARGS, "--retry-unclear")[0] == 2
    lp.cfg.community_tokens_file.write_text(json.dumps(TOKENS))
    answer("901")
    code, out = run(lp, capsys, *ARGS)                  # the unattended job after a person's one retry
    assert code == 2 and "may have gone through" in out["error"]
    assert lp.calls == ["sent"] * (1 if retry_fails == "token" else 2)


def test_two_unclear_runs_send_once(lp, capsys, monkeypatch):
    monkeypatch.setattr(api, "place_comment", lambda *_a, **_k: lp.calls.append("sent") or raising(
        api.PublishUncertain("LinkedIn accepted the comment but returned no id."))())
    assert [run(lp, capsys, *ARGS)[0] for _ in range(2)] == [3, 2] and lp.calls == ["sent"]


def test_a_placed_receipt_that_cannot_be_written_still_refuses_a_rerun(lp, capsys, monkeypatch):
    real = queue.write_receipt
    monkeypatch.setattr(queue, "write_receipt", lambda cfg, stage, *a, **k: (
        raising(OSError("disk full"))() if stage == "comment-placed" else real(cfg, stage, *a, **k)))
    assert run(lp, capsys, *ARGS)[0] == 0
    code, out = run(lp, capsys, *ARGS)
    assert code == 2 and "may have gone through" in out["error"] and len(lp.calls) == 1


def test_an_overlapping_run_is_kept_out_by_the_claim(lp, capsys, monkeypatch):
    inner = []

    def place_comment(*_a, **_k):
        inner.append(lp.run("comment", *ARGS))          # a second run, while the first one is sending
        lp.calls.append("sent")
        return "901"

    monkeypatch.setattr(api, "place_comment", place_comment)
    assert lp.run("comment", *ARGS) == 0 and inner == [2] and lp.calls == ["sent"]
    second, first = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    assert "holds the claim" in second["error"] and first["status"] == "placed"
    assert claims.keys(lp.cfg) == []                    # gone after the outcome: the receipts guard from here


def test_a_claim_of_a_live_process_keeps_out_a_retry_too(lp, capsys):
    ckey = linkcomment.claim_key(links.parse_target(POST_LINK), DIGEST)
    assert claims.take(lp.cfg, ckey, "another run")
    claims.claim_path(lp.cfg, ckey).write_text(json.dumps({"pid": os.getppid(), "claimed_at": "2026-01-01T00:00:00+00:00"}))
    for extra in ((), ("--retry-unclear",)):
        code, out = run(lp, capsys, *ARGS, *extra)
        assert code == 2 and "claim" in out["error"]
    assert lp.calls == [] and stages(lp) == [] and claims.exists(lp.cfg, ckey)


def dead_claim(setup, on=POST_LINK):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    ckey = linkcomment.claim_key(links.parse_target(on), DIGEST)
    assert claims.take(setup.cfg, ckey, "x")
    path = claims.claim_path(setup.cfg, ckey)
    claimed_at = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)).isoformat()
    path.write_text(json.dumps({"pid": child.pid, "file": linkcomment.receipt_key(links.parse_target(on), DIGEST),
                                "claimed_at": claimed_at}))
    return path


def test_a_dead_claim_with_nothing_sent_after_it_is_taken_over_by_a_plain_run(lp, capsys):
    path = dead_claim(lp)
    before = path.read_bytes()
    code, out = run(lp, capsys, *ARGS, "--retry-unclear")
    assert (code, lp.calls) == (2, []) and "only for an unclear" in out["error"]
    assert path.read_bytes() == before                  # a refused run leaves a claim it did not take alone
    assert run(lp, capsys, *ARGS)[0] == 0 and len(lp.calls) == 1 and not path.exists()
    assert stages(lp) == ["comment-claim-recovered", "comment-attempt", "comment-placed"]


def test_a_dead_claim_with_an_attempt_after_it_waits_for_a_person(lp, capsys):
    path = dead_claim(lp)
    linkcomment.receipt(lp.cfg, "comment-attempt", links.parse_target(POST_LINK), DIGEST)
    before = path.read_bytes()
    code, out = run(lp, capsys, *ARGS)
    assert (code, lp.calls) == (2, []) and "claim" in out["error"] and path.read_bytes() == before
    assert run(lp, capsys, *ARGS, "--retry-unclear")[0] == 0 and len(lp.calls) == 1
    assert stages(lp) == ["comment-attempt", "comment-retry", "comment-attempt", "comment-placed"]


def age(setup, path, minutes):
    path.write_text(json.dumps(json.loads(path.read_text()) | {
        "claimed_at": (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes)).isoformat()}))


def test_a_claim_that_cannot_be_removed_keeps_placed_and_is_reported_as_a_comment(lp, capsys, monkeypatch):
    real_release = claims.release
    monkeypatch.setattr(claims, "release", raising(OSError("read-only")))
    code = lp.run("comment", *ARGS)
    out, err = capsys.readouterr()
    assert code == 0 and json.loads(out)["comment_urn"] == f"urn:li:comment:({ACTIVITY},901)"
    assert "could not be removed" in err
    monkeypatch.setattr(claims, "release", real_release)
    age(lp, claims.claim_path(lp.cfg, linkcomment.claim_key(links.parse_target(POST_LINK), DIGEST)), 120)
    assert lp.run("publish", "--notify") == 0           # placed after the claim: nothing to report
    unresolved = dead_claim(lp, COMMENT)                # a claim of an answer with no receipt at all
    age(lp, unresolved, 120)
    capsys.readouterr()
    assert lp.run("publish", "--notify") == 3
    log = capsys.readouterr().out
    assert "--verify" in log and "--retry-unclear" in log and "--post --file" not in log
    assert stages(lp)[-1] == "claim-unresolved"


def test_every_failure_ends_in_the_json_line(lp, capsys, monkeypatch, tmp_path):
    lp.cfg.community_tokens_file.write_text(json.dumps({k: v for k, v in TOKENS.items() if k != "access_token"}))
    code, out = run(lp, capsys, *ARGS)
    assert code == 2 and "access_token" in out["error"]
    expired = {**TOKENS, "access_expires_at": "2000-01-01T00:00:00", "refresh_token": "r"}
    lp.cfg.community_tokens_file.write_text(json.dumps(expired))
    monkeypatch.setattr(api.requests, "post", lambda *_a, **_k: SimpleNamespace(status_code=200, json=dict))
    code, out = run(lp, capsys, *ARGS)                  # a refresh answer without an access token
    assert code == 2 and "access_token" in out["error"]
    assert json.loads(lp.cfg.community_tokens_file.read_text()) == expired
    (lp.cfg.receipts_dir / "2026-01-01.ndjson").write_bytes(b"\xff\xfe not UTF-8\n")
    code, out = run(lp, capsys, *ARGS)
    assert (code, out["status"]) == (1, "error") and "UnicodeDecodeError" in out["error"] and lp.calls == []
    for config in (tmp_path / "missing.toml", tmp_path):           # a folder is an OSError, not a ConfigError
        assert cli.main(["--config", str(config), "comment", *ARGS]) == 1
        assert json.loads(capsys.readouterr().out)["status"] == "error"


@pytest.mark.parametrize("extra", [(), ("--verify",), ("--retry-unclear",)])
def test_a_test_run_is_refused_before_any_token_work_or_receipt(tmp_path, capsys, monkeypatch, extra):
    """Nothing lifted. The token file is not read, refreshed or written, and no receipt is written."""
    setup = community(tmp_path, monkeypatch)
    before = setup.cfg.community_tokens_file.read_bytes()
    for name in ("community_problem", "load_credentials", "read_tokens", "ensure_access_token", "write_tokens"):
        monkeypatch.setattr(api, name, lambda *_a, **_k: pytest.fail("token work in a test run"))
    code, out = run(setup, capsys, "--on", COMMENT_LINK, "--text", TEXT, "--text-digest", DIGEST, *extra)
    assert (code, out["status"]) == (2, "refused") and "from a test run" in out["error"]
    assert setup.cfg.community_tokens_file.read_bytes() == before and not setup.cfg.receipts_dir.exists()


@pytest.mark.parametrize("on", [POST_LINK, COMMENT_LINK])
def test_with_the_first_refusal_lifted_place_comment_and_place_reply_still_refuse(tmp_path, capsys, monkeypatch, on):
    """The real place_comment and place_reply, the real token file: only the network is replaced, by a trap."""
    setup = community(tmp_path, monkeypatch)
    monkeypatch.setattr(linkcomment, "refuse_in_a_test_run", lambda: None)
    monkeypatch.setattr(api.requests, "post", lambda *_a, **_k: pytest.fail("a request was sent"))
    code, out = run(setup, capsys, "--on", on, "--text", TEXT, "--text-digest", DIGEST)
    assert (code, out["status"]) == (2, "refused") and "from a test run" in out["error"]
    assert stages(setup) == ["comment-attempt", "comment-failed"]


def test_the_exit_codes_are_the_ones_in_the_readme():
    assert linkcomment.EXIT_CODES == {"placed": 0, "found": 0, "dry-run": 0, "error": 1, "refused": 2, "uncertain": 3,
                                      "not-found": 4, "unverifiable": 5}
    section = README.split("## Comment from a link\n", 1)[1].split("\n## ", 1)[0]
    rows = dict(re.findall(r"^\| ([0-9]) \| (.+) \|$", section, flags=re.MULTILINE))
    assert sorted(map(int, rows)) == sorted(set(linkcomment.EXIT_CODES.values()))
    assert "placed" in rows["0"] and "found" in rows["0"] and rows["2"].startswith("refused")
    assert rows["3"].startswith("unclear") and rows["4"].endswith("not found")
    assert rows["1"].startswith("error") and rows["5"].startswith("unverifiable")
