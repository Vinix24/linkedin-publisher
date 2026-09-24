# linkedin-publisher

Write your LinkedIn posts as Markdown files. Put them in a folder with a date and a time.
When a post is due, you get a signal. You run one command and it goes out through LinkedIn's
official API, and the file moves to a `published` folder with its URL.

Nothing goes out unless a person decided it should. Either you press the button when a post
is due, or you decide in advance by marking a post `queued`.

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

**Posting twice is worse than not posting.** Every attempt leaves a local receipt: one just
before the post is sent, one with the outcome, both before the file is moved. A post that
went live is never offered again, not even after a failed move or a renamed file. And when
LinkedIn's answer is unclear (a timeout, a server error, or a success without a post id),
the tool does not try again by itself. It tells you to check LinkedIn first.

## What it does and does not do

| Supported | Not supported |
|---|---|
| Text posts | Video |
| One image per post, with required alt text | Documents and PDF carousels |
| Posting as yourself (your personal profile) | Several images in one post |
| Markdown in your files (converted to plain text) | Polls |
| Hashtags | Posting as a company page |
| Private notes below the post that are never sent | Scheduling inside LinkedIn itself |

## Read this before you start: scheduling needs something that runs

LinkedIn's API cannot schedule a post. It can only post right now. So at the moment a post is
due, something has to run. There are two ways, and both have limits you should know about.

| | On your computer | In GitHub Actions |
|---|---|---|
| Set up with | `linkedin-publisher schedule install` | `linkedin-publisher init --github` |
| Works when your computer is off | **No** | Yes |
| Who decides that a post goes out | You, when it is due (default), or in advance with `--auto` | You, in advance, by marking it `queued` and pushing |
| Cost | Nothing | Actions minutes, see below |
| Timing | Within 15 minutes of the slot | At the first run after the slot, usually within 30 minutes, sometimes later when GitHub is busy |

A server that is always on (Hetzner, Google Cloud, a Raspberry Pi) is a third option. See *Other places to run it*.

**On your computer** it only runs while the computer is on and awake. A Mac that was asleep
runs once when it wakes up. A post more than 90 minutes late is then not posted, on purpose:
a post about this morning's news should not appear in the evening. You get told once.

**In GitHub Actions** there is no approval button at posting time. On GitHub Free, Pro and
Team, required reviewers for workflow runs are only available in public repositories, and
your queue of unpublished posts belongs in a private one. So in this mode the decision is the
moment you set `status: queued` and push. That is how most scheduling tools work.

**The token expires after about 60 days** in both modes. You then run
`linkedin-publisher auth` again on your own computer. In GitHub Actions mode you also update
one secret, and a daily check turns red a week before, so posting does not stop by surprise.

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
linkedin-publisher publish --post --file queue/2026-10-01-my-post.md --retry
                                        # only after an unclear outcome, once you checked LinkedIn
```

One post per run. If two are due, it takes the first and tells you about the other.

A post that missed its slot by more than 90 minutes is not posted late. A scheduled run tells
you once (a notification on your computer, a red run in GitHub Actions), and you decide:
give it a new slot or take it out of the queue. `due` always shows it. Change
`grace_minutes` in `publisher.toml` if you want a different window.

## Run it on your computer

```bash
linkedin-publisher schedule install          # every 15 minutes: notify when a post is due
linkedin-publisher schedule install --auto   # every 15 minutes: post when a post is due
linkedin-publisher schedule show             # is it installed, and when did it last run
linkedin-publisher schedule remove
```

macOS uses launchd, Linux uses cron. On Windows, use Task Scheduler to run
`linkedin-publisher publish --notify` every 15 minutes, or use GitHub Actions.

Desktop notifications work on macOS and Linux. On Windows the message only goes to the log.
During the last week before the token expires, a scheduled run warns you once a day.

`schedule show` tells you when the last check ran. If that is more than 30 minutes ago, the
computer was asleep or the job is gone. An empty log alone cannot tell you that.

## Run it in GitHub Actions (computer may be off)

```bash
mkdir my-linkedin-queue && cd my-linkedin-queue
linkedin-publisher init --github            # uses your computer's timezone, or pass --timezone

# fill in .env, then:
linkedin-publisher auth

git init && git add -A && git commit -m "LinkedIn queue"
gh repo create my-linkedin-queue --private --source . --push

gh secret set LINKEDIN_CLIENT_ID
gh secret set LINKEDIN_CLIENT_SECRET
gh secret set LINKEDIN_TOKENS < .linkedin-tokens.json
```

**Keep this repository private.** It holds posts you have not published yet.

To schedule a post: write it in `queue/`, set `status: queued` and a `slot`, and push. The
workflow posts it, moves it to `published/` with its URL, and commits that back, together
with the receipt that stops it from going out twice.

What `init --github` adds:

- `.github/workflows/linkedin-publisher.yml` runs every 30 minutes between 06:00 and 22:59
  in your timezone. Posts are published in that timezone, not in UTC.
- `.github/workflows/linkedin-token-check.yml` runs once a day and fails during the last
  week before your token expires. Renew with:
  `linkedin-publisher auth && gh secret set LINKEDIN_TOKENS < .linkedin-tokens.json`
- a `.gitignore` that keeps secrets out but commits the queue, published posts and receipts.

**Actions minutes.** The publish workflow runs about 1,100 times a month. Even if every run
counted as a full minute, that stays well under the 2,000 free minutes a month for private
repositories on GitHub Free. Check your usage under *Settings, Billing* if you run other
workflows in private repositories too.

## Other places to run it

Any Linux machine that is always on works, with no extra code: a small server at Hetzner or
DigitalOcean, a virtual machine at Google Cloud, AWS or Azure, or a Raspberry Pi at home.
`schedule` uses cron there.

```bash
pipx install git+https://github.com/Vinix24/linkedin-publisher
mkdir ~/linkedin && cd ~/linkedin && linkedin-publisher init
# copy .env and .linkedin-tokens.json from the computer where you ran `auth`
linkedin-publisher status --online
linkedin-publisher schedule install --auto
```

Three things to know:

- **`auth` needs a browser**, which a server does not have. Run it on your own computer and
  copy `.linkedin-tokens.json` to the server. Do the same when the token expires.
- **Use `--auto` on a server.** A desktop notification reaches no one there. The approval
  moment is then the same as in GitHub Actions: marking a post `queued`.
- **Getting posts onto the server.** Write them there, or keep the queue in a private git
  repository and run `git pull` before and `git push` after, the way the GitHub Actions
  workflow does.

Serverless runtimes such as Cloud Run jobs or AWS Lambda do not work out of the box. The tool
keeps its queue, published posts and receipts on disk between runs, and those runtimes start
empty every time.

Scheduling on Linux is covered by the tests but has not yet been run on a real server.

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
| "an earlier attempt may have gone through" | LinkedIn's answer was unclear. Look at your profile. If the post is not there, run the `--retry` command the message shows. If it is there, move the file to `published/`. |
| GitHub Actions: "The job was not started because recent account payments have failed or your spending limit needs to be increased" | Actions in private repositories are blocked on your GitHub account. Nothing is posted until you fix it under *Settings, Billing and plans*. The run is red, so you will see it. |

The files in `.receipts/` are a plain log of every attempt, one JSON line each.

## Development

```bash
git clone https://github.com/Vinix24/linkedin-publisher && cd linkedin-publisher
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The tests cover the parts that can go wrong without anyone noticing: frontmatter parsing,
escaping, slots, receipts, moving files, the schedule files and the generated workflows. The calls to LinkedIn itself are not unit
tested. `status --online` is the safe way to check them.

## License

MIT
