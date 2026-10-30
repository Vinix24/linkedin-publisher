"""Precheck: look at the posts of the coming days and say what would stop them, without touching anything."""
import datetime as dt
import json

import pytest
from conftest import Setup, snapshot

from linkedin_publisher import approval, claims, cli
from linkedin_publisher import postqueue as queue

NOW = dt.datetime(2026, 9, 22, 17, 0)               # the evening before the slot
NEXT_MORNING = dt.datetime(2026, 9, 23, 8, 0)
SLOT_PLUS_5 = dt.datetime(2026, 9, 23, 10, 5)
TOML = """\
[approval]
required = true
external_route_field = "route_note"
external_route_text = "runs via a cron"

[checks]
timeout_seconds = 5
"""
REQUIRED_ONLY = "[approval]\nrequired = true\n"
TITLE_PRECHECK = "LinkedIn precheck: a post will not go out as planned"
JSON_POST_KEYS = {"post", "slot_date", "slot_time", "route", "checked", "problems"}
ROUTE = {"route_note": '"This one runs via a cron in a session"'}


@pytest.fixture
def ready(tmp_path):
    return Setup(tmp_path, TOML)


@pytest.fixture
def publishing(tmp_path, monkeypatch):
    setup = Setup(tmp_path, TOML)
    setup.install_fake_linkedin(monkeypatch)
    return setup


def block(*lines: str) -> str:
    return "|\n" + "\n".join(f"  {line}" for line in lines)


def approve(setup: Setup, name: str = "a.md") -> None:
    approval.approve(setup.cfg, setup.cfg.queue_dir / name, "Test Person")      # quiet: no output to mix with JSON


def approved_post(setup: Setup, name: str = "a.md", meta: dict | None = None, body: str = "Hello world."):
    path = setup.post(name, meta, body=body)
    approve(setup, name)
    return path


def precheck(setup: Setup, now: dt.datetime = NOW, days_ahead: int = 1, as_json: bool = False) -> int:
    return cli.cmd_precheck(setup.cfg, now, days_ahead, as_json)


def run_scheduled(setup: Setup, now: dt.datetime = SLOT_PLUS_5) -> int:
    return cli.cmd_publish(setup.cfg, now, "post", None)


