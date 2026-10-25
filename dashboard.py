import copy
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, g, jsonify, redirect, render_template, request, send_from_directory, session, url_for

import db  # loads .env
import devices
import extras
import manager as licenses
import procman
import tools

BASE = Path(__file__).resolve().parent


def _secret_key():
    key = os.getenv("FLASK_SECRET_KEY")
    if key:
        return key
    path = BASE / ".secret_key"  # persisted so logins survive restarts
    try:
        if path.exists():
            return path.read_text().strip()
        key = secrets.token_hex(32)
        path.write_text(key)
        return key
    except OSError:
        return secrets.token_hex(32)


app = Flask(__name__)
app.secret_key = _secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
    MAX_CONTENT_LENGTH=256 * 1024,
)
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "change-this-password")

FAILED_LOGINS = {}
MAX_ATTEMPTS, LOCK_WINDOW = 5, 300

KEY_MAX_ACCOUNTS = int(os.getenv("KEY_MAX_ACCOUNTS", "3"))
LOCAL_ACTIONS = {"translate"}
WORKER_ACTIONS = {"ping", "info", "refresh_snapshot", "user", "server", "avatar", "channels", "send", "purge",
                  "snipe", "dm_list", "dm_history", "friends", "friend_accept", "friend_remove", "friend_add",
                  "mod", "quest_list", "quest_enroll", "quest_start", "quest_pause", "quest_start_all", "quest_pause_all",
                  # added: fun commands, developer portal, shop, inbox mentions/replies
                  "fun", "hypesquad", "dev_overview",
                  "music_status", "music_voice", "music_play", "music_pause", "music_resume", "music_skip", "music_stop", "music_volume",
                  "music_seek", "music_loop", "music_remove", "music_clear",
                  "clone_guilds", "clone_start", "clone_status", "clone_cancel",
                  "chan_list", "chan_lock", "chan_unlock", "chan_create", "chan_rename", "dev_token", "shop_catalog", "dm_history2", "dm_members", "send2"}
SENSITIVE_ACTIONS = {"dev_token"}  # their job row (payload + result) is wiped as soon as the browser has collected it
SNOWFLAKE = re.compile(r"^\d{5,25}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-\.]{20,200}$")
COIN_ID_RE = re.compile(r"^[a-z0-9\-_.]{1,80}$")


# ------------------------------------------------------------------ auth helpers
def too_many_attempts(ip):
    now = time.time()
    recent = [t for t in FAILED_LOGINS.get(ip, []) if now - t < LOCK_WINDOW]
    FAILED_LOGINS[ip] = recent
    return len(recent) >= MAX_ATTEMPTS


def safe_next(target):
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    parsed = urlparse(target)
    return None if parsed.scheme or parsed.netloc else target


def session_ok():
    """Admin sessions stay valid; key sessions are re-checked so expiry/revocation logs the user out."""
    if not session.get("authenticated"):
        return False
    if session.get("role") == "user":
        rec = licenses.record_for_owner(session.get("owner", ""))
        if not rec or licenses.state(rec) != "active":
            session.clear()
            return False
    return True


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session_ok():
            if request.path.startswith("/api/"):
                return jsonify(error="Not signed in"), 401
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def is_admin():
    return session.get("role", "admin") == "admin"


def current_owner():
    return session.get("owner", "admin")


