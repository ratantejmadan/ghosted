#!/usr/bin/env python3
"""
login_browser.py
----------------
Browser-based login for ig-dm-purge.

Instead of typing your username/password into this tool, this opens a real
browser window pointed at Instagram's genuine login page. You log in there
(handling 2FA, "save login info", checkpoints, etc. exactly as normal), and
once you're in, the script captures your session cookie and saves it locally.

This is the same approach paid tools use: your credentials only ever go to
Instagram itself, never to this script.

The saved session (session.json) is then used by purge_dms.py.

Usage:
    python login_browser.py

Requires a one-time browser install:
    playwright install chromium
"""

import json
import sys
import time
from urllib.parse import unquote

from playwright.sync_api import sync_playwright

SESSION_FILE = "session.json"
LOGIN_URL = "https://www.instagram.com/accounts/login/"
HOME_URL = "https://www.instagram.com/"


def capture_session():
    with sync_playwright() as p:
        # headless=False so you can actually see and use the login page
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        page.goto(LOGIN_URL)

        print("=" * 60)
        print(" A browser window has opened.")
        print(" 1. Log into Instagram normally (incl. 2FA if prompted).")
        print(" 2. Wait until you see your normal Instagram home feed.")
        print(" 3. Come back here and press ENTER.")
        print("=" * 60)
        input(" Press ENTER once you're fully logged in... ")

        # Pull cookies from the authenticated browser context
        cookies = context.cookies()
        cookie_map = {c["name"]: c["value"] for c in cookies}

        sessionid = cookie_map.get("sessionid")
        ds_user_id = cookie_map.get("ds_user_id")
        csrftoken = cookie_map.get("csrftoken")

        browser.close()

        if not sessionid:
            print("\nERROR: couldn't find a sessionid cookie. Are you sure "
                  "you finished logging in before pressing ENTER?")
            sys.exit(1)

        # Instagram stores sessionid URL-encoded in the cookie (colons as
        # %3A). The private mobile API that instagrapi talks to needs the
        # decoded form, or it returns login_required. Decode it now.
        sessionid = unquote(sessionid)

        session_data = {
            "sessionid": sessionid,
            "ds_user_id": ds_user_id,
            "csrftoken": csrftoken,
            "captured_at": time.time(),
        }

        with open(SESSION_FILE, "w") as f:
            json.dump(session_data, f, indent=2)

        print(f"\nSession captured and saved to {SESSION_FILE}")
        print("You can now run: python purge_dms.py --dry-run")


if __name__ == "__main__":
    capture_session()