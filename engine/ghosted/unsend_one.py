#!/usr/bin/env python3
"""
unsend_one.py
-------------
Test harness: unsend a SINGLE message by ID, to verify deletion works
before running a full purge.

Reuses the session captured by login_browser.py (session.json).

Usage:
    python unsend_one.py --thread-id THREAD_ID --message-id MESSAGE_ID

Example (the most recent message from your export):
    python unsend_one.py \\
        --thread-id 34028XXXXXXXXXXXX \\
        --message-id 32410XXXXXXXXXXX

It will:
  1. Authenticate with your saved session.
  2. Show you the thread + message ID it's about to unsend.
  3. Ask for confirmation (type 'yes').
  4. Unsend that one message and report success/failure.
"""

import argparse
import json
import os
import sys
from urllib.parse import unquote

from instagrapi import Client

SESSION_FILE = "session.json"


def get_client():
    if not os.path.exists(SESSION_FILE):
        print("ERROR: no session.json found. Run login_browser.py first.")
        sys.exit(1)

    with open(SESSION_FILE) as f:
        session = json.load(f)

    sessionid = unquote(session.get("sessionid", ""))
    if not sessionid:
        print("ERROR: no sessionid in session.json. Re-run login_browser.py.")
        sys.exit(1)

    cl = Client()
    try:
        cl.login_by_sessionid(sessionid)
        print(f"Authenticated as @{cl.username} (user id {cl.user_id}).")
    except Exception as e:
        print(f"ERROR: session invalid or expired ({e}).")
        sys.exit(1)
    return cl


def main():
    parser = argparse.ArgumentParser(description="Unsend a single message (test).")
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--message-id", required=True)
    parser.add_argument("--yes", action="store_true",
                        help="Skip the confirmation prompt.")
    args = parser.parse_args()

    cl = get_client()

    print("\nAbout to UNSEND this message:")
    print(f"  thread_id  = {args.thread_id}")
    print(f"  message_id = {args.message_id}")
    print("\nThis permanently removes it from the conversation (for everyone).")

    if not args.yes:
        confirm = input("Type 'yes' to proceed: ").strip().lower()
        if confirm != "yes":
            print("Aborted. Nothing was deleted.")
            return

    try:
        ok = cl.direct_message_delete(args.thread_id, args.message_id)
        if ok:
            print("\nSUCCESS — message unsent. Check the thread in the app to confirm.")
        else:
            print("\nThe API returned a non-ok status. Message may not have been removed.")
    except Exception as e:
        print(f"\nFAILED to unsend: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
