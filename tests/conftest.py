import datetime as dt
import json
import sys
from pathlib import Path

import pytest

from linkedin_publisher import cli
from linkedin_publisher.config import load_config

CONFIG = """\
[paths]
queue = "queue"
published = "published"

[frontmatter]
notes_headings = ["Notes", "Image Brief"]

[slots]
default = "10:00"
sun = "11:00"
grace_minutes = 90
"""


@pytest.fixture
def cfg(tmp_path: Path):
    (tmp_path / "queue").mkdir()
    (tmp_path / "publisher.toml").write_text(CONFIG, encoding="utf-8")
    return load_config(str(tmp_path / "publisher.toml"))


def write_post(cfg, name: str, meta: dict, body: str = "Hello world.") -> Path:
    lines = ["---", *[f"{k}: {v}" for k, v in meta.items()], "---", "", body]
    path = cfg.queue_dir / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# 2026-09-23 is a Wednesday
WEDNESDAY_10 = dt.datetime(2026, 9, 23, 10, 0)


# ------------------------------------------------------------------ shared helpers

@pytest.fixture(autouse=True)
def no_desktop_notifications(monkeypatch):
    """A test run must not pop up real notifications. A test that wants to see
    them either replaces cli.notify itself or configures a [notify] command."""
    monkeypatch.setattr(cli, "notify", lambda title, message: None)


HOOK_SCRIPT = """\
import json, sys
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[2:]) + "\\n")
"""

CREDS = {"LINKEDIN_CLIENT_ID": "x", "LINKEDIN_CLIENT_SECRET": "y"}
TOKENS = {"access_token": "t", "author_urn": "urn:li:person:x"}


class Setup:
    """A queue in a temporary folder, a fake LinkedIn, and a notification hook that
    writes every notification it gets to a file."""

    def __init__(self, tmp_path: Path, extra_toml: str = "", hook: bool = True, frontmatter: str = ""):
        self.tmp_path = tmp_path
        (tmp_path / "queue").mkdir(exist_ok=True)
        self.hook_out = tmp_path / "notifications.ndjson"
        script = tmp_path / "hook.py"
        script.write_text(HOOK_SCRIPT, encoding="utf-8")
        toml = CONFIG.replace('notes_headings = ["Notes", "Image Brief"]\n',
                              'notes_headings = ["Notes", "Image Brief"]\n' + frontmatter)
        if hook:
            argv = json.dumps([sys.executable, str(script), str(self.hook_out)])
            toml += f"\n[notify]\ncommand = {argv}\n"
        toml += "\n" + extra_toml
        (tmp_path / "publisher.toml").write_text(toml, encoding="utf-8")
        self.config_path = tmp_path / "publisher.toml"
        self.cfg = load_config(str(self.config_path))
        self.sends: list[dict] = []

    def install_fake_linkedin(self, monkeypatch, fail_with: Exception | None = None):
        from linkedin_publisher import api
        sends = self.sends

        def fake_publish(_cfg, _creds, _tokens, commentary, image=None, alt_text=None, **kwargs):
            if fail_with is not None:
                raise fail_with
            sends.append({"commentary": commentary, "image": image, "alt_text": alt_text, **kwargs})
            return f"urn:li:share:{len(sends)}"

        monkeypatch.setattr(api, "load_credentials", lambda _cfg: CREDS)
        monkeypatch.setattr(api, "read_tokens", lambda _cfg: TOKENS)
        monkeypatch.setattr(api, "ensure_access_token", lambda *_a: TOKENS)
        monkeypatch.setattr(api, "publish", fake_publish)

    def notifications(self) -> list[list[str]]:
        if not self.hook_out.exists():
            return []
        return [json.loads(line) for line in self.hook_out.read_text().splitlines()]

    def titles(self) -> list[str]:
        return [n[0] for n in self.notifications()]

    def run(self, *args: str) -> int:
        return cli.main(["--config", str(self.config_path), *args])

    def post(self, name: str = "a.md", meta: dict | None = None, body: str = "Hello world."):
        meta = {"status": "queued", "slot": "2026-09-23", **(meta or {})}
        return write_post(self.cfg, name, meta, body)

    def receipt_stages(self) -> list[str]:
        if not self.cfg.receipts_dir.is_dir():
            return []
        return [json.loads(line)["stage"] for f in sorted(self.cfg.receipts_dir.glob("*.ndjson"))
                for line in f.read_text().splitlines() if line.strip()]


@pytest.fixture
def setup(tmp_path):
    return Setup(tmp_path)
