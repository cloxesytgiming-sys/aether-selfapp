"""
AetherPanel bot worker (discord.py-self) - one process per account.

Normally the dashboard starts and stops this for you (Accounts page).
Manual start:   python selfbot.py --account 1
Legacy start:   DISCORD_USER_TOKEN=... python selfbot.py     (creates an account called "env")

Privacy: message content is never written to the logs table. Notifications keep a short
preview of DMs/pings for your dashboard inbox. !snipe keeps deleted messages in memory only.
Selfbots break Discord's Terms of Service - use an alt account.
"""
import argparse
import asyncio
import os
import platform
import random
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord.ext import commands, tasks

import db
import devices
import extras
import tools

ACCOUNT_ID = None
S = {}                  # this account's settings (reloaded from the DB whenever the dashboard changes them)
last_rev = 0.0
start_time = time.time()
ready_once = False

sniped = {}             # channel_id -> (author, content, created_at)      memory only
afk_notified = {}       # user_id -> timestamp of the last AFK notice
seen_codes = set()      # gift codes already attempted
AFK_COOLDOWN = 300
MARK = "\u200b\u2063"  # invisible tail on everything the bot sends by itself, so it never counts as *you* being active
AFK_PREFIXES = ("I'm AFK", "AFK enabled", "AFK removed")
WIN_WEBHOOK_NAME = "𝗚𝗪𝗬 𝗦𝗡𝗜𝗣𝗘𝗥 𝗕𝗬 𝗢𝗞𝗜𝗗𝗔𝗔"
WIN_WEBHOOK_AVATAR = "https://cdn.discordapp.com/attachments/1542100234182590554/1555078349078921236/249093b969595907b23f9fb185c42959b79aee67fa1b63c843dcf2407815f440.png?backend=b2&ex=6abf3712&is=6abde592&hm=b5867f4862efae9adde2e5d68a5f150e608b18b65d66c229901f0c5bf4c20893&"
gw_entered, gw_inflight, gw_attempts, win_seen = set(), set(), {}, set()
quest_tasks = {}
_quest_sem = []
hb_ticks = 0

ACTIVITY_TYPES = {
    "playing": discord.ActivityType.playing,
    "listening": discord.ActivityType.listening,
    "watching": discord.ActivityType.watching,
    "competing": discord.ActivityType.competing,
}
GIFT_RE = re.compile(r"(?:discord\.gift|discord(?:app)?\.com/gifts?)/([A-Za-z0-9]{16,24})", re.I)
JOIN_WORDS = ("enter", "join", "participate", "react", "🎉")


# ------------------------------------------------------------------ helpers
def getv(obj, name, default=None):
    """getattr that never raises and treats None as 'missing'."""
    try:
        value = getattr(obj, name)
    except Exception:
        return default
    return default if value is None else value


def asset_url(asset, size=None):
    if asset is None:
        return None
    try:
        if size:
            asset = asset.with_size(size)
    except Exception:
        pass
    try:
        return asset.url
    except Exception:
        return str(asset)


def iso(dt):
    try:
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None
    except Exception:
        return None


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "-"


def fmt_duration(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def rel_type(rel):
    t = getattr(rel, "type", None)
    return getattr(t, "name", str(t))


def mask_email(email):
    if not email or "@" not in email:
        return None
    name, domain = email.split("@", 1)
    return f"{name[:2]}{'*' * max(len(name) - 2, 1)}@{domain}"


def result(title=None, fields=None, text=None, image=None, banner=None):
    return {k: v for k, v in
            {"title": title, "fields": fields or [], "text": text, "image": image, "banner": banner}.items() if v}


def to_chat(res):
    parts = []
    if res.get("title"):
        parts.append(f"**{res['title']}**")
    for key, value in res.get("fields", []):
        parts.append(f"{key}: `{value}`")
    if res.get("text"):
        parts.append(res["text"])
    for k in ("image", "banner"):
        if res.get(k):
            parts.append(res[k])
    return "\n".join(parts)[:1990]


def log(category, message, level="info", **kw):
    db.add_log(category, message, level, account_id=ACCOUNT_ID, **kw)


def gfields(guild):
    if guild is None:
        return {"guild_name": "Direct message", "guild_id": ""}
    return {"guild_name": guild.name, "guild_id": str(guild.id)}


def automated(message):
    """True for messages this bot wrote itself (AFK notice, auto-replies, command output)."""
    content = message.content or ""
    return content.endswith(MARK) or content.startswith(AFK_PREFIXES)


async def say(target, content, **kw):
    return await target.send(f"{content}{MARK}", **kw)


def parse_dt(value):
    """ISO timestamp (Discord sometimes sends 7+ fractional digits) -> aware datetime, or None."""
    if not value:
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", str(value).replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def spawn(coro):
    task = asyncio.ensure_future(coro)

    def _done(t):
        if not t.cancelled() and t.exception():
            print("Background task failed:", repr(t.exception()))
    task.add_done_callback(_done)
    return task


def load_settings():
    global S
    S = db.get_settings(ACCOUNT_ID)


def update_setting(key, value):
    global last_rev
    S[key] = value
    db.set_setting(ACCOUNT_ID, key, value)
    last_rev = db.settings_rev(ACCOUNT_ID)  # our own write: no need for the watcher to reload it


def get_prefix(_bot, _message):
    return S.get("prefix") or "!"


async def get_channel_any(channel_id):
    channel_id = int(channel_id)
    return bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)


DEV = devices.DeviceManager(lambda: db.get_token(ACCOUNT_ID), lambda msg, level="info": log("SYSTEM", msg, level))


async def apply_presence():
    p = S["presence"]
    try:  # extra VR / mobile / console session (no-op for desktop or when unchanged)
        await DEV.sync(S["device"], p.get("status", "online"))
    except Exception as exc:
        print("Device spoof sync failed:", exc)
    status = {"online": discord.Status.online, "idle": discord.Status.idle,
              "dnd": discord.Status.dnd, "invisible": discord.Status.invisible}.get(p.get("status"), discord.Status.online)
    try:
        rich = await X.rich_activity()
    except Exception as exc:
        print("Rich presence failed, using the simple activity:", exc)
        rich = None
    if rich is not None:
        try:
            await bot.change_presence(status=status, activity=rich)
        except Exception as exc:
            print("Could not change presence:", exc)
        return
    kind, text = p.get("kind", "none"), p.get("text", "")
    activity = None
    if kind == "streaming" and text:
        activity = discord.Streaming(name=text, url=p.get("url") or "https://twitch.tv/discord")
    elif kind in ACTIVITY_TYPES and text:
        activity = discord.Activity(type=ACTIVITY_TYPES[kind], name=text)
    try:
        await bot.change_presence(status=status, activity=activity)
    except Exception as exc:
        print("Could not change presence:", exc)


# ------------------------------------------------------------------ bot
class AetherSelfBot(commands.Bot):
    async def close(self):
        try:
            db.set_status(ACCOUNT_ID, online=0, pid=None)
            log("SYSTEM", "Bot disconnected", "warning")
        except Exception:
            pass
        await super().close()


bot = AetherSelfBot(command_prefix=get_prefix, self_bot=True, help_command=None)


