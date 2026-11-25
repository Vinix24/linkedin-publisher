"""Posting as a company page: the config, the scope, the author field, the keys, the media owner and the comment."""
import datetime as dt
import hashlib
import json
from types import SimpleNamespace

import pytest
from conftest import CREDS, TOKENS, WEDNESDAY_10, Setup

from linkedin_publisher import api, approval, claims, cli, firstcomment, frontmatter, littletext
from linkedin_publisher import postqueue as queue
from linkedin_publisher.config import COMMUNITY_SCOPES, ConfigError, load_config

ORG, ORG2 = "urn:li:organization:123", "urn:li:organization:456"
COMMUNITY = '[community]\ntokens_file = "community.json"\n'
PAGES = COMMUNITY + f'\n[pages.acme]\norganization = "{ORG}"\n'
PAGE_TOKENS = {"access_token": "c", "author_urn": "urn:li:person:c", "access_expires_at": "2999-01-01T00:00:00",
               "scope": "r_basicprofile w_member_social w_organization_social"}
EVENING_BEFORE = dt.datetime(2026, 9, 22, 17, 0)
SLOT_PLUS_5 = WEDNESDAY_10 + dt.timedelta(minutes=5)


def load(tmp_path, toml):
    (tmp_path / "publisher.toml").write_text(toml, encoding="utf-8")
    return load_config(str(tmp_path / "publisher.toml"))


@pytest.fixture
def pages(tmp_path, monkeypatch):
    """acme and acme-bv are two names for one page, beta is another page. `apps` lists the tokens read."""
    setup = Setup(tmp_path, PAGES + f'[pages.acme-bv]\norganization = "{ORG}"\n[pages.beta]\norganization = "{ORG2}"\n')
    setup.install_fake_linkedin(monkeypatch)
    setup.cfg.community_tokens_file.write_text(json.dumps(PAGE_TOKENS))
    setup.apps = []
    monkeypatch.setattr(api, "load_credentials", lambda _cfg, app="main": CREDS)
    monkeypatch.setattr(api, "read_tokens", lambda cfg, app="main": setup.apps.append(app) or (
        json.loads(cfg.community_tokens_file.read_text()) if app == "community" else TOKENS))
    monkeypatch.setattr(api, "ensure_access_token", lambda _cfg, _creds, tokens, *_a: tokens)
    return setup


def set_author(path, name):
    path.write_text(frontmatter.set_fields(path.read_text(), {"author": name}))


def test_a_page_is_a_name_and_an_organization_urn(tmp_path):
    assert load(tmp_path, PAGES).pages == {"acme": ORG}
    assert load(tmp_path, COMMUNITY).pages == {}


@pytest.mark.parametrize("line", ['organization = "urn:li:organization:"', 'organization = "urn:li:organization:12a"',
                                  'organization = "urn:li:person:123"', 'organization = "urn:li:organization:123\\n"',
                                  'organization = " urn:li:organization:123"', 'organization = "urn:li:organization:١٢"',
                                  'organization = "urn:li:organization:0123"', 'organization = "urn:li:organization:0"',
                                  "organization = 123", 'name = "acme"'])
def test_an_organization_that_is_not_an_organization_urn_is_refused(tmp_path, line):
    with pytest.raises(ConfigError, match=r"pages\.acme\.organization must be the URN"):
        load(tmp_path, COMMUNITY + f"[pages.acme]\n{line}\n")


def test_a_page_needs_the_community_section_and_a_plain_name(tmp_path):
    with pytest.raises(ConfigError, match=r"no \[community\] section"):
        load(tmp_path, f'[pages.acme]\norganization = "{ORG}"\n')
    with pytest.raises(ConfigError, match="not a plain name"):
        load(tmp_path, COMMUNITY + f'[pages."a b"]\norganization = "{ORG}"\n')


@pytest.mark.parametrize("toml, scopes", [
    (COMMUNITY, COMMUNITY_SCOPES),                                       # no page: the scopes it always had
    (PAGES, COMMUNITY_SCOPES + " w_organization_social"),
    (PAGES.replace("[community]\n", '[community]\nscopes = "r_basicprofile w_organization_social"\n'),
     "r_basicprofile w_organization_social"),                            # already asked for: not twice
])
def test_the_page_scope_is_asked_for_only_when_a_page_is_configured(tmp_path, toml, scopes):
    assert load(tmp_path, toml).community_scopes == scopes


@pytest.mark.parametrize("tokens, ok", [({}, False), ({"scope": "r_basicprofile w_member_social"}, False),
                                        ({"scope": "w_member_social w_organization_social"}, True),
                                        ({"scope": "w_member_social,w_organization_social"}, True)])
def test_the_page_scope_of_a_token(tokens, ok):
    assert api.has_page_scope(tokens) is ok          # {} is a token saved before 0.9.0


