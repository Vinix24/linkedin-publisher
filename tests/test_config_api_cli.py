import json

import pytest

from linkedin_publisher import api, cli
from linkedin_publisher.config import ConfigError, load_config


def test_paths_resolve_relative_to_the_config_file(cfg, tmp_path):
    assert cfg.queue_dir == (tmp_path / "queue").resolve()
    assert cfg.tokens_file == (tmp_path / ".linkedin-tokens.json").resolve()


def test_invalid_slot_time_is_a_clear_error(tmp_path):
    (tmp_path / "publisher.toml").write_text('[slots]\nmon = "25:00"\n')
    with pytest.raises(ConfigError, match="slots.mon"):
        load_config(str(tmp_path / "publisher.toml"))


def test_payload_text_only():
    payload = api.build_post_payload("urn:li:person:x", "Hello")
    assert payload["commentary"] == "Hello" and "content" not in payload
    assert payload["visibility"] == "PUBLIC" and payload["lifecycleState"] == "PUBLISHED"


def test_payload_with_image_carries_alt_text():
    payload = api.build_post_payload("urn:li:person:x", "Hello", "urn:li:image:1", "A chart")
    assert payload["content"] == {"media": {"id": "urn:li:image:1", "altText": "A chart"}}


def test_missing_credentials_name_the_file(cfg):
    with pytest.raises(api.PublisherError, match="LINKEDIN_CLIENT_ID"):
        api.load_credentials(cfg)


def test_token_days_left(cfg):
    assert api.token_days_left(cfg) is None
    cfg.tokens_file.write_text(json.dumps({"access_expires_at": "2026-10-03T00:00:00"}))
    import datetime as dt
    assert api.token_days_left(cfg, dt.datetime(2026, 9, 23)) == pytest.approx(10)


def test_init_scaffolds_and_never_overwrites(tmp_path, capsys):
    assert cli.main(["init", str(tmp_path)]) == 0
    for name in ("publisher.toml", ".env", ".gitignore", "queue/2026-01-05-example.md"):
        assert (tmp_path / name).exists()
    (tmp_path / ".env").write_text("LINKEDIN_CLIENT_ID=mine\n")
    cli.main(["init", str(tmp_path)])
    assert (tmp_path / ".env").read_text() == "LINKEDIN_CLIENT_ID=mine\n"


def test_example_post_is_a_draft_and_never_goes_out(tmp_path, capsys):
    cli.main(["init", str(tmp_path)])
    cli.main(["--config", str(tmp_path / "publisher.toml"), "due"])
    out = capsys.readouterr().out
    assert "Nothing is due." in out and "status is draft" in out


def test_check_runs_without_network_or_tokens(tmp_path, capsys):
    cli.main(["init", str(tmp_path)])
    assert cli.main(["--config", str(tmp_path / "publisher.toml"), "check"]) == 0
    assert "preflight: ok" in capsys.readouterr().out


def test_publish_post_without_credentials_fails_cleanly_and_leaves_a_receipt(cfg, tmp_path, capsys):
    from conftest import write_post
    import datetime as dt
    write_post(cfg, "a.md", {"status": "queued", "slot": dt.date.today().isoformat(),
                             "slot_time": dt.datetime.now().strftime("%H:%M")})
    code = cli.main(["--config", str(tmp_path / "publisher.toml"), "publish", "--post"])
    assert code == 1
    assert "LINKEDIN_CLIENT_ID is missing" in capsys.readouterr().err
    rows = [json.loads(l) for f in cfg.receipts_dir.glob("*.ndjson") for l in f.read_text().splitlines()]
    assert rows[-1]["stage"] == "failed"
    assert (cfg.queue_dir / "a.md").exists()
    assert not cfg.lock_file.exists()