# ------------------------------------------------------------------ snapshot (account overview)
async def build_snapshot():
    me = bot.user
    profile = None
    try:
        profile = await asyncio.wait_for(bot.fetch_user_profile(me.id), 15)
    except Exception as exc:
        print("Profile fetch failed:", exc)

    banner = getv(profile, "banner") or getv(me, "banner")
    deco = getv(me, "avatar_decoration") or getv(profile, "avatar_decoration")
    accent = (getv(profile, "accent_colour") or getv(me, "accent_colour") or getv(me, "accent_color")
              or getv(profile, "accent_color"))

    pt = getv(me, "premium_type")
    pt_name = getattr(pt, "name", str(pt)) if pt else "none"
    nitro = {"type": pt_name, "since": iso(getv(profile, "premium_since")),
             "boosting_since": iso(getv(profile, "premium_guild_since")), "renews": None, "status": None}
    try:
        for sub in await asyncio.wait_for(bot.subscriptions(), 10):
            end = getv(sub, "current_period_end")
            if end:
                nitro["renews"] = iso(end)
                nitro["status"] = getattr(getv(sub, "status"), "name", None)
                break
    except Exception:
        pass

    badges = []
    try:
        for b in getv(profile, "badges", []) or []:
            badges.append(str(getv(b, "description") or getv(b, "id") or b))
    except Exception:
        pass
    if not badges:
        try:
            badges = [n.replace("_", " ").title() for n, v in me.public_flags if v]
        except Exception:
            pass

    rels = list(getv(bot, "relationships", []) or [])
    count = lambda name: sum(1 for r in rels if rel_type(r) == name)
    guilds = list(bot.guilds)
    servers = sorted(({"id": str(x.id), "name": x.name, "members": x.member_count or 0,
                       "icon": asset_url(getv(x, "icon"), 64), "owner": x.owner_id == me.id} for x in guilds),
                     key=lambda d: d["name"].lower())[:400]
    snap = {
        "id": str(me.id),
        "username": me.name,
        "display_name": me.display_name,
        "global_name": getv(me, "global_name"),
        "avatar_url": asset_url(getv(me, "display_avatar"), 512),
        "banner_url": asset_url(banner, 600),
        "decoration_url": asset_url(deco, 160),
        "accent": str(accent) if accent else None,
        "bio": getv(profile, "bio"),
        "badges": badges,
        "created_at": iso(me.created_at),
        "nitro": nitro,
        "verified": bool(getv(me, "verified", False)),
        "mfa": bool(getv(me, "mfa_enabled", False)),
        "locale": str(getv(me, "locale", "")) or None,
        "email": mask_email(getv(me, "email")),
        "has_phone": bool(getv(me, "phone")),
        "counts": {"servers": len(guilds), "friends": count("friend"), "incoming": count("incoming_request"),
                   "outgoing": count("outgoing_request"), "blocked": count("blocked"),
                   "dms": len(getv(bot, "private_channels", []) or []),
                   "members": sum(x.member_count or 0 for x in guilds)},
        "servers": servers,
        "latency_ms": round(bot.latency * 1000) if bot.latency == bot.latency else None,
        "connected_at": iso(datetime.fromtimestamp(start_time, timezone.utc)),
        "library": f"discord.py-self {discord.__version__}",
        "python": platform.python_version(),
    }
    return snap


async def refresh_snapshot():
    snap = await build_snapshot()
    db.save_snapshot(ACCOUNT_ID, snap)
    db.update_account(ACCOUNT_ID, user_id=snap["id"], username=snap["username"],
                      display_name=snap["display_name"], avatar_url=snap["avatar_url"])
    db.set_status(ACCOUNT_ID, online=1, username=str(bot.user), guild_count=snap["counts"]["servers"],
                  user_count=snap["counts"]["members"])
    return snap


# ------------------------------------------------------------------ services (shared by chat commands + dashboard)
async def svc_ping():
    t0 = time.perf_counter()
    rest = None
    try:
        await bot.fetch_user(bot.user.id)
        rest = f"{(time.perf_counter() - t0) * 1000:.0f} ms"
    except Exception:
        pass
    fields = [["Gateway", f"{bot.latency * 1000:.0f} ms"]]
    if rest:
        fields.append(["REST API", rest])
    return result("Pong", fields)


async def svc_info():
    me = bot.user
    return result("Account info", [
        ["Account", str(me)], ["ID", me.id], ["Uptime", fmt_duration(time.time() - start_time)],
        ["Servers", len(bot.guilds)], ["Latency", f"{bot.latency * 1000:.0f} ms"],
        ["Python", platform.python_version()], ["Library", f"discord.py-self {discord.__version__}"]])


async def svc_user(user_id):
    uid = int(user_id)
    user = bot.get_user(uid) or await bot.fetch_user(uid)
    profile = None
    try:
        profile = await asyncio.wait_for(bot.fetch_user_profile(uid), 10)
    except Exception:
        pass
    banner = getv(profile, "banner") or getv(user, "banner")
    fields = [["Username", user.name], ["Display name", user.display_name], ["ID", user.id],
              ["Created", fmt_dt(user.created_at)], ["Bot", user.bot]]
    if getv(profile, "bio"):
        fields.append(["Bio", profile.bio])
    if getv(profile, "premium_since"):
        fields.append(["Nitro since", fmt_dt(profile.premium_since)])
    return result(str(user), fields, image=asset_url(getv(user, "display_avatar"), 512), banner=asset_url(banner, 600))


async def svc_server(guild_id):
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        raise ValueError("Server not found in this account")
    return result(guild.name, [
        ["ID", guild.id], ["Owner ID", guild.owner_id], ["Members", guild.member_count],
        ["Channels", len(guild.channels)], ["Roles", len(guild.roles)],
        ["Boosts", guild.premium_subscription_count], ["Created", fmt_dt(guild.created_at)]],
        image=asset_url(getv(guild, "icon"), 256))


async def svc_avatar(user_id=None):
    user = bot.user if not user_id else (bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id)))
    fields = [["User", str(user)]]
    deco = asset_url(getv(user, "avatar_decoration"), 160)
    if deco:
        fields.append(["Decoration", deco])
    return result("Avatar", fields, image=asset_url(getv(user, "display_avatar"), 1024))


async def svc_purge(channel_id, amount):
    channel = await get_channel_any(channel_id)
    amount = min(max(int(amount), 1), 100)
    deleted = 0
    async for msg in channel.history(limit=500):
        if deleted >= amount:
            break
        if msg.author.id == bot.user.id:
            try:
                await msg.delete()
                deleted += 1
                await asyncio.sleep(0.8)  # stay gentle on rate limits
            except discord.HTTPException:
                pass
    return result("Purge finished", [["Deleted", deleted]])


def svc_snipe(channel_id):
    data = sniped.get(int(channel_id))
    if not data:
        return result("Nothing to snipe", text="No deleted message cached for that channel since the bot started.")
    author, content, created = data
    return result("Sniped message", [["Author", author], ["Deleted message sent", fmt_dt(created)]],
                  text=content or "(no cached content)")


async def svc_send(channel_id, content):
    content = (content or "").strip()
    if not content:
        raise ValueError("Message is empty")
    channel = await get_channel_any(channel_id)
    msg = await channel.send(content[:2000])
    return result("Sent", [["Message ID", msg.id]])


# ------------------------------------------------------------------ dashboard actions (jobs)
async def act_ping(_p): return await svc_ping()
async def act_info(_p): return await svc_info()
async def act_user(p): return await svc_user(p["user_id"])
async def act_server(p): return await svc_server(p["guild_id"])
async def act_avatar(p): return await svc_avatar(p.get("user_id"))
async def act_purge(p): return await svc_purge(p["channel_id"], p.get("amount", 10))
async def act_snipe(p): return svc_snipe(p["channel_id"])
async def act_send(p): return await svc_send(p["channel_id"], p.get("content"))


