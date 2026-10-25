"""
AetherPanel license-key manager + keygen Discord bot.

Keys live in keys.txt (one per line, pipe-separated, human readable):
    # key | duration | created | activated | expires | note | status
    AETHER-AB12-CD34-EF56-GH78 | 7d | 2026-10-11T12:00:00+00:00 | - | - | for bob | unused

* Duration runs from the FIRST dashboard login with that key (the clock starts on activation).
* Durations: 7d, 14d, 30d, 90d ... any <n>d / <n>w / <n>m (30 days) / <n>y, or "lifetime".
* Status: unused -> active -> expired, or revoked.

START THE KEYGEN BOT:
    python manager.py                               just run it - the bot starts instantly, no extra argument needed

CLI (optional, only when you pass arguments):
    python manager.py gen 7d [count] [note...]      generate keys
    python manager.py list [all|active|unused]      list keys
    python manager.py info KEY                      show one key
    python manager.py revoke KEY                    disable a key (its holder is logged out)
    python manager.py run                           same as running it with no arguments
    python manager.py help                          show this text

Keygen bot (needs KEYGEN_BOT_TOKEN and KEYGEN_ADMINS in .env, and the Message Content intent
enabled for the bot in the Discord developer portal):
    !genkey <duration> [count] [note]   !keyinfo <key>   !revoke <key>   !extend <key> <duration>   !keys [valid|all]   !help
Keys are sent to the admin by DM. Only user IDs listed in KEYGEN_ADMINS may use the commands.

The bot talks to Discord directly through aiohttp, so it does not conflict with discord.py-self.
"""
import asyncio
import hashlib
import json
import os
import re
import secrets
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE = Path(__file__).resolve().parent
KEYS_FILE = Path(os.getenv("KEYS_FILE") or (BASE / "keys.txt"))
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I, O, 0, 1
HEADER = ("# Aether license keys - managed by Aether Development.\n"
          "# key | duration | created | activated | expires | note | status\n")
FIELDS = ["key", "duration", "created", "activated", "expires", "note", "status"]
LIFETIME_WORDS = {"lifetime", "life", "perm", "permanent", "forever", "lt"}
_cache = {"mtime": None, "records": []}


# ------------------------------------------------------------------ durations
def parse_duration(text):
    """Returns (label, seconds|None). None seconds = lifetime. Raises ValueError."""
    t = str(text or "").strip().lower()
    if t in LIFETIME_WORDS:
        return "lifetime", None
    m = re.fullmatch(r"(\d{1,4})\s*([dwmy])", t)
    if not m:
        raise ValueError("Duration must look like 7d, 2w, 1m, 1y or lifetime")
    n, unit = int(m.group(1)), m.group(2)
    if n < 1:
        raise ValueError("Duration must be at least 1")
    days = n * {"d": 1, "w": 7, "m": 30, "y": 365}[unit]
    if days > 3650:
        raise ValueError("Duration too long - use 'lifetime'")
    return f"{n}{unit}", days * 86400


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _parse(value):
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ file access (atomic + cross-process lock)
@contextmanager
def _lock(timeout=10.0):
    lock = Path(str(KEYS_FILE) + ".lock")
    deadline = time.time() + timeout
    while True:
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 15:  # stale lock from a crashed process
                    os.rmdir(lock)
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise TimeoutError("keys.txt is locked by another process")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.rmdir(lock)
        except OSError:
            pass


def _read():
    records = []
    if not KEYS_FILE.exists():
        return records
    for line in KEYS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        parts += [""] * (len(FIELDS) - len(parts))
        rec = dict(zip(FIELDS, parts[:len(FIELDS)]))
        if rec["key"]:
            records.append(rec)
    return records


