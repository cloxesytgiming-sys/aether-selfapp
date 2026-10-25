"""Starts, stops and monitors one bot worker process per account."""
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import db

BASE = Path(__file__).resolve().parent
LOG_DIR = BASE / "bot_logs"
PROCS = {}          # account_id -> Popen started by this dashboard
FRESH_SECONDS = 150  # a worker started by an earlier dashboard run counts as alive if its heartbeat is this fresh


def _age_seconds(iso):
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (TypeError, ValueError):
        return 10 ** 9


def _pid_alive(pid):
    # os.kill(pid, 0) would TERMINATE the process on Windows, so only probe on POSIX.
    if os.name != "posix" or not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def log_path(account_id):
    return LOG_DIR / f"account_{account_id}.log"


def _mark_dead(account_id, returncode):
    status = db.get_status(account_id) or {}
    error = status.get("last_error") or ""
    if not error and returncode not in (0, None, -15):
        error = f"Process exited with code {returncode} - check the console below."
    db.set_status(account_id, online=0, pid=None, last_error=error)


def is_running(account_id):
    proc = PROCS.get(account_id)
    if proc is not None:
        if proc.poll() is None:
            return True
        PROCS.pop(account_id, None)
        _mark_dead(account_id, proc.returncode)
        return False
    status = db.get_status(account_id)
    if status and status.get("pid") and status.get("online"):
        if _pid_alive(status["pid"]) and _age_seconds(status.get("updated_at")) < FRESH_SECONDS:
            return True
        db.set_status(account_id, online=0, pid=None)
    return False


def start(account_id):
    if not db.get_account(account_id):
        raise ValueError("Unknown account")
    if is_running(account_id):
        return False
    LOG_DIR.mkdir(exist_ok=True)
    path = log_path(account_id)
    if path.exists() and path.stat().st_size > 1_000_000:
        path.replace(path.with_suffix(".old"))
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["AETHER_DB_PATH"] = db.DB_PATH
    kwargs = {"start_new_session": True} if os.name == "posix" else {}
    with open(path, "ab") as logf:
        logf.write(f"\n--- starting {db.now_iso()} ---\n".encode())
        logf.flush()
        proc = subprocess.Popen(
            [sys.executable, "-u", str(BASE / "selfbot.py"), "--account", str(account_id)],
            cwd=str(BASE), stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, **kwargs)
    PROCS[account_id] = proc
    db.set_status(account_id, online=0, pid=proc.pid, last_error="", started_at=db.now_iso())
    return True


def stop(account_id):
    proc = PROCS.pop(account_id, None)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(8)
        except subprocess.TimeoutExpired:
            proc.kill()
    else:
        status = db.get_status(account_id) or {}
        pid = status.get("pid")
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(40):
                    if not _pid_alive(pid):
                        break
                    time.sleep(0.2)
                else:
                    os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    db.set_status(account_id, online=0, pid=None)


def console_tail(account_id, max_bytes=8000):
    path = log_path(account_id)
    if not path.exists():
        return ""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        return f.read().decode("utf-8", "replace")


def autostart():
    for acct in db.list_accounts():
        if acct.get("auto_start"):
            try:
                start(acct["id"])
            except Exception as exc:
                print(f"Could not auto-start account {acct['id']}: {exc}")