async def act_refresh(_p):
    await refresh_snapshot()
    return result("Account data refreshed")


async def act_channels(p):
    target = str(p.get("guild_id", ""))
    if target == "dm":
        return {"channels": [{"id": c["id"], "name": c["name"]} for c in dm_channels()]}
    guild = bot.get_guild(int(target))
    if guild is None:
        raise ValueError("Server not found")
    chans = sorted(guild.text_channels, key=lambda c: getv(c, "position", 0))[:400]
    return {"channels": [{"id": str(c.id), "name": "#" + c.name} for c in chans]}


def dm_channels():
    items = []
    for ch in getv(bot, "private_channels", []) or []:
        if isinstance(ch, discord.DMChannel):
            rec = ch.recipient
            name, avatar = (rec.display_name, asset_url(getv(rec, "display_avatar"), 64)) if rec else ("Unknown user", None)
            group = False
        else:
            names = ", ".join(u.display_name for u in getv(ch, "recipients", []) or [])
            name, avatar, group = (getv(ch, "name") or names or "Group DM"), asset_url(getv(ch, "icon"), 64), True
        last = getv(ch, "last_message_id", 0) or 0
        items.append({"id": str(ch.id), "name": name, "avatar": avatar, "group": group, "last_id": int(last),
                      "last_at": iso(discord.utils.snowflake_time(last)) if last else None})
    items.sort(key=lambda d: d["last_id"], reverse=True)
    return items[:150]


async def act_dm_list(_p):
    return {"channels": dm_channels()}


async def act_dm_history(p):
    channel = await get_channel_any(p["channel_id"])
    limit = min(max(int(p.get("limit", 40)), 1), 100)
    msgs = [m async for m in channel.history(limit=limit)]
    msgs.reverse()
    return {"messages": [{
        "id": str(m.id), "author": m.author.display_name, "author_id": str(m.author.id),
        "mine": m.author.id == bot.user.id, "content": (m.content or "")[:2000],
        "ts": iso(m.created_at), "attachments": [a.url for a in m.attachments][:6],
        "embeds": len(m.embeds)} for m in msgs]}


def _user_row(rel):
    u = rel.user
    return {"id": str(u.id), "username": u.name, "display_name": u.display_name,
            "avatar": asset_url(getv(u, "display_avatar"), 64), "since": iso(getv(rel, "since"))}


async def act_friends(_p):
    out = {"friends": [], "incoming": [], "outgoing": [], "blocked": []}
    buckets = {"friend": "friends", "incoming_request": "incoming", "outgoing_request": "outgoing", "blocked": "blocked"}
    for rel in list(getv(bot, "relationships", []) or []):
        key = buckets.get(rel_type(rel))
        if key:
            out[key].append(_user_row(rel))
    out["friends"].sort(key=lambda d: d["display_name"].lower())
    return out


def find_relationship(user_id):
    uid = int(user_id)
    for rel in list(getv(bot, "relationships", []) or []):
        if rel.user.id == uid:
            return rel
    raise ValueError("No relationship with that user")


async def act_friend_accept(p):
    rel = find_relationship(p["user_id"])
    await rel.accept()
    return result("Friend request accepted", [["User", str(rel.user)]])


async def act_friend_remove(p):  # unfriend, decline an incoming request, or cancel an outgoing one
    rel = find_relationship(p["user_id"])
    await rel.delete()
    return result("Removed", [["User", str(rel.user)]])


async def act_friend_add(p):
    target = str(p.get("user", "")).strip().lstrip("@")
    if not target:
        raise ValueError("Enter a username or user ID")
    if target.isdigit():
        user = await bot.fetch_user(int(target))
        await user.send_friend_request()
        return result("Friend request sent", [["User", str(user)]])
    await bot.send_friend_request(target)
    return result("Friend request sent", [["User", target]])


# ------------------------------------------------------------------ moderation
TIMEOUT_RE = re.compile(r"^(\d+)([smhdw])$", re.I)


def parse_timeout(text):
    m = TIMEOUT_RE.match(str(text or "").strip())
    if not m:
        raise ValueError("Duration must look like 30m, 2h or 1d (max 28d)")
    delta = timedelta(seconds=int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2).lower()])
    if not timedelta(seconds=1) <= delta <= timedelta(days=28):
        raise ValueError("A timeout must be between 1 second and 28 days")
    return delta


async def svc_mod(action, guild_id, user_id, reason="", duration=None):
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        raise ValueError("Server not found on this account")
    uid = int(user_id)
    reason = (str(reason or "").strip()[:400]) or None  # shows up in the server's audit log
    target = discord.Object(id=uid)
    extra = []
    if action == "ban":
        await guild.ban(target, reason=reason)
        verb = "Banned"
    elif action == "unban":
        await guild.unban(target, reason=reason)
        verb = "Unbanned"
    elif action == "kick":
        await guild.kick(target, reason=reason)
        verb = "Kicked"
    elif action in ("mute", "unmute"):
        member = guild.get_member(uid) or await guild.fetch_member(uid)
        if action == "mute":
            delta = parse_timeout(duration or "10m")
            await member.timeout(delta, reason=reason)
            verb, extra = "Muted", [["Duration", str(duration or "10m")]]
        else:
            await member.timeout(None, reason=reason)
            verb = "Unmuted"
    else:
        raise ValueError("Unknown moderation action")
    log("MOD", f"{verb} user {uid} in {guild.name}", actor=str(uid), actor_id=str(uid), **gfields(guild))
    return result(verb, [["User ID", uid], ["Server", guild.name], ["Reason", reason or "-"]] + extra)


async def act_mod(p):
    return await svc_mod(p["action"], p["guild_id"], p["user_id"], p.get("reason", ""), p.get("duration"))


# ------------------------------------------------------------------ quests
try:
    from discord.http import Route
except Exception:  # pragma: no cover - library without raw HTTP access
    Route = None

QUEST_PRIORITY = ("PLAY_ON_DESKTOP", "WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE")


def quest_sem():
    if not _quest_sem:
        _quest_sem.append(asyncio.Semaphore(2))  # at most two quests at a time
    return _quest_sem[0]


async def qapi(method, path, json=None, **fmt):
    if Route is None:
        raise RuntimeError("This library version cannot make raw API calls")
    route = Route(method, path, **fmt)
    return await (bot.http.request(route, json=json) if json is not None else bot.http.request(route))


def parse_quest(q):
    cfg = q.get("config") or {}
    us = q.get("user_status") or {}
    tasks_cfg = (cfg.get("task_config") or cfg.get("task_config_v2") or {}).get("tasks") or {}
    msgs, app = cfg.get("messages") or {}, cfg.get("application") or {}
    rewards = (cfg.get("rewards_config") or {}).get("rewards") or []
    supported = next((t for t in QUEST_PRIORITY if t in tasks_cfg), None)
    task = supported or (next(iter(tasks_cfg), None))
    return {
        "id": str(q.get("id") or cfg.get("id")),
        "name": msgs.get("quest_name") or msgs.get("game_title") or app.get("name") or "Quest",
        "game": msgs.get("game_title") or app.get("name") or "",
        "publisher": msgs.get("game_publisher") or "",
        "application_id": str(app.get("id") or ""),
        "reward": ", ".join(str((r.get("messages") or {}).get("name") or r.get("name") or "Reward") for r in rewards[:2]),
        "task": task, "tasks": list(tasks_cfg), "supported": supported is not None,
        "target": float((tasks_cfg.get(task) or {}).get("target") or 0),
        "progress": float(((us.get("progress") or {}).get(task) or {}).get("value") or 0),
        "starts_at": cfg.get("starts_at"), "expires_at": cfg.get("expires_at"),
        "enrolled_at": us.get("enrolled_at"), "completed_at": us.get("completed_at"), "claimed_at": us.get("claimed_at"),
    }


