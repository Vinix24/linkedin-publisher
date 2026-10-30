"""Every way a post could go out twice, tested against real HTTP and real receipts."""
import contextlib
import datetime as dt
import http.server
import json
import socket
import threading

import pytest
from conftest import WEDNESDAY_10, write_post

from linkedin_publisher import api, claims, cli
from linkedin_publisher import postqueue as queue

CREDS = {"LINKEDIN_CLIENT_ID": "x", "LINKEDIN_CLIENT_SECRET": "y"}
TOKENS = {"access_token": "t", "author_urn": "urn:li:person:x"}


def serve(status: int, headers: dict | None = None, body: bytes = b""):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/rest/posts"


@contextlib.contextmanager
def served(status: int, headers: dict | None = None, body: bytes = b""):
    server, url = serve(status, headers, body)
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------ what LinkedIn answers

def test_created_with_id_returns_the_urn(cfg):
    server, url = serve(201, {"x-restli-id": "urn:li:share:1"})
    assert api.publish(cfg, CREDS, TOKENS, "Hello", posts_url=url) == "urn:li:share:1"
    server.shutdown()


def test_created_without_id_is_uncertain_not_failed(cfg):
    server, url = serve(201)
    with pytest.raises(api.PublishUncertain):
        api.publish(cfg, CREDS, TOKENS, "Hello", posts_url=url)
    server.shutdown()


def test_server_error_is_uncertain(cfg):
    server, url = serve(500, body=b"oops")
    with pytest.raises(api.PublishUncertain):
        api.publish(cfg, CREDS, TOKENS, "Hello", posts_url=url)
    server.shutdown()


def test_refusal_is_a_plain_failure_that_may_be_retried(cfg):
    server, url = serve(403, body=b"forbidden")
    with pytest.raises(api.PublisherError) as err:
        api.publish(cfg, CREDS, TOKENS, "Hello", posts_url=url)
    assert not isinstance(err.value, api.PublishUncertain)
    server.shutdown()


NOT_AN_OBJECT = {"list": b"[]", "list-with-urn": b'["urn:li:share:9"]', "string": b'"urn:li:share:9"',
                 "number": b"12", "null": b"null", "bool": b"true"}
UNUSABLE_ID = {"empty-object": b"{}", "id-null": b'{"id": null}', "id-empty": b'{"id": ""}', "id-number": b'{"id": 5}',
               "id-list": b'{"id": ["urn:li:share:9"]}', "id-object": b'{"id": {"urn": "urn:li:share:9"}}'}


@pytest.mark.parametrize("body", [*NOT_AN_OBJECT.values(), *UNUSABLE_ID.values()],
                         ids=[*NOT_AN_OBJECT, *UNUSABLE_ID])
def test_a_success_without_a_usable_id_is_uncertain_whatever_the_body_is(cfg, body):
    with served(201, body=body) as url, pytest.raises(api.PublishUncertain):
        api.publish(cfg, CREDS, TOKENS, "Hello", posts_url=url)


@pytest.mark.parametrize("status", [200, 201])
def test_an_id_in_the_body_is_used_when_the_header_is_missing(cfg, status):
    with served(status, body=b'{"id": "urn:li:share:7"}') as url:
        assert api.publish(cfg, CREDS, TOKENS, "Hello", posts_url=url) == "urn:li:share:7"


@pytest.mark.parametrize("status", [202, 204, 302])
def test_a_success_or_redirect_other_than_200_and_201_is_uncertain_not_a_refusal(cfg, status):
    with served(status) as url, pytest.raises(api.PublishUncertain):
        api.publish(cfg, CREDS, TOKENS, "Hello", posts_url=url)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429])
def test_a_client_error_is_a_clear_refusal(cfg, status):
    with served(status, body=b"no") as url, pytest.raises(api.PublisherError) as err:
        api.publish(cfg, CREDS, TOKENS, "Hello", posts_url=url)
    assert not isinstance(err.value, api.PublishUncertain) and str(status) in str(err.value)


@pytest.mark.parametrize("status, body", [(201, b"[]"), (201, b'"urn:li:share:9"'), (202, b""), (200, b"12")],
                         ids=["list", "string", "accepted", "number"])
def test_an_answer_without_a_usable_id_keeps_the_claim_and_a_second_run_sends_nothing(cfg, monkeypatch, status, body):
    """The post may be live. A `failed` receipt and a released claim would let the next run post it again."""
    path = write_post(cfg, "a.md", {"status": "queued", "slot": "2026-09-23"})
    key = queue.content_key(queue.post_text(queue.read_post(path).body, cfg.notes_headings))
    calls = []
    real_publish = api.publish
    monkeypatch.setattr(api, "load_credentials", lambda _cfg: CREDS)
    monkeypatch.setattr(api, "read_tokens", lambda _cfg: TOKENS)
    monkeypatch.setattr(api, "ensure_access_token", lambda *_a: TOKENS)
    with served(status, body=body) as url:
        def publish_to_the_local_server(*args, **kwargs):
            calls.append(1)
            return real_publish(*args, posts_url=url, **kwargs)

        monkeypatch.setattr(api, "publish", publish_to_the_local_server)
        assert cli.cmd_publish(cfg, WEDNESDAY_10, "post", None) == 1
        assert [row["stage"] for row in queue._receipt_rows(cfg)] == ["attempt", "uncertain"]
        assert claims.exists(cfg, key) and path.exists()

        assert cli.cmd_publish(cfg, WEDNESDAY_10, "post", None) == 0
        assert len(calls) == 1
        assert [row["stage"] for row in queue._receipt_rows(cfg)] == ["attempt", "uncertain"]
        assert queue.load_states(cfg) == {key: "uncertain"}


