# linkedin-publisher

Write your LinkedIn posts as Markdown files. Put them in a folder with a date and a time.
When a post is due, you get a signal. You run one command and it goes out through LinkedIn's
official API, and the file moves to a `published` folder with its URL.

It never posts on its own. A person presses the button.

```
queue/2026-10-01-my-post.md   ->   linkedin-publisher publish --post   ->   published/2026-10-01-my-post.md
                                                                             linkedin_url: https://www.linkedin.com/feed/update/...
```

## Why this exists

**LinkedIn silently cuts posts short.** The post text is not plain text. It is a format in
which `( ) [ ] { } < > @ | * _ ~` and `\` are reserved. One unescaped parenthesis and
everything after it can vanish. The API still answers `201 Created`. One of my own posts
lost 63% of its text to a single parenthesis, and nothing reported an error.

This tool turns your Markdown into plain text, escapes what needs escaping, and runs a
preflight check that refuses to send anything that is still unsafe. `linkedin-publisher check`
shows exactly what will go out, without touching the network.

**The API cannot schedule.** It can only post right now. So scheduling happens here: a queue
of files with a `slot`, a `due` command that tells you what is ready, and an optional
reminder every 15 minutes. The posting itself stays a deliberate step.

**Posting twice is worse than not posting.** Every attempt is written to a local receipt
file before anything else happens. If a post went live but its file could not be moved,
the receipt still blocks a second attempt, even if you rename the file.

## What it does and does not do

| Supported | Not supported |
|---|---|
| Text posts | Video |
| One image per post, with required alt text | Documents and PDF carousels |
| Posting as yourself (your personal profile) | Several images in one post |
| Markdown in your files (converted to plain text) | Polls |
| Hashtags | Posting as a company page |
| Private notes below the post that are never sent | Scheduling inside LinkedIn itself |

## Install

Python 3.11 or newer.

```bash
pipx install git+https://github.com/Vinix24/linkedin-publisher
# or: pip install git+https://github.com/Vinix24/linkedin-publisher

mkdir ~/linkedin && cd ~/linkedin
linkedin-publisher init
```

`init` creates `publisher.toml`, an empty `.env`, a `.gitignore`, a `queue/` folder with an
example post and an empty `published/` folder. It never overwrites a file that exists.

## Set up LinkedIn

You need your own LinkedIn app. That takes about fifteen minutes, once.

1. **A LinkedIn Page.** LinkedIn only lets you create an app that belongs to a Page, and you
   need to be an admin of it. A Page for your own business is fine. Posts still go out under
   your personal name.
2. **Create the app** at <https://www.linkedin.com/developers/apps/new>. Link it to your Page
   and upload a logo, which is required.
3. **Products tab.** Add *Share on LinkedIn* and *Sign In with LinkedIn using OpenID Connect*.
   Both are self-serve.
4. **Settings tab.** Verify the app. LinkedIn gives you a link that a Page admin has to
   approve. If you are the admin, open it and approve it yourself.
5. **Auth tab.** Under *Authorized redirect URLs* add exactly:
   `http://localhost:8765/callback`
6. **Copy the credentials** from the Auth tab into `.env`:
   ```
   LINKEDIN_CLIENT_ID=...
   LINKEDIN_CLIENT_SECRET=...
   ```
7. **Sign in:**
   ```bash
   linkedin-publisher auth
   linkedin-publisher status --online    # asks LinkedIn if the token works, posts nothing
   ```

### The token expires

Most self-serve apps do not get a refresh token. The access token lasts about 60 days. After
that, run `linkedin-publisher auth` again. `due` and `status` warn you from seven days before
it expires, so it does not surprise you on the morning you want to post.

## Write a post

```markdown
---
status: queued
slot: 2026-10-01
slot_time: "10:00"
expires: 2026-10-15
---

The text of your post.

Markdown is fine: **bold** and *italic* are turned into plain text, because LinkedIn does
not render Markdown.

---

## Notes

Anything below a line with only three dashes is private. It is never posted.
```

| Field | Required | Meaning |
|---|---|---|
| `status` | yes | Only `queued` goes out. Use `draft` for anything that is not finished. |
| `slot` | yes | The date, `YYYY-MM-DD`. |
| `slot_time` | no | `HH:MM`. Without it, the default time for that weekday from `publisher.toml` is used. |
| `expires` | no | After this date the post is skipped, because it is no longer true. |
| `format` | no | `text` (default) or `image`. |
| `alt_text` | for images | Describes the image for people who cannot see it. |

**Images:** put the image next to the post with the same name. `2026-10-01-my-post.md` goes
with `2026-10-01-my-post.png` (or `.jpg`, `.jpeg`, `.gif`).

Values follow YAML rules. A `# comment` after a value is ignored. Quote a value that has to
contain ` #`.

## Daily use

```bash
linkedin-publisher due                  # what is due, and why the rest is skipped
linkedin-publisher check                # dry run over the whole queue: characters, escaping, preflight
linkedin-publisher publish              # show exactly what would be sent
linkedin-publisher publish --post       # post the next due post
linkedin-publisher publish --post --file queue/2026-10-01-my-post.md   # post this one now
```

One post per run. If two are due, it takes the first and tells you about the other.

A post that missed its slot by more than 90 minutes is not posted late. You get a message
instead, and you decide. Change `grace_minutes` in `publisher.toml` if you want a
different window.

## Get a reminder when a post is due

`publish --notify` checks the queue and shows a desktop notification when something is due.
It never posts. Run it every 15 minutes.

**macOS:** edit and load `examples/launchd/com.example.linkedin-publisher.plist`.

**Linux** (uses `notify-send`), in `crontab -e`:

```
*/15 * * * * linkedin-publisher --config /home/you/linkedin/publisher.toml publish --notify
```

## Configuration

`publisher.toml`, created by `init`. Every path is relative to this file. The tool finds it
through `--config`, then `$LINKEDIN_PUBLISHER_CONFIG`, then `./publisher.toml`.

You can point it at folders and field names you already use. For example, a queue in Dutch:

```toml
[paths]
queue = "~/Content/linkedin/buffer"
published = "~/Content/linkedin/gepubliceerd"

[frontmatter]
ready_status = "buffer"
published_status = "gepubliceerd"
expires_key = "houdbaar_tot"
notes_headings = ["Image Brief", "Notes"]

[slots]
default = "10:00"
sat = "11:00"
sun = "11:00"
```

## When something goes wrong

| You see | What to do |
|---|---|
| `LINKEDIN_CLIENT_ID is missing` | Fill in `.env`, see *Set up LinkedIn*. |
| `401` or "rejects the token" | The token expired. Run `linkedin-publisher auth`. |
| `403` when posting | *Share on LinkedIn* is not added to your app, or the app is not verified. |
| `redirect_uri` error during `auth` | The redirect URL in the Auth tab must be exactly `http://localhost:8765/callback`. |
| `426` when posting | LinkedIn retired the API version. Set `LINKEDIN_API_VERSION=YYYYMM` in `.env` to a current month. |
| `preflight: REFUSED` | Something in the text is still unsafe. Run `check` to see what. |
| "a receipt shows this was already posted" | The post is live. The file stayed behind. Move it by hand. |

The files in `.receipts/` are a plain log of every attempt, one JSON line each.

## Development

```bash
git clone https://github.com/Vinix24/linkedin-publisher && cd linkedin-publisher
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The tests cover the parts that can go wrong without anyone noticing: frontmatter parsing,
escaping, slots, receipts and moving files. The calls to LinkedIn itself are not unit
tested. `status --online` is the safe way to check them.

## License

MIT
