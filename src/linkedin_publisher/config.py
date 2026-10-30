"""Configuration: one TOML file. Every path in it is resolved relative to that file."""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CONFIG_ENV = "LINKEDIN_PUBLISHER_CONFIG"
DEFAULT_CONFIG_NAME = "publisher.toml"
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
# Names the tool itself reads or writes in a post file. An approval field must not be one of them.
RESERVED_FIELDS = {"status", "slot", "slot_time", "format", "alt_text", "video_title",
                   "document_title", "posted_date", "linkedin_url"}


class ConfigError(Exception):
    """The configuration file is missing or contains a value that cannot be used."""


@dataclass
class Config:
    root: Path
    queue_dir: Path
    published_dir: Path
    env_file: Path
    tokens_file: Path
    receipts_dir: Path
    lock_file: Path
    log_file: Path
    heartbeat_file: Path
    ready_status: str = "queued"
    published_status: str = "published"
    expires_key: str = "expires"
    notes_headings: tuple[str, ...] = ("Notes",)
    slot_times: dict[int, str] = field(default_factory=lambda: {i: "10:00" for i in range(7)})
    grace_minutes: int = 90
    redirect_port: int = 8765
    api_version: str = "202606"
    config_path: Path | None = None
    approval_required: bool = False
    approved_by_field: str = "approved_by"
    approved_at_field: str = "approved_at"
    approved_key_field: str = "approved_key"
    claim_unresolved_minutes: int = 15
    notify_command: tuple[str, ...] = ()


def find_config(explicit: str | None) -> Path | None:
    """--config wins, then $LINKEDIN_PUBLISHER_CONFIG, then ./publisher.toml."""
    if explicit:
        return Path(explicit).expanduser()
    from_env = os.environ.get(CONFIG_ENV)
    if from_env:
        return Path(from_env).expanduser()
    local = Path.cwd() / DEFAULT_CONFIG_NAME
    return local if local.exists() else None


def _check_time(value: str, where: str) -> str:
    if not TIME_RE.match(value):
        raise ConfigError(f"{where}: '{value}' is not a time in HH:MM format")
    return value


def _field_names(fm: dict, expires_key: str) -> tuple[str, str, str]:
    """The three frontmatter fields that record an approval."""
    names = []
    for option, default in (("approved_by_field", "approved_by"), ("approved_at_field", "approved_at"),
                            ("approved_key_field", "approved_key")):
        name = fm.get(option, default)
        if not isinstance(name, str) or not FIELD_RE.match(name):
            raise ConfigError(f"frontmatter.{option} must be a plain field name, not {name!r}")
        if name in RESERVED_FIELDS or name == expires_key:
            raise ConfigError(f"frontmatter.{option}: '{name}' is a field the tool uses itself")
        names.append(name)
    if len(set(names)) != 3:
        raise ConfigError("frontmatter: approved_by_field, approved_at_field and approved_key_field "
                          "must be three different names")
    return names[0], names[1], names[2]


def _notify_command(data: dict) -> tuple[str, ...]:
    if "command" not in data:
        return ()
    command = data["command"]
    if not isinstance(command, list) or not command or not all(isinstance(a, str) and a for a in command):
        raise ConfigError("notify.command must be a non-empty list of strings, "
                          'for example ["/path/to/script"]')
    return tuple(command)


def load_config(explicit: str | None = None) -> Config:
    path = find_config(explicit)
    data: dict = {}
    if path is not None:
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
        root = path.resolve().parent
    else:
        root = Path.cwd().resolve()

    paths = data.get("paths", {})
    fm = data.get("frontmatter", {})
    slots = data.get("slots", {})

    def resolve(key: str, default: str) -> Path:
        # An absolute path in the config wins over root, which is what pathlib does.
        return (root / Path(str(paths.get(key, default))).expanduser()).resolve()

    default_time = _check_time(str(slots.get("default", "10:00")), "slots.default")
    slot_times = {}
    for index, name in enumerate(WEEKDAYS):
        slot_times[index] = _check_time(str(slots.get(name, default_time)), f"slots.{name}")

    headings = fm.get("notes_headings", ["Notes"])
    if not isinstance(headings, list) or not all(isinstance(h, str) for h in headings):
        raise ConfigError("frontmatter.notes_headings must be a list of strings")

    approval = data.get("approval", {})
    required = approval.get("required", False)
    if not isinstance(required, bool):
        raise ConfigError("approval.required must be true or false")
    unresolved = data.get("claims", {}).get("unresolved_minutes", 15)
    if isinstance(unresolved, bool) or not isinstance(unresolved, int) or unresolved < 1:
        raise ConfigError("claims.unresolved_minutes must be a whole number of minutes, at least 1")
    expires_key = str(fm.get("expires_key", "expires"))
    by_field, at_field, key_field = _field_names(fm, expires_key)

    return Config(
        root=root,
        queue_dir=resolve("queue", "queue"),
        published_dir=resolve("published", "published"),
        env_file=resolve("env_file", ".env"),
        tokens_file=resolve("tokens_file", ".linkedin-tokens.json"),
        receipts_dir=resolve("receipts", ".receipts"),
        lock_file=resolve("lock_file", ".publisher.lock"),
        log_file=resolve("log_file", "publisher.log"),
        heartbeat_file=resolve("heartbeat_file", ".last-check"),
        ready_status=str(fm.get("ready_status", "queued")),
        published_status=str(fm.get("published_status", "published")),
        expires_key=expires_key,
        notes_headings=tuple(headings),
        slot_times=slot_times,
        grace_minutes=int(slots.get("grace_minutes", 90)),
        redirect_port=int(data.get("auth", {}).get("redirect_port", 8765)),
        api_version=str(data.get("api", {}).get("version", "202606")),
        config_path=path.resolve() if path is not None else None,
        approval_required=required,
        approved_by_field=by_field,
        approved_at_field=at_field,
        approved_key_field=key_field,
        claim_unresolved_minutes=unresolved,
        notify_command=_notify_command(data.get("notify", {})),
    )
