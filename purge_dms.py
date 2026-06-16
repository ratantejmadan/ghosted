#!/usr/bin/env python3
"""
purge_dms.py
------------
Export and bulk-unsend your own Instagram direct messages.

LOGIN: This script no longer takes a username/password. Instead, run
login_browser.py first - it opens Instagram's real login page in a browser,
you log in there, and it saves your session to session.json. This script
then reuses that session.

What it does:
  1. Exports all DM threads + messages to a local JSON archive (backup).
  2. Optionally unsends every message you've sent, across all threads,
     with randomized delays to behave like a normal user session.

Usage:
    python login_browser.py                      # do this first, once
    python purge_dms.py --export-only            # just back up everything
    python purge_dms.py --dry-run                # show what WOULD be deleted
    python purge_dms.py --unsend                 # actually unsend messages
    python purge_dms.py --unsend --include-others-messages
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from instagrapi import Client
from instagrapi.exceptions import ClientError, LoginRequired

SESSION_FILE = "session.json"
EXPORT_DIR = "export"


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def get_client():
    """Load the session captured by login_browser.py and authenticate."""
    if not os.path.exists(SESSION_FILE):
        print("ERROR: no session.json found.\n"
              "Run 'python login_browser.py' first to log in via the browser.")
        sys.exit(1)

    with open(SESSION_FILE) as f:
        session = json.load(f)

    sessionid = session.get("sessionid")
    if not sessionid:
        print("ERROR: session.json has no sessionid. Re-run login_browser.py.")
        sys.exit(1)

    # Defensive: if an older session.json stored the URL-encoded value
    # (colons as %3A), decode it. unquote() is a no-op on already-decoded
    # strings, so this is always safe.
    sessionid = unquote(sessionid)

    cl = Client()
    try:
        # login_by_sessionid validates the session itself by fetching the
        # account's own user info. If it returns True, we're authenticated;
        # no extra (and flakier) calls like get_timeline_feed needed.
        cl.login_by_sessionid(sessionid)
        log(f"Authenticated as @{cl.username} (user id {cl.user_id}).")
    except Exception as e:
        print(f"ERROR: session invalid or expired ({e}).\n"
              "Re-run 'python login_browser.py' to capture a fresh session.")
        sys.exit(1)

    return cl


def _tracking_params(cl):
    """Reuse instagrapi's tracking params if available, else a safe minimal set."""
    try:
        return cl._direct_request_tracking_params()
    except Exception:
        return {}


def fetch_inbox_threads_raw(cl, limit=0):
    """
    List DM threads via raw private API calls.

    limit: if > 0, stop once we have at least this many threads (the inbox
    is returned newest-first, so this gives the most recent N). 0 = all.

    We deliberately avoid cl.direct_threads(), because its extractor builds
    strict pydantic models for every message — including shared posts/reels
    whose internal 'instagram://' media URLs fail validation and crash the
    whole fetch. Here we parse the raw JSON ourselves and only keep the
    fields we need.
    """
    threads = []
    cursor = None
    while True:
        params = {
            **_tracking_params(cl),
            "visual_message_return_type": "unseen",
            "thread_message_limit": "10",
            "persistentBadging": "true",
            "limit": "20",
        }
        if cursor:
            params.update({"cursor": cursor, "direction": "older"})

        result = cl.private_request("direct_v2/inbox/", params=params)
        inbox = result.get("inbox", {})
        threads.extend(inbox.get("threads", []))

        if limit and len(threads) >= limit:
            return threads[:limit]
        if not inbox.get("has_older"):
            break
        cursor = inbox.get("oldest_cursor")
        if not cursor:
            break
        time.sleep(random.uniform(0.5, 1.2))

    return threads


def fetch_thread_messages_raw(cl, thread_id):
    """
    Pull the full message history of one thread via raw API calls,
    paginating with the thread's oldest_cursor. Returns a list of raw
    message-item dicts (we don't model them — we just read keys).
    """
    items = []
    cursor = None
    while True:
        params = {
            "visual_message_return_type": "unseen",
            "direction": "older",
            "limit": "20",
        }
        if cursor:
            params["cursor"] = cursor

        result = cl.private_request(f"direct_v2/threads/{thread_id}/", params=params)
        thread = result.get("thread", {})
        items.extend(thread.get("items", []))

        cursor = thread.get("oldest_cursor")
        if not cursor or not thread.get("has_older"):
            break
        time.sleep(random.uniform(0.5, 1.2))

    return items


