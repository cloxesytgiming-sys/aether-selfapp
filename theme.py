"""
Dashboard customization: colours, background (solid / gradient / image or upload), glass & glow, corner radius,
UI scale, font, brand text and custom CSS. One theme per sign-in (admin, or each licence key), stored in
theme_store.json; uploaded backgrounds go to uploads/. The theme CSS is generated here from *validated* values only,
so the live preview and the saved result are identical and nothing the browser sends can inject markup.
"""
import hashlib
import json
import os
import re
import secrets
import threading
from pathlib import Path

from flask import abort, jsonify, render_template, request, send_from_directory, session

BASE = Path(__file__).resolve().parent
STORE = BASE / "theme_store.json"
UPLOADS = BASE / "uploads"
UPLOAD_LIMIT = 6 * 1024 * 1024
_lock = threading.Lock()

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
LOCAL_IMG = re.compile(r"^/uploads/bg_[0-9a-f]{10}_[0-9a-f]{8}\.(png|jpg|gif|webp)$")
REMOTE_IMG = re.compile(r"^https?://[A-Za-z0-9\-._~:/?#@!$&*+,;=%\[\]]{4,500}$")
UPLOAD_NAME = re.compile(r"^bg_[0-9a-f]{10}_[0-9a-f]{8}\.(png|jpg|gif|webp)$")

FONTS = {
    "default": None,
    "system": "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    "grotesk": "'Space Grotesk', 'DM Sans', sans-serif",
    "mono": "'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace",
    "serif": "Georgia, 'Times New Roman', serif",
}
BG_MODES = ("default", "solid", "gradient", "image")

DEFAULT = {
    "preset": "aether",
    "accent": "#47c2ff", "accent2": "#b8f43c", "bg": "#060a13", "surface": "#0e1626", "text": "#e9efff", "muted": "#8693b0",
    "bg_mode": "default", "bg_color2": "#1a1040", "bg_angle": 135, "bg_image": "", "bg_overlay": 55, "bg_blur": 0,
    "glow": True, "grid": True, "panel_opacity": 78, "glass_blur": 14, "radius": 100, "scale": 100,
    "font": "default", "brand_title": "", "brand_tag": "", "custom_css": "",
}
COLOR_KEYS = ("accent", "accent2", "bg", "surface", "text", "muted", "bg_color2")
INT_RANGES = {"bg_angle": (0, 360), "bg_overlay": (0, 90), "bg_blur": (0, 24), "panel_opacity": (30, 100),
              "glass_blur": (0, 40), "radius": (0, 180), "scale": (80, 125)}
LABELS = {"accent": "Accent", "accent2": "Second accent", "bg": "Background", "surface": "Surface", "text": "Text",
          "muted": "Muted text", "bg_color2": "Gradient colour"}

PRESETS = {
    "aether": {},
    "midnight": {"accent": "#a78bfa", "accent2": "#f472b6", "bg": "#0b0716", "surface": "#150f26", "text": "#f1ecff", "muted": "#9a8fbf"},
    "sunset": {"accent": "#ff7a59", "accent2": "#ffc857", "bg": "#140a0c", "surface": "#22110f", "text": "#fff1ea", "muted": "#b59088",
               "bg_mode": "gradient", "bg_color2": "#2a0f1f", "bg_angle": 160},
    "emerald": {"accent": "#34d399", "accent2": "#a3e635", "bg": "#04120d", "surface": "#0a1f18", "text": "#e6fff4", "muted": "#7fae9a"},
    "rose": {"accent": "#fb7185", "accent2": "#fbbf24", "bg": "#14070b", "surface": "#220d14", "text": "#ffeef1", "muted": "#b88a93"},
    "ocean": {"accent": "#22d3ee", "accent2": "#818cf8", "bg": "#031420", "surface": "#07253a", "text": "#e6f7ff", "muted": "#7fa6bd",
              "bg_mode": "gradient", "bg_color2": "#041e33", "bg_angle": 150},
    "cyber": {"accent": "#00f5d4", "accent2": "#fee440", "bg": "#0a0014", "surface": "#14002b", "text": "#f3e8ff", "muted": "#a78bfa",
              "bg_mode": "gradient", "bg_color2": "#1a0033", "bg_angle": 135},
    "oled": {"accent": "#60a5fa", "accent2": "#34d399", "bg": "#000000", "surface": "#0a0a0a", "text": "#ededed", "muted": "#8a8a8a",
             "glow": False, "grid": False, "panel_opacity": 100},
}


def preset_theme(name):
    return {**DEFAULT, **PRESETS.get(name, {}), "preset": name}


# ------------------------------------------------------------------ validation
def _bool(v):
    return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")