async def fetch_quests():
    data = await qapi("GET", "/quests/@me")
    raw = data.get("quests") if isinstance(data, dict) else data
    quests = [parse_quest(q) for q in (raw or []) if isinstance(q, dict)]
    db.save_quests(ACCOUNT_ID, quests)
    return quests


async def enroll_quest(quest_id):
    await qapi("POST", "/quests/{quest_id}/enroll", json={"location": 11, "is_targeted": False, "metadata_raw": None},
               quest_id=quest_id)


def _run(qid, status, st, message=""):
    db.set_quest_run(ACCOUNT_ID, qid, status, st["progress"], st["target"], message)


def _progress_of(resp, task):
    if not isinstance(resp, dict):
        return None
    prog = resp.get("progress") or (resp.get("user_status") or {}).get("progress") or {}
    value = (prog.get(task) or {}).get("value")
    return float(value) if value is not None else None


async def quest_video(q, st):
    qid, target = q["id"], st["target"]
    enrolled = (parse_dt(q["enrolled_at"]) or datetime.now(timezone.utc)).timestamp()
    speed, ahead, done, completed = 7.0, 10.0, st["progress"], False
    while done < target and not completed:
        if (time.time() - enrolled) + ahead - done >= speed:  # the server only allows progress close to real elapsed time
            ts = min(target, done + speed)
            resp = await qapi("POST", "/quests/{quest_id}/video-progress",
                              json={"timestamp": min(target, ts + random.random())}, quest_id=qid)
            done, completed = ts, bool(isinstance(resp, dict) and resp.get("completed_at"))
            st["progress"] = done
            _run(qid, "running", st, "Watching video")
        await asyncio.sleep(1)
    if not completed:
        await qapi("POST", "/quests/{quest_id}/video-progress", json={"timestamp": target}, quest_id=qid)


async def quest_play(q, st):
    qid, app_id, task = q["id"], q["application_id"], q["task"]
    if q["game"]:
        try:
            await bot.change_presence(activity=discord.Game(name=q["game"]))
        except Exception:
            pass
    try:
        while True:
            resp = await qapi("POST", "/quests/{quest_id}/heartbeat", json={"application_id": app_id, "terminal": False}, quest_id=qid)
            value = _progress_of(resp, task)
            st["progress"] = value if value is not None else st["progress"] + 20
            _run(qid, "running", st, "Playing " + (q["game"] or "game"))
            if (isinstance(resp, dict) and resp.get("completed_at")) or st["progress"] >= st["target"]:
                break
            await asyncio.sleep(20)
    finally:
        try:
            await qapi("POST", "/quests/{quest_id}/heartbeat", json={"application_id": app_id, "terminal": True}, quest_id=qid)
        except Exception:
            pass
        await apply_presence()


async def run_quest(qid):
    st = {"progress": 0.0, "target": 0.0}
    try:
        _run(qid, "running", st, "Queued")
        async with quest_sem():
            q = next((x for x in await fetch_quests() if x["id"] == qid), None)
            if q is None:
                raise ValueError("Quest not found")
            st.update(progress=q["progress"], target=q["target"])
            if q["completed_at"]:
                return _run(qid, "done", st, "Already completed")
            if not q["supported"]:
                raise ValueError("Unsupported quest type: " + ", ".join(q["tasks"] or ["unknown"]))
            if not q["enrolled_at"]:
                _run(qid, "running", st, "Accepting quest")
                await enroll_quest(qid)
                q = next(x for x in await fetch_quests() if x["id"] == qid)
            _run(qid, "running", st, "Starting")
            await (quest_play if q["task"] == "PLAY_ON_DESKTOP" else quest_video)(q, st)
            st["progress"] = st["target"]
            _run(qid, "done", st, "Completed")
            log("SYSTEM", f"Quest completed: {q['name']}")
            db.add_notification(ACCOUNT_ID, "quest", "Quests", "", "", preview=f"Quest completed: {q['name']} - claim the reward in Discord")
    except asyncio.CancelledError:
        _run(qid, "paused", st, "Paused")
        raise
    except Exception as exc:
        _run(qid, "error", st, f"{type(exc).__name__}: {exc}"[:200])
        log("SYSTEM", f"Quest failed: {type(exc).__name__}", "warning")
    finally:
        quest_tasks.pop(qid, None)
        try:
            await fetch_quests()
        except Exception:
            pass


def _start_quest(qid):
    task = quest_tasks.get(qid)
    if task and not task.done():
        return False
    quest_tasks[qid] = spawn(run_quest(qid))
    return True


async def act_quest_list(_p):
    return result("Quests refreshed", [["Found", len(await fetch_quests())]])


async def act_quest_enroll(p):
    await enroll_quest(str(p["quest_id"]))
    await fetch_quests()
    return result("Quest accepted")


async def act_quest_start(p):
    started = _start_quest(str(p["quest_id"]))
    return result("Quest started" if started else "Already running")


async def act_quest_pause(p):
    task = quest_tasks.get(str(p["quest_id"]))
    if task and not task.done():
        task.cancel()
        await asyncio.sleep(0.2)
        return result("Quest paused")
    return result("That quest is not running")


async def act_quest_start_all(_p):
    now = datetime.now(timezone.utc)
    started = 0
    for q in await fetch_quests():
        expires, starts = parse_dt(q["expires_at"]), parse_dt(q["starts_at"])
        if q["supported"] and not q["completed_at"] and not (expires and expires <= now) and not (starts and starts > now):
            started += _start_quest(q["id"])
    return result("Quests started", [["Started", started]])


async def act_quest_pause_all(_p):
    count = 0
    for task in list(quest_tasks.values()):
        if not task.done():
            task.cancel()
            count += 1
    await asyncio.sleep(0.2)
    return result("Quests paused", [["Paused", count]])


ACTIONS = {
    "ping": act_ping, "info": act_info, "refresh_snapshot": act_refresh, "user": act_user, "server": act_server,
    "avatar": act_avatar, "channels": act_channels, "send": act_send, "purge": act_purge, "snipe": act_snipe,
    "dm_list": act_dm_list, "dm_history": act_dm_history, "friends": act_friends,
    "friend_accept": act_friend_accept, "friend_remove": act_friend_remove, "friend_add": act_friend_add,
    "mod": act_mod, "quest_list": act_quest_list, "quest_enroll": act_quest_enroll, "quest_start": act_quest_start,
    "quest_pause": act_quest_pause, "quest_start_all": act_quest_start_all, "quest_pause_all": act_quest_pause_all,
}
ACTION_TIMEOUT = {"purge": 300}


