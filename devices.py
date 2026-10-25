"""
Device spoofing for Aether: makes the account also appear online from another kind of device.

How it works: for VR / MOBILE / CONSOLE a second, lightweight gateway session is opened with the
account's own token and that device's IDENTIFY properties (the VR one is your Meta Quest script).
DESKTOP opens nothing extra - the bot's normal session is the desktop one.

Adding another device/script later = add one entry to PROFILES below (same keys as "vr").
Nothing here ever stores or prints the token; it is read from the database when needed.
"""
import asyncio
import json
import random
import time

GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
DEVICES = ("desktop", "vr", "mobile", "console")
STATUSES = ("online", "idle", "dnd", "invisible")
FATAL_CODES = {4004: "Discord rejected the account token"}

# desktop has no entry on purpose: no extra session is needed.
PROFILES = {
    "vr": {   # your Meta Quest script
        "label": "VR (Meta Quest 3)",
        "capabilities": 8189,
        "properties": {"os": "Android", "browser": "Discord VR", "device": "Meta Quest 3",
                       "system_locale": "en-US", "client_version": "1.0.0"},
        "activity": {"name": "Meta Quest 3", "type": 0},
    },
    "mobile": {   # starter profile - replace with your own script when you have it
        "label": "Mobile (Android)",
        "capabilities": 8189,
        "properties": {"os": "Android", "browser": "Discord Android", "device": "Pixel 8",
                       "system_locale": "en-US", "client_version": "1.0.0"},
        "activity": None,
    },
    "console": {   # starter profile - replace with your own script when you have it
        "label": "Console (Xbox)",
        "capabilities": 8189,
        "properties": {"os": "Xbox", "browser": "Discord Embedded", "device": "Xbox Series X",
                       "system_locale": "en-US", "client_version": "1.0.0"},
        "activity": {"name": "Xbox Series X", "type": 0},
    },
}


def clean_config(cfg):
    """Settings value -> (device, show_activity). Badge only: the 'Playing Meta Quest 3' activity was removed, so
    show_activity is always False (old saved values are ignored). Invalid device -> desktop."""
    cfg = cfg or {}
    kind = str(cfg.get("type", "desktop")).lower()
    return (kind if kind in DEVICES else "desktop"), False


class DeviceManager:
    def __init__(self, token_getter, log):
        self.token_getter = token_getter  # callable -> token string
        self.log = log                    # callable(message, level="info")
        self.kind, self.status, self.show_activity = "desktop", "online", False
        self.task = None
        self.ws = None
        self.seq = None
        self._sent = None  # last presence payload the live session knows about

    # ------------------------------------------------------------ public
    def presence(self):
        prof = PROFILES.get(self.kind) or {}
        acts = [dict(prof["activity"])] if self.show_activity and prof.get("activity") else []
        return {"status": self.status, "since": 0, "activities": acts, "afk": False}

    async def sync(self, cfg, status):
        """Called whenever presence is (re)applied. Cheap when nothing changed."""
        kind, show = clean_config(cfg)
        status = status if status in STATUSES else "online"
        kind_changed = kind != self.kind
        self.kind, self.show_activity, self.status = kind, show, status
        if kind == "desktop":
            await self.stop()
            return
        if kind_changed or not (self.task and not self.task.done()):
            await self.stop()
            self.task = asyncio.ensure_future(self._run())
        else:
            await self._push_presence()

    async def stop(self):
        task, self.task = self.task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            self.log("Device spoof stopped")
        self.ws, self._sent = None, None

    # ------------------------------------------------------------ internals
    async def _send(self, ws, payload):
        await ws.send_str(json.dumps(payload))

    async def _push_presence(self):
        ws, pres = self.ws, self.presence()
        if ws is None or ws.closed or pres == self._sent:
            return
        try:
            await self._send(ws, {"op": 3, "d": pres})
            self._sent = pres
        except Exception as exc:
            self.log(f"Device presence update failed: {type(exc).__name__}", "warning")

    async def _heartbeat(self, ws, interval):
        await asyncio.sleep(interval * random.random())  # Discord asks for a jittered first beat
        while True:
            await self._send(ws, {"op": 1, "d": self.seq})
            await asyncio.sleep(interval)

    async def _run(self):
        import aiohttp
        backoff = 5
        async with aiohttp.ClientSession() as http:
            while True:
                started = time.time()
                try:
                    if await self._session(http, aiohttp):
                        return  # fatal: do not hammer Discord with a bad token
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.log(f"Device spoof error: {type(exc).__name__}", "warning")
                backoff = 5 if time.time() - started > 120 else min(backoff * 2, 120)
                await asyncio.sleep(backoff)

    async def _session(self, http, aiohttp):
        """One gateway connection. Returns True when retrying is pointless."""
        prof = PROFILES[self.kind]
        token = self.token_getter()
        if not token:
            self.log("Device spoof: no token available", "error")
            return True
        hb = None
        self.seq = None
        async with http.ws_connect(GATEWAY, max_msg_size=0, heartbeat=None) as ws:
            self.ws = ws
            try:
                async for raw in ws:
                    if raw.type != aiohttp.WSMsgType.TEXT:
                        break
                    data = json.loads(raw.data)
                    if data.get("s") is not None:
                        self.seq = data["s"]
                    op = data["op"]
                    if op == 10:
                        hb = asyncio.ensure_future(self._heartbeat(ws, data["d"]["heartbeat_interval"] / 1000))
                        pres = self.presence()
                        await self._send(ws, {"op": 2, "d": {"token": token, "capabilities": prof["capabilities"],
                                                              "properties": dict(prof["properties"]), "presence": pres,
                                                              "compress": False}})
                        self._sent = pres
                    elif op == 1:
                        await self._send(ws, {"op": 1, "d": self.seq})
                    elif op in (7, 9):
                        break
                    elif op == 0 and data.get("t") == "READY":
                        self.log(f"Device spoof online: {prof['label']}")
                        await self._push_presence()  # settings may have changed while connecting
            finally:
                if hb:
                    hb.cancel()
                self.ws = None
            code = ws.close_code
        if code in FATAL_CODES:
            self.log(f"Device spoof stopped: {FATAL_CODES[code]}", "error")
            return True
        return False
