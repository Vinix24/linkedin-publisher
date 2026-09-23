import datetime as dt
import json
import plistlib
from pathlib import Path

import yaml

from linkedin_publisher import cli, cloud, schedule


# ------------------------------------------------------------------ schedule

def test_launchd_plist_is_valid_and_runs_every_15_minutes(tmp_path):
    argv = schedule.command(tmp_path / "publisher.toml", auto=False)
    plist = plistlib.loads(schedule.launchd_plist("x.label", argv, tmp_path / "s.log").encode())
    assert plist["StartInterval"] == 900
    assert plist["ProgramArguments"][-2:] == ["publish", "--notify"]


def test_auto_mode_posts_instead_of_notifying(tmp_path):
    assert schedule.command(tmp_path / "p.toml", auto=True)[-1] == "--post"


def test_job_id_differs_per_config(tmp_path):
    assert schedule.job_id(tmp_path / "a.toml") != schedule.job_id(tmp_path / "b.toml")


def test_crontab_keeps_other_lines_and_replaces_its_own():
    existing = "MAILTO=me\n0 3 * * * backup.sh\n*/15 * * * * old # linkedin-publisher:abc\n"
    new = schedule.crontab_with(existing, "*/15 * * * * new # linkedin-publisher:abc", "abc")
    assert "backup.sh" in new and "MAILTO=me" in new
    assert "old" not in new and new.count("linkedin-publisher:abc") == 1


def test_crontab_remove_leaves_the_rest():
    existing = "0 3 * * * backup.sh\n*/15 * * * * x # linkedin-publisher:abc\n"
    assert schedule.crontab_without(existing, "abc") == "0 3 * * * backup.sh\n"


# --------------------------------------------------------------------- cloud

def test_cron_hours_compresses_ranges():
    assert cloud.cron_hours([4, 5, 6, 20, 21]) == "4-6,20-21"
    assert cloud.cron_hours([7]) == "7"


def test_amsterdam_covers_winter_and_summer_time():
    # 06:00 local is 04:00 UTC in summer, 22:59 local is 21:59 UTC in winter
    assert cloud.utc_hours("Europe/Amsterdam") == list(range(4, 22))


def test_timezone_crossing_midnight_utc():
    assert cloud.cron_hours(cloud.utc_hours("Asia/Tokyo")) == "0-13,21-23"


def test_workflows_are_valid_yaml_with_intact_secrets():
    files = cloud.workflows("Europe/Amsterdam", "0.2.0")
    publish = files[".github/workflows/linkedin-publisher.yml"]
    parsed = yaml.safe_load(publish)
    # YAML 1.1 reads the key `on` as boolean True; GitHub reads it as the string "on".
    triggers = parsed.get("on", parsed.get(True))
    assert triggers["schedule"][0]["cron"] == "0,30 4-21 * * *"
    assert parsed["jobs"]["publish"]["env"]["TZ"] == "Europe/Amsterdam"
    assert "${{ secrets.LINKEDIN_TOKENS }}" in publish
    assert "@v0.2.0" in publish
    check = yaml.safe_load(files[".github/workflows/linkedin-token-check.yml"])
    assert "--fail-days 7" in check["jobs"]["check"]["steps"][-1]["run"]


def test_minutes_budget_stays_well_under_the_free_tier():
    runs = cloud.runs_per_month(cloud.utc_hours("Europe/Amsterdam"))
    assert runs < 2000 * 0.6, runs


def test_init_github_writes_workflows_and_tracks_receipts(tmp_path, capsys):
    cli.main(["init", str(tmp_path), "--github", "--timezone", "Europe/Amsterdam"])
    assert (tmp_path / ".github/workflows/linkedin-publisher.yml").exists()
    assert (tmp_path / ".github/workflows/linkedin-token-check.yml").exists()
    ignore = (tmp_path / ".gitignore").read_text()
    assert ".receipts" not in ignore.replace("# Receipts", "").split("\n.env")[1]
    assert ".linkedin-tokens.json" in ignore and ".env" in ignore
    assert "gh secret set LINKEDIN_TOKENS" in capsys.readouterr().out


# -------------------------------------------------------------- fail-days

def _tokens(cfg, days):
    expires = (dt.datetime.now() + dt.timedelta(days=days)).isoformat()
    cfg.tokens_file.write_text(json.dumps({"access_expires_at": expires}))


def test_fail_days_fails_close_to_expiry(cfg, tmp_path, capsys):
    _tokens(cfg, 3)
    assert cli.main(["--config", str(tmp_path / "publisher.toml"), "status", "--fail-days", "7"]) == 2


def test_fail_days_passes_with_time_left(cfg, tmp_path, capsys):
    _tokens(cfg, 30)
    assert cli.main(["--config", str(tmp_path / "publisher.toml"), "status", "--fail-days", "7"]) == 0


def test_fail_days_fails_without_tokens(cfg, tmp_path, capsys):
    assert cli.main(["--config", str(tmp_path / "publisher.toml"), "status", "--fail-days", "7"]) == 2


def test_notify_run_leaves_a_heartbeat_even_when_nothing_is_due(cfg, tmp_path, capsys):
    assert not cfg.heartbeat_file.exists()
    cli.main(["--config", str(tmp_path / "publisher.toml"), "publish", "--notify"])
    assert cfg.heartbeat_file.exists()
    assert "min ago" in cli.last_check_line(cfg)


def test_stale_heartbeat_is_flagged(cfg):
    cfg.heartbeat_file.write_text((dt.datetime.now() - dt.timedelta(hours=2)).isoformat())
    assert "is the computer asleep" in cli.last_check_line(cfg)
