"""A number in a post that is measured shortly before the post goes out.

A post names a measure and the sentence the number sits in:

    measure: open-items
    measure_line: "There are {n} items left."

The measure is a command in the configuration (``[measures.open-items]``), never a command
in the post file. ``{n}`` stands for the digits. The approval key masks exactly those digits
(see ``approval.approval_key``), so a person approves the post once and the number can change
afterwards, while every other change to the post still voids the approval.

Changing the number changes the identity of the post: receipts and claims are kept per text,
digits included. That is why ``refusal`` exists. A number that changes after a send attempt
would be a post that no receipt and no claim knows, and it could go out a second time.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import claims
from . import postqueue as queue
from .api import PublisherError
from .config import Config, Measure

PLACEHOLDER = "{n}"
MASK = "{n}"            # what stands in the key where the digits were
MAX_DIGITS = 12         # a count of anything has no more than this; a longer run of digits is a bad pattern
DIGITS_RE = re.compile(r"[0-9]{1,%d}" % MAX_DIGITS)


class MeasureLineError(PublisherError):
    """The measure line of a post cannot be used. ``ref`` is a short, stable name for the reason."""

    def __init__(self, message: str, ref: str):
        super().__init__(message)
        self.ref = ref


class MeasureFailed(Exception):
    """The command did not give a usable number. ``ref`` is a short, stable name for the reason."""

    def __init__(self, ref: str, detail: str):
        super().__init__(detail)
        self.ref = ref
        self.detail = detail


@dataclass(frozen=True)
class MeasureLine:
    field: str
    template: str
    regex: re.Pattern[str]

    def find(self, text: str) -> re.Match[str]:
        """The one place in the post text where the line stands. Group 1 is the number."""
        found = list(self.regex.finditer(text))
        if len(found) == 1:
            return found[0]
        said = ("does not match anything in the post text" if not found
                else f"matches {len(found)} places in the post text")
        raise MeasureLineError(f"{self.field} {said}. It must match exactly once.",
                               ref=f"measure-line:{len(found)}")


def parse_line(template: str, field: str) -> MeasureLine:
    """Everything in the template is literal except ``{n}``, which stands for the digits."""
    if len(template.splitlines()) != 1:
        raise MeasureLineError(f"{field} must be one line.", ref="measure-line:template")
    if template.count(PLACEHOLDER) != 1:
        raise MeasureLineError(f"{field} must contain {PLACEHOLDER} exactly once, where the number stands.",
                               ref="measure-line:template")
    before, after = template.split(PLACEHOLDER)
    return MeasureLine(field=field, template=template,
                       regex=re.compile(f"{re.escape(before)}(\\d+){re.escape(after)}", re.ASCII))


def line_of(post: queue.Post, cfg: Config) -> MeasureLine | None:
    """The measure line of a post, or None when it has none."""
    template = post.meta.get(cfg.measure_line_field)
    return parse_line(template, cfg.measure_line_field) if template else None


def masked(text: str, found: re.Match[str]) -> str:
    """The text with the digits replaced by a fixed token."""
    return text[:found.start(1)] + MASK + text[found.end(1):]


# ---------------------------------------------------------------- when measuring is allowed

def refusal(cfg: Config, post: queue.Post, slot: dt.datetime, now: dt.datetime) -> str | None:
    """Why the number of this post must not be changed now, or None when it may.

    The text of the post is its identity in the receipts and in the claims. Once the slot has
    been reached, or once anything has been tried with this text, a different number is a
    different post as far as those guards can tell."""
    if now >= slot:
        return f"its slot ({slot:%Y-%m-%d %H:%M}) has been reached"
    key = queue.content_key(queue.post_text(post.body, cfg.notes_headings))
    state = queue.load_states(cfg).get(key)
    if state == "placed":
        return "a receipt shows this text was already posted"
    if state in queue.UNCERTAIN_STAGES:
        return "an earlier attempt with this text may have gone through"
    if claims.exists(cfg, key):
        return "a claim exists for this text"
    return None


# ------------------------------------------------------------------- running the command

def read_number(measure: Measure, cwd: Path) -> str:
    """Run the command of a measure and return the digits it printed. Raises MeasureFailed.

    The command runs without a shell, from the folder of the configuration, and is stopped
    after its timeout together with whatever it started. The pattern must find exactly one
    number in what it printed: a pattern that fits two lines would otherwise quietly pick the
    wrong one."""
    try:
        process = subprocess.Popen(list(measure.command), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=cwd, start_new_session=os.name != "nt")
    except (OSError, ValueError) as exc:
        raise MeasureFailed("command-missing", f"the command could not be started: {exc}") from exc
    try:
        out, err = process.communicate(timeout=measure.timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill(process)
        process.communicate()
        raise MeasureFailed("timeout", f"the command timed out after {measure.timeout_seconds:g} seconds") from None
    if process.returncode != 0:
        said = err.decode("utf-8", "replace").strip()[-200:]
        raise MeasureFailed(f"exit-code-{process.returncode}",
                            f"the command exited with code {process.returncode}" + (f": {said}" if said else ""))
    found = list(measure.pattern.finditer(out.decode("utf-8", "replace")))
    if not found:
        raise MeasureFailed("no-match", "no line of the output matches the pattern")
    if len(found) > 1:
        raise MeasureFailed("ambiguous", f"the pattern matches {len(found)} places in the output, it must match one")
    digits = found[0].group(1)
    if digits is None or not DIGITS_RE.fullmatch(digits):
        raise MeasureFailed("bad-value", f"the pattern found {(digits or '')[:40]!r}, not a whole number "
                                         f"of at most {MAX_DIGITS} digits")
    return digits


def _kill(process: subprocess.Popen) -> None:
    """End the command and its children. A wrapper script that is stopped alone leaves its child
    holding the output pipe open, and the run would wait for that child instead."""
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# ----------------------------------------------------------------------- writing the digits

def replace_number(raw: str, found: re.Match[str], digits: str) -> str:
    """The file text with the digits of the measure line replaced and every other character as it was.

    ``found`` is the match in the post text. The post text has its line endings normalised and its
    private notes cut off, so the sentence is looked up again in the file, from the end of the
    frontmatter on. The post text is the start of that body, so the first hit is the one in the
    post and not a copy in the notes."""
    lines = raw.splitlines(keepends=True)
    closing = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    body_start = sum(len(line) for line in lines[:closing + 1])
    sentence = found.group(0)
    at = raw.find(sentence, body_start)
    if at < 0:
        raise PublisherError("the measure line could not be found in the file")
    first = at + found.start(1) - found.start(0)
    return raw[:first] + digits + raw[first + len(found.group(1)):]