def json_only(fn):
    """State-changing API calls must be JSON (a cross-site form cannot send that), which blocks CSRF."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if request.method != "GET" and not request.is_json:
            return jsonify(error="JSON body required"), 415
        return fn(*args, **kwargs)
    return wrapper


def decorate(acct):
    running = procman.is_running(acct["id"])
    if not running and not acct.get("last_error"):  # a crashed worker records its error while we check it
        acct["last_error"] = (db.get_status(acct["id"]) or {}).get("last_error") or ""
    acct["running"] = running
    acct["state"] = ("online" if acct.get("online") else "starting") if running else "offline"
    return acct


def resolve_current():
    if not hasattr(g, "_cur"):
        accounts = [decorate(a) for a in db.list_accounts(None if is_admin() else current_owner())]
        sid = session.get("account_id")
        cur = next((a for a in accounts if a["id"] == sid), None) or (accounts[0] if accounts else None)
        g._cur = (cur, accounts)
    return g._cur


def account_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        cur, _ = resolve_current()
        if not cur:
            return redirect(url_for("accounts_page"))
        g.account = cur
        return fn(*args, **kwargs)
    return wrapper


def api_account(explicit=None):
    cur, accounts = resolve_current()
    if explicit is not None:
        try:
            match = next((a for a in accounts if a["id"] == int(explicit)), None)
        except (TypeError, ValueError):
            match = None
        if match:
            return match
    return cur


def owned(aid):
    """The account if the signed-in user may touch it, else None."""
    acct = db.get_account(aid)
    if not acct or (not is_admin() and acct.get("owner") != current_owner()):
        return None
    return acct


# ------------------------------------------------------------------ template helpers
def parse_iso(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


@app.template_filter("fdate")
def fdate(value):
    dt = parse_iso(value)
    return dt.strftime("%d %b %Y") if dt else "—"


@app.template_filter("age")
def age(value):
    dt = parse_iso(value)
    if not dt:
        return "—"
    days = max((datetime.now(timezone.utc) - dt).days, 0)
    years, rem = divmod(days, 365)
    months, d = divmod(rem, 30)
    if years:
        return f"{years}y {months}m"
    return f"{months}m {d}d" if months else f"{d}d"


@app.template_filter("ago")
def ago(value):
    dt = parse_iso(value)
    if not dt:
        return "—"
    s = int((datetime.now(timezone.utc) - dt).total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


@app.template_filter("nitro_name")
def nitro_name(value):
    names = {"nitro": "Nitro", "nitro_classic": "Nitro Classic", "nitro_basic": "Nitro Basic", "none": "No Nitro"}
    return names.get(str(value or "none").lower(), str(value).replace("_", " ").title())


@app.context_processor
def inject_globals():
    ctx = {"brand_name": "AetherPanel"}
    if session_ok():
        cur, accounts = resolve_current()
        rec = None if is_admin() else licenses.record_for_owner(current_owner())
        ctx.update(role="admin" if is_admin() else "user", license_text=licenses.remaining_text(rec) if rec else None,
                   accounts=accounts, current=cur,
                   unread=db.unread_count(cur["id"]) if cur else 0,
                   last_notif_id=db.last_notification_id(cur["id"]) if cur else 0)
    return ctx


# ------------------------------------------------------------------ login
@app.route("/login", methods=["GET", "POST"])
def login():
    error, code = None, 200
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        if too_many_attempts(ip):
            error, code = "Too many attempts. Please wait a few minutes.", 429
        else:
            raw = request.form.get("password", "").strip()
            role = owner = None
            if secrets.compare_digest(raw.encode(), DASHBOARD_PASSWORD.encode()):
                role, owner = "admin", "admin"
            else:
                ok, message, rec = licenses.validate(raw)
                if ok:
                    role, owner = "user", licenses.owner_id(rec["key"])
                else:
                    error = message
            if role:
                FAILED_LOGINS.pop(ip, None)
                session.clear()
                session.update(authenticated=True, role=role, owner=owner)
                return redirect(safe_next(request.args.get("next")) or url_for("index"))
            FAILED_LOGINS.setdefault(ip, []).append(time.time())
            code = 401
            error = error or "Incorrect password or license key."
    return render_template("login.html", error=error), code


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------------ pages
def page_settings(account_id):
    """Settings for templates - the saved webhook URL is reduced to 'set' / ''."""
    cfg = db.get_settings(account_id)
    cfg["giveaway"]["webhook_url"] = "set" if cfg["giveaway"].get("webhook_url") else ""
    cfg["vanity"]["webhook_url"] = "set" if cfg["vanity"].get("webhook_url") else ""
    return cfg


@app.route("/")
@login_required
@account_required
def index():
    a = g.account
    snap, updated = db.get_snapshot(a["id"])
    return render_template("index.html", a=a, snap=snap or {}, snap_updated=updated, stats=db.get_stats(a["id"]),
                           sniper=db.sniper_summary(a["id"]), quests=quest_view(a["id"])[1])


@app.route("/accounts")
@login_required
def accounts_page():
    return render_template("accounts.html")


@app.route("/commands")
@login_required
@account_required
def commands_page():
    a = g.account
    snap, _ = db.get_snapshot(a["id"])
    return render_template("commands.html", a=a, settings=page_settings(a["id"]),
                           servers=(snap or {}).get("servers", []))


@app.route("/snipers")
@login_required
@account_required
def snipers_page():
    a = g.account
    snap, _ = db.get_snapshot(a["id"])
    return render_template("snipers.html", a=a, settings=page_settings(a["id"]), sniper=db.sniper_summary(a["id"]),
                           servers=(snap or {}).get("servers", []))


@app.route("/inbox")
@login_required
@account_required
def inbox_page():
    return render_template("inbox.html", a=g.account, settings=page_settings(g.account["id"]))


@app.route("/friends")
@login_required
@account_required
def friends_page():
    return render_template("friends.html", a=g.account)


@app.route("/servers")
@login_required
@account_required
def servers_page():
    snap, updated = db.get_snapshot(g.account["id"])
    return render_template("servers.html", a=g.account, servers=(snap or {}).get("servers", []), snap_updated=updated)


@app.route("/logs")
@login_required
@account_required
def logs_page():
    return render_template("logs.html", a=g.account, total=db.count_logs(g.account["id"]))


@app.get("/assets/<path:filename>")
def assets(filename):
    """Serves dashboard/assets/ (logo.png) - public, images only."""
    if filename.rsplit(".", 1)[-1].lower() not in {"png", "jpg", "jpeg", "webp", "gif", "svg", "ico"}:
        abort(404)
    return send_from_directory(BASE / "assets", filename, max_age=3600)


@app.route("/shop")
@login_required
@account_required
def shop_page():
    return render_template("shop.html", a=g.account)


DEVICE_ICON_NAMES = {"desktop": ("desktop", "pc", "computer", "windows"), "vr": ("vr", "quest", "metaquest", "meta-quest", "meta", "oculus"),
                     "mobile": ("mobile", "phone", "smartphone", "android", "ios"), "console": ("console", "xbox", "playstation", "ps5", "controller", "gamepad")}


@app.get("/device-icons/<kind>")
def device_icon(kind):
    """Serves the Device card logos from dashboard/templates/icons/ (desktop / vr / mobile / console, any of png, svg, webp, jpg, gif)."""
    folder = BASE / "templates" / "icons"
    if kind not in DEVICE_ICON_NAMES or not folder.is_dir():
        abort(404)
    files = {f.stem.lower(): f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in {".svg", ".png", ".webp", ".jpg", ".jpeg", ".gif"}}
    for name in DEVICE_ICON_NAMES[kind]:
        if name in files:
            return send_from_directory(folder, files[name].name, max_age=3600)
    abort(404)


@app.route("/cloner")
@login_required
@account_required
def cloner_page():
    return render_template("cloner.html", a=g.account)


@app.route("/music")
@login_required
@account_required
def music_page():
    return render_template("music.html", a=g.account)


@app.route("/devportal")
@login_required
@account_required
def devportal_page():
    return render_template("devportal.html", a=g.account)


# ------------------------------------------------------------------ API: state / accounts
@app.get("/api/state")
@login_required
def api_state():
    a = api_account(request.args.get("account_id"))
    accounts = [{"id": x["id"], "state": x["state"]} for x in resolve_current()[1]]
    if not a:
        return jsonify(account=None, accounts=accounts, unread=0)
    snap, updated = db.get_snapshot(a["id"])
    sniper = db.sniper_summary(a["id"])
    sniper["total_success"] = sniper["nitro"]["success"] + sniper["giveaway"]["success"]
    return jsonify(account=a, accounts=accounts, snapshot=snap or {}, snapshot_updated=updated,
                   stats=db.get_stats(a["id"]), sniper=sniper, unread=db.unread_count(a["id"]))


@app.post("/api/switch")
@login_required
@json_only
def api_switch():
    try:
        aid = int((request.get_json() or {}).get("account_id"))
    except (TypeError, ValueError):
        return jsonify(error="Invalid account"), 400
    if not owned(aid):
        return jsonify(error="Unknown account"), 404
    session["account_id"] = aid
    return jsonify(ok=True)


def clean_token(raw):
    token = str(raw or "").strip().strip("\"'").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


@app.post("/api/accounts")
@login_required
@json_only
def api_account_add():
    data = request.get_json() or {}
    label = str(data.get("label", "")).strip()[:40] or "Account"
    token = clean_token(data.get("token"))
    if not TOKEN_RE.match(token):
        return jsonify(error="That doesn't look like a Discord user token."), 400
    if not is_admin() and len(resolve_current()[1]) >= KEY_MAX_ACCOUNTS:
        return jsonify(error=f"Your license allows up to {KEY_MAX_ACCOUNTS} accounts."), 403
    aid = db.add_account(label, token, current_owner())
    session["account_id"] = aid
    return jsonify(ok=True, id=aid)


@app.patch("/api/accounts/<int:aid>")
@login_required
@json_only
def api_account_update(aid):
    if not owned(aid):
        return jsonify(error="Unknown account"), 404
    data = request.get_json() or {}
    fields = {}
    if "label" in data:
        fields["label"] = str(data["label"]).strip()[:40] or "Account"
    if "auto_start" in data:
        fields["auto_start"] = 1 if data["auto_start"] else 0
    if data.get("token"):
        token = clean_token(data["token"])
        if not TOKEN_RE.match(token):
            return jsonify(error="That doesn't look like a Discord user token."), 400
        fields["token"] = token
    db.update_account(aid, **fields)
    return jsonify(ok=True, restart_needed="token" in fields and procman.is_running(aid))


@app.delete("/api/accounts/<int:aid>")
@login_required
def api_account_delete(aid):
    if not owned(aid):
        return jsonify(error="Unknown account"), 404
    procman.stop(aid)
    db.delete_account(aid)
    if session.get("account_id") == aid:
        session.pop("account_id", None)
    return jsonify(ok=True)


@app.post("/api/accounts/<int:aid>/<verb>")
@login_required
@json_only
def api_account_power(aid, verb):
    if verb not in ("start", "stop", "restart") or not owned(aid):
        return jsonify(error="Unknown account or action"), 404
    if verb in ("stop", "restart"):
        procman.stop(aid)
        db.add_log("SYSTEM", "Bot stopped from the dashboard", "warning", account_id=aid)
    if verb in ("start", "restart"):
        procman.start(aid)
        db.add_log("SYSTEM", "Bot started from the dashboard", account_id=aid)
    return jsonify(ok=True)


@app.get("/api/accounts/<int:aid>/console")
@login_required
def api_console(aid):
    acct = owned(aid)
    if not acct:
        return jsonify(error="Unknown account"), 404
    decorate(acct)
    return jsonify(text=procman.console_tail(aid), state=acct["state"], error=acct.get("last_error") or "")


# ------------------------------------------------------------------ API: actions (dashboard -> worker)
def local_translate(payload):
    lang, text = str(payload.get("lang", "")).strip(), str(payload.get("text", "")).strip()
    if not lang or not text:
        raise ValueError("Enter a language code (e.g. es, fr, de) and some text.")
    return {"title": f"Translated to {lang}", "text": tools.translate_text(lang, text)}


@app.post("/api/act")
@login_required
@json_only
def api_act():
    data = request.get_json() or {}
    action, payload = data.get("action"), data.get("payload") or {}
    if not isinstance(payload, dict) or len(json.dumps(payload)) > 8000:
        return jsonify(error="Invalid payload"), 400
    if action in LOCAL_ACTIONS:
        try:
            return jsonify(status="done", result=local_translate(payload))
        except Exception as exc:
            return jsonify(error=str(exc)), 400
    if action not in WORKER_ACTIONS:
        return jsonify(error="Unknown action"), 400
    acct = api_account(data.get("account_id"))
    if not acct:
        return jsonify(error="Add an account first"), 409
    if acct["state"] == "offline":
        return jsonify(error="This account is offline. Start it first."), 409
    if acct["state"] == "starting":
        return jsonify(error="This account is still logging in. Try again in a few seconds."), 409
    job_id = db.create_job(acct["id"], action, payload)
    deadline = time.time() + 8
    while time.time() < deadline:
        job = db.get_job(job_id)
        if job["status"] in ("done", "error"):
            return jsonify(collect_job(job))
        time.sleep(0.2)
    return jsonify(pending=True, job_id=job_id)


def job_payload(job):
    if job["status"] == "error":
        return {"status": "error", "error": (job.get("result") or {}).get("error", "Unknown error")}
    return {"status": "done", "result": job.get("result")}


def collect_job(job):
    """job_payload + wipe of secrets (a revealed bot token, a 2FA code) from the jobs table once collected."""
    payload = job_payload(job)
    if job.get("action") in SENSITIVE_ACTIONS:
        try:
            with db.conn() as c:
                c.execute("UPDATE jobs SET result=?, payload=? WHERE id=?", ("{}", "{}", job["id"]))
        except Exception as exc:
            print("job scrub failed:", exc)
    return payload


@app.get("/api/jobs/<int:job_id>")
@login_required
def api_job(job_id):
    job = db.get_job(job_id)
    if not job or not owned(job["account_id"]):
        return jsonify(error="Unknown job"), 404
    if job["status"] in ("done", "error"):
        return jsonify(collect_job(job))
    return jsonify(pending=True, job_id=job_id)


# ------------------------------------------------------------------ API: settings
def _bool(v):
    return bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "yes", "on")


def _ids(values, limit=100):
    items = values if isinstance(values, list) else str(values or "").replace(",", " ").split()
    out = []
    for v in items:
        v = str(v).strip()
        if SNOWFLAKE.match(v) and v not in out:
            out.append(v)
    return out[:limit]


def _num(v, lo, hi, default):
    try:
        return min(max(float(v), lo), hi)
    except (TypeError, ValueError):
        return default


def validate_rpc(v, existing):
    """Rich-presence form -> stored value. Uses the same field rules as the !rpc chat command."""
    keep_presets = existing.get("presets") or {}
    if _bool(v.get("clear")):
        return {**extras.rpc_defaults(), "app_id": existing.get("app_id", ""), "presets": keep_presets}
    defaults = extras.rpc_defaults()
    r = {**defaults, **{k: copy.deepcopy(existing[k]) for k in defaults if k in existing}, "presets": keep_presets}

    def text(name):
        return str(v.get(name) or "").strip()

    def setf(field, value, label=None):
        err = extras.apply_rpc_field(r, field, value)
        if err:
            raise ValueError(f"{label or field.replace('_', ' ').capitalize()}: {err}")

    for field, value in (("type", text("type") or "playing"), ("name", text("name")), ("details", text("details")),
                         ("state", text("state")), ("large_image", text("large_image")), ("large_text", text("large_text")),
                         ("small_image", text("small_image")), ("small_text", text("small_text")),
                         ("app_id", text("app_id")), ("url", text("url"))):
        setf(field, value)
    for i in (1, 2):
        label, url = text(f"btn{i}_label"), text(f"btn{i}_url")
        if label or url:
            if not (label and url) or "|" in label:
                raise ValueError(f"Button {i} needs a label (no | character) and a link")
            setf(f"btn{i}", f"{label} | {url}", f"Button {i}")
        else:
            setf(f"btn{i}", "")
    mode, span_text = text("timer_mode") or "none", text("timer_span")
    if mode not in ("none", "elapsed", "countdown"):
        raise ValueError("Invalid timer mode")
    span = extras.parse_duration(span_text) if span_text else 0
    if span_text and not span:
        raise ValueError("Timer: use a duration like 90m or 2h30m")
    old = existing.get("timer") or {}
    if mode == "none":
        setf("timer", "none")
    elif mode == "countdown" and not span:
        raise ValueError("A countdown needs a duration")
    elif old.get("mode") == mode and int(old.get("span") or 0) == span:
        r["timer"] = copy.deepcopy(old)  # unchanged -> keep the running clock
    else:
        setf("timer", "start" if (mode == "elapsed" and not span) else f"{mode} {span}s")
    cur, mx = text("party_cur"), text("party_max")
    setf("party", f"{cur}/{mx}" if (cur or mx) else "")
    r["enabled"] = _bool(v.get("enabled"))
    if r["enabled"] and not r["name"]:
        raise ValueError("Enter the activity name")
    if (r["large_image"] or r["small_image"]) and not r["app_id"]:
        raise ValueError("Images need an Application ID (Developer Portal -> your app -> General Information)")
    return r


def clean_vanity_code(raw):
    code = str(raw or "").strip().lower()
    code = re.sub(r"^(https?://)?(www\.)?(discord\.gg|discord\.com/invite|discordapp\.com/invite)/", "", code).strip("/ ")
    return code


def validate_vanity(v, existing):
    """Vanity sniper form -> stored value. Only keys that are sent change; known/free_alerted stay owned by the bot."""
    out = copy.deepcopy(existing)
    if "enabled" in v:
        out["enabled"] = _bool(v["enabled"])
    if "watch" in v:
        raw = v["watch"] if isinstance(v["watch"], list) else re.split(r"[,\s]+", str(v["watch"] or ""))
        codes = []
        for item in raw:
            code = clean_vanity_code(item)
            if not code:
                continue
            if not extras.VANITY_RE.match(code):
                raise ValueError(f"'{code}' is not a valid vanity code (2-32 letters, numbers or hyphens)")
            if code not in codes:
                codes.append(code)
        if len(codes) > 50:
            raise ValueError("The watch list is limited to 50 codes")
        old = existing.get("watch") or []
        out["watch"] = codes
        out["free_alerted"] = [c for c in existing.get("free_alerted", []) if c in codes and c in old]
    if "interval" in v:
        out["interval"] = int(_num(v["interval"], 10, 3600, existing.get("interval", 30)))
    for flag in ("notify_changes", "notify_free"):
        if flag in v:
            out[flag] = _bool(v[flag])
    if "webhook_name" in v:
        out["webhook_name"] = str(v["webhook_name"] or "").strip()[:80] or "Aether Vanity Sniper"
    if "webhook_avatar" in v:
        avatar = str(v["webhook_avatar"] or "").strip()[:300]
        if avatar and not avatar.startswith(("http://", "https://")):
            raise ValueError("Webhook avatar must be an http(s) image link")
        out["webhook_avatar"] = avatar
    if _bool(v.get("clear_webhook")):
        out["webhook_url"] = ""
    elif str(v.get("webhook_url") or "").strip():
        hook = str(v["webhook_url"]).strip()
        if not extras.DISCORD_WEBHOOK_RE.match(hook):
            raise ValueError("That is not a Discord webhook URL")
        out["webhook_url"] = hook
    return out


def validate_setting(key, v, existing=None):
    if key == "prefix":
        v = str(v).strip()
        if not 1 <= len(v) <= 3 or " " in v:
            raise ValueError("Prefix must be 1-3 characters with no spaces")
        return v
    if key == "autoreact":
        if not isinstance(v, dict) or len(v) > 100:
            raise ValueError("Invalid auto-react list")
        out = {}
        for uid, emoji in v.items():
            if not SNOWFLAKE.match(str(uid)) or not str(emoji).strip():
                raise ValueError("Auto-react needs a numeric user ID and an emoji")
            out[str(uid)] = str(emoji).strip()[:64]
        return out
    if key == "wallets":
        if not isinstance(v, dict) or len(v) > 200:
            raise ValueError("Invalid wallet list")
        out = {}
        for _, w in v.items():
            if not isinstance(w, dict):
                raise ValueError("Invalid wallet entry")
            symbol = str(w.get("symbol", "")).strip().upper()[:15]
            network = str(w.get("network", "")).strip()[:30]
            address = tools.validate_wallet(symbol, w.get("address"))
            try:
                amount = max(float(w.get("amount") or 0), 0)
            except (TypeError, ValueError):
                raise ValueError("Amount must be a number")
            coin_id = str(w.get("coin_id", "")).strip().lower()
            if coin_id and not COIN_ID_RE.match(coin_id):
                coin_id = ""
            out[tools.wallet_key(symbol, network)] = {
                "symbol": symbol, "coin": str(w.get("coin", "")).strip()[:60] or symbol, "coin_id": coin_id,
                "network": network, "address": address, "amount": amount, "note": str(w.get("note", "")).strip()[:120]}
        return out
    if key == "autoreply":
        if not isinstance(v, dict) or len(v) > 100:
            raise ValueError("Invalid auto-reply list")
        return {str(t).strip().lower()[:100]: str(r)[:1000] for t, r in v.items() if str(t).strip() and str(r).strip()}
    if not isinstance(v, dict):
        raise ValueError("Invalid value")
    if key == "rpc":
        return validate_rpc(v, existing or {})
    if key == "device":
        kind = str(v.get("type", "desktop")).lower()
        if kind not in devices.DEVICES:
            raise ValueError("Pick desktop, vr, mobile or console")
        return {"type": kind, "show_activity": False}
    if key == "presence":
        status, kind = v.get("status", "online"), v.get("kind", "none")
        if status not in ("online", "idle", "dnd", "invisible"):
            raise ValueError("Invalid status")
        if kind not in ("none", "playing", "listening", "watching", "competing", "streaming"):
            raise ValueError("Invalid activity type")
        text, url = str(v.get("text", "")).strip()[:128], str(v.get("url", "")).strip()[:300]
        if kind != "none" and not text:
            raise ValueError("Enter the activity text")
        if url and not url.startswith(("http://", "https://")):
            raise ValueError("Stream URL must start with http:// or https://")
        return {"status": status, "kind": kind, "text": text if kind != "none" else "", "url": url}
    if key == "afk":
        on = _bool(v.get("on"))
        since = float(v.get("since") or 0) or (time.time() if on else 0)
        return {"on": on, "reason": str(v.get("reason") or "AFK")[:200], "since": since}
    if key == "nitro":
        return {"enabled": _bool(v.get("enabled")), "delay_ms": int(_num(v.get("delay_ms"), 0, 10000, 0)),
                "blacklist": _ids(v.get("blacklist"))}
    if key == "giveaway":
        def words(name, default, limit=40):
            raw = v.get(name)
            raw = raw if isinstance(raw, list) else str(raw if raw is not None else "").split(",")
            out = [str(k).strip().lower()[:40] for k in raw if str(k).strip()][:limit]
            return out or default
        lo, hi = _num(v.get("min_delay"), 0, 120, 2), _num(v.get("max_delay"), 0, 120, 6)
        webhook = str(v.get("webhook_url") or "").strip()
        if _bool(v.get("clear_webhook")):
            webhook = ""
        elif not webhook:
            webhook = (existing or {}).get("webhook_url", "")
        elif not re.match(r"^https://(discord|discordapp)\.com/api/webhooks/\d+/[\w\-]+$", webhook):
            raise ValueError("That is not a Discord webhook URL")
        d = db.DEFAULTS["giveaway"]
        return {"enabled": _bool(v.get("enabled")), "triggers": words("triggers", d["triggers"]),
                "ignore_words": words("ignore_words", [], 20), "win_keywords": words("win_keywords", d["win_keywords"], 20),
                "use_buttons": _bool(v.get("use_buttons", True)), "use_reactions": _bool(v.get("use_reactions", True)),
                "win_alerts": _bool(v.get("win_alerts", True)), "webhook_url": webhook,
                "min_delay": min(lo, hi), "max_delay": max(lo, hi), "blacklist": _ids(v.get("blacklist"))}
    if key == "notify":
        return {"dms": _bool(v.get("dms")), "pings": _bool(v.get("pings"))}
    if key == "vanity":
        return validate_vanity(v, existing or {})
    raise ValueError("Unknown setting")


def public_setting(key, value):
    """Never send the saved webhook URL back to the browser."""
    if key in ("giveaway", "vanity"):
        value = dict(value)
        value["webhook_set"] = bool(value.pop("webhook_url", ""))
    return value


@app.get("/api/settings/<key>")
@login_required
def api_setting_get(key):
    a = api_account(request.args.get("account_id"))
    if not a or key not in db.DEFAULTS:
        return jsonify(error="Unknown setting"), 404
    return jsonify(value=public_setting(key, db.get_setting(a["id"], key)))


@app.post("/api/settings/<key>")
@login_required
@json_only
def api_setting_set(key):
    data = request.get_json() or {}
    a = api_account(data.get("account_id"))
    if not a or key not in db.DEFAULTS:
        return jsonify(error="Unknown setting"), 404
    try:
        value = validate_setting(key, data.get("value"), db.get_setting(a["id"], key))
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    if key == "rpc" and value.get("enabled"):  # lets the bot notice if the simple activity is changed later
        value["sig"] = extras.presence_sig(db.get_setting(a["id"], "presence"))
    db.set_setting(a["id"], key, value)
    return jsonify(ok=True, value=public_setting(key, value), live=a["state"] == "online")


# ------------------------------------------------------------------ API: logs / notifications / snipers
@app.get("/api/logs")
@login_required
def api_logs():
    a = api_account(request.args.get("account_id"))
    if not a:
        return jsonify([])
    try:
        limit = int(request.args.get("limit", 200))
    except ValueError:
        limit = 200
    return jsonify(db.query_logs(a["id"], request.args.get("category", "ALL"), request.args.get("q", "").strip(), limit))


@app.post("/api/logs/clear")
@login_required
@json_only
def api_logs_clear():
    a = api_account((request.get_json() or {}).get("account_id"))
    if a:
        db.clear_logs(a["id"])
        db.add_log("SYSTEM", "Log history cleared from the dashboard", "warning", account_id=a["id"])
    return jsonify(ok=True)


@app.get("/api/notifications")
@login_required
def api_notifications():
    a = api_account(request.args.get("account_id"))
    if not a:
        return jsonify(items=[], unread=0)
    try:
        since, limit = int(request.args.get("since_id", 0)), int(request.args.get("limit", 100))
    except ValueError:
        since, limit = 0, 100
    items = db.list_notifications(a["id"], request.args.get("kind"), since, limit, request.args.get("unread") == "1")
    return jsonify(items=items, unread=db.unread_count(a["id"]))


@app.post("/api/notifications/read")
@login_required
@json_only
def api_notifications_read():
    data = request.get_json() or {}
    a = api_account(data.get("account_id"))
    if a:
        ids = [i for i in (data.get("ids") or []) if str(i).isdigit()]
        db.mark_read(a["id"], ids or None)
    return jsonify(ok=True)


@app.post("/api/notifications/clear")
@login_required
@json_only
def api_notifications_clear():
    a = api_account((request.get_json() or {}).get("account_id"))
    if a:
        db.clear_notifications(a["id"])
    return jsonify(ok=True)


@app.get("/api/sniper")
@login_required
def api_sniper():
    a = api_account(request.args.get("account_id"))
    if not a:
        return jsonify(events=[], summary={})
    kind = request.args.get("kind", "nitro")
    if kind not in ("nitro", "giveaway"):
        return jsonify(error="Unknown kind"), 400
    return jsonify(events=db.list_sniper_events(a["id"], kind), summary=db.sniper_summary(a["id"]))


# ------------------------------------------------------------------ vanity sniper helpers
_vanity_lock, _vanity_last = threading.Lock(), [0.0]


def vanity_lookup(code):
    """-> (status, info). status: free | taken | limited | unknown  (same logic as the bot's !vanity check)."""
    url = f"https://discord.com/api/v9/invites/{urllib.parse.quote(code)}?with_counts=false"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "AetherPanel/2.2"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode() or "{}")
        guild = data.get("guild") or {}
        return "taken", {"guild_name": guild.get("name"), "guild_id": guild.get("id")}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "free", {}
        if exc.code == 429:
            return "limited", {"retry_after": 30}
        return "unknown", {"detail": f"HTTP {exc.code}"}
    except Exception as exc:
        return "unknown", {"detail": type(exc).__name__}


