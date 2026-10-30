"""One route, run as its own process by test_claims.py.

Several of these start at the same moment and try to place the same post. LinkedIn
is replaced by a function that appends one line to sends.log, so the test can count
how often a post would have gone out.

usage: claim_worker.py <config> <index> <count> <workdir> take
       claim_worker.py <config> <index> <count> <workdir> post|file [--retry]

Each worker has its own lock file, the way the second route of a real setup has: the
lock is not what is under test. The credentials function waits until every worker that
can get there has arrived, so on code without a claim they all pass the receipt checks
before any of them has written a receipt.
"""
import datetime as dt
import sys
import time
from pathlib import Path

from linkedin_publisher import api, cli
from linkedin_publisher import postqueue as queue
from linkedin_publisher.config import load_config

CREDS = {"LINKEDIN_CLIENT_ID": "x", "LINKEDIN_CLIENT_SECRET": "y"}
TOKENS = {"access_token": "t", "author_urn": "urn:li:person:x"}
NOW = dt.datetime(2026, 9, 23, 10, 5)
TAKE_ATTEMPTS = 40
BARRIER_SECONDS = 0.5


def wait_for(condition, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.001)
    return False


def main() -> None:
    config, index, count, workdir, mode = sys.argv[1:6]
    retry = "--retry" in sys.argv[6:]
    index, count, work = int(index), int(count), Path(workdir)
    cfg = load_config(config)
    cfg.lock_file = work / f"lock-{index}"
    cli.notify = lambda title, message: None

    def arrive_and_wait():
        (work / f"arrived-{index}").touch()
        wait_for(lambda: len(list(work.glob("arrived-*"))) >= count, BARRIER_SECONDS)

    def load_credentials(_cfg):
        arrive_and_wait()
        return CREDS

    def publish(_cfg, _creds, _tokens, commentary, *_a, **_kw):
        with open(work / "sends.log", "a", encoding="utf-8") as fh:
            fh.write(f"{index}\n")
        time.sleep(0.2)
        return f"urn:li:share:{index}"

    api.load_credentials = load_credentials
    api.read_tokens = lambda _cfg: TOKENS
    api.ensure_access_token = lambda *_a: TOKENS
    api.publish = publish

    (work / f"ready-{index}").touch()
    if not wait_for((work / "go").exists, 30):
        sys.exit(99)

    if mode == "take":
        from linkedin_publisher import claims  # not imported above: the other modes also run on old code
        for number in range(TAKE_ATTEMPTS):
            if claims.take(cfg, f"key-{number:03d}", "a.md"):
                (work / f"won-{number:03d}-{index}").touch()
        code = 0
    else:
        only = queue.queue_files(cfg)[0] if mode == "file" else None
        try:
            code = cli.cmd_publish(cfg, NOW, "post", only, retry)
        except api.PublisherError:
            code = 1        # an older guard (a receipt another worker wrote) got there first
    (work / f"rc-{index}").write_text(str(code), encoding="utf-8")


if __name__ == "__main__":
    main()