def report(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def problems(data: dict, name: str = "a.md") -> list[dict]:
    return next(p["problems"] for p in data["posts"] if p["post"] == name)


def kinds(data: dict, name: str = "a.md") -> list[str]:
    return [p["kind"] for p in problems(data, name)]


# ------------------------------------------------------------------------ a post in order

def test_a_post_that_is_in_order_has_no_problem_and_no_alert(ready, capsys):
    approved_post(ready)
    assert precheck(ready) == 0
    out = capsys.readouterr().out
    assert "a.md" in out and "2026-09-23 10:00" in out and "ok" in out
    assert ready.notifications() == [] and ready.receipt_stages() == []


def test_with_nothing_in_range_it_says_so_and_exits_0(ready, capsys):
    assert precheck(ready) == 0
    assert "No posts" in capsys.readouterr().out


# --------------------------------------------------------------------------- approval

def test_a_post_without_approval_is_a_problem_with_one_alert_per_day(ready, capsys):
    ready.post("launch.md")
    assert precheck(ready) == 3
    out = capsys.readouterr().out
    assert "launch.md" in out and "approval" in out
    assert ready.titles() == [TITLE_PRECHECK]
    message = ready.notifications()[0][1]
    assert "launch" in message and "2026-09-23 10:00" in message
    assert "approve --file" in message and "launch.md" in message
    assert ready.receipt_stages() == ["precheck"]

    assert precheck(ready, NOW + dt.timedelta(minutes=30)) == 3             # still wrong, still reported ...
    assert "approval" in capsys.readouterr().out
    assert ready.titles() == [TITLE_PRECHECK]                               # ... but alerted once for the day

    assert precheck(ready, NEXT_MORNING, 0) == 3                            # the morning run reminds again
    assert ready.titles() == [TITLE_PRECHECK, TITLE_PRECHECK]
    assert ready.receipt_stages() == ["precheck", "precheck"]


def test_an_approval_for_another_version_is_a_problem(ready, capsys):
    path = approved_post(ready)
    path.write_text(path.read_text().replace("Hello world.", "Hello, world."))
    assert precheck(ready, as_json=True) == 3
    data = report(capsys)
    assert kinds(data) == ["approval-mismatch"]
    assert "different version" in problems(data)[0]["message"]


def test_approval_is_not_asked_for_when_it_is_not_required(tmp_path, capsys):
    setup = Setup(tmp_path)
    setup.post()
    assert precheck(setup) == 0
    assert "ok" in capsys.readouterr().out and setup.notifications() == []


def test_a_measure_line_that_does_not_match_is_named(tmp_path, capsys):
    setup = Setup(tmp_path, TOML)
    setup.post("a.md", {"measure": "count", "measure_line": '"There are {n} items left."'},
               body="Nothing is left.")
    assert precheck(setup, as_json=True) == 3
    data = report(capsys)
    assert kinds(data) == ["measure-line"]
    assert "measure_line" in problems(data)[0]["message"]


def test_a_measure_line_with_a_valid_approval_is_in_order(tmp_path, capsys):
    setup = Setup(tmp_path, TOML)
    approved_post(setup, meta={"measure": "count", "measure_line": '"There are {n} items left."'},
                  body="There are 53 items left.")
    assert precheck(setup) == 0


# ----------------------------------------------------------------- media and content

@pytest.mark.parametrize("meta, needle", [
    ({"format": "image", "alt_text": "A chart"}, "image"),
    ({"format": "video"}, ".mp4"),
    ({"format": "document", "document_title": "Notes"}, ".pdf"),
])
def test_a_missing_media_file_is_a_problem(tmp_path, capsys, meta, needle):
    setup = Setup(tmp_path)
    setup.post("launch.md", meta)
    assert precheck(setup, as_json=True) == 3
    data = report(capsys)
    assert kinds(data, "launch.md") == ["media"]
    assert needle in problems(data, "launch.md")[0]["message"]
    assert setup.titles() == [TITLE_PRECHECK] and "launch" in setup.notifications()[0][1]


def test_media_that_exists_is_fine(tmp_path):
    setup = Setup(tmp_path)
    path = setup.post("a.md", {"format": "image", "alt_text": "A chart"})
    path.with_suffix(".png").write_bytes(b"PNG")
    assert precheck(setup) == 0


def test_media_that_disappeared_after_the_approval_is_both_a_media_and_an_approval_problem(ready, capsys):
    path = ready.post("a.md", {"format": "image", "alt_text": "A chart"})
    path.with_suffix(".png").write_bytes(b"PNG")
    approve(ready)
    path.with_suffix(".png").unlink()
    assert precheck(ready, as_json=True) == 3
    assert sorted(kinds(report(capsys))) == ["approval-mismatch", "media"]


def test_text_that_the_preflight_would_refuse_is_a_content_problem(tmp_path, capsys):
    setup = Setup(tmp_path)
    path = setup.post("a.md", {"format": "image", "alt_text": '""'})
    path.with_suffix(".png").write_bytes(b"PNG")
    assert precheck(setup, as_json=True) == 3
    data = report(capsys)
    assert kinds(data) == ["content"] and "alt_text" in problems(data)[0]["message"]


# ------------------------------------------------------------------------------ expiry

def test_a_post_that_expires_before_its_slot_is_a_problem(ready, capsys):
    approved_post(ready, meta={"expires": "2026-09-22"})                  # still true tonight, gone at 10:00 tomorrow
    assert precheck(ready, as_json=True) == 3
    data = report(capsys)
    assert kinds(data) == ["expired"]
    assert "2026-09-22" in problems(data)[0]["message"]


def test_a_post_that_expires_on_its_slot_day_is_fine(ready):
    approved_post(ready, meta={"expires": "2026-09-23"})
    assert precheck(ready) == 0


def test_the_expiry_field_name_comes_from_the_config(tmp_path, capsys):
    setup = Setup(tmp_path, TOML, frontmatter='expires_key = "valid_until"\n')
    approved_post(setup, meta={"valid_until": "2026-09-21"})
    assert precheck(setup, as_json=True) == 3
    assert kinds(report(capsys)) == ["expired"]


# ------------------------------------------------------------------------------ checks

def test_a_failing_check_is_a_problem_and_a_passing_one_is_not(ready, site, capsys):
    bad = site.serve("/bad", status=503)
    good = site.serve("/good", "ok")
    approved_post(ready, "a.md", {"checks": block(bad)}, body="One.")
    approved_post(ready, "b.md", {"checks": block(f"{good} | ok")}, body="Two.")
    assert precheck(ready, as_json=True) == 3
    data = report(capsys)
    assert kinds(data, "a.md") == ["check-failed"] and kinds(data, "b.md") == []
    assert bad in problems(data)[0]["message"] and "503" in problems(data)[0]["message"]
    assert ready.titles() == [TITLE_PRECHECK]
    assert bad in ready.notifications()[0][1]


def test_a_check_whose_text_is_missing_is_a_problem(ready, site, capsys):
    url = site.serve("/page", "nothing useful")
    approved_post(ready, meta={"checks": block(f"{url} | the waiting list")})
    assert precheck(ready, as_json=True) == 3
    assert "the waiting list" in problems(report(capsys))[0]["message"]


def test_a_check_line_that_cannot_be_read_is_a_problem(ready, capsys):
    approved_post(ready, meta={"checks": block("not an address")})
    assert precheck(ready, as_json=True) == 3
    data = report(capsys)
    assert kinds(data) == ["check-failed"] and "cannot be read" in problems(data)[0]["message"]


def test_every_problem_of_a_post_is_listed_and_alerted_on_its_own(ready, site, capsys):
    bad = site.serve("/bad", status=500)
    ready.post("a.md", {"expires": "2026-09-01", "checks": block(bad)})
    assert precheck(ready, as_json=True) == 3
    assert sorted(kinds(report(capsys))) == ["approval-missing", "check-failed", "expired"]
    assert len(ready.titles()) == 3
    precheck(ready)
    assert len(ready.titles()) == 3


# --------------------------------------------------------------------- the external route

def test_a_post_on_the_external_route_is_shown_but_never_checked_or_alerted(ready, site, capsys):
    bad = site.serve("/bad", status=500)
    ready.post("cron.md", {"route_note": '"This one runs via a cron in a session"', "format": "image",
                           "expires": "2026-01-01", "checks": block(bad)})
    assert precheck(ready) == 0
    out = capsys.readouterr().out
    assert "cron.md" in out and "route: external, not checked" in out
    assert ready.notifications() == [] and ready.receipt_stages() == []
    assert site.hits == []
    precheck(ready, as_json=True)
    data = report(capsys)
    assert data["posts"] == [{"post": "cron.md", "slot_date": "2026-09-23", "slot_time": "10:00",
                              "route": "external", "checked": False, "problems": []}]


def test_the_external_text_must_be_in_the_configured_field(ready, capsys):
    ready.post("a.md", {"other_field": '"this runs via a cron"'})
    ready.post("b.md", {"route_note": '"this runs via a cron"'}, body="Another post.")
    assert precheck(ready, as_json=True) == 3
    data = report(capsys)
    routes = {p["post"]: p["route"] for p in data["posts"]}
    assert routes == {"a.md": "publisher", "b.md": "external"}
    assert kinds(data, "a.md") == ["approval-missing"] and kinds(data, "b.md") == []


def test_without_an_external_route_in_the_config_nothing_is_external(tmp_path, capsys):
    setup = Setup(tmp_path, REQUIRED_ONLY)
    setup.post("a.md", {"route_note": '"this runs via a cron"'})
    assert precheck(setup, as_json=True) == 3
    assert report(capsys)["posts"][0]["route"] == "publisher"


# ------------------------------------------------ an approval in the file means this tool is the route

def test_an_approved_post_that_still_carries_the_route_text_is_checked_as_the_publisher(ready, site, capsys):
    bad = site.serve("/bad", status=500)
    good = site.serve("/good", "ok")
    approved_post(ready, "a.md", {**ROUTE, "checks": block(bad)}, body="One.")
    approved_post(ready, "b.md", {**ROUTE, "checks": block(good)}, body="Two.")
    assert precheck(ready, as_json=True) == 3
    data = report(capsys)
    assert {p["post"]: (p["route"], p["checked"]) for p in data["posts"]} == \
        {"a.md": ("publisher", True), "b.md": ("publisher", True)}
    assert kinds(data, "a.md") == ["check-failed"] and kinds(data, "b.md") == []
    assert sorted(site.hits) == ["/bad", "/good"]
    assert ready.titles() == [TITLE_PRECHECK]


def test_an_approved_post_with_the_route_text_is_not_reported_as_external_in_the_text_output(ready, capsys):
    approved_post(ready, meta=ROUTE)
    assert precheck(ready) == 0
    out = capsys.readouterr().out
    assert "ok" in out and "external" not in out


def test_a_mismatched_approval_on_a_post_with_the_route_text_is_reported(ready, capsys):
    path = approved_post(ready, meta=ROUTE)
    path.write_text(path.read_text().replace("Hello world.", "Hello, world."))
    assert precheck(ready, as_json=True) == 3
    data = report(capsys)
    assert (data["posts"][0]["route"], data["posts"][0]["checked"]) == ("publisher", True)
    assert kinds(data) == ["approval-mismatch"]


def test_a_key_alone_next_to_the_route_text_still_means_the_tool_is_the_route(ready, capsys):
    ready.post("a.md", {**ROUTE, "approved_key": '"' + "0" * 64 + '"'})
    assert precheck(ready, as_json=True) == 3
    data = report(capsys)
    assert data["posts"][0]["route"] == "publisher" and kinds(data) == ["approval-missing"]


def test_an_empty_key_is_no_approval_and_the_post_stays_external(ready, capsys):
    ready.post("a.md", {**ROUTE, "approved_key": '""'})
    assert precheck(ready, as_json=True) == 0
    assert report(capsys)["posts"][0]["route"] == "external"


def test_an_approval_with_another_key_field_name_counts_too(tmp_path, capsys):
    setup = Setup(tmp_path, TOML, frontmatter='approved_key_field = "signed_off_key"\n')
    approved_post(setup, meta=ROUTE)
    assert "signed_off_key" in (setup.cfg.queue_dir / "a.md").read_text()
    assert precheck(setup, as_json=True) == 0
    assert report(capsys)["posts"][0]["route"] == "publisher"


# ------------------------------------------- a scheduled run and a post on the external route

def test_a_scheduled_run_leaves_posts_on_the_external_route_alone_without_any_alert(publishing, capsys):
    for index, slot_time in enumerate(("09:00", "09:30", "10:00")):
        publishing.post(f"cron{index}.md", {**ROUTE, "slot_time": f'"{slot_time}"'}, body=f"Runs elsewhere {index}.")
    assert run_scheduled(publishing) == 0
    assert run_scheduled(publishing) == 0
    assert publishing.sends == []
    assert publishing.notifications() == [] and publishing.receipt_stages() == []
    assert not (publishing.cfg.receipts_dir / "claims").exists()
    out = capsys.readouterr().out
    assert "another route" in out and "approv" not in out
    log = publishing.cfg.log_file.read_text()
    assert all(f"cron{index}.md" in log for index in range(3)) and "another route" in log
    assert all((publishing.cfg.queue_dir / f"cron{index}.md").exists() for index in range(3))


def test_an_external_post_does_not_hold_up_an_approved_post_in_the_same_run(publishing):
    publishing.post("cron.md", {**ROUTE, "slot_time": '"09:00"'}, body="Runs elsewhere.")
    approved_post(publishing, "b.md", {"slot_time": '"09:30"'}, body="Approved.")
    assert run_scheduled(publishing) == 0
    assert [s["commentary"] for s in publishing.sends] == ["Approved."]
    assert publishing.titles() == ["LinkedIn post is live"]
    assert publishing.receipt_stages() == ["attempt", "placed"]
    assert (publishing.cfg.queue_dir / "cron.md").exists()


def test_an_approved_post_that_carries_the_route_text_is_sent(publishing):
    approved_post(publishing, meta=ROUTE)
    assert run_scheduled(publishing) == 0
    assert [s["commentary"] for s in publishing.sends] == ["Hello world."]
    assert publishing.titles() == ["LinkedIn post is live"]


def test_a_mismatched_approval_on_a_post_with_the_route_text_is_held_and_reported(publishing):
    path = approved_post(publishing, meta=ROUTE)
    path.write_text(path.read_text().replace("Hello world.", "Hello, world."))
    assert run_scheduled(publishing) == 3
    assert publishing.sends == []
    assert publishing.titles() == ["LinkedIn post: approval belongs to a different version"]
    assert publishing.receipt_stages() == ["approval-mismatch"]


def test_the_held_back_line_does_not_count_an_external_post_as_unapproved(publishing, capsys):
    publishing.post("cron.md", {**ROUTE, "slot_time": '"09:00"'}, body="Runs elsewhere.")
    assert run_scheduled(publishing) == 0
    assert "1 due post(s) run through another route" in capsys.readouterr().out
    publishing.post("b.md", {"slot_time": '"09:30"'}, body="Not approved.")
    assert run_scheduled(publishing) == 3
    out = capsys.readouterr().out
    assert "1 without a valid approval" in out and "1 on another route" in out
    assert publishing.titles() == ["LinkedIn post needs approval"]        # b.md only


def test_with_nothing_due_the_run_still_says_so(publishing, capsys):
    assert run_scheduled(publishing) == 0
    assert "Nothing is due." in capsys.readouterr().out


def test_due_and_the_dry_run_mark_a_post_on_the_external_route(publishing, capsys):
    external = publishing.post("cron.md", ROUTE, body="Runs elsewhere.")
    approved_post(publishing, "b.md", ROUTE, body="Approved here.")
    publishing.post("c.md", {}, body="Nobody looked at this.")
    cli.cmd_due(publishing.cfg, SLOT_PLUS_5)
    out = capsys.readouterr().out
    assert "  cron.md  (slot 2026-09-23 10:00, text)  [external route]" in out
    assert "  b.md  (slot 2026-09-23 10:00, text)  [approved]" in out
    assert "  c.md  (slot 2026-09-23 10:00, text)  [not approved]" in out
    assert cli.cmd_publish(publishing.cfg, SLOT_PLUS_5, "dry", external) == 0
    out = capsys.readouterr().out
    assert "DRY RUN cron.md" in out and "[external route]" in out and "not approved" not in out


def test_without_required_approval_the_note_stays_empty(tmp_path, capsys):
    setup = Setup(tmp_path, '[approval]\nexternal_route_field = "route_note"\n'
                            'external_route_text = "runs via a cron"\n')
    setup.post("cron.md", ROUTE)
    cli.cmd_due(setup.cfg, SLOT_PLUS_5)
    assert "external route" not in capsys.readouterr().out


# ------------------------------------------------------------------- which posts are looked at

def test_only_queued_posts_inside_the_range_and_before_their_slot_are_looked_at(ready, capsys):
    ready.post("past.md", {"slot": "2026-09-22", "slot_time": '"09:00"'}, body="Past.")
    ready.post("tonight.md", {"slot": "2026-09-22", "slot_time": '"18:00"'}, body="Tonight.")
    ready.post("tomorrow.md", {}, body="Tomorrow.")
    ready.post("later.md", {"slot": "2026-09-24"}, body="Later.")
    ready.post("draft.md", {"status": "draft"}, body="Draft.")
    placed = ready.post("placed.md", {}, body="Placed.")
    queue.write_receipt(ready.cfg, "placed", queue.read_post(placed))
    precheck(ready, as_json=True)
    assert [p["post"] for p in report(capsys)["posts"]] == ["tonight.md", "tomorrow.md"]
    precheck(ready, NOW, 0, as_json=True)
    assert [p["post"] for p in report(capsys)["posts"]] == ["tonight.md"]
    precheck(ready, NOW, 2, as_json=True)
    assert [p["post"] for p in report(capsys)["posts"]] == ["tonight.md", "tomorrow.md", "later.md"]


def test_a_post_exactly_at_its_slot_is_past_it(ready, capsys):
    ready.post("a.md", {})
    precheck(ready, dt.datetime(2026, 9, 23, 10, 0), 0, as_json=True)
    assert report(capsys)["posts"] == []


def test_a_claim_or_an_unclear_attempt_that_stops_the_post_is_a_problem(ready, capsys):
    approved_post(ready, "a.md", body="Claimed.")
    claims.take(ready.cfg, queue.content_key("Claimed."), "a.md")
    path = approved_post(ready, "b.md", body="Unclear.")
    queue.write_receipt(ready.cfg, "uncertain", queue.read_post(path))
    assert precheck(ready, as_json=True) == 3
    data = report(capsys)
    assert kinds(data, "a.md") == ["blocked"] and kinds(data, "b.md") == ["blocked"]


def test_a_measure_post_whose_file_a_claim_names_under_another_key_is_blocked(ready, capsys):
    meta = {"measure": "count", "measure_line": '"There are {n} items left."'}
    approved_post(ready, "a.md", meta, body="There are 49 items left.")           # edited by hand after the attempt
    claims.take(ready.cfg, queue.content_key("There are 53 items left."), "a.md")
    approved_post(ready, "b.md", body="No measure line.")
    claims.take(ready.cfg, queue.content_key("Some earlier text."), "b.md")
    assert precheck(ready, as_json=True) == 3
    data = report(capsys)
    assert kinds(data, "a.md") == ["blocked"] and kinds(data, "b.md") == []
    assert "--retry" in problems(data, "a.md")[0]["message"] and "a.md" in problems(data, "a.md")[0]["message"]
    assert ready.titles() == [TITLE_PRECHECK]


# ------------------------------------------------------------------------------ json

def test_the_json_has_a_stable_shape(ready, capsys):
    approved_post(ready, "ok.md", body="Fine.")
    ready.post("bad.md", {"slot_time": '"11:30"'}, body="Not approved.")
    assert precheck(ready, as_json=True) == 3
    data = report(capsys)
    assert list(data) == ["schema", "days_ahead", "posts"]
    assert data["schema"] == 1 and data["days_ahead"] == 1
    assert [set(p) for p in data["posts"]] == [JSON_POST_KEYS, JSON_POST_KEYS]
    ok, bad = data["posts"]
    assert ok == {"post": "ok.md", "slot_date": "2026-09-23", "slot_time": "10:00", "route": "publisher",
                  "checked": True, "problems": []}
    assert (bad["post"], bad["slot_date"], bad["slot_time"], bad["route"]) == \
        ("bad.md", "2026-09-23", "11:30", "publisher")
    assert [set(p) for p in bad["problems"]] == [{"kind", "message"}]
    assert bad["problems"][0]["kind"] == "approval-missing"


def test_json_output_stays_json_when_the_notification_hook_breaks(tmp_path, capsys):
    setup = Setup(tmp_path, TOML + '\n[notify]\ncommand = ["/no/such/program"]\n', hook=False)
    setup.post("a.md")
    assert precheck(setup, as_json=True) == 3
    captured = capsys.readouterr()
    assert json.loads(captured.out)["posts"][0]["post"] == "a.md"
    assert "notification hook failed" in captured.err


def test_the_posts_come_in_slot_order(ready, capsys):
    ready.post("late.md", {"slot_time": '"15:00"'}, body="Late.")
    ready.post("early.md", {"slot_time": '"08:00"'}, body="Early.")
    ready.post("noon.md", {"slot_time": '"12:00"'}, body="Noon.")
    precheck(ready, as_json=True)
    assert [p["post"] for p in report(capsys)["posts"]] == ["early.md", "noon.md", "late.md"]


# ---------------------------------------------------------- what it must never do

def test_precheck_edits_no_file_in_the_queue_or_the_published_folder(ready, site, capsys):
    bad = site.serve("/bad", status=500)
    ready.post("a.md", {"format": "image", "alt_text": "x", "checks": block(bad)}, body="One.")
    approved_post(ready, "b.md", body="Two.")
    ready.cfg.published_dir.mkdir()
    (ready.cfg.published_dir / "old.md").write_text("old")
    before = {folder: snapshot(folder) for folder in (ready.cfg.queue_dir, ready.cfg.published_dir)}
    assert precheck(ready) == 3
    assert {folder: snapshot(folder) for folder in before} == before
    assert not ready.cfg.lock_file.exists()


def test_precheck_never_sends_and_never_calls_linkedin(tmp_path, site, linkedin_must_stay_silent, capsys):
    setup = Setup(tmp_path, TOML)
    good = site.serve("/good", "ok")
    approved_post(setup, meta={"checks": block(good)})
    setup.post("b.md", body="Unapproved.")
    assert precheck(setup, dt.datetime(2026, 9, 23, 9, 59), 1) == 3
    assert site.hits == ["/good"]
    assert not (setup.cfg.receipts_dir / "claims").exists()
    assert "attempt" not in setup.receipt_stages()


def test_precheck_writes_a_note_that_changes_no_outcome(ready):
    path = ready.post("a.md")
    precheck(ready)
    assert ready.receipt_stages() == ["precheck"]
    assert queue.load_states(ready.cfg) == {}
    ready_posts, _ = queue.collect(ready.cfg, dt.datetime(2026, 9, 23, 10, 5))
    assert [p.path.name for p in ready_posts] == [path.name]


# ----------------------------------------------------------------- the command line

def test_the_command_line_exit_code_and_flags(ready, capsys):
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    ready.post("a.md", {"slot": tomorrow})
    assert ready.run("precheck") == 3
    assert "a.md" in capsys.readouterr().out
    assert ready.run("precheck", "--days-ahead", "0") == 0                  # tomorrow is outside "today only"
    assert "No posts" in capsys.readouterr().out
    assert ready.run("precheck", "--days-ahead", "1", "--json") == 3
    assert json.loads(capsys.readouterr().out)["posts"][0]["problems"][0]["kind"] == "approval-missing"
    approve(ready)
    assert ready.run("precheck") == 0


def test_days_ahead_must_not_be_negative(ready, capsys):
    with pytest.raises(SystemExit) as exit_info:
        ready.run("precheck", "--days-ahead", "-1")
    assert exit_info.value.code == 2
    assert "days-ahead" in capsys.readouterr().err


def test_an_unreadable_queue_is_an_error_and_an_alert(tmp_path):
    setup = Setup(tmp_path, TOML)
    setup.cfg.queue_dir.rmdir()
    assert setup.run("precheck") == 1
    assert setup.titles() == ["LinkedIn queue unreadable"]