def _write(records):
    lines = [HEADER]
    for r in records:
        lines.append(" | ".join((r.get(f) or "-") if f != "note" else (r.get(f) or "") for f in FIELDS) + "\n")
    tmp = Path(str(KEYS_FILE) + ".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    os.replace(tmp, KEYS_FILE)
    _cache["mtime"] = None


def load():
    """Cached by file mtime so the dashboard can validate on every request cheaply."""
    try:
        mtime = KEYS_FILE.stat().st_mtime_ns
    except OSError:
        return []
    if _cache["mtime"] != mtime:
        _cache["records"] = _read()
        _cache["mtime"] = mtime
    return [dict(r) for r in _cache["records"]]


def normalize(key):
    return re.sub(r"\s+", "", str(key or "")).upper()


def owner_id(key):
    return "key:" + hashlib.sha256(normalize(key).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ record state
def state(rec, now=None):
    """unused | active | expired | revoked"""
    now = now or _now()
    if rec.get("status") == "revoked":
        return "revoked"
    if not _parse(rec.get("activated")):
        return "unused"
    if rec.get("expires") in ("never", "lifetime"):
        return "active"
    exp = _parse(rec.get("expires"))
    return "expired" if exp and exp <= now else "active"


def remaining_text(rec, now=None):
    now = now or _now()
    st = state(rec, now)
    if st == "revoked":
        return "revoked"
    if st == "unused":
        return f"unused ({rec.get('duration')})"
    if rec.get("expires") in ("never", "lifetime"):
        return "lifetime"
    exp = _parse(rec.get("expires"))
    if not exp or exp <= now:
        return "expired"
    secs = int((exp - now).total_seconds())
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    return f"{d}d {h}h left" if d else f"{h}h {rem // 60}m left"


# ------------------------------------------------------------------ operations
def generate(duration, count=1, note=""):
    label, _ = parse_duration(duration)
    count = max(1, min(int(count), 50))
    note = re.sub(r"[|\r\n]", " ", str(note or "")).strip()[:80]
    created = []
    with _lock():
        records = _read()
        existing = {r["key"] for r in records}
        for _ in range(count):
            while True:
                key = "AETHER-" + "-".join("".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(4))
                if key not in existing:
                    break
            existing.add(key)
            rec = {"key": key, "duration": label, "created": _iso(_now()), "activated": "-", "expires": "-",
                   "note": note, "status": "unused"}
            records.append(rec)
            created.append(rec)
        _write(records)
    return created


def find(key):
    k = normalize(key)
    for rec in load():
        if rec["key"] == k:
            return rec
    return None


def record_for_owner(owner):
    for rec in load():
        if owner_id(rec["key"]) == owner:
            return rec
    return None


def valid_owner_ids():
    return {owner_id(r["key"]) for r in load() if state(r) in ("active", "unused")}


def validate(key, activate=True):
    """Returns (ok, message, record). The first successful login activates the key."""
    k = normalize(key)
    if not re.fullmatch(r"AETHER(-[A-Z0-9]{4}){4}", k):
        return False, "Incorrect password or license key.", None
    with _lock():
        records = _read()
        rec = next((r for r in records if r["key"] == k), None)
        if rec is None:
            return False, "Incorrect password or license key.", None
        st = state(rec)
        if st == "revoked":
            return False, "This license key has been revoked.", rec
        if st == "expired":
            return False, "This license key has expired.", rec
        if st == "unused":
            if not activate:
                return True, "unused", rec
            _, seconds = parse_duration(rec["duration"])
            now = _now()
            rec["activated"] = _iso(now)
            rec["expires"] = "lifetime" if seconds is None else _iso(now + timedelta(seconds=seconds))
            rec["status"] = "active"
            _write(records)
        return True, "ok", dict(rec)


def revoke(key):
    k = normalize(key)
    with _lock():
        records = _read()
        for rec in records:
            if rec["key"] == k:
                rec["status"] = "revoked"
                _write(records)
                return True
    return False


def extend(key, duration):
    """Adds time to a key (or switches it to lifetime)."""
    label, seconds = parse_duration(duration)
    k = normalize(key)
    with _lock():
        records = _read()
        for rec in records:
            if rec["key"] != k:
                continue
            if rec.get("status") == "revoked":
                return False, "Key is revoked"
            if rec.get("expires") in ("never", "lifetime") or (rec.get("duration") == "lifetime" and seconds is not None):
                return True, "Already lifetime"
            if seconds is None:
                rec["duration"], rec["expires"] = "lifetime", "lifetime" if state(rec) != "unused" else "-"
            elif state(rec) == "unused":
                _, base = parse_duration(rec["duration"])
                rec["duration"] = rec["duration"] if base is None else f"{(base + seconds) // 86400}d"
            else:
                exp = _parse(rec["expires"]) or _now()
                rec["expires"] = _iso(max(exp, _now()) + timedelta(seconds=seconds))
                rec["status"] = "active"
            _write(records)
            return True, "ok"
    return False, "Key not found"


def list_keys(which="all"):
    out = []
    for rec in load():
        st = state(rec)
        if which == "all" or st == which:
            out.append({**rec, "state": st, "remaining": remaining_text(rec)})
    return out


def describe(rec):
    return (f"`{rec['key']}`\nDuration: {rec['duration']} · {state(rec)} · {remaining_text(rec)}\n"
            f"Created: {rec['created'][:10]}"
            + (f" · Activated: {rec['activated'][:10]}" if _parse(rec.get("activated")) else "")
            + (f" · Note: {rec['note']}" if rec.get("note") else ""))


# ------------------------------------------------------------------ keygen Discord bot (pure aiohttp gateway client)
API = "https://discord.com/api/v10"
GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)  # guilds, guild messages, DMs, message content
FATAL_CLOSE = {
    4004: "Discord rejected the TOKEN. Developer Portal -> your app -> Bot -> Reset Token, then paste that BOT token "
          "(not the client secret, no quotes, no 'Bot ' prefix) into KEYGEN_BOT_TOKEN.",
    4013: "Invalid intents - this should not happen, please report it.",
    4014: "Message Content Intent is OFF. Developer Portal -> your app -> Bot -> turn ON 'Message Content Intent' "
          "(under Privileged Gateway Intents) -> Save Changes, then start the bot again.",
}


class FatalGatewayError(Exception):
    pass


class KeygenBot:
    def __init__(self, token, admins, prefix="!"):
        self.token, self.admins, self.prefix = token, {str(a) for a in admins}, prefix
        self.http = None
        self.seq = None

    # ----- REST
    async def api(self, method, path, **kw):
        for _ in range(4):
            async with self.http.request(method, API + path, headers={"Authorization": f"Bot {self.token}"}, **kw) as r:
                if r.status == 429:
                    data = await r.json()
                    await asyncio.sleep(float(data.get("retry_after", 1)) + 0.2)
                    continue
                if r.status >= 400:
                    raise RuntimeError(f"Discord API {r.status}: {(await r.text())[:150]}")
                return await r.json() if r.content_type == "application/json" else None
        raise RuntimeError("Rate limited")

    async def send(self, channel_id, content):
        return await self.api("POST", f"/channels/{channel_id}/messages", json={"content": content[:2000]})

    async def dm(self, user_id, content):
        ch = await self.api("POST", "/users/@me/channels", json={"recipient_id": str(user_id)})
        return await self.send(ch["id"], content)

    async def dm_chunks(self, user_id, text):
        chunk = ""
        for line in text.splitlines(keepends=True):
            if len(chunk) + len(line) > 1900:
                await self.dm(user_id, chunk)
                chunk = ""
            chunk += line
        if chunk:
            await self.dm(user_id, chunk)

    # ----- commands
    async def handle(self, msg):
        author = msg.get("author") or {}
        content = (msg.get("content") or "").strip()
        if author.get("bot") or not content.startswith(self.prefix):
            return
        name, *args = content[len(self.prefix):].split()
        name, uid, channel = name.lower(), str(author.get("id")), msg["channel_id"]
        if name not in ("genkey", "keyinfo", "revoke", "keys", "extend", "help"):
            return
        if uid not in self.admins:
            return await self.send(channel, "⛔ You are not allowed to use the key manager.")
        try:
            if name == "help":
                p = self.prefix
                return await self.send(channel, f"**Key manager**\n`{p}genkey <7d|30d|90d|1y|lifetime> [count] [note]` · "
                                       f"`{p}keyinfo <key>` · `{p}revoke <key>` · `{p}extend <key> <duration>` · `{p}keys [valid|all|active|unused|expired|revoked]`")
            if name == "genkey":
                if not args:
                    return await self.send(channel, f"Usage: `{self.prefix}genkey <duration> [count] [note]` - e.g. `{self.prefix}genkey 7d 3 for bob`")
                count, note = 1, args[1:]
                if len(args) > 1 and args[1].isdigit():
                    count, note = int(args[1]), args[2:]
                keys = generate(args[0], count, " ".join(note))
                body = "\n".join(f"`{k['key']}`" for k in keys)
                try:
                    await self.dm(uid, f"🔑 **{len(keys)} key(s)** · duration **{keys[0]['duration']}** (starts on first login)\n{body}")
                    await self.send(channel, f"✅ Generated {len(keys)} key(s) - sent to your DMs.")
                except Exception:
                    await self.send(channel, f"✅ Generated, but I could not DM you (open your DMs):\n{body}")
            elif name == "keyinfo":
                rec = find(args[0]) if args else None
                await self.send(channel, describe(rec) if rec else "Key not found.")
            elif name == "revoke":
                await self.send(channel, "✅ Revoked." if args and revoke(args[0]) else "Key not found.")
            elif name == "extend":
                if len(args) < 2:
                    return await self.send(channel, f"Usage: `{self.prefix}extend <key> <duration>`")
                ok, text = extend(args[0], args[1])
                await self.send(channel, "✅ Extended." if ok else f"❌ {text}")
            elif name == "keys":
                which = args[0].lower() if args else "valid"
                if which == "valid":  # default: every key that can still be used (unused + active)
                    rows = [r for r in list_keys("all") if r["state"] in ("unused", "active")]
                else:
                    rows = list_keys(which if which in ("all", "active", "unused", "expired", "revoked") else "all")
                text = "\n".join(f"`{r['key']}` · {r['duration']} · {r['remaining']}" + (f" · {r['note']}" if r['note'] else "") for r in rows)
                await self.send(channel, f"📬 Sent {len(rows)} key(s) to your DMs." if rows else "No keys.")
                if rows:
                    await self.dm_chunks(uid, text)
        except ValueError as exc:
            await self.send(channel, f"❌ {exc}")
        except Exception as exc:
            print("keygen command error:", exc)
            await self.send(channel, "❌ Something went wrong - check the console.")

    # ----- gateway
    async def _heartbeat(self, ws, interval):
        await asyncio.sleep(interval * 0.5)
        while True:
            await ws.send_json({"op": 1, "d": self.seq})
            await asyncio.sleep(interval)

    async def _connection(self):
        import aiohttp
        hb = None
        print("Connecting to Discord...", flush=True)
        async with self.http.ws_connect(GATEWAY, max_msg_size=0) as ws:
            async for raw in ws:
                if raw.type != aiohttp.WSMsgType.TEXT:
                    break
                data = json.loads(raw.data)
                if data.get("s") is not None:
                    self.seq = data["s"]
                op = data["op"]
                if op == 10:
                    print("Connected, logging in...", flush=True)
                    hb = asyncio.ensure_future(self._heartbeat(ws, data["d"]["heartbeat_interval"] / 1000))
                    await ws.send_json({"op": 2, "d": {"token": self.token, "intents": INTENTS,
                                                       "properties": {"os": sys.platform, "browser": "aether", "device": "aether"}}})
                elif op == 1:
                    await ws.send_json({"op": 1, "d": self.seq})
                elif op in (7, 9):
                    print(f"Discord asked us to reconnect (op {op}).", flush=True)
                    break
                elif op == 0:
                    if data["t"] == "READY":
                        print("Keygen bot online as", data["d"]["user"]["username"], flush=True)
                    elif data["t"] == "MESSAGE_CREATE":
                        asyncio.ensure_future(self.handle(data["d"]))
            code = ws.close_code
        if hb:
            hb.cancel()
        if code in FATAL_CLOSE:  # a rejected login would otherwise retry silently forever
            raise FatalGatewayError(f"[{code}] {FATAL_CLOSE[code]}")
        print(f"Connection closed (code {code}), reconnecting in 5s...", flush=True)

    async def run(self):
        import aiohttp
        async with aiohttp.ClientSession() as self.http:
            while True:
                try:
                    await self._connection()
                except FatalGatewayError as exc:
                    raise SystemExit(f"\nKEYGEN BOT STOPPED: {exc}")
                except Exception as exc:
                    print("Gateway error:", repr(exc), flush=True)
                await asyncio.sleep(5)


def run_bot():
    token = re.sub(r"^bot\s+", "", os.getenv("KEYGEN_BOT_TOKEN", "").strip().strip("\"'"), flags=re.I)
    admins = [a for a in re.split(r"[,\s]+", os.getenv("KEYGEN_ADMINS", "")) if a]
    if not token:
        raise SystemExit("Set KEYGEN_BOT_TOKEN in your .env file first.")
    if not admins:
        raise SystemExit("Set KEYGEN_ADMINS (comma-separated Discord user IDs allowed to generate keys) in your .env file.")
    asyncio.run(KeygenBot(token, admins, os.getenv("KEYGEN_PREFIX", "!")).run())


# ------------------------------------------------------------------ CLI
def main(argv):
    if not argv:  # `python manager.py` -> start the bot right away
        return run_bot()
    cmd = argv[0].lower()
    try:
        if cmd == "gen" and len(argv) >= 2:
            count, note = 1, argv[2:]
            if len(argv) > 2 and argv[2].isdigit():
                count, note = int(argv[2]), argv[3:]
            for rec in generate(argv[1], count, " ".join(note)):
                print(rec["key"], "-", rec["duration"])
        elif cmd == "list":
            for r in list_keys(argv[1] if len(argv) > 1 else "all"):
                print(f"{r['key']}  {r['duration']:>8}  {r['state']:8}  {r['remaining']}  {r['note']}")
        elif cmd == "info" and len(argv) > 1:
            rec = find(argv[1])
            print(describe(rec) if rec else "Key not found")
        elif cmd == "revoke" and len(argv) > 1:
            print("Revoked" if revoke(argv[1]) else "Key not found")
        elif cmd == "extend" and len(argv) > 2:
            print(*extend(argv[1], argv[2]))
        elif cmd == "run":
            run_bot()
        else:  # help / anything unknown
            print(__doc__)
    except (ValueError, TimeoutError) as exc:
        print("Error:", exc)


if __name__ == "__main__":
    main(sys.argv[1:])
