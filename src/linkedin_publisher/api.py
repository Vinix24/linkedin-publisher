"""Everything that talks to LinkedIn: OAuth, tokens, image upload, the post itself."""
from __future__ import annotations

import datetime as dt
import http.server
import json
import os
import secrets
import threading
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, Callable

import requests

from .config import Config

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
POSTS_URL = "https://api.linkedin.com/rest/posts"
IMAGES_URL = "https://api.linkedin.com/rest/images"
SCOPES = "openid profile w_member_social"

Log = Callable[[str], None]


class PublisherError(Exception):
    """Something failed that a person has to look at. The message says what."""


class PreflightRejected(PublisherError):
    """The text would still contain an unescaped reserved character. Not sent."""


class PublishUncertain(PublisherError):
    """LinkedIn may have created the post, but there is no clear answer.

    A timeout, a lost connection, a server error, or a success without a post id.
    Retrying automatically could post the same text twice, so the tool never does."""


# ------------------------------------------------------------------ credentials

def load_credentials(cfg: Config) -> dict[str, str]:
    """Read the .env file; real environment variables win over it."""
    creds: dict[str, str] = {}
    if cfg.env_file.exists():
        for raw in cfg.env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            creds[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET", "LINKEDIN_API_VERSION"):
        if os.environ.get(key):
            creds[key] = os.environ[key]
    for required in ("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"):
        if not creds.get(required):
            raise PublisherError(f"{required} is missing. Put it in {cfg.env_file} "
                                 "or in the environment. See README, 'Set up LinkedIn'.")
    return creds


def api_version(cfg: Config, creds: dict[str, str]) -> str:
    return creds.get("LINKEDIN_API_VERSION") or cfg.api_version


def read_tokens(cfg: Config) -> dict[str, Any]:
    if not cfg.tokens_file.exists():
        raise PublisherError("No tokens yet. Run: linkedin-publisher auth")
    return json.loads(cfg.tokens_file.read_text(encoding="utf-8"))


def write_tokens(cfg: Config, data: dict[str, Any]) -> None:
    cfg.tokens_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    cfg.tokens_file.chmod(0o600)


def token_days_left(cfg: Config, now: dt.datetime | None = None) -> float | None:
    """Days until the access token expires, or None when there is no token."""
    if not cfg.tokens_file.exists():
        return None
    expires = json.loads(cfg.tokens_file.read_text(encoding="utf-8")).get("access_expires_at")
    if not expires:
        return None
    now = now or dt.datetime.now()
    return (dt.datetime.fromisoformat(expires) - now).total_seconds() / 86400


# -------------------------------------------------------------------------- auth

def authorize(cfg: Config, log: Log = print) -> str:
    """Browser login. Returns the author URN and writes the token file."""
    creds = load_credentials(cfg)
    redirect_uri = f"http://localhost:{cfg.redirect_port}/callback"
    state = secrets.token_urlsafe(16)
    url = f"{AUTH_URL}?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": creds["LINKEDIN_CLIENT_ID"],
        "redirect_uri": redirect_uri, "state": state, "scope": SCOPES,
    })

    result: dict[str, str] = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            result.update(urllib.parse.parse_qsl(parsed.query))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body style='font-family:system-ui;padding:3rem'>"
                             b"<h2>Done.</h2><p>You can close this tab and go back to the terminal.</p>"
                             b"</body></html>")

        def log_message(self, *_args: Any) -> None:
            return

    server = http.server.HTTPServer(("localhost", cfg.redirect_port), Callback)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    log("Opening LinkedIn in your browser. Sign in and allow access.")
    log(f"If nothing opens, paste this URL yourself:\n\n{url}\n")
    webbrowser.open(url)
    thread.join(timeout=300)
    server.server_close()

    if not result:
        raise PublisherError("No callback received within 5 minutes.")
    if result.get("state") != state:
        raise PublisherError("State did not match. Aborted.")
    if "error" in result:
        raise PublisherError(f"LinkedIn said: {result['error']} {result.get('error_description', '')}")
    if not result.get("code"):
        raise PublisherError("No authorization code received.")

    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code", "code": result["code"],
        "redirect_uri": redirect_uri,
        "client_id": creds["LINKEDIN_CLIENT_ID"], "client_secret": creds["LINKEDIN_CLIENT_SECRET"],
    }, timeout=30)
    if resp.status_code != 200:
        raise PublisherError(f"Token exchange failed ({resp.status_code}): {resp.text}")
    payload = resp.json()

    who = requests.get(USERINFO_URL, headers={"Authorization": f"Bearer {payload['access_token']}"},
                       timeout=30)
    if who.status_code != 200:
        raise PublisherError(f"userinfo failed ({who.status_code}): {who.text}. "
                             "Is 'Sign In with LinkedIn using OpenID Connect' added to your app?")
    member_id = who.json().get("sub")
    if not member_id:
        raise PublisherError("No member id in the userinfo response.")

    now = dt.datetime.now()
    author = f"urn:li:person:{member_id}"
    write_tokens(cfg, {
        "access_token": payload["access_token"],
        "access_expires_at": (now + dt.timedelta(seconds=payload.get("expires_in", 0))).isoformat(),
        "refresh_token": payload.get("refresh_token", ""),
        "refresh_expires_at": (now + dt.timedelta(seconds=payload["refresh_token_expires_in"])).isoformat()
        if payload.get("refresh_token_expires_in") else "",
        "author_urn": author,
        "obtained_at": now.isoformat(),
    })
    log(f"Tokens saved to {cfg.tokens_file}. Author: {author}")
    if not payload.get("refresh_token"):
        days = payload.get("expires_in", 0) // 86400
        log(f"No refresh token was issued. The access token lasts about {days} days. "
            "After that, run 'linkedin-publisher auth' again.")
    return author


