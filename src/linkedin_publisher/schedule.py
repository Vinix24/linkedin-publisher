"""Run linkedin-publisher every 15 minutes on this computer.

macOS uses launchd, Linux uses cron. Both only run while the computer is on.
A Mac that was asleep runs a missed job once when it wakes up; the grace period
then decides whether a post is still on time.
"""
from __future__ import annotations

import hashlib
import platform
import shlex
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

INTERVAL_SECONDS = 900
CRON_MARK = "# linkedin-publisher:"


class ScheduleError(Exception):
    """The reminder could not be installed or removed on this system."""


def job_id(config_path: Path) -> str:
    """Stable per config file, so two queues on one machine do not collide."""
    return hashlib.sha256(str(config_path.resolve()).encode()).hexdigest()[:8]


def command(config_path: Path, auto: bool) -> list[str]:
    return [sys.executable, "-m", "linkedin_publisher", "--config", str(config_path.resolve()),
            "publish", "--post" if auto else "--notify"]


def launchd_label(config_path: Path) -> str:
    return f"io.github.linkedin-publisher.{job_id(config_path)}"


def launchd_plist(label: str, argv: list[str], log_path: Path) -> str:
    args = "\n".join(f"    <string>{escape(a)}</string>" for a in argv)
    log = escape(str(log_path))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{escape(label)}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>StartInterval</key>
  <integer>{INTERVAL_SECONDS}</integer>
  <key>StandardOutPath</key>
  <string>{log}</string>
  <key>StandardErrorPath</key>
  <string>{log}</string>
</dict>
</plist>
"""


def cron_line(argv: list[str], log_path: Path, marker: str) -> str:
    return (f"*/15 * * * * {shlex.join(argv)} >> {shlex.quote(str(log_path))} 2>&1 "
            f"{CRON_MARK}{marker}")


def crontab_without(existing: str, marker: str) -> str:
    kept = [line for line in existing.splitlines() if f"{CRON_MARK}{marker}" not in line]
    return "\n".join(kept).strip("\n") + ("\n" if kept and any(k.strip() for k in kept) else "")


def crontab_with(existing: str, line: str, marker: str) -> str:
    return crontab_without(existing, marker) + line + "\n"


# ------------------------------------------------------------------ side effects

def _plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _run(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=stdin, capture_output=True, text=True, check=False)


def _read_crontab() -> str:
    result = _run(["crontab", "-l"])
    return result.stdout if result.returncode == 0 else ""


def install(config_path: Path, auto: bool, log_path: Path) -> str:
    argv = command(config_path, auto)
    system = platform.system()
    if system == "Darwin":
        import os
        label = launchd_label(config_path)
        plist = _plist_path(label)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(launchd_plist(label, argv, log_path), encoding="utf-8")
        domain = f"gui/{os.getuid()}"
        _run(["launchctl", "bootout", f"{domain}/{label}"])
        result = _run(["launchctl", "bootstrap", domain, str(plist)])
        if result.returncode != 0:
            raise ScheduleError(f"launchctl bootstrap failed: {result.stderr.strip()}")
        return f"launchd job {label} ({plist})"
    if system == "Linux":
        marker = job_id(config_path)
        result = _run(["crontab", "-"], stdin=crontab_with(_read_crontab(), cron_line(argv, log_path, marker),
                                                             marker))
        if result.returncode != 0:
            raise ScheduleError(f"crontab failed: {result.stderr.strip()}")
        return f"cron entry {CRON_MARK}{marker}"
    raise ScheduleError(f"{system} is not supported by 'schedule'. Use Task Scheduler to run "
                        f"'{shlex.join(argv)}' every 15 minutes, or use the GitHub Actions mode.")


def remove(config_path: Path) -> str:
    system = platform.system()
    if system == "Darwin":
        import os
        label = launchd_label(config_path)
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
        plist = _plist_path(label)
        existed = plist.exists()
        plist.unlink(missing_ok=True)
        return f"removed {label}" if existed else "nothing was installed"
    if system == "Linux":
        marker = job_id(config_path)
        existing = _read_crontab()
        if f"{CRON_MARK}{marker}" not in existing:
            return "nothing was installed"
        _run(["crontab", "-"], stdin=crontab_without(existing, marker))
        return f"removed cron entry {CRON_MARK}{marker}"
    raise ScheduleError(f"{system} is not supported by 'schedule'.")


def show(config_path: Path) -> str:
    system = platform.system()
    if system == "Darwin":
        plist = _plist_path(launchd_label(config_path))
        if not plist.exists():
            return "No reminder installed."
        mode = "posts automatically" if "--post" in plist.read_text() else "only notifies"
        return f"Installed: {plist} ({mode}, every 15 minutes)"
    if system == "Linux":
        line = next((l for l in _read_crontab().splitlines()
                     if f"{CRON_MARK}{job_id(config_path)}" in l), "")
        if not line:
            return "No reminder installed."
        return f"Installed ({'posts automatically' if '--post' in line else 'only notifies'}): {line}"
    return f"{system}: 'schedule' is not supported."
