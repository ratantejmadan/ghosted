# ghosted

**Bulk-unsend your own Instagram direct messages — free, open-source, and entirely local.**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

ghosted deletes (unsends) the direct messages _you've_ sent on Instagram — in bulk, with no time limit. It's a free alternative to the paid data-deletion services that charge for this one job. Everything runs on your own machine against your own logged-in session; your data never touches a third-party server.

There are two ways to use it:

- **CLI** — a Python command-line tool for scripting and power users.
- **Desktop app** — a Tauri GUI (currently macOS) with a classic Aqua interface, live progress, filtering, and safety controls.

Both are driven by the same underlying engine, so they behave identically.

---

## ⚠️ Please read before using

- **This deletes _your own_ messages on _your own_ account.** It cannot delete anyone else's messages (Instagram only lets you unsend your own).
- **It uses Instagram's private API** (via [instagrapi](https://github.com/subzeroid/instagrapi)), which is **against Instagram's Terms of Service.** There is inherent risk in automating any action on your account. Use it at your own risk — your account is your responsibility.
- **Deletion is permanent and cannot be undone.** Before anything is removed, ghosted writes a local JSON backup of every affected conversation to an `export/` folder.
- **Rate limiting is built in** to keep deletions under Instagram's throttling thresholds, but no tool can _guarantee_ your account won't be flagged. Go slow.
- **Your login stays on your machine.** You log in through Instagram's real login page in a browser; ghosted captures only the resulting session token and stores it locally. Your password is never seen or stored.

---

## Features

- **Browser-based login** — authenticate through Instagram's own page (including 2FA / checkpoints); only the session is saved, locally.
- **Two-tier indexing** — a fast, lightweight pass lists all your conversations instantly; a per-conversation **Deep Scan** fetches full message history only when you ask for it.
- **Resumable deletion** — every thread's work-queue is cached to disk, so a purge can be stopped and resumed across sessions (or survive an expired login) without re-fetching or re-deleting.
- **Persistent hourly rate cap** — a rolling-window limiter never lets you exceed a hard ceiling of **200 deletions/hour** (default target: **150/hour**), and the cap _persists across restarts_ since Instagram's limit is server-side.
- **Pacing presets** — Conservative (100/hr), Balanced (150/hr, default), and Fast (180/hr), plus fully custom pacing.
- **Live progress** — real-time counts, ETA, deletion rate, and per-thread progress, with **Pause / Resume / Stop**.
- **Sort & filter** — order threads newest/oldest, and filter by your-message count, total-message count, purged status, or scan status.
- **Safe by default** — a type-to-confirm gate before any deletion, an automatic backup export, and no re-crawling of already-indexed threads unless you explicitly opt in.

---

## How it works

Instagram's official API doesn't allow reading or deleting personal DMs, so ghosted uses a captured browser session with the private mobile API (the same approach the paid services use).

**The two-tier model** keeps things fast and lets you delete a single chat without waiting for your whole account to be crawled:

1. **Index (lightweight)** — pulls just the conversation list (names, IDs, group/DM). Instant, and persisted to `threads_index.json` so the list is there every time you open the app.
2. **Deep Scan (on demand)** — for the conversations you select, fetches the full message history, counts how many are yours, and caches a delete-queue to disk.
3. **Purge** — deletes straight from those cached queues. It never re-crawls an already-scanned thread (unless you tick _Rebuild cache_); a not-yet-scanned thread is fetched once, on the fly.

**Rate limiting** is a hard rolling-window cap, not just randomized delays: before each deletion, ghosted checks how many deletions happened in the last 60 minutes and waits if it would cross the limit. The log of deletion timestamps is saved to disk, so quitting and reopening the app doesn't reset the window.

---

## Requirements

**Engine / CLI**

