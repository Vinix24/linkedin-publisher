"""Command line: linkedin-publisher <command>."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import subprocess
import sys
from pathlib import Path

import requests

from . import __version__, api, cloud, littletext, postqueue as queue, schedule, templates
from .config import DEFAULT_CONFIG_NAME, Config, ConfigError, load_config

TOKEN_WARN_DAYS = 7


def make_log(cfg: Config):
    def log(message: str) -> None:
        line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
        print(line)
        try:
            cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
            with cfg.log_file.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
    return log


def notify(title: str, message: str) -> None:
    """Desktop notification where one is available. The terminal line is always printed too."""
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["osascript", "-e",
                            f"display notification {json.dumps(message)} with title {json.dumps(title)}"],
                           check=False, capture_output=True, timeout=10)
        elif system == "Linux":
            subprocess.run(["notify-send", title, message], check=False, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def write_heartbeat(cfg: Config, now: dt.datetime) -> None:
    """Written at the start of every scheduled run, whether or not a post is due.
    It answers 'is the schedule running?', which an empty log cannot."""
    try:
        cfg.heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.heartbeat_file.write_text(now.isoformat(timespec="seconds"), encoding="utf-8")
    except OSError:
        pass


def last_check_line(cfg: Config, now: dt.datetime | None = None) -> str:
    if not cfg.heartbeat_file.exists():
        return "Last check: never. The schedule has not run yet."
    last = dt.datetime.fromisoformat(cfg.heartbeat_file.read_text(encoding="utf-8").strip())
    minutes = int(((now or dt.datetime.now()) - last).total_seconds() // 60)
    warning = "  <- more than 30 minutes ago, is the computer asleep or the job gone?" if minutes > 30 else ""
    return f"Last check: {last:%Y-%m-%d %H:%M} ({minutes} min ago){warning}"


def token_line(cfg: Config) -> str | None:
    days = api.token_days_left(cfg)
    if days is None:
        return None
    if days <= 0:
        return "Access token has EXPIRED. Run 'linkedin-publisher auth'."
    if days <= TOKEN_WARN_DAYS:
        return f"Access token expires in {days:.0f} days. Run 'linkedin-publisher auth' soon."
    return f"Access token valid for {days:.0f} more days."


# ----------------------------------------------------------------------- commands

def cmd_init(target: Path, github: bool = False, tz_name: str | None = None) -> None:
    target.mkdir(parents=True, exist_ok=True)
    created = []
    files = [(DEFAULT_CONFIG_NAME, templates.CONFIG), (".env", templates.ENV),
             (".gitignore", cloud.GITIGNORE if github else templates.GITIGNORE),
             ("queue/2026-01-05-example.md", templates.EXAMPLE_POST)]
    if github:
        tz_name = tz_name or cloud.local_timezone()
        files += list(cloud.workflows(tz_name, __version__).items())
    for name, content in files:
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            continue
        path.write_text(content, encoding="utf-8")
        if name == ".env":
            path.chmod(0o600)
        created.append(name)
    (target / "published").mkdir(exist_ok=True)
    print(f"Initialized {target}")
    for name in created:
        print(f"  created {name}")
    if not github:
        print("\nNext: fill in .env (see README, 'Set up LinkedIn'), then run: linkedin-publisher auth")
        return
    print(f"\nGitHub Actions mode, timezone {tz_name}. Next:")
    print("  1. Fill in .env, then run: linkedin-publisher auth")
    print("  2. git init && git add -A && git commit -m 'LinkedIn queue'")
    print("     gh repo create my-linkedin-queue --private --source . --push")
    print("  3. gh secret set LINKEDIN_CLIENT_ID")
    print("     gh secret set LINKEDIN_CLIENT_SECRET")
    print("     gh secret set LINKEDIN_TOKENS < .linkedin-tokens.json")
    print("  4. Queue a post (status: queued, a slot) and git push. Keep the repository private.")


def cmd_status(cfg: Config, online: bool = False, fail_days: int | None = None) -> int:
    files = queue.queue_files(cfg) if cfg.queue_dir.exists() else []
    print(f"linkedin-publisher {__version__}")
    print(f"root:      {cfg.root}")
    print(f"queue:     {cfg.queue_dir} ({len(files)} posts)")
    print(f"published: {cfg.published_dir}")
    print(f"receipts:  {cfg.receipts_dir} ({len(queue.load_placed_keys(cfg))} placed)")
    print(f"env file:  {cfg.env_file} ({'present' if cfg.env_file.exists() else 'MISSING'})")
    print(token_line(cfg) or "No tokens yet. Run 'linkedin-publisher auth'.")
    if online:
        print(f"LinkedIn accepts the token. Posting as: {api.verify_online(cfg)}")
    if fail_days is not None:
        days = api.token_days_left(cfg)
        if days is None or days <= fail_days:
            print(f"FAIL: the token expires within {fail_days} days or is missing.", file=sys.stderr)
            return 2
    return 0


def cmd_due(cfg: Config, now: dt.datetime) -> None:
    ready, skipped = queue.collect(cfg, now)
    print(f"Now: {now:%A %Y-%m-%d %H:%M}\n")
    if ready:
        print("Ready to post:")
        for post in ready:
            print(f"  {post.path.name}  (slot {post.due:%Y-%m-%d %H:%M}, {post.meta.get('format', 'text')})")
    else:
        print("Nothing is due.")
    if skipped:
        print("\nSkipped:")
        for post in skipped:
            print(f"  {post.path.name}: {post.reason}")
    line = token_line(cfg)
    if line:
        print(f"\n{line}")


LABELS = ("bold", "italic", "headers", "links", "bullets")


def cmd_check(cfg: Config) -> None:
    """Dry run over the whole queue. No network and no token needed."""
    files = queue.queue_files(cfg)
    if not files:
        print(f"No posts in {cfg.queue_dir}.")
        return
    for md_path in files:
        post = queue.read_post(md_path)
        raw = queue.post_text(post.body, cfg.notes_headings)
        normalized, counts = littletext.normalize_markdown(raw)
        commentary = littletext.escape(normalized)
        escaped = sorted({ch for ch in normalized if ch in littletext.RESERVED or ch == "\\"})
        leftover = littletext.find_unescaped(commentary)
        changed = ", ".join(f"{key} {counts[key]}x" for key in LABELS if counts[key])
        print(md_path.name)
        print(f"  characters sent: {len(commentary)} (source: {len(raw)})")
        print(f"  markdown normalized: {changed or 'none'}")
        print(f"  characters escaped: {', '.join(escaped) if escaped else 'none'}")
        problem = queue.content_problem(post, cfg)
        if leftover:
            print(f"  preflight: REFUSED, unescaped {', '.join(sorted(set(leftover)))}")
        elif problem:
            print(f"  preflight: REFUSED, {problem}")
        else:
            print("  preflight: ok")
        print()


def cmd_schedule(cfg: Config, action: str, auto: bool) -> None:
    if cfg.config_path is None:
        raise api.PublisherError("schedule needs a config file. Run it next to publisher.toml "
                                 "or pass --config.")
    if action == "install":
        where = schedule.install(cfg.config_path, auto, cfg.root / "schedule.log")
        mode = "posts automatically when a post is due" if auto else "notifies you when a post is due"
        print(f"Installed {where}. Every 15 minutes it {mode}.")
        print("It only runs while this computer is on. For a computer that may be off, "
              "use the GitHub Actions mode (see README).")
    elif action == "remove":
        print(schedule.remove(cfg.config_path))
    else:
        print(schedule.show(cfg.config_path))
        print(last_check_line(cfg))


def resolve_file(cfg: Config, raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        here = Path.cwd() / path
        path = here if here.exists() else cfg.root / path
    return path.resolve()


def cmd_publish(cfg: Config, now: dt.datetime, mode: str, only: Path | None) -> int:
    """mode: dry (show) | notify (signal, never post) | post (post for real)."""
    log = make_log(cfg)
    if mode == "notify" or (mode == "post" and only is None):
        write_heartbeat(cfg, now)

    if only is not None:
        if mode == "notify":
            raise api.PublisherError("--file and --notify cannot be combined.")
        if not only.exists():
            raise api.PublisherError(f"{only} does not exist.")
        post = queue.read_post(only)
        problem = queue.content_problem(post, cfg)
        if problem:
            raise api.PublisherError(f"{only.name}: {problem}")
        key = queue.content_key(queue.post_text(post.body, cfg.notes_headings))
        if key in queue.load_placed_keys(cfg):
            raise api.PublisherError(f"{only.name}: a receipt shows this was already posted.")
        post.due = now
    else:
        ready, _skipped = queue.collect(cfg, now)
        if not ready:
            if mode != "notify":
                print("Nothing is due.")
            return 0
        post = ready[0]  # one post per run
        if len(ready) > 1:
            log(f"More than one post is due. Taking {post.path.name}, leaving "
                f"{', '.join(p.path.name for p in ready[1:])}.")

    if mode == "notify":
        log(f"Due: {post.path.name} (slot {post.due:%Y-%m-%d %H:%M}). Waiting for a person to post it.")
        notify("LinkedIn post is due", f"{post.path.stem} is due. Run: linkedin-publisher publish --post")
        return 0

    text = queue.post_text(post.body, cfg.notes_headings)
    commentary = littletext.prepare(text)

    if mode == "dry":
        print(f"DRY RUN {post.path.name} (slot {post.due:%Y-%m-%d %H:%M})")
        print(f"characters: {len(commentary)} | format: {post.meta.get('format', 'text')} | "
              f"image: {post.image.name if post.image else 'none'}")
        print("-" * 60)
        print(commentary)
        print("-" * 60)
        print("Add --post to publish it for real.")
        return 0

    if cfg.lock_file.exists():
        age = dt.datetime.now() - dt.datetime.fromtimestamp(cfg.lock_file.stat().st_mtime)
        if age < dt.timedelta(minutes=30):
            log(f"Another run holds the lock ({int(age.total_seconds())}s old). Stopping.")
            return 1
        log("Found a stale lock, removing it.")
    cfg.lock_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.lock_file.touch()
    try:
        try:
            creds = api.load_credentials(cfg)
            tokens = api.ensure_access_token(cfg, creds, api.read_tokens(cfg), log)
            image = post.image if post.meta.get("format") == "image" else None
            urn = api.publish(cfg, creds, tokens, commentary, image, post.meta.get("alt_text"))
        except api.PreflightRejected as exc:
            queue.write_receipt(cfg, "preflight-rejected", post, error=str(exc))
            log(f"Skipped by preflight: {post.path.name}: {exc}")
            return 1
        except Exception as exc:
            queue.write_receipt(cfg, "failed", post, error=str(exc))
            raise

        url = api.post_url_for(urn)
        log(f"Sent {len(commentary)} characters ({urn}).")
        # The receipt is written before the file moves, so a failed move can
        # never lead to posting the same text twice.
        queue.write_receipt(cfg, "placed", post, post_urn=urn, post_url=url)
        try:
            target = queue.move_to_published(post, cfg, url, now.date())
        except OSError as exc:
            queue.write_receipt(cfg, "move-failed", post, post_urn=urn, post_url=url, error=str(exc))
            log(f"WARNING: {post.path.name} is live ({url}) but could not be moved: {exc}. "
                "It will not be offered again. Move it by hand.")
            notify("LinkedIn post is live, cleanup failed", f"{post.path.stem}: move it by hand.")
            return 1
        log(f"Posted: {post.path.name} -> {url}")
        log(f"Moved to {target}")
        notify("LinkedIn post is live", f"{post.path.stem}. The first hour counts, answer comments.")
        return 0
    finally:
        cfg.lock_file.unlink(missing_ok=True)


# --------------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linkedin-publisher",
        description="Queue LinkedIn posts as Markdown files and publish them. A person presses the button.")
    parser.add_argument("--config", help=f"path to {DEFAULT_CONFIG_NAME} "
                                          "(default: $LINKEDIN_PUBLISHER_CONFIG or ./publisher.toml)")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")
    init = sub.add_parser("init", help="create a config, .env and queue in a folder")
    init.add_argument("folder", nargs="?", default=".")
    init.add_argument("--github", action="store_true",
                      help="also add GitHub Actions workflows, so posting works with your computer off")
    init.add_argument("--timezone", help="IANA timezone for the workflows (default: this computer's)")
    sub.add_parser("auth", help="sign in with LinkedIn and store tokens")
    sub.add_parser("refresh", help="refresh the access token if a refresh token exists")
    status = sub.add_parser("status", help="show paths, queue size and token lifetime")
    status.add_argument("--online", action="store_true",
                        help="also ask LinkedIn whether the token works (read-only, posts nothing)")
    status.add_argument("--fail-days", type=int, metavar="N",
                        help="exit with code 2 when the token expires within N days (for CI)")
    sch = sub.add_parser("schedule", help="run every 15 minutes on this computer (launchd or cron)")
    sch.add_argument("action", choices=["install", "remove", "show"])
    sch.add_argument("--auto", action="store_true",
                     help="post automatically when due, instead of only notifying")
    sub.add_parser("due", help="show what is due and why other posts are skipped")
    sub.add_parser("check", help="dry run over the whole queue, no network")
    pub = sub.add_parser("publish", help="show, signal or post the next due post")
    mode = pub.add_mutually_exclusive_group()
    mode.add_argument("--post", action="store_true", help="post for real")
    mode.add_argument("--notify", action="store_true", help="only signal that a post is due (for cron/launchd)")
    pub.add_argument("--file", help="post this file now, outside its slot")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "due"
    try:
        if command == "init":
            cmd_init(Path(args.folder).expanduser().resolve(), args.github, args.timezone)
            return 0
        cfg = load_config(args.config)
        now = dt.datetime.now()
        if command == "auth":
            api.authorize(cfg, make_log(cfg))
        elif command == "refresh":
            api.ensure_access_token(cfg, api.load_credentials(cfg), api.read_tokens(cfg), make_log(cfg))
        elif command == "status":
            return cmd_status(cfg, args.online, args.fail_days)
        elif command == "schedule":
            cmd_schedule(cfg, args.action, args.auto)
        elif command == "due":
            cmd_due(cfg, now)
        elif command == "check":
            cmd_check(cfg)
        elif command == "publish":
            mode = "post" if args.post else "notify" if args.notify else "dry"
            only = resolve_file(cfg, args.file) if args.file else None
            return cmd_publish(cfg, now, mode, only)
        return 0
    except (ConfigError, api.PublisherError, schedule.ScheduleError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"ERROR: could not reach LinkedIn: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
