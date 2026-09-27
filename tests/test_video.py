"""Video upload against a local stand-in for LinkedIn: parts, ETags, processing, the post."""
import http.server
import json
import threading
import urllib.parse

import pytest

from linkedin_publisher import api

CREDS = {"LINKEDIN_CLIENT_ID": "x", "LINKEDIN_CLIENT_SECRET": "y"}
TOKENS = {"access_token": "t", "author_urn": "urn:li:person:x"}
VIDEO_URN = "urn:li:video:V1"
PART = 4 * 1024 * 1024


class FakeLinkedIn:
    """Answers the calls the Videos and Posts APIs get, and remembers what came in."""

    def __init__(self, statuses=("PROCESSING", "AVAILABLE"), failure_reason=None,
                 part_status=200, etag=True):
        self.statuses = list(statuses)
        self.failure_reason = failure_reason
        self.part_status = part_status
        self.etag = etag
        self.init_body = None
        self.parts: dict[int, bytes] = {}
        self.finalize_body = None
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
                    size = body["initializeUploadRequest"]["fileSizeBytes"]
                    parts = [{"uploadUrl": f"{fake.base}/upload/{i}", "firstByte": first,
                              "lastByte": min(first + PART, size) - 1}
                             for i, first in enumerate(range(0, size, PART))]
                    self._send(200, {"value": {"video": VIDEO_URN, "uploadToken": "tok",
                                               "uploadInstructions": parts}})
                elif self.path.endswith("action=finalizeUpload"):
                    fake.finalize_body = body
                    self._send(200)
                elif self.path == "/rest/posts":
                    fake.post_body = body
                    self._send(201, headers={"x-restli-id": "urn:li:share:9"})
                else:
                    self._send(404)

            def do_PUT(self):  # noqa: N802
                index = int(self.path.rsplit("/", 1)[1])
                fake.parts[index] = self._body()
                headers = {"ETag": f'"etag-{index}"'} if fake.etag else {}
                self._send(fake.part_status, headers=headers)

            def do_GET(self):  # noqa: N802
                assert urllib.parse.unquote(self.path) == f"/rest/videos/{VIDEO_URN}"
                fake.status_calls += 1
                status = fake.statuses.pop(0) if len(fake.statuses) > 1 else fake.statuses[0]
                info = {"id": VIDEO_URN, "status": status}
                if fake.failure_reason:
                    info["processingFailureReason"] = fake.failure_reason
                self._send(200, info)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def publish(self, cfg, video, **kwargs):
        return api.publish(cfg, CREDS, TOKENS, "Hello", posts_url=f"{self.base}/rest/posts",
                           video=video, videos_url=f"{self.base}/rest/videos", **kwargs)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(api, "VIDEO_POLL_SECONDS", 0)
    monkeypatch.setattr(api.time, "sleep", lambda _s: None)


def make_video(tmp_path, size):
    path = tmp_path / "clip.mp4"
    path.write_bytes(bytes(i % 251 for i in range(size)))
    return path


def test_video_is_uploaded_in_parts_and_posted_once_available(cfg, tmp_path):
    video = make_video(tmp_path, PART * 2 + 1234)
    fake = FakeLinkedIn()
    assert fake.publish(cfg, video, title="How it works") == "urn:li:share:9"

    assert fake.init_body == {"initializeUploadRequest": {
        "owner": "urn:li:person:x", "fileSizeBytes": PART * 2 + 1234,
        "uploadCaptions": False, "uploadThumbnail": False}}
    assert b"".join(fake.parts[i] for i in sorted(fake.parts)) == video.read_bytes()
    assert fake.finalize_body == {"finalizeUploadRequest": {
        "video": VIDEO_URN, "uploadToken": "tok",
        "uploadedPartIds": ["etag-0", "etag-1", "etag-2"]}}
    assert fake.status_calls == 2
    assert fake.post_body["content"] == {"media": {"id": VIDEO_URN, "title": "How it works"}}


def test_processing_failure_stops_before_the_post(cfg, tmp_path):
    fake = FakeLinkedIn(statuses=("PROCESSING_FAILED",), failure_reason="unsupported codec")
    with pytest.raises(api.PublisherError, match="unsupported codec"):
        fake.publish(cfg, make_video(tmp_path, 100_000))
    assert fake.post_body is None


def test_video_that_never_becomes_available_is_not_posted(cfg, tmp_path, monkeypatch):
    fake = FakeLinkedIn(statuses=("PROCESSING",))
    clock = iter(range(0, 10_000, 60))
    monkeypatch.setattr(api.time, "monotonic", lambda: next(clock))
    with pytest.raises(api.PublisherError, match="Nothing was posted"):
        fake.publish(cfg, make_video(tmp_path, 100_000))
    assert fake.post_body is None


def test_refused_part_stops_the_upload(cfg, tmp_path):
    fake = FakeLinkedIn(part_status=400)
    with pytest.raises(api.PublisherError, match="video part 1"):
        fake.publish(cfg, make_video(tmp_path, 100_000))
    assert fake.finalize_body is None and fake.post_body is None


def test_part_without_etag_cannot_be_finalized(cfg, tmp_path):
    fake = FakeLinkedIn(etag=False)
    with pytest.raises(api.PublisherError, match="no ETag"):
        fake.publish(cfg, make_video(tmp_path, 100_000))
    assert fake.finalize_body is None


def test_image_and_video_together_are_refused(cfg, tmp_path):
    image = tmp_path / "a.png"
    image.write_bytes(b"x")
    with pytest.raises(api.PublisherError, match="not both"):
        FakeLinkedIn().publish(cfg, make_video(tmp_path, 100_000), image=image)


def test_video_payload_has_no_alt_text_and_title_is_optional():
    payload = api.build_post_payload("urn:li:person:x", "Hi", VIDEO_URN, alt_text="ignored")
    assert payload["content"] == {"media": {"id": VIDEO_URN}}
