#!/usr/bin/env python3
"""
ghosted.engine — the sidecar the GUI (e.g. a Tauri Rust host) drives.

Protocol: newline-delimited JSON.
  - IN  (stdin) : one command object per line, e.g. {"cmd":"list_threads"}
  - OUT (stdout): one event object per line,   e.g. {"event":"progress",...}

stdout carries ONLY event JSON, so it can be parsed line-by-line by the host.
All human/debug logging is redirected to stderr.

Long-running work (indexing, purging) runs on a worker thread so the main
loop can keep reading stdin — that's what makes pause/resume/stop responsive
while a job is in flight.

Commands
--------
{"cmd":"login_status"}                 -> {"event":"login", ...}
{"cmd":"index", "params":{...}}        -> stream index_* events
{"cmd":"list_threads"}                 -> {"event":"threads", ...} (from cache)
{"cmd":"purge", "params":{...}}        -> stream job_*/progress/backoff events
{"cmd":"pause"} / "resume" / "stop"    -> control a running job
{"cmd":"progress"}                     -> {"event":"summary", ...}
{"cmd":"shutdown"}                     -> exit

purge params: {"thread_ids":[...] | "all", "include_others":bool,
               "min_delay":f,"max_delay":f,"batch_size":i,
               "pause_between_batches":f,"max_deletes":i,"rebuild_cache":bool}
"""

import json
import os
import random
import sys
import threading
import time

# Import the proven core (fetch/cache/ledger/progress live here).
try:
    from . import purge_dms as core
except ImportError:  # allow running as a plain script
    import purge_dms as core

EXPORT_DIR = "export"

# --- stdout is the event channel; send all core logging to stderr ----------
_out_lock = threading.Lock()


def emit(event, **data):
    line = json.dumps({"event": event, **data})
    with _out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _log_stderr(msg):
    sys.stderr.write(f"[engine] {msg}\n")
    sys.stderr.flush()


core.log = _log_stderr  # redirect every core.log() call away from stdout


# --- lightweight thread-list index (summaries only, no messages) -----------
# Saved by do_index so scan/purge can resolve thread names/participants
# locally, without re-hitting the inbox endpoint. Message histories are only
# fetched later, per-thread, on an explicit Deep Scan or purge.
THREADS_INDEX = "threads_index.json"


def save_threads_index(raw_summaries):
    """Persist just the fields scan/purge need, in a build_queue-compatible
    shape (users as [{'username': ...}])."""
    data = []
    for t in raw_summaries:
        data.append({
            "thread_id": str(t.get("thread_id")),
            "thread_title": t.get("thread_title"),
            "is_group": bool(t.get("is_group", False)),
            "users": [{"username": u.get("username")}
                      for u in t.get("users", [])],
        })
    with open(THREADS_INDEX, "w") as f:
        json.dump(data, f, indent=2)


def load_threads_index():
    if os.path.exists(THREADS_INDEX):
        try:
            with open(THREADS_INDEX) as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _summary_name(summary, tid):
    users = [u.get("username") for u in summary.get("users", [])]
    return summary.get("thread_title") or ", ".join(
        u for u in users if u) or tid


# --- scheduled cleanups (persisted; fired by the app while it's open) -------
SCHEDULES_FILE = "schedules.json"


def load_schedules():
    if os.path.exists(SCHEDULES_FILE):
        try:
            with open(SCHEDULES_FILE) as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_schedules(scheds):
    try:
        with open(SCHEDULES_FILE, "w") as f:
            json.dump(scheds, f, indent=2)
    except Exception:
        pass


# --- user preferences: application defaults + custom pacing presets --------
PREFS_FILE = "prefs.json"


