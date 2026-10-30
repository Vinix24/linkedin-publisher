"""A number that is measured shortly before a post goes out, and an approval that survives it."""
import datetime as dt
import json
import os
import sys
import time

import pytest
from conftest import Setup

from linkedin_publisher import api, approval, claims, cli
from linkedin_publisher import postqueue as queue
from linkedin_publisher.config import ConfigError, load_config

LINE = "There are {n} items left."
BODY = "Intro paragraph.\n\nThere are 53 items left.\n\nOutro paragraph."
NOW = dt.datetime(2026, 9, 22, 17, 0)               # the evening before the slot
SLOT = dt.datetime(2026, 9, 23, 10, 0)
SLOT_PLUS_5 = dt.datetime(2026, 9, 23, 10, 5)
TITLE_NOT_UPDATED = "LinkedIn post: number not updated"
TITLE_LINE_MISMATCH = "LinkedIn post: measure line does not match"
REQUIRED = "[approval]\nrequired = true\n"

GOLDEN_TEXT_POST = "3b28743b23cf67524db4410ca04e76b2aff0807aa2ab976b37238b0731b3227e"
GOLDEN_IMAGE_POST = "12c838c277ee5a70cd534ba9f99425370ff12715705a0b5bf510a11b40c7f7f6"
GOLDEN_MULTI_PARAGRAPH = "2106338f062c75f4cf02293e5111a5c1b5af53d3a585ac8607eed9e00537387b"

METER_SCRIPT = """\
import pathlib, sys, time
here = pathlib.Path(__file__).parent
with (here / "calls.log").open("a") as fh:
    fh.write("x\\n")
delay = here / "delay.txt"
time.sleep(float(delay.read_text()) if delay.exists() else 0)
sys.stdout.write((here / "output.txt").read_text())
code = here / "exit.txt"
sys.exit(int(code.read_text()) if code.exists() else 0)
"""


class Meter:
    """A measure command that prints what a file says and writes down every call."""

    def __init__(self, tmp_path):
        self.dir = tmp_path / "meter"
        self.dir.mkdir()
        (self.dir / "measure.py").write_text(METER_SCRIPT, encoding="utf-8")
        self.prints("OPEN: 49\n")

    def prints(self, text: str, exit_code: int = 0, delay: float = 0.0) -> None:
        (self.dir / "output.txt").write_text(text, encoding="utf-8")
        (self.dir / "exit.txt").write_text(str(exit_code), encoding="utf-8")
        (self.dir / "delay.txt").write_text(str(delay), encoding="utf-8")

    def calls(self) -> int:
        log = self.dir / "calls.log"
        return len(log.read_text().splitlines()) if log.exists() else 0

    def toml(self, timeout: float = 20, name: str = "count") -> str:
        argv = json.dumps([sys.executable, str(self.dir / "measure.py")])
        return (f"[measures.{name}]\ncommand = {argv}\npattern = '^OPEN: ([0-9]+)$'\n"
                f"timeout_seconds = {timeout}\n")


@pytest.fixture
def metered(tmp_path, monkeypatch):
    meter = Meter(tmp_path)
    setup = Setup(tmp_path, REQUIRED + meter.toml())
    setup.install_fake_linkedin(monkeypatch)
    setup.meter = meter
    return setup


def measured_post(setup: Setup, name: str = "a.md", number: int = 53, meta: dict | None = None,
                  body: str = BODY):
    fields = {"measure": "count", "measure_line": f'"{LINE}"', **(meta or {})}
    return setup.post(name, fields, body=body.replace("53", str(number)))


def post_of(setup: Setup, name: str = "a.md"):
    return queue.read_post(setup.cfg.queue_dir / name)


def key_of(setup: Setup, name: str = "a.md") -> str:
    return approval.approval_key(post_of(setup, name), setup.cfg)


def approve(setup: Setup, name: str = "a.md") -> int:
    return setup.run("approve", "--file", str(setup.cfg.queue_dir / name), "--by", "Test Person")


def measure(setup: Setup, now: dt.datetime = NOW, days_ahead: int = 1) -> int:
    return cli.cmd_measure(setup.cfg, now, days_ahead)


