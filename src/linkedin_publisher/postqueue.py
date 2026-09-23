"""The queue: Markdown files with a slot, and what happens to them after posting."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import frontmatter, littletext
from .config import TIME_RE, Config

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif")


@dataclass
class Post:
    path: Path
    meta: dict[str, str]
    body: str
    fm_lines: list[str]
    image: Path | None
    due: dt.datetime | None = None
    reason: str = ""
    stranded: bool = False


def find_image(md_path: Path) -> Path | None:
    """An image belongs to a post when it has the same name: post.md + post.png."""
    for suffix in IMAGE_SUFFIXES:
        candidate = md_path.with_suffix(suffix)
        if candidate.exists():
            return candidate
    return None


def read_post(md_path: Path) -> Post:
    raw = md_path.read_text(encoding="utf-8")
    fm_lines, body = frontmatter.split(raw)
    return Post(path=md_path, meta=frontmatter.parse(fm_lines), body=body.strip(),
                fm_lines=fm_lines, image=find_image(md_path))


def post_text(body: str, notes_headings: tuple[str, ...]) -> str:
    """The part that gets posted: everything up to a line with only ``---`` or
    up to a heading named in notes_headings. What follows is private notes."""
    alternatives = [r"---[ \t]*$"]
    alternatives += [rf"#{{1,6}}[ \t]+{re.escape(h)}\b" for h in notes_headings]
    cut = re.search(rf"^[ \t]*(?:{'|'.join(alternatives)})", body, flags=re.M)
    return (body[:cut.start()] if cut else body).strip()


def content_key(text: str) -> str:
    """Identity of a post by its text, not by its filename.

    A renamed file, or a status someone set back by hand, must not break the
    link with a placement that already happened."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slot_datetime(meta: dict[str, str], cfg: Config) -> dt.datetime | None:
    raw_slot = meta.get("slot")
    if not raw_slot:
        return None
    try:
        day = dt.date.fromisoformat(raw_slot)
    except ValueError:
        return None
    explicit = meta.get("slot_time")
    if explicit:
        match = TIME_RE.match(explicit)
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2))
    else:
        hour, minute = (int(x) for x in cfg.slot_times[day.weekday()].split(":"))
    return dt.datetime.combine(day, dt.time(hour, minute))


def content_problem(post: Post, cfg: Config) -> str | None:
    """Objections to the content itself, regardless of timing."""
    text = post_text(post.body, cfg.notes_headings)
    if not text:
        return "empty post text"
    if post.meta.get("format") == "image":
        if not post.image:
            return "format is image but there is no image file next to the post"
        if not post.meta.get("alt_text"):
            return "image without alt_text"
    leftover = littletext.find_unescaped(littletext.prepare(text))
    if leftover:
        return f"preflight refused: unescaped reserved character ({', '.join(sorted(set(leftover)))})"
    return None


def queue_files(cfg: Config) -> list[Path]:
    return sorted(p for p in cfg.queue_dir.glob("*.md") if p.name.lower() != "readme.md")


def collect(cfg: Config, now: dt.datetime) -> tuple[list[Post], list[Post]]:
    """Return (ready to post, skipped with a reason)."""
    ready: list[Post] = []
    skipped: list[Post] = []
    placed = load_placed_keys(cfg)
    for md_path in queue_files(cfg):
        try:
            post = read_post(md_path)
        except OSError as exc:
            # One unreadable file must not stop the rest of the queue.
            skipped.append(Post(md_path, {}, "", [], None, reason=f"read error: {exc}"))
            continue

        # First and strongest check: a receipt that proves this text was placed.
        # This covers a post that went live but whose file could not be moved.
        if content_key(post_text(post.body, cfg.notes_headings)) in placed:
            post.reason = "a receipt shows this was already posted, file is ignored"
            skipped.append(post)
            continue

        status = post.meta.get("status")
        if status != cfg.ready_status:
            post.reason = f"status is {status or 'empty'}, not {cfg.ready_status}"
            skipped.append(post)
            continue

        due = slot_datetime(post.meta, cfg)
        if due is None:
            post.reason = "no usable slot in the frontmatter"
            skipped.append(post)
            continue
        post.due = due

        expires = post.meta.get(cfg.expires_key)
        if expires:
            try:
                if dt.date.fromisoformat(expires) < now.date():
                    post.reason = f"{cfg.expires_key} {expires} has passed"
                    skipped.append(post)
                    continue
            except ValueError:
                pass

        if due > now:
            post.reason = f"scheduled for {due:%Y-%m-%d %H:%M}"
            skipped.append(post)
            continue
        if now - due > dt.timedelta(minutes=cfg.grace_minutes):
            post.stranded = True
            post.reason = (f"slot {due:%Y-%m-%d %H:%M} is more than {cfg.grace_minutes} "
                           "minutes ago, not posting late")
            skipped.append(post)
            continue

        problem = content_problem(post, cfg)
        if problem:
            post.reason = problem
            skipped.append(post)
            continue
        ready.append(post)

    ready.sort(key=lambda p: p.due or now)
    return ready, skipped


def _unique_target(directory: Path, name: str) -> Path:
    """Never overwrite a file that is already in the published folder."""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 2
    while True:
        candidate = directory / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def move_to_published(post: Post, cfg: Config, post_url: str, posted_on: dt.date) -> Path:
    managed = {"status", "slot", "slot_time", cfg.expires_key, "posted_date", "linkedin_url"}
    kept = []
    for line in post.fm_lines:
        match = frontmatter.KEY_RE.match(line)
        if match and match.group(1) in managed:
            continue
        kept.append(line)
    kept += [f"status: {cfg.published_status}",
             f"posted_date: {posted_on.isoformat()}",
             f"linkedin_url: {post_url}"]
    new_raw = "---\n" + "\n".join(kept) + "\n---\n\n" + post.body.strip() + "\n"

    cfg.published_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_target(cfg.published_dir, post.path.name)
    target.write_text(new_raw, encoding="utf-8")
    post.path.unlink()
    if post.image:
        shutil.move(str(post.image), str(_unique_target(cfg.published_dir, post.image.name)))
    return target


# ---------------------------------------------------------------- receipts
#
# Every attempt leaves a line behind, before the file is moved. If the move
# fails afterwards, the proof that the post is live is already on disk, and
# collect() will never offer that text again.

def write_receipt(cfg: Config, stage: str, post: Post | None = None, *,
                  post_urn: str | None = None, post_url: str | None = None,
                  error: str | None = None) -> None:
    """Append-only NDJSON, one file per day.

    stage: placed | preflight-rejected | failed | move-failed"""
    now = dt.datetime.now(dt.UTC)
    row: dict[str, Any] = {
        "stage": stage,
        "file": post.path.name if post else None,
        "content_key": content_key(post_text(post.body, cfg.notes_headings)) if post else None,
        "timestamp": now.isoformat(),
        "post_urn": post_urn,
        "post_url": post_url,
        "error": error,
    }
    cfg.receipts_dir.mkdir(parents=True, exist_ok=True)
    with (cfg.receipts_dir / f"{now:%Y-%m-%d}.ndjson").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def load_placed_keys(cfg: Config) -> set[str]:
    keys: set[str] = set()
    if not cfg.receipts_dir.is_dir():
        return keys
    for receipt_file in sorted(cfg.receipts_dir.glob("*.ndjson")):
        for line in receipt_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("stage") == "placed" and row.get("content_key"):
                keys.add(row["content_key"])
    return keys
