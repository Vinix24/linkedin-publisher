"""A delayed first comment: the delay syntax, the approval, the schedule, the sweep and its claim."""
import datetime as dt
import json

import pytest
from conftest import CREDS, TOKENS, Setup
from test_first_comment import COMMENT, COMMUNITY_TOKENS, POST_URN, comment_stages, raising, with_comment

from linkedin_publisher import api, approval, cli, claims, firstcomment
from linkedin_publisher import postqueue as queue
from linkedin_publisher.config import ConfigError, load_config, parse_delay


def make(tmp_path, monkeypatch, community=""):
    tmp_path.mkdir(parents=True, exist_ok=True)
    setup = Setup(tmp_path, f'[community]\ntokens_file = "community.json"\n{community}')
    setup.install_fake_linkedin(monkeypatch)
    setup.cfg.community_tokens_file.write_text(json.dumps(COMMUNITY_TOKENS))
    setup.sent = []
    monkeypatch.setattr(api, "load_credentials", lambda _cfg, app="main": CREDS)
    monkeypatch.setattr(api, "read_tokens", lambda _cfg, app="main": COMMUNITY_TOKENS if app == "community" else TOKENS)
    monkeypatch.setattr(api, "ensure_access_token", lambda _cfg, _creds, tokens, *_a: tokens)
    monkeypatch.setattr(api, "place_comment", lambda _c, _cr, _t, urn, text, url=None:
                        setup.sent.append((urn, text)) or "777")
    return setup


@pytest.fixture
def delayed(tmp_path, monkeypatch):
    return make(tmp_path, monkeypatch)


def tick(setup, minutes):
    """One scheduled run, `minutes` after now."""
    return cli.cmd_publish(setup.cfg, dt.datetime.now() + dt.timedelta(minutes=minutes), "post", None)


def scheduled_post(setup, delay="30m"):
    path = with_comment(setup)
    path.write_text(path.read_text().replace("slot: 2026-09-23", f"slot: 2026-09-23\ncomment_delay: {delay}"))
    assert setup.run("publish", "--post", "--file", str(path)) == 0
    return setup.cfg.published_dir / "a.md"


# ------------------------------------------------------------------ the syntax

@pytest.mark.parametrize("text, minutes", [("0", 0), ("30m", 30), ("1h", 60), ("2h", 120), ("90m", 90), ('"45m"', 45)])
def test_parse_delay_accepts(text, minutes):
    assert parse_delay(text) == minutes


@pytest.mark.parametrize("text", ["", "30", "1.5h", "-5m", "1d", "30 min", "m", "00m", "soon"])
def test_parse_delay_rejects(text):
    with pytest.raises(ValueError, match="not a delay"):
        parse_delay(text)


def test_a_bad_delay_on_a_post_is_a_content_problem(delayed):
    path = with_comment(delayed)
    path.write_text(path.read_text().replace("status: queued", "status: queued\ncomment_delay: tomorrow"))
    problem = queue.content_problem(queue.read_post(path), delayed.cfg)
    assert problem and problem.startswith("comment_delay: 'tomorrow' is not a delay")


def test_a_bad_default_in_the_config_is_refused(tmp_path):
    (tmp_path / "publisher.toml").write_text('[community]\ncomment_delay = "soon"\n')
    with pytest.raises(ConfigError, match="community.comment_delay"):
        load_config(str(tmp_path / "publisher.toml"))


def test_the_post_field_wins_over_the_config_default(tmp_path, monkeypatch):
    setup = make(tmp_path, monkeypatch, 'comment_delay = "1h"\n')
    path = with_comment(setup)
    assert queue.comment_delay(queue.read_post(path), setup.cfg) == 60
    path.write_text(path.read_text().replace("status: queued", "status: queued\ncomment_delay: 0"))
    assert queue.comment_delay(queue.read_post(path), setup.cfg) == 0


# ------------------------------------------------------------------ the approval

def key_with(setup, delay=None, comment=True):
    path = with_comment(setup) if comment else setup.post()
    if delay is not None:
        path.write_text(path.read_text().replace("status: queued", f"status: queued\ncomment_delay: {delay}"))
    return approval.approval_key(queue.read_post(path), setup.cfg)


def test_no_delay_keeps_the_key_of_a_post_without_a_delay(tmp_path, monkeypatch):
    plain = key_with(make(tmp_path / "a", monkeypatch), None)
    assert key_with(make(tmp_path / "b", monkeypatch), "0") == plain
    assert key_with(make(tmp_path / "c", monkeypatch, 'comment_delay = "0"\n'), None) == plain


def test_a_delay_changes_the_key_and_a_different_delay_changes_it_again(tmp_path, monkeypatch):
    plain = key_with(make(tmp_path / "a", monkeypatch), None)
    thirty = key_with(make(tmp_path / "b", monkeypatch), "30m")
    assert thirty != plain and key_with(make(tmp_path / "c", monkeypatch), "1h") != thirty
    assert key_with(make(tmp_path / "d", monkeypatch), "60m") == key_with(make(tmp_path / "e", monkeypatch), "1h")