def clean(raw, base=None):
    """Any dict from the browser -> a safe, complete theme. Raises ValueError with a human message."""
    raw = raw if isinstance(raw, dict) else {}
    t = dict(base or DEFAULT)
    for k in COLOR_KEYS:
        if k in raw:
            v = str(raw[k]).strip()
            if not HEX.match(v):
                raise ValueError(f"{LABELS[k]}: use a #rrggbb colour")
            t[k] = v.lower()
    for k, (lo, hi) in INT_RANGES.items():
        if k in raw:
            try:
                t[k] = max(lo, min(hi, int(float(raw[k]))))
            except (TypeError, ValueError):
                raise ValueError(f"{k.replace('_', ' ').capitalize()} must be a number")
    for k in ("glow", "grid"):
        if k in raw:
            t[k] = _bool(raw[k])
    if "bg_mode" in raw:
        if raw["bg_mode"] not in BG_MODES:
            raise ValueError("Unknown background type")
        t["bg_mode"] = raw["bg_mode"]
    if "font" in raw:
        if raw["font"] not in FONTS:
            raise ValueError("Unknown font")
        t["font"] = raw["font"]
    if "preset" in raw:
        t["preset"] = raw["preset"] if raw["preset"] in PRESETS else "custom"
    if "bg_image" in raw:
        img = str(raw["bg_image"] or "").strip()
        if img and not (LOCAL_IMG.match(img) or REMOTE_IMG.match(img)):
            raise ValueError("Background image: use an https:// link (no spaces or quotes) or upload a file")
        t["bg_image"] = img
    for k, limit in (("brand_title", 24), ("brand_tag", 32)):
        if k in raw:
            t[k] = re.sub(r"[<>]", "", str(raw[k] or "")).strip()[:limit]
    if "custom_css" in raw:
        css = str(raw["custom_css"] or "")[:4000]
        css = re.sub(r"<", "", css)
        css = re.sub(r"@import[^;]*;?", "", css, flags=re.I)
        css = re.sub(r"(expression\s*\(|javascript:)", "", css, flags=re.I)
        t["custom_css"] = css.strip()
    return t


# ------------------------------------------------------------------ CSS generation
def _rgb(h):
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def _mix(a, b, amount):
    ra, rb = _rgb(a), _rgb(b)
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * amount) for x, y in zip(ra, rb))


RADII = {".panel": 24, ".stat-card": 22, ".cmd-card": 20, ".quest-card": 20, ".profile": 28, ".dp-card": 22, ".shop-item": 18, ".acct-card": 18,
         ".banner": 16, ".tabs": 16, ".srv-card": 16, ".pv-card": 16, ".list-row": 14, ".notif": 14, ".table-wrap": 14, ".toast": 14,
         ".vn-row": 14, ".tab": 12, ".input": 12, ".dm-item": 12}


def css(t):
    """Theme -> CSS text (empty for the untouched default look)."""
    if t == DEFAULT:
        return ""
    d, out, root = DEFAULT, [], []
    if t["accent"] != d["accent"]:
        root += [f"--sky:{t['accent']}", f"--sky-2:{_mix(t['accent'], '#ffffff', .35)}"]
    if t["accent2"] != d["accent2"]:
        root.append(f"--lime:{t['accent2']}")
    if t["bg"] != d["bg"]:
        root.append(f"--bg:{t['bg']}")
    if t["surface"] != d["surface"] or t["text"] != d["text"]:
        root += [f"--surface:{t['surface']}", f"--surface-2:{_mix(t['surface'], t['text'], .045)}"]
    if t["text"] != d["text"]:
        root += [f"--text:{t['text']}", "--line:color-mix(in srgb, var(--text) 9%, transparent)",
                 "--line-2:color-mix(in srgb, var(--text) 18%, transparent)"]
    if t["muted"] != d["muted"]:
        root += [f"--muted:{t['muted']}", f"--dim:{_mix(t['muted'], t['bg'], .35)}"]
    if FONTS.get(t["font"]):
        root.append(f"--font:{FONTS[t['font']]}")
        root.append(f"--display:{FONTS[t['font']]}")
    if root:
        out.append(":root{" + ";".join(root) + "}")
    if t["scale"] != 100:
        out.append(f"body{{zoom:{t['scale'] / 100:.2f}}}")
    if not t["glow"]:
        out.append(".ambient{display:none!important}")
    if not t["grid"]:
        out.append("body::before{display:none!important}")
    if t["panel_opacity"] != d["panel_opacity"] or t["glass_blur"] != d["glass_blur"]:
        out.append(f".panel,.cmd-card,.stat-card,.dp-card,.quest-card{{background:color-mix(in srgb, var(--surface) {t['panel_opacity']}%, transparent)}}")
        out.append(f".panel,.cmd-card,.stat-card,.dp-card,.quest-card,.sidebar{{backdrop-filter:blur({t['glass_blur']}px);-webkit-backdrop-filter:blur({t['glass_blur']}px)}}")
    if t["radius"] != 100:
        out.append("".join(f"{sel}{{border-radius:{max(0, round(px * t['radius'] / 100))}px}}" for sel, px in RADII.items()))
    mode = t["bg_mode"]
    if mode == "solid":
        out.append("html{background:var(--bg)}body{background:transparent}")
    elif mode == "gradient":
        out.append("html{background:var(--bg)}body{background:transparent}"
                   f"html::before{{content:'';position:fixed;inset:0;z-index:-1;background:linear-gradient({t['bg_angle']}deg,{t['bg']},{t['bg_color2']})}}")
    elif mode == "image" and t["bg_image"]:
        r, g, b = _rgb(t["bg"])
        blur = t["bg_blur"]
        out.append("html{background:var(--bg)}body{background:transparent}"
                   f"html::before{{content:'';position:fixed;inset:{-2 * blur}px;z-index:-1;background:url('{t['bg_image']}') center/cover no-repeat;filter:blur({blur}px)}}"
                   f"html::after{{content:'';position:fixed;inset:0;z-index:-1;background:rgba({r},{g},{b},{t['bg_overlay'] / 100:.2f})}}")
    if t["custom_css"]:
        out.append(t["custom_css"])
    return "\n".join(out)


