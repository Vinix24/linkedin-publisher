"""Document upload against a local stand-in for LinkedIn: one upload, processing, the post."""
import http.server
import json
import threading
import urllib.parse

import pytest

from linkedin_publisher import api

CREDS = {"LINKEDIN_CLIENT_ID": "x", "LINKEDIN_CLIENT_SECRET": "y"}
TOKENS = {"access_token": "t", "author_urn": "urn:li:person:x"}
DOCUMENT_URN = "urn:li:document:D1"


class FakeLinkedIn:
    """Answers the calls the Documents and Posts APIs get, and remembers what came in."""

    def __init__(self, statuses=("PROCESSING", "AVAILABLE"), failure_reason=None, upload_status=201):
        self.statuses = list(statuses)
        self.failure_reason = failure_reason
        self.upload_status = upload_status
        self.init_body = None
        self.uploaded = None
        self.upload_auth = None
        self.status_calls = 0
        self.post_body = None
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length", 0)))

            def _send(self, status, payload=None, headers=None):
                raw = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):  # noqa: N802
                body = json.loads(self._body() or b"{}")
                if self.path.endswith("action=initializeUpload"):
                    fake.init_body = body
                    self._send(200, {"value": {"uploadUrl": f"{fake.base}/upload",
                                               "uploadUrlExpiresAt": 1,
                                               "document": DOCUMENT_URN}})
                elif self.path == "/rest/posts":
                    fake.post_body = body
                    self._send(201, headers={"x-restli-id": "urn:li:share:9"})
                else:
                    self._send(404)

            def do_PUT(self):  # noqa: N802
                assert self.path == "/upload"
                fake.uploaded = self._body()
                fake.upload_auth = self.headers.get("Authorization")
                self._send(fake.upload_status)

            def do_GET(self):  # noqa: N802
                assert urllib.parse.unquote(self.path) == f"/rest/documents/{DOCUMENT_URN}"
                fake.status_calls += 1
                status = fake.statuses.pop(0) if len(fake.statuses) > 1 else fake.statuses[0]
                info = {"id": DOCUMENT_URN, "status": status}
                if fake.failure_reason:
                    info["processingFailureReason"] = fake.failure_reason
                self._send(200, info)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def publish(self, cfg, document, **kwargs):
        return api.publish(cfg, CREDS, TOKENS, "Hello", posts_url=f"{self.base}/rest/posts",
                           document=document, documents_url=f"{self.base}/rest/documents",
                           videos_url=f"{self.base}/rest/videos", **kwargs)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda _s: None)


def make_pdf(tmp_path, size=20_000):
    path = tmp_path / "deck.pdf"
    path.write_bytes(b"%PDF-1.7\n" + bytes(i % 251 for i in range(size)))
    return path


def test_document_is_uploaded_and_posted_once_available(cfg, tmp_path):
    pdf = make_pdf(tmp_path)
    fake = FakeLinkedIn()
    assert fake.publish(cfg, pdf, title="Five lessons") == "urn:li:share:9"

    assert fake.init_body == {"initializeUploadRequest": {"owner": "urn:li:person:x"}}
    assert fake.uploaded == pdf.read_bytes()
    assert fake.upload_auth == "Bearer t"
    assert fake.status_calls == 2
    assert fake.post_body["content"] == {"media": {"id": DOCUMENT_URN, "title": "Five lessons"}}


def test_processing_failure_stops_before_the_post(cfg, tmp_path):
    fake = FakeLinkedIn(statuses=("PROCESSING_FAILED",), failure_reason="too many pages")
    with pytest.raises(api.PublisherError, match="could not process the document: too many pages"):
        fake.publish(cfg, make_pdf(tmp_path), title="Deck")
    assert fake.post_body is None


def test_document_that_never_becomes_available_is_not_posted(cfg, tmp_path, monkeypatch):
    fake = FakeLinkedIn(statuses=("PROCESSING",))
    clock = iter(range(0, 10_000, 60))
    monkeypatch.setattr(api.time, "monotonic", lambda: next(clock))
    with pytest.raises(api.PublisherError, match="document was still PROCESSING.*Nothing was posted"):
        fake.publish(cfg, make_pdf(tmp_path), title="Deck")
    assert fake.post_body is None


def test_refused_upload_stops_before_the_post(cfg, tmp_path):
    fake = FakeLinkedIn(upload_status=400)
    with pytest.raises(api.PublisherError, match="Uploading the document failed"):
        fake.publish(cfg, make_pdf(tmp_path), title="Deck")
    assert fake.status_calls == 0 and fake.post_body is None


@pytest.mark.parametrize("other", ["image", "video"])
def test_a_document_never_goes_together_with_other_media(cfg, tmp_path, other, monkeypatch):
    media = tmp_path / ("a.png" if other == "image" else "a.mp4")
    media.write_bytes(b"x")
    fake = FakeLinkedIn()
    # Should the guard ever break, the image upload still lands here and not at LinkedIn.
    monkeypatch.setattr(api, "IMAGES_URL", f"{fake.base}/rest/images")
    with pytest.raises(api.PublisherError, match="never two"):
        fake.publish(cfg, make_pdf(tmp_path), title="Deck", **{other: media})
    assert fake.init_body is None and fake.post_body is None


def test_document_payload_has_a_title_and_no_alt_text():
    payload = api.build_post_payload("urn:li:person:x", "Hi", DOCUMENT_URN,
                                     alt_text="ignored", title="Five lessons")
    assert payload["content"] == {"media": {"id": DOCUMENT_URN, "title": "Five lessons"}}