def test_status_and_precheck_say_when_the_token_cannot_post_as_a_page(pages, capsys):
    pages.cfg.community_tokens_file.write_text(json.dumps({**PAGE_TOKENS, "scope": "r_basicprofile w_member_social"}))
    pages.post(meta={"author": "acme"})
    cli.cmd_status(pages.cfg)
    assert ("Posting as a page will fail: the community token was not granted w_organization_social. "
            "Run: linkedin-publisher auth --app community") in capsys.readouterr().out
    assert cli.cmd_precheck(pages.cfg, EVENING_BEFORE, 1, as_json=True) == 3
    post = json.loads(capsys.readouterr().out)["posts"][0]
    assert post["author"] == "acme" and [p["kind"] for p in post["problems"]] == ["page"]


def test_a_page_post_is_not_sent_without_the_page_scope(pages):
    pages.cfg.community_tokens_file.write_text(json.dumps({**PAGE_TOKENS, "scope": "w_member_social"}))
    path = pages.post(meta={"author": "acme"})
    assert pages.run("publish", "--post", "--file", str(path)) == 1
    assert pages.sends == [] and pages.receipt_stages() == ["failed"] and path.exists()
    assert not claims.exists(pages.cfg, queue.post_key(queue.read_post(path), pages.cfg))


def test_a_page_post_the_token_cannot_send_does_not_hold_up_a_member_post(pages):
    """The page post comes first in the queue. Taken first, it would fail after its claim on every run."""
    pages.cfg.community_tokens_file.write_text(json.dumps({**PAGE_TOKENS, "scope": "r_basicprofile w_member_social"}))
    page = pages.post("a.md", {"author": "acme"}, body="Page text.")
    pages.post("b.md", body="Member text.")
    codes = [cli.cmd_publish(pages.cfg, WEDNESDAY_10 + dt.timedelta(minutes=m), "post", None) for m in (5, 20, 35)]
    assert codes == [3, 0, 0] and [s["author"] for s in pages.sends] == [None]
    assert "Member text" in pages.sends[0]["commentary"] and page.exists()
    assert pages.receipt_stages() == ["page-problem", "attempt", "placed"]   # the page post: one note, never tried
    assert pages.titles() == [cli.PAGE_HELD_TITLE, "LinkedIn post is live"]
    assert not claims.exists(pages.cfg, queue.post_key(queue.read_post(page), pages.cfg))


def test_an_unknown_page_is_reported_by_precheck_and_never_sent(pages, capsys):
    path = pages.post(meta={"author": "other"})
    assert cli.cmd_precheck(pages.cfg, EVENING_BEFORE, 1, as_json=True) == 3
    problem = json.loads(capsys.readouterr().out)["posts"][0]["problems"][0]
    assert problem["kind"] == "content" and "author: 'other' is not a page" in problem["message"]
    assert cli.cmd_publish(pages.cfg, SLOT_PLUS_5, "post", None) == 0
    assert pages.run("publish", "--post", "--file", str(path)) == 1
    assert pages.sends == [] and path.exists()


def test_due_precheck_status_and_the_dry_run_name_the_page(pages, capsys):
    path = pages.post("a.md", {"author": "acme"})
    pages.post("m.md", body="Another text.")
    cli.cmd_due(pages.cfg, SLOT_PLUS_5)
    out = capsys.readouterr().out
    assert "  a.md  (slot 2026-09-23 10:00, text, as page acme)" in out
    assert "  m.md  (slot 2026-09-23 10:00, text)\n" in out                 # the member line as it was
    cli.cmd_precheck(pages.cfg, EVENING_BEFORE, 1)
    assert "2026-09-23 10:00  a.md, as page acme  ok" in capsys.readouterr().out
    cli.cmd_status(pages.cfg)
    out = capsys.readouterr().out
    assert f"page:      acme ({ORG})" in out and "The community token has w_organization_social." in out
    pages.run("publish", "--file", str(path))
    assert f"goes out as page acme ({ORG})" in capsys.readouterr().out


def v1_key(text="Hello world."):
    """The key 0.8.0 wrote for a plain text post, built by hand with the v1 recipe."""
    digest = hashlib.sha256(approval.KEY_VERSION)
    for label, data in (("text", text), ("commentary", littletext.prepare(text)), ("format", "text")):
        raw = data.encode()
        digest.update(f"{label}:{len(raw)}\n".encode() + raw + b"\n")
    return digest.hexdigest()


def test_a_post_approved_before_pages_existed_keeps_its_approval(pages):
    path = pages.post(meta={"approved_by": "Ann", "approved_at": "2026-09-20T10:00:00+02:00", "approved_key": v1_key()})
    assert approval.check(queue.read_post(path), pages.cfg) == (approval.APPROVED, v1_key())


