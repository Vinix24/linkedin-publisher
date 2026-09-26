import datetime as dt
import json

import pytest

from conftest import WEDNESDAY_10, write_post

from linkedin_publisher import postqueue as queue


def names(posts):
    return [p.path.name for p in posts]


def reason(skipped, name):
    return next(p.reason for p in skipped if p.path.name == name)


def test_due_post_is_ready(cfg):
    write_post(cfg, "a.md", {"status": "queued", "slot": "2026-09-23"})
    ready, skipped = queue.collect(cfg, WEDNESDAY_10)
    assert names(ready) == ["a.md"] and skipped == []


def test_future_post_waits(cfg):
    write_post(cfg, "a.md", {"status": "queued", "slot": "2026-09-24"})
    _, skipped = queue.collect(cfg, WEDNESDAY_10)
    assert reason(skipped, "a.md") == "scheduled for 2026-09-24 10:00"


def test_weekday_default_time_is_used(cfg):
    # 2026-09-27 is a Sunday, configured at 11:00
    assert queue.slot_datetime({"slot": "2026-09-27"}, cfg) == dt.datetime(2026, 9, 27, 11, 0)


def test_explicit_slot_time_wins(cfg):
    assert queue.slot_datetime({"slot": "2026-09-27", "slot_time": "16:30"}, cfg) == \
        dt.datetime(2026, 9, 27, 16, 30)


def test_unusable_slot_time_is_reported_not_guessed(cfg):
    write_post(cfg, "a.md", {"status": "queued", "slot": "2026-09-23", "slot_time": "tenish"})
    _, skipped = queue.collect(cfg, WEDNESDAY_10)
    assert reason(skipped, "a.md") == "no usable slot in the frontmatter"


def test_draft_is_never_posted(cfg):
    write_post(cfg, "a.md", {"status": "draft", "slot": "2026-09-23"})
    _, skipped = queue.collect(cfg, WEDNESDAY_10)
    assert reason(skipped, "a.md") == "status is draft, not queued"


def test_expired_post_is_skipped(cfg):
    write_post(cfg, "a.md", {"status": "queued", "slot": "2026-09-23", "expires": "2026-09-22"})
    _, skipped = queue.collect(cfg, WEDNESDAY_10)
    assert reason(skipped, "a.md") == "expires 2026-09-22 has passed"


def test_missed_slot_is_not_posted_late(cfg):
    write_post(cfg, "a.md", {"status": "queued", "slot": "2026-09-23"})
    _, skipped = queue.collect(cfg, WEDNESDAY_10 + dt.timedelta(minutes=91))
    assert skipped[0].stranded is True


def test_image_post_needs_image_and_alt_text(cfg):
    write_post(cfg, "a.md", {"status": "queued", "slot": "2026-09-23", "format": "image"})
    _, skipped = queue.collect(cfg, WEDNESDAY_10)
    assert "no image file" in reason(skipped, "a.md")
    (cfg.queue_dir / "a.png").write_bytes(b"png")
    _, skipped = queue.collect(cfg, WEDNESDAY_10)
    assert reason(skipped, "a.md") == "image without alt_text"


def test_notes_are_never_posted(cfg):
    body = "The post.\n\n## Image Brief\n\nsecret brief"
    assert queue.post_text(body, cfg.notes_headings) == "The post."
    assert queue.post_text("The post.\n\n---\n\nnotes", cfg.notes_headings) == "The post."


def test_receipt_blocks_a_second_placement(cfg):
    path = write_post(cfg, "a.md", {"status": "queued", "slot": "2026-09-23"})
    queue.write_receipt(cfg, "placed", queue.read_post(path), post_urn="urn:li:share:1")
    # Same text under a new name, as after a failed move plus a rename.
    path.rename(cfg.queue_dir / "renamed.md")
    ready, skipped = queue.collect(cfg, WEDNESDAY_10)
    assert ready == []
    assert "already posted" in reason(skipped, "renamed.md")


def test_failed_receipt_does_not_block(cfg):
    path = write_post(cfg, "a.md", {"status": "queued", "slot": "2026-09-23"})
    queue.write_receipt(cfg, "failed", queue.read_post(path), error="network")
    ready, _ = queue.collect(cfg, WEDNESDAY_10)
    assert names(ready) == ["a.md"]


def test_receipt_file_is_ndjson(cfg):
    path = write_post(cfg, "a.md", {"status": "queued", "slot": "2026-09-23"})
    queue.write_receipt(cfg, "placed", queue.read_post(path), post_urn="urn:li:share:1")
    rows = [json.loads(line) for f in cfg.receipts_dir.glob("*.ndjson")
            for line in f.read_text().splitlines()]
    assert rows[0]["stage"] == "placed" and rows[0]["file"] == "a.md"


def test_move_rewrites_frontmatter_and_keeps_other_fields(cfg):
    path = write_post(cfg, "a.md", {"status": "queued", "slot": "2026-09-23", "slot_time": "10:00",
                                    "expires": "2026-10-01", "pillar": "market"})
    (cfg.queue_dir / "a.png").write_bytes(b"png")
    target = queue.move_to_published(queue.read_post(path), cfg,
                                     "https://www.linkedin.com/feed/update/urn:li:share:1/",
                                     dt.date(2026, 9, 23))
    text = target.read_text()
    assert "pillar: market" in text
    assert "status: published" in text and "posted_date: 2026-09-23" in text
    assert "linkedin_url: https://www.linkedin.com/feed/update/urn:li:share:1/" in text
    assert "slot:" not in text and "expires:" not in text
    assert not path.exists() and (cfg.published_dir / "a.png").exists()


def test_move_never_overwrites(cfg):
    cfg.published_dir.mkdir()
    (cfg.published_dir / "a.md").write_text("an older post")
    path = write_post(cfg, "a.md", {"status": "queued", "slot": "2026-09-23"})
    target = queue.move_to_published(queue.read_post(path), cfg, "u", dt.date(2026, 9, 23))
    assert target.name == "a-2.md"
    assert (cfg.published_dir / "a.md").read_text() == "an older post"


def test_unreadable_queue_is_an_error_not_an_empty_queue(cfg, monkeypatch):
    # macOS privacy protection: a launchd job without Full Disk Access gets a
    # PermissionError on ~/Desktop, which Path.glob used to turn into "Nothing is due".
    write_post(cfg, "a.md", {"status": "queued", "slot": "2026-09-23"})

    def refuse(path):
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(queue.os, "listdir", refuse)
    with pytest.raises(queue.QueueUnreadable, match="Full Disk Access"):
        queue.collect(cfg, WEDNESDAY_10)


def test_missing_queue_folder_is_an_error(cfg):
    cfg.queue_dir.rmdir()
    with pytest.raises(queue.QueueUnreadable, match="not found"):
        queue.queue_files(cfg)


def test_queue_files_skips_readme_and_non_markdown(cfg):
    write_post(cfg, "b.md", {"status": "queued"})
    (cfg.queue_dir / "README.md").write_text("x", encoding="utf-8")
    (cfg.queue_dir / "b.png").write_bytes(b"")
    assert [p.name for p in queue.queue_files(cfg)] == ["b.md"]
