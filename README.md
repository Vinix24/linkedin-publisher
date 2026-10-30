# linkedin-publisher

Write your LinkedIn posts as Markdown files. Put them in a folder with a date and a time.
When a post is due, you get a signal. You run one command and it goes out through LinkedIn's
official API, and the file moves to a `published` folder with its URL.

Nothing goes out unless a person decided it should. Either you press the button when a post
is due, or you decide in advance by marking a post `queued`, or by approving one exact version
of it (see *Approval*).

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
the tool does not try again by itself. It tells you to check LinkedIn first. Receipts are
read first and written later, so two runs that start in the same second could both pass
them. Before anything is sent, a run therefore takes a *claim* on the post, a file that only
one run can create. See *One post, one claim*.

## What it does and does not do

| Supported | Not supported |
|---|---|
| Text posts | Several images in one post |
| One image per post, with required alt text | Captions and a custom thumbnail for a video |
| One MP4 video per post, up to 500 MB | Polls |
| One PDF per post (a document or carousel), up to 100 MB and 300 pages | Posting as a company page |
| Posting as yourself (your personal profile) | Scheduling inside LinkedIn itself |
| Markdown in your files (converted to plain text) | |
| Hashtags | |
| Private notes below the post that are never sent | |

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
| `format` | no | `text` (default), `image`, `video` or `document`. |
| `alt_text` | for images | Describes the image for people who cannot see it. |
| `video_title` | no | Title of the video. |
| `document_title` | for documents | Title LinkedIn shows above the document. |

**Images:** put the image next to the post with the same name. `2026-10-01-my-post.md` goes
with `2026-10-01-my-post.png` (or `.jpg`, `.jpeg`, `.gif`).

**Videos:** the same pairing with an `.mp4`: `2026-10-01-my-post.mp4`, and `format: video`.
LinkedIn accepts 75 KB to 500 MB and 3 seconds to 30 minutes. The size is checked before
anything is sent. The tool uploads the video, waits until LinkedIn has processed it (up to
15 minutes) and only then creates the post. If processing fails, nothing is posted and the
reason is in the log. In GitHub Actions, keep videos under 100 MB: GitHub refuses larger
files in a repository.

**Documents:** the same pairing with a `.pdf`: `2026-10-01-my-post.pdf`, `format: document`
and a `document_title`. PDF only: export a Word or PowerPoint file to PDF first. LinkedIn
accepts up to 100 MB and 300 pages. The size is checked before anything is sent. The page
count only LinkedIn can judge. The tool uploads the PDF, waits until LinkedIn has processed it
(up to 10 minutes) and only then creates the post. If processing fails, nothing is posted and
the reason is in the log.

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
linkedin-publisher approve --file queue/2026-10-01-my-post.md --by "Your Name"
                                        # sign off on this exact version, see Approval
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

On macOS, a scheduled job may not read `~/Desktop` or `~/Documents` unless the Python that
runs this tool has Full Disk Access (*System Settings, Privacy & Security*). Without it the
run stops with "no permission to read the queue folder". Simpler: keep the queue elsewhere.

Desktop notifications work on macOS and Linux. On Windows the message only goes to the log.
During the last week before the token expires, a scheduled run warns you once a day.

`schedule show` tells you when the last check ran. If that is more than 30 minutes ago, the
computer was asleep or the job is gone. An empty log alone cannot tell you that.

A job that wakes late catches up once. Within `grace_minutes` of the slot it posts, with the
same rules as on time (an approval is still required when you turned that on). After that it
only reports the post as missed, once, and what happens next is your decision. The tool never
wakes the computer and never changes its power settings.

To run it without a person at the keyboard, use `schedule install --auto` with approval
required, and a notification command for the messages you would otherwise miss:

```bash
linkedin-publisher schedule install --auto
```

## Approval: a person signs off on one version

By default `queued` is the approval. When a scheduled job posts for you, that can be too
little: a post is edited after you read it, or its image is replaced, and the job posts what
it finds. Turn on approval and a scheduled run only posts a post that a person signed off on,
in exactly the version that is about to go out.