def test_a_delay_without_a_comment_is_not_in_the_key(tmp_path, monkeypatch):
    assert key_with(make(tmp_path / "a", monkeypatch), "1h", comment=False) == \
        key_with(make(tmp_path / "b", monkeypatch), None, comment=False)


def test_changing_the_delay_voids_the_approval(delayed):
    path = with_comment(delayed)
    approval.approve(delayed.cfg, path, "Test Person")
    assert approval.check(queue.read_post(path), delayed.cfg)[0] == approval.APPROVED
    path.write_text(path.read_text().replace("status: queued", "status: queued\ncomment_delay: 30m"))
    assert approval.check(queue.read_post(path), delayed.cfg)[0] == approval.MISMATCH
    approval.approve(delayed.cfg, path, "Test Person")
    path.write_text(path.read_text().replace("comment_delay: 30m", "comment_delay: 1h"))
    assert approval.check(queue.read_post(path), delayed.cfg)[0] == approval.MISMATCH


def test_changing_the_config_default_voids_the_approval(tmp_path, monkeypatch):
    setup = make(tmp_path, monkeypatch, 'comment_delay = "1h"\n')
    path = with_comment(setup)
    approval.approve(setup.cfg, path, "Test Person")
    setup.cfg.comment_delay_minutes = 120
    assert approval.check(queue.read_post(path), setup.cfg)[0] == approval.MISMATCH


# ------------------------------------------------------------------ scheduling and the sweep

def test_a_delay_schedules_the_comment_and_places_nothing(delayed):
    published = scheduled_post(delayed)
    assert delayed.sent == []
    assert delayed.receipt_stages() == ["attempt", "placed", "comment-scheduled"]
    meta = queue.read_post(published).meta
    assert meta["comment_status"] == "scheduled" and meta["status"] == "published"
    due = dt.datetime.fromisoformat(meta["comment_due"]).astimezone()
    assert dt.timedelta(minutes=29) < due - dt.datetime.now().astimezone() < dt.timedelta(minutes=31)
    assert (delayed.cfg.published_dir / "a.comment.md").exists()


def test_no_delay_places_the_comment_at_once_as_before(delayed):
    path = with_comment(delayed)
    assert delayed.run("publish", "--post", "--file", str(path)) == 0
    assert delayed.sent == [(POST_URN, COMMENT)]
    assert delayed.receipt_stages() == ["attempt", "placed", "comment-attempt", "comment-placed"]


def test_a_scheduled_run_places_the_comment_only_when_it_is_due(delayed):
    published = scheduled_post(delayed)
    tick(delayed, 10)
    assert delayed.sent == []
    tick(delayed, 31)
    assert delayed.sent == [(POST_URN, COMMENT)]
    assert comment_stages(delayed) == ["comment-scheduled", "comment-attempt", "comment-placed"]
    meta = queue.read_post(published).meta
    assert meta["comment_status"] == "placed" and meta["comment_urn"] == "777"
    tick(delayed, 60)
    assert len(delayed.sent) == 1


def test_a_due_comment_is_placed_while_a_post_fails(delayed, monkeypatch):
    scheduled_post(delayed)
    slot = dt.datetime.now() + dt.timedelta(minutes=26)          # due in the run, 31 minutes from now
    delayed.post("b.md", {"slot": f"{slot:%Y-%m-%d}", "slot_time": f'"{slot:%H:%M}"'}, body="Another post.")
    monkeypatch.setattr(api, "publish", raising(api.PublisherError("boom")))
    with pytest.raises(api.PublisherError):
        tick(delayed, 31)
    assert delayed.sent == [(POST_URN, COMMENT)]


def test_the_claim_lets_exactly_one_of_two_runs_place_the_comment(delayed, monkeypatch):
    """The second run starts while the first is inside the LinkedIn call."""
    scheduled_post(delayed)
    inner = []

    def slow_place(_c, _cr, _t, urn, text, url=None):
        delayed.sent.append((urn, text))
        if not inner:
            inner.append(firstcomment.sweep(delayed.cfg, dt.datetime.now() + dt.timedelta(minutes=31),
                                            lambda _m: None, cli.alert))
        return "777"

    monkeypatch.setattr(api, "place_comment", slow_place)
    tick(delayed, 31)
    assert inner == [0] and len(delayed.sent) == 1


def test_a_claim_held_by_another_run_keeps_the_comment_back(delayed):
    scheduled_post(delayed)
    key = queue.content_key("Hello world.")
    assert claims.take(delayed.cfg, firstcomment.claim_key(key), "a.md#comment")
    tick(delayed, 31)
    assert delayed.sent == [] and "comment-attempt" not in delayed.receipt_stages()


def test_the_claim_of_a_comment_is_not_the_claim_of_its_post(delayed):
    scheduled_post(delayed)
    key = queue.content_key("Hello world.")
    assert claims.exists(delayed.cfg, key) and not claims.exists(delayed.cfg, firstcomment.claim_key(key))
    tick(delayed, 31)
    assert claims.exists(delayed.cfg, firstcomment.claim_key(key))


