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
MEASURE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# The frontmatter fields whose names can be changed: option, default.
FIELD_OPTIONS = (("approved_by_field", "approved_by"), ("approved_at_field", "approved_at"),
                 ("approved_key_field", "approved_key"), ("measure_field", "measure"),
                 ("measure_line_field", "measure_line"), ("checks_field", "checks"))
# Names the tool itself reads or writes in a post file. A field of ours must not be one of them.
RESERVED_FIELDS = {"status", "slot", "slot_time", "format", "alt_text", "video_title",
                   "document_title", "posted_date", "linkedin_url"}


class ConfigError(Exception):
    """The configuration file is missing or contains a value that cannot be used."""


@dataclass(frozen=True)
class Measure:
    """A command that prints a number, and the pattern that finds that number in its output."""
    name: str
    command: tuple[str, ...]
    pattern: re.Pattern[str]
    timeout_seconds: float = 60


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
    measure_field: str = "measure"
    measure_line_field: str = "measure_line"
    checks_field: str = "checks"
    measures: dict[str, Measure] = field(default_factory=dict)
    check_timeout_seconds: float = 15
    external_route_field: str | None = None
    external_route_text: str | None = None


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


def _field_names(fm: dict, expires_key: str) -> dict[str, str]:
    """The frontmatter fields the tool reads or writes under a name of your choice, by option."""
    names: dict[str, str] = {}
    for option, default in FIELD_OPTIONS:
        name = fm.get(option, default)
        if not isinstance(name, str) or not FIELD_RE.match(name):
            raise ConfigError(f"frontmatter.{option} must be a plain field name, not {name!r}")
        if name in RESERVED_FIELDS or name == expires_key:
            raise ConfigError(f"frontmatter.{option}: '{name}' is a field the tool uses itself")
        for earlier, taken in names.items():
            if taken == name:
                raise ConfigError(f"frontmatter.{earlier} and frontmatter.{option} must be different "
                                  f"fields, both are '{name}'")
        names[option] = name
    return names


def _command(data: dict, where: str) -> tuple[str, ...]:
    command = data["command"]
    if not isinstance(command, list) or not command or not all(isinstance(a, str) and a for a in command):
        raise ConfigError(f"{where}.command must be a non-empty list of strings, "
                          'for example ["/path/to/script"]')
    return tuple(command)


def _notify_command(data: dict) -> tuple[str, ...]:
    return _command(data, "notify") if "command" in data else ()


def _seconds(value: object, where: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{where} must be a number of seconds above 0")
    return value


def _measures(data: object) -> dict[str, Measure]:
    """One [measures.NAME] table per measure: the command to run and the pattern for its output."""
    if not isinstance(data, dict):
        raise ConfigError("measures must hold one table per measure, for example [measures.open-items]")
    out: dict[str, Measure] = {}
    for name, table in data.items():
        where = f"measures.{name}"
        if not MEASURE_NAME_RE.match(name):
            raise ConfigError(f"{where}: '{name}' is not a plain name (letters, digits, - and _)")
        if not isinstance(table, dict):
            raise ConfigError(f"{where} must be a table with a command and a pattern")
        if "command" not in table:
            raise ConfigError(f"{where}.command is missing")
        pattern = table.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ConfigError(f"{where}.pattern is missing: a regular expression with one group, "
                              "for example '^OPEN: ([0-9]+)$'")
        try:
            compiled = re.compile(pattern, re.MULTILINE)
        except re.error as exc:
            raise ConfigError(f"{where}.pattern is not a valid regular expression: {exc}") from exc
        if compiled.groups != 1:
            raise ConfigError(f"{where}.pattern must contain exactly one group, the number to use")
        out[name] = Measure(name=name, command=_command(table, where), pattern=compiled,
                            timeout_seconds=_seconds(table.get("timeout_seconds"),
                                                     f"{where}.timeout_seconds", 60))
    return out


def _external_route(approval: dict) -> tuple[str | None, str | None]:
    """A field and a piece of text. A post whose field contains that text runs on another route."""
    route_field = approval.get("external_route_field")
    route_text = approval.get("external_route_text")
    if route_field is None and route_text is None:
        return None, None
    if route_field is None or route_text is None:
        raise ConfigError("approval.external_route_field and approval.external_route_text go together: "
                          "set both or neither")
    if not isinstance(route_field, str) or not FIELD_RE.match(route_field):
        raise ConfigError(f"approval.external_route_field must be a plain field name, not {route_field!r}")
    if not isinstance(route_text, str) or not route_text.strip():
        raise ConfigError("approval.external_route_text must be a piece of text, not empty")
    return route_field, route_text


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
    names = _field_names(fm, expires_key)
    route_field, route_text = _external_route(approval)

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
        approved_by_field=names["approved_by_field"],
        approved_at_field=names["approved_at_field"],
        approved_key_field=names["approved_key_field"],
        claim_unresolved_minutes=unresolved,
        notify_command=_notify_command(data.get("notify", {})),
        measure_field=names["measure_field"],
        measure_line_field=names["measure_line_field"],
        checks_field=names["checks_field"],
        measures=_measures(data.get("measures", {})),
        check_timeout_seconds=_seconds(data.get("checks", {}).get("timeout_seconds"),
                                       "checks.timeout_seconds", 15),
        external_route_field=route_field,
        external_route_text=route_text,
    )