```toml
[approval]
required = true
```

```bash
linkedin-publisher approve --file queue/2026-10-01-my-post.md --by "Your Name"
```

`approve` adds three fields to the frontmatter and changes nothing else in the file, not
even a line ending:

```yaml
approved_by: "Your Name"
approved_at: 2026-10-01T09:12:44+02:00
approved_key: "6f1c..."   # sha256 over what goes out
```

The key is a hash over everything that is sent: the text (without the private notes), the
format, the alt text, the title, and the bytes of the image, video or PDF. Change any of
those and the approval stops counting. Change the slot, the status or any other field and it
keeps counting. Writing the three fields does not change the key. Running `approve` again
overwrites all three. `approve` refuses a post that has a content problem, or that a receipt
shows as already posted, and `--by` has no default.

With `required = true`, a scheduled `publish --post` looks at every due post before it sends
anything:

| The post | What the run does |
|---|---|
| has no approval | does not post, notifies once that it is due and needs one |
| has an approval that belongs to another version | does not post, notifies once that the approval belongs to a different version |
| has an approval for exactly this version | posts it, one post per run as always |

A post that is waiting for approval does not hold up an approved post behind it. The notices
are written to the receipts (`approval-missing`, `approval-mismatch`), so each goes out once
per version of the post and not on every run. `due` shows `[approved]`, `[not approved]` or
`[approval belongs to a different version]` next to each due post.

`publish --post --file` is a person pressing the button and needs no approval. `--notify`
only signals that a post is due and is not affected.

The check happens when the run has decided to send. A file that someone replaces in the
seconds between that check and the upload is not caught.

The field names are configurable, like `ready_status`:

```toml
[frontmatter]
approved_by_field = "signed_off_by"
approved_at_field = "signed_off_at"
approved_key_field = "signed_off_key"
```

## One post, one claim

Two runs can start in the same second: a scheduled job and a command you typed, or two
tools that share one queue. Both read the receipts, both see nothing, both send. To make that
impossible, a run creates a file before it sends, and only one run can create it:

```
.receipts/claims/<content key>.claim     {"pid", "file", "route", "claimed_at"}
```

It is created with `O_CREAT | O_EXCL`, so the filesystem decides who gets it. The run that
does not get it stops, writes a line in the log and returns a non-zero exit code. It calls
LinkedIn zero times and writes no `failed` receipt. This applies to every way of sending,
`publish --post --file` included. The claim is taken after the receipt checks and before the
`attempt` receipt and the first call to LinkedIn.

| How the run ends | The claim |
|---|---|
| `placed` | stays for good |
| `failed` or `preflight-rejected` (nothing was sent) | the receipt is written first, then the claim is removed |
| `uncertain`, or the run dies | stays |

A claim that stays without an outcome is unresolved. A scheduled run reports a claim that is
older than 15 minutes and has no `placed` or `failed` receipt, once, and tells you to look at
LinkedIn. Nobody posts that post again by themselves. A video can take up to 15 minutes to
process, so if you post video, raise the limit:

```toml
[claims]
unresolved_minutes = 30
```

If the post is not on LinkedIn, `publish --post --file <post> --retry` takes the claim over.
It refuses while the process that made the claim is still running, it refuses a claim that
names no process until that claim is a minute old (its owner may be about to write it), it
refuses a post that has a `placed` receipt, and when two retries start together only one gets
the claim. The old
claim is kept next to the new one as `<key>.claim.retired-<id>`. Where the tool cannot check
whether the process still runs (Windows), `--retry` leaves the claim alone and tells you to
remove the file yourself after you checked LinkedIn.

Another program that places posts in the same folder takes part by following the same file
contract: same path, same `O_EXCL` creation, same JSON, and the same rule that a `placed`
claim stays.

## Notifications for a job nobody watches