def test_a_receipt_that_shows_the_comment_placed_wins_over_the_file(delayed):
    published = scheduled_post(delayed)
    queue.write_receipt(delayed.cfg, "comment-placed", queue.read_post(published), ref="1")
    tick(delayed, 31)
    assert delayed.sent == []


def test_an_uncertain_scheduled_comment_is_never_tried_again(delayed, monkeypatch):
    published = scheduled_post(delayed)
    monkeypatch.setattr(api, "place_comment", raising(api.PublishUncertain("timeout")))
    tick(delayed, 31)
    assert comment_stages(delayed)[-2:] == ["comment-attempt", "comment-uncertain"]
    assert queue.read_post(published).meta["comment_status"] == "uncertain"
    monkeypatch.setattr(api, "place_comment", lambda *_a, **_k: delayed.sent.append("again") or "1")
    tick(delayed, 60)
    assert delayed.sent == []


def test_a_missing_community_token_at_the_due_time_is_recorded_and_told(delayed):
    published = scheduled_post(delayed)
    delayed.cfg.community_tokens_file.unlink()
    tick(delayed, 31)
    meta = queue.read_post(published).meta
    assert meta["comment_status"] == "failed" and "no community token" in meta["comment_error"]
    assert comment_stages(delayed)[-1] == "comment-failed"
    assert "LinkedIn first comment not placed" in delayed.titles()
    tick(delayed, 60)
    assert comment_stages(delayed).count("comment-failed") == 1


# ------------------------------------------------------------------ what a person sees

def test_due_and_status_show_the_scheduled_comment(delayed, capsys):
    published = scheduled_post(delayed)
    due = dt.datetime.fromisoformat(queue.read_post(published).meta["comment_due"]).astimezone()
    cli.cmd_due(delayed.cfg, dt.datetime.now())
    out = capsys.readouterr().out
    assert "Scheduled first comments" in out and f"a.md  due {due:%Y-%m-%d %H:%M}" in out
    cli.cmd_status(delayed.cfg)
    assert f"a.md  due {due:%Y-%m-%d %H:%M}" in capsys.readouterr().out


def test_precheck_reports_a_scheduled_comment_whose_token_is_invalid_at_its_due_time(delayed, capsys, monkeypatch):
    scheduled_post(delayed)
    soon = dict(COMMUNITY_TOKENS, access_expires_at=(dt.datetime.now() + dt.timedelta(minutes=5)).isoformat())
    delayed.cfg.community_tokens_file.write_text(json.dumps(soon))
    monkeypatch.setattr(api, "read_tokens", lambda _cfg, app="main": soon if app == "community" else TOKENS)
    capsys.readouterr()
    assert cli.cmd_precheck(delayed.cfg, dt.datetime.now(), 1, True) == 3
    problem = json.loads(capsys.readouterr().out)["scheduled_comments"][0]
    assert problem["post"] == "a.md" and "expired" in problem["problem"]
    assert "LinkedIn precheck: a first comment will not be placed" in delayed.titles()


# ------------------------------------------------------------------ the review round of the first comment

def test_a_delayed_comment_follows_the_post_when_the_name_is_taken(delayed):
    delayed.cfg.published_dir.mkdir(parents=True)
    (delayed.cfg.published_dir / "a.md").write_text("---\nstatus: published\n---\n\nOld post.\n", encoding="utf-8")
    scheduled_post(delayed)
    published = delayed.cfg.published_dir
    assert (published / "a-2.comment.md").exists() and not (published / "a.comment.md").exists()
    assert [item.path.name for item in firstcomment.scheduled(delayed.cfg)] == ["a-2.md"]
    tick(delayed, 31)
    assert delayed.sent == [(POST_URN, COMMENT)]
    assert queue.read_post(published / "a-2.md").meta["comment_status"] == "placed"


def test_a_manual_retry_stays_out_while_another_run_holds_the_claim_of_the_comment(delayed, capsys):
    import os
    published = scheduled_post(delayed)
    key = queue.content_key("Hello world.")
    assert claims.take(delayed.cfg, firstcomment.claim_key(key), "a.md#comment")
    claims.claim_path(delayed.cfg, firstcomment.claim_key(key)).write_text(json.dumps(
        {"pid": os.getppid(), "file": "a.md#comment", "claimed_at": dt.datetime.now(dt.UTC).isoformat()}))
    assert delayed.run("publish", "--comment-retry", "--file", str(published)) == 1
    assert delayed.sent == [] and "still holds the claim" in capsys.readouterr().err


def test_after_a_manual_retry_the_sweep_does_not_send_the_comment_again(delayed):
    published = scheduled_post(delayed)
    assert delayed.run("publish", "--comment-retry", "--file", str(published)) == 0
    key = queue.content_key("Hello world.")
    assert claims.exists(delayed.cfg, firstcomment.claim_key(key))
    tick(delayed, 31)
    assert delayed.sent == [(POST_URN, COMMENT)]
