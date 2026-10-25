"""
Extra worker actions for the dashboard (run inside selfbot.py, one process per account):

    dev_overview   every Developer Portal application + team of this account, with links and info
    dev_token      reset + reveal ONE application's bot token (Discord never shows an existing token again)
    shop_catalog   Discord Shop: avatar decorations, nameplates and profile effects for the preview page
    dm_history2    DM history with message ids, mentions and reply references
    dm_members     people you can @mention in a DM / group DM
    send2          send a message, optionally as a reply to a message id

Wired up from selfbot.py:   ACTIONS.update(portal.build(bot, ...))
Tokens are never written to the logs; the dashboard also scrubs the finished job row.
"""
import re
import time

import discord

try:
    from discord.http import Route
except Exception:  # pragma: no cover - depends on the installed discord.py-self
    Route = None

CDN = "https://cdn.discordapp.com"
SNOWFLAKE = re.compile(r"^\d{5,25}$")
# application.flags bits for privileged gateway intents
INTENT_FLAGS = ((1 << 12, "Presence intent"), (1 << 13, "Presence intent (limited)"),
                (1 << 14, "Server Members intent"), (1 << 15, "Server Members intent (limited)"),
                (1 << 18, "Message Content intent"), (1 << 19, "Message Content intent (limited)"))
TEAM_ROLES = {"admin": "Admin", "developer": "Developer", "read_only": "Read-only"}
PALETTES = {"crimson": "#be1d2f", "berry": "#a43aa4", "sky": "#3a8bd9", "teal": "#1f9d9d", "forest": "#2f7d3f",
            "bubble_gum": "#f06ab8", "violet": "#7a4ad9", "cobalt": "#2a4fc9", "clover": "#4a9a3a"}
SHOP_TTL = 600


