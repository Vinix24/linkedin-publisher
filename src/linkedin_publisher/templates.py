"""Files that `linkedin-publisher init` writes into a new folder."""

CONFIG = """\
# linkedin-publisher configuration. Paths are relative to this file.

[paths]
queue = "queue"            # posts waiting for their slot
published = "published"    # posts move here after posting, with their URL
env_file = ".env"          # LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET

[frontmatter]
ready_status = "queued"          # only posts with this status can go out
published_status = "published"   # set on the file after posting
expires_key = "expires"          # optional date after which a post is stale
notes_headings = ["Notes"]       # a heading with this name starts private notes

[slots]
# Default posting time when a post has a `slot` date but no `slot_time`.
default = "10:00"
# sat = "11:00"
# sun = "11:00"
grace_minutes = 90   # a post that missed its slot by more than this is not posted late

[api]
version = "202606"   # LinkedIn API version (YYYYMM). Override with LINKEDIN_API_VERSION.
"""

ENV = """\
# Credentials from your app on https://www.linkedin.com/developers/apps
# This file is in .gitignore. Never commit it.
LINKEDIN_CLIENT_ID=
LINKEDIN_CLIENT_SECRET=
# Only needed when posting fails with a 426: set the newest version (YYYYMM).
LINKEDIN_API_VERSION=
"""

EXAMPLE_POST = """\
---
status: draft
slot: 2026-01-05
format: text
---

This is an example post. Change `status` to `queued` and set a `slot` date to schedule it.

Everything above the line with three dashes is the post.

---

## Notes

Everything from the line with three dashes down is private and is never posted.
"""

GITIGNORE = """\
.env
.linkedin-tokens.json
.receipts/
.publisher.lock
publisher.log
schedule.log
.last-check
"""
