import datetime as dt
import http.server
import json
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

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


# ------------------------------------------------------------------ a local website

class _QuietServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):     # a client that gave up is not news
        pass


class Site:
    """A website on 127.0.0.1 for the checks. `pages` can change between two runs, and every
    request is recorded in `hits`, so a test can also prove that nothing asked."""

    def __init__(self):
        self.pages: dict[str, tuple[int, bytes, dict]] = {}
        self.delays: dict[str, float] = {}
        self.hits: list[str] = []
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                site.hits.append(self.path)
                time.sleep(site.delays.get(self.path, 0))
                status, body, headers = site.pages.get(self.path, (404, b"not found", {}))
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.server = _QuietServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def serve(self, path: str, body: str = "", status: int = 200, headers: dict | None = None) -> str:
        self.pages[path] = (status, body.encode("utf-8"), headers or {})
        return self.url(path)

    def redirect(self, path: str, target: str, status: int = 302) -> str:
        return self.serve(path, status=status, headers={"Location": self.url(target)})

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_port}{path}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def site():
    server = Site()
    yield server
    server.close()


@pytest.fixture
def linkedin_must_stay_silent(monkeypatch):
    """Any call into the LinkedIn API, or a request to any host but this machine, fails the test."""
    import requests

    from linkedin_publisher import api

    def forbidden(*_a, **_kw):
        raise AssertionError("the LinkedIn API must not be called")

    for name in ("publish", "load_credentials", "read_tokens", "ensure_access_token",
                 "verify_online", "authorize"):
        monkeypatch.setattr(api, name, forbidden)
    real = requests.Session.request

    def only_local(self, method, url, *args, **kwargs):
        assert urlparse(url).hostname in ("127.0.0.1", "localhost"), f"unexpected request to {url}"
        return real(self, method, url, *args, **kwargs)

    monkeypatch.setattr(requests.Session, "request", only_local)


def snapshot(folder: Path) -> dict[str, bytes]:
    """Every file under a folder, by relative name, with its bytes."""
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}