def ensure_access_token(cfg: Config, creds: dict[str, str], tokens: dict[str, Any],
                        log: Log = print) -> dict[str, Any]:
    """Refresh the access token when it expires within a day and a refresh token exists."""
    expires = tokens.get("access_expires_at")
    if expires:
        try:
            if dt.datetime.fromisoformat(expires) - dt.datetime.now() > dt.timedelta(days=1):
                return tokens
        except ValueError:
            pass
    if not tokens.get("refresh_token"):
        raise PublisherError("The access token has expired or expires within a day, and there is "
                             "no refresh token. Run 'linkedin-publisher auth' again.")
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
        "client_id": creds["LINKEDIN_CLIENT_ID"], "client_secret": creds["LINKEDIN_CLIENT_SECRET"],
    }, timeout=30)
    if resp.status_code != 200:
        raise PublisherError(f"Refresh failed ({resp.status_code}): {resp.text}. Run 'auth' again.")
    payload = resp.json()
    tokens["access_token"] = payload["access_token"]
    tokens["access_expires_at"] = (
        dt.datetime.now() + dt.timedelta(seconds=payload.get("expires_in", 0))).isoformat()
    if payload.get("refresh_token"):
        tokens["refresh_token"] = payload["refresh_token"]
    write_tokens(cfg, tokens)
    log("Access token refreshed.")
    return tokens


# ------------------------------------------------------------------------- posts

def build_post_payload(author: str, commentary: str, image_urn: str | None = None,
                       alt_text: str | None = None) -> dict[str, Any]:
    """The JSON body for POST /rest/posts. Pure, so it can be tested."""
    payload: dict[str, Any] = {
        "author": author,
        "commentary": commentary,
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                         "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if image_urn:
        payload["content"] = {"media": {"id": image_urn, "altText": alt_text or ""}}
    return payload


def upload_image(session: requests.Session, author: str, image_path: Path) -> str:
    init = session.post(f"{IMAGES_URL}?action=initializeUpload",
                        json={"initializeUploadRequest": {"owner": author}}, timeout=60)
    if init.status_code not in (200, 201):
        raise PublisherError(f"Starting the image upload failed ({init.status_code}): {init.text}")
    value = init.json()["value"]
    put = requests.put(value["uploadUrl"], data=image_path.read_bytes(),
                       headers={"Authorization": session.headers["Authorization"]}, timeout=180)
    if put.status_code not in (200, 201):
        raise PublisherError(f"Uploading the image failed ({put.status_code}): {put.text}")
    return str(value["image"])


def publish(cfg: Config, creds: dict[str, str], tokens: dict[str, Any], commentary: str,
            image: Path | None = None, alt_text: str | None = None,
            posts_url: str = POSTS_URL) -> str:
    """Post it. Returns the post URN."""
    from .littletext import find_unescaped

    leftover = find_unescaped(commentary)
    if leftover:
        raise PreflightRejected(
            f"unescaped reserved character ({', '.join(sorted(set(leftover)))}), not sent")

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {tokens['access_token']}",
        "LinkedIn-Version": api_version(cfg, creds),
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    })
    author = tokens["author_urn"]
    image_urn = upload_image(session, author, image) if image else None
    try:
        resp = session.post(posts_url, json=build_post_payload(author, commentary, image_urn, alt_text),
                            timeout=60)
    except requests.RequestException as exc:
        raise PublishUncertain(f"No clear answer from LinkedIn ({exc}). The post may be live.") from exc
    if resp.status_code == 426:
        raise PublisherError(f"LinkedIn no longer accepts API version {api_version(cfg, creds)}. "
                             "Set a newer LINKEDIN_API_VERSION (format YYYYMM) in your .env. "
                             f"Response: {resp.text}")
    if resp.status_code >= 500:
        raise PublishUncertain(f"LinkedIn answered with a server error ({resp.status_code}). "
                               "The post may be live.")
    if resp.status_code not in (200, 201):
        raise PublisherError(f"LinkedIn refused the post ({resp.status_code}): {resp.text}")
    urn = resp.headers.get("x-restli-id") or ""
    if not urn and resp.text:
        try:
            urn = resp.json().get("id", "")
        except ValueError:
            urn = ""
    if not urn:
        raise PublishUncertain("LinkedIn accepted the post but returned no id. It is probably live.")
    return urn


def verify_online(cfg: Config) -> str:
    """Read-only check that the stored token works. Posts nothing."""
    tokens = read_tokens(cfg)
    resp = requests.get(USERINFO_URL, headers={"Authorization": f"Bearer {tokens['access_token']}"},
                        timeout=30)
    if resp.status_code == 401:
        raise PublisherError("LinkedIn rejects the token (401). Run 'linkedin-publisher auth' again.")
    if resp.status_code != 200:
        raise PublisherError(f"userinfo failed ({resp.status_code}): {resp.text}")
    member = f"urn:li:person:{resp.json().get('sub', '')}"
    if member != tokens.get("author_urn"):
        raise PublisherError(f"Token belongs to {member}, but posts would go out as "
                             f"{tokens.get('author_urn')}. Run 'auth' again.")
    return resp.json().get("name") or member


def post_url_for(post_urn: str) -> str:
    return f"https://www.linkedin.com/feed/update/{post_urn}/"
