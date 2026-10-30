"""Approval: a person signs off on exactly one version of a post, in the post file."""
import datetime as dt
import hashlib

import pytest
from conftest import Setup

from linkedin_publisher import cli, frontmatter
from linkedin_publisher import postqueue as queue

REQUIRED = "[approval]\nrequired = true\n"
SLOT_PLUS_5 = dt.datetime(2026, 9, 23, 10, 5)
SLOT_PLUS_80 = dt.datetime(2026, 9, 23, 11, 20)
SLOT_PLUS_100 = dt.datetime(2026, 9, 23, 11, 40)
IMAGE = {"format": "image", "alt_text": "A chart with two bars"}


@pytest.fixture
def strict(tmp_path, monkeypatch):
    setup = Setup(tmp_path, REQUIRED)
    setup.install_fake_linkedin(monkeypatch)
    return setup


def post_object(setup: Setup, name: str = "a.md"):
    return queue.read_post(setup.cfg.queue_dir / name)


def key(setup: Setup, name: str = "a.md") -> str:
    from linkedin_publisher import approval
    return approval.approval_key(post_object(setup, name), setup.cfg)


def approve(setup: Setup, name: str = "a.md", by: str = "Test Person") -> int:
    return setup.run("approve", "--file", str(setup.cfg.queue_dir / name), "--by", by)


def run_scheduled(setup: Setup, now: dt.datetime = SLOT_PLUS_5) -> int:
    return cli.cmd_publish(setup.cfg, now, "post", None)


# ------------------------------------------------------------------ the key

def image_post(setup: Setup, **meta):
    path = setup.post("a.md", {**IMAGE, **meta})
    path.with_suffix(".png").write_bytes(b"PNG one")
    return path


def test_the_key_changes_with_text_alt_text_and_image_but_not_with_the_slot(setup):
    path = image_post(setup)
    base = key(setup)

    def key_after(old: str, new: str) -> str:
        original = path.read_text()
        assert old in original
        path.write_text(original.replace(old, new))
        try:
            return key(setup)
        finally:
            path.write_text(original)

    assert key_after("Hello world.", "Hello World.") != base
    assert key_after("two bars", "three bars") != base
    path.with_suffix(".png").write_bytes(b"PNG two")
    assert key(setup) != base
    path.with_suffix(".png").write_bytes(b"PNG one")
    assert key(setup) == base
    # None of these go out, so none of them may cancel an approval.
    assert key_after("slot: 2026-09-23", "slot: 2026-10-02") == base
    assert key_after("slot:", "slot_time: 16:00\nslot:") == base
    assert key_after("status: queued", "status: draft") == base
    assert key_after("format:", "pillar: market\nformat:") == base
    assert key_after("Hello world.", "Hello world.\n\n---\n\n## Notes\n\nprivate") == base


def test_the_key_covers_the_video_and_its_title(setup):
    path = setup.post("a.md", {"format": "video", "video_title": "Demo"})
    path.with_suffix(".mp4").write_bytes(b"video one")
    base = key(setup)
    path.with_suffix(".mp4").write_bytes(b"video two")
    assert key(setup) != base
    path.with_suffix(".mp4").write_bytes(b"video one")
    assert key(setup) == base
    path.write_text(path.read_text().replace("Demo", "Other"))
    assert key(setup) != base


def test_the_key_covers_the_document_and_its_title(setup):
    path = setup.post("a.md", {"format": "document", "document_title": "Five lessons"})
    path.with_suffix(".pdf").write_bytes(b"%PDF one")
    base = key(setup)
    path.with_suffix(".pdf").write_bytes(b"%PDF two")
    assert key(setup) != base
    path.with_suffix(".pdf").write_bytes(b"%PDF one")
    path.write_text(path.read_text().replace("Five lessons", "Six lessons"))
    assert key(setup) != base


def test_the_key_ignores_what_is_not_sent(setup):
    path = setup.post("a.md", {"alt_text": "unused", "video_title": "unused"})
    base = key(setup)
    path.with_suffix(".png").write_bytes(b"an image next to a text post")
    path.write_text(path.read_text().replace("unused", "still unused"))
    assert key(setup) == base


def test_the_format_is_part_of_the_key(setup):
    path = setup.post("a.md", {"format": "text"})
    base = key(setup)
    path.write_text(path.read_text().replace("format: text", "format: image"))
    assert key(setup) != base


