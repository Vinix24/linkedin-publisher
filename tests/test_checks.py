"""Checks before sending: a post goes out only when every address it names answers as promised."""
import datetime as dt
import socket

import pytest
from conftest import Setup

from linkedin_publisher import cli
from linkedin_publisher import postqueue as queue

REQUIRED = "[approval]\nrequired = true\n"
SLOT = dt.datetime(2026, 9, 23, 10, 0)
SLOT_PLUS_5 = SLOT + dt.timedelta(minutes=5)
SLOT_PLUS_20 = SLOT + dt.timedelta(minutes=20)
TITLE_CHECK = "LinkedIn post held back: a check failed"


def block(*lines: str) -> str:
    """A frontmatter block scalar, one check per line."""
    return "|\n" + "\n".join(f"  {line}" for line in lines)


@pytest.fixture
def shop(tmp_path, monkeypatch):
    setup = Setup(tmp_path, "[checks]\ntimeout_seconds = 5\n")
    setup.install_fake_linkedin(monkeypatch)
    return setup


def run_scheduled(setup: Setup, now: dt.datetime = SLOT_PLUS_5) -> int:
    return cli.cmd_publish(setup.cfg, now, "post", None)


def claims_dir(setup: Setup):
    return setup.cfg.receipts_dir / "claims"


# ------------------------------------------------------------------ the main promise

def test_a_failing_check_blocks_the_send_alerts_once_and_a_later_pass_sends(shop, site):
    url = site.serve("/blog", "the blog", status=503)
    shop.post("launch.md", {"checks": block(url)})
    assert run_scheduled(shop, SLOT_PLUS_5) == 3
    assert run_scheduled(shop, SLOT_PLUS_20) == 0                     # the same failure is not reported again
    assert shop.sends == []
    assert shop.titles() == [TITLE_CHECK]
    message = shop.notifications()[0][1]
    assert "launch" in message and "2026-09-23 10:00" in message and url in message and "503" in message
    assert shop.receipt_stages() == ["check-failed"]
    assert (shop.cfg.queue_dir / "launch.md").exists()

    site.serve("/blog", "the blog")                                   # the blip is over
    assert run_scheduled(shop, dt.datetime(2026, 9, 23, 10, 35)) == 0
    assert len(shop.sends) == 1
    assert shop.receipt_stages() == ["check-failed", "attempt", "placed"]
    assert shop.titles() == [TITLE_CHECK, "LinkedIn post is live"]
    assert len(site.hits) == 3                                        # one request per run, none after the send


def test_a_failing_post_does_not_hold_up_the_next_due_post(shop, site):
    bad = site.serve("/bad", status=500)
    good = site.serve("/good", "fine")
    shop.post("a.md", {"slot_time": '"09:00"', "checks": block(bad)}, body="Blocked.")
    shop.post("b.md", {"slot_time": '"09:30"', "checks": block(good)}, body="Fine.")
    assert run_scheduled(shop) == 3
    assert [s["commentary"] for s in shop.sends] == ["Fine."]
    assert shop.titles() == [TITLE_CHECK, "LinkedIn post is live"]
    assert (shop.cfg.queue_dir / "a.md").exists() and not (shop.cfg.queue_dir / "b.md").exists()


def test_the_checks_of_a_second_due_post_are_not_asked_when_the_first_one_goes_out(shop, site):
    first = site.serve("/first", "ok")
    second = site.serve("/second", "ok")
    shop.post("a.md", {"slot_time": '"09:00"', "checks": block(first)}, body="One.")
    shop.post("b.md", {"slot_time": '"09:30"', "checks": block(second)}, body="Two.")
    run_scheduled(shop)
    assert site.hits == ["/first"]


def test_every_check_of_the_post_is_asked_and_every_failure_is_named(shop, site):
    one = site.serve("/one", status=404)
    two = site.serve("/two", status=500)
    fine = site.serve("/fine", "ok")
    shop.post("a.md", {"checks": block(one, fine, two)})
    assert run_scheduled(shop) == 3
    assert sorted(site.hits) == ["/fine", "/one", "/two"]
    assert len(shop.titles()) == 2
    named = " ".join(n[1] for n in shop.notifications())
    assert one in named and two in named and fine not in named
    run_scheduled(shop, SLOT_PLUS_20)
    assert len(shop.titles()) == 2


