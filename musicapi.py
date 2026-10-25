"""
Dashboard-side music API (search + link resolving + health). No Discord library needed here.

.env:  SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET   (search + Spotify track / album / playlist links)
       LAVALINK_HOST / LAVALINK_PORT / LAVALINK_PASSWORD / LAVALINK_SECURE   (YouTube search + links, voice playback)
Spotify only provides metadata: the dashboard player uses Spotify's own embed, and the voice-channel player matches
each song on YouTube through Lavalink.
"""
import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import jsonify, request

UA = "AetherPanel/2.3"
SPOTIFY_RE = re.compile(r"(?:open\.spotify\.com/(?:intl-[a-z\-]+/)?|spotify:)(track|album|playlist)[/:]([A-Za-z0-9]{22})")
URL_RE = re.compile(r"^https?://[^\s\"'<>]{4,500}$")
_lock = threading.Lock()
_tok = {"value": None, "exp": 0.0}
_cache = {}
_ping = {"t": 0.0, "data": None}


class MusicError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class SpotifyDenied(MusicError):
    """Spotify answered 403/404 for this endpoint (often Development Mode limits - see README)."""


# ------------------------------------------------------------------ config
def spotify_configured():
    return bool(os.getenv("SPOTIFY_CLIENT_ID", "").strip() and os.getenv("SPOTIFY_CLIENT_SECRET", "").strip())


def lavalink_cfg():
    host = os.getenv("LAVALINK_HOST", "").strip()
    if not host:
        return None
    secure = os.getenv("LAVALINK_SECURE", "0").strip().lower() in ("1", "true", "yes") or host.lower().startswith("https://")
    host = re.sub(r"^(https?|wss?)://", "", host, flags=re.I).rstrip("/")
    port = os.getenv("LAVALINK_PORT", "").strip()
    if ":" in host:
        host, _, p = host.partition(":")
        port = port or p
    return {"host": host, "port": int(port) if port.isdigit() else (443 if secure else 2333),
            "password": os.getenv("LAVALINK_PASSWORD", "youshallnotpass"), "secure": secure}


def _http(url, *, headers=None, data=None, method=None, timeout=12):
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MusicError(f"Network error: {getattr(exc, 'reason', exc)}", 502)


def _json(text):
    try:
        return json.loads(text)
    except ValueError:
        return {}


# ------------------------------------------------------------------ Spotify
def spotify_token():
    with _lock:
        if _tok["value"] and time.time() < _tok["exp"] - 60:
            return _tok["value"]
        if not spotify_configured():
            raise MusicError("Spotify isn't set up. Add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to your .env and restart.")
        basic = base64.b64encode(f"{os.getenv('SPOTIFY_CLIENT_ID').strip()}:{os.getenv('SPOTIFY_CLIENT_SECRET').strip()}".encode()).decode()
        status, text = _http("https://accounts.spotify.com/api/token", data=b"grant_type=client_credentials", method="POST",
                             headers={"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"})
        body = _json(text)
        if status != 200 or not body.get("access_token"):
            raise MusicError("Spotify rejected the client ID / secret (" + str(body.get("error_description") or status) + ")", 502)
        _tok.update(value=body["access_token"], exp=time.time() + int(body.get("expires_in", 3600)))
        return _tok["value"]


def spotify_get(path, params=None):
    url = "https://api.spotify.com/v1" + path + ("?" + urllib.parse.urlencode(params) if params else "")
    status, text = _http(url, headers={"Authorization": "Bearer " + spotify_token()})
    if status == 401:  # token expired early: refresh once
        _tok["exp"] = 0
        status, text = _http(url, headers={"Authorization": "Bearer " + spotify_token()})
    body = _json(text)
    if status == 429:
        raise MusicError("Spotify is rate limiting this app - try again in a moment.", 429)
    detail = str((body.get("error") or {}).get("message") or "").strip()
    if status == 403:
        raise SpotifyDenied(f"Spotify refused this request (403{': ' + detail if detail else ''}). Apps in Spotify's Development Mode "
                            "now need a Premium owner and can only use a limited set of endpoints.", 403)
    if status == 404:
        raise SpotifyDenied("Spotify says that link doesn't exist or is private (404).", 404)
    if status >= 400:
        raise MusicError(f"Spotify error {status}: {detail or 'unknown'}", 502)
    return body