# ------------------------------------------------------------------ storage
def _read():
    try:
        return json.loads(STORE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def get(owner):
    with _lock:
        raw = _read().get(owner)
    try:
        return clean(raw, DEFAULT) if raw else dict(DEFAULT)
    except ValueError:
        return dict(DEFAULT)


def save(owner, theme):
    with _lock:
        data = _read()
        data[owner] = theme
        tmp = STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), "utf-8")
        os.replace(tmp, STORE)


def forget(owner):
    with _lock:
        data = _read()
        data.pop(owner, None)
        STORE.write_text(json.dumps(data, indent=1), "utf-8")


def owner_hash(owner):
    return hashlib.sha1(owner.encode()).hexdigest()[:10]


def sniff_image(head):
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:4] == b"GIF8":
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def save_upload(owner, data):
    ext = sniff_image(data[:16])
    if not ext:
        raise ValueError("That file is not a PNG, JPEG, GIF or WebP image")
    UPLOADS.mkdir(exist_ok=True)
    prefix = f"bg_{owner_hash(owner)}_"
    for old in UPLOADS.glob(prefix + "*"):  # one background per sign-in
        try:
            old.unlink()
        except OSError:
            pass
    name = f"{prefix}{secrets.token_hex(4)}.{ext}"
    (UPLOADS / name).write_bytes(data)
    return "/uploads/" + name


# ------------------------------------------------------------------ routes
def init(app, login_required, json_only, session_ok):
    def owner():
        return session.get("owner", "admin") if session_ok() else "admin"  # the login page wears the admin's theme

    @app.context_processor
    def theme_context():
        t = get(owner())
        return {"theme": t, "theme_css": css(t)}

    @app.route("/customize")
    @login_required
    def customize_page():
        return render_template("customize.html", presets={k: preset_theme(k) for k in PRESETS}, fonts=list(FONTS))

    @app.get("/api/theme")
    @login_required
    def api_theme():
        t = get(owner())
        return jsonify(theme=t, css=css(t))

    @app.post("/api/theme/css")
    @login_required
    @json_only
    def api_theme_preview():
        try:
            t = clean(request.get_json() or {}, get(owner()))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(css=css(t))

    @app.post("/api/theme")
    @login_required
    @json_only
    def api_theme_save():
        try:
            t = clean(request.get_json() or {}, get(owner()))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        save(owner(), t)
        return jsonify(ok=True, theme=t, css=css(t))

    @app.post("/api/theme/reset")
    @login_required
    @json_only
    def api_theme_reset():
        forget(owner())
        return jsonify(ok=True, theme=dict(DEFAULT), css="")

    @app.post("/api/theme/upload")
    @login_required
    def api_theme_upload():
        try:
            request.max_content_length = UPLOAD_LIMIT + 4096  # Flask 3.1+: per-request limit (the global one is small)
        except Exception:
            pass
        try:
            f = request.files.get("file")
        except Exception:
            return jsonify(error="That image is too large for this server setup (update Flask to 3.1+ or use an image link)."), 413
        if not f:
            return jsonify(error="Choose an image first"), 400
        data = f.read(UPLOAD_LIMIT + 1)
        if len(data) > UPLOAD_LIMIT:
            return jsonify(error="Images can be up to 6 MB"), 413
        try:
            url = save_upload(owner(), data)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(ok=True, url=url)

    @app.get("/uploads/<name>")
    def uploaded_file(name):
        if not UPLOAD_NAME.match(name):
            abort(404)
        return send_from_directory(UPLOADS, name, max_age=3600)
