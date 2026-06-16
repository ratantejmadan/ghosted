#!/usr/bin/env python3
"""
ig-dm-purge
-----------
Export and bulk-unsend your own Instagram direct messages.

This tool logs into YOUR Instagram account (using your own credentials,
stored only locally) and:

  1. Exports all DM threads + messages to a local JSON archive (backup).
  2. Optionally unsends every message you've sent, across all threads,
     with randomized delays to behave like a normal user session.

Usage:
    python purge_dms.py --export-only          # just back up everything
    python purge_dms.py --dry-run              # show what WOULD be deleted
    python purge_dms.py --unsend                # actually unsend messages
    python purge_dms.py --unsend --include-others-messages
                                                 # also delete-for-me on
                                                 # messages others sent
                                                 # (doesn't remove their copy)

Notes:
  - Instagram only lets you "Unsend" messages YOU sent (removes for everyone).
  - For messages others sent, you can only "Remove for you" (removes from
    your view only, not theirs) - that's what --include-others-messages does.
  - This uses instagrapi, an unofficial/reverse-engineered client. Using it
    is against Instagram's Terms of Service. Your account could be rate
    limited, challenged (checkpoint), or banned. Use a non-critical account
    first if you want to test, and keep delays conservative.
  - 2FA (TOTP) is supported. App passwords / SMS 2FA may need adjustment.
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from instagrapi import Client
from instagrapi.exceptions import ClientError, LoginRequired

SESSION_FILE = "session.json"
EXPORT_DIR = "export"


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def get_client(username, password, totp_code=None):
    cl = Client()

    # Reuse a saved session if we have one, to avoid repeated logins
    # (repeated logins are one of the things that trigger checkpoints).
    if os.path.exists(SESSION_FILE):
        try:
            cl.load_settings(SESSION_FILE)
            cl.login(username, password)
            cl.get_timeline_feed()  # cheap call to confirm session is valid
            log("Resumed existing session.")
            return cl
        except Exception as e:
            log(f"Saved session invalid ({e}), logging in fresh.")

    if totp_code:
        cl.login(username, password, verification_code=totp_code)
    else:
        cl.login(username, password)

    cl.dump_settings(SESSION_FILE)
    log("Logged in and saved session.")
    return cl


def export_threads(cl, export_dir):
    Path(export_dir).mkdir(parents=True, exist_ok=True)
    log("Fetching DM threads (this can take a while for large histories)...")

    all_threads = []
    threads = cl.direct_threads(amount=0)  # 0 = no limit, paginate all

    for i, thread in enumerate(threads, 1):
        thread_data = {
            "thread_id": thread.id,
            "users": [u.username for u in thread.users],
            "messages": [],
        }

        # direct_threads() usually returns recent messages; pull full history
        try:
            messages = cl.direct_messages(thread.id, amount=0)
        except Exception as e:
            log(f"  Warning: couldn't fetch full history for thread {thread.id}: {e}")
            messages = thread.messages

        for m in messages:
            thread_data["messages"].append({
                "id": m.id,
                "user_id": str(m.user_id),
                "timestamp": m.timestamp.isoformat() if m.timestamp else None,
                "item_type": m.item_type,
                "text": m.text,
            })

        all_threads.append(thread_data)
        log(f"  [{i}/{len(threads)}] thread {thread.id}: {len(thread_data['messages'])} messages")

        time.sleep(random.uniform(0.5, 1.5))

    out_path = Path(export_dir) / f"dm_export_{datetime.now():%Y%m%d_%H%M%S}.json"
    with open(out_path, "w") as f:
        json.dump(all_threads, f, indent=2, default=str)

    log(f"Export complete: {out_path} ({len(all_threads)} threads)")
    return all_threads, out_path


def unsend_messages(cl, all_threads, dry_run=True, include_others=False,
                     min_delay=2.0, max_delay=6.0):
    my_user_id = str(cl.user_id)

    to_delete = []
    for thread in all_threads:
        for m in thread["messages"]:
            if m["user_id"] == my_user_id or include_others:
                to_delete.append((thread["thread_id"], m["id"], m["user_id"]))

    log(f"Found {len(to_delete)} messages to {'review' if dry_run else 'delete'}.")

    if dry_run:
        for thread_id, msg_id, user_id in to_delete[:20]:
            mine = "yours" if user_id == my_user_id else "other's"
            log(f"  Would delete ({mine}) msg {msg_id} in thread {thread_id}")
        if len(to_delete) > 20:
            log(f"  ... and {len(to_delete) - 20} more.")
        log("Dry run complete. Re-run with --unsend to actually delete.")
        return

    deleted, failed = 0, 0
    for idx, (thread_id, msg_id, user_id) in enumerate(to_delete, 1):
        try:
            if user_id == my_user_id:
                cl.direct_message_delete(thread_id, msg_id)  # unsend (everyone)
            else:
                # "remove for me" - only available for others' messages
                cl.direct_message_delete(thread_id, msg_id, revoke=False) \
                    if hasattr(cl, "direct_message_delete") else None

            deleted += 1
            if idx % 10 == 0:
                log(f"  Progress: {idx}/{len(to_delete)} ({deleted} deleted, {failed} failed)")
        except LoginRequired:
            log("Session expired mid-run. Re-run the script to resume "
                "(already-deleted messages won't reappear).")
            break
        except ClientError as e:
            failed += 1
            log(f"  Failed on msg {msg_id}: {e}")
            # Back off harder on errors - likely rate limiting
            time.sleep(random.uniform(10, 20))
            continue

        time.sleep(random.uniform(min_delay, max_delay))

    log(f"Done. Deleted {deleted}, failed {failed}, out of {len(to_delete)} total.")


def main():
    parser = argparse.ArgumentParser(description="Export and purge Instagram DMs.")
    parser.add_argument("--username", default=os.environ.get("IG_USERNAME"))
    parser.add_argument("--password", default=os.environ.get("IG_PASSWORD"))
    parser.add_argument("--totp", default=os.environ.get("IG_TOTP"),
                         help="Current 2FA TOTP code, if enabled")
    parser.add_argument("--export-only", action="store_true",
                         help="Only export DM history, don't delete anything")
    parser.add_argument("--dry-run", action="store_true",
                         help="Show what would be deleted without deleting")
    parser.add_argument("--unsend", action="store_true",
                         help="Actually delete/unsend messages")
    parser.add_argument("--include-others-messages", action="store_true",
                         help="Also remove messages others sent (from your view only)")
    parser.add_argument("--min-delay", type=float, default=2.0)
    parser.add_argument("--max-delay", type=float, default=6.0)
    args = parser.parse_args()

    if not args.username or not args.password:
        print("ERROR: provide --username/--password or set IG_USERNAME/IG_PASSWORD env vars.")
        sys.exit(1)

    cl = get_client(args.username, args.password, args.totp)

    all_threads, export_path = export_threads(cl, EXPORT_DIR)

    if args.export_only:
        return

    unsend_messages(
        cl,
        all_threads,
        dry_run=not args.unsend,
        include_others=args.include_others_messages,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
    )


if __name__ == "__main__":
    main()