def oembed(url):
    """Title + cover for a Spotify link without any API access (public endpoint). {} if unavailable."""
    try:
        status, text = _http("https://open.spotify.com/oembed?url=" + urllib.parse.quote(url, safe=""), timeout=8)
        return _json(text) if status == 200 else {}
    except MusicError:
        return {}


def _art(images):
    imgs = sorted([i for i in (images or []) if i.get("url")], key=lambda i: -(i.get("width") or 0))
    return (imgs[1] if len(imgs) > 1 else imgs[0])["url"] if imgs else ""


def sp_item(t, album=None):
    if not t or not t.get("id"):
        return None
    artists = [a.get("name") for a in t.get("artists") or [] if a.get("name")]
    alb = t.get("album") or album or {}
    first = artists[0] if artists else ""
    return {"id": t["id"], "title": t.get("name") or "Unknown", "artist": ", ".join(artists), "album": alb.get("name") or "",
            "art": _art(alb.get("images")), "duration_ms": int(t.get("duration_ms") or 0), "source": "spotify",
            "query": f"{first} - {t.get('name') or ''}".strip(" -")[:200], "url": (t.get("external_urls") or {}).get("spotify") or ""}


def spotify_search(q):
    body = spotify_get("/search", {"q": q, "type": "track", "limit": 20})
    return [i for i in (sp_item(t) for t in (body.get("tracks") or {}).get("items") or []) if i]


def _from_page(rows, album=None):
    out = []
    for x in rows:
        t = (x or {}).get("track") or (x or {}).get("item") or x  # playlist rows: "track" (old) / "item" (2026 API) / plain track
        if isinstance(t, dict) and t.get("id") and not t.get("is_local"):
            item = sp_item(t, album)
            if item:
                out.append(item)
    return out


def spotify_resolve(kind, sid, url=""):
    link = f"https://open.spotify.com/{kind}/{sid}"
    if kind == "track":
        item = sp_item(spotify_get(f"/tracks/{sid}"))
        return [item] if item else [], None
    if kind == "album":
        try:  # full album object first; Development Mode apps can still read the plain track list
            alb = spotify_get(f"/albums/{sid}")
            return [i for i in (sp_item(t, alb) for t in (alb.get("tracks") or {}).get("items") or []) if i], \
                {"kind": "album", "title": alb.get("name"), "art": _art(alb.get("images"))}
        except SpotifyDenied:
            meta = oembed(link)
            cover = {"name": meta.get("title") or "", "images": [{"url": meta.get("thumbnail_url") or "", "width": 300}]}
            page = spotify_get(f"/albums/{sid}/tracks", {"limit": 50})
            return _from_page(page.get("items") or [], cover), {"kind": "album", "title": meta.get("title"), "art": meta.get("thumbnail_url") or ""}
    meta, items, denied = oembed(link), [], None
    for endpoint in (f"/playlists/{sid}/tracks", f"/playlists/{sid}/items"):  # the endpoint was renamed in the 2026 API
        try:
            items, offset = [], 0
            while offset < 200:
                page = spotify_get(endpoint, {"limit": 100, "offset": offset})
                items += _from_page(page.get("items") or [])
                if not page.get("next"):
                    break
                offset += 100
            break
        except SpotifyDenied as exc:
            denied = exc
    else:
        raise denied
    return items, {"kind": "playlist", "title": meta.get("title"), "art": meta.get("thumbnail_url") or ""}


# ------------------------------------------------------------------ Lavalink (REST only: search + link metadata)
def lavalink_get(path, params=None, timeout=12):
    cfg = lavalink_cfg()
    if not cfg:
        raise MusicError("Lavalink isn't set up. Add LAVALINK_HOST, LAVALINK_PORT and LAVALINK_PASSWORD to your .env and restart.")
    url = f"{'https' if cfg['secure'] else 'http'}://{cfg['host']}:{cfg['port']}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    status, text = _http(url, headers={"Authorization": cfg["password"]}, timeout=timeout)
    if status == 401:
        raise MusicError("Lavalink rejected the password (LAVALINK_PASSWORD).", 502)
    if status >= 400:
        raise MusicError("Lavalink error: " + str(_json(text).get("message") or status), 502)
    return text


def yt_item(t):
    i = t.get("info") or {}
    src = str(i.get("sourceName") or "url")
    art = i.get("artworkUrl") or (f"https://i.ytimg.com/vi/{i['identifier']}/hqdefault.jpg" if src == "youtube" and i.get("identifier") else "")
    return {"id": i.get("identifier"), "title": i.get("title") or "Unknown", "artist": i.get("author") or "", "album": "",
            "art": art, "duration_ms": int(i.get("length") or 0), "source": src, "uri": i.get("uri") or "",
            "video_id": i.get("identifier") if src == "youtube" else "", "live": bool(i.get("isStream")),
            "query": f"{(i.get('author') or '').split(',')[0]} - {i.get('title') or ''}".strip(" -")[:200] if src == "spotify" else ""}