async def run_job(job):
    action = job["action"]
    try:
        import json
        payload = json.loads(job["payload"] or "{}")
        handler = ACTIONS.get(action)
        if handler is None:
            raise ValueError(f"Unknown action: {action}")
        out = await asyncio.wait_for(handler(payload), ACTION_TIMEOUT.get(action, 30))
        db.finish_job(job["id"], "done", out)
    except asyncio.TimeoutError:
        db.finish_job(job["id"], "error", {"error": "Timed out"})
    except KeyError as exc:
        db.finish_job(job["id"], "error", {"error": f"Missing field: {exc}"})
    except Exception as exc:
        db.finish_job(job["id"], "error", {"error": f"{type(exc).__name__}: {exc}"[:300]})


@tasks.loop(seconds=1.0)
async def job_loop():
    global last_rev
    try:
        rev = db.settings_rev(ACCOUNT_ID)
        if rev > last_rev:
            last_rev = rev
            load_settings()
            await apply_presence()
        for job in db.claim_jobs(ACCOUNT_ID):
            spawn(run_job(job))
    except Exception as exc:
        print("job loop error:", exc)


@tasks.loop(seconds=60)
async def heartbeat():
    global hb_ticks
    hb_ticks += 1
    try:
        await refresh_snapshot()
    except Exception as exc:
        print("heartbeat error:", exc)
    if hb_ticks % 5 == 0:  # quest list every ~5 minutes
        try:
            await fetch_quests()
        except Exception:
            pass


# ------------------------------------------------------------------ snipers
async def redeem_code(code):
    try:
        gift = await bot.fetch_gift(code)
    except discord.NotFound:
        return "invalid", "Unknown or expired code"
    except AttributeError:
        return "failed", "fetch_gift is not available in this library version"
    except Exception as exc:
        return "failed", f"Could not fetch gift: {exc}"[:200]
    if getv(gift, "redeemed", False):
        return "failed", "Already redeemed"
    try:
        await gift.redeem()
        return "claimed", "Redeemed"
    except Exception as exc:
        return "failed", str(exc)[:200]


async def handle_nitro(message):
    cfg = S["nitro"]
    if not cfg.get("enabled") or message.author.id == bot.user.id:
        return
    if message.guild and str(message.guild.id) in [str(x) for x in cfg.get("blacklist", [])]:
        return
    for code in GIFT_RE.findall(message.content or ""):
        if code in seen_codes:
            continue
        seen_codes.add(code)
        if len(seen_codes) > 2000:
            seen_codes.clear()
        if cfg.get("delay_ms"):
            await asyncio.sleep(int(cfg["delay_ms"]) / 1000)
        t0 = time.perf_counter()
        outcome, detail = await redeem_code(code)
        ms = int((time.perf_counter() - t0) * 1000)
        where = gfields(message.guild)
        db.add_sniper_event(ACCOUNT_ID, "nitro", code, where["guild_name"], str(getv(message.channel, "name", "DM")),
                            str(message.author), outcome, detail, ms)
        log("SNIPER", f"Nitro code {outcome}: {detail}", "info" if outcome == "claimed" else "warning",
            actor=str(message.author), actor_id=str(message.author.id), **where)
        db.add_notification(ACCOUNT_ID, "sniper", str(message.author), str(message.author.id),
                            asset_url(getv(message.author, "display_avatar"), 64), where["guild_name"],
                            str(getv(message.channel, "name", "DM")), str(message.channel.id), str(message.id),
                            message.jump_url, f"Nitro code {outcome} ({ms} ms): {detail}")


# ------------------------------------------------------------------ giveaway sniper (+ win alerts)
def clean_text(text):
    """Normalises fancy unicode fonts and strips markdown so trigger words always match."""
    if not text:
        return ""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("utf-8")
    return " ".join(re.sub(r"[*_~`|#><\-]", " ", ascii_text).lower().split())


def extract_full_text(message):
    """Text from the message body, embed titles/descriptions/authors/footers and fields."""
    parts = [message.content or ""]
    for embed in getv(message, "embeds", []) or []:
        parts += [getv(embed, "title", ""), getv(embed, "description", ""),
                  getv(getv(embed, "author"), "name", ""), getv(getv(embed, "footer"), "text", "")]
        for field in getv(embed, "fields", []) or []:
            parts += [getv(field, "name", ""), getv(field, "value", "")]
    return " ".join(str(p) for p in parts if p)


def has_word(cleaned, word):
    word = clean_text(word)
    return bool(word) and re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", cleaned) is not None


def is_giveaway(message, cfg):
    """True only when the text contains a giveaway trigger word (reactions alone never count)."""
    cleaned = clean_text(extract_full_text(message))
    if any(has_word(cleaned, w) for w in cfg.get("ignore_words", [])):
        return False
    return any(has_word(cleaned, t) for t in cfg.get("triggers", []))


def pick_reaction(message):
    """The ONE entry reaction: the party popper, else the emoji the text tells you to use, else the only reaction."""
    reactions = list(getv(message, "reactions", []) or [])
    if not reactions:
        return None
    raw = extract_full_text(message)
    for r in reactions:
        if str(r.emoji) == "🎉":
            return r
    for r in reactions:
        if str(r.emoji) in raw:
            return r
    return reactions[0] if len(reactions) == 1 else None


def find_join_button(message):
    buttons = []
    for row in getv(message, "components", []) or []:
        for child in getv(row, "children", []) or []:
            if hasattr(child, "click") and not getv(child, "disabled", False):
                buttons.append(child)
    for btn in buttons:
        label = (getv(btn, "label", "") or "").lower()
        if str(getv(btn, "emoji", "")) == "🎉" or any(w in label for w in JOIN_WORDS):
            return btn
    return buttons[0] if len(buttons) == 1 else None


def _bounded(items, limit=3000):
    if len(items) > limit:
        items.clear()


async def process_giveaway(message):
    cfg = S["giveaway"]
    mid = message.id
    if mid in gw_entered or mid in gw_inflight or gw_attempts.get(mid, 0) >= 2:
        return
    if message.guild and str(message.guild.id) in [str(x) for x in cfg.get("blacklist", [])]:
        return
    gw_inflight.add(mid)
    try:
        method = target = None
        for attempt in range(2):  # bots often add the button / reaction a moment after posting
            await asyncio.sleep(1.5 if attempt == 0 else 4)
            try:
                message = await message.channel.fetch_message(mid)
            except Exception:
                pass
            if not message.author.bot or not is_giveaway(message, cfg):
                return
            if cfg.get("use_buttons", True):
                target = find_join_button(message)
                if target:
                    method = "button"
                    break
            if cfg.get("use_reactions", True):
                target = pick_reaction(message)
                if target is not None:
                    if getv(target, "me", False):  # already in
                        gw_entered.add(mid)
                        return
                    method = "reaction"
                    break
        if not method:
            return
        lo, hi = float(cfg.get("min_delay", 2)), float(cfg.get("max_delay", 6))
        await asyncio.sleep(random.uniform(lo, max(lo, hi)))
        gw_attempts[mid] = gw_attempts.get(mid, 0) + 1
        t0 = time.perf_counter()
        try:
            if method == "button":
                await target.click()
            else:
                await message.add_reaction(target.emoji)
            outcome, detail = "joined", f"Joined via {method}"
            gw_entered.add(mid)
        except Exception as exc:
            outcome, detail = "failed", str(exc)[:200]
        ms = int((time.perf_counter() - t0) * 1000)
        first = message.embeds[0] if message.embeds else None
        title = (getv(first, "title", "") or message.content or "Giveaway")[:80]
        where = gfields(message.guild)
        db.add_sniper_event(ACCOUNT_ID, "giveaway", title, where["guild_name"], str(getv(message.channel, "name", "")),
                            str(message.author), outcome, detail, ms)
        log("SNIPER", f"Giveaway {outcome}: {title}", "info" if outcome == "joined" else "warning",
            actor=str(message.author), actor_id=str(message.author.id), **where)
        db.add_notification(ACCOUNT_ID, "sniper", str(message.author), str(message.author.id),
                            asset_url(getv(message.author, "display_avatar"), 64), where["guild_name"],
                            str(getv(message.channel, "name", "")), str(message.channel.id), str(message.id),
                            message.jump_url, f"Giveaway {outcome}: {title}")
    finally:
        gw_inflight.discard(mid)
        _bounded(gw_entered)
        if len(gw_attempts) > 3000:
            gw_attempts.clear()


