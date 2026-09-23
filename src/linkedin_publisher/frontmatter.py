"""A small, predictable frontmatter reader.

It supports what a post file needs and nothing more:

- flat ``key: value`` pairs that start at column 0
- quoted values (``"..."`` or ``'...'``), so a value can contain `` #``
- a trailing ``# comment`` after an unquoted value is ignored, as in YAML
- block scalars (``key: |`` keeps line breaks, ``key: >`` joins lines)

Indented lines that do not belong to a block scalar are ignored. That matters:
a stray indented line such as ``  slot: 2099-01-01`` must never override the
real ``slot`` key.
"""
from __future__ import annotations

import re

KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)[ \t]*:(.*)$")
BLOCK_RE = re.compile(r"^[|>][+-]?[0-9]?$")
COMMENT_RE = re.compile(r"[ \t]+#.*$")


def split(raw: str) -> tuple[list[str], str]:
    """Return (frontmatter lines, body). No frontmatter means ([], raw)."""
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return [], raw
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            body = "\n".join(lines[index + 1:])
            return lines[1:index], body.lstrip("\n")
    return [], raw


def _scalar(value: str) -> str:
    value = value.strip()
    if value[:1] in ('"', "'"):
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end != -1 else value[1:]
    return COMMENT_RE.sub("", value).strip()


def parse(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    index = 0
    while index < len(lines):
        match = KEY_RE.match(lines[index])
        if not match:
            index += 1
            continue
        key, rest = match.group(1), match.group(2)
        indicator = COMMENT_RE.sub("", rest).strip()
        if BLOCK_RE.match(indicator):
            index += 1
            block: list[str] = []
            while index < len(lines) and (lines[index][:1] in (" ", "\t") or not lines[index].strip()):
                block.append(lines[index])
                index += 1
            filled = [line for line in block if line.strip()]
            indent = min((len(line) - len(line.lstrip()) for line in filled), default=0)
            dedented = [line[indent:] if line.strip() else "" for line in block]
            while dedented and not dedented[-1]:
                dedented.pop()
            if indicator.startswith("|"):
                out[key] = "\n".join(dedented)
            else:
                out[key] = " ".join(line.strip() for line in dedented if line.strip())
            continue
        out[key] = _scalar(rest)
        index += 1
    return out