def test_no_connection_is_uncertain(cfg):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again when the block ends
    with pytest.raises(api.PublishUncertain):
        api.publish(cfg, CREDS, TOKENS, "Hello", posts_url=f"http://127.0.0.1:{port}/rest/posts")


# ------------------------------------------------------------- receipts

def _post(cfg):
    return queue.read_post(write_post(cfg, "a.md", {"status": "queued", "slot": "2026-09-23"}))


def test_attempt_without_outcome_blocks_a_retry(cfg):
    queue.write_receipt(cfg, "attempt", _post(cfg))
    ready, skipped = queue.collect(cfg, WEDNESDAY_10)
    assert ready == [] and "may have gone through" in skipped[0].reason


def test_uncertain_blocks_a_retry(cfg):
    post = _post(cfg)
    queue.write_receipt(cfg, "attempt", post)
    queue.write_receipt(cfg, "uncertain", post)
    assert queue.collect(cfg, WEDNESDAY_10)[0] == []


def test_a_clear_failure_after_an_attempt_can_be_retried(cfg):
    post = _post(cfg)
    queue.write_receipt(cfg, "attempt", post)
    queue.write_receipt(cfg, "failed", post)
    assert [p.path.name for p in queue.collect(cfg, WEDNESDAY_10)[0]] == ["a.md"]


def test_placed_stays_placed_whatever_comes_after(cfg):
    post = _post(cfg)
    queue.write_receipt(cfg, "placed", post)
    queue.write_receipt(cfg, "failed", post)
    assert queue.collect(cfg, WEDNESDAY_10)[0] == []


def test_stranded_note_changes_no_outcome(cfg):
    post = _post(cfg)
    queue.write_receipt(cfg, "stranded", post)
    assert [p.path.name for p in queue.collect(cfg, WEDNESDAY_10)[0]] == ["a.md"]


# ------------------------------------------------------------ --retry

def _cli(tmp_path, *args):
    return cli.main(["--config", str(tmp_path / "publisher.toml"), *args])


def test_file_after_uncertain_needs_retry(cfg, tmp_path, capsys):
    path = write_post(cfg, "a.md", {"status": "queued", "slot": "2026-09-23"})
    queue.write_receipt(cfg, "uncertain", queue.read_post(path))
    assert _cli(tmp_path, "publish", "--post", "--file", str(path)) == 1
    assert "add --retry" in capsys.readouterr().err
    # With --retry it passes the guard and stops at the missing credentials instead.
    assert _cli(tmp_path, "publish", "--post", "--file", str(path), "--retry") == 1
    assert "LINKEDIN_CLIENT_ID is missing" in capsys.readouterr().err


def test_retry_never_overrides_placed(cfg, tmp_path, capsys):
    path = write_post(cfg, "a.md", {"status": "queued", "slot": "2026-09-23"})
    queue.write_receipt(cfg, "placed", queue.read_post(path))
    assert _cli(tmp_path, "publish", "--post", "--file", str(path), "--retry") == 1
    assert "already posted" in capsys.readouterr().err


def test_retry_without_file_is_refused(cfg, tmp_path, capsys):
    assert _cli(tmp_path, "publish", "--post", "--retry") == 1


# ---------------------------------------------- missed slot, reported once

def test_missed_slot_is_reported_once(cfg, tmp_path, capsys):
    long_ago = dt.datetime.now() - dt.timedelta(hours=5)
    write_post(cfg, "late.md", {"status": "queued", "slot": long_ago.date().isoformat(),
                                "slot_time": long_ago.strftime("%H:%M")})
    assert _cli(tmp_path, "publish", "--post") == 3
    assert "Missed its slot" in capsys.readouterr().out
    assert _cli(tmp_path, "publish", "--post") == 0
    assert "Missed its slot" not in capsys.readouterr().out
    stages = [json.loads(l)["stage"] for f in cfg.receipts_dir.glob("*.ndjson")
              for l in f.read_text().splitlines()]
    assert stages.count("stranded") == 1


# ----------------------------------------------- token warning, once a day

def test_scheduled_run_warns_about_the_token_once_a_day(cfg, tmp_path, capsys):
    expires = (dt.datetime.now() + dt.timedelta(days=3)).isoformat()
    cfg.tokens_file.write_text(json.dumps({"access_expires_at": expires}))
    _cli(tmp_path, "publish", "--notify")
    assert "token expires in 3 days" in capsys.readouterr().out
    _cli(tmp_path, "publish", "--notify")
    assert "token" not in capsys.readouterr().out