def load_prefs():
    if os.path.exists(PREFS_FILE):
        try:
            with open(PREFS_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_prefs(prefs):
    try:
        with open(PREFS_FILE, "w") as f:
            json.dump(prefs, f, indent=2)
    except Exception:
        pass


# --- hourly rate cap (hard ceiling, persisted across restarts) -------------
# Instagram throttles at roughly 200 actions/hour per account. We enforce a
# rolling-window cap so we NEVER exceed it, and default to a safer 150/hour.
# Timestamps are persisted so the cap holds even if the app is restarted
# mid-hour (the server-side limit doesn't reset when we do).
HARD_CAP_PER_HOUR = 200      # absolute ceiling — no preset may exceed this
DEFAULT_PER_HOUR = 150       # recommended operating rate
RATE_WINDOW_SEC = 3600
RATE_LOG = "rate_log.json"


class RateLimiter:
    """Never allow more than `max_actions` within any `window` seconds."""

    def __init__(self, max_actions, window, path):
        self.max = max(1, int(max_actions))
        self.window = window
        self.path = path
        self.times = self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                return [float(x) for x in json.load(f)]
        except Exception:
            return []

    def _save(self):
        try:
            with open(self.path, "w") as f:
                json.dump(self.times, f)
        except Exception:
            pass

    def _prune(self, now):
        cutoff = now - self.window
        self.times = [t for t in self.times if t > cutoff]

    def wait_seconds(self, now=None):
        """How long to wait before the next action is allowed (0 = go now)."""
        now = time.time() if now is None else now
        self._prune(now)
        if len(self.times) < self.max:
            return 0.0
        oldest = min(self.times)
        return max(0.0, (oldest + self.window) - now)

    def record(self, now=None):
        now = time.time() if now is None else now
        self.times.append(now)
        self._prune(now)
        self._save()

    def count_in_window(self, now=None):
        now = time.time() if now is None else now
        self._prune(now)
        return len(self.times)


# --- job control -----------------------------------------------------------
class Control:
    def __init__(self):
        self.pause = threading.Event()
        self.stop = threading.Event()

    def reset(self):
        self.pause.clear()
        self.stop.clear()


class Engine:
    def __init__(self):
        self.cl = None
        self.control = Control()
        self.worker = None

    # -- session --
    def ensure_client(self):
        if self.cl is not None:
            return True
        sid = None
        if os.path.exists(core.SESSION_FILE):
            try:
                with open(core.SESSION_FILE) as f:
                    sid = json.load(f).get("sessionid")
            except Exception:
                sid = None
        if not sid:
            emit("login", logged_in=False, reason="no_session")
            return False
        try:
            from urllib.parse import unquote
            cl = core.Client()
            cl.login_by_sessionid(unquote(sid))
            self.cl = cl
            emit("login", logged_in=True, username=cl.username,
                 user_id=str(cl.user_id))
            return True
        except Exception as e:
            emit("login", logged_in=False, reason=str(e))
            return False

    def busy(self):
        return self.worker is not None and self.worker.is_alive()

    # -- indexing: deep-fetch every thread once, build caches + counts --
    def do_index(self, params):
        """LIGHTWEIGHT index: fetch the thread list only (name, id, group),
        no message histories. Fast, so the Threads tab fills immediately.
        Counts show only for threads already Deep-Scanned (from cache)."""
        if not self.ensure_client():
            return
        limit = int(params.get("limit", 0))          # 0 = whole account
        emit("index_start", limit=limit)
        try:
            raw = core.fetch_inbox_threads_raw(self.cl, limit=limit)
        except Exception as e:
            emit("error", where="index", message=str(e))
            return
        if not limit:
            core.set_total_threads(len(raw))
        # Persist summaries so scan/purge resolve names locally (no re-fetch).
        save_threads_index(raw)
        total = len(raw)
        ledger = core.load_purged()
        for i, t in enumerate(raw, 1):
            if self.control.stop.is_set():
                emit("index_stopped", done=i - 1, total=total)
                return
            tid = str(t.get("thread_id"))
            users = [u.get("username") for u in t.get("users", [])]
            name = t.get("thread_title") or ", ".join(
                u for u in users if u) or tid
            cached = core.load_queue(tid)   # counts only if already scanned
            if cached:
                remaining = len(cached.get("pending", []))
                emit("thread_indexed", thread_id=tid, name=name,
                     is_group=bool(t.get("is_group", False)),
                     total=cached.get("total_messages"),
                     mine=cached.get("total"),
                     remaining=remaining, scanned=True,
                     purged=(tid in ledger) or (remaining == 0),
                     done=i, total_threads=total)
            else:
                emit("thread_indexed", thread_id=tid, name=name,
                     is_group=bool(t.get("is_group", False)),
                     total=None, mine=None, remaining=None, scanned=False,
                     purged=(tid in ledger),
                     done=i, total_threads=total)
        emit("index_complete", threads=total)

    # -- Deep Scan: fetch message history for selected threads on demand --
    def do_scan(self, params):
        """For each selected thread, deep-fetch its history and build the
        queue cache — filling in the total/your-message counts. This is the
        expensive per-thread step, now explicit and user-initiated."""
        if not self.ensure_client():
            return
        my = str(self.cl.user_id)
        include_others = bool(params.get("include_others", False))
        rebuild = bool(params.get("rebuild_cache", False))
        want = [str(x) for x in params.get("thread_ids", [])]
        index = {s["thread_id"]: s for s in load_threads_index()}
        ledger = core.load_purged()
        emit("scan_start", threads=len(want))
        for i, tid in enumerate(want, 1):
            if self.control.stop.is_set():
                emit("scan_stopped", done=i - 1, total=len(want))
                return
            summary = index.get(tid) or {"thread_id": tid, "users": [],
                                         "thread_title": None}
            emit("scan_progress", thread_id=tid,
                 name=_summary_name(summary, tid), done=i, total=len(want))
            try:
                q = core.get_queue(self.cl, summary, my, include_others,
                                   EXPORT_DIR, rebuild=rebuild)
            except Exception as e:
                emit("error", where="scan_thread", thread_id=tid,
                     message=str(e))
                continue
            remaining = len(q.get("pending", []))
            emit("thread_scanned", thread_id=tid,
                 name=q.get("thread_title") or ", ".join(q.get("users", [])) or tid,
                 total=q.get("total_messages", 0),
                 mine=q.get("total", remaining),
                 remaining=remaining,
                 purged=(tid in ledger) or (remaining == 0))
        emit("scan_complete", threads=len(want))

    # -- list from disk (fast path, no network): full index + cached counts --
    def do_list(self):
        """What the app loads on startup. Returns the full thread list from the
        persisted lightweight index (threads_index.json), enriched with counts
        for any threads that have been Deep-Scanned. No network, no re-index —
        so the whole list survives app restarts, not just scanned threads."""
        ledger = core.load_purged()
        index = load_threads_index()
        rows = []
        for s in index:
            tid = str(s.get("thread_id"))
            name = _summary_name(s, tid)
            cached = core.load_queue(tid)     # counts only if already scanned
            if cached:
                remaining = len(cached.get("pending", []))
                rows.append({
                    "thread_id": tid, "name": name,
                    "is_group": bool(s.get("is_group", False)),
                    "total": cached.get("total_messages"),
                    "mine": cached.get("total"),
                    "remaining": remaining, "scanned": True,
                    "purged": (tid in ledger) or (remaining == 0),
                })
            else:
                rows.append({
                    "thread_id": tid, "name": name,
                    "is_group": bool(s.get("is_group", False)),
                    "total": None, "mine": None, "remaining": None,
                    "scanned": False,
                    "purged": (tid in ledger),
                })
        # Preserve the index's native newest-first order (the UI's sort toggle
        # relies on it); do NOT re-sort here.
        emit("threads", threads=rows)

    # -- purge: resumable, controllable, event-streaming --
    def do_purge(self, params):
        if not self.ensure_client():
            return
        my = str(self.cl.user_id)
        include_others = bool(params.get("include_others", False))
        min_delay = float(params.get("min_delay", 8))
        max_delay = float(params.get("max_delay", 20))
        batch_size = int(params.get("batch_size", 0))
        pause_between = float(params.get("pause_between_batches", 0))
        max_deletes = int(params.get("max_deletes", 0))
        # Hard hourly cap — clamp to the absolute ceiling so nothing (not even
        # a hand-edited setting) can exceed Instagram's limit.
        max_per_hour = int(params.get("max_per_hour", DEFAULT_PER_HOUR))
        max_per_hour = max(1, min(max_per_hour, HARD_CAP_PER_HOUR))
        limiter = RateLimiter(max_per_hour, RATE_WINDOW_SEC, RATE_LOG)
        # By default purge consumes the existing cache (from a prior Deep Scan
        # or earlier purge), or builds a not-yet-scanned thread's queue ONCE on
        # the fly — it does NOT re-crawl cached threads. Only if the user
        # explicitly ticks "Rebuild cache" do we re-fetch history first.
        rebuild = bool(params.get("rebuild_cache", False))

        want = params.get("thread_ids", "all")

        # Resolve target summaries from the LOCAL index (saved by do_index) —
        # no inbox re-fetch. Cached threads don't need a summary at all; any
        # uncached target falls back to a stub and build_queue fetches by id.
        by_id = {s["thread_id"]: s for s in load_threads_index()}
        if want == "all":
            targets = list(by_id.keys())
        else:
            targets = [str(x) for x in want]

        # Compute grand total for progress denominator.
        ledger = core.load_purged()
        grand_total, queues = 0, {}
        for tid in targets:
            summary = by_id.get(tid, {"thread_id": tid, "users": [],
                                      "thread_title": None})
            try:
                q = core.get_queue(self.cl, summary, my, include_others,
                                   EXPORT_DIR, rebuild=rebuild)
            except Exception as e:
                emit("error", where="purge_build", thread_id=tid, message=str(e))
                continue
            queues[tid] = q
            grand_total += len(q["pending"])

        emit("job_start", threads=len(queues), total_messages=grand_total,
             max_per_hour=max_per_hour,
             already_this_hour=limiter.count_in_window())

        deleted = 0
        item_errors = 0
        batch_count = 0
        t0 = time.time()

        def progress(tid, q):
            elapsed = max(time.time() - t0, 0.5)
            rate = deleted / elapsed
            remain = (grand_total - deleted) / rate if rate > 0 else 0
            emit("progress",
                 deleted=deleted, total=grand_total,
                 thread_id=tid,
                 thread_name=q.get("thread_title") or ", ".join(q.get("users", [])) or tid,
                 thread_done=len(q["done"]), thread_total=q.get("total", 0),
                 rate=round(rate, 2), eta_sec=int(remain),
                 batch=(batch_count // batch_size + 1) if batch_size else None)

        for tid, q in queues.items():
            if not q["pending"]:
                core.mark_purged(tid, ledger)
                core.render_progress()
                emit("thread_done", thread_id=tid)
                continue

            # Show this thread immediately (before the first slow delete) so
            # the UI updates the moment work on it begins.
            progress(tid, q)

            while q["pending"]:
                if self.control.stop.is_set():
                    core.save_queue(q)
                    emit("job_stopped", deleted=deleted, item_errors=item_errors)
                    return
                # user pause: block here until resumed or stopped
                if self.control.pause.is_set():
                    emit("paused", paused=True)
                    while self.control.pause.is_set():
                        if self.control.stop.is_set():
                            core.save_queue(q)
                            emit("job_stopped", deleted=deleted)
                            return
                        time.sleep(0.2)
                    emit("paused", paused=False)

                if max_deletes and deleted >= max_deletes:
                    core.save_queue(q)
                    emit("job_capped", deleted=deleted, cap=max_deletes)
                    return

                # --- hard hourly cap: wait if we'd exceed max_per_hour ---
                wait = limiter.wait_seconds()
                if wait > 0:
                    core.save_queue(q)
                    emit("rate_wait", seconds=int(wait) + 1,
                         per_hour=max_per_hour,
                         count=limiter.count_in_window())
                    deadline = time.time() + wait
                    while time.time() < deadline:
                        if self.control.stop.is_set():
                            core.save_queue(q)
                            emit("job_stopped", deleted=deleted,
                                 item_errors=item_errors)
                            return
                        time.sleep(0.5)
                    emit("rate_resume", per_hour=max_per_hour)

                msg_id = q["pending"][0]
                try:
                    self.cl.direct_message_delete(tid, msg_id)
                    q["pending"].pop(0)
                    q["done"].append(msg_id)
                    deleted += 1
                    batch_count += 1
                    limiter.record()   # count this delete toward the hourly cap
                    # Emit progress on EVERY delete so the UI updates live;
                    # persist the queue less often (every 5) to spare disk I/O.
                    if deleted % 5 == 0:
                        core.save_queue(q)
                    progress(tid, q)
                except core.LoginRequired:
                    core.save_queue(q)
                    emit("session_expired", deleted=deleted)
                    return
                except core.ClientError as e:
                    m = str(e).lower()
                    rate_limited = any(s in m for s in (
                        "feedback_required", "wait a few minutes", "rate",
                        "429", "please wait", "try again later"))
                    if rate_limited:
                        core.save_queue(q)
                        emit("backoff", seconds=90, message=str(e))
                        time.sleep(random.uniform(60, 120))
                    else:
                        q["pending"].pop(0)
                        q["done"].append(msg_id)
                        item_errors += 1
                    continue

                time.sleep(random.uniform(min_delay, max_delay))
                if batch_size and batch_count >= batch_size:
                    core.save_queue(q)
                    jitter = random.uniform(0.8, 1.2)
                    pause = pause_between * jitter
                    emit("batch_pause", seconds=int(pause))
                    time.sleep(pause)
                    batch_count = 0

            core.save_queue(q)
            core.mark_purged(tid, ledger)
            core.render_progress()
            emit("thread_done", thread_id=tid)
            progress(tid, q)

        emit("job_done", deleted=deleted, item_errors=item_errors)

    # -- progress snapshot --
    def do_summary(self):
        state = core.load_progress_state()
        purged = core.load_purged()
        emit("summary",
             total_threads=state.get("total_threads"),
             purged_threads=len(purged),
             counted_at=state.get("total_counted_at"))

    # -- scheduled cleanups (persisted; the app fires them while open) --
    def do_schedules_list(self):
        emit("schedules", schedules=load_schedules())

    def do_schedules_save(self, params):
        scheds = params.get("schedules", [])
        save_schedules(scheds)
        emit("schedules", schedules=scheds)

    # -- user preferences (application defaults + custom presets) --
    def do_prefs_load(self):
        emit("prefs", prefs=load_prefs())

    def do_prefs_save(self, params):
        prefs = params.get("prefs", {})
        save_prefs(prefs)
        emit("prefs", prefs=prefs)

    # -- dispatch --
    def start_worker(self, fn, *a):
        if self.busy():
            emit("error", message="A job is already running.")
            return
        self.control.reset()
        self.worker = threading.Thread(target=fn, args=a, daemon=True)
        self.worker.start()

    def handle(self, msg):
        cmd = msg.get("cmd")
        params = msg.get("params", {}) or {}
        if cmd == "login_status":
            self.ensure_client()
        elif cmd == "index":
            self.start_worker(self.do_index, params)
        elif cmd == "scan":
            self.start_worker(self.do_scan, params)
        elif cmd == "list_threads":
            self.do_list()
        elif cmd == "purge":
            self.start_worker(self.do_purge, params)
        elif cmd == "pause":
            self.control.pause.set(); emit("ack", cmd="pause")
        elif cmd == "resume":
            self.control.pause.clear(); emit("ack", cmd="resume")
        elif cmd == "stop":
            self.control.stop.set(); emit("ack", cmd="stop")
        elif cmd == "progress":
            self.do_summary()
        elif cmd == "schedules_list":
            self.do_schedules_list()
        elif cmd == "schedules_save":
            self.do_schedules_save(params)
        elif cmd == "prefs_load":
            self.do_prefs_load()
        elif cmd == "prefs_save":
            self.do_prefs_save(params)
        elif cmd == "shutdown":
            emit("bye"); return False
        else:
            emit("error", message=f"unknown command: {cmd}")
        return True


def main():
    eng = Engine()
    emit("ready", version=getattr(core, "__version__", "0"))
    # readline (not `for line in sys.stdin`) so control commands like pause/
    # stop are seen immediately during a running job, without read-ahead buffering.
    while True:
        line = sys.stdin.readline()
        if not line:          # EOF: stdin closed -> exit
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            emit("error", message=f"bad json: {e}")
            continue
        try:
            if eng.handle(msg) is False:
                break
        except Exception as e:
            emit("error", message=str(e))


if __name__ == "__main__":
    main()