"""The claim: the one step before sending that two processes cannot both pass.

Every other guard (receipts, the lock file) reads first and writes later. Two runs that
read at the same moment both pass them. Creating a file with O_EXCL is decided by the
filesystem, so exactly one process gets it.

    <receipts_dir>/claims/<content_key>.claim      {"pid", "file", "route", "claimed_at"}

A run takes the claim after the receipt checks and before the ``attempt`` receipt and the
first call to LinkedIn. After ``placed`` the claim stays for good. After ``failed`` and
``preflight-rejected`` nothing was sent, so the claim is removed. After ``uncertain`` or a
crash it stays, and only a person who checked LinkedIn takes it over, with ``--retry``.
A second route that implements the same file contract shares these claims.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config

ROUTE = "linkedin-publisher"
# A claim file exists a moment before its content does, and a run that dies in that moment
# leaves it empty. --retry leaves such a claim alone this long, then treats it as abandoned.
UNREADABLE_GRACE_SECONDS = 60


class ClaimHeld(Exception):
    """A takeover of an existing claim is not allowed. The message says why."""


@dataclass
class Claim:
    key: str
    path: Path
    pid: int | None
    file: str | None
    claimed_at: dt.datetime     # timezone-aware


def claims_dir(cfg: Config) -> Path:
    return cfg.receipts_dir / "claims"


def claim_path(cfg: Config, key: str) -> Path:
    return claims_dir(cfg) / f"{key}.claim"


def exists(cfg: Config, key: str) -> bool:
    return claim_path(cfg, key).exists()


def take(cfg: Config, key: str, file_name: str) -> bool:
    """Create the claim. True when this process got it, False when one already exists."""
    path = claim_path(cfg, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    row = {"pid": os.getpid(), "file": file_name, "route": ROUTE,
           "claimed_at": dt.datetime.now(dt.UTC).isoformat()}
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(row))
    except OSError:
        path.unlink(missing_ok=True)     # nothing was sent: an empty claim would only block the post
        raise
    return True


def release(cfg: Config, key: str) -> None:
    """Only for a run that is sure nothing was sent."""
    claim_path(cfg, key).unlink(missing_ok=True)


def read(cfg: Config, key: str) -> Claim | None:
    """A claim whose content cannot be read still counts as a claim, aged by its file time."""
    path = claim_path(cfg, key)
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return _parse(key, path, fh.read(), os.fstat(fh.fileno()))
    except FileNotFoundError:
        return None


def _parse(key: str, path: Path, raw: str, stat: os.stat_result) -> Claim:
    try:
        row = json.loads(raw)
    except ValueError:
        row = None
    if not isinstance(row, dict):
        row = {}
    pid = row.get("pid")
    file_name = row.get("file")
    return Claim(key=key, path=path,
                 pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
                 file=file_name if isinstance(file_name, str) else None,
                 claimed_at=_parse_time(row.get("claimed_at")) or dt.datetime.fromtimestamp(stat.st_mtime, dt.UTC))


def keys(cfg: Config) -> list[str]:
    """The content keys that have a claim. Retired claims have another suffix and are not listed."""
    try:
        return sorted(name[:-len(".claim")] for name in os.listdir(claims_dir(cfg))
                      if name.endswith(".claim"))
    except FileNotFoundError:
        return []


def _parse_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.astimezone()


def process_alive(pid: int) -> bool:
    """Whether a process with this id exists. Where that cannot be asked safely (Windows, where
    os.kill ends the process) the answer is yes, which keeps --retry out until a person acts."""
    if os.name == "nt":
        return True
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True           # it exists, it just belongs to someone else
    return True


def supersede(cfg: Config, key: str, may_proceed: Callable[[], bool]) -> None:
    """Make room for ``--retry``: take an existing claim out of the way.

    Returns when the claim is gone and the caller should call take(). Raises ClaimHeld
    when it must not go on. Four things stand between a retry and a second post:

    1. A claim whose process still runs is not stale, it is a send in progress. A claim
       that names no process (empty or unreadable) is treated the same for a minute: its
       owner may be between creating the file and writing it.
    2. ``may_proceed`` is asked after that process is known to be gone. It re-reads the
       receipts, because the holder may have placed the post since the caller looked.
    3. Of several retries on the same claim exactly one wins. Each links the claim under a
       name built from that claim's identity (inode and time). link() fails when the name
       exists, so the first wins and the rest stop. The linked file is kept as evidence.
    4. What was inspected and what was linked must be the same file. Identity and content
       are read from one open file, and the link is checked against that identity.
    """
    path = claim_path(cfg, key)
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            stat = os.fstat(fh.fileno())
            holder = _parse(key, path, fh.read(), stat)
    except FileNotFoundError:
        return
    if holder.pid is None:
        age = (dt.datetime.now(dt.UTC) - holder.claimed_at).total_seconds()
        if age < UNREADABLE_GRACE_SECONDS:
            raise ClaimHeld("the claim names no process. It may just be written, or its run may have "
                            f"died while writing it. Try again in {UNREADABLE_GRACE_SECONDS} seconds.")
    elif holder.pid != os.getpid() and process_alive(holder.pid):
        raise ClaimHeld(f"process {holder.pid} still holds the claim for this post, so it may still be "
                        f"sending. Wait for it to finish. If it is stuck, remove {path} yourself.")
    if not may_proceed():
        raise ClaimHeld("a receipt shows this was already posted.")
    retired = path.with_name(f"{key}.claim.retired-{stat.st_ino}-{stat.st_mtime_ns}")
    try:
        os.link(path, retired)
    except FileExistsError:
        raise ClaimHeld("another retry took over this claim first.") from None
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ClaimHeld(f"the claim cannot be taken over safely here ({exc}). After you checked "
                        f"LinkedIn, remove {path} yourself.") from exc
    linked = os.stat(retired)
    if (linked.st_ino, linked.st_dev) != (stat.st_ino, stat.st_dev):
        retired.unlink(missing_ok=True)      # somebody else's claim was in the path by now: not ours to remove
        raise ClaimHeld("the claim changed while it was being taken over. Check LinkedIn and try again.")
    path.unlink(missing_ok=True)
