"""Small helpers shared by the dashboard and the bot worker."""
import re

BTC_RE = re.compile(r"^(bc1[0-9a-z]{11,87}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$")
ETH_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
HEX64_RE = re.compile(r"^(0x)?[a-fA-F0-9]{64}$")
SLUG_RE = re.compile(r"[^a-z0-9]+")


def translate_text(lang, text):
    try:
        from deep_translator import GoogleTranslator
    except ImportError:
        raise RuntimeError("deep-translator is not installed (pip install deep-translator)")
    return GoogleTranslator(source="auto", target=lang).translate(text)


def wallet_key(symbol, network=""):
    symbol = SLUG_RE.sub("", str(symbol).lower())
    network = SLUG_RE.sub("", str(network).lower())
    if not symbol:
        raise ValueError("Enter a coin symbol")
    return f"{symbol}-{network}" if network else symbol


def validate_wallet(symbol, address):
    """Only PUBLIC deposit addresses are stored. Raises ValueError for anything suspicious."""
    address = str(address or "").strip()
    if not 8 <= len(address) <= 200 or re.search(r"\s", address):
        raise ValueError("Address must be 8-200 characters with no spaces")
    if HEX64_RE.match(address):
        raise ValueError("That looks like a private key. Only save public deposit addresses, never keys or seed phrases.")
    sym = str(symbol).lower()
    if sym == "btc" and not BTC_RE.match(address):
        raise ValueError("That is not a valid Bitcoin address")
    if sym == "eth" and not ETH_RE.match(address):
        raise ValueError("That is not a valid Ethereum address (0x + 40 hex characters)")
    return address


def find_wallets(wallets, query):
    """Entries matching a coin symbol, name or key prefix (so 'usdt' finds usdt-trc20 and usdt-erc20)."""
    q = SLUG_RE.sub("", str(query).lower())
    out = []
    for key, w in (wallets or {}).items():
        sym = SLUG_RE.sub("", str(w.get("symbol", key.split("-")[0])).lower())
        name = SLUG_RE.sub("", str(w.get("coin", "")).lower())
        if q and (q == key.replace("-", "") or q == sym or q == name or key.startswith(q + "-")):
            out.append((key, w))
    return out
