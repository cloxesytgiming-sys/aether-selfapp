"""
Aether extras - plugs into selfbot.py (one process per account).

Adds:
  * !rpc      fully customisable Rich Presence (images, buttons, timer, party, presets)
  * !device   show as online from another device (desktop / VR / mobile / console)
  * !wallet   live crypto balance by address (BTC, LTC, DOGE, ETH + EVM chains, SOL, TRX)
  * !gwy      giveaway hosting (start / end / reroll / cancel / list)
  * !vanity   vanity sniper (watches every server you are in + a custom watch list -> webhook)
  * !help     the new categorised help menu

selfbot.py calls setup() once and start() from on_ready. Nothing here imports selfbot.py.
"""
import asyncio
import copy
import random
import re
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import devices
import tools

EMOJI = "🎉"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
DISCORD_WEBHOOK_RE = re.compile(r"^https://(?:ptb\.|canary\.)?(?:discord|discordapp)\.com/api/(?:v\d+/)?webhooks/\d+/[\w\-]+$")
VANITY_RE = re.compile(r"^[a-z0-9\-]{2,32}$")


# ====================================================================== generic helpers
def clip(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


def chunk_text(text, limit=1900):
    """Split on line boundaries so every Discord message stays under the length limit."""
    out, cur = [], ""
    for line in str(text).split("\n"):
        while len(line) > limit:
            head, line = line[:limit], line[limit:]
            if cur:
                out.append(cur)
                cur = ""
            out.append(head)
        if cur and len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out or [""]


_DUR_PART = re.compile(r"(\d+)([smhdw])")
_DUR_FULL = re.compile(r"(?:\d+[smhdw])+")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text):
    """'1h30m' -> 5400. Returns None when the text is not a duration."""
    text = re.sub(r"\s+", "", str(text or "").lower())
    if not text or not _DUR_FULL.fullmatch(text):
        return None
    return sum(int(n) * _UNIT[u] for n, u in _DUR_PART.findall(text))


def fmt_dur(seconds):
    seconds = int(max(seconds, 0))
    parts = []
    for name, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{name}")
            seconds %= size
    return " ".join(parts[:3]) or "0s"


def rel(ts):
    return f"<t:{int(ts)}:R>"


def full_ts(ts):
    return f"<t:{int(ts)}:f>"


def split_sub(args):
    """'set  name x' -> ('set', 'name x'); splits on any whitespace, including a newline."""
    parts = re.split(r"\s+", str(args or "").strip(), maxsplit=1)
    return parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def http_json(method, url, *, json_body=None, headers=None, timeout=15):
    """The single network choke point. Returns (status, parsed_json_or_None, response_headers)."""
    import aiohttp
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    hdrs.update(headers or {})
    async with aiohttp.ClientSession(headers=hdrs) as session:
        async with session.request(method, url, json=json_body, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = None
            return resp.status, data, dict(resp.headers)


def fmt_amount(value, max_dec=8):
    text = f"{value:,.{max_dec}f}".rstrip("0").rstrip(".")
    return text or "0"


def fmt_usd(value):
    return f"${value:,.2f}"


# ====================================================================== WALLET (balance by address)
class WalletError(Exception):
    pass


LTC_RE = re.compile(r"^(ltc1[0-9a-z]{20,90}|[LM][a-km-zA-HJ-NP-Z1-9]{26,33})$")
DOGE_RE = re.compile(r"^D[5-9A-HJ-NP-U][1-9A-HJ-NP-Za-km-z]{32}$")
TRX_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
SOL_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

EVM = {
    "eth":      {"name": "Ethereum",  "symbol": "ETH",  "cg": "ethereum",
                 "rpc": ["https://ethereum-rpc.publicnode.com", "https://cloudflare-eth.com"],
                 "tokens": {"USDT": ("0xdAC17F958D2ee523a2206206994597C13D831ec7", 6, "tether"),
                            "USDC": ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6, "usd-coin")}},
    "bsc":      {"name": "BNB Smart Chain", "symbol": "BNB", "cg": "binancecoin",
                 "rpc": ["https://bsc-rpc.publicnode.com", "https://bsc-dataseed.binance.org"],
                 "tokens": {"USDT": ("0x55d398326f99059fF775485246999027B3197955", 18, "tether")}},
    "polygon":  {"name": "Polygon",   "symbol": "POL",  "cg": "polygon-ecosystem-token",
                 "rpc": ["https://polygon-bor-rpc.publicnode.com", "https://polygon-rpc.com"], "tokens": {}},
    "arbitrum": {"name": "Arbitrum",  "symbol": "ETH",  "cg": "ethereum",
                 "rpc": ["https://arbitrum-one-rpc.publicnode.com", "https://arb1.arbitrum.io/rpc"], "tokens": {}},
    "optimism": {"name": "Optimism",  "symbol": "ETH",  "cg": "ethereum",
                 "rpc": ["https://optimism-rpc.publicnode.com", "https://mainnet.optimism.io"], "tokens": {}},
    "base":     {"name": "Base",      "symbol": "ETH",  "cg": "ethereum",
                 "rpc": ["https://base-rpc.publicnode.com", "https://mainnet.base.org"], "tokens": {}},
    "avax":     {"name": "Avalanche", "symbol": "AVAX", "cg": "avalanche-2",
                 "rpc": ["https://avalanche-c-chain-rpc.publicnode.com", "https://api.avax.network/ext/bc/C/rpc"], "tokens": {}},
}
TRC20_USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

CHAIN_ALIASES = {
    "btc": "btc", "bitcoin": "btc", "ltc": "ltc", "litecoin": "ltc", "doge": "doge", "dogecoin": "doge",
    "sol": "sol", "solana": "sol", "trx": "trx", "tron": "trx",
    "eth": "eth", "ethereum": "eth", "erc20": "eth", "bsc": "bsc", "bnb": "bsc", "bep20": "bsc",
    "polygon": "polygon", "matic": "polygon", "pol": "polygon", "arbitrum": "arbitrum", "arb": "arbitrum",
    "optimism": "optimism", "op": "optimism", "base": "base", "avax": "avax", "avalanche": "avax",
    "evm": "evm_all", "all": "evm_all",
}
CHAIN_LABEL = {"btc": ("Bitcoin", "BTC", "bitcoin"), "ltc": ("Litecoin", "LTC", "litecoin"),
               "doge": ("Dogecoin", "DOGE", "dogecoin"), "sol": ("Solana", "SOL", "solana"),
               "trx": ("Tron", "TRX", "tron")}


def detect_chain(address):
    a = str(address or "").strip()
    if tools.ETH_RE.match(a):
        return "evm"
    if tools.BTC_RE.match(a):
        return "btc"
    if LTC_RE.match(a):
        return "ltc"
    if DOGE_RE.match(a):
        return "doge"
    if TRX_RE.match(a):
        return "trx"
    if SOL_RE.match(a):
        return "sol"
    return None


def valid_for(chain, address):
    if chain in EVM or chain == "evm_all":
        return bool(tools.ETH_RE.match(address))
    return {"btc": tools.BTC_RE, "ltc": LTC_RE, "doge": DOGE_RE, "trx": TRX_RE, "sol": SOL_RE}[chain].match(address) is not None


def saved_wallet_chain(w):
    """Pick the chain for a wallet saved with !crypto add, using its address (+ network name for EVM addresses)."""
    addr = str(w.get("address", ""))
    kind = detect_chain(addr)
    if kind != "evm":
        return kind
    net = str(w.get("network", "")).lower()
    for needle, chain in (("bep", "bsc"), ("bsc", "bsc"), ("bnb", "bsc"), ("poly", "polygon"), ("matic", "polygon"),
                          ("arb", "arbitrum"), ("optim", "optimism"), ("base", "base"), ("avax", "avax")):
        if needle in net:
            return chain
    return "eth"