@app.get("/api/vanity/check")
@login_required
def api_vanity_check():
    code = clean_vanity_code(request.args.get("code"))
    if not extras.VANITY_RE.match(code):
        return jsonify(error="Vanity codes are 2-32 letters, numbers or hyphens."), 400
    with _vanity_lock:  # one lookup per second so a long watch list can't get this IP rate limited
        wait = 1.0 - (time.time() - _vanity_last[0])
        if wait > 0:
            time.sleep(wait)
        _vanity_last[0] = time.time()
        status, info = vanity_lookup(code)
    return jsonify(code=code, status=status, **info)


@app.post("/api/vanity/test")
@login_required
@json_only
def api_vanity_test():
    a = api_account((request.get_json() or {}).get("account_id"))
    if not a:
        return jsonify(error="Add an account first"), 409
    cfg = db.get_settings(a["id"])
    v = cfg["vanity"]
    hook = v.get("webhook_url") or cfg["giveaway"].get("webhook_url")
    if not hook or not extras.DISCORD_WEBHOOK_RE.match(hook):
        return jsonify(error="No webhook saved yet - add one first."), 400
    body = {"username": v.get("webhook_name") or "Aether Vanity Sniper",
            "embeds": [{"title": "✅ Vanity Sniper Test", "description": "Your webhook is working.", "color": 0x5865F2,
                        "fields": [{"name": "Account", "value": a.get("username") or a["label"], "inline": True}]}]}
    if v.get("webhook_avatar"):
        body["avatar_url"] = v["webhook_avatar"]
    req = urllib.request.Request(hook, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "AetherPanel/2.2"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        return jsonify(error="Discord refused the webhook (deleted, or the URL is wrong)."), 502
    return jsonify(ok=True)


# ------------------------------------------------------------------ quests
QUEST_NEW_HOURS = 72


def quest_view(account_id):
    quests, updated = db.get_quests(account_id)
    runs = db.get_quest_runs(account_id)
    now = datetime.now(timezone.utc)
    out, counts = [], {k: 0 for k in ("all", "new", "available", "running", "paused", "completed", "expired")}
    for q in quests:
        run = runs.get(str(q["id"]))
        fresh = run and time.time() - run["updated"] < 120
        progress = max(float(q.get("progress") or 0), float(run["progress"]) if run else 0)
        expires, starts = parse_iso(q.get("expires_at")), parse_iso(q.get("starts_at"))
        if q.get("completed_at"):
            status = "completed"
        elif expires and expires <= now:
            status = "expired"
        elif run and run["status"] == "running" and fresh:
            status = "running"
        elif q.get("enrolled_at") and (progress > 0 or run):
            status = "paused"
        elif q.get("enrolled_at"):
            status = "available"
        elif starts and (now - starts).total_seconds() < QUEST_NEW_HOURS * 3600:
            status = "new"
        else:
            status = "available"
        counts[status] += 1
        counts["all"] += 1
        out.append({**q, "status": status, "progress": progress,
                    "run_status": run["status"] if run else None, "run_message": run["message"] if run else ""})
    order = {"running": 0, "new": 1, "available": 2, "paused": 3, "completed": 4, "expired": 5}
    out.sort(key=lambda x: (order.get(x["status"], 9), x.get("expires_at") or ""))
    return out, counts, updated


@app.route("/quests")
@login_required
@account_required
def quests_page():
    return render_template("quests.html", a=g.account)


@app.get("/api/quests")
@login_required
def api_quests():
    a = api_account(request.args.get("account_id"))
    if not a:
        return jsonify(quests=[], counts={}, updated=None)
    quests, counts, updated = quest_view(a["id"])
    return jsonify(quests=quests, counts=counts, updated=updated, state=a["state"])


# ------------------------------------------------------------------ crypto market data (CoinGecko, cached)
CG_BASE = "https://api.coingecko.com/api/v3"
CG_KEY = os.getenv("COINGECKO_API_KEY", "")
_cg_cache, _cg_lock, _cg_last = {}, threading.Lock(), [0.0]
VS_CURRENCIES = {"usd", "eur", "gbp", "inr", "jpy", "btc"}
CHART_DAYS = {"1", "7", "14", "30", "90", "180", "365"}


def cg_fetch(path, params):
    """Raw HTTP call (separate so tests can replace it)."""
    url = CG_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"Accept": "application/json", "User-Agent": "AetherPanel/2.0"}
    if CG_KEY:
        headers["x-cg-demo-api-key"] = CG_KEY
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=12) as r:
        return json.loads(r.read().decode())