def replace_in(path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


def run_scheduled(setup: Setup, now: dt.datetime = SLOT_PLUS_5) -> int:
    return cli.cmd_publish(setup.cfg, now, "post", None)


# ------------------------------------------------- the key with a measure line

def test_posts_without_a_measure_line_keep_exactly_the_key_they_had(setup):
    setup.post("a.md", {})
    assert key_of(setup) == GOLDEN_TEXT_POST
    path = setup.post("b.md", {"format": "image", "alt_text": "A chart with two bars"})
    path.with_suffix(".png").write_bytes(b"PNG one")
    assert key_of(setup, "b.md") == GOLDEN_IMAGE_POST
    setup.post("c.md", {}, body="There are 53 items left.\n\nSecond paragraph with *emphasis* and a (paren).")
    assert key_of(setup, "c.md") == GOLDEN_MULTI_PARAGRAPH


def test_a_measure_field_without_a_measure_line_changes_nothing_about_the_key(setup):
    setup.post("a.md", {"measure": "count"})
    assert key_of(setup) == GOLDEN_TEXT_POST


def test_only_the_number_changed_keeps_the_approval(metered):
    path = measured_post(metered)
    assert approve(metered) == 0
    before = key_of(metered)
    replace_in(path, "There are 53 items", "There are 52 items")
    assert key_of(metered) == before
    assert approval.check(post_of(metered), metered.cfg) == (approval.APPROVED, before)
    replace_in(path, "There are 52 items", "There are 1000 items")      # also a longer number
    assert approval.check(post_of(metered), metered.cfg)[0] == approval.APPROVED


@pytest.mark.parametrize("old, new", [
    ("Intro paragraph.", "Intro paragraphs."),
    ("Outro paragraph.", "Outro paragraph!"),
    ("Intro paragraph.", "Intro paragraph. Extra."),
])
def test_one_other_word_changed_voids_the_approval_even_next_to_a_new_number(metered, old, new):
    path = measured_post(metered)
    approve(metered)
    replace_in(path, "There are 53 items", "There are 52 items")
    replace_in(path, old, new)
    assert approval.check(post_of(metered), metered.cfg)[0] == approval.MISMATCH


def test_a_digit_somewhere_else_in_the_text_voids_the_approval(metered):
    measured_post(metered, body="Version 2.0 is near.\n\nThere are 53 items left.")
    approve(metered)
    path = metered.cfg.queue_dir / "a.md"
    replace_in(path, "Version 2.0", "Version 2.1")
    assert approval.check(post_of(metered), metered.cfg)[0] == approval.MISMATCH


def test_a_token_in_the_text_cannot_stand_in_for_the_number_at_another_place(metered):
    """Two texts that differ only in which of two places holds digits and which holds the
    literal mask token would hash the same if the place of the number were not in the key."""
    path = measured_post(metered, meta={"measure_line": '"P{n}S"'}, body="First P12S then P{n}S.")
    approve(metered)
    before = key_of(metered)
    path.write_text(path.read_text().replace("First P12S then P{n}S.", "First P{n}S then P12S."))
    assert key_of(metered) != before
    assert approval.check(post_of(metered), metered.cfg)[0] == approval.MISMATCH


def test_the_template_is_part_of_the_key(metered):
    path = measured_post(metered)
    base = key_of(metered)
    replace_in(path, f'"{LINE}"', '"are {n} items"')       # the same text, found exactly once either way
    assert key_of(metered) != base


def test_a_change_after_the_number_in_the_same_sentence_voids_the_approval(metered):
    path = measured_post(metered)
    approve(metered)
    replace_in(path, "items left.", "items remain.")
    assert approval.check(post_of(metered), metered.cfg)[0] == approval.MISMATCH


def test_the_number_can_sit_inside_markdown(metered):
    path = measured_post(metered, meta={"measure_line": '"There are **{n}** items left."'},
                         body="Intro.\n\nThere are **53** items left.")
    assert approve(metered) == 0
    replace_in(path, "**53**", "**9**")
    assert approval.check(post_of(metered), metered.cfg)[0] == approval.APPROVED


def test_the_notes_are_not_searched_for_the_line(metered):
    measured_post(metered, body=BODY + "\n\n---\n\n## Notes\n\nThere are 53 items left. (private copy)")
    assert approve(metered) == 0


@pytest.mark.parametrize("body", [
    "Intro paragraph.\n\nOutro paragraph.",
    "There are 53 items left.\n\nThere are 12 items left.",
])
def test_a_line_that_matches_zero_or_several_times_cannot_be_approved(metered, capsys, body):
    path = measured_post(metered, body=body)
    before = path.read_bytes()
    assert approve(metered) == 1
    error = capsys.readouterr().err
    assert "measure_line" in error and "exactly once" in error
    assert path.read_bytes() == before


@pytest.mark.parametrize("value", [
    '"No placeholder here."',
    '"{n} and {n} again"',
    "|\n  Line one\n  There are {n} items left.",
])
def test_a_template_needs_exactly_one_placeholder_on_one_line(metered, capsys, value):
    measured_post(metered, meta={"measure_line": value})
    assert approve(metered) == 1
    assert "measure_line" in capsys.readouterr().err


def test_a_scheduled_run_treats_a_line_that_matches_nothing_as_a_mismatch(metered):
    path = measured_post(metered)
    approve(metered)
    replace_in(path, "There are 53 items left.", "Nothing is left.")
    assert run_scheduled(metered) == 3
    assert run_scheduled(metered) == 0
    assert metered.sends == []
    assert metered.titles() == [TITLE_LINE_MISMATCH]
    assert "measure_line" in metered.notifications()[0][1]
    assert metered.receipt_stages() == ["approval-mismatch"]


def test_due_names_the_measure_line_when_it_does_not_match(metered, capsys):
    path = measured_post(metered)
    approve(metered)
    replace_in(path, "There are 53 items left.", "Nothing is left.")
    cli.cmd_due(metered.cfg, SLOT_PLUS_5)
    assert "measure line" in capsys.readouterr().out


def test_the_post_goes_out_with_the_measured_number_under_the_old_approval(metered):
    measured_post(metered)
    approve(metered)
    assert measure(metered) == 0
    assert run_scheduled(metered) == 0
    assert len(metered.sends) == 1
    assert "There are 49 items left." in metered.sends[0]["commentary"]
    assert "53" not in metered.sends[0]["commentary"]


# ------------------------------------------------------------ measure: the write

def test_measure_writes_only_the_digits_byte_for_byte(metered):
    raw = (b"---\r\nstatus: queued\r\nslot: 2026-09-23\r\nmeasure: count\r\n"
           b'measure_line: "There are {n} items left."\r\n'
           b"context: |\r\n  Earlier I wrote: There are 53 items left.\r\n"
           b"---\r\n\r\nIntro paragraph.\r\n\r\nThere are 53 items left.\r\n\r\nOutro.  \r\n\r\n"
           b"---\r\n\r\n## Notes\r\n\r\nThere are 53 items left. (private copy), no final newline")
    path = metered.cfg.queue_dir / "a.md"
    path.write_bytes(raw)
    assert measure(metered) == 0
    expected = raw.replace(b"\r\nThere are 53 items left.\r\n\r\nOutro", b"\r\nThere are 49 items left.\r\n\r\nOutro")
    assert expected != raw
    assert path.read_bytes() == expected


def test_measure_leaves_no_temporary_file_and_keeps_the_file_mode(metered):
    path = measured_post(metered)
    path.chmod(0o640)
    measure(metered)
    assert [p.name for p in metered.cfg.queue_dir.iterdir()] == ["a.md"]
    assert path.stat().st_mode & 0o777 == 0o640


def test_measure_records_old_and_new_in_a_measured_note_and_changes_no_outcome(metered):
    measured_post(metered)
    old_key = queue.content_key(queue.post_text(post_of(metered).body, metered.cfg.notes_headings))
    measure(metered)
    rows = [json.loads(line) for f in metered.cfg.receipts_dir.glob("*.ndjson")
            for line in f.read_text().splitlines()]
    assert [r["stage"] for r in rows] == ["measured"]
    assert rows[0]["ref"] == "53->49" and rows[0]["file"] == "a.md"
    new_key = queue.content_key(queue.post_text(post_of(metered).body, metered.cfg.notes_headings))
    assert rows[0]["content_key"] == new_key != old_key
    assert queue.load_states(metered.cfg) == {}
    assert metered.notifications() == []


def test_a_number_that_did_not_change_writes_nothing(metered):
    path = measured_post(metered, number=49)
    before, stamp = path.read_bytes(), path.stat().st_mtime_ns
    assert measure(metered) == 0
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp
    assert metered.receipt_stages() == []
    assert metered.meter.calls() == 1


def test_measure_keeps_a_valid_approval_valid(metered):
    measured_post(metered)
    approve(metered)
    measure(metered)
    assert approval.check(post_of(metered), metered.cfg)[0] == approval.APPROVED


def test_measure_only_touches_queued_posts_with_a_measure_inside_the_range(metered):
    measured_post(metered, "today.md", meta={"slot": "2026-09-22", "slot_time": '"18:00"'})
    measured_post(metered, "tomorrow.md")
    measured_post(metered, "later.md", meta={"slot": "2026-09-24"})
    measured_post(metered, "draft.md", meta={"status": "draft"})
    metered.post("plain.md", {}, body="There are 53 items left.")

    def numbers() -> dict[str, bool]:
        return {p.name: "49" in p.read_text() for p in sorted(metered.cfg.queue_dir.glob("*.md"))}

    measure(metered, days_ahead=0)
    assert numbers() == {"draft.md": False, "later.md": False, "plain.md": False,
                         "today.md": True, "tomorrow.md": False}
    measure(metered, days_ahead=1)
    assert numbers() == {"draft.md": False, "later.md": False, "plain.md": False,
                         "today.md": True, "tomorrow.md": True}
    measure(metered, days_ahead=2)
    assert numbers()["later.md"] is True


def test_measure_never_posts_and_never_calls_linkedin(tmp_path, linkedin_must_stay_silent):
    meter = Meter(tmp_path)
    setup = Setup(tmp_path, meter.toml())
    measured_post(setup)
    assert measure(setup) == 0
    assert "49" in (setup.cfg.queue_dir / "a.md").read_text()


# ---------------------------------------------------------- measure: what it refuses

def _claim(setup):
    claims.take(setup.cfg, queue.content_key(queue.post_text(post_of(setup).body, setup.cfg.notes_headings)),
                "a.md")


def _receipt(stage):
    def write(setup):
        queue.write_receipt(setup.cfg, stage, post_of(setup))
    return write


REFUSALS = {
    "slot-reached": (SLOT, lambda setup: None),
    "after-the-slot": (SLOT + dt.timedelta(minutes=30), lambda setup: None),
    "claim": (NOW, _claim),
    "placed": (NOW, _receipt("placed")),
    "uncertain": (NOW, _receipt("uncertain")),
    "attempt": (NOW, _receipt("attempt")),
}


@pytest.mark.parametrize("reason", sorted(REFUSALS))
def test_measure_refuses_a_post_whose_text_is_already_in_play(metered, reason):
    now, prepare = REFUSALS[reason]
    path = measured_post(metered, meta={"slot_time": '"10:00"'})
    prepare(metered)
    before = path.read_bytes()
    stages = metered.receipt_stages()
    assert measure(metered, now) == 0
    assert path.read_bytes() == before
    assert metered.meter.calls() == 0
    assert metered.receipt_stages() == stages
    assert metered.notifications() == []


def test_a_failed_attempt_that_sent_nothing_does_not_stop_measure(metered):
    measured_post(metered)
    post = post_of(metered)
    queue.write_receipt(metered.cfg, "attempt", post)
    queue.write_receipt(metered.cfg, "failed", post)
    measure(metered)
    assert "49" in (metered.cfg.queue_dir / "a.md").read_text()


def test_a_number_changed_after_a_send_attempt_would_allow_a_second_post(metered, monkeypatch):
    """The identity of a post is the hash of its text, number included. A number changed after
    a send attempt is a post that no receipt and no claim knows."""
    path = measured_post(metered)
    metered.install_fake_linkedin(monkeypatch, fail_with=api.PublishUncertain("timeout"))
    assert metered.run("publish", "--post", "--file", str(path)) == 1       # a person, ahead of the slot
    assert metered.receipt_stages() == ["attempt", "uncertain"]
    before = path.read_bytes()
    changed = before.replace(b"There are 53", b"There are 49")

    def key(raw: bytes) -> str:
        return queue.content_key(queue.post_text(queue.parse_post(path, raw.decode()).body,
                                                 metered.cfg.notes_headings))

    assert key(changed) != key(before)
    assert key(changed) not in queue.load_states(metered.cfg)
    assert not claims.exists(metered.cfg, key(changed))

    assert measure(metered) == 0                       # the guard: measure leaves it alone
    assert path.read_bytes() == before and metered.meter.calls() == 0

    path.write_bytes(changed)                          # a hand edit: measure itself never writes here
    ready, skipped = queue.collect(metered.cfg, SLOT_PLUS_5)
    assert ready == [] and [p.path.name for p in skipped] == ["a.md"]      # the claim still names the file
    assert "left a claim" in skipped[0].reason


def test_the_slot_arriving_while_the_command_runs_stops_the_write(metered):
    path = measured_post(metered)
    metered.meter.prints("OPEN: 49\n", delay=0.4)
    before = path.read_bytes()
    assert measure(metered, SLOT - dt.timedelta(milliseconds=100)) == 0
    assert metered.meter.calls() == 1                  # it did measure, and then it refused to write
    assert path.read_bytes() == before
    assert metered.receipt_stages() == []


def test_a_claim_taken_while_the_command_runs_stops_the_write(metered, monkeypatch):
    from linkedin_publisher import measures
    path = measured_post(metered)
    before = path.read_bytes()
    real = measures.read_number

    def number_then_a_claim(*args, **kwargs):
        value = real(*args, **kwargs)
        _claim(metered)
        return value

    monkeypatch.setattr(measures, "read_number", number_then_a_claim)
    assert measure(metered) == 0
    assert path.read_bytes() == before


def test_a_person_editing_while_the_command_runs_wins(metered, monkeypatch):
    from linkedin_publisher import measures
    path = measured_post(metered)
    real = measures.read_number

    def number_then_an_edit(*args, **kwargs):
        value = real(*args, **kwargs)
        replace_in(path, "Intro paragraph.", "An edited intro.")
        return value

    monkeypatch.setattr(measures, "read_number", number_then_an_edit)
    measure(metered)
    text = path.read_text()
    assert "An edited intro." in text and "There are 53 items" in text
    assert [p.name for p in metered.cfg.queue_dir.iterdir()] == ["a.md"]


# ------------------------------------- a hand edit of the digits after an unclear send attempt

def _unclear(setup, monkeypatch):
    setup.install_fake_linkedin(monkeypatch, fail_with=api.PublishUncertain("timeout"))
    assert run_scheduled(setup) == 1
    assert setup.receipt_stages() == ["attempt", "uncertain"]


def _crash(setup, monkeypatch):
    setup.install_fake_linkedin(monkeypatch, fail_with=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        run_scheduled(setup)
    assert setup.receipt_stages() == ["attempt"]


def _placed_but_not_moved(setup, monkeypatch):
    setup.install_fake_linkedin(monkeypatch)

    def broken_move(*_args, **_kwargs):
        raise OSError("disk is read-only")

    with monkeypatch.context() as patch:
        patch.setattr(queue, "move_to_published", broken_move)
        assert run_scheduled(setup) == 1
    assert setup.receipt_stages() == ["attempt", "placed", "move-failed"]
    assert len(setup.sends) == 1


EARLIER_ATTEMPTS = {"unclear": _unclear, "crash": _crash, "placed-but-not-moved": _placed_but_not_moved}


@pytest.fixture(params=sorted(EARLIER_ATTEMPTS))
def edited_by_hand(request, metered, monkeypatch):
    """An approved measure post that a scheduled run claimed and could not finish, whose digits a
    person then changed by hand. That edit keeps the approval and gives the text a new identity:
    no receipt and no claim knows it. Returns (setup, path of the post)."""
    path = measured_post(metered)
    assert approve(metered) == 0
    EARLIER_ATTEMPTS[request.param](metered, monkeypatch)
    replace_in(path, "There are 53", "There are 49")
    metered.install_fake_linkedin(monkeypatch)          # LinkedIn answers again
    return metered, path


def test_a_digits_only_edit_keeps_the_approval_and_leaves_the_new_text_unknown(edited_by_hand):
    setup, path = edited_by_hand
    post = queue.read_post(path)
    new_key = queue.content_key(queue.post_text(post.body, setup.cfg.notes_headings))
    assert approval.check(post, setup.cfg)[0] == approval.APPROVED
    assert new_key not in queue.load_states(setup.cfg) and not claims.exists(setup.cfg, new_key)
    assert claims.held_for_file(setup.cfg, "a.md")


def test_a_scheduled_run_sends_nothing_after_a_digits_only_edit(edited_by_hand, capsys):
    setup, _path = edited_by_hand
    sent, stages = len(setup.sends), setup.receipt_stages()
    ready, skipped = queue.collect(setup.cfg, SLOT_PLUS_5)
    assert ready == [] and [p.path.name for p in skipped] == ["a.md"]
    reason = skipped[0].reason
    assert "earlier attempt" in reason and "claim" in reason and "Check LinkedIn" in reason
    assert f"publish --post --file {setup.cfg.queue_dir / 'a.md'} --retry" in reason
    capsys.readouterr()
    assert run_scheduled(setup) == 0
    assert len(setup.sends) == sent
    assert setup.receipt_stages() == stages
    assert "Nothing is due." in capsys.readouterr().out


def test_file_without_retry_sends_nothing_after_a_digits_only_edit(edited_by_hand, capsys):
    setup, path = edited_by_hand
    sent, stages = len(setup.sends), setup.receipt_stages()
    assert setup.run("publish", "--post", "--file", str(path)) == 1
    err = capsys.readouterr().err
    assert "a.md" in err and "claim" in err and "Check LinkedIn" in err and "--retry" in err
    assert len(setup.sends) == sent and setup.receipt_stages() == stages
    assert path.exists() and not setup.cfg.lock_file.exists()


def test_file_with_retry_is_the_way_out_after_a_digits_only_edit(edited_by_hand):
    setup, path = edited_by_hand
    sent = len(setup.sends)
    assert setup.run("publish", "--post", "--file", str(path), "--retry") == 0
    assert len(setup.sends) == sent + 1 and "There are 49 items left." in setup.sends[-1]["commentary"]
    assert setup.receipt_stages()[-2:] == ["attempt", "placed"]
    assert (setup.cfg.published_dir / "a.md").exists() and not path.exists()


def test_a_claim_naming_another_file_does_not_hold_a_measure_post(metered):
    measured_post(metered)
    approve(metered)
    assert claims.take(metered.cfg, "9" * 64, "other.md")
    ready, skipped = queue.collect(metered.cfg, SLOT_PLUS_5)
    assert [p.path.name for p in ready] == ["a.md"] and skipped == []
    assert run_scheduled(metered) == 0
    assert len(metered.sends) == 1 and "There are 53" in metered.sends[0]["commentary"]


@pytest.mark.parametrize("meta", [{"measure": "count"}, {"measure_line": ""}], ids=["measure-only", "empty-line"])
def test_a_post_without_a_measure_line_is_not_held_by_a_claim_on_its_file(metered, meta):
    metered.post("a.md", meta, body=BODY)
    approve(metered)
    assert claims.take(metered.cfg, "9" * 64, "a.md")                  # another text, same file name
    assert [p.path.name for p in queue.collect(metered.cfg, SLOT_PLUS_5)[0]] == ["a.md"]
    assert run_scheduled(metered) == 0
    assert len(metered.sends) == 1


def test_measure_and_the_scheduled_run_still_work_for_a_post_nothing_was_tried_with(metered):
    measured_post(metered)
    approve(metered)
    assert measure(metered) == 0
    assert "There are 49 items left." in (metered.cfg.queue_dir / "a.md").read_text()
    assert run_scheduled(metered) == 0
    assert "There are 49 items left." in metered.sends[0]["commentary"]


# ----------------------------------------------------------- measure: when it fails

def _timeout_setup(tmp_path, monkeypatch):
    meter = Meter(tmp_path)
    meter.prints("OPEN: 49\n", delay=30)
    setup = Setup(tmp_path, REQUIRED + meter.toml(timeout=0.5))
    setup.install_fake_linkedin(monkeypatch)
    setup.meter = meter
    return setup


FAILURES = {
    "exit-code": lambda m: m.prints("OPEN: 49\n", exit_code=3),
    "no-match": lambda m: m.prints("nothing useful here\n"),
    "ambiguous": lambda m: m.prints("OPEN: 49\nOPEN: 50\n"),
    "huge": lambda m: m.prints("OPEN: 1234567890123456789\n"),
}


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_a_failed_measure_keeps_the_number_goes_out_and_alerts_once(metered, failure):
    FAILURES[failure](metered.meter)
    path = measured_post(metered, "launch.md")
    approve(metered, "launch.md")
    before = path.read_bytes()
    assert measure(metered) == 3
    assert measure(metered) == 0
    assert path.read_bytes() == before
    assert metered.titles() == [TITLE_NOT_UPDATED]
    message = metered.notifications()[0][1]
    assert "launch" in message and "2026-09-23 10:00" in message and "53" in message
    assert metered.receipt_stages() == ["measure-failed"]
    assert run_scheduled(metered) == 0                                  # the post still goes out
    assert len(metered.sends) == 1 and "There are 53 items left." in metered.sends[0]["commentary"]


def test_a_command_that_runs_too_long_is_a_failure_and_the_post_keeps_its_number(tmp_path, monkeypatch):
    setup = _timeout_setup(tmp_path, monkeypatch)
    path = measured_post(setup)
    before = path.read_bytes()
    started = time.monotonic()
    assert measure(setup) == 3
    assert time.monotonic() - started < 10
    assert path.read_bytes() == before
    assert setup.titles() == [TITLE_NOT_UPDATED]
    assert "timed out" in setup.notifications()[0][1]


@pytest.mark.skipif(os.name == "nt", reason="process groups")
def test_a_timeout_ends_the_processes_the_command_started_too(tmp_path, monkeypatch):
    script = tmp_path / "wrapper.py"
    script.write_text("import subprocess, sys, time\n"
                      "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                      "time.sleep(30)\n", encoding="utf-8")
    argv = json.dumps([sys.executable, str(script)])
    setup = Setup(tmp_path, f"[measures.count]\ncommand = {argv}\npattern = '([0-9]+)'\ntimeout_seconds = 0.5\n")
    measured_post(setup)
    started = time.monotonic()
    assert measure(setup) == 3
    assert time.monotonic() - started < 10


def test_a_command_that_does_not_exist_is_a_failure(tmp_path):
    setup = Setup(tmp_path, '[measures.count]\ncommand = ["/no/such/program"]\npattern = "([0-9]+)"\n')
    path = measured_post(setup)
    before = path.read_bytes()
    assert measure(setup) == 3
    assert path.read_bytes() == before and setup.titles() == [TITLE_NOT_UPDATED]


def test_each_new_failure_reason_alerts_once_for_the_same_post(metered):
    path = measured_post(metered)
    metered.meter.prints("x", exit_code=2)
    measure(metered)
    measure(metered)
    metered.meter.prints("no number\n")
    measure(metered)
    measure(metered)
    metered.meter.prints("OPEN: 49\n", exit_code=2)
    measure(metered)
    assert metered.titles() == [TITLE_NOT_UPDATED] * 2
    assert metered.receipt_stages() == ["measure-failed"] * 2
    assert "53" in path.read_text()


def test_a_post_with_a_measure_but_no_measure_line_is_reported(metered):
    metered.post("a.md", {"measure": "count"}, body="There are 53 items left.")
    assert measure(metered) == 3
    assert "measure_line" in metered.notifications()[0][1]


def test_a_measure_line_that_does_not_match_is_reported_and_nothing_runs(metered):
    path = measured_post(metered, body="Nothing here.")
    assert measure(metered) == 3
    assert metered.meter.calls() == 0 and "Nothing here." in path.read_text()
    assert "measure_line" in metered.notifications()[0][1]


def test_a_measure_name_that_is_not_in_the_config_is_reported(metered):
    measured_post(metered, meta={"measure": "other"})
    assert measure(metered) == 3
    assert metered.meter.calls() == 0
    assert "other" in metered.notifications()[0][1]


@pytest.mark.parametrize("name", ["touch pwned", "/bin/sh -c 'touch pwned'", "$(touch pwned)", "../meter/measure.py"])
def test_the_command_comes_from_the_config_never_from_the_frontmatter(metered, name):
    measured_post(metered, meta={"measure": f'"{name}"', "command": '"touch pwned"'})
    assert measure(metered) == 3
    assert metered.meter.calls() == 0
    assert not (metered.tmp_path / "pwned").exists() and not (metered.cfg.root / "pwned").exists()


def test_measure_runs_the_command_without_a_shell(tmp_path):
    argv = json.dumps([sys.executable, "-c", "print('OPEN: 7;touch pwned')", "x; touch pwned"])
    setup = Setup(tmp_path, f"[measures.count]\ncommand = {argv}\npattern = '^OPEN: ([0-9]+);'\n")
    measured_post(setup)
    measure(setup)
    assert "There are 7 items left." in (setup.cfg.queue_dir / "a.md").read_text()
    assert not (tmp_path / "pwned").exists()


# ----------------------------------------------------------------- the command line

def test_measure_through_the_command_line(metered):
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    path = measured_post(metered, meta={"slot": tomorrow})
    assert metered.run("measure") == 0
    assert "There are 49 items left." in path.read_text()


def test_days_ahead_must_not_be_negative(metered, capsys):
    with pytest.raises(SystemExit) as exit_info:
        metered.run("measure", "--days-ahead", "-1")
    assert exit_info.value.code == 2
    assert "days-ahead" in capsys.readouterr().err


def test_an_unreadable_queue_is_an_error_and_an_alert(tmp_path):
    setup = Setup(tmp_path)
    setup.cfg.queue_dir.rmdir()
    assert setup.run("measure") == 1
    assert setup.titles() == ["LinkedIn queue unreadable"]


# ------------------------------------------------------------------ configuration

def load(tmp_path, toml: str):
    (tmp_path / "publisher.toml").write_text(toml, encoding="utf-8")
    return load_config(str(tmp_path / "publisher.toml"))


def test_defaults_leave_existing_setups_alone(tmp_path):
    cfg = load(tmp_path, "")
    assert cfg.measures == {}
    assert (cfg.measure_field, cfg.measure_line_field, cfg.checks_field) == ("measure", "measure_line", "checks")
    assert cfg.external_route_field is None and cfg.external_route_text is None
    assert cfg.check_timeout_seconds == 15


def test_measures_and_field_names_are_read(tmp_path):
    cfg = load(tmp_path, '[frontmatter]\nmeasure_field = "reading"\nmeasure_line_field = "reading_line"\n'
                         'checks_field = "prechecks"\n'
                         '[approval]\nexternal_route_field = "note"\nexternal_route_text = "runs via a cron"\n'
                         '[checks]\ntimeout_seconds = 3\n'
                         "[measures.open-items]\ncommand = [\"/bin/count\", \"--all\"]\n"
                         "pattern = '^OPEN: ([0-9]+)$'\ntimeout_seconds = 90\n")
    assert (cfg.measure_field, cfg.measure_line_field, cfg.checks_field) == ("reading", "reading_line", "prechecks")
    assert (cfg.external_route_field, cfg.external_route_text) == ("note", "runs via a cron")
    assert cfg.check_timeout_seconds == 3
    measure_config = cfg.measures["open-items"]
    assert measure_config.command == ("/bin/count", "--all") and measure_config.timeout_seconds == 90
    assert measure_config.pattern.search("x\nOPEN: 12\n").group(1) == "12"


def test_custom_field_names_drive_the_whole_flow(tmp_path, monkeypatch):
    meter = Meter(tmp_path)
    setup = Setup(tmp_path, REQUIRED + meter.toml(),
                  frontmatter='measure_field = "reading"\nmeasure_line_field = "reading_line"\n')
    setup.install_fake_linkedin(monkeypatch)
    setup.post("a.md", {"reading": "count", "reading_line": f'"{LINE}"'}, body=BODY)
    setup.post("b.md", {"measure": "count", "measure_line": f'"{LINE}"'}, body=BODY)     # the default names are plain text now
    assert setup.run("approve", "--file", str(setup.cfg.queue_dir / "a.md"), "--by", "x") == 0
    measure(setup)
    assert "49" in (setup.cfg.queue_dir / "a.md").read_text()
    assert "49" not in (setup.cfg.queue_dir / "b.md").read_text()
    assert approval.check(post_of(setup), setup.cfg)[0] == approval.APPROVED


@pytest.mark.parametrize("toml, message", [
    ('[measures.count]\npattern = "(x)"\n', "measures.count.command"),
    ('[measures.count]\ncommand = "x"\npattern = "(x)"\n', "measures.count.command"),
    ('[measures.count]\ncommand = []\npattern = "(x)"\n', "measures.count.command"),
    ('[measures.count]\ncommand = ["x"]\n', "measures.count.pattern"),
    ('[measures.count]\ncommand = ["x"]\npattern = "("\n', "measures.count.pattern"),
    ('[measures.count]\ncommand = ["x"]\npattern = "no group"\n', "exactly one"),
    ('[measures.count]\ncommand = ["x"]\npattern = "(a)(b)"\n', "exactly one"),
    ('[measures.count]\ncommand = ["x"]\npattern = "(x)"\ntimeout_seconds = 0\n', "measures.count.timeout_seconds"),
    ('[measures.count]\ncommand = ["x"]\npattern = "(x)"\ntimeout_seconds = "soon"\n', "timeout_seconds"),
    ('[measures]\ncount = "x"\n', "measures.count"),
    ('[measures."bad name"]\ncommand = ["x"]\npattern = "(x)"\n', "bad name"),
    ('[checks]\ntimeout_seconds = 0\n', "checks.timeout_seconds"),
    ('[frontmatter]\nmeasure_field = "status"\n', "measure_field"),
    ('[frontmatter]\nchecks_field = ""\n', "checks_field"),
    ('[frontmatter]\nmeasure_field = "same"\nchecks_field = "same"\n', "different"),
    ('[frontmatter]\nmeasure_line_field = "approved_by"\n', "different"),
    ('[approval]\nexternal_route_field = "note"\n', "external_route"),
    ('[approval]\nexternal_route_text = "cron"\n', "external_route"),
    ('[approval]\nexternal_route_field = "bad name"\nexternal_route_text = "x"\n', "external_route_field"),
    ('[approval]\nexternal_route_field = "note"\nexternal_route_text = ""\n', "external_route_text"),
])
def test_unusable_values_are_refused_with_the_name_of_the_option(tmp_path, toml, message):
    with pytest.raises(ConfigError, match=message):
        load(tmp_path, toml)


def test_a_relative_command_runs_from_the_folder_of_the_config(tmp_path):
    (tmp_path / "count.py").write_text("print('OPEN: 8')\n", encoding="utf-8")
    argv = json.dumps([sys.executable, "count.py"])
    setup = Setup(tmp_path, f"[measures.count]\ncommand = {argv}\npattern = '^OPEN: ([0-9]+)$'\n")
    measured_post(setup)
    other = tmp_path / "elsewhere"
    other.mkdir()
    original = os.getcwd()
    os.chdir(other)
    try:
        measure(setup)
    finally:
        os.chdir(original)
    assert "There are 8 items left." in (setup.cfg.queue_dir / "a.md").read_text()