def clip(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


def build(bot, *, get_channel_any, iso, log, getv, rel_type):
    shop_cache = {"t": 0.0, "data": None}

    # ------------------------------------------------------------ raw API
    async def api(method, path, json=None):
        if Route is None:
            raise RuntimeError("This discord.py-self version cannot make raw API calls")
        route = Route(method, path)
        return await (bot.http.request(route, json=json) if json is not None else bot.http.request(route))

    def explain(exc):
        if isinstance(exc, discord.HTTPException):
            if getattr(exc, "code", 0) == 60003:
                return "Discord wants your 2FA code for this - enter the 6-digit authenticator code and try again."
            if exc.status == 403:
                return "Discord refused (403). This account may not have access, or it needs to verify first."
            if exc.status == 404:
                return "Not found (404) - the application may have no bot user yet, or it was deleted."
            if exc.status == 429:
                return "Rate limited by Discord - wait a minute and try again."
            return f"Discord said {exc.status}: {clip(getattr(exc, 'text', ''), 160)}"
        return f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------ developer portal
    def app_row(a):
        aid = str(a.get("id"))
        team = a.get("team") or {}
        bot_user = a.get("bot") or {}
        flags = int(a.get("flags") or 0)
        icon = a.get("icon")
        try:
            created = iso(discord.utils.snowflake_time(int(aid)))
        except Exception:
            created = None
        return {
            "id": aid, "name": a.get("name") or "Unnamed", "description": clip(a.get("description"), 280),
            "icon": f"{CDN}/app-icons/{aid}/{icon}.png?size=128" if icon else None, "created": created,
            "public": bool(a.get("bot_public")), "code_grant": bool(a.get("bot_require_code_grant")),
            "public_key": a.get("verify_key") or "",
            "bot": {"id": str(bot_user.get("id")), "name": bot_user.get("username")} if bot_user else None,
            "team_id": str(team["id"]) if team.get("id") else None, "team_name": team.get("name"),
            "intents": [label for bit, label in INTENT_FLAGS if flags & bit],
            "guilds": a.get("approximate_guild_count"), "tags": (a.get("tags") or [])[:6],
            "interactions_url": a.get("interactions_endpoint_url") or "",
            "redirects": (a.get("redirect_uris") or [])[:5],
        }

    def team_row(t, apps):
        tid = str(t.get("id"))
        owner = str(t.get("owner_user_id") or "")
        members = []
        for m in t.get("members") or []:
            u = m.get("user") or {}
            members.append({"id": str(u.get("id", "")), "name": u.get("global_name") or u.get("username") or "?",
                            "role": TEAM_ROLES.get(m.get("role"), str(m.get("role") or "member").title()),
                            "owner": str(u.get("id", "")) == owner, "pending": m.get("membership_state") == 1})
        icon = t.get("icon")
        return {"id": tid, "name": t.get("name") or "Team", "owner_id": owner,
                "icon": f"{CDN}/team-icons/{tid}/{icon}.png?size=128" if icon else None,
                "members": members[:25], "apps": [a["id"] for a in apps if a["team_id"] == tid]}

    async def dev_overview(_p):
        try:
            raw_apps = await api("GET", "/applications?with_team_applications=true")
        except Exception as exc:
            raise ValueError(explain(exc))
        apps = [app_row(a) for a in (raw_apps if isinstance(raw_apps, list) else [])]
        try:
            raw_teams = await api("GET", "/teams")
        except Exception:
            raw_teams = []
        teams = [team_row(t, apps) for t in (raw_teams if isinstance(raw_teams, list) else [])]
        known = {t["id"] for t in teams}  # a team that only appears on an app still gets a card
        for a in apps:
            if a["team_id"] and a["team_id"] not in known:
                known.add(a["team_id"])
                teams.append({"id": a["team_id"], "name": a["team_name"] or "Team", "owner_id": "", "icon": None,
                              "members": [], "apps": [x["id"] for x in apps if x["team_id"] == a["team_id"]]})
        apps.sort(key=lambda a: (a["team_name"] or "", a["name"].lower()))
        return {"apps": apps, "teams": teams}

    async def dev_token(p):
        aid = str(p.get("app_id", "")).strip()
        if not SNOWFLAKE.match(aid):
            raise ValueError("Invalid application id")
        if not p.get("confirm"):
            raise ValueError("Confirmation required: resetting a token logs out whatever is using the old one")
        body = {}
        code = re.sub(r"\D", "", str(p.get("code") or ""))[:8]
        if code:
            body["code"] = code
        try:
            data = await api("POST", f"/applications/{aid}/bot/reset", json=body)
        except Exception as exc:
            raise ValueError(explain(exc))
        token = (data or {}).get("token")
        if not token:
            raise ValueError("Discord did not return a token for that application")
        log("SYSTEM", f"Developer Portal: bot token reset for application {aid}", "warning")  # id only - never the token
        return {"title": "Token reset", "token": token, "app_id": aid,
                "text": "Copy it now - Discord will not show this token again. Any bot using the old token is logged out."}

    # ------------------------------------------------------------ shop
    def walk_price(obj):
        if isinstance(obj, dict):
            if "amount" in obj and "currency" in obj:
                return obj
            for v in obj.values():
                hit = walk_price(v)
                if hit:
                    return hit
        elif isinstance(obj, list):
            for v in obj:
                hit = walk_price(v)
                if hit:
                    return hit
        return None

    def price_text(product):
        hit = walk_price(product.get("prices") or product.get("price") or {})
        if not hit:
            return ""
        try:
            amount, cur = float(hit["amount"]), str(hit["currency"]).lower()
            if "orb" in cur:
                return f"{int(amount):,} Orbs"
            return f"{amount / (10 ** int(hit.get('exponent', 2))):.2f} {cur.upper()}"
        except (TypeError, ValueError):
            return ""

    def kind_of(item, effects, cat_name):
        if "palette" in item or item.get("type") == 2:
            return "nameplate"
        if str(item.get("id")) in effects or str(item.get("sku_id")) in effects or item.get("type") == 1 \
                or "effects" in item or "thumbnailPreviewSrc" in item:
            return "effect"
        if item.get("type") == 0 or item.get("asset"):
            low = cat_name.lower()
            return "nameplate" if "nameplate" in low else ("effect" if "effect" in low else "decoration")
        low = cat_name.lower()
        return "nameplate" if "nameplate" in low else ("effect" if "effect" in low else None)

    def shop_item(item, product, cat_name, effects):
        kind = kind_of(item, effects, cat_name)
        if not kind:
            return None
        sku = str(item.get("sku_id") or product.get("sku_id") or item.get("id") or "")
        base = {"kind": kind, "id": str(item.get("id") or sku), "sku_id": sku, "category": cat_name,
                "name": item.get("label") or product.get("name") or "Unnamed", "summary": clip(product.get("summary"), 160),
                "price": price_text(product), "url": f"https://discord.com/shop#itemSkuId={sku}" if sku else ""}
        if kind == "decoration":
            asset = str(item.get("asset") or "")
            if not asset:
                return None
            base.update(asset=asset, thumb=f"{CDN}/avatar-decoration-presets/{asset}.png?size=160&passthrough=false",
                        preview=f"{CDN}/avatar-decoration-presets/{asset}.png?size=240&passthrough=true", animated=asset.startswith("a_"))
        elif kind == "nameplate":
            asset = str(item.get("asset") or "")
            if not asset:
                return None
            palette = str(item.get("palette") or "")
            base.update(asset=asset, palette=palette, color=PALETTES.get(palette, "#2b2d31"),
                        thumb=f"{CDN}/assets/collectibles/{asset}static.png", video=f"{CDN}/assets/collectibles/{asset}asset.webm", animated=True)
        else:
            cfg = effects.get(str(item.get("id"))) or effects.get(sku) or item
            layers = [{"src": e.get("src"), "z": e.get("zIndex", 0)} for e in (cfg.get("effects") or []) if e.get("src")][:6]
            thumb = cfg.get("thumbnailPreviewSrc") or cfg.get("reducedMotionSrc") or ""
            if not (thumb or layers):
                return None
            base.update(name=cfg.get("title") or base["name"], thumb=thumb, layers=layers, animated=bool(layers),
                        summary=clip(cfg.get("description") or base["summary"], 160))
        return base

    async def shop_catalog(p):
        if shop_cache["data"] and not p.get("refresh") and time.time() - shop_cache["t"] < SHOP_TTL:
            return shop_cache["data"]
        try:
            try:
                cats = await api("GET", "/collectibles-categories?include_bundles=true")
            except discord.HTTPException:
                cats = await api("GET", "/collectibles-categories")
        except Exception as exc:
            raise ValueError("Could not load the shop: " + explain(exc))
        effects = {}
        try:
            data = await api("GET", "/user-profile-effects")
            for e in (data or {}).get("profile_effect_configs", []) or []:
                effects[str(e.get("id"))] = e
                if e.get("sku_id"):
                    effects[str(e["sku_id"])] = e
        except Exception:
            pass  # effects then fall back to whatever the category itself carries
        items, seen = [], set()
        for cat in cats if isinstance(cats, list) else []:
            cat_name = cat.get("name") or "Shop"
            for product in cat.get("products") or []:
                for item in (product.get("items") or [product]):
                    row = shop_item(item, product, cat_name, effects)
                    if row and (row["kind"], row["id"]) not in seen:
                        seen.add((row["kind"], row["id"]))
                        items.append(row)
        out = {"items": items, "categories": sorted({i["category"] for i in items}),
               "counts": {k: sum(1 for i in items if i["kind"] == k) for k in ("decoration", "nameplate", "effect")}}
        shop_cache.update(t=time.time(), data=out)
        return out

    # ------------------------------------------------------------ inbox: members, history, reply
    def person(u):
        return {"id": str(u.id), "username": getattr(u, "name", ""), "display_name": getattr(u, "display_name", None) or getattr(u, "name", "?"),
                "avatar": _avatar(u)}

    def _avatar(u):
        try:
            asset = getv(u, "display_avatar")
            return asset.with_size(64).url if asset else None
        except Exception:
            return None

    async def dm_members(p):
        channel = await get_channel_any(p["channel_id"])
        people = {}

        def add(u):
            if u is not None and str(u.id) not in people:
                people[str(u.id)] = person(u)
        if isinstance(channel, discord.DMChannel):
            add(getv(channel, "recipient"))
        else:
            for u in getv(channel, "recipients", []) or []:
                add(u)
        add(bot.user)
        for rel in list(getv(bot, "relationships", []) or [])[:300]:  # friends are mentionable too
            if rel_type(rel) == "friend":
                add(getv(rel, "user"))
        return {"members": list(people.values())[:300]}

    async def dm_history2(p):
        channel = await get_channel_any(p["channel_id"])
        limit = min(max(int(p.get("limit", 40)), 1), 100)
        msgs = [m async for m in channel.history(limit=limit)]
        msgs.reverse()
        out = []
        for m in msgs:
            ref = None
            if m.reference and m.reference.message_id:
                resolved = getv(m.reference, "resolved")
                if resolved is not None and hasattr(resolved, "author"):
                    ref = {"id": str(m.reference.message_id), "author": resolved.author.display_name,
                           "preview": clip(resolved.content or "(attachment)", 90)}
                else:
                    ref = {"id": str(m.reference.message_id), "author": "", "preview": "original message"}
            out.append({"id": str(m.id), "author": m.author.display_name, "author_id": str(m.author.id),
                        "mine": m.author.id == bot.user.id, "content": (m.content or "")[:2000], "ts": iso(m.created_at),
                        "attachments": [a.url for a in m.attachments][:6], "embeds": len(m.embeds), "ref": ref,
                        "mentions": [{"id": str(u.id), "name": u.display_name} for u in (m.mentions or [])][:20]})
        return {"messages": out}

    async def send2(p):
        content = str(p.get("content") or "").strip()
        if not content:
            raise ValueError("Message is empty")
        channel = await get_channel_any(p["channel_id"])
        reply_to = str(p.get("reply_to") or "").strip()
        kwargs = {}
        if reply_to:
            if not SNOWFLAKE.match(reply_to):
                raise ValueError("Invalid message to reply to")
            kwargs["reference"] = discord.MessageReference(message_id=int(reply_to), channel_id=channel.id, fail_if_not_exists=False)
        msg = await channel.send(content[:2000], **kwargs)
        return {"title": "Replied" if reply_to else "Sent", "fields": [["Message ID", str(msg.id)]]}

    # ------------------------------------------------------------ HypeSquad badge
    HOUSES = {1: "Bravery", 2: "Brilliance", 3: "Balance"}

    async def hypesquad(p):
        choice = str(p.get("house", "")).strip().lower()
        if choice in ("leave", "none", "0"):
            try:
                await api("DELETE", "/hypesquad/online")
            except Exception as exc:
                raise ValueError(explain(exc))
            log("SYSTEM", "HypeSquad house removed from the dashboard")
            return {"title": "HypeSquad badge removed", "text": "The house badge was removed from this account."}
        if choice not in ("1", "2", "3"):
            raise ValueError("Pick House Bravery, Brilliance or Balance")
        try:
            await api("POST", "/hypesquad/online", json={"house_id": int(choice)})
        except Exception as exc:
            raise ValueError(explain(exc))
        log("SYSTEM", f"HypeSquad house set to {HOUSES[int(choice)]} from the dashboard")
        return {"title": f"HypeSquad {HOUSES[int(choice)]}", "text": f"This account is now in House {HOUSES[int(choice)]}. It can take a moment to show on the profile."}

    return {"hypesquad": hypesquad, "dev_overview": dev_overview, "dev_token": dev_token, "shop_catalog": shop_catalog,
            "dm_members": dm_members, "dm_history2": dm_history2, "send2": send2}