def resolve_wallet_targets(args, wallets):
    """-> (list of (chain, address, label_or_None), error_or_None)"""
    if not args:
        return [], "usage"
    first = args[0]
    alias = CHAIN_ALIASES.get(first.lower())
    if alias and len(args) >= 2:
        chain, addr = alias, args[1].strip()
        if not valid_for(chain, addr):
            return [], f"That doesn't look like a valid **{first.upper()}** address."
        return [(chain, addr, None)], None
    kind = detect_chain(first)
    if kind:
        chain = kind
        if kind == "evm":
            opt = CHAIN_ALIASES.get(args[1].lower()) if len(args) > 1 else None
            chain = opt if opt in EVM or opt == "evm_all" else "eth"
        return [(chain, first.strip(), None)], None
    matches = tools.find_wallets(wallets, first)
    targets = []
    for key, w in matches[:3]:
        chain = saved_wallet_chain(w)
        if chain:
            targets.append((chain, w["address"], f"{w.get('symbol', key).upper()}" + (f" · {w['network']}" if w.get("network") else "")))
    if targets:
        return targets, None
    return [], "That isn't a recognised address or saved coin. Supported: BTC, LTC, DOGE, ETH/EVM, SOL, TRX."


def _hex_int(value):
    value = value or "0x0"
    return int(value, 16) if len(value) > 2 else 0