def lavalink_load(identifier):
    res = _json(lavalink_get("/v4/loadtracks", {"identifier": identifier}))
    kind, data = res.get("loadType"), res.get("data")
    if kind == "track":
        return [yt_item(data)], None
    if kind == "search":
        return [yt_item(t) for t in data or []][:20], None
    if kind == "playlist":
        info = data.get("info") or {}
        return [yt_item(t) for t in (data.get("tracks") or [])[:100]], {"kind": "playlist", "title": info.get("name"), "art": ""}
    if kind == "error":
        raise MusicError(str((data or {}).get("message") or "Lavalink could not load that"), 502)
    return [], None


def lavalink_search(q):
    for prefix in ("ytmsearch:", "ytsearch:"):
        try:
            items, _ = lavalink_load(prefix + q)
        except MusicError as exc:
            if exc.status == 502 and prefix == "ytmsearch:":
                continue
            raise
        if items:
            return items
    return []


def status_payload():
    cfg = lavalink_cfg()
    out = {"spotify": spotify_configured(), "lavalink": {"configured": bool(cfg), "online": False, "version": "", "host": ""}}
    if cfg:
        out["lavalink"]["host"] = f"{cfg['host']}:{cfg['port']}"
        if time.time() - _ping["t"] < 10 and _ping["data"]:
            out["lavalink"].update(_ping["data"])
        else:
            try:
                version = lavalink_get("/version", timeout=5).strip()
                data = {"online": True, "version": version}
            except MusicError as exc:
                data = {"online": False, "version": "", "error": str(exc)}
            _ping.update(t=time.time(), data=data)
            out["lavalink"].update(data)
    return out


def cached(key, fn, ttl=120):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    if len(_cache) > 200:
        _cache.clear()
    _cache[key] = (time.time(), value)
    return value


def search(q, source):
    q = q.strip()[:300]
    if not q:
        raise MusicError("Type a song name or paste a link.")
    m = SPOTIFY_RE.search(q)
    if m:
        try:
            items, collection = cached(("sp", m.group(1), m.group(2)), lambda: spotify_resolve(m.group(1), m.group(2), q))
            return {"items": items, "collection": collection, "mode": "spotify-link"}
        except SpotifyDenied as exc:
            if not lavalink_cfg():  # no second route available: explain instead of failing silently
                raise MusicError(str(exc) + " Tip: paste a YouTube link or search by song name instead.", exc.status)
            try:  # Lavalink with the LavaSrc plugin can open Spotify links itself
                items, collection = cached(("url", q), lambda: lavalink_load(q))
            except MusicError:
                items = []
            if items:
                return {"items": items, "collection": collection, "mode": "link", "note": "Spotify blocked this link for your app, so Lavalink loaded it instead."}
            raise MusicError(str(exc) + " Lavalink could not open it either (it needs the LavaSrc plugin). Paste a YouTube link or search by song name.", exc.status)
    if URL_RE.match(q):
        items, collection = cached(("url", q), lambda: lavalink_load(q))
        return {"items": items, "collection": collection, "mode": "link"}
    use_spotify = source == "spotify" or (source in ("auto", "") and spotify_configured())
    if use_spotify:
        try:
            return {"items": cached(("sps", q), lambda: spotify_search(q)), "collection": None, "mode": "spotify"}
        except SpotifyDenied as exc:
            if not lavalink_cfg():
                raise
            return {"items": cached(("yts", q), lambda: lavalink_search(q)), "collection": None, "mode": "youtube",
                    "note": "Spotify search was refused (" + str(exc).split(" Apps in")[0] + "), showing YouTube results instead."}
    return {"items": cached(("yts", q), lambda: lavalink_search(q)), "collection": None, "mode": "youtube"}


def init(app, login_required):
    @app.get("/api/music/status")
    @login_required
    def api_music_status():
        return jsonify(status_payload())

    @app.get("/api/music/search")
    @login_required
    def api_music_search():
        try:
            return jsonify(search(request.args.get("q", ""), request.args.get("source", "auto")))
        except MusicError as exc:
            return jsonify(error=str(exc)), exc.status