async def post_win_webhook(message):
    url = S["giveaway"].get("webhook_url")
    if not url:
        return False
    where = gfields(message.guild)
    payload = {"username": WIN_WEBHOOK_NAME, "avatar_url": WIN_WEBHOOK_AVATAR, "content": (
        f" **GIVEAWAY WON!** \n**Account:** {bot.user.name}\n**Server:** {where['guild_name']}\n"
        f"**Channel:** #{getv(message.channel, 'name', 'unknown-channel')}\n**Message Link:** {message.jump_url}")}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                return resp.status in (200, 204)
    except Exception as exc:
        print("Win webhook failed:", exc)
        return False


async def check_win(message):
    cfg = S["giveaway"]
    if not cfg.get("enabled") or not cfg.get("win_alerts", True) or not message.guild:
        return
    if getv(message, "webhook_id") is not None or bot.user not in message.mentions or message.id in win_seen:
        return
    if "GIVEAWAY WON!" in (message.content or ""):  # our own webhook post
        return
    cleaned = clean_text(extract_full_text(message))
    for negative in ("won't", "won t", "wont"):  # "won't be there" is not a win
        cleaned = cleaned.replace(negative, " ")
    if not any(has_word(cleaned, w) for w in cfg.get("win_keywords", [])):
        return
    win_seen.add(message.id)
    _bounded(win_seen)
    where = gfields(message.guild)
    sent = await post_win_webhook(message)
    db.add_sniper_event(ACCOUNT_ID, "giveaway", f"Win in {where['guild_name']}", where["guild_name"],
                        str(getv(message.channel, "name", "")), str(message.author), "won", message.jump_url, 0)
    log("SNIPER", f"🎉 Giveaway won in {where['guild_name']}", actor=str(message.author), actor_id=str(message.author.id), **where)
    db.add_notification(ACCOUNT_ID, "win", str(message.author), str(message.author.id),
                        asset_url(getv(message.author, "display_avatar"), 64), where["guild_name"],
                        "#" + str(getv(message.channel, "name", "")), str(message.channel.id), str(message.id), message.jump_url,
                        "🎉 You won a giveaway!" + (" (webhook sent)" if sent else ""))


# ------------------------------------------------------------------ notifications (DMs + pings)
def notify_for(message):
    channel = message.channel
    is_dm = isinstance(channel, (discord.DMChannel, discord.GroupChannel))
    where = gfields(message.guild)
    preview = (message.clean_content or "").strip()
    if message.attachments:
        preview = (preview + " [attachment]").strip()
    preview = preview[:200]
    avatar = asset_url(getv(message.author, "display_avatar"), 64)
    chan_name = "DM" if is_dm else "#" + str(getv(channel, "name", ""))
    if is_dm and S["notify"].get("dms"):
        kind, text = "dm", f"DM from {message.author}"
    elif not is_dm and bot.user in message.mentions and S["notify"].get("pings"):
        kind, text = "ping", f"Mentioned by {message.author} in {chan_name}"
    else:
        return
    db.add_notification(ACCOUNT_ID, kind, str(message.author), str(message.author.id), avatar, where["guild_name"],
                        chan_name, str(channel.id), str(message.id), message.jump_url, preview)
    log("DM" if kind == "dm" else "PING", text, actor=str(message.author), actor_id=str(message.author.id), **where)


# ------------------------------------------------------------------ events
@bot.event
async def on_ready():
    global ready_once, last_rev
    me = bot.user
    db.set_status(ACCOUNT_ID, online=1, pid=os.getpid(), username=str(me), guild_count=len(bot.guilds), last_error="")
    if ready_once:
        return
    ready_once = True
    db.set_status(ACCOUNT_ID, started_at=db.now_iso())
    load_settings()
    last_rev = db.settings_rev(ACCOUNT_ID)
    db.fail_stale_jobs(ACCOUNT_ID)
    db.pause_stale_quest_runs(ACCOUNT_ID)
    await apply_presence()
    try:
        await refresh_snapshot()
    except Exception as exc:
        print("Initial snapshot failed:", exc)
    try:
        await fetch_quests()
    except Exception as exc:
        print("Quest fetch failed:", exc)
    log("SYSTEM", f"Connected as {me}")
    job_loop.start()
    heartbeat.start()
    await X.start()  # giveaway hosting + vanity sniper loops
    print(f"Logged in as {me} ({me.id})")


@bot.event
async def on_guild_join(guild):
    log("GUILD", "Joined a server", **gfields(guild))
    spawn(refresh_snapshot())


@bot.event
async def on_guild_remove(guild):
    log("GUILD", "Left a server", "warning", **gfields(guild))
    spawn(refresh_snapshot())


@bot.event
async def on_member_update(before, after):
    if after.id != bot.user.id:  # only this account's own roles
        return
    added = set(after.roles) - set(before.roles)
    removed = set(before.roles) - set(after.roles)
    for role in added:
        log("ROLE", f"You received a role in {after.guild.name}", role_name=role.name, role_id=str(role.id), **gfields(after.guild))
    for role in removed:
        log("ROLE", f"A role was removed from you in {after.guild.name}", role_name=role.name, role_id=str(role.id), **gfields(after.guild))


@bot.event
async def on_relationship_add(rel):
    kind = rel_type(rel)
    user = rel.user
    if kind == "incoming_request":
        log("FRIEND", f"Friend request received from {user}", actor=str(user), actor_id=str(user.id))
        db.add_notification(ACCOUNT_ID, "friend", str(user), str(user.id), asset_url(getv(user, "display_avatar"), 64),
                            preview="Sent you a friend request")
    elif kind == "friend":
        log("FRIEND", f"{user} is now your friend", actor=str(user), actor_id=str(user.id))
    elif kind == "outgoing_request":
        log("FRIEND", f"Friend request sent to {user}", actor=str(user), actor_id=str(user.id))
    spawn(refresh_snapshot())


@bot.event
async def on_relationship_remove(rel):
    log("FRIEND", f"Relationship removed: {rel.user}", "warning", actor=str(rel.user), actor_id=str(rel.user.id))
    spawn(refresh_snapshot())


@bot.event
async def on_relationship_update(before, after):
    if rel_type(before) == "outgoing_request" and rel_type(after) == "friend":
        log("FRIEND", f"{after.user} accepted your friend request", actor=str(after.user), actor_id=str(after.user.id))
        db.add_notification(ACCOUNT_ID, "friend", str(after.user), str(after.user.id),
                            asset_url(getv(after.user, "display_avatar"), 64), preview="Accepted your friend request")