# ------------------------------------------------------------------- what counts as passing

def test_required_text_must_be_in_the_page(shop, site):
    url = site.serve("/seopera", "<h1>Join the waiting list</h1>")
    shop.post("a.md", {"checks": block(f"{url} | the waiting list")})
    run_scheduled(shop)
    assert len(shop.sends) == 1


def test_a_missing_required_text_blocks_the_send(shop, site):
    url = site.serve("/seopera", "<h1>Now live, sign up</h1>")
    shop.post("a.md", {"checks": block(f"{url} | the waiting list")})
    assert run_scheduled(shop) == 3
    assert shop.sends == []
    message = shop.notifications()[0][1]
    assert "the waiting list" in message and url in message


def test_the_required_text_is_matched_exactly_and_in_utf8(shop, site):
    url = site.serve("/page", "A café menu")
    shop.post("a.md", {"checks": block(f"{url} | café")})
    run_scheduled(shop)
    assert len(shop.sends) == 1


def test_a_page_may_declare_another_charset(shop, site):
    url = site.url("/page")
    site.pages["/page"] = (200, "A café menu".encode("iso-8859-1"),
                           {"Content-Type": "text/html; charset=iso-8859-1"})
    shop.post("a.md", {"checks": block(f"{url} | café")})
    run_scheduled(shop)
    assert len(shop.sends) == 1


def test_redirects_are_followed_and_the_final_page_counts(shop, site):
    site.serve("/final", "arrived")
    start = site.redirect("/start", "/final")
    shop.post("a.md", {"checks": block(f"{start} | arrived")})
    run_scheduled(shop)
    assert len(shop.sends) == 1 and site.hits == ["/start", "/final"]


def test_a_redirect_to_a_missing_page_fails(shop, site):
    start = site.redirect("/start", "/gone")
    shop.post("a.md", {"checks": block(start)})
    assert run_scheduled(shop) == 3 and shop.sends == []
    assert "404" in shop.notifications()[0][1]


@pytest.mark.parametrize("status", [204, 301, 404, 410, 500, 503])
def test_only_a_final_200_passes(shop, site, status):
    url = site.serve("/page", "text", status=status)
    shop.post("a.md", {"checks": block(url)})
    run_scheduled(shop)
    assert shop.sends == []
    assert str(status) in shop.notifications()[0][1]


def test_a_redirect_loop_fails(shop, site):
    one = site.redirect("/one", "/two")
    site.redirect("/two", "/one")
    shop.post("a.md", {"checks": block(one)})
    run_scheduled(shop)
    assert shop.sends == [] and shop.titles() == [TITLE_CHECK]


def test_a_site_that_cannot_be_reached_fails_and_names_the_check(shop):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]                                  # closed again when the block ends
    url = f"http://127.0.0.1:{port}/blog"
    shop.post("a.md", {"checks": block(url)})
    assert run_scheduled(shop) == 3
    assert shop.sends == [] and url in shop.notifications()[0][1]


def test_a_site_that_answers_too_slowly_fails(tmp_path, monkeypatch, site):
    setup = Setup(tmp_path, "[checks]\ntimeout_seconds = 0.5\n")
    setup.install_fake_linkedin(monkeypatch)
    url = site.serve("/slow", "late")
    site.delays["/slow"] = 3
    setup.post("a.md", {"checks": block(url)})
    assert run_scheduled(setup) == 3
    assert setup.sends == []
    assert "timed out" in setup.notifications()[0][1]


@pytest.mark.parametrize("line", [
    "not an address",
    "ftp://example.org/file",
    "example.org/no-scheme",
    "http://",
    "http://127.0.0.1:1/x |",
    "http://127.0.0.1:1/x | ",
    "http://127.0.0.1:1/x and more words",
])
def test_a_line_that_cannot_be_read_fails_closed(shop, site, line):
    good = site.serve("/good", "ok")
    shop.post("a.md", {"checks": block(good, line)})
    assert run_scheduled(shop) == 3
    assert shop.sends == []
    assert shop.titles() == [TITLE_CHECK]
    assert line.strip() in shop.notifications()[0][1] and "cannot be read" in shop.notifications()[0][1]
    run_scheduled(shop, SLOT_PLUS_20)
    assert shop.titles() == [TITLE_CHECK]


