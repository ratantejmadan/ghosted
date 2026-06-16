# ig-dm-purge

Export and bulk-unsend your own Instagram direct messages — a free,
open-source alternative to paid "social media cleanup" tools for this
specific use case.

## What it does

1. **Exports** every DM thread and message to a local JSON file (a backup,
   so you have a record of your conversation history before it's gone).
2. **Unsends** every message *you* sent, across every thread, with
   randomized delays between actions.

Optionally, it can also "remove for me" messages that *other people* sent
you — note this only hides them from your own view, it does not delete
them from the other person's account (Instagram doesn't allow that).

## ⚠️ Important caveats

- This uses [`instagrapi`](https://github.com/subzeroid/instagrapi), an
  **unofficial** client that mimics Instagram's private mobile API. Using
  it is against Instagram's Terms of Service.
- Possible consequences: temporary action blocks, "checkpoint" challenges
  requiring you to verify via the app, or in rare cases account
  restrictions. Risk increases with very fast/aggressive deletion.
- **Run the export step first and keep the JSON file.** Once a message is
  unsent, it's gone — Instagram does not let you retrieve it.
- Large histories (years of messages) can take hours due to the
  randomized delays. This is intentional — going faster increases risk.
- If you have 2FA enabled, you'll need to supply a fresh TOTP code (this
  script does not handle SMS-based 2FA).

## Setup

```bash
git clone <this-repo>
cd ig-dm-purge
pip install -r requirements.txt
```

## Usage

Set credentials via environment variables (recommended, avoids them
showing up in shell history) or pass as flags:

```bash
export IG_USERNAME="your_username"
export IG_PASSWORD="your_password"
export IG_TOTP="123456"   # only if 2FA is enabled, must be current
```

### 1. Back up everything first

```bash
python purge_dms.py --export-only
```

This writes `export/dm_export_<timestamp>.json`.

### 2. Dry run — see what would be deleted

```bash
python purge_dms.py --dry-run
```

### 3. Actually unsend your messages

```bash
python purge_dms.py --unsend
```

### 4. (Optional) Also remove messages others sent, from your view

```bash
python purge_dms.py --unsend --include-others-messages
```

### Tuning delays

```bash
python purge_dms.py --unsend --min-delay 3 --max-delay 8
```

Longer delays = slower but lower risk of rate limiting / checkpoints.

## How it works

- Logs in once and caches the session (`session.json`) to avoid repeated
  logins, which are more likely to trigger security checks.
- Paginates through `direct_threads()` and `direct_messages()` to get full
  history, not just recent messages.
- Calls Instagram's private "unsend" endpoint
  (`direct_message_delete`) for each message, with randomized human-like
  pauses between calls.

## Contributing

This is an early starting point. Ideas for improvement:

- Resume support (skip already-deleted messages on re-run)
- Filtering by date range, keyword, or specific thread
- Support for other platforms (Twitter/X, Discord, Reddit, etc.) using a
  similar pattern
- A simple GUI/CLI progress bar
- Better handling of Instagram's various 2FA flows

PRs welcome.

## License

MIT — do whatever you want with this, including building something
better.