def test_the_content_key_is_untouched_and_differs_from_the_approval_key(setup):
    setup.post()
    text = queue.post_text(post_object(setup).body, setup.cfg.notes_headings)
    assert queue.content_key(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert key(setup) != queue.content_key(text)


def test_the_key_is_stable_between_calls(setup):
    image_post(setup)
    assert key(setup) == key(setup)


# ---------------------------------------------------------------- approve

def test_approve_writes_who_when_and_the_key_and_the_key_stays_valid(setup, capsys):
    from linkedin_publisher import approval
    path = image_post(setup)
    before = key(setup)
    assert approve(setup, by="Test Person") == 0
    meta = post_object(setup).meta
    assert meta["approved_by"] == "Test Person"
    assert meta["approved_key"] == before
    stamp = dt.datetime.fromisoformat(meta["approved_at"])
    assert stamp.utcoffset() is not None
    assert abs((dt.datetime.now(dt.UTC) - stamp).total_seconds()) < 60
    assert key(setup) == before
    assert approval.check(post_object(setup), setup.cfg) == (approval.APPROVED, before)
    assert before[:12] in capsys.readouterr().out
    assert path.exists()


def test_approve_keeps_the_rest_of_the_file_byte_identical(setup):
    raw = ("---\r\nstatus: queued\r\nslot: 2026-09-23\r\nnote: keep  \r\n---\r\n\r\n"
           "Body line  \r\n\r\n---\r\n\r\n## Notes\r\n\r\nprivate, no final newline").encode()
    path = setup.cfg.queue_dir / "a.md"
    path.write_bytes(raw)
    assert approve(setup) == 0
    new = path.read_bytes()
    kept = [line for line in new.split(b"\r\n")
            if not line.startswith((b"approved_by:", b"approved_at:", b"approved_key:"))]
    assert b"\r\n".join(kept) == raw
    assert new.count(b"\r\n") == raw.count(b"\r\n") + 3
    assert b"\n" not in new.replace(b"\r\n", b"")


def test_approve_keeps_a_file_with_unix_line_endings_and_a_final_newline(setup):
    raw = b"---\nstatus: queued\nslot: 2026-09-23\n---\n\nBody.\n"
    path = setup.cfg.queue_dir / "a.md"
    path.write_bytes(raw)
    approve(setup)
    new = path.read_bytes()
    assert new.startswith(b"---\nstatus: queued\nslot: 2026-09-23\napproved_by: ")
    assert new.endswith(b"---\n\nBody.\n")
    assert b"\r" not in new


def test_approving_again_overwrites_all_three_fields_in_place(setup):
    path = setup.post("a.md", {"pillar": "market"})
    approve(setup, by="First Person")
    first = path.read_text()
    path.write_text(path.read_text().replace("Hello world.", "Hello again."))
    approve(setup, by="Second Person")
    second = path.read_text()
    for field in ("approved_by", "approved_at", "approved_key"):
        assert second.count(f"{field}:") == 1
    assert 'approved_by: "Second Person"' in second and "First Person" not in second
    assert second.splitlines().index("pillar: market") == first.splitlines().index("pillar: market")
    assert post_object(setup).meta["approved_key"] == key(setup)


def test_by_is_required(setup, capsys):
    path = setup.post()
    with pytest.raises(SystemExit) as exit_info:
        setup.run("approve", "--file", str(path))
    assert exit_info.value.code == 2
    assert "--by" in capsys.readouterr().err
    assert "approved_by" not in path.read_text()


@pytest.mark.parametrize("name", ["", "   ", "Eve\nstatus: draft", 'Say "hi"'])
def test_by_cannot_be_empty_or_break_the_frontmatter(setup, name, capsys):
    path = setup.post()
    before = path.read_bytes()
    assert approve(setup, by=name) == 1
    assert "ERROR" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_approve_refuses_a_post_with_a_content_problem(setup, capsys):
    path = setup.post("a.md", {"format": "image"})          # no image, no alt text
    before = path.read_bytes()
    assert approve(setup) == 1
    assert "no image file" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_approve_refuses_a_post_that_is_already_placed(setup, capsys):
    path = setup.post()
    queue.write_receipt(setup.cfg, "placed", post_object(setup), post_urn="urn:li:share:1")
    before = path.read_bytes()
    assert approve(setup) == 1
    assert "already posted" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_approve_refuses_a_file_without_frontmatter(setup, capsys):
    path = setup.cfg.queue_dir / "a.md"
    path.write_text("Just text.\n")
    assert approve(setup) == 1
    assert "frontmatter" in capsys.readouterr().err
    assert path.read_text() == "Just text.\n"


def test_approve_refuses_a_missing_file(setup, capsys):
    assert setup.run("approve", "--file", str(setup.cfg.queue_dir / "nope.md"), "--by", "x") == 1
    assert "does not exist" in capsys.readouterr().err


def test_approve_is_atomic_and_leaves_no_temporary_file(setup, monkeypatch):
    from linkedin_publisher import approval
    path = setup.post()
    before = path.read_bytes()

    def broken_replace(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(approval.os, "replace", broken_replace)
    assert approve(setup) == 1
    assert path.read_bytes() == before
    assert [p.name for p in setup.cfg.queue_dir.iterdir()] == ["a.md"]


def test_a_successful_approve_leaves_no_temporary_file(setup):
    setup.post()
    approve(setup)
    assert [p.name for p in setup.cfg.queue_dir.iterdir()] == ["a.md"]


def test_approve_refuses_when_the_file_changes_while_it_is_being_approved(setup, monkeypatch, capsys):
    from linkedin_publisher import approval
    path = setup.post()
    real = approval.approval_key

    def key_then_an_edit(post, cfg):
        result = real(post, cfg)
        path.write_text(path.read_text().replace("Hello world.", "Edited meanwhile."))
        return result

    monkeypatch.setattr(approval, "approval_key", key_then_an_edit)
    assert approve(setup) == 1
    assert "changed while" in capsys.readouterr().err
    assert "approved_by" not in path.read_text() and "Edited meanwhile." in path.read_text()


def test_field_names_come_from_the_config(tmp_path):
    custom = Setup(tmp_path, frontmatter='approved_by_field = "signed_off_by"\n'
                                         'approved_at_field = "signed_off_at"\n'
                                         'approved_key_field = "signed_off_key"\n')
    custom.post()
    assert approve(custom) == 0
    meta = post_object(custom).meta
    assert {"signed_off_by", "signed_off_at", "signed_off_key"} <= set(meta)
    assert not {"approved_by", "approved_at", "approved_key"} & set(meta)


def test_the_frontmatter_set_fields_helper_handles_a_replaced_block_value():
    raw = "---\nstatus: queued\napproved_by: |\n  old\n  value\nslot: 2026-09-23\n---\n\nBody\n"
    new = frontmatter.set_fields(raw, {"approved_by": '"New"'})
    assert new == '---\nstatus: queued\napproved_by: "New"\nslot: 2026-09-23\n---\n\nBody\n'


# ------------------------------------------- a scheduled run that needs approval

def test_without_the_option_a_queued_post_goes_out_as_before(tmp_path, monkeypatch):
    plain = Setup(tmp_path)
    plain.install_fake_linkedin(monkeypatch)
    plain.post()
    run_scheduled(plain)
    assert len(plain.sends) == 1
    assert plain.titles() == ["LinkedIn post is live"]


def test_no_approval_means_not_posted_and_one_notification_over_two_runs(strict):
    strict.post()
    run_scheduled(strict)
    run_scheduled(strict)
    assert strict.sends == []
    assert strict.titles() == ["LinkedIn post needs approval"]
    assert "approve --file" in strict.notifications()[0][1]
    assert strict.receipt_stages() == ["approval-missing"]


def test_a_valid_approval_posts_once(strict):
    strict.post()
    approve(strict)
    run_scheduled(strict)
    run_scheduled(strict)
    assert len(strict.sends) == 1
    assert strict.receipt_stages() == ["attempt", "placed"]
    published = (strict.cfg.published_dir / "a.md").read_text()
    assert "approved_by:" in published and "approved_key:" in published
    assert not (strict.cfg.queue_dir / "a.md").exists()
    assert strict.titles() == ["LinkedIn post is live"]
    claims_dir = strict.cfg.receipts_dir / "claims"
    assert len(list(claims_dir.glob("*.claim"))) == 1


@pytest.mark.parametrize("edit", ["text", "alt_text", "image"])
def test_a_change_after_the_approval_stops_the_post_and_notifies_once(strict, edit):
    path = image_post(strict)
    approve(strict)
    if edit == "text":
        path.write_text(path.read_text().replace("Hello world.", "Hello, world."))
    elif edit == "alt_text":
        path.write_text(path.read_text().replace("two bars", "three bars"))
    else:
        path.with_suffix(".png").write_bytes(b"another image")
    run_scheduled(strict)
    run_scheduled(strict)
    assert strict.sends == []
    assert strict.titles() == ["LinkedIn post: approval belongs to a different version"]
    assert strict.receipt_stages() == ["approval-mismatch"]
    assert (strict.cfg.queue_dir / "a.md").exists()


def test_approving_the_new_version_lets_it_go_out(strict):
    path = strict.post()
    approve(strict)
    path.write_text(path.read_text().replace("Hello world.", "Hello, world."))
    run_scheduled(strict)
    assert strict.sends == []
    approve(strict)
    run_scheduled(strict)
    assert len(strict.sends) == 1


def test_a_new_mismatch_after_a_second_edit_is_reported_again(strict):
    path = strict.post()
    approve(strict)
    path.write_text(path.read_text().replace("Hello world.", "Version two."))
    run_scheduled(strict)
    path.write_text(path.read_text().replace("Version two.", "Version three."))
    run_scheduled(strict)
    run_scheduled(strict)
    assert len(strict.notifications()) == 2


def test_changing_the_slot_does_not_cancel_the_approval(strict):
    path = strict.post()
    approve(strict)
    path.write_text(path.read_text().replace("slot: 2026-09-23", "slot: 2026-09-23\nslot_time: 10:30"))
    run_scheduled(strict, dt.datetime(2026, 9, 23, 10, 35))
    assert len(strict.sends) == 1


def test_an_unapproved_post_does_not_hold_up_an_approved_one(strict):
    strict.post("a.md", {"slot_time": "09:00"}, body="Not approved.")
    strict.post("b.md", {"slot_time": "09:30"}, body="Approved.")
    approve(strict, "b.md")
    run_scheduled(strict)
    assert [s["commentary"] for s in strict.sends] == ["Approved."]
    assert strict.titles() == ["LinkedIn post needs approval", "LinkedIn post is live"]


def test_a_half_written_approval_counts_as_no_approval(strict):
    strict.post("a.md", {"approved_key": "0" * 64})
    run_scheduled(strict)
    assert strict.sends == [] and strict.receipt_stages() == ["approval-missing"]


def test_a_scheduled_run_that_reports_exits_with_3_once(strict):
    strict.post()
    assert run_scheduled(strict) == 3
    assert run_scheduled(strict) == 0


def test_file_is_a_person_pressing_the_button_and_needs_no_approval(strict):
    path = strict.post()
    assert strict.run("publish", "--post", "--file", str(path)) == 0
    assert len(strict.sends) == 1


def test_a_notify_run_still_only_signals_that_a_post_is_due(strict):
    strict.post()
    cli.cmd_publish(strict.cfg, SLOT_PLUS_5, "notify", None)
    assert strict.titles() == ["LinkedIn post is due"]
    assert strict.sends == []


def test_due_shows_the_approval_state(strict, capsys):
    strict.post()
    cli.cmd_due(strict.cfg, SLOT_PLUS_5)
    assert "[not approved]" in capsys.readouterr().out
    approve(strict)
    cli.cmd_due(strict.cfg, SLOT_PLUS_5)
    out = capsys.readouterr().out
    assert "[approved]" in out and "not approved" not in out


# ------------------------------------------------------------- a late job

def test_a_late_job_posts_an_approved_post_within_the_grace_period(strict):
    strict.post()
    approve(strict)
    run_scheduled(strict, SLOT_PLUS_80)
    assert len(strict.sends) == 1


def test_a_late_job_only_reports_after_the_grace_period(strict):
    strict.post()
    approve(strict)
    assert run_scheduled(strict, SLOT_PLUS_100) == 3
    assert run_scheduled(strict, SLOT_PLUS_100) == 0
    assert strict.sends == []
    assert strict.receipt_stages() == ["stranded"]
    assert strict.titles() == ["LinkedIn post missed its slot"]


def test_stranded_and_approval_notes_do_not_stop_a_later_valid_post(strict):
    strict.post("a.md", {"slot_time": "06:00"}, body="Too late.")
    strict.post("b.md", {"slot_time": "10:00"}, body="On time.")
    approve(strict, "a.md")
    approve(strict, "b.md")
    run_scheduled(strict, SLOT_PLUS_5)
    assert [s["commentary"] for s in strict.sends] == ["On time."]


# ------------------------------------------------ --require-approval, for a job nobody watches

REFUSED_TITLE = "LinkedIn publish stopped: approval is not required"
NOT_REQUIRED = [("", "missing"), ("[approval]\nrequired = false\n", "false")]
MODES = {"post": ("--post",), "notify": ("--notify",), "dry": (), "file": ("--post", "--file", "FILE")}


def argv_for(mode: str, path) -> list[str]:
    return [str(path) if arg == "FILE" else arg for arg in MODES[mode]]


@pytest.mark.parametrize("mode", sorted(MODES))
@pytest.mark.parametrize("toml", [t for t, _ in NOT_REQUIRED], ids=[i for _, i in NOT_REQUIRED])
def test_the_flag_stops_a_run_when_the_config_does_not_require_approval(tmp_path, monkeypatch, capsys, toml, mode):
    setup = Setup(tmp_path, toml)
    setup.install_fake_linkedin(monkeypatch)
    path = setup.post()
    before = path.read_bytes()
    assert setup.run("publish", "--require-approval", *argv_for(mode, path)) == 1
    err = capsys.readouterr().err
    assert err.startswith("ERROR: ") and "[approval]" in err and "required = true" in err
    assert setup.sends == []
    assert setup.titles() == [REFUSED_TITLE] and len(setup.notifications()) == 1
    assert "[approval]" in setup.notifications()[0][1]
    # Nothing else happened: no heartbeat, no log, no receipt, no claim, no lock, the post is as it was.
    assert not setup.cfg.heartbeat_file.exists() and not setup.cfg.log_file.exists()
    assert not setup.cfg.receipts_dir.exists() and not setup.cfg.lock_file.exists()
    assert path.read_bytes() == before and [p.name for p in setup.cfg.queue_dir.iterdir()] == ["a.md"]


@pytest.mark.parametrize("mode", ["post", "notify"])
def test_the_flag_is_checked_before_the_queue_is_read(tmp_path, capsys, mode):
    setup = Setup(tmp_path)
    setup.cfg.queue_dir.rmdir()
    assert setup.run("publish", "--require-approval", *argv_for(mode, None)) == 1
    assert "[approval]" in capsys.readouterr().err
    assert setup.titles() == [REFUSED_TITLE]            # and not also "queue unreadable"


def test_the_flag_in_front_of_the_other_flags_or_behind_them_is_the_same(tmp_path, capsys):
    setup = Setup(tmp_path)
    setup.post()
    assert setup.run("publish", "--post", "--require-approval") == 1
    assert setup.run("publish", "--require-approval", "--notify") == 1
    assert setup.titles() == [REFUSED_TITLE, REFUSED_TITLE]


@pytest.mark.parametrize("mode", ["post", "notify", "dry"])
def test_with_approval_required_the_flag_changes_nothing(tmp_path, monkeypatch, mode):
    outcomes = []
    for name, flag in (("without", False), ("with", True)):
        base = tmp_path / name
        base.mkdir()
        setup = Setup(base, REQUIRED)
        setup.install_fake_linkedin(monkeypatch)
        setup.post("a.md", {"slot_time": "09:00"}, body="Not approved.")
        setup.post("b.md", {"slot_time": "09:30"}, body="Approved.")
        approve(setup, "b.md")
        code = cli.cmd_publish(setup.cfg, SLOT_PLUS_5, {"post": "post", "notify": "notify", "dry": "dry"}[mode], None,
                               require_approval=flag)
        outcomes.append((code, setup.titles(), setup.receipt_stages(), [s["commentary"] for s in setup.sends],
                         sorted(p.name for p in setup.cfg.queue_dir.iterdir())))
    assert outcomes[0] == outcomes[1]
    code, titles, stages, sent, queued = outcomes[1]
    assert REFUSED_TITLE not in titles
    if mode == "post":
        assert (code, sent, stages, queued) == (3, ["Approved."], ["approval-missing", "attempt", "placed"], ["a.md"])
        assert titles == ["LinkedIn post needs approval", "LinkedIn post is live"]
    elif mode == "notify":
        assert (code, sent, titles) == (0, [], ["LinkedIn post is due"])


def test_with_approval_required_an_approved_post_goes_out_through_the_command_line_with_the_flag(strict):
    path = strict.post()
    approve(strict)
    assert strict.run("publish", "--post", "--file", str(path), "--require-approval") == 0
    assert len(strict.sends) == 1 and strict.titles() == ["LinkedIn post is live"]
    assert strict.receipt_stages() == ["attempt", "placed"]


def test_without_the_flag_a_config_that_does_not_require_approval_posts_as_before(tmp_path, monkeypatch):
    setup = Setup(tmp_path, "[approval]\nrequired = false\n")
    setup.install_fake_linkedin(monkeypatch)
    setup.post()
    assert cli.cmd_publish(setup.cfg, SLOT_PLUS_5, "post", None) == 0
    assert len(setup.sends) == 1
    assert setup.titles() == ["LinkedIn post is live"] and setup.receipt_stages() == ["attempt", "placed"]
    assert setup.cfg.heartbeat_file.exists()