def _parse_message(item):
    """Pull just the fields we need from a raw message item, tolerating
    whatever attachment type it is."""
    return {
        "id": str(item.get("item_id", "")),
        "user_id": str(item.get("user_id", "")),
        "timestamp": item.get("timestamp"),
        "item_type": item.get("item_type"),
        # text only exists on text items; other types (media, shares,
        # reels, etc.) just won't have it, which is fine.
        "text": item.get("text"),
    }


def list_threads(cl, limit=0):
    """Print all DM threads with their IDs, type, participants, and recent
    message count, so you can find the exact thread (e.g. a group) to target."""
    raw_threads = fetch_inbox_threads_raw(cl, limit=limit)
    suffix = f" (most recent {limit})" if limit else ""
    log(f"Found {len(raw_threads)} thread(s){suffix}:\n")
    for t in raw_threads:
        thread_id = t.get("thread_id")
        is_group = t.get("is_group", False)
        title = t.get("thread_title") or ""
        users = [u.get("username") for u in t.get("users", [])]
        kind = "GROUP" if is_group else "1:1  "
        label = title if title else ", ".join(users)
        print(f"  [{kind}] {thread_id}")
        print(f"          {label}")
        if is_group:
            print(f"          participants: {', '.join(users)}")
        print()
    print("To target one thread:  python purge_dms.py --thread-id <ID> --dry-run")


def export_threads(cl, export_dir, only_user=None, thread_id=None):
    Path(export_dir).mkdir(parents=True, exist_ok=True)
    log("Fetching DM threads (this can take a while for large histories)...")

    raw_threads = fetch_inbox_threads_raw(cl)

    if thread_id:
        raw_threads = [t for t in raw_threads
                       if str(t.get("thread_id")) == str(thread_id)]
        if not raw_threads:
            log(f"No thread found with id '{thread_id}'. "
                f"Run --list-threads to see valid IDs.")
            return [], None
        log(f"Targeting thread {thread_id}.")
    elif only_user:
        only_user = only_user.lstrip("@").lower()
        raw_threads = [
            t for t in raw_threads
            if any((u.get("username") or "").lower() == only_user
                   for u in t.get("users", []))
        ]
        if not raw_threads:
            log(f"No thread found with user '{only_user}'. "
                f"Check the username spelling.")
            return [], None
        log(f"Filtered to {len(raw_threads)} thread(s) with '{only_user}'.")

    all_threads = []
    for i, t in enumerate(raw_threads, 1):
        tid = t.get("thread_id")
        thread_data = {
            "thread_id": tid,
            "is_group": t.get("is_group", False),
            "thread_title": t.get("thread_title"),
            "users": [u.get("username") for u in t.get("users", [])],
            "messages": [],
        }

        try:
            raw_items = fetch_thread_messages_raw(cl, tid)
        except Exception as e:
            log(f"  Warning: couldn't fetch full history for {tid}: {e}")
            raw_items = t.get("items", [])  # fall back to recent items inline

        for item in raw_items:
            thread_data["messages"].append(_parse_message(item))

        all_threads.append(thread_data)
        label = thread_data["thread_title"] or ", ".join(thread_data["users"])
        log(f"  [{i}/{len(raw_threads)}] thread {tid} ({label}): "
            f"{len(thread_data['messages'])} messages")

        time.sleep(random.uniform(0.5, 1.5))

    out_path = Path(export_dir) / f"dm_export_{datetime.now():%Y%m%d_%H%M%S}.json"
    with open(out_path, "w") as f:
        json.dump(all_threads, f, indent=2, default=str)

    log(f"Export complete: {out_path} ({len(all_threads)} threads)")
    return all_threads, out_path


