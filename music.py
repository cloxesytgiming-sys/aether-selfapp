"""
Music for the dashboard (deliberately NO chat commands): lets a linked account join a voice channel and play
songs through Lavalink. Runs inside the account's worker (selfbot.py) and is driven by dashboard actions.

    music_status   state of every player + Lavalink health        music_voice   servers / voice channels + where the account is now
    music_play     join a channel and play / queue tracks          music_pause / music_resume / music_skip / music_stop
    music_volume   0-150                                          music_loop    off | track | queue
    music_seek     jump to a position (ms)                        music_remove / music_clear   edit the queue

Needs a Lavalink v4.2.0+ server (older ones cannot join Discord voice any more - DAVE encryption) set in .env:
    LAVALINK_HOST=...  LAVALINK_PORT=2333  LAVALINK_PASSWORD=...  LAVALINK_SECURE=0
Spotify tracks are matched to YouTube audio through Lavalink (ytmsearch / ytsearch), picking the closest length.

Wired up from selfbot.py:   ACTIONS.update(music.build(bot, ...))
"""
import asyncio
import json
import os
import re
import time

import discord

URL_RE = re.compile(r"^https?://[^\s\"'<>]{4,500}$")
SNOWFLAKE = re.compile(r"^\d{5,25}$")
IDLE_LEAVE_SECONDS = 300
MAX_BATCH = 25


def clip(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


def config():
    """Lavalink settings from .env, or None when not configured."""
    host = os.getenv("LAVALINK_HOST", "").strip()
    if not host:
        return None
    secure = os.getenv("LAVALINK_SECURE", "0").strip().lower() in ("1", "true", "yes")
    if re.match(r"^https://", host, re.I):
        secure = True
    host = re.sub(r"^(https?|wss?)://", "", host, flags=re.I).rstrip("/")
    port = os.getenv("LAVALINK_PORT", "").strip()
    if ":" in host:
        host, _, p = host.partition(":")
        port = port or p
    return {"host": host, "port": int(port) if port.isdigit() else (443 if secure else 2333),
            "password": os.getenv("LAVALINK_PASSWORD", "youshallnotpass"), "secure": secure}


class LavalinkError(Exception):
    pass


def _default_session():
    import aiohttp
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25))


def _is_text(msg):
    return getattr(msg.type, "name", str(msg.type)) == "TEXT"


