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


def set_fields(raw: str, values: dict[str, str]) -> str:
    """Return ``raw`` with each key set to its value, and every other byte untouched.

    ``values`` maps a key to the text after ``key: ``, quoting included. A key that is
    already there keeps its place (a value that was a block scalar loses its indented
    lines); a key that is not there is added at the end of the frontmatter. Line
    endings follow the file. A file without frontmatter raises ValueError.
    """
    lines = raw.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("the file has no frontmatter")
    closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if closing is None:
        raise ValueError("the frontmatter is never closed")
    eol = _eol(lines[0])

    out = lines[:1]
    seen: set[str] = set()
    index = 1
    while index < closing:
        line = lines[index]
        match = KEY_RE.match(line.rstrip("\r\n"))
        key = match.group(1) if match else None
        if key not in values:
            out.append(line)
            index += 1
            continue
        block = bool(BLOCK_RE.match(COMMENT_RE.sub("", match.group(2)).strip()))
        index += 1
        if block:
            end = index
            while end < closing and (lines[end][:1] in (" ", "\t") or not lines[end].strip()):
                end += 1
            while end > index and not lines[end - 1].strip():   # blank lines after the value are not part of it
                end -= 1
            index = end
        if key not in seen:       # a duplicate of the key is dropped, the first one counts
            seen.add(key)
            out.append(f"{key}: {values[key]}{_eol(line, eol)}")
    out.extend(f"{key}: {value}{eol}" for key, value in values.items() if key not in seen)
    out.extend(lines[closing:])
    return "".join(out)


def _eol(line: str, default: str = "\n") -> str:
    for ending in ("\r\n", "\n", "\r"):
        if line.endswith(ending):
            return ending
    return default
