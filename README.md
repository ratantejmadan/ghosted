# ghosted

[![PyPI](https://img.shields.io/pypi/v/getghosted)](https://pypi.org/project/getghosted/)
[![Python](https://img.shields.io/pypi/pyversions/getghosted)](https://pypi.org/project/getghosted/)
[![License](https://img.shields.io/pypi/l/getghosted)](LICENSE)
[![Downloads](https://img.shields.io/pypi/dm/getghosted)](https://pypi.org/project/getghosted/)
[![GitHub stars](https://img.shields.io/github/stars/ratantejmadan/ghosted)](https://github.com/ratantejmadan/ghosted)

**Bulk-unsend your Instagram DMs. Free and open source.**

A free alternative to paid "social media cleanup" tools for one specific job:
permanently removing your own Instagram direct messages — every message you've
ever sent, across every thread or just one — with no time limit and no
subscription.

It logs in through Instagram's own login page (your password never touches
this tool), backs up your conversation history to a local file, and then
unsends your messages with human-paced timing.

---

## Table of contents

- [What it does](#what-it-does)
- [How it works](#how-it-works)
- [The files](#the-files)
- [Setup](#setup)
- [Step-by-step usage](#step-by-step-usage)
- [All options](#all-options)
- [Avoiding rate limits / action blocks](#avoiding-rate-limits--action-blocks)
- [Troubleshooting](#troubleshooting)
- [Important caveats](#important-caveats)
- [Roadmap / contributing](#roadmap--contributing)
- [License](#license)

---

## What it does

- **Exports** every DM thread and message to a local JSON file _before_ it
  deletes anything — so you always have a record of what existed.
- **Unsends** the messages _you_ sent (removes them for everyone in the
  conversation), with randomized delays and optional batching to behave like
  a normal user session.
- **Targets precisely**: run it across your whole inbox, a single person's
  DM, or one exact thread (e.g. a specific group chat).
- **Handles every message type** — text, shared posts and reels, profile
  shares, links, voice notes — by reading the raw conversation data instead
  of relying on fragile parsing.

Unsend on Instagram has no age limit, so this works on messages from years
ago, not just recent ones.

---

## How it works

The tool is split into two stages so that login is isolated from the
deletion work.

**1. Login (`login_browser.py`)**
Opens Instagram's real login page in a Playwright-controlled browser. You log
in there yourself — including 2FA, "save login info", and any security
checkpoints — exactly as you normally would. Once you're at your home feed,
the script captures your `sessionid` cookie and saves it to `session.json`.

Your credentials only ever go to Instagram. This tool never sees your
password. (Note: Instagram's _official_ API does not allow reading or
deleting personal DMs, so a captured browser session is the only realistic
way to do this — there is no OAuth "authorize app" flow for it.)

**2. Purge (`purge_dms.py`)**
Loads that session, then talks to Instagram's private API to:

- List your threads and pull full message history, reading the **raw JSON**
  directly. This deliberately avoids the higher-level parsing in the
  underlying library, which crashes on shared posts/reels whose internal
  media URLs don't validate. We only keep the five fields we actually need
  per message: id, sender, timestamp, type, and text.
- Unsend each of your messages one at a time, with random pauses, skipping
  system items that can't be deleted.

---

## The files

| File                  | Purpose                                                                                      |
| --------------------- | -------------------------------------------------------------------------------------------- |
| `login_browser.py`    | One-time browser login; captures and saves your session.                                     |
| `purge_dms.py`        | The main tool: list, export, dry-run, and unsend.                                            |
| `unsend_one.py`       | Test harness — unsends a single message by ID, to confirm everything works before a big run. |
| `requirements.txt`    | Python dependencies.                                                                         |
| `session.json`        | Your saved session (created by login; git-ignored, sensitive).                               |
| `purged_threads.json` | Ledger of threads already cleared (created on first purge; git-ignored).                     |
| `PROGRESS.txt`        | Human-readable progress tracker (e.g. `10/50 threads purged`).                               |
| `progress.json`       | Backing state for the tracker (the total count). Git-ignored.                                |
| `export/`             | Timestamped JSON backups written before each run (git-ignored).                              |

---

## Setup

Requires Python 3.8+.

```bash
git clone <your-repo-url>
cd ghosted

python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium     # one-time browser download (a few hundred MB)
```

> On macOS, use `python3` and `pip` (or just `pip` once the venv is active).

---

## Step-by-step usage

The recommended order — test small, then go big.

### 1. Log in

```bash
python3 login_browser.py
```

A browser opens to Instagram. Log in, wait for your home feed, then press
ENTER in the terminal. You should see `Session captured and saved`.

### 2. Confirm a single delete works (optional but recommended)

Find a thread, do an export to get a message ID, then unsend exactly one:

```bash
python3 unsend_one.py --thread-id <THREAD_ID> --message-id <MESSAGE_ID>
```

It shows you what it's about to remove and waits for you to type `yes`.

### 3. Test on one conversation

```bash
# preview only — deletes nothing
python3 purge_dms.py --only-user someusername --dry-run

# do it for real
python3 purge_dms.py --only-user someusername --unsend
```

### 4. Target a specific group chat

```bash
# find the group's exact ID (most recent 10 threads)
python3 purge_dms.py --list-threads --limit 10

# or list your oldest threads first
python3 purge_dms.py --list-threads --limit 10 --order oldest

# preview, then unsend, that one thread
python3 purge_dms.py --thread-id <GROUP_ID> --dry-run
python3 purge_dms.py --thread-id <GROUP_ID> --unsend
```

### 5. Purge everything

```bash
# see the full scope first
python3 purge_dms.py --dry-run

# run it, batched and slow (recommended for large histories)
python3 purge_dms.py --unsend \
  --min-delay 5 --max-delay 12 \
  --batch-size 100 --pause-between-batches 1800
```

### Or purge just your most recent threads

Useful for clearing things incrementally instead of the whole inbox at once:

```bash
# preview your 5 most recently active threads
python3 purge_dms.py --limit 5 --dry-run

# unsend across just those 5 threads
python3 purge_dms.py --limit 5 --unsend

# or start from your 5 OLDEST threads instead
python3 purge_dms.py --limit 5 --order oldest --unsend
```

On a Mac, prefix with `caffeinate -i` to stop the machine sleeping during a
long run:

```bash
caffeinate -i python3 purge_dms.py --unsend --batch-size 100 --pause-between-batches 1800
```

---

## All options

| Flag                              | What it does                                                                                                                                                 |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--list-threads`                  | Print all threads (IDs, group titles, participants) and exit. Deletes nothing.                                                                               |
| `--limit N`                       | Only act on the N most recent threads. Works with `--list-threads`, `--dry-run`, and `--unsend`. Ignored when `--only-user`/`--thread-id` is set. `0` = all. |
| `--order recent\|oldest`          | Thread order for `--list-threads` and `--limit`. `recent` (default) = newest activity first; `oldest` = oldest first.                                        |
| `--include-purged`                | Don't skip threads already in the purged ledger. Use to re-clear threads that got new messages since you last purged them.                                   |
| `--show-purged`                   | Print the purged-threads ledger and exit.                                                                                                                    |
| `--only-user USERNAME`            | Limit to the thread with this user (matches groups they're in).                                                                                              |
| `--thread-id ID`                  | Limit to one exact thread — the precise way to target a group.                                                                                               |
| `--export-only`                   | Back up history only; delete nothing.                                                                                                                        |
| `--dry-run`                       | Show what _would_ be deleted.                                                                                                                                |
| `--unsend`                        | Actually unsend messages.                                                                                                                                    |
| `--include-others-messages`       | Also remove others' messages from _your_ view (does not delete their copy).                                                                                  |
| `--min-delay` / `--max-delay`     | Random pause range (seconds) between deletes. Default 2–6.                                                                                                   |
| `--batch-size N`                  | Delete in chunks of N, with a long pause between chunks. `0` = continuous.                                                                                   |
| `--pause-between-batches SECONDS` | Length of that pause (default 900 = 15 min), with jitter.                                                                                                    |

Only your own messages are ever unsent unless you pass
`--include-others-messages`.

---

## Avoiding rate limits / action blocks

There is **no setting that makes mass-deletion invisible** — Instagram can
flag unusual volume regardless of timing. But pacing meaningfully lowers the
risk of a temporary action block. The levers, in order of impact:

1. **Batching** is the big one. A long, uninterrupted stream of deletes is
   the clearest bot signature. `--batch-size` + `--pause-between-batches`
   breaks the work into human-sized sessions. For large histories, prefer a
   smaller batch with a longer pause, spread across a day.
2. **Wider, randomized per-message delays** (`--min-delay 5 --max-delay 12`).
   The randomness matters as much as the length — a fixed interval is itself
   a tell.
3. **Run from your home IP**, not a datacenter/VPS IP.
4. **Keep one stable session** rather than re-capturing fresh ones repeatedly.

The script tells genuine rate limiting apart from harmless per-item errors.
If the end-of-run summary shows **rate-limit failures climbing**, stop, wait
a few hours, and widen your delays before continuing.

---

## Resuming and the purged-threads ledger

Because unsending leaves the _conversation_ in place (this tool never leaves
chats — that would notify people), an already-cleared thread still shows up
in your inbox. To stop `--limit` from re-selecting threads you've already
done, the tool keeps a ledger.

- After every deletable message in a thread is handled, that thread ID is
  recorded in `purged_threads.json` (with a timestamp).
- In whole-inbox mode, `--limit` automatically **skips** threads in the
  ledger, so `--limit 10` always means "10 threads you haven't cleared yet."
- This survives interruptions: completed threads are saved as you go, so if a
  session expires mid-run, the next run picks up where you left off.

So you can purge in arbitrary chunks without overlap:

```bash
python3 purge_dms.py --limit 10 --order oldest --unsend   # oldest 10
python3 purge_dms.py --limit 10 --unsend                  # next 10 recent
python3 purge_dms.py --limit 20 --unsend                  # next 20
# ...each run automatically skips everything already cleared
```

Inspect or override the ledger:

```bash
python3 purge_dms.py --show-purged          # see what's been cleared

# re-clear threads that got NEW messages since you purged them:
python3 purge_dms.py --limit 10 --include-purged --unsend
```

`--list-threads` marks already-purged threads with `(purged)` so you can see
their status at a glance.

### Progress tracker

A plain-text **`PROGRESS.txt`** gives you an at-a-glance overall view:

```
ghosted — purge progress
============================

Threads purged:  10/50  (20%)
[####----------------]
Remaining:       40
Total counted:   2026-06-16T05:00:43

Last updated:    2026-06-16T05:12:09
```

- The **total** (denominator) is recorded whenever you run `--list-threads`
  without `--limit` — that full listing counts your whole inbox.
- The **purged** count (numerator) comes straight from the ledger and updates
  live: every time a thread is fully cleared during an unsend run,
  `PROGRESS.txt` is rewritten.
- A thread only counts toward progress once **every** message in it has been
  handled — partial threads don't count.

If your inbox grows later, just re-run `--list-threads` to recount the total.

**`login_required` even right after capturing a session.**
Instagram stores the `sessionid` cookie URL-encoded (colons as `%3A`); the
private API needs it decoded. The tool decodes it automatically now. If you
still hit this, re-run `login_browser.py` to capture a fresh session.

**A traceback about `MediaXma` / `video_url` / `url_scheme`.**
This came from the underlying library choking on a shared post/reel
attachment. The tool no longer uses that parsing path — it reads raw thread
data instead — so an up-to-date copy won't hit this.

**`1545003` "something went wrong" on a few messages.**
These are **not** bot detection. They're system items (chat events, call
logs) or already-gone placeholders that can't be unsent. The tool skips
known non-deletable types and reports the rest as "un-deletable" skips. A
handful of these is normal and harmless.

**How do I know if I'm _actually_ rate limited?**
Real rate limiting hits consistently across messages with "please wait" /
"feedback_required" style errors, or forces a checkpoint in the app. Scattered
failures on specific messages while others succeed is not that.

---

## Important caveats

- This uses an **unofficial** client that talks to Instagram's private API.
  Doing so is against Instagram's Terms of Service. Possible consequences
  include temporary action blocks, security checkpoints, or account
  restrictions. Use at your own risk.
- **Unsend is permanent.** Once a message is removed, Instagram does not let
  you recover it. The export JSON written before each run is your only record
  — keep those files safe.
- You can only **unsend messages you sent** (removed for everyone). For
  others' messages, `--include-others-messages` does a "remove for me" that
  only affects your view, not theirs.
- Large histories can take hours because of the deliberate pacing. That's by
  design.

---

## Roadmap / contributing

This started as a personal tool and is shared so others can use it or build
something better. High-value next steps:

- **Filtering** by date range or keyword.
- **Disappearing mode** — re-run on a schedule to keep clearing new messages.
- **Other platforms** — the same login-and-raw-API pattern extends to
  Twitter/X, Reddit, Discord, etc.

PRs welcome.

---

## License

GPL v3. You're free to use, modify, and distribute this — but any distributed
derivative must also be open-sourced under the same license. The work stays
in the commons.
