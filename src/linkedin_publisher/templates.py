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
# Where `approve` records who approved a post, when, and which version:
# approved_by_field = "approved_by"
# approved_at_field = "approved_at"
# approved_key_field = "approved_key"
# Where a post names the number it measures, the sentence it sits in, and its checks:
# measure_field = "measure"
# measure_line_field = "measure_line"
# checks_field = "checks"

[slots]
# Default posting time when a post has a `slot` date but no `slot_time`.
default = "10:00"
# sat = "11:00"
# sun = "11:00"
grace_minutes = 90   # a post that missed its slot by more than this is not posted late

[api]
version = "202606"   # LinkedIn API version (YYYYMM). Override with LINKEDIN_API_VERSION.

# [approval]
# required = true    # a scheduled `publish --post` only posts a post that `approve` signed off
# external_route_field = "note"            # posts whose field holds this text are shown by `precheck`,
# external_route_text = "runs via a cron"  # not checked: they run on another route

# [notify]
# command = ["/path/to/script"]   # for a job nobody watches; gets the title and message as last arguments

# [claims]
# unresolved_minutes = 15   # a claim with no outcome after this long is reported once

# [checks]
# timeout_seconds = 15      # how long an address named in a post's `checks` may take

# [measures.open-items]     # a number a post can name with `measure: open-items`
# command = ["/path/to/count-open-items"]
# pattern = '^OPEN: ([0-9]+)$'   # one group: the number in what the command prints
# timeout_seconds = 60
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
.token-warned
"""
