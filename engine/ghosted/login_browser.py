#!/usr/bin/env python3
"""
login_browser.py — capture an Instagram session via a real browser.

Opens Instagram's own login page in a Playwright browser. You log in there
(including 2FA / checkpoints). Instead of asking you to press ENTER, this
auto-detects a completed login by polling for the session cookies, then
captures and saves the session to session.json.

Designed to be run either:
  - standalone from a terminal (human-readable logs on stderr), or
  - spawned by the desktop app (which reads the final JSON result on stdout).

Protocol when spawned:
  stderr -> human progress lines
  stdout -> exactly one final JSON result:
              {"event":"login_success","username":"...","validated":true}
              {"event":"login_saved","username":null,"validated":false}
              {"event":"login_failed","reason":"..."}
Exit code 0 on success/saved, 1 on failure.
"""

import json
import sys
import time
from urllib.parse import unquote

from playwright.sync_api import sync_playwright

SESSION_FILE = "session.json"
LOGIN_URL = "https://www.instagram.com/accounts/login/"
POLL_TIMEOUT = 300          # seconds to wait for the user to finish logging in
POLL_INTERVAL = 1.0
SETTLE = 2.0                # let cookies settle after login is detected

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _log(msg):
    print(f"[login] {msg}", file=sys.stderr, flush=True)


def _emit(obj):
    print(json.dumps(obj), flush=True)


def capture_session():
    sessionid = ds_user_id = csrftoken = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(
                user_agent=USER_AGENT, viewport={"width": 1280, "height": 800})
            page = context.new_page()
            page.goto(LOGIN_URL)
            _log("Browser opened. Log in to Instagram in that window…")

            deadline = time.time() + POLL_TIMEOUT
            while time.time() < deadline:
                try:
                    cookies = {c["name"]: c["value"] for c in context.cookies()}
                except Exception as e:
                    _log(f"browser closed before login completed ({e})")
                    break
                if cookies.get("sessionid") and cookies.get("ds_user_id"):
                    time.sleep(SETTLE)
                    cookies = {c["name"]: c["value"] for c in context.cookies()}
                    sessionid = cookies.get("sessionid")
                    ds_user_id = cookies.get("ds_user_id")
                    csrftoken = cookies.get("csrftoken")
                    _log("Login detected — capturing session.")
                    break
                time.sleep(POLL_INTERVAL)

            try:
                browser.close()
            except Exception:
                pass
    except Exception as e:
        _emit({"event": "login_failed", "reason": f"browser_error: {e}"})
        sys.exit(1)

    if not sessionid:
        _emit({"event": "login_failed", "reason": "timed_out"})
        _log("Gave up waiting for login.")
        sys.exit(1)

    # Instagram stores sessionid URL-encoded; the mobile API needs it decoded.
    sessionid = unquote(sessionid)
    with open(SESSION_FILE, "w") as f:
        json.dump({
            "sessionid": sessionid,
            "ds_user_id": ds_user_id,
            "csrftoken": csrftoken,
            "captured_at": time.time(),
        }, f, indent=2)
    _log(f"Saved session to {SESSION_FILE}")

    # Validate the captured session with instagrapi and grab the username.
    try:
        from instagrapi import Client
        cl = Client()
        cl.login_by_sessionid(sessionid)
        username = cl.username
        _emit({"event": "login_success", "username": username, "validated": True})
        _log(f"Logged in as @{username}")
    except Exception as e:
        # Session was captured and saved, but the mobile API couldn't validate
        # it right now. Still a usable session for many operations.
        _log(f"Captured session but could not validate: {e}")
        _emit({"event": "login_saved", "username": None, "validated": False})


if __name__ == "__main__":
    capture_session()