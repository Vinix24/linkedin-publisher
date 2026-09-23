import datetime as dt
from pathlib import Path

import pytest

from linkedin_publisher.config import load_config

CONFIG = """\
[paths]
queue = "queue"
published = "published"

[frontmatter]
notes_headings = ["Notes", "Image Brief"]

[slots]
default = "10:00"
sun = "11:00"
grace_minutes = 90
"""


@pytest.fixture
def cfg(tmp_path: Path):
    (tmp_path / "queue").mkdir()
    (tmp_path / "publisher.toml").write_text(CONFIG, encoding="utf-8")
    return load_config(str(tmp_path / "publisher.toml"))


def write_post(cfg, name: str, meta: dict, body: str = "Hello world.") -> Path:
    lines = ["---", *[f"{k}: {v}" for k, v in meta.items()], "---", "", body]
    path = cfg.queue_dir / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# 2026-09-23 is a Wednesday
WEDNESDAY_10 = dt.datetime(2026, 9, 23, 10, 0)
