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
PURGED_FILE = "purged_threads.json"


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def load_purged():
    """Load the ledger of already-purged thread IDs -> last purged time."""
    if not os.path.exists(PURGED_FILE):
        return {}
    try:
        with open(PURGED_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_purged(purged):
    with open(PURGED_FILE, "w") as f:
        json.dump(purged, f, indent=2)


def mark_purged(thread_id, purged=None):
    """Record a thread as fully purged (idempotent). Updates timestamp on
    re-purge. Returns the updated ledger."""
    if purged is None:
        purged = load_purged()
    purged[str(thread_id)] = datetime.now().isoformat(timespec="seconds")
    save_purged(purged)
    return purged


# ---- Human-readable progress tracker -------------------------------------
# progress.json holds the denominator (total thread count from --list-threads);
# the numerator (threads purged) always comes live from the purged ledger.
# PROGRESS.txt is the pretty, viewable rendering, refreshed on every change.
PROGRESS_STATE = "progress.json"
PROGRESS_VIEW = "PROGRESS.txt"


def load_progress_state():
    if os.path.exists(PROGRESS_STATE):
        try:
            with open(PROGRESS_STATE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"total_threads": None, "total_counted_at": None}


def render_progress():
    """Rewrite PROGRESS.txt from current state + the purged ledger."""
    state = load_progress_state()
    done = len(load_purged())
    total = state.get("total_threads")

    lines = ["ghosted — purge progress", "=" * 28, ""]
    if total:
        remaining = max(total - done, 0)
        pct = (done / total * 100) if total else 0
        bar_len = 20
        filled = min(bar_len, int(round(bar_len * done / total))) if total else 0
        bar = "#" * filled + "-" * (bar_len - filled)
        lines += [
            f"Threads purged:  {done}/{total}  ({pct:.0f}%)",
            f"[{bar}]",
            f"Remaining:       {remaining}",
            f"Total counted:   {state.get('total_counted_at')}",
        ]
    else:
        lines += [
            f"Threads purged:  {done}",
            "Total unknown — run --list-threads (no --limit) to count.",
        ]
    lines += ["", f"Last updated:    {datetime.now().isoformat(timespec='seconds')}"]

    with open(PROGRESS_VIEW, "w") as f:
        f.write("\n".join(lines) + "\n")


def set_total_threads(total):
    """Record the full inbox thread count (the denominator) and refresh view."""
    state = load_progress_state()
    state["total_threads"] = total
    state["total_counted_at"] = datetime.now().isoformat(timespec="seconds")
    with open(PROGRESS_STATE, "w") as f:
        json.dump(state, f, indent=2)
    render_progress()


# ---- Per-thread work-queue cache (resumable across sessions) --------------
# For huge threads (years of history), we fetch the full message list ONCE,
# persist the queue of message IDs to delete, and consume it across as many
# sessions as needed. On resume we read the cache and skip the expensive
# re-fetch entirely. Valid because the account is frozen (no new messages).
CACHE_DIR = "cache"


def queue_path(thread_id):
    return os.path.join(CACHE_DIR, f"queue_{thread_id}.json")


def load_queue(thread_id):
    p = queue_path(thread_id)
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            return None
    return None


def save_queue(q):
    """Write the queue atomically so an interrupted write can't corrupt it."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = queue_path(q["thread_id"])
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(q, f)
    os.replace(tmp, p)


def build_queue(cl, summary, my_user_id, include_others, export_dir):
    """Deep-fetch a thread's full history once, write a backup export, and
    build the pending list of our deletable message IDs."""
    tid = str(summary.get("thread_id"))
    users = [u.get("username") for u in summary.get("users", [])]
    log(f"  Building cache for thread {tid} "
        f"({summary.get('thread_title') or ', '.join(users)}) — "
        f"fetching full history...")

    raw_items = fetch_thread_messages_raw(cl, tid)
    messages = [_parse_message(i) for i in raw_items]

    # Backup this thread's history before we delete anything.
    Path(export_dir).mkdir(parents=True, exist_ok=True)
    backup = {
        "thread_id": tid,
        "users": users,
        "thread_title": summary.get("thread_title"),
        "messages": messages,
    }
    with open(Path(export_dir) / f"thread_{tid}.json", "w") as f:
        json.dump(backup, f, indent=2, default=str)

    pending = [
        m["id"] for m in messages
        if m.get("item_type") not in NON_DELETABLE
        and (m["user_id"] == my_user_id or include_others)
    ]
    q = {
        "thread_id": tid,
        "users": users,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "complete": True,          # full history was fetched
        "total_messages": len(messages),   # everyone's messages (for UI)
        "thread_title": summary.get("thread_title"),
        "total": len(pending),
        "pending": pending,
        "done": [],
    }
    save_queue(q)
    log(f"  Cached {len(pending)} deletable message(s) for thread {tid}.")
    return q


def get_queue(cl, summary, my_user_id, include_others, export_dir,
              rebuild=False):
    """Load a thread's queue from cache, or build it if missing/rebuild."""
    tid = str(summary.get("thread_id"))
    if not rebuild:
        q = load_queue(tid)
        if q and q.get("complete"):
            remaining = len(q.get("pending", []))
            log(f"  Resuming thread {tid} from cache "
                f"({remaining} of {q.get('total', remaining)} left).")
            return q
    return build_queue(cl, summary, my_user_id, include_others, export_dir)


NON_DELETABLE = {"action_log", "placeholder", "video_call_event"}


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


def select_threads(cl, limit=0, order="recent", skip_purged=True):
    """
    Return threads ordered and limited as requested.

    Instagram returns the inbox newest-first (by most recent activity).
      - order="recent": that native order. With a limit we can stop paging
        early once we have N (fast) — but only when we're NOT skipping
        purged threads (skipping needs the full list to count correctly).
      - order="oldest": we must fetch the whole inbox first, then reverse,
        because the oldest threads are at the very end.

    skip_purged: drop threads already recorded in the purged ledger, so a
    --limit counts only threads you haven't cleared yet.
    """
    purged = load_purged() if skip_purged else {}

    # If we're skipping purged threads, we can't stop paging early (an early
    # batch might be all-purged), so fetch everything then filter + slice.
    early_limit = limit if (order == "recent" and not skip_purged) else 0
    raw = fetch_inbox_threads_raw(cl, limit=early_limit)

    if order == "oldest":
        raw = list(reversed(raw))

    if skip_purged and purged:
        before = len(raw)
        raw = [t for t in raw if str(t.get("thread_id")) not in purged]
        dropped = before - len(raw)
        if dropped:
            log(f"Skipping {dropped} already-purged thread(s). "
                f"Use --include-purged to include them.")

    if limit:
        raw = raw[:limit]
    return raw


def list_threads(cl, limit=0, order="recent", skip_purged=False):
    """Print all DM threads with their IDs, type, participants, and recent
    message count, so you can find the exact thread (e.g. a group) to target."""
    raw_threads = select_threads(cl, limit=limit, order=order,
                                 skip_purged=skip_purged)
    word = "oldest" if order == "oldest" else "most recent"
    suffix = f" ({word} {limit})" if limit else f" ({word} first)"
    log(f"Found {len(raw_threads)} thread(s){suffix}:\n")

    # A full, unfiltered listing is our chance to record the true total
    # thread count (the denominator) for the progress tracker.
    if not limit and not skip_purged:
        set_total_threads(len(raw_threads))
        log(f"Recorded total of {len(raw_threads)} threads in {PROGRESS_VIEW}.")

    purged = load_purged()
    for t in raw_threads:
        thread_id = t.get("thread_id")
        is_group = t.get("is_group", False)
        title = t.get("thread_title") or ""
        users = [u.get("username") for u in t.get("users", [])]
        kind = "GROUP" if is_group else "1:1  "
        label = title if title else ", ".join(users)
        flag = "  (purged)" if str(thread_id) in purged else ""
        print(f"  [{kind}] {thread_id}{flag}")
        print(f"          {label}")
        if is_group:
            print(f"          participants: {', '.join(users)}")
        print()
    print("To target one thread:  python purge_dms.py --thread-id <ID> --dry-run")


def export_threads(cl, export_dir, only_user=None, thread_id=None, limit=0,
                   order="recent", skip_purged=True):
    Path(export_dir).mkdir(parents=True, exist_ok=True)
    log("Fetching DM threads (this can take a while for large histories)...")

    if thread_id or only_user:
        # Targeting a specific thread/user: search the whole inbox; order,
        # limit, and the purged-ledger skip don't apply.
        raw_threads = fetch_inbox_threads_raw(cl, limit=0)
    else:
        raw_threads = select_threads(cl, limit=limit, order=order,
                                     skip_purged=skip_purged)

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
    elif limit:
        word = "oldest" if order == "oldest" else "most recent"
        log(f"Limiting to your {len(raw_threads)} {word} thread(s).")

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
    skipped_system = 0
    thread_totals = {}  # thread_id -> count of deletable messages we'll attempt
    for thread in all_threads:
        tid = thread["thread_id"]
        for m in thread["messages"]:
            if m.get("item_type") in NON_DELETABLE:
                skipped_system += 1
                continue
            if m["user_id"] == my_user_id or include_others:
                to_delete.append((tid, m["id"], m["user_id"]))
                thread_totals[tid] = thread_totals.get(tid, 0) + 1

    # Threads that have NO deletable messages (e.g. you never sent anything,
    # or only system items) are still "done" — record them so they don't get
    # rescanned by future --limit runs.
    threads_with_nothing = [t["thread_id"] for t in all_threads
                            if thread_totals.get(t["thread_id"], 0) == 0]

    log(f"Found {len(to_delete)} deletable messages across "
        f"{len(thread_totals)} thread(s) "
        f"({skipped_system} system/placeholder items skipped).")

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

    # Per-thread progress tracking so we can mark a thread purged the moment
    # its last message is handled (survives interruptions).
    purged_ledger = load_purged()
    thread_done = {tid: 0 for tid in thread_totals}
    thread_rate_failed = set()

    # Threads with nothing to delete are immediately complete.
    for tid in threads_with_nothing:
        mark_purged(tid, purged_ledger)
    if threads_with_nothing:
        render_progress()

    deleted, failed, item_errors = 0, 0, 0
    for idx, (thread_id, msg_id, user_id) in enumerate(to_delete, 1):
        try:
            cl.direct_message_delete(thread_id, msg_id)
            deleted += 1
            thread_done[thread_id] += 1
            if idx % 10 == 0:
                log(f"  Progress: {idx}/{len(to_delete)} "
                    f"({deleted} deleted, {failed + item_errors} failed)")
        except LoginRequired:
            log("Session expired mid-run. Re-run login_browser.py, then "
                "re-run this script (deleted messages won't reappear; "
                "completed threads are saved and will be skipped).")
            break
        except ClientError as e:
            msg = str(e).lower()
            # Distinguish genuine rate limiting from harmless item-level
            # errors. Rate limiting needs a real backoff; an un-deletable
            # item just gets skipped so we don't waste time.
            is_rate_limit = any(s in msg for s in (
                "feedback_required", "wait a few minutes", "rate", "429",
                "please wait", "try again later",
            ))
            if is_rate_limit:
                failed += 1
                thread_rate_failed.add(thread_id)
                log(f"  Rate-limit signal on msg {msg_id} — backing off. ({e})")
                time.sleep(random.uniform(60, 120))  # real backoff
            else:
                # e.g. 1545003: message can't be unsent (already gone, or a
                # type that doesn't support unsend). Counts as handled.
                item_errors += 1
                thread_done[thread_id] += 1
                log(f"  Skipped msg {msg_id} (can't be unsent): {e}")
            continue

        # If this thread's every deletable message has now been handled and
        # it had no rate-limit failures, record it as purged right away.
        if (thread_done[thread_id] >= thread_totals[thread_id]
                and thread_id not in thread_rate_failed):
            mark_purged(thread_id, purged_ledger)
            render_progress()  # live-update the tracker as each thread finishes

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

    fully_purged = sum(
        1 for tid in thread_totals
        if thread_done[tid] >= thread_totals[tid]
        and tid not in thread_rate_failed
    ) + len(threads_with_nothing)

    render_progress()
    log(f"Done. Deleted {deleted}, "
        f"skipped {item_errors} un-deletable, "
        f"{failed} rate-limit failures, out of {len(to_delete)} attempted.")
    log(f"{fully_purged} thread(s) recorded as purged in {PURGED_FILE}.")
    state = load_progress_state()
    if state.get("total_threads"):
        log(f"Overall progress: {len(load_purged())}/{state['total_threads']} "
            f"threads purged (see {PROGRESS_VIEW}).")
    else:
        log(f"Run --list-threads (no --limit) to record a total for "
            f"{PROGRESS_VIEW}.")


def resolve_targets(cl, only_user=None, thread_id=None, limit=0,
                    order="recent", skip_purged=True):
    """Return the list of raw thread summaries to purge, applying the same
    targeting/filtering rules as export_threads — but WITHOUT deep-fetching
    each thread's history (that happens lazily, per thread, in the cache)."""
    if thread_id or only_user:
        raw = fetch_inbox_threads_raw(cl, limit=0)
        if thread_id:
            raw = [t for t in raw if str(t.get("thread_id")) == str(thread_id)]
            if not raw:
                log(f"No thread found with id '{thread_id}'.")
            return raw
        wanted = only_user.lstrip("@").lower()
        raw = [t for t in raw
               if any((u.get("username") or "").lower() == wanted
                      for u in t.get("users", []))]
        if not raw:
            log(f"No thread found with user '{only_user}'.")
        return raw
    return select_threads(cl, limit=limit, order=order, skip_purged=skip_purged)


def purge_resumable(cl, summaries, export_dir, include_others=False,
                    min_delay=2.0, max_delay=6.0, batch_size=0,
                    pause_between_batches=0, max_deletes=0, rebuild_cache=False):
    """
    Cache-backed, resumable purge. Works one thread at a time:
      - builds (or loads from cache) that thread's queue of message IDs,
      - deletes them, flushing progress to disk continuously,
      - honors batching, a per-session delete cap, and mid-thread resume.
    """
    my_user_id = str(cl.user_id)
    purged_ledger = load_purged()

    session_deletes = 0
    batch_count = 0
    total_deleted, total_item_errors = 0, 0

    def flush_pause_if_needed():
        nonlocal batch_count
        if batch_size and batch_count >= batch_size:
            jitter = random.uniform(0.8, 1.2)
            pause = pause_between_batches * jitter
            log(f"  Batch of {batch_size} done. "
                f"Pausing {pause/60:.1f} min before next batch...")
            time.sleep(pause)
            batch_count = 0

    for summary in summaries:
        tid = str(summary.get("thread_id"))
        q = get_queue(cl, summary, my_user_id, include_others, export_dir,
                      rebuild=rebuild_cache)

        # Thread already fully cleared (cache empty)? Record and move on.
        if not q["pending"]:
            mark_purged(tid, purged_ledger)
            render_progress()
            continue

        while q["pending"]:
            if max_deletes and session_deletes >= max_deletes:
                save_queue(q)
                log(f"Session cap of {max_deletes} deletes reached. Progress "
                    f"saved — re-run to continue where you left off.")
                log(f"Session totals: {total_deleted} deleted, "
                    f"{total_item_errors} un-deletable.")
                return

            msg_id = q["pending"][0]
            try:
                cl.direct_message_delete(tid, msg_id)
                q["pending"].pop(0)
                q["done"].append(msg_id)
                total_deleted += 1
                session_deletes += 1
                batch_count += 1
                if total_deleted % 10 == 0:
                    save_queue(q)
                    log(f"  {tid}: {len(q['pending'])} left "
                        f"({total_deleted} deleted this session)")
            except LoginRequired:
                save_queue(q)
                log("Session expired. Progress saved. Re-run login_browser.py, "
                    "then re-run — it resumes from the cache (no re-fetch).")
                return
            except ClientError as e:
                msg = str(e).lower()
                is_rate_limit = any(s in msg for s in (
                    "feedback_required", "wait a few minutes", "rate", "429",
                    "please wait", "try again later",
                ))
                if is_rate_limit:
                    save_queue(q)
                    log(f"  Rate-limit signal on {msg_id}. Backing off 90s. "
                        f"({e})")
                    time.sleep(random.uniform(60, 120))
                    # retry same message once; if it fails again, stop the run
                    try:
                        cl.direct_message_delete(tid, msg_id)
                        q["pending"].pop(0)
                        q["done"].append(msg_id)
                        total_deleted += 1
                        session_deletes += 1
                        batch_count += 1
                    except Exception:
                        save_queue(q)
                        log("Still rate-limited. Stopping — progress saved. "
                            "Wait a few hours and re-run to resume.")
                        return
                else:
                    # un-deletable item (already gone / unsupported type)
                    q["pending"].pop(0)
                    q["done"].append(msg_id)
                    total_item_errors += 1
                continue

            time.sleep(random.uniform(min_delay, max_delay))
            flush_pause_if_needed()

        # Thread fully drained.
        save_queue(q)
        mark_purged(tid, purged_ledger)
        render_progress()
        log(f"Thread {tid} fully purged.")

    log(f"Run complete. {total_deleted} deleted this session, "
        f"{total_item_errors} un-deletable.")
    state = load_progress_state()
    if state.get("total_threads"):
        log(f"Overall: {len(load_purged())}/{state['total_threads']} "
            f"threads purged (see {PROGRESS_VIEW}).")


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
                        help="Only act on the N most recent threads. Works "
                             "with --list-threads, --dry-run, and --unsend. "
                             "Ignored when --only-user/--thread-id is set. "
                             "0 = all.")
    parser.add_argument("--order", choices=["recent", "oldest"],
                        default="recent",
                        help="Thread order for --list-threads and --limit: "
                             "'recent' (default, newest activity first) or "
                             "'oldest' (oldest first).")
    parser.add_argument("--include-purged", action="store_true",
                        help="Don't skip threads already in the purged ledger. "
                             "Use this to re-clear threads that got new "
                             "messages since you last purged them.")
    parser.add_argument("--show-purged", action="store_true",
                        help="Print the purged-threads ledger and exit.")
    parser.add_argument("--min-delay", type=float, default=2.0)
    parser.add_argument("--max-delay", type=float, default=6.0)
    parser.add_argument("--batch-size", type=int, default=0,
                        help="Delete in chunks of this many messages, with a "
                             "long pause between chunks. 0 = no batching "
                             "(continuous). Try 100-200 for large purges.")
    parser.add_argument("--pause-between-batches", type=float, default=900,
                        help="Seconds to pause between batches (default 900 = "
                             "15 min). Only used when --batch-size > 0.")
    parser.add_argument("--max-deletes", type=int, default=0,
                        help="Stop after this many deletes in one run (per "
                             "session cap), saving progress to resume later. "
                             "0 = no cap. Great for spreading huge purges "
                             "across days.")
    parser.add_argument("--rebuild-cache", action="store_true",
                        help="Ignore cached thread queues and re-fetch history. "
                             "Only needed if the account got new messages "
                             "since the cache was built.")
    args = parser.parse_args()

    cl = get_client()

    if args.show_purged:
        purged = load_purged()
        if not purged:
            log("Purged ledger is empty.")
        else:
            log(f"{len(purged)} thread(s) recorded as purged:")
            for tid, when in purged.items():
                print(f"  {tid}  (purged {when})")
        return

    if args.list_threads:
        list_threads(cl, limit=args.limit, order=args.order,
                     skip_purged=args.include_purged is False and args.limit > 0)
        return

    # For dry-run and export-only, use the simple upfront path (shows counts).
    if not args.unsend:
        all_threads, _ = export_threads(
            cl, EXPORT_DIR,
            only_user=args.only_user,
            thread_id=args.thread_id,
            limit=args.limit,
            order=args.order,
            skip_purged=not args.include_purged,
        )
        if not all_threads:
            return
        if args.export_only:
            return
        unsend_messages(
            cl, all_threads,
            dry_run=True,
            include_others=args.include_others_messages,
            min_delay=args.min_delay,
            max_delay=args.max_delay,
            batch_size=args.batch_size,
            pause_between_batches=args.pause_between_batches,
        )
        return

    # Real purge: resumable, cache-backed, lazy per-thread fetch.
    summaries = resolve_targets(
        cl,
        only_user=args.only_user,
        thread_id=args.thread_id,
        limit=args.limit,
        order=args.order,
        skip_purged=not args.include_purged,
    )
    if not summaries:
        return

    purge_resumable(
        cl, summaries, EXPORT_DIR,
        include_others=args.include_others_messages,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        batch_size=args.batch_size,
        pause_between_batches=args.pause_between_batches,
        max_deletes=args.max_deletes,
        rebuild_cache=args.rebuild_cache,
    )


if __name__ == "__main__":
    main()