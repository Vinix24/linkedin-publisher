import json
import threading
from types import SimpleNamespace
import urllib.parse
import urllib.request

import pytest

from linkedin_publisher import api, cli
from linkedin_publisher.config import COMMUNITY_SCOPES, load_config

COMMUNITY_ENV = "LINKEDIN_COMMUNITY_CLIENT_ID=c-id\nLINKEDIN_COMMUNITY_CLIENT_SECRET=c-secret\n"
COMMUNITY_TOML = '[community]\ntokens_file = "community.json"\n'


@pytest.fixture
def community_cfg(tmp_path, cfg):
    (tmp_path / "publisher.toml").write_text(
        (tmp_path / "publisher.toml").read_text() + COMMUNITY_TOML, encoding="utf-8")
    return load_config(str(tmp_path / "publisher.toml"))


def Reply(status, body):
    return SimpleNamespace(status_code=status, text=json.dumps(body), json=lambda: body)


@pytest.fixture
def login(monkeypatch):
    seen = {"urls": [], "posts": []}

    def open_url(url):
        seen["urls"].append(url)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        threading.Thread(target=lambda: urllib.request.urlopen(
            f"{query['redirect_uri'][0]}?code=abc&state={query['state'][0]}"), daemon=True).start()

    def post(url, data=None, **_):
        seen["posts"].append(data)
        return Reply(200, {"access_token": "tok", "expires_in": 5184000})

    monkeypatch.setattr(api.webbrowser, "open", open_url)
    monkeypatch.setattr(api.requests, "post", post)
    return seen


def test_config_without_community_section(cfg):
    assert cfg.community_tokens_file is None and cfg.community_scopes == COMMUNITY_SCOPES
    with pytest.raises(api.PublisherError, match=r"\[community\]"):
        api.read_tokens(cfg, "community")


def test_community_defaults_and_custom_scopes(tmp_path, cfg):
    path = tmp_path / "publisher.toml"
    path.write_text(path.read_text() + '[community]\nscopes = "r_basicprofile  w_member_social"\n')
    loaded = load_config(str(path))
    assert loaded.community_tokens_file == (tmp_path / ".linkedin-community-tokens.json").resolve()
    assert loaded.community_scopes == "r_basicprofile w_member_social"


def test_community_credentials_never_use_the_main_keys(community_cfg, monkeypatch):
    community_cfg.env_file.write_text("LINKEDIN_CLIENT_ID=main-id\nLINKEDIN_CLIENT_SECRET=main-secret\n")
    monkeypatch.setenv("LINKEDIN_CLIENT_ID", "env-main-id")
    with pytest.raises(api.PublisherError, match="LINKEDIN_COMMUNITY_CLIENT_ID"):
        api.load_credentials(community_cfg, "community")
    community_cfg.env_file.write_text(COMMUNITY_ENV)
    monkeypatch.setenv("LINKEDIN_COMMUNITY_CLIENT_SECRET", "env-c-secret")
    creds = api.load_credentials(community_cfg, "community")
    assert api.client_pair(creds, "community") == ("c-id", "env-c-secret")
    with pytest.raises(api.PublisherError, match="LINKEDIN_CLIENT_SECRET"):
        api.load_credentials(community_cfg)


def test_parse_me():
    assert api.parse_me({"id": "abc123"}) == "urn:li:person:abc123"
    for bad in ({}, {"id": ""}, {"id": 5}):
        with pytest.raises(api.PublisherError, match="/v2/me"):
            api.parse_me(bad)


def test_authorize_community_uses_its_scopes_keys_and_me(community_cfg, login, monkeypatch):
    community_cfg.env_file.write_text(COMMUNITY_ENV)
    called = []
    monkeypatch.setattr(api.requests, "get", lambda url, **_: called.append(url) or Reply(200, {"id": "me1"}))
    assert api.authorize(community_cfg, lambda _m: None, "community") == "urn:li:person:me1"
    query = urllib.parse.parse_qs(urllib.parse.urlparse(login["urls"][0]).query)
    assert query["scope"] == [COMMUNITY_SCOPES] and query["client_id"] == ["c-id"]
    assert login["posts"][0]["client_secret"] == "c-secret"
    assert called == [api.ME_URL]
    assert "me1" in community_cfg.community_tokens_file.read_text() and not community_cfg.tokens_file.exists()


def test_authorize_community_refuses_another_account(community_cfg, login, monkeypatch):
    community_cfg.env_file.write_text(COMMUNITY_ENV)
    community_cfg.tokens_file.write_text(json.dumps({"author_urn": "urn:li:person:main1"}))
    monkeypatch.setattr(api.requests, "get", lambda *_a, **_k: Reply(200, {"id": "other"}))
    with pytest.raises(api.PublisherError, match="different LinkedIn account"):
        api.authorize(community_cfg, lambda _m: None, "community")
    assert not community_cfg.community_tokens_file.exists()


def test_status_shows_community_token_only_when_configured(community_cfg, capsys):
    community_cfg.community_tokens_file.write_text('{"access_expires_at": "2999-01-01T00:00:00"}')
    cli.cmd_status(community_cfg)
    assert "Community access token valid" in capsys.readouterr().out
