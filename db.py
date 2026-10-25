"""Shared SQLite layer used by the dashboard (Flask) and the bot workers."""
import copy
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:  # optional: load variables from a .env file
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE = Path(__file__).resolve().parent
DB_PATH = os.path.abspath(os.getenv("AETHER_DB_PATH") or str(BASE / "aether_logs.sqlite3"))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    return db


@contextmanager
def conn():
    db = connect()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ------------------------------------------------------------------ schema
def init_db():
    with conn() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            category TEXT NOT NULL,
            message TEXT NOT NULL,
            level TEXT NOT NULL,
            guild_name TEXT, guild_id TEXT,
            actor TEXT, actor_id TEXT,
            role_name TEXT, role_id TEXT
        )""")
        cols = [r["name"] for r in db.execute("PRAGMA table_info(logs)")]
        if "account_id" not in cols:  # migrate logs from the single-account version
            db.execute("ALTER TABLE logs ADD COLUMN account_id INTEGER")
        db.execute("CREATE INDEX IF NOT EXISTS idx_logs_acc ON logs(account_id, id)")

        db.execute("""CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            token TEXT NOT NULL,
            user_id TEXT, username TEXT, display_name TEXT, avatar_url TEXT,
            auto_start INTEGER NOT NULL DEFAULT 0,
            created_at TEXT
        )""")
        acols = [r["name"] for r in db.execute("PRAGMA table_info(accounts)")]
        if "owner" not in acols:  # 'admin' = master password, 'key:<id>' = a license key holder
            db.execute("ALTER TABLE accounts ADD COLUMN owner TEXT NOT NULL DEFAULT 'admin'")
        db.execute("""CREATE TABLE IF NOT EXISTS account_status (
            account_id INTEGER PRIMARY KEY,
            online INTEGER NOT NULL DEFAULT 0,
            pid INTEGER,
            username TEXT,
            guild_count INTEGER NOT NULL DEFAULT 0,
            user_count INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            updated_at TEXT,
            last_error TEXT NOT NULL DEFAULT ''
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS settings (
            account_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated REAL NOT NULL,
            PRIMARY KEY (account_id, key)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            result TEXT,
            created_at REAL NOT NULL,
            finished_at REAL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_pending ON jobs(account_id, status)")
        db.execute("""CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            author TEXT, author_id TEXT, avatar_url TEXT,
            guild_name TEXT, channel_name TEXT, channel_id TEXT, message_id TEXT,
            jump_url TEXT, preview TEXT,
            created_at TEXT NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_notif_acc ON notifications(account_id, id)")
        db.execute("""CREATE TABLE IF NOT EXISTS sniper_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            ref TEXT, guild_name TEXT, channel_name TEXT, author TEXT,
            result TEXT NOT NULL, detail TEXT, latency_ms INTEGER,
            created_at TEXT NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_sniper_acc ON sniper_events(account_id, kind, id)")
        db.execute("""CREATE TABLE IF NOT EXISTS stats (
            account_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (account_id, key)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS quests_cache (
            account_id INTEGER PRIMARY KEY,
            data TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS quest_runs (
            account_id INTEGER NOT NULL,
            quest_id TEXT NOT NULL,
            status TEXT NOT NULL,
            progress REAL NOT NULL DEFAULT 0,
            target REAL NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            updated REAL NOT NULL,
            PRIMARY KEY (account_id, quest_id)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS snapshots (
            account_id INTEGER PRIMARY KEY,
            data TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")


# ------------------------------------------------------------------ accounts
def mask_token(token):
    token = token or ""
    return token[:5] + "•" * 8 + token[-4:] if len(token) > 14 else "••••••••"


def _public(row):
    d = dict(row)
    d["token_masked"] = mask_token(d.pop("token", ""))
    return d


_ACCOUNT_SELECT = """SELECT a.*, s.online AS online, s.pid AS pid, s.updated_at AS status_updated,
    s.last_error AS last_error, s.started_at AS started_at, s.guild_count AS guild_count
    FROM accounts a LEFT JOIN account_status s ON s.account_id = a.id"""


def list_accounts(owner=None):
    """owner=None returns every account (admin); otherwise only that owner's."""
    with conn() as db:
        if owner is None:
            rows = db.execute(_ACCOUNT_SELECT + " ORDER BY a.id").fetchall()
        else:
            rows = db.execute(_ACCOUNT_SELECT + " WHERE a.owner=? ORDER BY a.id", (owner,)).fetchall()
    return [_public(r) for r in rows]


def get_account(account_id):
    with conn() as db:
        row = db.execute(_ACCOUNT_SELECT + " WHERE a.id=?", (account_id,)).fetchone()
    return _public(row) if row else None


def get_token(account_id):
    with conn() as db:
        row = db.execute("SELECT token FROM accounts WHERE id=?", (account_id,)).fetchone()
    return row["token"] if row else None


def add_account(label, token, owner="admin"):
    with conn() as db:
        cur = db.execute("INSERT INTO accounts(label, token, created_at, owner) VALUES(?,?,?,?)",
                         (label, token, now_iso(), owner))
        return cur.lastrowid


_ACCOUNT_FIELDS = {"label", "token", "auto_start", "user_id", "username", "display_name", "avatar_url"}


def update_account(account_id, **fields):
    fields = {k: v for k, v in fields.items() if k in _ACCOUNT_FIELDS}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with conn() as db:
        db.execute(f"UPDATE accounts SET {sets} WHERE id=?", (*fields.values(), account_id))


def delete_account(account_id):
    with conn() as db:
        for table in ("logs", "account_status", "settings", "jobs", "notifications", "sniper_events", "stats",
                      "snapshots", "quests_cache", "quest_runs"):
            db.execute(f"DELETE FROM {table} WHERE account_id=?", (account_id,))
        db.execute("DELETE FROM accounts WHERE id=?", (account_id,))


# ------------------------------------------------------------------ status
_STATUS_FIELDS = {"online", "pid", "username", "guild_count", "user_count", "started_at", "last_error"}


def get_status(account_id):
    with conn() as db:
        row = db.execute("SELECT * FROM account_status WHERE account_id=?", (account_id,)).fetchone()
    return dict(row) if row else None


def set_status(account_id, **fields):
    fields = {k: v for k, v in fields.items() if k in _STATUS_FIELDS}
    fields["updated_at"] = now_iso()
    sets = ", ".join(f"{k}=?" for k in fields)
    with conn() as db:
        db.execute("INSERT OR IGNORE INTO account_status(account_id) VALUES(?)", (account_id,))
        db.execute(f"UPDATE account_status SET {sets} WHERE account_id=?", (*fields.values(), account_id))


# ------------------------------------------------------------------ logs
def add_log(category, message, level="info", guild_name="", guild_id="", actor="", actor_id="",
            role_name="", role_id="", account_id=None):
    try:
        with conn() as db:
            db.execute("""INSERT INTO logs(timestamp,category,message,level,guild_name,guild_id,actor,actor_id,
                          role_name,role_id,account_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (now_iso(), category, message, level, guild_name, guild_id, actor, actor_id,
                        role_name, role_id, account_id))
            if account_id is not None:  # keep each account's log history bounded
                db.execute("""DELETE FROM logs WHERE account_id=? AND id <
                              (SELECT COALESCE(MIN(id),0) FROM (SELECT id FROM logs WHERE account_id=?
                               ORDER BY id DESC LIMIT 5000))""", (account_id, account_id))
    except Exception as exc:
        print("Could not write log:", exc)


def query_logs(account_id, category="ALL", q="", limit=200):
    sql = "SELECT * FROM logs WHERE account_id=?"
    args = [account_id]
    category = (category or "ALL").upper()
    if category != "ALL":
        sql += " AND category=?"
        args.append(category)
    if q:
        sql += " AND (message LIKE ? OR guild_name LIKE ? OR actor LIKE ? OR role_name LIKE ?)"
        args.extend([f"%{q}%"] * 4)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(min(max(int(limit), 1), 1000))
    with conn() as db:
        return [dict(r) for r in db.execute(sql, args).fetchall()]


def count_logs(account_id):
    with conn() as db:
        return db.execute("SELECT COUNT(*) FROM logs WHERE account_id=?", (account_id,)).fetchone()[0]


def clear_logs(account_id):
    with conn() as db:
        db.execute("DELETE FROM logs WHERE account_id=?", (account_id,))


# ------------------------------------------------------------------ settings
DEFAULTS = {
    "prefix": "!",
    "presence": {"status": "online", "kind": "none", "text": "", "url": ""},
    "autoreact": {},
    "autoreply": {},
    "afk": {"on": False, "reason": "AFK", "since": 0},
    "nitro": {"enabled": False, "delay_ms": 0, "blacklist": []},
    "giveaway": {
        "enabled": False,
        "triggers": ["giveaway", "gwy", "react with", "enter", "hosted by", "winners", "winner"],
        "ignore_words": ["ended"],
        "use_buttons": True, "use_reactions": True,
        "win_alerts": True, "win_keywords": ["congrats", "won", "congratulations"], "webhook_url": "",
        "min_delay": 2, "max_delay": 6, "blacklist": [],
    },
    "notify": {"dms": True, "pings": True},
    "wallets": {},
    # --- added by the extras update ---
    "rpc": {"enabled": False, "type": "playing", "name": "", "details": "", "state": "", "large_image": "", "large_text": "",
            "small_image": "", "small_text": "", "buttons": [{}, {}], "timer": {"mode": "none", "span": 0, "start": 0, "end": 0},
            "party": [], "app_id": "", "url": "", "sig": "", "presets": {}},
    "vanity": {"enabled": False, "webhook_url": "", "webhook_name": "Aether Vanity Sniper", "webhook_avatar": "",
               "watch": [], "interval": 30, "notify_changes": True, "notify_free": True, "known": {}, "free_alerted": []},
    "gwyhost": {"active": {}, "ended": {}},
    "device": {"type": "desktop", "show_activity": False},
}
_OPTION_KEYS = {"presence", "afk", "nitro", "giveaway", "notify", "rpc", "vanity", "gwyhost", "device"}


def get_settings(account_id):
    out = copy.deepcopy(DEFAULTS)
    with conn() as db:
        rows = db.execute("SELECT key, value FROM settings WHERE account_id=?", (account_id,)).fetchall()
    for r in rows:
        key = r["key"]
        if key not in DEFAULTS:
            continue
        try:
            value = json.loads(r["value"])
        except ValueError:
            continue
        if key in _OPTION_KEYS and isinstance(value, dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


def get_setting(account_id, key):
    return get_settings(account_id)[key]


def set_setting(account_id, key, value):
    if key not in DEFAULTS:
        raise KeyError(key)
    with conn() as db:
        db.execute("""INSERT INTO settings(account_id,key,value,updated) VALUES(?,?,?,?)
                      ON CONFLICT(account_id,key) DO UPDATE SET value=excluded.value, updated=excluded.updated""",
                   (account_id, key, json.dumps(value, ensure_ascii=False), time.time()))


def settings_rev(account_id):
    with conn() as db:
        return db.execute("SELECT COALESCE(MAX(updated),0) FROM settings WHERE account_id=?", (account_id,)).fetchone()[0]


# ------------------------------------------------------------------ jobs (dashboard -> worker)
def create_job(account_id, action, payload):
    with conn() as db:
        db.execute("DELETE FROM jobs WHERE created_at < ?", (time.time() - 3600,))
        cur = db.execute("INSERT INTO jobs(account_id,action,payload,created_at) VALUES(?,?,?,?)",
                         (account_id, action, json.dumps(payload or {}), time.time()))
        return cur.lastrowid


def get_job(job_id):
    with conn() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["result"] = json.loads(d["result"]) if d["result"] else None
    except ValueError:
        d["result"] = None
    return d


def claim_jobs(account_id, limit=5):
    claimed = []
    with conn() as db:
        rows = db.execute("SELECT * FROM jobs WHERE account_id=? AND status='pending' ORDER BY id LIMIT ?",
                          (account_id, limit)).fetchall()
        for r in rows:
            cur = db.execute("UPDATE jobs SET status='running' WHERE id=? AND status='pending'", (r["id"],))
            if cur.rowcount:
                claimed.append(dict(r))
    return claimed


def finish_job(job_id, status, result):
    with conn() as db:
        db.execute("UPDATE jobs SET status=?, result=?, finished_at=? WHERE id=?",
                   (status, json.dumps(result, ensure_ascii=False, default=str), time.time(), job_id))


def fail_stale_jobs(account_id):
    with conn() as db:
        db.execute("""UPDATE jobs SET status='error', result=?, finished_at=?
                      WHERE account_id=? AND status IN ('pending','running')""",
                   (json.dumps({"error": "Bot restarted before this finished"}), time.time(), account_id))


# ------------------------------------------------------------------ notifications
def add_notification(account_id, kind, author="", author_id="", avatar_url="", guild_name="", channel_name="",
                     channel_id="", message_id="", jump_url="", preview=""):
    with conn() as db:
        db.execute("""INSERT INTO notifications(account_id,kind,author,author_id,avatar_url,guild_name,channel_name,
                      channel_id,message_id,jump_url,preview,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (account_id, kind, author, author_id, avatar_url, guild_name, channel_name, channel_id,
                    message_id, jump_url, preview, now_iso()))
        db.execute("""DELETE FROM notifications WHERE account_id=? AND id <
                      (SELECT COALESCE(MIN(id),0) FROM (SELECT id FROM notifications WHERE account_id=?
                       ORDER BY id DESC LIMIT 500))""", (account_id, account_id))


def list_notifications(account_id, kind=None, since_id=0, limit=100, unread_only=False):
    sql = "SELECT * FROM notifications WHERE account_id=? AND id>?"
    args = [account_id, int(since_id or 0)]
    if kind and kind != "all":
        sql += " AND kind=?"
        args.append(kind)
    if unread_only:
        sql += " AND is_read=0"
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(min(max(int(limit), 1), 500))
    with conn() as db:
        return [dict(r) for r in db.execute(sql, args).fetchall()]


def unread_count(account_id):
    with conn() as db:
        return db.execute("SELECT COUNT(*) FROM notifications WHERE account_id=? AND is_read=0", (account_id,)).fetchone()[0]


def last_notification_id(account_id):
    with conn() as db:
        return db.execute("SELECT COALESCE(MAX(id),0) FROM notifications WHERE account_id=?", (account_id,)).fetchone()[0]


def mark_read(account_id, ids=None):
    with conn() as db:
        if ids:
            marks = ",".join("?" * len(ids))
            db.execute(f"UPDATE notifications SET is_read=1 WHERE account_id=? AND id IN ({marks})",
                       (account_id, *[int(i) for i in ids]))
        else:
            db.execute("UPDATE notifications SET is_read=1 WHERE account_id=?", (account_id,))


def clear_notifications(account_id):
    with conn() as db:
        db.execute("DELETE FROM notifications WHERE account_id=?", (account_id,))


# ------------------------------------------------------------------ snipers
def add_sniper_event(account_id, kind, ref="", guild_name="", channel_name="", author="", result="", detail="", latency_ms=0):
    with conn() as db:
        db.execute("""INSERT INTO sniper_events(account_id,kind,ref,guild_name,channel_name,author,result,detail,
                      latency_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                   (account_id, kind, ref, guild_name, channel_name, author, result, detail, latency_ms, now_iso()))


def list_sniper_events(account_id, kind, limit=100):
    with conn() as db:
        rows = db.execute("SELECT * FROM sniper_events WHERE account_id=? AND kind=? ORDER BY id DESC LIMIT ?",
                          (account_id, kind, min(max(int(limit), 1), 500))).fetchall()
    return [dict(r) for r in rows]


def sniper_summary(account_id):
    out = {k: {"seen": 0, "success": 0, "failed": 0, "wins": 0} for k in ("nitro", "giveaway")}
    with conn() as db:
        rows = db.execute("""SELECT kind, result, COUNT(*) AS c FROM sniper_events WHERE account_id=?
                             GROUP BY kind, result""", (account_id,)).fetchall()
    for r in rows:
        if r["kind"] not in out:
            continue
        if r["result"] == "won":
            out[r["kind"]]["wins"] += r["c"]
            continue
        out[r["kind"]]["seen"] += r["c"]
        out[r["kind"]]["success" if r["result"] in ("claimed", "joined") else "failed"] += r["c"]
    return out


# ------------------------------------------------------------------ stats / snapshots
def incr_stat(account_id, key, n=1):
    with conn() as db:
        db.execute("INSERT OR IGNORE INTO stats(account_id,key,value) VALUES(?,?,0)", (account_id, key))
        db.execute("UPDATE stats SET value=value+? WHERE account_id=? AND key=?", (n, account_id, key))


def get_stats(account_id):
    with conn() as db:
        rows = db.execute("SELECT key, value FROM stats WHERE account_id=?", (account_id,)).fetchall()
    return {r["key"]: r["value"] for r in rows}


def save_snapshot(account_id, data):
    with conn() as db:
        db.execute("""INSERT INTO snapshots(account_id,data,updated_at) VALUES(?,?,?)
                      ON CONFLICT(account_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at""",
                   (account_id, json.dumps(data, ensure_ascii=False, default=str), now_iso()))


def get_snapshot(account_id):
    with conn() as db:
        row = db.execute("SELECT data, updated_at FROM snapshots WHERE account_id=?", (account_id,)).fetchone()
    if not row:
        return None, None
    try:
        return json.loads(row["data"]), row["updated_at"]
    except ValueError:
        return None, None


# ------------------------------------------------------------------ quests
def save_quests(account_id, quests):
    with conn() as db:
        db.execute("""INSERT INTO quests_cache(account_id,data,updated_at) VALUES(?,?,?)
                      ON CONFLICT(account_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at""",
                   (account_id, json.dumps(quests, ensure_ascii=False, default=str), now_iso()))


def get_quests(account_id):
    with conn() as db:
        row = db.execute("SELECT data, updated_at FROM quests_cache WHERE account_id=?", (account_id,)).fetchone()
    if not row:
        return [], None
    try:
        return json.loads(row["data"]), row["updated_at"]
    except ValueError:
        return [], None


def set_quest_run(account_id, quest_id, status, progress=0, target=0, message=""):
    with conn() as db:
        db.execute("""INSERT INTO quest_runs(account_id,quest_id,status,progress,target,message,updated)
                      VALUES(?,?,?,?,?,?,?)
                      ON CONFLICT(account_id,quest_id) DO UPDATE SET status=excluded.status,
                      progress=excluded.progress, target=excluded.target, message=excluded.message,
                      updated=excluded.updated""",
                   (account_id, str(quest_id), status, progress, target, message, time.time()))


def get_quest_runs(account_id):
    with conn() as db:
        rows = db.execute("SELECT * FROM quest_runs WHERE account_id=?", (account_id,)).fetchall()
    return {r["quest_id"]: dict(r) for r in rows}


def pause_stale_quest_runs(account_id):
    with conn() as db:
        db.execute("""UPDATE quest_runs SET status='paused', message='Bot restarted', updated=?
                      WHERE account_id=? AND status='running'""", (time.time(), account_id))