# ====================================================================== Lavalink v4 node (REST + websocket)
class Node:
    def __init__(self, cfg, user_id, dispatch, session_factory=None):
        self.cfg, self.user_id, self.dispatch = cfg, user_id, dispatch
        self._factory = session_factory or _default_session
        self.session = self.ws = self.session_id = None
        self.ready = asyncio.Event()
        self.last_error = ""
        self._task = None

    @property
    def base(self):
        return f"{'https' if self.cfg['secure'] else 'http'}://{self.cfg['host']}:{self.cfg['port']}"

    @property
    def ws_url(self):
        return f"{'wss' if self.cfg['secure'] else 'ws'}://{self.cfg['host']}:{self.cfg['port']}/v4/websocket"

    def start(self):
        if not (self._task and not self._task.done()):
            self._task = asyncio.ensure_future(self._run())

    async def wait_ready(self, timeout=10):
        try:
            await asyncio.wait_for(self.ready.wait(), timeout)
        except asyncio.TimeoutError:
            raise LavalinkError(f"Can't reach Lavalink at {self.cfg['host']}:{self.cfg['port']}" + (f" ({self.last_error})" if self.last_error else ""))

    async def _run(self):
        delay = 2
        while True:
            try:
                if self.session is None:
                    self.session = self._factory()
                self.ws = await self.session.ws_connect(self.ws_url, heartbeat=30, headers={
                    "Authorization": self.cfg["password"], "User-Id": str(self.user_id), "Client-Name": "AetherPanel/2.3"})
                self.last_error, delay = "", 2
                async for msg in self.ws:
                    if _is_text(msg):
                        await self._handle(json.loads(msg.data))
                    elif getattr(msg.type, "name", "") in ("CLOSED", "CLOSING", "ERROR"):
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:160]
            had_session = self.session_id is not None
            self.session_id = None
            self.ready.clear()
            if had_session:
                await self.dispatch({"op": "nodeLost"})
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _handle(self, msg):
        if msg.get("op") == "ready":
            self.session_id = msg.get("sessionId")
            self.ready.set()
        try:
            await self.dispatch(msg)
        except Exception as exc:  # a bad event must never kill the socket loop
            print("lavalink event error:", repr(exc))

    async def rest(self, method, path, *, params=None, body=None):
        if self.session is None:
            self.session = self._factory()
        async with self.session.request(method, self.base + path, params=params, json=body,
                                        headers={"Authorization": self.cfg["password"]}) as r:
            text = await r.text()
            try:
                data = json.loads(text) if text else None
            except ValueError:
                data = None
            if r.status >= 400:
                raise LavalinkError((data or {}).get("message") or f"Lavalink answered HTTP {r.status}")
            return data

    async def load(self, identifier):
        return await self.rest("GET", "/v4/loadtracks", params={"identifier": identifier})

    async def update_player(self, guild_id, **body):
        if not self.session_id:
            raise LavalinkError("Lavalink is not connected")
        return await self.rest("PATCH", f"/v4/sessions/{self.session_id}/players/{guild_id}", params={"noReplace": "false"}, body=body)

    async def destroy_player(self, guild_id):
        if self.session_id:
            try:
                await self.rest("DELETE", f"/v4/sessions/{self.session_id}/players/{guild_id}")
            except LavalinkError:
                pass


# ====================================================================== track resolving
def tracks_of(res):
    kind, data = (res or {}).get("loadType"), (res or {}).get("data")
    if kind == "track" and isinstance(data, dict):
        return [data]
    if kind == "search" and isinstance(data, list):
        return data
    if kind == "playlist" and isinstance(data, dict):
        return data.get("tracks") or []
    return []


def best_match(tracks, duration_ms):
    """First hit, unless another of the top 5 is clearly closer to the wanted length (avoids slowed / extended edits)."""
    if not tracks or not duration_ms:
        return tracks[0] if tracks else None
    top = tracks[:5]
    closest = min(top, key=lambda t: abs(int((t.get("info") or {}).get("length") or 0) - duration_ms))
    close_enough = abs(int((closest.get("info") or {}).get("length") or 0) - duration_ms) <= 12000
    return closest if close_enough else tracks[0]


async def resolve_item(node, item):
    """-> Lavalink track dict for a queue item (a link, or a 'artist - title' search)."""
    if item.get("uri"):
        res = await node.load(item["uri"])
        found = tracks_of(res)
        if not found:
            msg = ((res or {}).get("data") or {}).get("message") if (res or {}).get("loadType") == "error" else ""
            raise LavalinkError(msg or "Lavalink could not load that link")
        return found[0]
    last = ""
    for prefix in ("ytmsearch:", "ytsearch:"):
        try:
            res = await node.load(prefix + item["query"])
        except LavalinkError as exc:
            last = str(exc)
            continue
        found = tracks_of(res)
        if found:
            return best_match(found, int(item.get("duration_ms") or 0))
        if (res or {}).get("loadType") == "error":
            last = ((res or {}).get("data") or {}).get("message") or last
    raise LavalinkError(last or "No playable match found")


def clean_items(raw):
    out = []
    for it in (raw or [])[:MAX_BATCH]:
        if not isinstance(it, dict):
            continue
        uri = str(it.get("uri") or "").strip()
        uri = uri if URL_RE.match(uri) else ""
        query = clip(str(it.get("query") or "").strip(), 200)
        if not (uri or query):
            continue
        art = str(it.get("art") or "").strip()
        try:
            dur = max(0, min(int(it.get("duration_ms") or 0), 6 * 3600 * 1000))
        except (TypeError, ValueError):
            dur = 0
        out.append({"title": clip(it.get("title") or query or uri, 150), "artist": clip(it.get("artist"), 100),
                    "art": art if URL_RE.match(art) else "", "duration_ms": dur, "source": clip(it.get("source") or "", 16),
                    "uri": uri, "query": query})
    return out