def test_a_folded_block_joins_the_lines_and_fails_closed(shop, site):
    one = site.serve("/one", "ok")
    two = site.serve("/two", "ok")
    shop.post("a.md", {"checks": f">\n  {one}\n  {two}"})
    run_scheduled(shop)
    assert shop.sends == [] and site.hits == []


def test_blank_lines_are_ignored_and_a_single_line_value_is_one_check(shop, site):
    url = site.serve("/page", "ok")
    shop.post("a.md", {"checks": f"|\n  {url}\n\n  {url} | ok\n"})
    run_scheduled(shop)
    assert len(shop.sends) == 1 and site.hits == ["/page", "/page"]


# ----------------------------------------------------------- when the checks run

def test_checks_run_after_the_approval_filter_and_only_for_posts_that_pass_it(tmp_path, monkeypatch, site):
    setup = Setup(tmp_path, REQUIRED)
    setup.install_fake_linkedin(monkeypatch)
    url = site.serve("/page", "ok")
    setup.post("a.md", {"checks": block(url)})
    run_scheduled(setup)
    assert site.hits == [] and setup.titles() == ["LinkedIn post needs approval"]
    assert setup.run("approve", "--file", str(setup.cfg.queue_dir / "a.md"), "--by", "x") == 0
    run_scheduled(setup)
    assert site.hits == ["/page"] and len(setup.sends) == 1


def test_a_check_is_not_part_of_the_approval(tmp_path, monkeypatch, site):
    from linkedin_publisher import approval
    setup = Setup(tmp_path, REQUIRED)
    setup.install_fake_linkedin(monkeypatch)
    url = site.serve("/page", "ok")
    path = setup.post("a.md", {"checks": block(url)})
    setup.run("approve", "--file", str(path), "--by", "x")
    path.write_text(path.read_text().replace(url, url + "?other=1"))
    assert approval.check(queue.read_post(path), setup.cfg)[0] == approval.APPROVED


@pytest.mark.parametrize("when", ["future", "stranded", "draft"])
def test_checks_run_only_for_posts_that_are_actually_due(shop, site, when):
    url = site.serve("/page", "ok")
    now = SLOT_PLUS_5
    meta = {"checks": block(url)}
    if when == "future":
        now = SLOT - dt.timedelta(hours=1)
    elif when == "stranded":
        now = SLOT + dt.timedelta(minutes=91)
    else:
        meta["status"] = "draft"
    shop.post("a.md", meta)
    run_scheduled(shop, now)
    assert site.hits == [] and shop.sends == []


def test_a_post_that_is_already_placed_is_not_checked(shop, site):
    url = site.serve("/page", "ok")
    path = shop.post("a.md", {"checks": block(url)})
    queue.write_receipt(shop.cfg, "placed", queue.read_post(path))
    run_scheduled(shop)
    assert site.hits == []


def test_a_failed_check_happens_before_the_claim_and_before_the_attempt(shop, site):
    url = site.serve("/page", status=500)
    shop.post("a.md", {"checks": block(url)})
    run_scheduled(shop)
    assert not claims_dir(shop).exists() or list(claims_dir(shop).iterdir()) == []
    assert "attempt" not in shop.receipt_stages()


def test_a_notify_run_does_not_run_the_checks(shop, site):
    url = site.serve("/page", status=500)
    shop.post("a.md", {"checks": block(url)})
    cli.cmd_publish(shop.cfg, SLOT_PLUS_5, "notify", None)
    assert site.hits == [] and shop.titles() == ["LinkedIn post is due"]


def test_publish_file_is_a_person_deciding_and_runs_no_checks(shop, site):
    url = site.serve("/page", status=500)
    path = shop.post("a.md", {"checks": block(url)})
    assert shop.run("publish", "--post", "--file", str(path)) == 0
    assert len(shop.sends) == 1 and site.hits == []


def test_due_and_the_dry_run_show_the_checks_without_asking(shop, site, capsys):
    url = site.serve("/page", status=500)
    shop.post("a.md", {"checks": block(url, f"{url}2 | some text")})
    cli.cmd_due(shop.cfg, SLOT_PLUS_5)
    out = capsys.readouterr().out
    assert url in out and f"{url}2 | some text" in out
    cli.cmd_publish(shop.cfg, SLOT_PLUS_5, "dry", None)
    out = capsys.readouterr().out
    assert url in out and f"{url}2 | some text" in out
    assert site.hits == [] and shop.sends == []