def cg_get(path, params, ttl):
    """Cached + throttled (about 30 calls/min). Falls back to stale data if the API errors or rate-limits."""
    key = path + "?" + urllib.parse.urlencode(sorted((params or {}).items()))
    hit = _cg_cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    with _cg_lock:
        hit = _cg_cache.get(key)  # another thread may have just filled it
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        wait = 2.0 - (time.time() - _cg_last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            data = cg_fetch(path, params)
            _cg_last[0] = time.time()
        except Exception as exc:
            _cg_last[0] = time.time()
            if hit:
                return hit[1]
            raise RuntimeError("Market data is unavailable right now (rate limited or offline). Try again in a minute.") from exc
        if len(_cg_cache) > 400:
            _cg_cache.clear()
        _cg_cache[key] = (time.time(), data)
        return data


def vs_param():
    vs = request.args.get("vs", "usd").lower()
    return vs if vs in VS_CURRENCIES else "usd"


def crypto_json(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return jsonify(fn(*args, **kwargs))
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 502
    return wrapper


@app.route("/crypto")
@login_required
@account_required
def crypto_page():
    return render_template("crypto.html", a=g.account, settings=page_settings(g.account["id"]))


@app.get("/api/crypto/markets")
@login_required
@crypto_json
def api_crypto_markets():
    vs, q = vs_param(), request.args.get("q", "").strip()[:60]
    try:
        page, per_page = max(int(request.args.get("page", 1)), 1), min(max(int(request.args.get("per_page", 50)), 10), 100)
    except ValueError:
        page, per_page = 1, 50
    params = {"vs_currency": vs, "order": "market_cap_desc", "per_page": per_page, "page": page,
              "sparkline": "true", "price_change_percentage": "1h,24h,7d"}
    if q:
        found = cg_get("/search", {"query": q}, 300).get("coins", [])[:25]
        ids = ",".join(c["id"] for c in found if COIN_ID_RE.match(str(c.get("id", ""))))
        if not ids:
            return {"coins": [], "page": 1, "vs": vs}
        params.update(ids=ids, per_page=25, page=1)
    coins = cg_get("/coins/markets", params, 45)
    slim = [{"id": c["id"], "symbol": c.get("symbol", ""), "name": c.get("name", ""), "image": c.get("image"),
             "rank": c.get("market_cap_rank"), "price": c.get("current_price"), "mcap": c.get("market_cap"),
             "volume": c.get("total_volume"), "ch1h": c.get("price_change_percentage_1h_in_currency"),
             "ch24h": c.get("price_change_percentage_24h_in_currency", c.get("price_change_percentage_24h")),
             "ch7d": c.get("price_change_percentage_7d_in_currency"),
             "spark": ((c.get("sparkline_in_7d") or {}).get("price") or [])[::4]} for c in coins]
    return {"coins": slim, "page": page, "vs": vs}


@app.get("/api/crypto/chart/<coin_id>")
@login_required
@crypto_json
def api_crypto_chart(coin_id):
    days = request.args.get("days", "7")
    if not COIN_ID_RE.match(coin_id) or days not in CHART_DAYS:
        raise RuntimeError("Invalid coin or range")
    data = cg_get(f"/coins/{coin_id}/market_chart", {"vs_currency": vs_param(), "days": days}, 120 if days == "1" else 300)
    prices = data.get("prices") or []
    step = max(len(prices) // 240, 1)  # keep the payload small for phones
    return {"id": coin_id, "days": days, "vs": vs_param(), "prices": prices[::step] + ([prices[-1]] if prices and (len(prices) - 1) % step else [])}


@app.get("/api/crypto/global")
@login_required
@crypto_json
def api_crypto_global():
    d = (cg_get("/global", {}, 180) or {}).get("data", {})
    vs = vs_param()
    return {"mcap": (d.get("total_market_cap") or {}).get(vs), "volume": (d.get("total_volume") or {}).get(vs),
            "btc_dom": (d.get("market_cap_percentage") or {}).get("btc"), "eth_dom": (d.get("market_cap_percentage") or {}).get("eth"),
            "change24h": d.get("market_cap_change_percentage_24h_usd"), "coins": d.get("active_cryptocurrencies"), "vs": vs}


@app.get("/api/crypto/trending")
@login_required
@crypto_json
def api_crypto_trending():
    out = []
    for entry in (cg_get("/search/trending", {}, 300) or {}).get("coins", [])[:15]:
        item = entry.get("item", {})
        data = item.get("data") or {}
        change = (data.get("price_change_percentage_24h") or {}).get("usd")
        out.append({"id": item.get("id"), "name": item.get("name"), "symbol": item.get("symbol"), "image": item.get("small") or item.get("thumb"),
                    "rank": item.get("market_cap_rank"), "price": data.get("price"), "ch24h": change})
    return {"coins": out}


@app.get("/api/crypto/prices")
@login_required
@crypto_json
def api_crypto_prices():
    ids = [i for i in request.args.get("ids", "").split(",") if COIN_ID_RE.match(i)][:60]
    if not ids:
        return {"prices": {}}
    data = cg_get("/simple/price", {"ids": ",".join(sorted(ids)), "vs_currencies": vs_param(), "include_24hr_change": "true"}, 30)
    return {"prices": data, "vs": vs_param()}


# ------------------------------------------------------------------ license enforcement
def enforce_licenses():
    """Stops the workers of key holders whose key expired or was revoked."""
    try:
        valid = licenses.valid_owner_ids()
        for acct in db.list_accounts():
            if acct.get("owner", "admin") != "admin" and acct["owner"] not in valid and procman.is_running(acct["id"]):
                procman.stop(acct["id"])
                db.add_log("SYSTEM", "Bot stopped: the license key expired or was revoked", "warning", account_id=acct["id"])
    except Exception as exc:
        print("license check failed:", exc)


def _license_loop():
    while True:
        time.sleep(60)
        enforce_licenses()


db.init_db()

import musicapi  # noqa: E402  (Spotify / Lavalink search for the Music page)
import theme     # noqa: E402  (dashboard customization)
musicapi.init(app, login_required)
theme.init(app, login_required, json_only, session_ok)

if __name__ == "__main__":
    if DASHBOARD_PASSWORD == "change-this-password":
        print("WARNING: using the default dashboard password. Set DASHBOARD_PASSWORD in your .env file.")
    procman.autostart()
    enforce_licenses()  # don't leave workers running for expired keys
    threading.Thread(target=_license_loop, daemon=True).start()
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "5000")), debug=False)