def test_switching_who_a_post_goes_out_as_voids_the_approval(pages):
    path = pages.post()
    approval.approve(pages.cfg, path, "Ann")
    set_author(path, "acme")
    assert approval.check(queue.read_post(path), pages.cfg)[0] == approval.MISMATCH     # member -> page
    approval.approve(pages.cfg, path, "Ann")
    set_author(path, "beta")
    assert approval.check(queue.read_post(path), pages.cfg)[0] == approval.MISMATCH     # page -> another page
    set_author(path, "acme-bv")
    assert approval.check(queue.read_post(path), pages.cfg)[0] == approval.APPROVED     # another name, same page


def test_the_receipts_know_a_post_by_its_text_and_its_page(pages):
    member = pages.post("member.md")
    assert queue.post_key(queue.read_post(member), pages.cfg) == queue.content_key("Hello world.")   # as before
    assert cli.cmd_publish(pages.cfg, SLOT_PLUS_5, "post", None) == 0
    page = pages.post("page.md", {"author": "acme"})
    assert [p.path for p in queue.collect(pages.cfg, SLOT_PLUS_5)[0]] == [page]    # same text, other author: free
    assert cli.cmd_publish(pages.cfg, SLOT_PLUS_5, "post", None) == 0
    assert [s["author"] for s in pages.sends] == [None, ORG]
    assert pages.apps.count("main") == 1 and pages.apps[-1] == "community"      # the page post read the community token
    pages.post("renamed.md", {"author": "acme-bv"})
    beta = pages.post("beta.md", {"author": "beta"})
    ready, skipped = queue.collect(pages.cfg, SLOT_PLUS_5)
    assert [p.path for p in ready] == [beta]
    assert [p.path.name for p in skipped if "already posted" in p.reason] == ["renamed.md"]   # same page, new name


def reply(status, body=None, headers=None):
    return SimpleNamespace(status_code=status, headers=headers or {}, text=json.dumps(body or {}), json=lambda: body)


@pytest.mark.parametrize("kind", ["image", "video", "document"])
@pytest.mark.parametrize("author, owner", [(ORG, ORG), (None, TOKENS["author_urn"])])
def test_a_page_post_owns_its_media(cfg, tmp_path, monkeypatch, kind, author, owner):
    calls = []
    value = {"uploadUrl": "https://upload.test/1", "image": "urn:li:image:1", "video": "urn:li:video:1",
             "document": "urn:li:document:1", "uploadInstructions": [{"uploadUrl": "https://upload.test/1",
                                                                       "firstByte": 0, "lastByte": 0}]}

    def post(_session, url, json=None, **_):
        calls.append((url, json))
        if "action=" in url:
            return reply(200, {"value": value})
        return reply(201, headers={"x-restli-id": "urn:li:share:1"})

    monkeypatch.setattr(api.requests.Session, "post", post)
    monkeypatch.setattr(api.requests.Session, "get", lambda _s, _url, **_: reply(200, {"status": "AVAILABLE"}))
    monkeypatch.setattr(api.requests, "put", lambda _url, **_: reply(201, headers={"ETag": '"e1"'}))
    media = tmp_path / "media"
    media.write_bytes(b"x")
    assert api.publish(cfg, CREDS, TOKENS, "Hello", **{kind: media}, author=author) == "urn:li:share:1"
    assert calls[0][1]["initializeUploadRequest"]["owner"] == owner and calls[-1][1]["author"] == owner


def test_a_page_post_with_a_first_comment_is_refused(pages, capsys):
    path = pages.post(meta={"author": "acme"})
    queue.comment_path_for(path).write_text("A comment.\n", encoding="utf-8")
    assert cli.cmd_precheck(pages.cfg, EVENING_BEFORE, 1, as_json=True) == 3
    problem = json.loads(capsys.readouterr().out)["posts"][0]["problems"]
    assert [p["kind"] for p in problem] == ["content"] and "as a page has no first comment" in problem[0]["message"]
    assert pages.run("publish", "--post", "--file", str(path)) == 1 and pages.sends == []


def test_a_comment_is_never_placed_under_a_page_post(pages, monkeypatch):
    """Not even by hand afterwards: the community token would place it as the member."""
    monkeypatch.setattr(api, "place_comment", lambda *_a, **_k: pytest.fail("a comment was sent"))
    path = pages.post(meta={"author": "acme"})
    queue.comment_path_for(path).write_text("A comment.\n", encoding="utf-8")
    status = firstcomment.place(pages.cfg, queue.read_post(path), "urn:li:share:1", path, lambda _m: None,
                                lambda *_a: None)
    assert status == firstcomment.FAILED and pages.receipt_stages() == ["comment-failed"]