def test_check_field_name_comes_from_the_config(tmp_path, monkeypatch, site):
    setup = Setup(tmp_path, frontmatter='checks_field = "prechecks"\n')
    setup.install_fake_linkedin(monkeypatch)
    url = site.serve("/page", status=500)
    setup.post("a.md", {"prechecks": block(url)})
    setup.post("b.md", {"slot_time": '"10:01"', "checks": block(url)}, body="Plain text, the default name is not a check now.")
    run_scheduled(setup, SLOT_PLUS_20)
    assert [s["commentary"] for s in setup.sends] == ["Plain text, the default name is not a check now."]
    assert site.hits == ["/page"]


def test_a_different_reason_for_the_same_check_is_reported_again(shop, site):
    url = site.serve("/page", "ok", status=503)
    shop.post("a.md", {"checks": block(f"{url} | needle")})
    run_scheduled(shop, SLOT_PLUS_5)
    site.serve("/page", "no luck")                                    # up again, but the text is gone
    run_scheduled(shop, SLOT_PLUS_20)
    run_scheduled(shop, SLOT_PLUS_20)
    assert shop.titles() == [TITLE_CHECK, TITLE_CHECK]


def test_a_failed_check_changes_no_outcome(shop, site):
    url = site.serve("/page", status=500)
    path = shop.post("a.md", {"checks": block(url)})
    run_scheduled(shop)
    assert queue.load_states(shop.cfg) == {}
    ready, _ = queue.collect(shop.cfg, SLOT_PLUS_5)
    assert [p.path.name for p in ready] == [path.name]


# ------------------------------------------------------ a job that wakes up late

def late_chain(tmp_path, monkeypatch, site) -> Setup:
    setup = Setup(tmp_path, REQUIRED + "[checks]\ntimeout_seconds = 5\n")
    setup.install_fake_linkedin(monkeypatch)
    url = site.serve("/page", "ok")
    path = setup.post("a.md", {"checks": block(url)})
    setup.run("approve", "--file", str(path), "--by", "x")
    return setup


@pytest.mark.parametrize("minutes", [1, 45, 89, 90])
def test_a_late_job_places_the_post_inside_the_grace_period_after_its_checks(tmp_path, monkeypatch, site, minutes):
    setup = late_chain(tmp_path, monkeypatch, site)
    assert run_scheduled(setup, SLOT + dt.timedelta(minutes=minutes)) == 0
    assert len(setup.sends) == 1 and site.hits == ["/page"]
    assert setup.receipt_stages() == ["attempt", "placed"]


@pytest.mark.parametrize("minutes", [91, 240])
def test_a_late_job_beyond_the_grace_period_only_reports_and_asks_nothing(tmp_path, monkeypatch, site, minutes):
    setup = late_chain(tmp_path, monkeypatch, site)
    late = SLOT + dt.timedelta(minutes=minutes)
    assert run_scheduled(setup, late) == 3
    assert run_scheduled(setup, late) == 0
    assert setup.sends == [] and site.hits == []
    assert setup.receipt_stages() == ["stranded"]
    assert setup.titles() == ["LinkedIn post missed its slot"]
    assert (setup.cfg.queue_dir / "a.md").exists()


def test_a_blip_that_lasts_into_the_grace_period_recovers_and_one_that_outlasts_it_is_reported(tmp_path, monkeypatch, site):
    setup = late_chain(tmp_path, monkeypatch, site)
    site.serve("/page", status=503)
    for minutes in (5, 20, 35, 50, 65, 80):                           # every 15 minutes, all inside the window
        run_scheduled(setup, SLOT + dt.timedelta(minutes=minutes))
    assert setup.sends == [] and setup.titles() == [TITLE_CHECK]
    assert run_scheduled(setup, SLOT + dt.timedelta(minutes=95)) == 3
    assert setup.titles() == [TITLE_CHECK, "LinkedIn post missed its slot"]
    site.serve("/page", "ok")
    run_scheduled(setup, SLOT + dt.timedelta(minutes=110))
    assert setup.sends == []
