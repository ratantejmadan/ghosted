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
        if not self.ensure_client():
            return
        rebuild = bool(params.get("rebuild_cache", False))
        my = str(self.cl.user_id)
        emit("index_start")
        try:
            raw = core.fetch_inbox_threads_raw(self.cl, limit=0)
        except Exception as e:
            emit("error", where="index", message=str(e))
            return
        core.set_total_threads(len(raw))
        total = len(raw)
        ledger = core.load_purged()
        for i, t in enumerate(raw, 1):
            if self.control.stop.is_set():
                emit("index_stopped", done=i - 1, total=total)
                return
            tid = str(t.get("thread_id"))
            try:
                q = core.get_queue(self.cl, t, my, False, EXPORT_DIR,
                                   rebuild=rebuild)
            except Exception as e:
                emit("error", where="index_thread", thread_id=tid,
                     message=str(e))
                continue
            name = q.get("thread_title") or ", ".join(q.get("users", [])) or tid
            emit("thread_indexed",
                 thread_id=tid, name=name,
                 total=q.get("total_messages", 0),
                 mine=q.get("total", len(q.get("pending", []))),
                 purged=(tid in ledger) or (len(q.get("pending", [])) == 0),
                 done=i, total_threads=total)
        emit("index_complete", threads=total)

    # -- list from cache (fast path, no network) --
    def do_list(self):
        ledger = core.load_purged()
        rows = []
        if os.path.isdir(core.CACHE_DIR):
            for fn in os.listdir(core.CACHE_DIR):
                if not (fn.startswith("queue_") and fn.endswith(".json")):
                    continue
                try:
                    with open(os.path.join(core.CACHE_DIR, fn)) as f:
                        q = json.load(f)
                except Exception:
                    continue
                tid = str(q.get("thread_id"))
                rows.append({
                    "thread_id": tid,
                    "name": q.get("thread_title") or ", ".join(q.get("users", [])) or tid,
                    "total": q.get("total_messages", 0),
                    "mine": q.get("total", len(q.get("pending", []))),
                    "remaining": len(q.get("pending", [])),
                    "purged": (tid in ledger) or (len(q.get("pending", [])) == 0),
                })
        rows.sort(key=lambda r: r["total"], reverse=True)
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
        rebuild = bool(params.get("rebuild_cache", False))

        want = params.get("thread_ids", "all")

        # Resolve target thread summaries (needed to build any uncached queue).
        try:
            inbox = core.fetch_inbox_threads_raw(self.cl, limit=0)
        except Exception as e:
            emit("error", where="purge", message=str(e))
            return
        by_id = {str(t.get("thread_id")): t for t in inbox}
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

        emit("job_start", threads=len(queues), total_messages=grand_total)

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

                msg_id = q["pending"][0]
                try:
                    self.cl.direct_message_delete(tid, msg_id)
                    q["pending"].pop(0)
                    q["done"].append(msg_id)
                    deleted += 1
                    batch_count += 1
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
        elif cmd == "shutdown":
            emit("bye"); return False
        else:
            emit("error", message=f"unknown command: {cmd}")
        return True


def main():
    eng = Engine()
    emit("ready", version=getattr(core, "__version__", "0"))
    for line in sys.stdin:
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