@bot.event
async def on_message_edit(before, after):
    gw = S["giveaway"]
    if after.author.id == bot.user.id or not after.guild or not gw.get("enabled"):
        return
    if after.author.bot and is_giveaway(after, gw):
        spawn(process_giveaway(after))
    spawn(check_win(after))


@bot.event
async def on_message_delete(message):
    if message.author.id != bot.user.id:
        sniped[message.channel.id] = (str(message.author), message.content, message.created_at)  # memory only


@bot.event
async def on_command_completion(ctx):
    db.incr_stat(ACCOUNT_ID, "commands_used")
    log("SYSTEM", f"Command {get_prefix(bot, None)}{ctx.command.name} used", actor=str(ctx.author),
        actor_id=str(ctx.author.id), **gfields(ctx.guild))


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        return await say(ctx, f"⚠️ Bad usage. Try `{get_prefix(bot, None)}help`.", delete_after=6)
    log("SYSTEM", f"Command error: {type(error).__name__}", "error", **gfields(ctx.guild))
    await say(ctx, f"❌ {type(error).__name__}: {error}", delete_after=8)


@bot.event
async def on_message(message):
    prefix = get_prefix(bot, message)
    if message.author.id == bot.user.id:
        db.incr_stat(ACCOUNT_ID, "messages_sent")
        if automated(message):  # our own AFK notice / auto-reply / command output is not *you* coming back
            return
        if S["afk"].get("on") and not message.content.startswith(prefix + "afk"):
            update_setting("afk", {**S["afk"], "on": False})
            afk_notified.clear()
            await say(message.channel, "👋 AFK removed.", delete_after=5)
        await bot.process_commands(message)
        return

    spawn(handle_nitro(message))
    gw = S["giveaway"]
    if gw.get("enabled") and message.guild:
        if message.author.bot and is_giveaway(message, gw):  # giveaway bots only, trigger words only
            spawn(process_giveaway(message))
        spawn(check_win(message))
    notify_for(message)
    if message.author.bot:
        return

    emoji = S["autoreact"].get(str(message.author.id))
    if emoji:
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass

    low = (message.content or "").lower()
    for trigger, response in S["autoreply"].items():
        if trigger in low:
            await say(message.channel, response)
            break

    afk = S["afk"]
    if afk.get("on") and (bot.user in message.mentions or isinstance(message.channel, discord.DMChannel)):
        if time.time() - afk_notified.get(message.author.id, 0) > AFK_COOLDOWN:
            afk_notified[message.author.id] = time.time()
            away = fmt_duration(time.time() - float(afk.get("since", time.time())))
            await say(message.channel, f" I'm AFK ({afk.get('reason', 'AFK')}) - away for {away}.")


# ------------------------------------------------------------------ chat commands
@bot.command()
async def ping(ctx):
    await say(ctx, to_chat(await svc_ping()))


@bot.command()
async def info(ctx):
    await say(ctx, to_chat(await svc_info()))


@bot.command()
async def user(ctx, target: discord.User = None):
    await say(ctx, to_chat(await svc_user((target or ctx.author).id)))


@bot.command()
async def server(ctx):
    if not ctx.guild:
        return await say(ctx, "⚠️ Use this in a server.")
    await say(ctx, to_chat(await svc_server(ctx.guild.id)))


@bot.command()
async def avatar(ctx, target: discord.User = None):
    await say(ctx, to_chat(await svc_avatar((target or ctx.author).id)))


@bot.command()
async def purge(ctx, amount: int = 10):
    res = await svc_purge(ctx.channel.id, amount)
    await say(ctx, f"Deleted {res['fields'][0][1]} message(s).", delete_after=4)


@bot.command()
async def snipe(ctx):
    await say(ctx, to_chat(svc_snipe(ctx.channel.id)), delete_after=30)


@bot.command()
async def translate(ctx, lang: str, *, text: str):
    loop = asyncio.get_running_loop()
    try:
        out = await loop.run_in_executor(None, tools.translate_text, lang, text)
    except Exception as exc:
        return await say(ctx, f"❌ Translation failed: {exc}")
    await say(ctx, f" **{lang}:** {out}")


@bot.command(name="status")
async def status_cmd(ctx, state: str):
    state = state.lower()
    if state not in ("online", "idle", "dnd", "invisible"):
        return await say(ctx, "Choose: online, idle, dnd, invisible")
    update_setting("presence", {**S["presence"], "status": state})
    await apply_presence()
    await say(ctx, f"✅ Status set to **{state}**", delete_after=4)


@bot.command()
async def stream(ctx, *, args: str):
    parts = args.rsplit(" ", 1)
    name, url = (parts[0], parts[1]) if len(parts) == 2 and parts[1].startswith("http") else (args, "https://twitch.tv/discord")
    update_setting("presence", {**S["presence"], "kind": "streaming", "text": name, "url": url})
    await apply_presence()
    await say(ctx, f"✅ Streaming **{name}**", delete_after=4)


@bot.command()
async def autoreact(ctx, emoji: str, target: discord.User = None):
    reacts = dict(S["autoreact"])
    if emoji.lower() == "list":
        return await say(ctx, "\n".join(f"<@{uid}> → {e}" for uid, e in reacts.items()) or "No auto-reacts set.")
    if target is None:
        return await say(ctx, f"Usage: `{get_prefix(bot, None)}autoreact <emoji> <user>` or `off <user>`")
    if emoji.lower() == "off":
        reacts.pop(str(target.id), None)
        update_setting("autoreact", reacts)
        return await say(ctx, f"✅ Auto-react off for **{target}**", delete_after=4)
    reacts[str(target.id)] = emoji
    update_setting("autoreact", reacts)
    await say(ctx, f"✅ Reacting {emoji} to **{target}**", delete_after=4)


@bot.command()
async def autoreply(ctx, action: str, *, rest: str = ""):
    replies = dict(S["autoreply"])
    action = action.lower()
    if action == "add":
        if "|" not in rest:
            return await say(ctx, f"Usage: `{get_prefix(bot, None)}autoreply add <trigger> | <reply>`")
        trigger, response = (x.strip() for x in rest.split("|", 1))
        replies[trigger.lower()] = response
        update_setting("autoreply", replies)
        await say(ctx, f"✅ `{trigger}` → {response}", delete_after=5)
    elif action == "remove":
        if replies.pop(rest.strip().lower(), None) is None:
            return await say(ctx, "Trigger not found.", delete_after=5)
        update_setting("autoreply", replies)
        await say(ctx, "✅ Removed.", delete_after=4)
    elif action == "list":
        await say(ctx, "\n".join(f"`{t}` → {r}" for t, r in replies.items()) or "No auto-replies set.")
    else:
        await say(ctx, "Actions: add, remove, list")


@bot.command()
async def afk(ctx, *, reason: str = "AFK"):
    update_setting("afk", {"on": True, "reason": reason, "since": time.time()})
    afk_notified.clear()
    await say(ctx, f" AFK enabled: {reason}")


# ---- moderation chat commands
def uid_of(text):
    digits = re.sub(r"\D", "", str(text or ""))
    if not digits:
        raise ValueError("Give a user mention or ID")
    return int(digits)


