"""Checks before sending: addresses a post names, and what each of them must answer.

A post lists its checks in a block scalar, one per line:

    checks: |
      https://example.org/blog/my-article
      https://example.org/waitlist | Join the waiting list

An address on its own must answer 200 once redirects are followed. With `` | text`` the page
must also contain that text, exactly as written. A line that cannot be read counts as a check
that failed: a check nobody can run must not let the post through.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

from . import __version__

SEPARATOR = " | "
MAX_BYTES = 5 * 1024 * 1024          # a page is read up to this far when it has to contain a text
UNREADABLE = "cannot be read. Write an address, or an address, then ' | ', then the text it must contain."


@dataclass(frozen=True)
class Check:
    line: str                        # as written, without the surrounding blanks
    url: str = ""
    text: str | None = None
    problem: str | None = None       # set when the line cannot be read


@dataclass(frozen=True)
class Failure:
    check: Check
    kind: str                        # unreadable | network | status | text
    detail: str

    @property
    def ref(self) -> str:
        """Names the check and the kind of failure, so a different failure is news and a repeat is not."""
        return f"{self.check.line} [{self.kind}]"

    def describe(self) -> str:
        return f"{self.check.line}: {self.detail}"


def parse(value: str | None) -> list[Check]:
    """The checks of a post. Blank lines are skipped, every other line is one check."""
    checks = []
    for raw in (value or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        url, separator, text = line.partition(SEPARATOR)
        url, text = url.strip(), text.strip()
        if _address(url):
            checks.append(Check(line=line, url=url, text=text if separator else None))
        else:
            checks.append(Check(line=line, problem=UNREADABLE))
    return checks


def _address(url: str) -> bool:
    if not url or re.search(r"\s", url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def run(check: Check, timeout: float) -> Failure | None:
    """Ask for the address once. None means the check passed."""
    if check.problem:
        return Failure(check, "unreadable", check.problem)
    headers = {"User-Agent": f"linkedin-publisher/{__version__} (check before sending)"}
    try:
        with requests.get(check.url, headers=headers, timeout=timeout, allow_redirects=True, stream=True) as response:
            if response.status_code != 200:
                return Failure(check, "status", f"answered {response.status_code}, expected 200")
            if check.text is None:
                return None
            body = _read(response, timeout)
    except requests.Timeout:
        return Failure(check, "network", f"timed out after {timeout:g} seconds")
    except requests.RequestException as exc:
        return Failure(check, "network", f"could not be reached: {str(exc)[:200]}")
    if check.text not in body:
        return Failure(check, "text", f'answered 200 but the page does not contain "{check.text}"')
    return None


def run_all(checks: list[Check], timeout: float) -> list[Failure]:
    """Every check is asked, so one run names everything that is wrong."""
    return [failure for check in checks if (failure := run(check, timeout)) is not None]


def _read(response: requests.Response, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    data = bytearray()
    for chunk in response.iter_content(chunk_size=65536):
        data += chunk
        if len(data) >= MAX_BYTES:
            break
        if time.monotonic() > deadline:
            raise requests.Timeout()
    declared = re.search(r"charset=([^\s;]+)", response.headers.get("Content-Type", ""), re.IGNORECASE)
    try:
        return bytes(data).decode(declared.group(1).strip("\"'") if declared else "utf-8", errors="replace")
    except LookupError:
        return bytes(data).decode("utf-8", errors="replace")