async def evm_rpc(urls, method, params):
    last = "no response"
    for url in urls:
        try:
            status, data, _ = await http_json("POST", url, json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        except Exception as exc:
            last = f"{type(exc).__name__}"
            continue
        if status == 200 and isinstance(data, dict) and "result" in data:
            return data["result"]
        last = (data or {}).get("error", {}).get("message", f"HTTP {status}") if isinstance(data, dict) else f"HTTP {status}"
    raise WalletError(f"RPC unavailable ({last})")


async def fetch_blockcypher(coin, address):
    status, data, _ = await http_json("GET", f"https://api.blockcypher.com/v1/{coin}/main/addrs/{address}/balance")
    if status == 200 and isinstance(data, dict) and "balance" in data:
        return {"balance": data["balance"] / 1e8, "pending": data.get("unconfirmed_balance", 0) / 1e8,
                "received": data.get("total_received", 0) / 1e8, "tx": data.get("n_tx"), "source": "blockcypher.com"}
    if status == 429:
        raise WalletError("BlockCypher rate limit hit - try again in a minute")
    raise WalletError(f"Lookup failed (HTTP {status})")


async def fetch_btc(address):
    try:
        status, data, _ = await http_json("GET", f"https://blockstream.info/api/address/{address}")
        if status == 200 and isinstance(data, dict) and "chain_stats" in data:
            c, m = data["chain_stats"], data.get("mempool_stats", {})
            return {"balance": (c["funded_txo_sum"] - c["spent_txo_sum"]) / 1e8,
                    "pending": (m.get("funded_txo_sum", 0) - m.get("spent_txo_sum", 0)) / 1e8,
                    "received": c["funded_txo_sum"] / 1e8, "tx": c.get("tx_count", 0) + m.get("tx_count", 0),
                    "source": "blockstream.info"}
    except Exception:
        pass
    return await fetch_blockcypher("btc", address)


async def fetch_sol(address):
    last = "no response"
    for url in ("https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com"):
        try:
            status, data, _ = await http_json("POST", url, json_body={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [address]})
        except Exception as exc:
            last = type(exc).__name__
            continue
        if status == 200 and isinstance(data, dict) and isinstance(data.get("result"), dict):
            return {"balance": data["result"]["value"] / 1e9, "source": "Solana RPC"}
        last = f"HTTP {status}"
    raise WalletError(f"Solana RPC unavailable ({last})")


async def fetch_trx(address):
    status, data, _ = await http_json("GET", f"https://api.trongrid.io/v1/accounts/{address}")
    if status != 200 or not isinstance(data, dict):
        raise WalletError(f"TronGrid unavailable (HTTP {status})")
    rows = data.get("data") or []
    if not rows:  # never activated on-chain
        return {"balance": 0.0, "tokens": [], "source": "trongrid.io"}
    acct = rows[0]
    usdt = 0
    for entry in acct.get("trc20") or []:
        if TRC20_USDT in entry:
            usdt = int(entry[TRC20_USDT])
    tokens = [("USDT", usdt / 1e6, "tether")] if usdt else []
    return {"balance": acct.get("balance", 0) / 1e6, "tokens": tokens, "source": "trongrid.io"}


async def fetch_evm(chain, address):
    cfg = EVM[chain]
    native = _hex_int(await evm_rpc(cfg["rpc"], "eth_getBalance", [address, "latest"])) / 1e18
    tokens = []
    padded = "0x70a08231" + address[2:].lower().rjust(64, "0")
    for sym, (contract, dec, cg) in cfg["tokens"].items():
        try:
            raw = await evm_rpc(cfg["rpc"], "eth_call", [{"to": contract, "data": padded}, "latest"])
            amount = _hex_int(raw) / 10 ** dec
            if amount:
                tokens.append((sym, amount, cg))
        except WalletError:
            pass
    return {"balance": native, "tokens": tokens, "source": "public RPC"}


async def wallet_lookup(chain, address):
    """-> dict(name, symbol, address, rows=[(label, amount, symbol, cg_id)], extra=[(label, text)], source)"""
    if chain in EVM:
        cfg = EVM[chain]
        d = await fetch_evm(chain, address)
        rows = [(cfg["name"], d["balance"], cfg["symbol"], cfg["cg"])] + [(s, a, s, cg) for s, a, cg in d["tokens"]]
        return {"name": cfg["name"], "symbol": cfg["symbol"], "address": address, "rows": rows, "extra": [], "source": d["source"]}
    name, symbol, cg = CHAIN_LABEL[chain]
    d = await {"btc": fetch_btc, "sol": fetch_sol, "trx": fetch_trx,
               "ltc": lambda a: fetch_blockcypher("ltc", a), "doge": lambda a: fetch_blockcypher("doge", a)}[chain](address)
    rows = [("Balance", d["balance"], symbol, cg)] + [(s, a, s, c) for s, a, c in d.get("tokens", [])]
    extra = []
    if d.get("pending"):
        extra.append(("Pending", f"{'+' if d['pending'] > 0 else ''}{fmt_amount(d['pending'])} {symbol}"))
    if d.get("received"):
        extra.append(("Total received", f"{fmt_amount(d['received'])} {symbol}"))
    if d.get("tx") is not None:
        extra.append(("Transactions", f"{d['tx']:,}"))
    return {"name": name, "symbol": symbol, "address": address, "rows": rows, "extra": extra, "source": d["source"]}


async def fetch_prices(ids):
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    try:
        status, data, _ = await http_json("GET", "https://api.coingecko.com/api/v3/simple/price?vs_currencies=usd&ids=" + ",".join(ids), timeout=10)
        if status == 200 and isinstance(data, dict):
            return {k: v["usd"] for k, v in data.items() if isinstance(v, dict) and "usd" in v}
    except Exception:
        pass
    return {}


def format_wallet(res, prices, label=None):
    lines = [f"## ◈ __**Wallet Balance**__" + (f" ・ {label}" if label else ""),
             f"- **Network:** {res['name']} `{res['symbol']}`",
             f"- **Address:** `{res['address']}`"]
    total = 0.0
    priced = False
    for name, amount, symbol, cg in res["rows"]:
        usd = prices.get(cg)
        value = f" ≈ **{fmt_usd(amount * usd)}**" if usd is not None else ""
        if usd is not None:
            total += amount * usd
            priced = True
        lines.append(f"- **{name}:** **{fmt_amount(amount)} {symbol}**{value}")
    for label_, text in res["extra"]:
        lines.append(f"- **{label_}:** {text}")
    if priced and len(res["rows"]) > 1:
        lines.append(f"- **Total value:** __**{fmt_usd(total)}**__")
    lines.append(f"-# Live data ・ {res['source']} ・ prices: CoinGecko" if priced else f"-# Live data ・ {res['source']}")
    return "\n".join(lines)


# ====================================================================== RICH PRESENCE
RPC_TYPES = {"playing": 0, "streaming": 1, "listening": 2, "watching": 3, "competing": 5}
CLEAR_WORDS = {"clear", "none", "off", "remove", "reset", "-"}
FIELD_ALIASES = {
    "type": "type", "kind": "type", "name": "name", "title": "name",
    "details": "details", "detail": "details", "line1": "details", "state": "state", "line2": "state",
    "large": "large_image", "largeimage": "large_image", "image": "large_image", "img": "large_image",
    "largetext": "large_text", "imagetext": "large_text", "imgtext": "large_text",
    "small": "small_image", "smallimage": "small_image", "icon": "small_image",
    "smalltext": "small_text", "icontext": "small_text",
    "btn1": "btn1", "button1": "btn1", "b1": "btn1", "btn2": "btn2", "button2": "btn2", "b2": "btn2",
    "timer": "timer", "time": "timer", "party": "party", "appid": "app_id", "app": "app_id", "applicationid": "app_id",
    "url": "url", "stream": "url",
}
TEXT_LIMITS = {"name": 128, "details": 128, "state": 128, "large_text": 128, "small_text": 128, "large_image": 512, "small_image": 512}
IMAGE_KEYS = ("large_image", "small_image")


def presence_sig(p):
    """Fingerprint of the dashboard's simple activity, so a change made there switches rich RPC off."""
    return f"{p.get('kind', 'none')}|{p.get('text', '')}|{p.get('url', '')}"


def rpc_defaults():
    return {"enabled": False, "type": "playing", "name": "", "details": "", "state": "", "large_image": "", "large_text": "",
            "small_image": "", "small_text": "", "buttons": [{}, {}], "timer": {"mode": "none", "span": 0, "start": 0, "end": 0},
            "party": [], "app_id": "", "url": "", "sig": ""}


def apply_rpc_field(r, key, value):
    """Mutates r. Returns an error string, or None on success."""
    key = FIELD_ALIASES.get(re.sub(r"[\s_\-]", "", str(key).lower()))
    if key is None:
        return "Unknown field"
    value = str(value or "").strip()
    clear = value.lower() in CLEAR_WORDS or not value
    if key == "type":
        value = value.lower()
        if value not in RPC_TYPES:
            return f"Type must be one of: {', '.join(RPC_TYPES)}"
        r["type"] = value
    elif key in TEXT_LIMITS:
        r[key] = "" if clear else clip(value, TEXT_LIMITS[key])
        if key in IMAGE_KEYS and not clear and not value.startswith(("http://", "https://", "mp:")) and not re.fullmatch(r"[\w\-.]{1,64}", value):
            return "Image must be a direct image URL (https://…) or an uploaded asset key"
    elif key in ("btn1", "btn2"):
        slot = 0 if key == "btn1" else 1
        buttons = list(r.get("buttons") or [{}, {}])
        buttons += [{}] * (2 - len(buttons))
        if clear:
            buttons[slot] = {}
        else:
            if "|" not in value:
                return "Use: Label | https://link"
            label, url = (x.strip() for x in value.split("|", 1))
            if not label or not re.match(r"^https?://\S{3,}$", url) or len(url) > 512:
                return "Button needs a label and a valid http(s) link"
            buttons[slot] = {"label": clip(label, 32), "url": url}
        r["buttons"] = buttons[:2]
    elif key == "timer":
        parts = value.lower().split()
        now = time.time()
        if clear or parts[0] == "none":
            r["timer"] = {"mode": "none", "span": 0, "start": 0, "end": 0}
        elif parts[0] in ("start", "now"):
            r["timer"] = {"mode": "elapsed", "span": 0, "start": now, "end": 0}
        elif parts[0] in ("elapsed", "countdown") and len(parts) > 1:
            span = parse_duration(parts[1])
            if not span:
                return "Give a duration like 90m or 2h30m"
            r["timer"] = ({"mode": "elapsed", "span": span, "start": now - span, "end": 0} if parts[0] == "elapsed"
                          else {"mode": "countdown", "span": span, "start": 0, "end": now + span})
        else:
            return "Timer: start | elapsed <1h30m> | countdown <2h> | none"
    elif key == "party":
        if clear:
            r["party"] = []
        else:
            m = re.fullmatch(r"(\d{1,2})\s*/\s*(\d{1,2})", value)
            if not m or not 1 <= int(m.group(1)) <= int(m.group(2)):
                return "Party looks like 1/5"
            r["party"] = [int(m.group(1)), int(m.group(2))]
    elif key == "app_id":
        if clear:
            r["app_id"] = ""
        elif not re.fullmatch(r"\d{17,20}", value):
            return "Application ID is 17-20 digits (Developer Portal -> your app -> General Information)"
        else:
            r["app_id"] = value
    elif key == "url":
        if not clear and not re.match(r"^https?://\S{3,}$", value):
            return "URL must start with http(s)://"
        r["url"] = "" if clear else value
    return None


def rpc_summary(r, p="!"):
    def row(label, value):
        return f"- **{label}:** {f'`{value}`' if value else '—'}"
    btn = [b for b in (r.get("buttons") or []) if b.get("label")]
    t = r.get("timer") or {}
    timer = {"none": "", "elapsed": f"elapsed (since {rel(t.get('start', 0))})", "countdown": f"countdown (ends {rel(t.get('end', 0))})"}[t.get("mode", "none")]
    lines = ["## ◈ __**Rich Presence**__",
             f"-# {'🟢 active' if r.get('enabled') else '⚪ off'} ・ `{p}rpc set <field> <value>` to edit",
             row("Type", r.get("type")), row("Name", r.get("name")), row("Details", r.get("details")), row("State", r.get("state")),
             row("Large image", clip(r.get("large_image"), 60)), row("Large text", r.get("large_text")),
             row("Small image", clip(r.get("small_image"), 60)), row("Small text", r.get("small_text")),
             "- **Buttons:** " + (" ・ ".join(f"[{b['label']}]" for b in btn) if btn else "—"),
             "- **Timer:** " + (timer or "—"),
             "- **Party:** " + (f"`{r['party'][0]}/{r['party'][1]}`" if r.get("party") else "—"),
             row("App ID", r.get("app_id"))]
    if (r.get("large_image") or r.get("small_image")) and not r.get("app_id"):
        lines.append(f"-# ⚠️ Images need an Application ID: `{p}rpc set appid <id>`")
    return "\n".join(lines)


# ====================================================================== HELP MENU
def help_catalog(p):
    return {
        "general": ("General", "Basics and account info", [
            (f"{p}help [category|all]", "this menu"), (f"{p}ping", "latency check"), (f"{p}info", "account & bot overview"),
            (f"{p}user [user]", "profile details"), (f"{p}server", "current server details"), (f"{p}avatar [user]", "avatar link")]),
        "utility": ("Utility", "Handy everyday tools", [
            (f"{p}purge [n]", "delete your last n messages"), (f"{p}snipe", "show the last deleted message"),
            (f"{p}translate <lang> <text>", "translate text (e.g. `en`, `fr`)"), (f"{p}afk [reason]", "enable AFK auto-notice")]),
        "rpc": ("Rich Presence", "Fully customisable activity: images, buttons, timer, party", [
            (f"{p}rpc <type> <text>", "quick activity: playing, streaming, listening, watching, competing"),
            (f"{p}rpc set <field> <value>", "edit one field (see fields below)"),
            (f"{p}rpc build", "multi-line `field: value` block to set everything at once"),
            (f"{p}rpc show", "view current configuration"), (f"{p}rpc on / off", "toggle without losing settings"),
            (f"{p}rpc clear", "remove the activity completely"),
            (f"{p}rpc save|load|delete <name>", "presets"), (f"{p}rpc presets", "list presets"),
            (f"{p}status <online|idle|dnd|invisible>", "online status"), (f"{p}stream <text> [url]", "quick streaming activity")],
         "**Fields:** `type` `name` `details` `state` `large` `largetext` `small` `smalltext` `btn1` `btn2` `timer` `party` `appid` `url`\n"
         f"**Buttons:** `{p}rpc set btn1 Label | https://link`\n"
         f"**Timer:** `start` ・ `elapsed 1h30m` ・ `countdown 2h` ・ `none`\n"
         "**Images:** direct image URL (https://…) · needs an Application ID from the Developer Portal\n"
         "-# Discord never shows you your own buttons, other people see them."),
        "device": ("Device", "Show as online from another device", [
            (f"{p}device", "show the current device"),
            (f"{p}device <desktop|vr|mobile|console>", "switch device badge (VR = Meta Quest)")],
         "-# Also on the dashboard under Presence. VR/mobile/console open a second session on your account."),
        "automation": ("Automation", "Reactions and replies on autopilot", [
            (f"{p}autoreact <emoji> <user>", "react to everything a user sends"), (f"{p}autoreact off <user>", "stop reacting"),
            (f"{p}autoreact list", "show auto-reacts"), (f"{p}autoreply add <trigger> | <reply>", "reply to a trigger word"),
            (f"{p}autoreply remove <trigger>", "delete an auto-reply"), (f"{p}autoreply list", "show auto-replies")]),
        "moderation": ("Moderation", "Server moderation shortcuts", [
            (f"{p}ban <user> [reason]", "ban a member"), (f"{p}unban <id> [reason]", "unban"), (f"{p}kick <user> [reason]", "kick a member"),
            (f"{p}mute <user> [30m] [reason]", "timeout (default 10m, max 28d)"), (f"{p}unmute <user>", "remove a timeout")]),
        "crypto": ("Crypto & Wallets", "Saved addresses and live balances", [
            (f"{p}wallet <address>", "live balance, auto-detects the network"),
            (f"{p}wallet <chain> <address>", "force a network (btc ltc doge eth bsc polygon arbitrum optimism base avax sol trx)"),
            (f"{p}wallet <address> all", "check an 0x address on every EVM chain"),
            (f"{p}wallet <saved coin>", "balance of a wallet you saved"),
            (f"{p}crypto <coin>", "show a saved deposit address"), (f"{p}crypto list", "list saved wallets"),
            (f"{p}crypto add <coin> <address> [network]", "save an address"), (f"{p}crypto remove <coin>", "delete a saved address")],
         "-# Public addresses only. Stablecoin balances: USDT/USDC on ETH, USDT on BSC and Tron."),
        "gwy": ("Giveaway Hosting", "Run your own giveaways (react with 🎉 to enter)", [
            (f"{p}gwy start <time> <winners> <prize>", "e.g. `1h 2 Nitro Classic`"),
            (f"{p}gwy start <time> <winners> <prize> | <note>", "add a requirement note"),
            (f"{p}gwy end <message id>", "end early and draw winners"), (f"{p}gwy reroll <message id> [n]", "draw new winners"),
            (f"{p}gwy cancel <message id>", "cancel without winners"), (f"{p}gwy list", "running giveaways")],
         "-# Time: `30s` `15m` `2h` `1d` `1w` or combined like `1d12h`. Giveaways survive bot restarts."),
        "snipers": ("Snipers", "Automatic claimers (toggle commands)", [
            (f"{p}gwsniper", "toggle the giveaway sniper"), (f"{p}nitrosniper", "toggle the Nitro sniper"),
            (f"{p}vanity", "vanity sniper (see `" + f"{p}help vanity`)")]),
        "vanity": ("Vanity Sniper", "Watches every server's vanity and your watch list, alerts your webhook", [
            (f"{p}vanity", "status"), (f"{p}vanity on / off", "toggle the sniper"),
            (f"{p}vanity webhook <url|clear>", "set the alert webhook (message is auto-deleted)"),
            (f"{p}vanity watch <code>", "watch a vanity code until it frees up"), (f"{p}vanity unwatch <code>", "stop watching"),
            (f"{p}vanity list", "watch list"), (f"{p}vanity check <code>", "is it free right now?"),
            (f"{p}vanity scan", "vanities of all servers you are in"), (f"{p}vanity interval <10-3600>", "watch-list check speed (seconds)"),
            (f"{p}vanity test", "send a test alert")],
         "-# Falls back to the giveaway webhook when no vanity webhook is set. Vanity changes in your servers are instant."),
    }


def render_help_page(key, p):
    title, blurb, cmds, *rest = help_catalog(p)[key]
    lines = [f"# ◈ __**{title}**__", f"-# {blurb}", ""]
    lines += [f"- **`{usage}`** ⟶ {desc}" for usage, desc in cmds]
    if rest:
        lines += ["", rest[0]]
    return "\n".join(lines)


def render_help_overview(p):
    cat = help_catalog(p)
    total = sum(len(v[2]) for v in cat.values())
    lines = ["# ✦ __**AETHER**__ ✦ Command Center",
             f"-# prefix `{p}` ・ {total} commands ・ `{p}help <category>` ・ `{p}help all`", "", "### __**Categories**__"]
    for key, (title, blurb, *_r) in cat.items():
        lines.append(f"- **{title}** ⟶ `{p}help {key}`")
        lines.append(f"-# {blurb}")
    return "\n".join(lines)


# ====================================================================== VANITY
def classify_vanity_change(old, new):
    old, new = (old or "").lower(), (new or "").lower()
    if old == new:
        return None
    if old and new:
        return "changed"
    return "removed" if old else "set"


async def check_vanity(code):
    """-> ('free'|'taken'|'limited'|'unknown', info)"""
    try:
        status, data, _ = await http_json("GET", f"https://discord.com/api/v9/invites/{code}?with_counts=false", timeout=12)
    except Exception as exc:
        return "unknown", type(exc).__name__
    if status == 404:
        return "free", None
    if status == 200 and isinstance(data, dict):
        guild = data.get("guild") or {}
        return "taken", {"guild_id": guild.get("id"), "guild_name": guild.get("name")}
    if status == 429:
        return "limited", float((data or {}).get("retry_after", 30)) if isinstance(data, dict) else 30.0
    return "unknown", f"HTTP {status}"


# ====================================================================== setup (commands, loops, listeners)
def setup(bot, *, S, update_setting, say, log, get_prefix, get_channel_any, apply_presence, mark, getv):
    import discord
    from discord.ext import tasks

    ending, gwy_fails = set(), {}
    recheck, last_checked = {}, {}
    pause = {"until": 0.0}
    img_cache = {}
    warned = set()

    def cfg(key):
        return copy.deepcopy(S()[key])

    async def send_long(ctx, text, **kw):
        for part in chunk_text(text):
            await say(ctx, part, **kw)

    async def try_delete(ctx):
        try:
            await ctx.message.delete()
        except Exception:
            pass

    for name in ("help", "rpc"):  # replace the old basic versions from selfbot.py
        bot.remove_command(name)

    # ------------------------------------------------------------ help
    @bot.command(name="help", aliases=["commands", "menu"])
    async def help_cmd(ctx, category: str = ""):
        p = get_prefix()
        key = category.lower().strip()
        cat = help_catalog(p)
        if not key:
            return await send_long(ctx, render_help_overview(p))
        if key == "all":
            for k in cat:
                await say(ctx, render_help_page(k, p))
                await asyncio.sleep(0.6)
            return
        aliases = {"presence": "rpc", "rich": "rpc", "wallet": "crypto", "wallets": "crypto", "giveaway": "gwy", "gw": "gwy",
                   "mod": "moderation", "auto": "automation", "sniper": "snipers", "util": "utility", "gen": "general"}
        key = aliases.get(key, key)
        if key not in cat:
            return await say(ctx, f"⚠️ Unknown category **{category}**. Try `{p}help`.", delete_after=8)
        await send_long(ctx, render_help_page(key, p))

    # ------------------------------------------------------------ rich presence
    async def resolve_image(app_id, value):
        value = (value or "").strip()
        if not value:
            return None
        if not value.startswith(("http://", "https://")):
            return value  # mp:… or an uploaded asset key
        key = (app_id, value)
        if key in img_cache:
            return img_cache[key]
        from discord.http import Route
        data = await bot.http.request(Route("POST", "/applications/{app_id}/external-assets", app_id=app_id), json={"urls": [value]})
        path = data[0]["external_asset_path"]
        img_cache[key] = f"mp:{path}"
        return img_cache[key]

    async def build_activity_dict(r):
        d = {"type": RPC_TYPES.get(r.get("type"), 0), "name": r.get("name") or "Aether"}
        app_id = str(r.get("app_id") or "")
        if app_id:
            d["application_id"] = app_id
        for src, dst in (("details", "details"), ("state", "state")):
            if r.get(src):
                d[dst] = r[src]
        t = r.get("timer") or {}
        if t.get("mode") == "elapsed":
            d["timestamps"] = {"start": int(t["start"] * 1000)}
        elif t.get("mode") == "countdown":
            d["timestamps"] = {"end": int(t["end"] * 1000)}
        if r.get("party"):
            d["party"] = {"size": list(r["party"])}
        assets = {}
        for field, a_key, text_key in (("large_image", "large_image", "large_text"), ("small_image", "small_image", "small_text")):
            if not r.get(field):
                continue
            if not app_id:
                if field not in warned:
                    warned.add(field)
                    log("SYSTEM", f"RPC {field} skipped: set an Application ID first (!rpc set appid <id>)", "warning")
                continue
            try:
                resolved = await resolve_image(app_id, r[field])
            except Exception as exc:
                log("SYSTEM", f"RPC {field} could not be resolved: {type(exc).__name__}", "warning")
                continue
            if resolved:
                assets[a_key] = resolved
                if r.get(text_key):
                    assets[text_key] = r[text_key]
        if assets:
            d["assets"] = assets
        if d["type"] == 1:
            d["url"] = r.get("url") or "https://twitch.tv/discord"
        else:
            buttons = [b for b in (r.get("buttons") or []) if b.get("label") and b.get("url")][:2]
            if buttons:
                d["buttons"] = [b["label"] for b in buttons]
                d["metadata"] = {"button_urls": [b["url"] for b in buttons]}
        return d

    _cls = {}

    def make_activity(d):
        if "cls" not in _cls:
            class RichActivity(discord.BaseActivity):
                def __init__(self, data):
                    super().__init__()
                    self._data = data
                    self.type = discord.ActivityType(data["type"])
                    self.name = data["name"]

                def to_dict(self):
                    return dict(self._data)
            _cls["cls"] = RichActivity
        return _cls["cls"](d)

    async def rich_activity():
        """Called by selfbot.apply_presence. None -> rich RPC is off, use the simple activity."""
        r = S()["rpc"]
        if not r.get("enabled"):
            return None
        if r.get("sig") and r["sig"] != presence_sig(S()["presence"]):
            update_setting("rpc", {**r, "enabled": False})  # the dashboard changed the activity -> it wins
            return None
        d = await build_activity_dict(r)
        try:
            return make_activity(d)
        except Exception as exc:
            log("SYSTEM", f"Rich presence class failed ({type(exc).__name__}), using a plain activity", "warning")
            return discord.Activity(type=discord.ActivityType(d["type"]), name=d["name"], state=d.get("state"), details=d.get("details"))

    async def commit_rpc(r, ctx, message, *, enable=True):
        r = dict(r)
        if enable:
            r["enabled"] = True
            r["sig"] = presence_sig(S()["presence"])
        update_setting("rpc", r)
        await apply_presence()
        await say(ctx, message, delete_after=6)

    @bot.command(name="rpc")
    async def rpc_cmd(ctx, *, args: str = ""):
        p = get_prefix()
        sub, rest = split_sub(args)
        r = cfg("rpc")
        if not sub or sub == "show":
            return await send_long(ctx, rpc_summary(r, p))
        if sub == "clear":
            keep = {"app_id": r.get("app_id", ""), "presets": r.get("presets", {})}
            update_setting("rpc", {**rpc_defaults(), **keep})
            update_setting("presence", {**S()["presence"], "kind": "none", "text": ""})
            await apply_presence()
            return await say(ctx, "✅ Activity cleared.", delete_after=5)
        if sub in ("on", "off"):
            if sub == "on":
                return await commit_rpc(r, ctx, "✅ Rich presence **on**.")
            return await commit_rpc({**r, "enabled": False}, ctx, "✅ Rich presence **off**.", enable=False)
        if sub in RPC_TYPES:  # !rpc playing Something
            if not rest:
                return await say(ctx, f"Usage: `{p}rpc {sub} <text>`", delete_after=8)
            r["type"], r["name"] = sub, clip(rest, 128)
            if sub == "streaming" and not r.get("url"):
                r["url"] = "https://twitch.tv/discord"
            return await commit_rpc(r, ctx, f"✅ Now **{sub}** {r['name']}")
        if sub == "set":
            key, _, value = rest.partition(" ")
            if not key:
                return await say(ctx, f"Usage: `{p}rpc set <field> <value>` ・ `{p}help rpc`", delete_after=8)
            err = apply_rpc_field(r, key, value)
            if err:
                return await say(ctx, f"❌ {err}", delete_after=10)
            return await commit_rpc(r, ctx, f"✅ **{key.lower()}** updated.")
        if sub == "build":
            body = rest if rest else ""
            errors, applied = [], 0
            for line in body.splitlines():
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                err = apply_rpc_field(r, key.strip(), value.strip())
                if err:
                    errors.append(f"`{key.strip()}` {err}")
                else:
                    applied += 1
            if not applied:
                return await say(ctx, f"Usage: `{p}rpc build` then one `field: value` per line (Shift+Enter).", delete_after=10)
            await try_delete(ctx)
            note = f"✅ Applied **{applied}** field(s)." + (f"\n⚠️ Skipped: {'; '.join(errors)}" if errors else "")
            return await commit_rpc(r, ctx, note)
        if sub in ("save", "load", "delete", "del", "presets", "list"):
            presets = dict(r.get("presets") or {})
            name = re.sub(r"[^\w\-]", "", rest.lower())[:24]
            if sub in ("presets", "list"):
                return await say(ctx, ("**Presets:** " + ", ".join(f"`{n}`" for n in presets)) if presets else "No presets saved yet.")
            if not name:
                return await say(ctx, f"Usage: `{p}rpc {sub} <name>`", delete_after=8)
            if sub == "save":
                snap = {k: v for k, v in r.items() if k not in ("presets", "enabled", "sig")}
                presets[name] = snap
                update_setting("rpc", {**r, "presets": presets})
                return await say(ctx, f"✅ Preset **{name}** saved.", delete_after=5)
            if name not in presets:
                return await say(ctx, f"❌ No preset named **{name}**.", delete_after=6)
            if sub == "load":
                new = {**rpc_defaults(), **copy.deepcopy(presets[name]), "presets": presets}
                t, now = new.get("timer") or {}, time.time()
                if t.get("mode") == "elapsed":
                    t["start"] = now - t.get("span", 0)
                elif t.get("mode") == "countdown":
                    t["end"] = now + t.get("span", 0)
                new["timer"] = t or rpc_defaults()["timer"]
                return await commit_rpc(new, ctx, f"✅ Loaded preset **{name}**.")
            presets.pop(name)
            update_setting("rpc", {**r, "presets": presets})
            return await say(ctx, f"✅ Preset **{name}** deleted.", delete_after=5)
        await say(ctx, f"⚠️ Unknown option. See `{p}help rpc`.", delete_after=8)

    # ------------------------------------------------------------ device
    @bot.command(name="device")
    async def device_cmd(ctx, *, args: str = ""):
        p = get_prefix()
        sub, rest = split_sub(args)
        d = cfg("device")
        if not sub:
            kind, show = devices.clean_config(d)
            label = devices.PROFILES.get(kind, {}).get("label", "Desktop")
            return await say(ctx, f"## ◈ __**Device**__\n- **Current:** {label}\n"
                                  f"-# `{p}device <desktop|vr|mobile|console>`")
        if sub in devices.DEVICES:
            d["type"] = sub
            update_setting("device", d)
            await apply_presence()
            return await say(ctx, f"✅ Device set to **{sub}**.", delete_after=6)
        await say(ctx, f"⚠️ Options: desktop, vr, mobile, console ・ `{p}help device`", delete_after=8)

    # ------------------------------------------------------------ wallet
    @bot.command(name="wallet", aliases=["bal", "balance"])
    async def wallet_cmd(ctx, *args):
        p = get_prefix()
        targets, err = resolve_wallet_targets(list(args), S()["wallets"])
        if err == "usage":
            return await say(ctx, f"Usage: `{p}wallet <address>` ・ `{p}wallet <chain> <address>` ・ `{p}wallet <saved coin>` ・ see `{p}help crypto`", delete_after=12)
        if err:
            return await say(ctx, f"❌ {err}", delete_after=10)
        async with ctx.typing():
            for chain, address, label in targets:
                chains = list(EVM) if chain == "evm_all" else [chain]
                results = await asyncio.gather(*(wallet_lookup(c, address) for c in chains), return_exceptions=True)
                good = [r for r in results if not isinstance(r, Exception)]
                if not good:
                    msg = next((str(r) for r in results if isinstance(r, WalletError)), "network error")
                    await say(ctx, f"❌ Could not fetch the balance: {msg}")
                    continue
                prices = await fetch_prices({row[3] for r in good for row in r["rows"]})
                if chain == "evm_all":
                    shown = [r for r in good if any(row[1] for row in r["rows"])] or good[:1]
                    text = "\n\n".join(format_wallet(r, prices, label) for r in shown)
                    if len(shown) < len(good):
                        text += f"\n-# {len(good) - len(shown)} other EVM chain(s) hold nothing."
                else:
                    text = format_wallet(good[0], prices, label)
                await send_long(ctx, text)

    # ------------------------------------------------------------ giveaway hosting
    def gwy_text(rec, state="running", winners=None, entries=None):
        host = f"<@{rec['host_id']}>"
        if state == "running":
            lines = [f"# {EMOJI} __**GIVEAWAY**__ {EMOJI}", f"- **Prize:** {rec['prize']}", f"- **Winners:** {rec['winners']}",
                     f"- **Ends:** {rel(rec['end_ts'])} ({full_ts(rec['end_ts'])})", f"- **Hosted by:** {host}"]
            if rec.get("note"):
                lines.append(f"- **Requirement:** {rec['note']}")
            lines.append(f"-# React with {EMOJI} to enter!")
        else:
            title = "GIVEAWAY ENDED" if state == "ended" else "GIVEAWAY CANCELLED"
            lines = [f"#  __**{title}**__", f"- **Prize:** {rec['prize']}"]
            if state == "ended":
                lines.append("- **Winner(s):** " + (", ".join(f"<@{w}>" for w in winners) if winners else "no valid entries"))
                lines.append(f"- **Entries:** {entries or 0}")
            lines.append(f"- **Hosted by:** {host}")
        return "\n".join(lines)

    async def collect_entries(msg):
        users = {}
        for reaction in msg.reactions:
            if str(reaction.emoji) == EMOJI:
                async for user in reaction.users():
                    if not getattr(user, "bot", False) and user.id != bot.user.id:
                        users[user.id] = user
        return list(users.values())

    async def gwy_finish(mid, cancel=False):
        g = cfg("gwyhost")
        rec = g["active"].get(mid)
        if not rec or mid in ending:
            return
        ending.add(mid)
        try:
            channel = await get_channel_any(rec["channel_id"])
            try:
                msg = await channel.fetch_message(int(mid))
            except discord.NotFound:
                msg = None
            winners, entries = [], 0
            if msg and not cancel:
                pool = await collect_entries(msg)
                entries = len(pool)
                winners = [u.id for u in random.sample(pool, min(int(rec["winners"]), len(pool)))]
            if msg:
                await msg.edit(content=gwy_text(rec, "cancelled" if cancel else "ended", winners, entries) + mark)
                if not cancel:
                    if winners:
                        announce = f"{EMOJI} Congratulations {', '.join(f'<@{w}>' for w in winners)}! You won **{rec['prize']}**!\n-# Hosted by <@{rec['host_id']}>"
                    else:
                        announce = f"Nobody entered the giveaway for **{rec['prize']}**."
                    await channel.send(announce + mark, reference=msg)
            g = cfg("gwyhost")
            g["active"].pop(mid, None)
            ended = g.get("ended", {})
            if not cancel:
                ended[mid] = {**rec, "winner_ids": winners}
                for old in sorted(ended, key=lambda k: ended[k].get("end_ts", 0))[:-15]:
                    ended.pop(old, None)
            g["ended"] = ended
            update_setting("gwyhost", g)
            log("SYSTEM", f"Giveaway {'cancelled' if cancel else 'ended'}: {clip(rec['prize'], 60)}")
        except Exception as exc:
            gwy_fails[mid] = gwy_fails.get(mid, 0) + 1
            log("SYSTEM", f"Giveaway end failed: {type(exc).__name__}", "error")
            if gwy_fails[mid] >= 5:  # give up instead of retrying forever
                g = cfg("gwyhost")
                g["active"].pop(mid, None)
                update_setting("gwyhost", g)
        finally:
            ending.discard(mid)

    async def gwy_tick():
        now = time.time()
        for mid, rec in list(cfg("gwyhost")["active"].items()):
            if rec["end_ts"] <= now and mid not in ending:
                asyncio.ensure_future(gwy_finish(mid))

    @bot.command(name="gwy", aliases=["giveaway", "gw"])
    async def gwy_cmd(ctx, *, args: str = ""):
        p = get_prefix()
        sub, rest = split_sub(args)
        g = cfg("gwyhost")
        if sub in ("start", "create", "host"):
            if not ctx.guild:
                return await say(ctx, "⚠️ Use this in a server channel.", delete_after=6)
            note = ""
            if "|" in rest:
                rest, _, note = (x.strip() for x in rest.partition("|"))
            tokens = rest.split(maxsplit=2)
            seconds = parse_duration(tokens[0]) if tokens else None
            wm = re.fullmatch(r"(\d{1,2})w?", tokens[1]) if len(tokens) > 1 else None
            if not seconds or not wm or len(tokens) < 3:
                return await say(ctx, f"Usage: `{p}gwy start <time> <winners> <prize> [| requirement]` ・ e.g. `{p}gwy start 1h 2 Nitro Classic`", delete_after=12)
            if not 10 <= seconds <= 60 * 86400:
                return await say(ctx, "❌ Duration must be between 10s and 60d.", delete_after=8)
            winners = max(1, int(wm.group(1)))
            rec = {"channel_id": str(ctx.channel.id), "guild_id": str(ctx.guild.id), "prize": clip(tokens[2], 200), "winners": winners,
                   "end_ts": time.time() + seconds, "host_id": str(bot.user.id), "note": clip(note, 200)}
            await try_delete(ctx)
            msg = await ctx.channel.send(gwy_text(rec) + mark)
            try:
                await msg.add_reaction(EMOJI)
            except Exception:
                pass
            g["active"][str(msg.id)] = rec
            update_setting("gwyhost", g)
            log("SYSTEM", f"Giveaway started: {clip(rec['prize'], 60)}", **{"guild_name": ctx.guild.name, "guild_id": str(ctx.guild.id)})
            return
        if sub in ("end", "cancel", "reroll"):
            mid = re.sub(r"\D", "", rest.split()[0]) if rest else ""
            if sub in ("end", "cancel"):
                if mid not in g["active"]:
                    return await say(ctx, "❌ No running giveaway with that message ID. See `" + f"{p}gwy list`.", delete_after=8)
                await try_delete(ctx)
                return await gwy_finish(mid, cancel=(sub == "cancel"))
            if sub == "reroll":
                rec = g.get("ended", {}).get(mid)
                if not rec:
                    return await say(ctx, "❌ I can only reroll the last 15 giveaways this bot ended.", delete_after=8)
                try:
                    count = int(rest.split()[1]) if len(rest.split()) > 1 else int(rec["winners"])
                except ValueError:
                    count = int(rec["winners"])
                channel = await get_channel_any(rec["channel_id"])
                msg = await channel.fetch_message(int(mid))
                pool = await collect_entries(msg)
                if not pool:
                    return await say(ctx, " No valid entries to pick from.", delete_after=8)
                picked = random.sample(pool, min(count, len(pool)))
                await channel.send(f" New winner(s) for **{rec['prize']}**: {', '.join(u.mention for u in picked)} {EMOJI}{mark}", reference=msg)
                return await try_delete(ctx)
        if sub == "list":
            if not g["active"]:
                return await say(ctx, "No running giveaways.")
            lines = ["## ◈ __**Running Giveaways**__"]
            for mid, rec in sorted(g["active"].items(), key=lambda kv: kv[1]["end_ts"]):
                lines.append(f"- **{clip(rec['prize'], 50)}** ・ {rec['winners']} winner(s) ・ ends {rel(rec['end_ts'])} ・ <#{rec['channel_id']}>")
                lines.append(f"-# id `{mid}`")
            return await send_long(ctx, "\n".join(lines))
        await say(ctx, f"Giveaway hosting ⟶ `{p}help gwy`", delete_after=10)

    # ------------------------------------------------------------ vanity sniper
    def webhook_target():
        v = S()["vanity"]
        return v.get("webhook_url") or S()["giveaway"].get("webhook_url") or ""

    async def send_webhook(embed):
        url = webhook_target()
        if not url:
            return False
        v = S()["vanity"]
        payload = {"username": v.get("webhook_name") or "Aether Vanity Sniper", "embeds": [embed], "allowed_mentions": {"parse": []}}
        if v.get("webhook_avatar"):
            payload["avatar_url"] = v["webhook_avatar"]
        try:
            status, _, _ = await http_json("POST", url, json_body=payload)
            return status in (200, 204)
        except Exception as exc:
            print("Vanity webhook failed:", exc)
            return False

    def vembed(title, color, desc, fields):
        return {"title": title, "description": desc, "color": color, "timestamp": iso_now(),
                "fields": [{"name": n, "value": clip(v, 1000), "inline": inl} for n, v, inl in fields],
                "footer": {"text": f"Aether Vanity Sniper ・ {bot.user.name}"}}

    async def verify_free(code, tries=(0, 2, 5)):
        status, info = "unknown", None
        for delay in tries:
            if delay:
                await asyncio.sleep(delay)
            status, info = await check_vanity(code)
            if status == "free":
                break
        return status, info

    async def vanity_alert_change(guild, old, new):
        v = S()["vanity"]
        kind = classify_vanity_change(old, new)
        base = [("Server", f"{guild.name}\n`{guild.id}`", True)]
        members = getv(guild, "member_count")
        if members:
            base.append(("Members", f"{members:,}", True))
        base.append(("When", f"{rel(time.time())}", True))
        sent = False
        if kind in ("changed", "removed") and v.get("notify_free", True):
            status, info = await verify_free(old)
            if status == "free":
                state = "🟢 **Free right now**"
            elif status == "taken":
                owner = (info or {}).get("guild_name")
                state = "🟡 Still resolving" + (f" (shows **{owner}**)" if owner else "") + ", re-checking for 10 minutes"
                recheck[old] = time.time() + 600
            else:
                state = "⚪ Could not verify"
                recheck[old] = time.time() + 600
            title = "Vanity Released" if kind == "removed" else "Vanity Changed"
            fields = base + [("Old vanity", f"`discord.gg/{old}`", True)] + ([("New vanity", f"`discord.gg/{new}`", True)] if new else []) + [("Status", state, False)]
            sent = await send_webhook(vembed(title, 0x57F287 if status == "free" else 0xFEE75C, f"The vanity **{old}** left **{guild.name}**.", fields))
        elif kind in ("set", "changed") and v.get("notify_changes", True):
            fields = base + ([("Old vanity", f"`discord.gg/{old}`", True)] if old else []) + [("New vanity", f"`discord.gg/{new}`", True)]
            sent = await send_webhook(vembed("🆕 Vanity Set", 0x5865F2, f"**{guild.name}** now uses **{new}**.", fields))
        log("GUILD", f"Vanity {kind}: {old or '-'} -> {new or '-'}" + (" (webhook sent)" if sent else ""),
            guild_name=guild.name, guild_id=str(guild.id))

    async def on_guild_update(before, after):
        v = cfg("vanity")
        gid = str(after.id)
        new = (getv(after, "vanity_url_code", "") or "").lower()
        old = v["known"].get(gid, (getv(before, "vanity_url_code", "") or "").lower())
        if old == new:
            return
        v["known"][gid] = new
        update_setting("vanity", v)
        if v.get("enabled"):
            await vanity_alert_change(after, old, new)

    async def on_guild_join(guild):
        v = cfg("vanity")
        v["known"][str(guild.id)] = (getv(guild, "vanity_url_code", "") or "").lower()
        update_setting("vanity", v)

    async def on_guild_remove(guild):
        v = cfg("vanity")
        if v["known"].pop(str(guild.id), None) is not None:
            update_setting("vanity", v)

    async def vanity_baseline():
        """On connect: compare every server's vanity with what we saw last time (catches changes made while offline)."""
        v = cfg("vanity")
        known, changes = v["known"], []
        current = {str(g.id): ((getv(g, "vanity_url_code", "") or "").lower(), g) for g in bot.guilds}
        for gid, (code, guild) in current.items():
            if gid in known and known[gid] != code:
                changes.append((guild, known[gid], code))
            known[gid] = code
        for gid in [k for k in known if k not in current]:
            known.pop(gid)
        update_setting("vanity", v)
        if v.get("enabled"):
            for guild, old, new in changes:
                await vanity_alert_change(guild, old, new)
                await asyncio.sleep(1)

    async def vanity_tick():
        v = cfg("vanity")
        now = time.time()
        if not v.get("enabled") or now < pause["until"]:
            return
        for code in [c for c, until in recheck.items() if until <= now]:
            recheck.pop(code, None)
        interval = max(10, int(v.get("interval") or 30))
        codes = list(dict.fromkeys(list(v.get("watch", [])) + list(recheck)))
        alerted = set(v.get("free_alerted", []))
        dirty = False
        for code in codes:
            if last_checked.get(code, 0) + interval > time.time():
                continue
            last_checked[code] = time.time()
            status, info = await check_vanity(code)
            if status == "limited":
                pause["until"] = time.time() + float(info) + 1
                break
            if status == "free" and code not in alerted:
                alerted.add(code)
                dirty = True
                recheck.pop(code, None)
                await send_webhook(vembed("🟢 Vanity Available", 0x57F287, f"**discord.gg/{code}** is free right now.",
                                          [("Code", f"`{code}`", True), ("Link", f"discord.gg/{code}", True), ("Detected", rel(time.time()), True)]))
                log("GUILD", f"Vanity {code} is free (webhook sent)")
            elif status == "taken" and code in alerted:
                alerted.discard(code)
                dirty = True
            await asyncio.sleep(1.5)
        if dirty:
            v = cfg("vanity")
            v["free_alerted"] = sorted(alerted)
            update_setting("vanity", v)

    @bot.command(name="vanity")
    async def vanity_cmd(ctx, *, args: str = ""):
        p = get_prefix()
        sub, rest = split_sub(args)
        v = cfg("vanity")
        if sub in ("", "status"):
            hook = "✅ vanity webhook" if v.get("webhook_url") else ("✅ giveaway webhook (fallback)" if S()["giveaway"].get("webhook_url") else "❌ not set")
            guilds_with = sum(1 for c in v["known"].values() if c)
            return await send_long(ctx, "\n".join([
                "## ◈ __**Vanity Sniper**__", f"-# `{p}help vanity` for all commands",
                f"- **Status:** {'🟢 on' if v.get('enabled') else '⚪ off'}", f"- **Webhook:** {hook}",
                f"- **Servers tracked:** {len(v['known'])} ({guilds_with} with a vanity)",
                f"- **Watch list:** {', '.join(f'`{c}`' for c in v['watch']) or '—'}", f"- **Check every:** {v.get('interval', 30)}s"]))
        if sub in ("on", "off"):
            v["enabled"] = sub == "on"
            update_setting("vanity", v)
            return await say(ctx, f"✅ Vanity sniper **{'enabled' if v['enabled'] else 'disabled'}**." + ("" if webhook_target() or sub == "off" else f"\n⚠️ No webhook yet: `{p}vanity webhook <url>`"), delete_after=10)
        if sub == "webhook":
            await try_delete(ctx)  # never leave the webhook URL sitting in a channel
            if rest.lower() in ("clear", "none", "off"):
                v["webhook_url"] = ""
                update_setting("vanity", v)
                return await say(ctx, "✅ Vanity webhook cleared.", delete_after=6)
            if not DISCORD_WEBHOOK_RE.match(rest):
                return await say(ctx, "❌ That is not a Discord webhook URL.", delete_after=8)
            v["webhook_url"] = rest
            update_setting("vanity", v)
            return await say(ctx, "✅ Vanity webhook saved (your message was deleted for safety).", delete_after=8)
        if sub in ("watch", "add", "unwatch", "remove"):
            code = rest.lower().replace("discord.gg/", "").strip("/ ")
            if not VANITY_RE.match(code):
                return await say(ctx, "❌ Vanity codes are 2-32 letters, numbers or hyphens.", delete_after=8)
            if sub in ("watch", "add"):
                if code not in v["watch"]:
                    v["watch"].append(code)
                v["free_alerted"] = [c for c in v.get("free_alerted", []) if c != code]
                update_setting("vanity", v)
                return await say(ctx, f"✅ Watching **{code}**" + ("" if v.get("enabled") else f" (turn the sniper on: `{p}vanity on`)"), delete_after=8)
            if code in v["watch"]:
                v["watch"].remove(code)
                update_setting("vanity", v)
            return await say(ctx, f"✅ Stopped watching **{code}**.", delete_after=6)
        if sub == "list":
            return await say(ctx, ("**Watching:** " + ", ".join(f"`{c}`" for c in v["watch"])) if v["watch"] else "Watch list is empty.")
        if sub == "interval":
            if not rest.isdigit() or not 10 <= int(rest) <= 3600:
                return await say(ctx, "❌ Pick between 10 and 3600 seconds.", delete_after=8)
            v["interval"] = int(rest)
            update_setting("vanity", v)
            return await say(ctx, f"✅ Checking the watch list every **{rest}s**.", delete_after=6)
        if sub == "check":
            code = rest.lower().replace("discord.gg/", "").strip("/ ")
            if not VANITY_RE.match(code):
                return await say(ctx, "❌ Vanity codes are 2-32 letters, numbers or hyphens.", delete_after=8)
            status, info = await check_vanity(code)
            text = {"free": "🟢 **free** right now", "taken": "🔴 **taken**" + (f" by **{info['guild_name']}**" if info and info.get("guild_name") else ""),
                    "limited": "⏳ rate limited, try again shortly", "unknown": f"⚪ could not check ({info})"}[status]
            return await say(ctx, f"`discord.gg/{code}` ⟶ {text}")
        if sub == "scan":
            rows = sorted(((getv(g, "vanity_url_code", "") or "", g.name) for g in bot.guilds if getv(g, "vanity_url_code", "")), key=lambda x: x[0])
            if not rows:
                return await say(ctx, "None of your servers has a vanity URL.")
            return await send_long(ctx, "## ◈ __**Server Vanities**__\n" + "\n".join(f"- **{clip(n, 40)}** ⟶ `{c}`" for c, n in rows))
        if sub == "test":
            ok = await send_webhook(vembed("✅ Vanity Sniper Test", 0x5865F2, "Your webhook is working.", [("Account", bot.user.name, True)]))
            return await say(ctx, "✅ Test alert sent." if ok else "❌ Could not post to the webhook (not set, or Discord refused it).", delete_after=8)
        await say(ctx, f"⚠️ Unknown option. See `{p}help vanity`.", delete_after=8)

    # ------------------------------------------------------------ listeners + loops
    bot.add_listener(on_guild_update, "on_guild_update")
    bot.add_listener(on_guild_join, "on_guild_join")
    bot.add_listener(on_guild_remove, "on_guild_remove")

    async def _safe(label, fn):
        try:
            await fn()
        except Exception as exc:
            print(f"{label} error:", repr(exc))

    async def _gwy_job():
        await _safe("gwy", gwy_tick)

    async def _vanity_job():
        await _safe("vanity", vanity_tick)

    gwy_loop = tasks.loop(seconds=5)(_gwy_job)
    vanity_loop = tasks.loop(seconds=5)(_vanity_job)

    async def start():
        await _safe("vanity baseline", vanity_baseline)
        for loop in (gwy_loop, vanity_loop):
            if not loop.is_running():
                loop.start()

    return SimpleNamespace(rich_activity=rich_activity, start=start)