async def run_mod_command(ctx, action, target, reason="", duration=None):
    if not ctx.guild:
        return await say(ctx, "⚠️ Use this in a server.")
    try:
        uid = uid_of(target)
        res = await svc_mod(action, ctx.guild.id, uid, reason, duration)
    except discord.Forbidden:
        return await say(ctx, " I don't have permission to do that here.", delete_after=8)
    except Exception as exc:
        return await say(ctx, f"❌ {exc}", delete_after=8)
    await say(ctx, f"✅ {res['title']} `{uid}`", delete_after=8)


@bot.command()
async def ban(ctx, target: str, *, reason: str = ""):
    await run_mod_command(ctx, "ban", target, reason)


@bot.command()
async def unban(ctx, target: str, *, reason: str = ""):
    await run_mod_command(ctx, "unban", target, reason)


@bot.command()
async def kick(ctx, target: str, *, reason: str = ""):
    await run_mod_command(ctx, "kick", target, reason)


@bot.command()
async def mute(ctx, target: str, *, args: str = ""):
    """!mute <user> [30m] [reason] - a Discord timeout (default 10m, max 28d)."""
    tokens = args.split(maxsplit=1)
    if tokens and TIMEOUT_RE.match(tokens[0]):
        await run_mod_command(ctx, "mute", target, tokens[1] if len(tokens) > 1 else "", tokens[0])
    else:
        await run_mod_command(ctx, "mute", target, args, "10m")


@bot.command()
async def unmute(ctx, target: str, *, reason: str = ""):
    await run_mod_command(ctx, "unmute", target, reason)


# ---- saved crypto wallets:  !crypto <coin>
@bot.command(name="crypto")
async def crypto_cmd(ctx, *args):
    p = get_prefix(bot, None)
    wallets = dict(S["wallets"])
    if not args or args[0].lower() == "list":
        if not wallets:
            return await say(ctx, f"No wallets saved yet. Add one with `{p}crypto add <coin> <address> [network]` or on the dashboard.")
        names = sorted(w.get("symbol", k).upper() + (f" ({w['network']})" if w.get("network") else "") for k, w in wallets.items())
        return await say(ctx, f"**Saved wallets:** {', '.join(names)}\nUse `{p}crypto <coin>` to get an address.")
    sub = args[0].lower()
    if sub == "add":
        if len(args) < 3:
            return await say(ctx, f"Usage: `{p}crypto add <coin> <address> [network]`")
        network = args[3] if len(args) > 3 else ""
        try:
            address = tools.validate_wallet(args[1], args[2])
            key = tools.wallet_key(args[1], network)
        except ValueError as exc:
            return await say(ctx, f"❌ {exc}", delete_after=10)
        old = wallets.get(key, {})
        wallets[key] = {"symbol": args[1].upper()[:15], "coin": old.get("coin") or args[1].upper(), "coin_id": old.get("coin_id", ""),
                        "network": network[:30], "address": address, "amount": old.get("amount", 0), "note": old.get("note", "")}
        update_setting("wallets", wallets)
        return await say(ctx, f"✅ Saved {args[1].upper()} wallet.", delete_after=6)
    if sub in ("remove", "delete", "del"):
        matches = tools.find_wallets(wallets, args[1]) if len(args) > 1 else []
        for key, _ in matches:
            wallets.pop(key, None)
        if matches:
            update_setting("wallets", wallets)
        return await say(ctx, f"✅ Removed {len(matches)} wallet(s)." if matches else "No matching wallet.", delete_after=6)
    matches = tools.find_wallets(wallets, args[0])
    if not matches:
        return await say(ctx, f"No wallet saved for **{args[0]}**. See `{p}crypto list`.", delete_after=10)
    await say(ctx, "\n".join(f"**{w.get('symbol', key).upper()}{' · ' + w['network'] if w.get('network') else ''}** ({w.get('coin', '')})\n`{w['address']}`"
                             for key, w in matches[:6]))


@bot.command()
async def gwsniper(ctx):
    cfg = dict(S["giveaway"])
    cfg["enabled"] = not cfg.get("enabled")
    update_setting("giveaway", cfg)
    text = f"Giveaway Sniper is now **{'enabled' if cfg['enabled'] else 'disabled'}**."
    try:
        await ctx.message.edit(content=text + MARK)
    except Exception:
        await say(ctx, text)


@bot.command()
async def nitrosniper(ctx):
    cfg = dict(S["nitro"])
    cfg["enabled"] = not cfg.get("enabled")
    update_setting("nitro", cfg)
    await say(ctx, f"Nitro Sniper is now **{'enabled' if cfg['enabled'] else 'disabled'}**.", delete_after=8)


# ------------------------------------------------------------------ extras (rpc, wallet, gwy, vanity, help)
X = extras.setup(bot, S=lambda: S, update_setting=update_setting, say=say, log=log, get_prefix=lambda: get_prefix(bot, None),
                 get_channel_any=get_channel_any, apply_presence=apply_presence, mark=MARK, getv=getv)

# ------------------------------------------------------------------ fun commands + developer portal / shop / inbox actions
import fun      # noqa: E402  (fun chat commands + the dashboard "Fun" tab)
import portal   # noqa: E402  (developer portal, shop catalog, inbox mentions / replies)

ACTIONS.update(fun.setup(bot, say=say, log=log, get_prefix=lambda: get_prefix(bot, None), get_channel_any=get_channel_any, mark=MARK))
import chanmod  # noqa: E402  (lock / unlock / channelcreate / channelrename)
import music    # noqa: E402  (dashboard-only music through Lavalink - no chat commands)
import cloner   # noqa: E402  (dashboard-only server cloner)

ACTIONS.update(chanmod.setup(bot, say=say, log=log, get_prefix=lambda: get_prefix(bot, None), mark=MARK))
ACTIONS.update(music.build(bot, log=log, getv=getv))
ACTIONS.update(cloner.build(bot, log=log))
ACTION_TIMEOUT.update({"music_play": 75})
ACTIONS.update(portal.build(bot, get_channel_any=get_channel_any, iso=iso, log=log, getv=getv, rel_type=rel_type))


# ------------------------------------------------------------------ run
def main():
    global ACCOUNT_ID
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", type=int, help="account id from the dashboard")
    args = parser.parse_args()
    db.init_db()

    if args.account:
        ACCOUNT_ID = args.account
    elif os.getenv("DISCORD_USER_TOKEN"):  # legacy single-token mode
        existing = [a for a in db.list_accounts() if a["label"] == "env"]
        ACCOUNT_ID = existing[0]["id"] if existing else db.add_account("env", os.environ["DISCORD_USER_TOKEN"].strip())
    else:
        raise SystemExit("Pass --account <id> (add accounts in the dashboard) or set DISCORD_USER_TOKEN.")

    token = db.get_token(ACCOUNT_ID)
    if not token:
        raise SystemExit(f"Account {ACCOUNT_ID} not found")
    load_settings()
    db.set_status(ACCOUNT_ID, online=0, pid=os.getpid(), last_error="")
    try:
        bot.run(token)
    except discord.LoginFailure:
        db.set_status(ACCOUNT_ID, online=0, pid=None, last_error="Login failed - the token is invalid or was reset.")
        log("SYSTEM", "Login failed: invalid token", "error")
        raise SystemExit(1)
    except Exception as exc:
        db.set_status(ACCOUNT_ID, online=0, pid=None, last_error=f"{type(exc).__name__}: {exc}"[:200])
        log("SYSTEM", f"Bot crashed: {type(exc).__name__}", "error")
        raise
    finally:
        db.set_status(ACCOUNT_ID, online=0, pid=None)


if __name__ == "__main__":
    main()