def item_view(it):
    return {k: it.get(k) for k in ("title", "artist", "art", "duration_ms", "source", "uri")} if it else None


# ====================================================================== voice (the Discord side of a Lavalink player)
class LLVoice(discord.VoiceProtocol):
    """Joins a channel through the normal gateway and hands Discord's voice credentials to Lavalink."""
    manager = None  # set by build()

    def __init__(self, client, channel):
        super().__init__(client, channel)
        self.client, self.channel = client, channel
        self.guild = channel.guild
        self.session_id = self.token = self.endpoint = None
        self.pushed = asyncio.Event()

    async def on_voice_state_update(self, data):
        if data.get("channel_id") is None:  # left, moved out, or kicked
            self.session_id = None
            self.pushed.clear()
            await self.manager.voice_dropped(self.guild.id)
            return
        self.session_id = data.get("session_id")
        chan = self.guild.get_channel(int(data["channel_id"]))
        if chan is not None:
            self.channel = chan
        await self._push()

    async def on_voice_server_update(self, data):
        self.token, self.endpoint = data.get("token"), data.get("endpoint")
        if self.endpoint:
            await self._push()

    async def _push(self):
        node = self.manager.node
        if not (self.session_id and self.token and self.endpoint and node and node.session_id):
            return
        await node.update_player(self.guild.id, voice={"token": self.token, "endpoint": self.endpoint,
                                                       "sessionId": self.session_id, "channelId": str(self.channel.id)})
        self.pushed.set()

    async def _join(self, channel, self_mute=False, self_deaf=False):
        self.pushed.clear()
        await self.guild.change_voice_state(channel=channel, self_mute=self_mute, self_deaf=self_deaf)

    async def connect(self, *, timeout=30.0, reconnect=True, self_deaf=False, self_mute=False, **_kw):
        await self._join(self.channel, self_mute, self_deaf)
        try:
            await asyncio.wait_for(self.pushed.wait(), timeout)
        except asyncio.TimeoutError:
            await self.disconnect(force=True)
            raise LavalinkError("Joined the channel but Discord / Lavalink did not finish the voice handshake (is Lavalink v4.2+?)")

    async def move_to(self, channel):
        self.channel = channel
        await self._join(channel)
        try:
            await asyncio.wait_for(self.pushed.wait(), 20)
        except asyncio.TimeoutError:
            raise LavalinkError("Could not move to that channel")

    async def disconnect(self, *, force=False):
        try:
            await self.guild.change_voice_state(channel=None)
        except Exception:
            pass
        try:
            self.cleanup()
        except Exception:
            pass
        await self.manager.player_gone(self.guild.id)


