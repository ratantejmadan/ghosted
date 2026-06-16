# ghosted

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

| File               | Purpose                                                                                      |
| ------------------ | -------------------------------------------------------------------------------------------- |
| `login_browser.py` | One-time browser login; captures and saves your session.                                     |
| `purge_dms.py`     | The main tool: list, export, dry-run, and unsend.                                            |
| `unsend_one.py`    | Test harness — unsends a single message by ID, to confirm everything works before a big run. |
| `requirements.txt` | Python dependencies.                                                                         |
| `session.json`     | Your saved session (created by login; git-ignored, sensitive).                               |
| `export/`          | Timestamped JSON backups written before each run (git-ignored).                              |

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

On a Mac, prefix with `caffeinate -i` to stop the machine sleeping during a
long run:

```bash
caffeinate -i python3 purge_dms.py --unsend --batch-size 100 --pause-between-batches 1800
```

---

## All options

| Flag                              | What it does                                                                   |
| --------------------------------- | ------------------------------------------------------------------------------ |
| `--list-threads`                  | Print all threads (IDs, group titles, participants) and exit. Deletes nothing. |
| `--limit N`                       | With `--list-threads`, only fetch the N most recent threads. `0` = all.        |
| `--only-user USERNAME`            | Limit to the thread with this user (matches groups they're in).                |
| `--thread-id ID`                  | Limit to one exact thread — the precise way to target a group.                 |
| `--export-only`                   | Back up history only; delete nothing.                                          |
| `--dry-run`                       | Show what _would_ be deleted.                                                  |
| `--unsend`                        | Actually unsend messages.                                                      |
| `--include-others-messages`       | Also remove others' messages from _your_ view (does not delete their copy).    |
| `--min-delay` / `--max-delay`     | Random pause range (seconds) between deletes. Default 2–6.                     |
| `--batch-size N`                  | Delete in chunks of N, with a long pause between chunks. `0` = continuous.     |
| `--pause-between-batches SECONDS` | Length of that pause (default 900 = 15 min), with jitter.                      |

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

## Troubleshooting

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

- **Resume support** — record which message IDs are already deleted so a
  mid-run session expiry doesn't force a full re-scan from the top. (The
  single most useful addition for large purges.)
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
