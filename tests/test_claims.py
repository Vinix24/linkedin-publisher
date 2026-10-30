"""The claim: one file per post, created atomically before anything is sent.

The rest of the guards (receipts, the lock) are read-then-write. Two routes that both
read before either writes both pass them. The claim is the one step that cannot be
passed twice.
"""
import datetime as dt
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import WEDNESDAY_10, Setup

import linkedin_publisher
from linkedin_publisher import api, cli
from linkedin_publisher import postqueue as queue

WORKER = Path(__file__).parent / "claim_worker.py"
SRC = str(Path(linkedin_publisher.__file__).resolve().parents[1])
SLOT_PLUS_5 = dt.datetime(2026, 9, 23, 10, 5)


def key_of(setup: Setup, name: str = "a.md") -> str:
    post = queue.read_post(setup.cfg.queue_dir / name)
    return queue.content_key(queue.post_text(post.body, setup.cfg.notes_headings))


def claim_file(setup: Setup, key: str) -> Path:
    return setup.cfg.receipts_dir / "claims" / f"{key}.claim"


def write_claim(setup: Setup, key: str, *, pid: int, age_minutes: float = 0, file: str = "a.md",
                now: dt.datetime | None = None) -> Path:
    """A claim that is `age_minutes` old at `now` (default: the real now)."""
    path = claim_file(setup, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    base = now.astimezone(dt.UTC) if now else dt.datetime.now(dt.UTC)
    claimed_at = base - dt.timedelta(minutes=age_minutes)
    path.write_text(json.dumps({"pid": pid, "file": file, "route": "linkedin-publisher",
                                "claimed_at": claimed_at.isoformat()}), encoding="utf-8")
    return path


def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def run_workers(setup: Setup, count: int, mode: str, *extra: str) -> tuple[list[int], list[str]]:
    """Start `count` separate processes, release them at the same moment, return their
    exit codes and the lines they wrote to sends.log."""
    work = setup.tmp_path / "race"
    work.mkdir()
    env = {**os.environ, "PYTHONPATH": SRC}
    procs = [subprocess.Popen([sys.executable, str(WORKER), str(setup.config_path), str(i),
                               str(count), str(work), mode, *extra],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for i in range(count)]
    deadline = time.monotonic() + 30
    while len(list(work.glob("ready-*"))) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    (work / "go").touch()
    outputs = [p.communicate(timeout=60) for p in procs]
    for proc, (_out, err) in zip(procs, outputs, strict=True):
        assert proc.returncode == 0, err
    codes = [int((work / f"rc-{i}").read_text()) for i in range(count)]
    sends = (work / "sends.log").read_text().split() if (work / "sends.log").exists() else []
    return codes, sends


# ------------------------------------------------------------------- the file

def test_claim_has_the_agreed_path_content_and_mode(setup):
    from linkedin_publisher import claims
    key = "ab" * 32
    assert claims.take(setup.cfg, key, "a.md") is True
    path = setup.cfg.receipts_dir / "claims" / f"{key}.claim"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o644 & ~_umask()
    row = json.loads(path.read_text())
    assert set(row) == {"pid", "file", "route", "claimed_at"}
    assert row["pid"] == os.getpid() and row["file"] == "a.md" and row["route"] == "linkedin-publisher"
    claimed_at = dt.datetime.fromisoformat(row["claimed_at"])
    assert claimed_at.utcoffset() is not None
    assert abs((dt.datetime.now(dt.UTC) - claimed_at).total_seconds()) < 60


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


def test_a_second_claim_fails_and_leaves_the_first_untouched(setup):
    from linkedin_publisher import claims
    assert claims.take(setup.cfg, "k1", "first.md") is True
    before = claim_file(setup, "k1").read_text()
    assert claims.take(setup.cfg, "k1", "second.md") is False
    assert claim_file(setup, "k1").read_text() == before


def test_claims_folder_does_not_disturb_the_receipt_files(setup):
    from linkedin_publisher import claims
    path = setup.post()
    claims.take(setup.cfg, "k1", "a.md")
    queue.write_receipt(setup.cfg, "placed", queue.read_post(path), post_urn="urn:li:share:1")
    (setup.cfg.receipts_dir / "claims" / "stray.txt").write_text("not a claim")
    assert list(queue.load_placed_keys(setup.cfg)) == [key_of(setup)]
    assert [p.name for p in setup.cfg.receipts_dir.glob("*.ndjson")] != []
    assert all(p.suffix == ".ndjson" for p in setup.cfg.receipts_dir.glob("*.ndjson"))
    assert setup.run("status") == 0


# --------------------------------------------------------- two real processes

def test_eight_processes_claim_forty_keys_and_each_key_has_one_winner(setup):
    codes, _ = run_workers(setup, 8, "take")
    assert codes == [0] * 8
    work = setup.tmp_path / "race"
    for number in range(40):
        winners = list(work.glob(f"won-{number:03d}-*"))
        assert len(winners) == 1, (number, winners)
        assert claim_file(setup, f"key-{number:03d}").is_file()


def test_two_routes_run_publish_file_at_once_and_one_sends(setup):
    setup.post()
    codes, sends = run_workers(setup, 2, "file")
    assert len(sends) == 1
    assert sorted(codes) == [0, 1]
    assert setup.receipt_stages().count("placed") == 1
    assert claim_file(setup, key_of_published(setup)).is_file()


def test_two_routes_run_the_scheduled_publish_at_once_and_one_sends(setup):
    setup.post()
    codes, sends = run_workers(setup, 3, "post")
    assert len(sends) == 1
    # A worker that starts late finds the claim or the receipt while it looks for due posts,
    # and then has nothing to do: 0. One that reaches the claim itself stops with 1.
    assert set(codes) <= {0, 1}
    assert setup.receipt_stages().count("placed") == 1


def test_a_scheduled_run_that_already_listed_the_post_still_loses_the_claim(setup, monkeypatch):
    """The window the claim exists for: both runs listed the post as due before either took it."""
    setup.install_fake_linkedin(monkeypatch)
    setup.post()
    listed = queue.collect(setup.cfg, SLOT_PLUS_5)
    assert len(listed[0]) == 1
    assert cli.cmd_publish(setup.cfg, SLOT_PLUS_5, "post", None) == 0
    monkeypatch.setattr(queue, "collect", lambda _cfg, _now: listed)
    for _ in range(2):
        assert cli.cmd_publish(setup.cfg, SLOT_PLUS_5, "post", None) == 1
    assert len(setup.sends) == 1
    assert setup.receipt_stages() == ["attempt", "placed"]


def test_concurrent_retries_over_a_stale_claim_send_once(setup):
    setup.post()
    key = key_of(setup)
    write_claim(setup, key, pid=dead_pid(), age_minutes=60)
    queue.write_receipt(setup.cfg, "uncertain", queue.read_post(setup.cfg.queue_dir / "a.md"))
    codes, sends = run_workers(setup, 3, "file", "--retry")
    assert len(sends) == 1
    assert sorted(codes) == [0, 1, 1]
    retired = list((setup.cfg.receipts_dir / "claims").glob("*.claim.retired-*"))
    assert len(retired) == 1


def key_of_published(setup: Setup) -> str:
    post = queue.read_post(setup.cfg.published_dir / "a.md")
    return queue.content_key(queue.post_text(post.body, setup.cfg.notes_headings))


# ------------------------------------------------------- what happens to a claim

def test_placed_keeps_the_claim(setup, monkeypatch):
    setup.install_fake_linkedin(monkeypatch)
    path = setup.post()
    key = key_of(setup)
    assert setup.run("publish", "--post", "--file", str(path)) == 0
    assert claim_file(setup, key).is_file()
    assert setup.receipt_stages() == ["attempt", "placed"]
    assert (setup.cfg.published_dir / "a.md").exists()


def test_claim_comes_after_the_checks_and_before_the_attempt_receipt(setup, monkeypatch):
    from linkedin_publisher import claims
    setup.install_fake_linkedin(monkeypatch)
    path = setup.post()
    seen = {}
    real_take = claims.take

    def watching_take(cfg, key, name):
        seen["stages_before_claim"] = setup.receipt_stages()
        return real_take(cfg, key, name)

    monkeypatch.setattr(claims, "take", watching_take)
    assert setup.run("publish", "--post", "--file", str(path)) == 0
    assert seen["stages_before_claim"] == []
    assert setup.receipt_stages()[0] == "attempt"


def test_a_clear_failure_writes_the_receipt_first_and_then_releases_the_claim(setup, monkeypatch):
    from linkedin_publisher import claims
    setup.install_fake_linkedin(monkeypatch, fail_with=api.PublisherError("LinkedIn refused the post (403)"))
    path = setup.post()
    key = key_of(setup)
    order = []
    real_release = claims.release
    monkeypatch.setattr(claims, "release",
                        lambda cfg, k: (order.append(setup.receipt_stages()), real_release(cfg, k)))
    assert setup.run("publish", "--post", "--file", str(path)) == 1
    assert order == [["attempt", "failed"]]
    assert not claim_file(setup, key).exists()
    assert path.exists()


def test_preflight_rejection_releases_the_claim(setup, monkeypatch):
    setup.install_fake_linkedin(monkeypatch, fail_with=api.PreflightRejected("unescaped"))
    path = setup.post()
    assert setup.run("publish", "--post", "--file", str(path)) == 1
    assert setup.receipt_stages() == ["attempt", "preflight-rejected"]
    assert not claim_file(setup, key_of(setup)).exists()


def test_missing_credentials_release_the_claim(setup, capsys):
    path = setup.post()
    assert setup.run("publish", "--post", "--file", str(path)) == 1
    assert "LINKEDIN_CLIENT_ID is missing" in capsys.readouterr().err
    assert setup.receipt_stages() == ["failed"]
    assert not claim_file(setup, key_of(setup)).exists()


def test_an_unclear_outcome_keeps_the_claim(setup, monkeypatch):
    setup.install_fake_linkedin(monkeypatch, fail_with=api.PublishUncertain("timeout"))
    path = setup.post()
    assert setup.run("publish", "--post", "--file", str(path)) == 1
    assert setup.receipt_stages() == ["attempt", "uncertain"]
    assert claim_file(setup, key_of(setup)).is_file()


def test_a_crash_keeps_the_claim(setup, monkeypatch):
    setup.install_fake_linkedin(monkeypatch, fail_with=KeyboardInterrupt())
    path = setup.post()
    with pytest.raises(KeyboardInterrupt):
        setup.run("publish", "--post", "--file", str(path))
    assert setup.receipt_stages() == ["attempt"]
    assert claim_file(setup, key_of(setup)).is_file()


def test_a_run_that_lost_the_claim_does_not_remove_the_lock_of_the_run_that_won(setup, monkeypatch):
    from linkedin_publisher import claims
    setup.install_fake_linkedin(monkeypatch)
    path = setup.post()

    def winner_puts_its_lock_down_first(_cfg, _key, _name):
        setup.cfg.lock_file.touch()
        return False

    monkeypatch.setattr(claims, "take", winner_puts_its_lock_down_first)
    assert setup.run("publish", "--post", "--file", str(path)) == 1
    assert setup.cfg.lock_file.exists()


def test_the_lock_file_is_removed_after_a_run_that_won_the_claim(setup, monkeypatch):
    setup.install_fake_linkedin(monkeypatch)
    path = setup.post()
    assert setup.run("publish", "--post", "--file", str(path)) == 0
    assert not setup.cfg.lock_file.exists()


# ---------------------------------------------------------- an existing claim

def test_file_respects_an_existing_claim(setup, monkeypatch, capsys):
    setup.install_fake_linkedin(monkeypatch)
    path = setup.post()
    write_claim(setup, key_of(setup), pid=os.getpid())
    assert setup.run("publish", "--post", "--file", str(path)) != 0
    assert setup.sends == []
    assert setup.receipt_stages() == []          # no 'failed', and no 'attempt' either
    assert "claim" in setup.cfg.log_file.read_text()
    assert path.exists()


def test_scheduled_run_does_not_pick_up_a_claimed_post(setup, monkeypatch):
    setup.install_fake_linkedin(monkeypatch)
    setup.post()
    write_claim(setup, key_of(setup), pid=os.getpid())
    ready, skipped = queue.collect(setup.cfg, WEDNESDAY_10)
    assert ready == [] and "claim" in skipped[0].reason


def test_a_claimed_post_does_not_hold_up_the_next_one(setup, monkeypatch):
    setup.install_fake_linkedin(monkeypatch)
    setup.post("a.md", {"slot_time": "09:00"}, body="First.")
    setup.post("b.md", {"slot_time": "09:30"}, body="Second.")
    write_claim(setup, key_of(setup, "a.md"), pid=os.getpid())
    assert cli.cmd_publish(setup.cfg, SLOT_PLUS_5, "post", None) == 0
    assert [s["commentary"] for s in setup.sends] == ["Second."]


def test_placed_but_the_move_fails_and_the_receipt_and_claim_block_a_second_placement(setup, monkeypatch):
    setup.install_fake_linkedin(monkeypatch)
    setup.post()
    key = key_of(setup)

    def broken_move(*_a, **_kw):
        raise OSError("disk is read-only")

    monkeypatch.setattr(queue, "move_to_published", broken_move)
    assert cli.cmd_publish(setup.cfg, SLOT_PLUS_5, "post", None) == 1
    assert setup.receipt_stages() == ["attempt", "placed", "move-failed"]
    assert len(setup.sends) == 1 and claim_file(setup, key).is_file()

    # The receipt alone blocks the scheduled run and --file.
    assert cli.cmd_publish(setup.cfg, SLOT_PLUS_5, "post", None) == 0
    assert setup.run("publish", "--post", "--file", str(setup.cfg.queue_dir / "a.md"), "--retry") == 1
    assert len(setup.sends) == 1

    # And the claim alone blocks it too, should the receipts ever be lost.
    for receipt_file in setup.cfg.receipts_dir.glob("*.ndjson"):
        receipt_file.unlink()
    assert cli.cmd_publish(setup.cfg, SLOT_PLUS_5, "post", None) == 0
    assert setup.run("publish", "--post", "--file", str(setup.cfg.queue_dir / "a.md")) != 0
    assert len(setup.sends) == 1


# -------------------------------------------------------------------- --retry

def test_retry_takes_over_a_stale_claim_and_keeps_the_old_one_as_evidence(setup, monkeypatch):
    setup.install_fake_linkedin(monkeypatch)
    path = setup.post()
    key = key_of(setup)
    old = write_claim(setup, key, pid=dead_pid(), age_minutes=60)
    old_content = old.read_text()
    queue.write_receipt(setup.cfg, "uncertain", queue.read_post(path))
    assert setup.run("publish", "--post", "--file", str(path), "--retry") == 0
    assert len(setup.sends) == 1
    assert json.loads(claim_file(setup, key).read_text())["pid"] == os.getpid()
    retired = list((setup.cfg.receipts_dir / "claims").glob("*.claim.retired-*"))
    assert [p.read_text() for p in retired] == [old_content]


def test_retry_refuses_while_the_process_that_holds_the_claim_is_alive(setup, monkeypatch):
    setup.install_fake_linkedin(monkeypatch)
    path = setup.post()
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        write_claim(setup, key_of(setup), pid=holder.pid)
        assert setup.run("publish", "--post", "--file", str(path), "--retry") != 0
    finally:
        holder.kill()
        holder.wait()
    assert setup.sends == []
    assert json.loads(claim_file(setup, key_of(setup)).read_text())["pid"] == holder.pid


def test_retry_stops_when_the_claim_cannot_be_taken_over_atomically(setup, monkeypatch):
    setup.install_fake_linkedin(monkeypatch)
    path = setup.post()
    claim = write_claim(setup, key_of(setup), pid=dead_pid())

    def no_hard_links(*_a, **_kw):
        raise OSError(95, "Operation not supported")

    monkeypatch.setattr(os, "link", no_hard_links)
    assert setup.run("publish", "--post", "--file", str(path), "--retry") != 0
    assert setup.sends == [] and claim.exists()
    assert "remove" in setup.cfg.log_file.read_text()


def test_retry_leaves_a_claim_alone_that_is_still_being_written(setup, monkeypatch):
    """A claim exists a moment before its content does. A retry that reads it in that moment
    must not take it for a stale one: its owner is about to send."""
    setup.install_fake_linkedin(monkeypatch)
    path = setup.post()
    claim = claim_file(setup, key_of(setup))
    claim.parent.mkdir(parents=True)
    claim.write_text("")
    assert setup.run("publish", "--post", "--file", str(path), "--retry") != 0
    assert setup.sends == [] and claim.exists() and claim.read_text() == ""
    assert not list(claim.parent.glob("*.retired-*"))
    assert "Try again" in setup.cfg.log_file.read_text()


def test_retry_takes_over_an_empty_claim_once_it_is_clearly_abandoned(setup, monkeypatch):
    setup.install_fake_linkedin(monkeypatch)
    path = setup.post()
    claim = claim_file(setup, key_of(setup))
    claim.parent.mkdir(parents=True)
    claim.write_text("")
    long_ago = time.time() - 300
    os.utime(claim, (long_ago, long_ago))
    assert setup.run("publish", "--post", "--file", str(path), "--retry") == 0
    assert len(setup.sends) == 1


def test_retry_stops_when_another_claim_took_the_place_of_the_one_it_inspected(setup, monkeypatch):
    setup.install_fake_linkedin(monkeypatch)
    path = setup.post()
    claim = write_claim(setup, key_of(setup), pid=dead_pid(), age_minutes=60)
    real_link = os.link

    def somebody_else_claims_first(src, dst):
        keep = str(src) + ".keep"
        real_link(src, keep)                         # keeps the old inode from being reused
        Path(src).unlink()
        Path(src).write_text(json.dumps({"pid": os.getpid() + 1, "file": "other.md"}))
        real_link(src, dst)

    monkeypatch.setattr(os, "link", somebody_else_claims_first)
    assert setup.run("publish", "--post", "--file", str(path), "--retry") != 0
    assert setup.sends == []
    assert json.loads(claim.read_text())["file"] == "other.md"      # the other claim was not removed
    assert not list(claim.parent.glob("*.claim.retired-*"))


def test_where_a_process_cannot_be_checked_a_retry_waits_for_a_person(monkeypatch):
    import types

    from linkedin_publisher import claims
    monkeypatch.setattr(claims, "os", types.SimpleNamespace(name="nt"))
    assert claims.process_alive(99_999_999) is True


def test_retry_never_overrides_the_claim_of_a_placed_post(setup, monkeypatch):
    setup.install_fake_linkedin(monkeypatch)
    path = setup.post()
    key = key_of(setup)
    claim = write_claim(setup, key, pid=dead_pid())
    queue.write_receipt(setup.cfg, "placed", queue.read_post(path), post_urn="urn:li:share:1")
    assert setup.run("publish", "--post", "--file", str(path), "--retry") == 1
    assert setup.sends == [] and claim.exists()
    assert not list(claim.parent.glob("*.retired-*"))


def test_retry_rechecks_the_receipts_once_the_holder_is_known_to_be_gone(setup, monkeypatch):
    """The holder placed the post between the first receipt check and the takeover."""
    from linkedin_publisher import claims
    setup.install_fake_linkedin(monkeypatch)
    path = setup.post()
    key = key_of(setup)
    claim = write_claim(setup, key, pid=dead_pid())
    queue.write_receipt(setup.cfg, "uncertain", queue.read_post(path))
    real_alive = claims.process_alive

    def holder_finishes_and_places(pid):
        queue.write_receipt(setup.cfg, "placed", queue.read_post(path), post_urn="urn:li:share:1")
        return real_alive(pid)

    monkeypatch.setattr(claims, "process_alive", holder_finishes_and_places)
    assert setup.run("publish", "--post", "--file", str(path), "--retry") != 0
    assert setup.sends == [] and claim.exists()


# ------------------------------------------------------- a claim without outcome

def test_claim_without_outcome_is_reported_once_and_never_retried(setup, monkeypatch):
    setup.install_fake_linkedin(monkeypatch)
    setup.post()
    write_claim(setup, key_of(setup), pid=dead_pid(), age_minutes=30, now=SLOT_PLUS_5)
    for _ in range(2):
        cli.cmd_publish(setup.cfg, SLOT_PLUS_5, "post", None)
    assert setup.sends == []
    assert setup.titles() == ["LinkedIn post: claimed but no outcome"]
    message = setup.notifications()[0][1]
    assert "a.md" in message and "Check LinkedIn" in message
    assert setup.receipt_stages() == ["claim-unresolved"]


def test_claim_report_also_happens_on_a_notify_run(setup):
    write_claim(setup, "k" * 64, pid=dead_pid(), age_minutes=30)
    setup.run("publish", "--notify")
    setup.run("publish", "--notify")
    assert setup.titles() == ["LinkedIn post: claimed but no outcome"]


def test_a_young_claim_is_not_reported_yet(setup):
    write_claim(setup, "k" * 64, pid=os.getpid(), age_minutes=5)
    setup.run("publish", "--notify")
    assert setup.notifications() == [] and setup.receipt_stages() == []


def test_the_claim_of_a_placed_post_is_not_reported(setup):
    path = setup.post()
    write_claim(setup, key_of(setup), pid=dead_pid(), age_minutes=120)
    queue.write_receipt(setup.cfg, "placed", queue.read_post(path))
    assert setup.run("publish", "--notify") == 0
    assert setup.notifications() == []
    assert setup.receipt_stages() == ["placed"]


@pytest.mark.parametrize("stage", ["failed", "preflight-rejected"])
def test_a_stale_claim_after_an_earlier_release_is_reported_once(setup, stage):
    """An earlier run ended clearly and released its claim. A later run took a new claim and
    died before it wrote anything. The old receipt says nothing about this claim."""
    path = setup.post()
    post = queue.read_post(path)
    queue.write_receipt(setup.cfg, "attempt", post)
    queue.write_receipt(setup.cfg, stage, post)
    write_claim(setup, key_of(setup), pid=dead_pid(), age_minutes=30)
    assert setup.run("publish", "--notify") == 3
    assert setup.run("publish", "--notify") == 0
    assert setup.titles() == ["LinkedIn post: claimed but no outcome"]
    assert "a.md" in setup.notifications()[0][1]
    assert setup.receipt_stages() == ["attempt", stage, "claim-unresolved"]


@pytest.mark.parametrize("stage", ["failed", "preflight-rejected"])
def test_a_fresh_claim_after_an_earlier_release_is_not_reported(setup, stage):
    path = setup.post()
    post = queue.read_post(path)
    queue.write_receipt(setup.cfg, "attempt", post)
    queue.write_receipt(setup.cfg, stage, post)
    write_claim(setup, key_of(setup), pid=os.getpid(), age_minutes=5)
    assert setup.run("publish", "--notify") == 0
    assert setup.notifications() == []
    assert setup.receipt_stages() == ["attempt", stage]


def test_the_threshold_is_configurable(tmp_path):
    custom = Setup(tmp_path, "[claims]\nunresolved_minutes = 60\n")
    write_claim(custom, "k" * 64, pid=dead_pid(), age_minutes=30)
    custom.run("publish", "--notify")
    assert custom.notifications() == []
    write_claim(custom, "l" * 64, pid=dead_pid(), age_minutes=90)
    custom.run("publish", "--notify")
    assert len(custom.notifications()) == 1


def test_a_new_claim_over_the_same_post_is_reported_again(setup):
    key = "k" * 64
    write_claim(setup, key, pid=dead_pid(), age_minutes=30)
    setup.run("publish", "--notify")
    claim_file(setup, key).unlink()
    write_claim(setup, key, pid=dead_pid(), age_minutes=20)
    setup.run("publish", "--notify")
    setup.run("publish", "--notify")
    assert len(setup.notifications()) == 2


def test_a_claim_without_content_is_read_by_its_age(setup):
    path = claim_file(setup, "k" * 64)
    path.parent.mkdir(parents=True)
    path.write_text("")
    old = time.time() - 3600
    os.utime(path, (old, old))
    setup.run("publish", "--notify")
    assert len(setup.notifications()) == 1


# ------------------------------------------------- notes never mask an outcome

NOTES = ["stranded", "approval-missing", "approval-mismatch", "claim-unresolved"]


@pytest.mark.parametrize("note", NOTES)
@pytest.mark.parametrize("outcome", ["attempt", "uncertain", "placed", "failed"])
def test_an_informational_stage_never_replaces_the_latest_outcome(setup, note, outcome):
    path = setup.post()
    post = queue.read_post(path)
    key = key_of(setup)
    queue.write_receipt(setup.cfg, outcome, post)
    queue.write_receipt(setup.cfg, note, post)
    assert queue.load_states(setup.cfg) == {key: outcome}


@pytest.mark.parametrize("note", NOTES)
def test_a_note_written_before_an_outcome_does_not_hide_it_either(setup, note):
    post = queue.read_post(setup.post())
    queue.write_receipt(setup.cfg, note, post)
    queue.write_receipt(setup.cfg, "uncertain", post)
    assert key_of(setup) in queue.load_uncertain_keys(setup.cfg)
    assert queue.collect(setup.cfg, WEDNESDAY_10)[0] == []
