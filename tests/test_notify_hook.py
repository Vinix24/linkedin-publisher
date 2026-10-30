"""The notification hook: a command a launchd job can use where no desktop is watching."""
import json
import sys
import time

import pytest
from conftest import Setup

from linkedin_publisher import cli
from linkedin_publisher.config import ConfigError, load_config

NASTY = 'Title; touch pwned && echo "$(whoami)"'


def test_the_hook_gets_title_and_message_appended_and_no_shell_is_involved(setup):
    cli.alert(setup.cfg, NASTY, "line one\nline two `id` | $HOME")
    assert setup.notifications() == [[NASTY, "line one\nline two `id` | $HOME"]]
    assert not (setup.tmp_path / "pwned").exists()


def test_without_the_option_only_the_desktop_notification_is_used(tmp_path, monkeypatch):
    plain = Setup(tmp_path, hook=False)
    seen = []
    monkeypatch.setattr(cli, "notify", lambda title, message: seen.append((title, message)))
    cli.alert(plain.cfg, "T", "M")
    assert seen == [("T", "M")]
    assert plain.cfg.notify_command == ()


def test_with_the_option_the_desktop_notification_still_goes_out(setup, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "notify", lambda title, message: seen.append((title, message)))
    cli.alert(setup.cfg, "T", "M")
    assert seen == [("T", "M")] and setup.notifications() == [["T", "M"]]


def broken_hook(tmp_path, command: str) -> Setup:
    return Setup(tmp_path, f"[notify]\ncommand = {command}\n", hook=False)


@pytest.mark.parametrize("command", [
    '["/no/such/program"]',
    f'[{json.dumps(sys.executable)}, "-c", "import sys; sys.exit(3)"]',
])
def test_a_failing_hook_never_breaks_a_placement(tmp_path, monkeypatch, command):
    broken = broken_hook(tmp_path, command)
    broken.install_fake_linkedin(monkeypatch)
    path = broken.post()
    assert broken.run("publish", "--post", "--file", str(path)) == 0
    assert len(broken.sends) == 1
    assert (broken.cfg.published_dir / "a.md").exists()
    assert broken.receipt_stages() == ["attempt", "placed"]
    assert "notification hook failed" in broken.cfg.log_file.read_text()


def test_a_hook_that_hangs_is_stopped(tmp_path, monkeypatch):
    hung = broken_hook(tmp_path, f'[{json.dumps(sys.executable)}, "-c", "import time; time.sleep(30)"]')
    monkeypatch.setattr(cli, "NOTIFY_HOOK_TIMEOUT", 0.5)
    started = time.monotonic()
    cli.alert(hung.cfg, "T", "M")
    assert time.monotonic() - started < 10
    assert "timed out" in hung.cfg.log_file.read_text()


def test_the_hook_exit_code_is_logged(tmp_path):
    failing = broken_hook(tmp_path, f'[{json.dumps(sys.executable)}, "-c", "import sys; sys.exit(3)"]')
    cli.alert(failing.cfg, "T", "M")
    assert "exit code 3" in failing.cfg.log_file.read_text()


# --------------------------------------------------------------- configuration

def load(tmp_path, toml: str):
    (tmp_path / "publisher.toml").write_text(toml, encoding="utf-8")
    return load_config(str(tmp_path / "publisher.toml"))


def test_defaults_leave_everything_as_it_was(tmp_path):
    cfg = load(tmp_path, "")
    assert cfg.approval_required is False
    assert cfg.notify_command == ()
    assert cfg.claim_unresolved_minutes == 15
    assert (cfg.approved_by_field, cfg.approved_at_field, cfg.approved_key_field) == \
        ("approved_by", "approved_at", "approved_key")


def test_the_options_are_read(tmp_path):
    cfg = load(tmp_path, '[approval]\nrequired = true\n[notify]\ncommand = ["/bin/x", "--flag"]\n'
                         '[claims]\nunresolved_minutes = 45\n')
    assert cfg.approval_required is True
    assert cfg.notify_command == ("/bin/x", "--flag")
    assert cfg.claim_unresolved_minutes == 45


@pytest.mark.parametrize("toml, message", [
    ('[approval]\nrequired = "yes"\n', "approval.required"),
    ('[notify]\ncommand = "/bin/x"\n', "notify.command"),
    ('[notify]\ncommand = []\n', "notify.command"),
    ('[notify]\ncommand = ["/bin/x", 3]\n', "notify.command"),
    ('[claims]\nunresolved_minutes = 0\n', "claims.unresolved_minutes"),
    ('[claims]\nunresolved_minutes = "soon"\n', "claims.unresolved_minutes"),
    ('[frontmatter]\napproved_by_field = "status"\n', "approved_by_field"),
    ('[frontmatter]\napproved_by_field = "x"\napproved_at_field = "x"\n', "different"),
    ('[frontmatter]\napproved_key_field = ""\n', "approved_key_field"),
])
def test_unusable_values_are_refused_with_the_name_of_the_option(tmp_path, toml, message):
    with pytest.raises(ConfigError, match=message):
        load(tmp_path, toml)