# ====================================================================== player (queue + playback state for one server)
class Player:
    def __init__(self, mgr, guild_id):
        self.mgr, self.guild_id = mgr, guild_id
        self.queue, self.current = [], None
        self.volume, self.loop, self.paused = 100, "off", False
        self.position, self.pos_ts = 0, None
        self.idle_task = None

    # ---- position bookkeeping
    def now_position(self):
        if not self.current:
            return 0
        pos = self.position
        if self.pos_ts and not self.paused:
            pos += (time.time() - self.pos_ts) * 1000
        dur = int(self.current.get("duration_ms") or 0)
        return int(min(pos, dur) if dur else pos)

    def _mark(self, position, running):
        self.position, self.pos_ts = position, (time.time() if running else None)

    # ---- queue
    def _cancel_idle(self):
        if self.idle_task:
            self.idle_task.cancel()
            self.idle_task = None

    async def _idle_leave(self):
        try:
            await asyncio.sleep(IDLE_LEAVE_SECONDS)
        except asyncio.CancelledError:
            return
        if self.current is None:
            await self.leave()

    async def add(self, items, now=False):
        self._cancel_idle()
        if now:
            self.queue[0:0] = items
            await self.next()
        else:
            self.queue.extend(items)
            if self.current is None:
                await self.next()

    async def next(self, from_end=False):
        node = self.mgr.node
        tries = 0
        while True:
            if from_end and self.loop == "track" and self.current and tries == 0:
                item = self.current
            else:
                if from_end and self.loop == "queue" and self.current and tries == 0:
                    self.queue.append(self.current)
                item = self.queue.pop(0) if self.queue else None
            if item is None:
                self.current = None
                self._mark(0, False)
                try:
                    await node.update_player(self.guild_id, track={"encoded": None})
                except LavalinkError:
                    pass
                self._cancel_idle()
                self.idle_task = asyncio.ensure_future(self._idle_leave())
                return
            try:
                track = await resolve_item(node, item)
                info = track.get("info") or {}
                item.setdefault("title", info.get("title"))
                item["title"] = item.get("title") or info.get("title")
                item["artist"] = item.get("artist") or info.get("author")
                item["art"] = item.get("art") or info.get("artworkUrl") or (
                    f"https://i.ytimg.com/vi/{info['identifier']}/hqdefault.jpg" if info.get("sourceName") == "youtube" and info.get("identifier") else "")
                item["duration_ms"] = item.get("duration_ms") or int(info.get("length") or 0)
                self.current, self.paused = item, False
                self._mark(0, True)
                await node.update_player(self.guild_id, track={"encoded": track["encoded"]}, volume=self.volume, paused=False, position=0)
                return
            except LavalinkError as exc:
                self.mgr.log(f"Music: skipped '{clip(item.get('title'), 60)}' - {exc}")
                self.mgr.last_problem = str(exc)
                self.current = None
                tries += 1
                if self.loop == "track":
                    self.loop = "off"
                if tries >= 5:
                    return

    # ---- controls
    async def set_paused(self, paused):
        if not self.current:
            raise ValueError("Nothing is playing")
        self._mark(self.now_position(), not paused)
        self.paused = paused
        await self.mgr.node.update_player(self.guild_id, paused=paused)

    async def set_volume(self, volume):
        self.volume = max(0, min(150, int(volume)))
        await self.mgr.node.update_player(self.guild_id, volume=self.volume)

    async def seek(self, ms):
        if not self.current:
            raise ValueError("Nothing is playing")
        ms = max(0, int(ms))
        self._mark(ms, not self.paused)
        await self.mgr.node.update_player(self.guild_id, position=ms)

    async def leave(self):
        self._cancel_idle()
        self.queue.clear()
        self.current = None
        vc = self.mgr.voice_for(self.guild_id)
        if vc:
            await vc.disconnect(force=True)
        else:
            await self.mgr.player_gone(self.guild_id)

    async def resync(self):
        """Lavalink restarted: send the voice credentials and the current song again."""
        vc = self.mgr.voice_for(self.guild_id)
        if vc:
            await vc._push()
        if self.current:
            item, pos = self.current, self.now_position()
            self.current = None
            self.queue.insert(0, item)
            await self.next()
            if pos > 5000:
                await self.seek(pos)

    def view(self, guild):
        vc = self.mgr.voice_for(self.guild_id)
        now = item_view(self.current)
        if now:
            now["position_ms"] = self.now_position()
        return {"guild_id": str(self.guild_id), "guild_name": getattr(guild, "name", ""),
                "channel_id": str(vc.channel.id) if vc else "", "channel_name": vc.channel.name if vc else "",
                "paused": self.paused, "volume": self.volume, "loop": self.loop, "now": now,
                "queue": [item_view(i) for i in self.queue[:100]], "queue_total": len(self.queue)}