- Python **3.10+** (3.12 recommended)
- [instagrapi](https://github.com/subzeroid/instagrapi) and [Playwright](https://playwright.dev/python/) (installed via the steps below)

**Desktop app** (in addition to the above)

- [Node.js](https://nodejs.org/) 20+ (22 recommended)
- [Rust](https://www.rust-lang.org/tools/install) (stable toolchain)
- macOS: Xcode Command Line Tools (`xcode-select --install`)

---

## Setup

### 1. Engine (Python)

From the repository root:

```bash
cd engine
python3.12 -m venv ../venv
source ../venv/bin/activate          # Windows: ..\venv\Scripts\activate
pip install -e .
playwright install chromium          # browser used for login
```

This installs the `ghosted` command and its dependencies into the virtualenv.

### 2. Log in (once)

ghosted keeps its runtime files (your session, caches, exports) in whatever directory you run it from. Create a working directory so they don't clutter the source tree — this repo ignores a `rundir/` for exactly this:

```bash
mkdir -p rundir && cd rundir
ghosted-login                        # opens a browser; log in to Instagram there
```

A browser window opens to Instagram's login page. Log in normally (2FA included); ghosted auto-detects success and saves `session.json` locally.

### 3. Desktop app (optional)

> **Heads up:** the desktop app currently has two machine-specific paths hard-coded in `desktop/src-tauri/src/lib.rs`. Before building, open that file and set:
>
> - `VENV_PYTHON` → the absolute path to your venv's `python3`
> - `RUNDIR` → the absolute path to your `rundir/` working directory
>
> (Making these configurable is on the roadmap — see below.)

Then:

```bash
cd desktop
npm install
npm run tauri dev
```

The first build compiles the Rust dependency tree and takes a few minutes; subsequent runs are fast. A native window opens with the ghosted interface.

---

## Usage

### CLI

Run all commands from your `rundir/` working directory (with the venv activated).

```bash
# List every conversation (IDs, participants, group titles)
ghosted --list-threads

# Preview what WOULD be deleted, most recent 5 threads — deletes nothing
ghosted --dry-run --limit 5

# Back up all conversations to export/ without deleting
ghosted --export-only

# Actually unsend your messages in one specific thread
ghosted --unsend --thread-id <THREAD_ID>

# Unsend across your most recent 10 threads, gently
ghosted --unsend --limit 10 --min-delay 18 --max-delay 30

# Spread a huge purge across sessions (cap deletes per run)
ghosted --unsend --max-deletes 150
```

Useful flags: `--order recent|oldest`, `--only-user <username>`, `--batch-size`, `--pause-between-batches`, `--max-deletes`, `--include-purged`, `--rebuild-cache`, `--show-purged`. Run `ghosted --help` for the full list.

### Desktop app

1. **Log in** — click _Log in to Instagram_; if you have no valid session, a browser opens for you to authenticate.
2. **Load your list** — _Re-index now_ fetches your conversation list (fast). It persists, so it's there on every launch.
3. **Deep Scan** (optional) — select conversations and click _Deep Scan selected_ to see message counts before deleting.
4. **Purge** — select conversations, click _Purge selected…_, review the scope, type `delete` to confirm, and watch it run. Use **Pause / Resume / Stop** anytime; progress is saved.
5. **Tune pacing** — the Settings tab has presets and a _Max deletions per hour_ control (hard-capped at 200).

---

## Project structure

```
ghosted/
├── engine/                     # Python: CLI + shared engine
│   ├── pyproject.toml
│   └── ghosted/
│       ├── purge_dms.py        # CLI entry point + core fetch/cache/delete logic
│       ├── engine.py           # JSON sidecar the desktop app drives
│       ├── login_browser.py    # Playwright browser login / session capture
│       └── unsend_one.py       # single-message helper
├── desktop/                    # Tauri desktop app
│   ├── src/index.html          # the UI (Vanilla JS + CSS)
│   └── src-tauri/
│       ├── src/lib.rs          # Rust host (spawns the Python sidecar)
│       └── tauri.conf.json
├── rundir/                     # runtime files (git-ignored): session, caches, exports
└── .github/workflows/          # PyPI release workflow
```

**Runtime files** (all written to your working directory, none committed): `session.json` (your login), `threads_index.json` (conversation list), `cache/queue_*.json` (per-thread delete queues), `rate_log.json` (hourly-cap timestamps), `purged_threads.json` (ledger), and `export/` (pre-deletion backups).

---

## Roadmap

- **Portable desktop config** — replace the hard-coded `lib.rs` paths with an env var / config file so anyone can build without editing source.
- **Packaged builds** — signed/notarized installers so non-technical users can download and run (currently: build from source).
- **Scheduled cleanups** — the Schedule tab is a UI placeholder; the recurring-purge backend isn't wired up yet.
- **Incremental re-index** — append only new messages instead of a full re-fetch.
- **Cross-platform** — Windows support alongside macOS.

---

## License

[GPL-3.0](LICENSE). Commercial derivatives must remain open-source under the same license.

Published on PyPI as [`getghosted`](https://pypi.org/project/getghosted/). Built by Celestara Dynamics.

---

_This project is not affiliated with, endorsed by, or connected to Instagram or Meta in any way._
