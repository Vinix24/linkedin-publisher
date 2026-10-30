"""Command line: linkedin-publisher <command>."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import platform
import shlex
import subprocess
import sys
import time
from pathlib import Path

import requests

from . import (__version__, api, approval, checks, claims, cloud, littletext, measures, precheck,
               postqueue as queue, schedule, templates)
from .config import DEFAULT_CONFIG_NAME, Config, ConfigError, load_config

TOKEN_WARN_DAYS = 7
NOTIFY_HOOK_TIMEOUT = 30


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


def alert(cfg: Config, title: str, message: str) -> None:
    """A notification for a person: the desktop one, and the [notify] command when there is one.

    The command is for a job that no desktop is watching. It gets title and message as its last
    two arguments and runs without a shell. Whatever goes wrong with it is logged and goes no
    further, because a run that found something to report must still do its work."""
    notify(title, message)
    if not cfg.notify_command:
        return
    try:
        result = subprocess.run([*cfg.notify_command, title, message], check=False, capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=NOTIFY_HOOK_TIMEOUT)
    except subprocess.TimeoutExpired:
        failure = f"timed out after {NOTIFY_HOOK_TIMEOUT} seconds"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        failure = str(exc)
    else:
        if result.returncode == 0:
            return
        said = result.stderr.decode("utf-8", "replace").strip()[:200]
        failure = f"exit code {result.returncode}" + (f": {said}" if said else "")
    make_log(cfg)(f"The notification hook failed ({failure}): {cfg.notify_command[0]}")


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
    try:
        queue_state = f"{len(queue.queue_files(cfg))} posts"
    except queue.QueueUnreadable as exc:
        queue_state = f"UNREADABLE: {exc}"
    print(f"linkedin-publisher {__version__}")
    print(f"root:      {cfg.root}")
    print(f"queue:     {cfg.queue_dir} ({queue_state})")
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
            print(f"  {post.path.name}  (slot {post.due:%Y-%m-%d %H:%M}, {post.meta.get('format', 'text')})"
                  f"{approval_note(cfg, post)}")
            for check in checks.parse(post.meta.get(cfg.checks_field)):
                print(f"      check, asked by a scheduled run: {check.line}")
    else:
        print("Nothing is due.")
    if skipped:
        print("\nSkipped:")
        for post in skipped:
            print(f"  {post.path.name}: {post.reason}")
    line = token_line(cfg)
    if line:
        print(f"\n{line}")


def approval_note(cfg: Config, post) -> str:
    """Only when approval is required: whether a scheduled run would post this one."""
    if not cfg.approval_required:
        return ""
    if approval.is_external(post, cfg):
        return "  [external route]"
    state, _current = approval.check(post, cfg)
    if state == approval.MISMATCH and approval.measure_line_problem(post, cfg):
        return "  [measure line does not match]"
    return {approval.APPROVED: "  [approved]", approval.MISSING: "  [not approved]",
            approval.MISMATCH: "  [approval belongs to a different version]"}[state]


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


def cmd_approve(cfg: Config, path: Path, by: str) -> None:
    if not path.exists():
        raise api.PublisherError(f"{path} does not exist.")
    key = approval.approve(cfg, path, by)
    print(f"Approved {path.name} as {by.strip()}.")
    print(f"  key {key[:12]}, over the text, format, alt text, title and media as they are now.")
    print("  Change any of those and the approval no longer counts. Changing the slot does not.")
    if measures.line_of(queue.read_post(path), cfg):
        print("  The measured number in the measure line is not part of the key. Nothing else in the post may change.")


def resolve_file(cfg: Config, raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        here = Path.cwd() / path
        path = here if here.exists() else cfg.root / path
    return path.resolve()


def warn_token_once_a_day(cfg: Config, now: dt.datetime, log) -> None:
    """During the last week before the token expires, a scheduled run warns once a day."""
    days = api.token_days_left(cfg, now)
    if days is None or days > TOKEN_WARN_DAYS:
        return
    marker = cfg.heartbeat_file.with_name(".token-warned")
    today = now.date().isoformat()
    try:
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == today:
            return
        marker.write_text(today, encoding="utf-8")
    except OSError:
        pass
    state = "has expired" if days <= 0 else f"expires in {days:.0f} days"
    log(f"The LinkedIn token {state}. Run 'linkedin-publisher auth'.")
    alert(cfg, f"LinkedIn token {state}", "Run: linkedin-publisher auth")


def report_new_stranded(cfg: Config, skipped: list, log) -> int:
    """A post that missed its slot is reported once, not on every run.
    Returns 3 when there was something new to report, so a CI run turns red once."""
    known = queue.load_stranded_keys(cfg)
    new = [p for p in skipped if p.stranded
           and queue.content_key(queue.post_text(p.body, cfg.notes_headings)) not in known]
    for post in new:
        queue.write_receipt(cfg, "stranded", post)
        log(f"Missed its slot and was not posted late: {post.path.name}. "
            "Give it a new slot or take it out of the queue.")
        alert(cfg, "LinkedIn post missed its slot", f"{post.path.stem} was not posted. You decide what happens.")
    return 3 if new else 0


def report_unresolved_claims(cfg: Config, now: dt.datetime, log) -> int:
    """A claim with no outcome after claim_unresolved_minutes means a run took the post and never
    said how it ended. Nobody posts it again. A person looks at LinkedIn, once.
    Only 'placed' finishes a claim. 'failed' and 'preflight-rejected' remove their claim right after
    the receipt, so a claim that outlives the limit next to one of them is a later attempt that
    died, or a run that stopped between the two steps. The old receipt says nothing about it.
    Returns 3 when there was something new to report."""
    states = queue.load_states(cfg)
    noted = queue.load_notes(cfg, "claim-unresolved")
    moment = now.astimezone(dt.UTC)
    limit = dt.timedelta(minutes=cfg.claim_unresolved_minutes)
    new = 0
    for key in claims.keys(cfg):
        if states.get(key) == "placed":
            continue
        claim = claims.read(cfg, key)
        if claim is None or moment - claim.claimed_at <= limit:
            continue
        ref = claim.claimed_at.isoformat()
        if (key, ref) in noted:
            continue
        name = claim.file or key[:12]
        minutes = int((moment - claim.claimed_at).total_seconds() // 60)
        queue.write_receipt(cfg, "claim-unresolved", key=key, file_name=claim.file, ref=ref)
        log(f"{name} was claimed {minutes} minutes ago and has no outcome. It will not be posted again. "
            "Check LinkedIn. If the post is not there, post it again with: "
            f"linkedin-publisher publish --post --file <the post> --retry")
        alert(cfg, "LinkedIn post: claimed but no outcome",
              f"{name} was claimed {minutes} minutes ago and has no outcome. Check LinkedIn.")
        new += 1
    return 3 if new else 0


APPROVAL_NOTICES = {
    approval.MISSING: ("approval-missing", "LinkedIn post needs approval",
                       "{stem} is due and nobody approved it. "
                       "Run: linkedin-publisher approve --file {path} --by NAME"),
    approval.MISMATCH: ("approval-mismatch", "LinkedIn post: approval belongs to a different version",
                        "{stem} changed after it was approved. Check it, then run: "
                        "linkedin-publisher approve --file {path} --by NAME"),
}


MEASURE_LINE_NOTICE = ("LinkedIn post: measure line does not match",
                       "{stem} cannot be approved: {problem}")


def apply_approval(cfg: Config, ready: list, log) -> tuple[list, int]:
    """Keep the due posts whose approval matches what would go out now. Every other one is
    reported once per version, with a note in the receipts, and left alone.
    A post on an external route (see ``approval.is_external``) is none of this tool's business: it
    is not sent, and it gets no note and no notification, only a line in the log.
    Returns (posts to send, 3 when there was something new to report)."""
    noted = {stage: queue.load_notes(cfg, stage) for stage, _title, _text in APPROVAL_NOTICES.values()}
    send, reported = [], 0
    for post in ready:
        if approval.is_external(post, cfg):
            log(f"{post.path.name} runs through another route. Leaving it to that route.")
            continue
        try:
            state, current = approval.check(post, cfg)
        except OSError as exc:
            log(f"Could not read the files of {post.path.name}: {exc}. Not posting it.")
            continue
        if state == approval.APPROVED:
            send.append(post)
            continue
        stage, title, template = APPROVAL_NOTICES[state]
        problem = approval.measure_line_problem(post, cfg) if state == approval.MISMATCH else None
        if problem:
            title, template = MEASURE_LINE_NOTICE
        key = queue.content_key(queue.post_text(post.body, cfg.notes_headings))
        if (key, current) in noted[stage]:
            continue
        noted[stage].add((key, current))
        queue.write_receipt(cfg, stage, post, ref=current)
        path = shlex.quote(str(post.path))
        message = template.format(stem=post.path.stem, path=path, problem=problem)
        log(f"Not posting {post.path.name}. {message}")
        alert(cfg, title, message)
        reported += 1
    return send, 3 if reported else 0


def apply_checks(cfg: Config, ready: list, log) -> tuple[list, int, int]:
    """Ask the checks of the due posts, in slot order, until one post passes all of its own.

    A post with a failing check is not sent and does not hold up the next one. Each failing check
    is reported once, with a note in the receipts; the next run asks again, so a site that was
    down for a moment does not cost the post. The posts after the first one that passes are
    returned unasked: only one post goes out per run.
    Returns (posts to send, posts held back, 3 when there was something new to report)."""
    noted = queue.load_notes(cfg, "check-failed")
    held, reported = 0, 0
    for index, post in enumerate(ready):
        failures = checks.run_all(checks.parse(post.meta.get(cfg.checks_field)), cfg.check_timeout_seconds)
        if not failures:
            return ready[index:], held, 3 if reported else 0
        held += 1
        key = queue.content_key(queue.post_text(post.body, cfg.notes_headings))
        until = post.due + dt.timedelta(minutes=cfg.grace_minutes)
        for failure in failures:
            message = (f"{post.path.stem} (slot {post.due:%Y-%m-%d %H:%M}) was not sent. Check failed, "
                       f"{failure.describe()}. The next run asks again, until {until:%H:%M}.")
            log(f"Not posting {post.path.name}. {message}")
            if (key, failure.ref) in noted:
                continue
            noted.add((key, failure.ref))
            queue.write_receipt(cfg, "check-failed", post, ref=failure.ref, error=failure.describe())
            alert(cfg, "LinkedIn post held back: a check failed", message)
            reported += 1
    return [], held, 3 if reported else 0


def media_line(post) -> str:
    fmt = post.meta.get("format", "text")
    if fmt in ("video", "document"):
        media = post.video if fmt == "video" else post.document
        if not media:
            return f"{fmt}: none"
        title = post.meta.get(f"{fmt}_title")
        size = media.stat().st_size / (1024 * 1024)
        return f"{fmt}: {media.name} ({size:.1f} MB{', title: ' + title if title else ''})"
    return f"image: {post.image.name if post.image else 'none'}"


def held_back_line(unapproved: int, failing_checks: int, external: int = 0) -> str:
    reasons = []
    if unapproved:
        reasons.append(f"{unapproved} without a valid approval")
    if failing_checks:
        reasons.append(f"{failing_checks} with a failing check")
    if not reasons:
        if external:
            return f"Nothing to post here: {external} due post(s) run through another route."
        return "Nothing is due."
    if external:
        reasons.append(f"{external} on another route")
    return f"Nothing can be posted: due post(s), {', '.join(reasons)}. See 'linkedin-publisher due' and the log."


def take_claim(cfg: Config, key: str, path: Path, retry: bool, log) -> bool:
    """The step that two runs cannot both pass. False means this run must not send, and has said why."""
    name = path.name
    if claims.take(cfg, key, name):
        return True
    if not retry:
        log(f"{name}: not posted. A claim for this post exists: another run is sending it, or an "
            "earlier run ended without an outcome. Check LinkedIn. If the post is not there: "
            f"linkedin-publisher publish --post --file {path} --retry")
        return False
    try:
        claims.supersede(cfg, key, lambda: key not in queue.load_placed_keys(cfg))
    except claims.ClaimHeld as exc:
        log(f"{name}: not posted. {exc}")
        return False
    if claims.take(cfg, key, name):
        log(f"{name}: --retry took over the claim of the earlier attempt.")
        return True
    log(f"{name}: not posted. Another run claimed it first.")
    return False


def cmd_publish(cfg: Config, now: dt.datetime, mode: str, only: Path | None, retry: bool = False) -> int:
    """mode: dry (show) | notify (signal, never post) | post (post for real).

    Returns 0, 1 for a run that could not do its work, or 3 when it found something new to
    report (a missed slot, a claim without an outcome, a post that needs approval)."""
    log = make_log(cfg)
    scheduled = mode == "notify" or (mode == "post" and only is None)
    report_code = 0
    if scheduled:
        write_heartbeat(cfg, now)
        warn_token_once_a_day(cfg, now, log)

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
        if key in queue.load_uncertain_keys(cfg) and not retry:
            raise api.PublisherError(f"{only.name}: an earlier attempt may have gone through. Check "
                                     "LinkedIn. If the post is not there, add --retry.")
        if not retry and queue.held_by_earlier_attempt(cfg, post):
            raise api.PublisherError(f"{only.name}: an earlier attempt of this post left a claim. It may have "
                                     "gone through. Check LinkedIn. If the post is not there, add --retry.")
        post.due = now
    else:
        try:
            ready, skipped = queue.collect(cfg, now)
        except queue.QueueUnreadable as exc:
            if scheduled:
                log(f"Could not read the queue: {exc}")
                alert(cfg, "LinkedIn queue unreadable", "A scheduled run could not see the queue. See the log.")
            raise
        if mode in ("notify", "post"):
            report_code = report_new_stranded(cfg, skipped, log)
            report_code = report_unresolved_claims(cfg, now, log) or report_code
        unapproved_count = check_held = external_count = 0
        if mode == "post" and cfg.approval_required:
            # Before anything is sent: a due post goes out only when a person approved this version.
            due_count = len(ready)
            external_count = sum(approval.is_external(post, cfg) for post in ready)
            ready, unapproved = apply_approval(cfg, ready, log)
            report_code = report_code or unapproved
            unapproved_count = due_count - len(ready) - external_count
        if mode == "post":
            # After the approval and before the claim: the addresses the post names must answer.
            ready, check_held, failed_checks = apply_checks(cfg, ready, log)
            report_code = report_code or failed_checks
        if not ready:
            if mode != "notify":
                print(held_back_line(unapproved_count, check_held, external_count))
            return report_code
        post = ready[0]  # one post per run
        if len(ready) > 1:
            log(f"More than one post is due. Taking {post.path.name}, leaving "
                f"{', '.join(p.path.name for p in ready[1:])}.")

    if mode == "notify":
        log(f"Due: {post.path.name} (slot {post.due:%Y-%m-%d %H:%M}). Waiting for a person to post it.")
        alert(cfg, "LinkedIn post is due", f"{post.path.stem} is due. Run: linkedin-publisher publish --post")
        return report_code

    text = queue.post_text(post.body, cfg.notes_headings)
    commentary = littletext.prepare(text)

    if mode == "dry":
        print(f"DRY RUN {post.path.name} (slot {post.due:%Y-%m-%d %H:%M}){approval_note(cfg, post)}")
        print(f"characters: {len(commentary)} | format: {post.meta.get('format', 'text')} | "
              f"{media_line(post)}")
        for check in checks.parse(post.meta.get(cfg.checks_field)):
            print(f"check, asked by a scheduled run: {check.line}")
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

    # The lock above and the receipts read earlier are checks, and two runs can pass them
    # together. The claim is the step only one of them gets. It comes before the first call
    # to LinkedIn, the token refresh included, and before this run puts its own lock down:
    # a run that lost the claim must not remove the lock of the run that won it.
    key = queue.content_key(text)
    if not take_claim(cfg, key, post.path, retry, log):
        return 1
    try:
        cfg.lock_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.lock_file.touch()
    except OSError:
        claims.release(cfg, key)     # nothing was sent
        raise
    try:
        try:
            creds = api.load_credentials(cfg)
            tokens = api.ensure_access_token(cfg, creds, api.read_tokens(cfg), log)
            out = queue.outgoing(post)
            # Written before anything is sent. If the run dies while sending, the
            # next run sees an attempt without an outcome and will not retry.
            queue.write_receipt(cfg, "attempt", post)
            urn = api.publish(cfg, creds, tokens, commentary, out.image, out.alt_text,
                              video=out.video, title=out.title, document=out.document)
        except api.PreflightRejected as exc:
            # Nothing was sent, so the claim goes, but only after the receipt says why.
            queue.write_receipt(cfg, "preflight-rejected", post, error=str(exc))
            claims.release(cfg, key)
            log(f"Skipped by preflight: {post.path.name}: {exc}")
            return 1
        except api.PublishUncertain as exc:
            queue.write_receipt(cfg, "uncertain", post, error=str(exc))
            log(f"UNCLEAR: {post.path.name}: {exc} It will not be tried again automatically. "
                "Check LinkedIn. If the post is not there, run: linkedin-publisher publish --post "
                f"--file {post.path} --retry")
            alert(cfg, "LinkedIn post: unclear if it went out", f"Check LinkedIn before retrying {post.path.stem}.")
            return 1
        except Exception as exc:
            queue.write_receipt(cfg, "failed", post, error=str(exc))
            claims.release(cfg, key)
            raise

        url = api.post_url_for(urn)
        log(f"Sent {len(commentary)} characters ({urn}).")
        # Written before the file moves, so a failed move can never lead to
        # posting the same text twice. The claim stays for good as well.
        queue.write_receipt(cfg, "placed", post, post_urn=urn, post_url=url)
        try:
            target = queue.move_to_published(post, cfg, url, now.date())
        except OSError as exc:
            queue.write_receipt(cfg, "move-failed", post, post_urn=urn, post_url=url, error=str(exc))
            log(f"WARNING: {post.path.name} is live ({url}) but could not be moved: {exc}. "
                "It will not be offered again. Move it by hand.")
            alert(cfg, "LinkedIn post is live, cleanup failed", f"{post.path.stem}: move it by hand.")
            return 1
        log(f"Posted: {post.path.name} -> {url}")
        log(f"Moved to {target}")
        alert(cfg, "LinkedIn post is live", f"{post.path.stem}. The first hour counts, answer comments.")
        return report_code
    finally:
        cfg.lock_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------- measure

def cmd_measure(cfg: Config, now: dt.datetime, days_ahead: int) -> int:
    """Update the measured number of every queued post whose slot is today or within ``days_ahead``
    days. It never posts. A post is left alone once its slot has been reached or anything has
    been tried with its text (see measures.refusal). A measure that fails leaves the number as
    it is: the post goes out with the last number it had, and a person is told once.
    Returns 3 when there was something new to report."""
    log = make_log(cfg)
    try:
        paths = queue.queue_files(cfg)
    except queue.QueueUnreadable as exc:
        log(f"Could not read the queue: {exc}")
        alert(cfg, "LinkedIn queue unreadable", "A scheduled run could not see the queue. See the log.")
        raise
    last = now.date() + dt.timedelta(days=days_ahead)
    noted = queue.load_notes(cfg, "measure-failed")
    reported = sum(measure_post(cfg, path, now, last, noted, log) for path in paths)
    return 3 if reported else 0


def measure_post(cfg: Config, path: Path, now: dt.datetime, last: dt.date, noted: set, log) -> int:
    """Returns 1 when this post produced a failure that was reported, otherwise 0."""
    try:
        original = path.read_bytes()
        raw = original.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log(f"Not measuring {path.name}: cannot read the file: {exc}")
        return 0
    post = queue.parse_post(path, raw)
    name = post.meta.get(cfg.measure_field)
    slot = queue.slot_datetime(post.meta, cfg)
    if post.meta.get("status") != cfg.ready_status or not name or slot is None \
            or not now.date() <= slot.date() <= last:
        return 0
    why = measures.refusal(cfg, post, slot, now)
    if why:
        log(f"Not measuring {path.name}: {why}. The post keeps its number.")
        return 0

    text = queue.post_text(post.body, cfg.notes_headings)
    key = queue.content_key(text)
    current = None
    started = time.monotonic()
    try:
        line = measures.line_of(post, cfg)
        if line is None:
            raise measures.MeasureFailed("no-measure-line",
                                         f"the post names a measure but has no {cfg.measure_line_field}")
        found = line.find(text)
        current = found.group(1)
        measure = cfg.measures.get(name)
        if measure is None:
            raise measures.MeasureFailed("unknown-measure", f"'{name}' is not defined in the configuration, "
                                                            f"as [measures.{name}]")
        digits = measures.read_number(measure, cfg.root)
    except measures.MeasureLineError as exc:
        return report_measure_failure(cfg, post, key, slot, current, exc.ref, str(exc), noted, log)
    except measures.MeasureFailed as exc:
        return report_measure_failure(cfg, post, key, slot, current, exc.ref, exc.detail, noted, log)

    # The command may have taken a while, and the world did not wait: ask again before writing.
    why = measures.refusal(cfg, post, slot, now + dt.timedelta(seconds=time.monotonic() - started))
    if why:
        log(f"Not writing the number of {path.name}: {why}. The post keeps its number.")
        return 0
    if digits == current:
        log(f"{path.name}: the number is still {current}.")
        return 0
    updated = measures.replace_number(raw, found, digits)
    try:
        approval.replace_file(path, original, updated.encode("utf-8"), what="measurement", command="measure")
    except api.PublisherError as exc:
        return report_measure_failure(cfg, post, key, slot, current, "write", str(exc), noted, log)
    new_key = queue.content_key(queue.post_text(queue.parse_post(path, updated).body, cfg.notes_headings))
    queue.write_receipt(cfg, "measured", key=new_key, file_name=path.name, ref=f"{current}->{digits}")
    log(f"Measured {path.name}: {current} -> {digits}.")
    return 0


def report_measure_failure(cfg: Config, post, key: str, slot: dt.datetime, current: str | None,
                           ref: str, detail: str, noted: set, log) -> int:
    """The number stays as it is. Once per reason and text, a person is told."""
    keeps = f"keeps its number ({current})" if current else "keeps the number it has"
    message = f"{post.path.stem} (slot {slot:%Y-%m-%d %H:%M}) {keeps} and still goes out. {detail}"
    log(f"Could not measure {post.path.name}: {detail}. The post keeps its number.")
    if (key, ref) in noted:
        return 0
    noted.add((key, ref))
    queue.write_receipt(cfg, "measure-failed", post, ref=ref, error=detail)
    alert(cfg, "LinkedIn post: number not updated", message)
    return 1


# --------------------------------------------------------------------- precheck

def cmd_precheck(cfg: Config, now: dt.datetime, days_ahead: int, as_json: bool = False) -> int:
    """Look at the posts of today and the next ``days_ahead`` days that have not reached their slot,
    and say what would stop them. It never posts and never changes a file. Every problem goes to
    ``alert`` once per post, problem and day. Returns 3 when anything is wrong, otherwise 0."""
    log = make_log(cfg)
    # In JSON mode stdout is the report and nothing else. What the run itself says goes to stderr.
    with contextlib.redirect_stdout(sys.stderr) if as_json else contextlib.nullcontext():
        try:
            reports = precheck.evaluate(cfg, now, days_ahead)
        except queue.QueueUnreadable as exc:
            log(f"Could not read the queue: {exc}")
            alert(cfg, "LinkedIn queue unreadable", "A scheduled run could not see the queue. See the log.")
            raise
        alert_precheck_problems(cfg, now, reports)
    if as_json:
        print(json.dumps(precheck_json(reports, days_ahead), indent=2))
    else:
        print_precheck(reports, now, days_ahead)
    return 3 if any(report.problems for report in reports) else 0


def alert_precheck_problems(cfg: Config, now: dt.datetime, reports: list) -> None:
    """A problem that is still there on a later day is reported again: the run of the morning
    must remind a person of what the run of the evening found, while there is time to act."""
    noted = queue.load_notes(cfg, "precheck")
    day = now.date().isoformat()
    for report in reports:
        key = queue.content_key(queue.post_text(report.post.body, cfg.notes_headings))
        for problem in report.problems:
            ref = f"{problem.ref}@{day}"
            if (key, ref) in noted:
                continue
            noted.add((key, ref))
            queue.write_receipt(cfg, "precheck", report.post, ref=ref, error=problem.message)
            alert(cfg, "LinkedIn precheck: a post will not go out as planned",
                  f"{report.post.path.stem} (slot {report.slot:%Y-%m-%d %H:%M}): {problem.message}")


def precheck_json(reports: list, days_ahead: int) -> dict:
    return {"schema": 1, "days_ahead": days_ahead, "posts": [
        {"post": report.post.path.name, "slot_date": f"{report.slot:%Y-%m-%d}", "slot_time": f"{report.slot:%H:%M}",
         "route": report.route, "checked": report.checked,
         "problems": [{"kind": problem.kind, "message": problem.message} for problem in report.problems]}
        for report in reports]}


def print_precheck(reports: list, now: dt.datetime, days_ahead: int) -> None:
    last = now.date() + dt.timedelta(days=days_ahead)
    print(f"Precheck {now:%Y-%m-%d %H:%M}: posts with a slot up to and including {last:%Y-%m-%d}\n")
    if not reports:
        print("No posts with a slot in this range.")
        return
    for report in reports:
        head = f"{report.slot:%Y-%m-%d %H:%M}  {report.post.path.name}"
        if not report.checked:
            print(f"{head}  route: external, not checked")
        elif not report.problems:
            print(f"{head}  ok")
        else:
            print(f"{head}  {len(report.problems)} problem(s)")
            for problem in report.problems:
                print(f"    {problem.kind}: {problem.message}")


# --------------------------------------------------------------------------- main

def days_ahead_arg(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not a whole number of days") from None
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or more (0 is today only)")
    return number


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
    appr = sub.add_parser("approve", help="record that a person approved this exact version of a post")
    appr.add_argument("--file", required=True, help="the post to approve")
    appr.add_argument("--by", required=True, help="who approves it; written into the file")
    sub.add_parser("check", help="dry run over the whole queue, no network")
    meas = sub.add_parser("measure", help="update the measured number in queued posts, never posts")
    meas.add_argument("--days-ahead", type=days_ahead_arg, default=1, metavar="N",
                      help="posts with a slot today up to N days from now (default 1)")
    pre = sub.add_parser("precheck", help="report what would stop the posts of the coming days, "
                                          "never posts and never edits")
    pre.add_argument("--days-ahead", type=days_ahead_arg, default=1, metavar="N",
                     help="posts with a slot today up to N days from now; 0 is today only (default 1)")
    pre.add_argument("--json", action="store_true", help="print the report as JSON")
    pub = sub.add_parser("publish", help="show, signal or post the next due post")
    mode = pub.add_mutually_exclusive_group()
    mode.add_argument("--post", action="store_true", help="post for real")
    mode.add_argument("--notify", action="store_true", help="only signal that a post is due (for cron/launchd)")
    pub.add_argument("--file", help="post this file now, outside its slot")
    pub.add_argument("--retry", action="store_true",
                     help="with --file: try again after an unclear outcome, once you checked LinkedIn")
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
        elif command == "approve":
            cmd_approve(cfg, resolve_file(cfg, args.file), args.by)
        elif command == "check":
            cmd_check(cfg)
        elif command == "measure":
            return cmd_measure(cfg, now, args.days_ahead)
        elif command == "precheck":
            return cmd_precheck(cfg, now, args.days_ahead, args.json)
        elif command == "publish":
            mode = "post" if args.post else "notify" if args.notify else "dry"
            only = resolve_file(cfg, args.file) if args.file else None
            if args.retry and only is None:
                raise api.PublisherError("--retry only works together with --file.")
            return cmd_publish(cfg, now, mode, only, args.retry)
        return 0
    except (ConfigError, api.PublisherError, schedule.ScheduleError, queue.QueueUnreadable) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"ERROR: could not reach LinkedIn: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