def unsend_messages(cl, all_threads, dry_run=True, include_others=False,
                    min_delay=2.0, max_delay=6.0,
                    batch_size=0, pause_between_batches=0):
    my_user_id = str(cl.user_id)

    to_delete = []
    for thread in all_threads:
        for m in thread["messages"]:
            if m["user_id"] == my_user_id or include_others:
                to_delete.append((thread["thread_id"], m["id"], m["user_id"]))

    log(f"Found {len(to_delete)} messages to "
        f"{'review' if dry_run else 'delete'}.")

    if dry_run:
        for thread_id, msg_id, user_id in to_delete[:20]:
            mine = "yours" if user_id == my_user_id else "other's"
            log(f"  Would delete ({mine}) msg {msg_id} in thread {thread_id}")
        if len(to_delete) > 20:
            log(f"  ... and {len(to_delete) - 20} more.")
        if batch_size > 0:
            n_batches = (len(to_delete) + batch_size - 1) // batch_size
            log(f"Would run in {n_batches} batch(es) of up to {batch_size}, "
                f"pausing ~{pause_between_batches}s between them.")
        log("Dry run complete. Re-run with --unsend to actually delete.")
        return

    deleted, failed = 0, 0
    for idx, (thread_id, msg_id, user_id) in enumerate(to_delete, 1):
        try:
            cl.direct_message_delete(thread_id, msg_id)
            deleted += 1
            if idx % 10 == 0:
                log(f"  Progress: {idx}/{len(to_delete)} "
                    f"({deleted} deleted, {failed} failed)")
        except LoginRequired:
            log("Session expired mid-run. Re-run login_browser.py, then "
                "re-run this script (deleted messages won't reappear).")
            break
        except ClientError as e:
            failed += 1
            log(f"  Failed on msg {msg_id}: {e}")
            time.sleep(random.uniform(10, 20))  # back off harder on errors
            continue

        # Pause between individual deletes (human-like pacing)
        time.sleep(random.uniform(min_delay, max_delay))

        # After every `batch_size` deletes, take a long break. This breaks up
        # the continuous-stream pattern into human-sized sessions, which is
        # the single biggest factor in not tripping action blocks on large
        # purges. A little jitter (±20%) keeps the pauses from looking robotic.
        if batch_size > 0 and idx % batch_size == 0 and idx < len(to_delete):
            jitter = random.uniform(0.8, 1.2)
            pause = pause_between_batches * jitter
            log(f"  Batch of {batch_size} done ({idx}/{len(to_delete)}). "
                f"Pausing {pause/60:.1f} min before next batch...")
            time.sleep(pause)

    log(f"Done. Deleted {deleted}, failed {failed}, "
        f"out of {len(to_delete)} total.")


def main():
    parser = argparse.ArgumentParser(description="Export and purge Instagram DMs.")
    parser.add_argument("--export-only", action="store_true",
                        help="Only export DM history, don't delete anything")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be deleted without deleting")
    parser.add_argument("--unsend", action="store_true",
                        help="Actually delete/unsend messages")
    parser.add_argument("--include-others-messages", action="store_true",
                        help="Also remove messages others sent (from your view)")
    parser.add_argument("--only-user", default=None,
                        help="Limit everything to the DM thread with this "
                             "username (for testing). Group threads match if "
                             "the user is a participant.")
    parser.add_argument("--thread-id", default=None,
                        help="Limit everything to one exact thread by its ID "
                             "(the precise way to target a specific group "
                             "chat). Use --list-threads to find the ID.")
    parser.add_argument("--list-threads", action="store_true",
                        help="List all threads (IDs, group titles, "
                             "participants) and exit. Deletes nothing.")
    parser.add_argument("--limit", type=int, default=0,
                        help="With --list-threads, only fetch the N most "
                             "recent threads (faster for testing). 0 = all.")
    parser.add_argument("--min-delay", type=float, default=2.0)
    parser.add_argument("--max-delay", type=float, default=6.0)
    parser.add_argument("--batch-size", type=int, default=0,
                        help="Delete in chunks of this many messages, with a "
                             "long pause between chunks. 0 = no batching "
                             "(continuous). Try 100-200 for large purges.")
    parser.add_argument("--pause-between-batches", type=float, default=900,
                        help="Seconds to pause between batches (default 900 = "
                             "15 min). Only used when --batch-size > 0.")
    args = parser.parse_args()

    cl = get_client()

    if args.list_threads:
        list_threads(cl, limit=args.limit)
        return

    all_threads, _ = export_threads(
        cl, EXPORT_DIR,
        only_user=args.only_user,
        thread_id=args.thread_id,
    )

    if not all_threads:
        return

    if args.export_only:
        return

    unsend_messages(
        cl,
        all_threads,
        dry_run=not args.unsend,
        include_others=args.include_others_messages,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        batch_size=args.batch_size,
        pause_between_batches=args.pause_between_batches,
    )


if __name__ == "__main__":
    main()