# ====================================================================== manager + actions
def build(bot, *, log, getv, session_factory=None):
    class Manager:
        cfg = config()
        node = None
        players = {}
        last_problem = ""

        def log(self, text):
            try:
                log("SYSTEM", text)
            except Exception:
                pass

        def voice_for(self, guild_id):
            g = bot.get_guild(int(guild_id))
            vc = getattr(g, "voice_client", None) if g else None
            return vc if isinstance(vc, LLVoice) else None

        def player(self, guild_id):
            if guild_id not in self.players:
                self.players[guild_id] = Player(self, guild_id)
            return self.players[guild_id]

        def ensure_started(self):
            if self.cfg and self.node is None:
                self.node = Node(self.cfg, bot.user.id, self.dispatch, session_factory)
            if self.node:
                self.node.start()
            return self.node

        async def get_node(self):
            if not self.cfg:
                raise ValueError("Lavalink isn't set up. Add LAVALINK_HOST, LAVALINK_PORT and LAVALINK_PASSWORD to your .env and restart.")
            node = self.ensure_started()
            try:
                await node.wait_ready(10)
            except LavalinkError as exc:
                raise ValueError(str(exc))
            return node

        async def player_gone(self, guild_id):
            pl = self.players.pop(guild_id, None)
            if pl:
                pl._cancel_idle()
            if self.node:
                await self.node.destroy_player(guild_id)

        async def voice_dropped(self, guild_id):  # kicked / disconnected from Discord's side
            await self.player_gone(guild_id)

        async def dispatch(self, msg):
            op = msg.get("op")
            if op == "nodeLost":
                for pl in self.players.values():
                    pl._mark(pl.now_position(), False)
            elif op == "ready":
                for pl in list(self.players.values()):
                    asyncio.ensure_future(pl.resync())
            elif op == "playerUpdate":
                pl = self.players.get(int(msg["guildId"]))
                if pl and pl.current:
                    pl._mark(int((msg.get("state") or {}).get("position") or 0), not pl.paused)
            elif op == "event":
                pl = self.players.get(int(msg.get("guildId", 0) or 0))
                kind = msg.get("type")
                if not pl:
                    return
                if kind == "TrackEndEvent" and str(msg.get("reason", "")).lower() in ("finished", "loadfailed"):
                    await pl.next(from_end=True)
                elif kind == "TrackStuckEvent":
                    await pl.next(from_end=True)
                elif kind == "TrackExceptionEvent":
                    self.last_problem = str(((msg.get("exception") or {}).get("message")) or "Track error")
                    self.log("Music: track error - " + clip(self.last_problem, 120))

    mgr = Manager()
    LLVoice.manager = mgr

    def need_player(p):
        gid = str(p.get("guild_id", ""))
        if not SNOWFLAKE.match(gid) or int(gid) not in mgr.players:
            raise ValueError("The account isn't playing in that server")
        return mgr.players[int(gid)]

    def snapshot(guild_id=None):
        return {"players": [pl.view(bot.get_guild(gid)) for gid, pl in mgr.players.items() if guild_id in (None, gid)]}

    async def act_status(_p):
        node = mgr.ensure_started() if mgr.cfg else None
        out = snapshot()
        out.update(configured=bool(mgr.cfg), online=bool(node and node.session_id), error=(node.last_error if node else "") or mgr.last_problem,
                   host=f"{mgr.cfg['host']}:{mgr.cfg['port']}" if mgr.cfg else "")
        return out

    async def act_voice(_p):
        guilds, current = [], None
        for g in sorted(bot.guilds, key=lambda x: x.name.lower())[:150]:
            chans = []
            for c in sorted(list(getv(g, "voice_channels", []) or []) + list(getv(g, "stage_channels", []) or []), key=lambda c: getv(c, "position", 0))[:60]:
                try:
                    n = len(getv(c, "members", []) or [])
                except Exception:
                    n = 0
                chans.append({"id": str(c.id), "name": c.name, "members": n, "stage": isinstance(c, discord.StageChannel)})
            if chans:
                guilds.append({"id": str(g.id), "name": g.name, "channels": chans})
            try:
                here = getv(getv(getv(g, "me"), "voice"), "channel")
                if here is not None and current is None:
                    current = {"guild_id": str(g.id), "channel_id": str(here.id)}
            except Exception:
                pass
        return {"guilds": guilds, "current": current}

    async def act_play(p):
        gid = str(p.get("guild_id", ""))
        if not SNOWFLAKE.match(gid):
            raise ValueError("Pick a server")
        items = clean_items(p.get("items"))
        if not items:
            raise ValueError("Nothing to play")
        await mgr.get_node()
        guild = bot.get_guild(int(gid))
        if guild is None:
            raise ValueError("Server not found")
        vc, chan_id = getattr(guild, "voice_client", None), str(p.get("channel_id") or "")
        if chan_id:
            if not SNOWFLAKE.match(chan_id):
                raise ValueError("Pick a voice channel")
            channel = guild.get_channel(int(chan_id))
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                raise ValueError("That is not a voice channel")
            try:
                if vc is None:
                    await channel.connect(cls=LLVoice, timeout=30)
                elif isinstance(vc, LLVoice):
                    if vc.channel.id != channel.id:
                        await vc.move_to(channel)
                else:
                    raise ValueError("This account is already in a call that was not started from the dashboard")
            except LavalinkError as exc:
                raise ValueError(str(exc))
            except discord.Forbidden:
                raise ValueError("The account is not allowed to join that channel")
        elif not isinstance(vc, LLVoice):
            raise ValueError("Pick a voice channel first")
        pl = mgr.player(int(gid))
        try:
            await pl.add(items, now=p.get("mode") == "now")
        except LavalinkError as exc:
            raise ValueError(str(exc))
        view = pl.view(guild)
        if pl.current is None and mgr.last_problem:
            raise ValueError("Could not play that: " + mgr.last_problem)
        return {"title": "Playing" if p.get("mode") == "now" else f"Queued {len(items)}", **view}

    def wrap(fn):
        async def handler(p):
            try:
                return await fn(p)
            except LavalinkError as exc:
                raise ValueError(str(exc))
        return handler

    async def act_pause(p): await need_player(p).set_paused(True); return {"title": "Paused"}
    async def act_resume(p): await need_player(p).set_paused(False); return {"title": "Resumed"}
    async def act_skip(p): await need_player(p).next(); return {"title": "Skipped"}
    async def act_stop(p): await need_player(p).leave(); return {"title": "Stopped and left the channel"}

    async def act_volume(p):
        await need_player(p).set_volume(p.get("volume", 100))
        return {"title": "Volume set"}

    async def act_seek(p):
        await need_player(p).seek(p.get("position_ms", 0))
        return {"title": "Seeked"}

    async def act_loop(p):
        mode = str(p.get("mode", "off"))
        if mode not in ("off", "track", "queue"):
            raise ValueError("Loop must be off, track or queue")
        need_player(p).loop = mode
        return {"title": f"Loop: {mode}"}

    async def act_remove(p):
        pl = need_player(p)
        idx = int(p.get("index", -1))
        if not 0 <= idx < len(pl.queue):
            raise ValueError("That song is no longer in the queue")
        pl.queue.pop(idx)
        return {"title": "Removed"}

    async def act_clear(p):
        need_player(p).queue.clear()
        return {"title": "Queue cleared"}

    return {"music_status": act_status, "music_voice": act_voice, "music_play": wrap(act_play), "music_pause": wrap(act_pause),
            "music_resume": wrap(act_resume), "music_skip": wrap(act_skip), "music_stop": wrap(act_stop), "music_volume": wrap(act_volume),
            "music_seek": wrap(act_seek), "music_loop": wrap(act_loop), "music_remove": wrap(act_remove), "music_clear": wrap(act_clear)}