A desktop notification reaches nobody when a launchd or cron job runs on a machine without
a screen. Point the tool at a command:

```toml
[notify]
command = ["/path/to/script"]
```

The command is called with the title and the message added as the last two arguments, for
example `/path/to/script "LinkedIn post needs approval" "2026-10-01-my-post is due ..."`. It
runs without a shell, so nothing in a title can be interpreted as a command, and it is
stopped after 30 seconds. The desktop notification still goes out as well. If the command
fails, is missing or hangs, that is written to the log and the run goes on: a post is never
held up by a broken notification channel. Without the option nothing changes.

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

A complete `publisher.toml` for a job that posts without a person at the keyboard:

```toml
[paths]
queue = "queue"
published = "published"

[frontmatter]
approved_by_field = "approved_by"      # these three are the defaults
approved_at_field = "approved_at"
approved_key_field = "approved_key"

[slots]
default = "10:00"
grace_minutes = 90

[approval]
required = true                        # default false: nothing changes for existing setups

[claims]
unresolved_minutes = 15                # default 15

[notify]
command = ["/Users/YOU/bin/tell-me"]   # default: none, the desktop notification only
```

## When something goes wrong

| You see | What to do |
|---|---|
| `LINKEDIN_CLIENT_ID is missing` | Fill in `.env`, see *Set up LinkedIn*. |
| `401` or "rejects the token" | The token expired. Run `linkedin-publisher auth`. |
| `403` when posting | *Share on LinkedIn* is not added to your app, or the app is not verified. |
| `redirect_uri` error during `auth` | The redirect URL in the Auth tab must be exactly `http://localhost:8765/callback`. |
| `426` when posting | LinkedIn retired the API version. Set `LINKEDIN_API_VERSION=YYYYMM` in `.env` to a current month. |
| "no permission to read the queue folder" | A scheduled job on macOS may not look in `~/Desktop` or `~/Documents`. Give the Python behind `linkedin-publisher` Full Disk Access, or move the queue. |
| `preflight: REFUSED` | Something in the text is still unsafe. Run `check` to see what. |
| "a receipt shows this was already posted" | The post is live. The file stayed behind. Move it by hand. |
| "an earlier attempt may have gone through" | LinkedIn's answer was unclear. Look at your profile. If the post is not there, run the `--retry` command the message shows. If it is there, move the file to `published/`. |
| "a claim for this post exists" | Another run is sending it, or an earlier run ended without an outcome. Look at your profile. If the post is not there, run the `--retry` command the message shows. |
| "claimed but no outcome" | A run took the post and never said how it ended. Same as above: look at LinkedIn first. |
| "needs approval" | Read the post, then run `linkedin-publisher approve --file <post> --by "Your Name"`. |
| "approval belongs to a different version" | The text, alt text, title or media changed after the approval. Read it again and approve again. |
| "The notification hook failed" | Your `[notify] command` failed or hung. The log has the exit code or the error. The run itself went on. |
| GitHub Actions: "The job was not started because recent account payments have failed or your spending limit needs to be increased" | Actions in private repositories are blocked on your GitHub account. Nothing is posted until you fix it under *Settings, Billing and plans*. The run is red, so you will see it. |

The files in `.receipts/` are a plain log of every attempt, one JSON line each. Besides the outcomes
(`attempt`, `placed`, `uncertain`, `failed`, `preflight-rejected`, `move-failed`) they hold notes that
tell a person something once: `stranded`, `approval-missing`, `approval-mismatch` and
`claim-unresolved`. A note never counts as an outcome. `.receipts/claims/` holds the claims.

## Development

```bash
git clone https://github.com/Vinix24/linkedin-publisher && cd linkedin-publisher
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The tests cover the parts that can go wrong without anyone noticing: frontmatter parsing,
escaping, slots, receipts, claims (including separate processes that start at the same moment),
approvals, moving files, the schedule files and the generated workflows. The calls to LinkedIn
itself are not unit tested. `status --online` is the safe way to check them.

## License

MIT
