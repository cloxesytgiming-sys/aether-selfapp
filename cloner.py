"""
Server cloner - dashboard only. Runs inside the selected account's worker.

    clone_guilds   servers this account is in (+ whether it may change them, + size of each)
    clone_start    clone SOURCE -> TARGET with the chosen parts (runs in the background)
    clone_status   live progress of the running / last clone
    clone_cancel   stop after the current step

Parts: clear target (channels / roles) · roles · categories · channels · emojis & stickers · server info
       · messages (chat + media / media only / chat only).
Permission overwrites are copied for every role, including @everyone. Roles keep their order, channels keep their
order and categories. Community-only channels (announcement, stage, forum, media) are created after Community mode
is switched on; if the target cannot use Community they become text / voice channels instead.

The account needs Manage Channels + Manage Roles (and Manage Server / Expressions for those parts) in the TARGET.
Deleting the target's channels / roles has to be confirmed by the dashboard.
"""
import asyncio
import base64
import io
import re
import time

import discord

try:
    from discord.http import Route
except Exception:  # pragma: no cover
    Route = None

SNOWFLAKE = re.compile(r"^\d{5,25}$")
REASON = "Server cloning"
CATEGORY, TEXT, VOICE, NEWS, STAGE, FORUM, MEDIA = 4, 0, 2, 5, 13, 15, 16
COMMUNITY_ONLY = {NEWS: TEXT, STAGE: VOICE, FORUM: TEXT, MEDIA: TEXT}   # channel type -> stand-in without Community
PHASES = (("delete", "Clear the target"), ("roles", "Roles"), ("categories", "Categories"), ("channels", "Channels"),
          ("community", "Community mode"), ("special", "Announcement / stage / forum channels"), ("emojis", "Emojis & stickers"),
          ("info", "Server info"), ("order", "Channel order"), ("messages", "Messages"))


class Cancelled(Exception):
    pass


def val(x, default=None):
    return getattr(x, "value", x) if x is not None else default


def clip(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


def short_error(exc):
    if isinstance(exc, discord.Forbidden):
        return "missing permission"
    if isinstance(exc, discord.HTTPException):
        return clip(f"{exc.status}: {getattr(exc, 'text', '') or exc}", 110)
    return clip(f"{type(exc).__name__}: {exc}", 110)


def data_uri(data):
    mime = "image/png"
    if data[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif data[:4] == b"GIF8":
        mime = "image/gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def clean_options(raw):
    raw = raw if isinstance(raw, dict) else {}
    flag = lambda k, d=True: bool(raw.get(k, d))  # noqa: E731
    mode = str(raw.get("messages", "none"))
    try:
        limit = max(1, min(300, int(raw.get("message_limit", 100))))
    except (TypeError, ValueError):
        limit = 100
    return {"delete_channels": flag("delete_channels", False), "delete_roles": flag("delete_roles", False), "roles": flag("roles"),
            "categories": flag("categories"), "channels": flag("channels"), "emojis": flag("emojis"), "info": flag("info"),
            "messages": mode if mode in ("all", "media", "chat") else "none", "message_limit": limit}


def missing_permissions(guild, opt):
    """Names of the permissions this account lacks in `guild` for the chosen parts ([] = fine or unknown)."""
    me = getattr(guild, "me", None)
    perms = getattr(me, "guild_permissions", None)
    if perms is None or perms.administrator:
        return []
    need = {}
    if opt["delete_channels"] or opt["categories"] or opt["channels"]:
        need["Manage Channels"] = perms.manage_channels
    if opt["delete_roles"] or opt["roles"]:
        need["Manage Roles"] = perms.manage_roles
    if opt["emojis"]:
        need["Manage Expressions"] = getattr(perms, "manage_expressions", getattr(perms, "manage_emojis_and_stickers", True))
    if opt["info"]:
        need["Manage Server"] = perms.manage_guild
    return [name for name, ok in need.items() if not ok]


class Run:
    def __init__(self, bot, state, source, target, opt):
        self.bot, self.state, self.source, self.target, self.opt = bot, state, source, target, opt
        self.role_map, self.chan_map = {}, {}          # source id -> target Role object / target channel id
        self.target_community = "COMMUNITY" in target.features
        self.deferred = []                              # community-only channels waiting for Community mode

    # ---- plumbing ------------------------------------------------------------------------
    @property
    def stats(self):
        return self.state.stats

    def check(self):
        if self.state.cancelled:
            raise Cancelled()

    async def pause(self, seconds):
        self.check()
        await asyncio.sleep(seconds)

    def note(self, text):
        self.state.notes.append(clip(text, 200))
        del self.state.notes[:-150]

    def fail(self, what, exc):
        self.stats["failed"] += 1
        self.note(f"{what}: {short_error(exc)}")

    def step(self, key, status):
        self.state.steps[key] = status
        if status == "running":
            self.state.phase = key

    async def api(self, method, path, body=None, **fmt):
        kwargs = {"reason": REASON}
        if body is not None:
            kwargs["json"] = body
        return await self.bot.http.request(Route(method, path, **fmt), **kwargs)

    # ---- the run --------------------------------------------------------------------------
    async def go(self):
        o, st = self.opt, self.state
        plan = (("delete", o["delete_channels"] or o["delete_roles"], self.clear), ("roles", o["roles"], self.roles),
                ("categories", o["categories"], self.categories), ("channels", o["channels"], self.channels),
                ("community", o["channels"], self.community), ("special", o["channels"], self.special),
                ("emojis", o["emojis"], self.emojis), ("info", o["info"], self.info),
                ("order", o["channels"] or o["categories"], self.order), ("messages", o["messages"] != "none" and o["channels"], self.messages))
        try:
            for key, enabled, fn in plan:
                if not enabled:
                    continue
                self.check()
                self.step(key, "running")
                try:
                    self.step(key, (await fn()) or "done")
                except Cancelled:
                    raise
                except Exception as exc:
                    self.step(key, "failed")
                    self.fail(dict(PHASES)[key], exc)
        except Cancelled:
            st.error = "Cancelled"
        except Exception as exc:
            st.error = short_error(exc)
        finally:
            st.running, st.finished, st.phase = False, time.time(), ""

    # ---- clear the target ---------------------------------------------------------------
    async def clear(self):
        if self.opt["delete_channels"]:
            for ch in reversed(list(self.target.channels)):
                try:
                    await ch.delete(reason=REASON)
                    self.stats["deleted"] += 1
                except Exception:
                    pass
                await self.pause(0.25)
        if self.opt["delete_roles"]:
            me = self.target.me
            top = me.top_role if me else None
            for role in sorted(self.target.roles, key=lambda r: r.position):
                if role.is_default() or role.managed or (top is not None and role >= top):
                    continue
                try:
                    await role.delete(reason=REASON)
                    self.stats["deleted"] += 1
                except Exception:
                    pass
                await self.pause(0.25)

    # ---- roles ----------------------------------------------------------------------------
    async def roles(self):
        roles = [r for r in self.source.roles if not r.is_default() and not r.managed]
        for role in sorted(roles, key=lambda r: r.position, reverse=True):  # highest first -> same order in the target
            try:
                new = await self.target.create_role(name=role.name[:100], permissions=discord.Permissions(role.permissions.value),
                                                    colour=discord.Colour(role.colour.value), hoist=role.hoist,
                                                    mentionable=role.mentionable, reason=REASON)
                self.role_map[role.id] = new
                self.stats["roles"] += 1
            except Exception as exc:
                self.fail(f"Role {role.name}", exc)
            await self.pause(0.35)
        try:
            await self.target.default_role.edit(permissions=discord.Permissions(self.source.default_role.permissions.value), reason=REASON)
        except Exception as exc:
            self.fail("@everyone permissions", exc)

    # ---- permission overwrites -------------------------------------------------------------
    def overwrites(self, channel):
        out = []
        try:
            items = list(channel.overwrites.items())
        except Exception:
            return out
        for key, ow in items:
            if isinstance(key, discord.Role):
                if key.is_default():
                    tid = self.target.default_role.id
                else:
                    mapped = self.role_map.get(key.id)
                    tid = mapped.id if mapped else None
                kind = 0
            else:
                tid = key.id if self.target.get_member(key.id) else None   # members must be in the target too
                kind = 1
            if tid is None:
                continue
            allow, deny = ow.pair()
            out.append({"id": str(tid), "type": kind, "allow": str(allow.value), "deny": str(deny.value)})
        return out

    # ---- channels ---------------------------------------------------------------------------
    def channel_body(self, ch, parent_id, ctype, with_overwrites=True):
        g = lambda name, default=None: getattr(ch, name, default)  # noqa: E731
        body = {"name": ch.name[:100], "type": ctype}
        if parent_id:
            body["parent_id"] = str(parent_id)
        if with_overwrites:
            body["permission_overwrites"] = self.overwrites(ch)
        if ctype in (TEXT, NEWS, FORUM, MEDIA):
            body.update(nsfw=bool(g("nsfw", False)), rate_limit_per_user=int(g("slowmode_delay", 0) or 0))
            if g("topic"):
                body["topic"] = clip(g("topic"), 1024)
            if g("default_auto_archive_duration"):
                body["default_auto_archive_duration"] = int(g("default_auto_archive_duration"))
            if g("default_thread_slowmode_delay"):
                body["default_thread_rate_limit_per_user"] = int(g("default_thread_slowmode_delay"))
        if ctype in (VOICE, STAGE):
            body["bitrate"] = min(int(g("bitrate", 64000) or 64000), int(getattr(self.target, "bitrate_limit", 96000)))
            if g("user_limit"):
                body["user_limit"] = int(g("user_limit"))
            if val(g("video_quality_mode")):
                body["video_quality_mode"] = int(val(g("video_quality_mode")))
        if ctype in (FORUM, MEDIA):
            tags = []
            for tag in g("available_tags", []) or []:
                item = {"name": clip(tag.name, 20), "moderated": bool(tag.moderated)}
                if getattr(tag, "emoji", None) is not None and tag.emoji.id is None and tag.emoji.name:
                    item["emoji_name"] = tag.emoji.name        # unicode emoji only; custom ones belong to the source server
                tags.append(item)
            if tags:
                body["available_tags"] = tags
            emoji = g("default_reaction_emoji")
            if emoji is not None and getattr(emoji, "id", None) is None and getattr(emoji, "name", None):
                body["default_reaction_emoji"] = {"emoji_id": None, "emoji_name": emoji.name}
            if val(g("default_sort_order")) is not None:
                body["default_sort_order"] = int(val(g("default_sort_order")))
            if ctype == FORUM and val(g("default_layout")):
                body["default_forum_layout"] = int(val(g("default_layout")))
        return body

    async def create_channel(self, ch, parent_id, ctype):
        body = self.channel_body(ch, parent_id, ctype)
        try:
            made = await self.api("POST", "/guilds/{guild_id}/channels", body, guild_id=self.target.id)
        except discord.HTTPException as exc:
            if getattr(exc, "status", 0) != 403 or not body.get("permission_overwrites"):
                raise
            self.note(f"#{ch.name}: created without permissions (the account lacks some of them here)")
            made = await self.api("POST", "/guilds/{guild_id}/channels", self.channel_body(ch, parent_id, ctype, False), guild_id=self.target.id)
        self.chan_map[ch.id] = int(made["id"])
        self.stats["permissions"] += len(body.get("permission_overwrites", []))
        return made

    def groups(self):
        try:
            return self.source.by_category()
        except Exception:
            return [(None, [c for c in self.source.channels if not isinstance(c, discord.CategoryChannel)])]

    async def categories(self):
        for cat, _children in self.groups():
            if cat is None:
                continue
            try:
                await self.create_channel(cat, None, CATEGORY)
                self.stats["categories"] += 1
            except Exception as exc:
                self.fail(f"Category {cat.name}", exc)
            await self.pause(0.35)

    def parent_of(self, ch, cat):
        return self.chan_map.get(cat.id) if cat is not None else None

    async def channels(self):
        for cat, children in self.groups():
            for ch in children:
                ctype = val(ch.type)
                if ctype in COMMUNITY_ONLY and not self.target_community:
                    self.deferred.append((ch, cat))                 # needs Community mode first - see community() / special()
                    continue
                await self.make(ch, cat, ctype)

    async def make(self, ch, cat, ctype):
        try:
            await self.create_channel(ch, self.parent_of(ch, cat), ctype)
            self.stats["channels"] += 1
        except Exception as exc:
            self.fail(f"Channel {ch.name}", exc)
        await self.pause(0.35)

    async def community(self):
        src, tgt = self.source, self.target
        if not self.deferred or self.target_community or "COMMUNITY" not in src.features:
            return "skipped"
        first_text = next((self.chan_map[c.id] for c in src.text_channels if c.id in self.chan_map), None)
        rules = self.chan_map.get(getattr(src.rules_channel, "id", None)) or first_text
        updates = self.chan_map.get(getattr(src.public_updates_channel, "id", None)) or rules
        if not rules:
            self.note("Community mode not enabled: no text channel to use for rules / updates")
            return "failed"
        body = {"features": sorted(set(tgt.features) | {"COMMUNITY"}), "verification_level": max(1, int(val(src.verification_level, 1))),
                "explicit_content_filter": 2, "rules_channel_id": str(rules), "public_updates_channel_id": str(updates),
                "preferred_locale": str(val(getattr(src, "preferred_locale", "en-US"), "en-US"))}
        try:
            await self.api("PATCH", "/guilds/{guild_id}", body, guild_id=tgt.id)
            self.target_community = True
        except Exception as exc:
            self.note(f"Community mode could not be enabled ({short_error(exc)}) - announcement / stage / forum channels become text / voice")
            return "failed"

    async def special(self):
        if not self.deferred:
            return "skipped"
        for ch, cat in self.deferred:
            ctype = val(ch.type)
            if not self.target_community:
                ctype = COMMUNITY_ONLY[ctype]
                self.stats["fallback"] += 1
            await self.make(ch, cat, ctype)

    # ---- emojis & stickers -----------------------------------------------------------------
    async def emojis(self):
        tgt = self.target
        have = {False: sum(1 for e in tgt.emojis if not e.animated), True: sum(1 for e in tgt.emojis if e.animated)}
        for emoji in self.source.emojis:
            if emoji.managed or have[emoji.animated] >= tgt.emoji_limit:
                continue
            try:
                await tgt.create_custom_emoji(name=emoji.name, image=await emoji.read(), reason=REASON)
                have[emoji.animated] += 1
                self.stats["emojis"] += 1
            except Exception as exc:
                self.fail(f"Emoji {emoji.name}", exc)
            await self.pause(1.5)
        room = getattr(tgt, "sticker_limit", 0) - len(getattr(tgt, "stickers", []))
        for sticker in list(getattr(self.source, "stickers", []) or []):
            if room <= 0 or "lottie" in str(val(getattr(sticker, "format", ""))).lower():
                continue
            try:
                file = discord.File(io.BytesIO(await sticker.read()), filename=f"{sticker.name}.png")
                await tgt.create_sticker(name=sticker.name, description=sticker.description or sticker.name,
                                         emoji=getattr(sticker, "emoji", None) or "🙂", file=file, reason=REASON)
                room -= 1
                self.stats["stickers"] += 1
            except Exception as exc:
                self.fail(f"Sticker {sticker.name}", exc)
            await self.pause(1.5)

    # ---- server info ------------------------------------------------------------------------
    async def info(self):
        src, tgt = self.source, self.target
        settings = {"name": src.name[:100], "verification_level": int(val(src.verification_level, 0)),
                    "default_message_notifications": int(val(getattr(src, "default_notifications", 0), 0)),
                    "explicit_content_filter": int(val(getattr(src, "explicit_content_filter", 0), 0)),
                    "afk_timeout": int(getattr(src, "afk_timeout", 300) or 300),
                    "system_channel_flags": int(val(getattr(src, "system_channel_flags", 0), 0))}
        if self.target_community:
            settings["explicit_content_filter"] = max(2, settings["explicit_content_filter"])
            if src.description:
                settings["description"] = clip(src.description, 120)
        for key, chan in (("afk_channel_id", src.afk_channel), ("system_channel_id", src.system_channel)):
            mapped = self.chan_map.get(getattr(chan, "id", None))
            if mapped:
                settings[key] = str(mapped)
        try:
            await self.api("PATCH", "/guilds/{guild_id}", settings, guild_id=tgt.id)
        except Exception as exc:
            self.fail("Server settings", exc)
        images = {}
        for key, asset, feature in (("icon", src.icon, None), ("banner", src.banner, "BANNER"), ("splash", src.splash, "INVITE_SPLASH")):
            if asset is None or (feature and feature not in tgt.features):
                continue
            try:
                if asset.is_animated() and key == "icon" and "ANIMATED_ICON" not in tgt.features:
                    asset = asset.replace(format="png")
                images[key] = data_uri(await asset.read())
            except Exception as exc:
                self.fail(f"Server {key}", exc)
        if images:
            try:
                await self.api("PATCH", "/guilds/{guild_id}", images, guild_id=tgt.id)
            except Exception as exc:
                self.fail("Server images", exc)

    # ---- channel order ---------------------------------------------------------------------
    async def order(self):
        items = []
        for ch in self.source.channels:
            new = self.chan_map.get(ch.id)
            if new is None or isinstance(ch, discord.Thread):
                continue
            cat_id = getattr(ch, "category_id", None)
            items.append({"id": str(new), "position": int(ch.position), "parent_id": str(self.chan_map[cat_id]) if cat_id in self.chan_map else None})
        for i in range(0, len(items), 100):
            await self.api("PATCH", "/guilds/{guild_id}/channels", items[i:i + 100], guild_id=self.target.id)
            await self.pause(0.5)

    # ---- messages ---------------------------------------------------------------------------
    async def messages(self):
        mode, limit, tgt = self.opt["messages"], self.opt["message_limit"], self.target
        size_limit = int(getattr(tgt, "filesize_limit", 25 * 1024 * 1024))
        for ch in self.source.text_channels:
            new_id = self.chan_map.get(ch.id)
            if not new_id:
                continue
            dest = tgt.get_channel(new_id)
            if dest is None:
                try:
                    dest = await self.bot.fetch_channel(new_id)
                except Exception:
                    continue
            if not isinstance(dest, discord.TextChannel):
                continue
            try:
                async for msg in ch.history(limit=limit, oldest_first=True):
                    self.check()
                    if msg.type not in (discord.MessageType.default, discord.MessageType.reply):
                        continue
                    has_files = bool(msg.attachments)
                    if (mode == "media" and not has_files) or (mode == "chat" and (has_files or not msg.content.strip())) or \
                            (mode == "all" and not has_files and not msg.content.strip()):
                        continue
                    try:
                        files = []
                        if mode != "chat":
                            for att in msg.attachments[:10]:
                                if att.size <= size_limit:
                                    files.append(discord.File(io.BytesIO(await att.read()), filename=att.filename, spoiler=att.is_spoiler()))
                        header = f"**[{msg.author.display_name}]** ({msg.created_at.strftime('%Y-%m-%d %H:%M')})"
                        text = header + ("\n" + msg.content if msg.content and mode != "media" else "")
                        await dest.send(content=text[:2000], files=files or None, allowed_mentions=discord.AllowedMentions.none())
                        self.stats["messages"] += 1
                        self.stats["files"] += len(files)
                    except Cancelled:
                        raise
                    except Exception as exc:
                        self.fail(f"Message in #{ch.name}", exc)
                    await self.pause(1.5)
            except Cancelled:
                raise
            except Exception as exc:
                self.fail(f"Reading #{ch.name}", exc)


class State:
    def __init__(self):
        self.reset()

    def reset(self):
        self.running = self.cancelled = False
        self.phase, self.error, self.source, self.target = "", "", "", ""
        self.started = self.finished = 0.0
        self.stats = {k: 0 for k in ("roles", "categories", "channels", "emojis", "stickers", "permissions", "messages", "files", "fallback", "deleted", "failed")}
        self.steps, self.notes = {}, []

    def view(self):
        return {"running": self.running, "phase": self.phase, "error": self.error, "source": self.source, "target": self.target,
                "started": self.started, "finished": self.finished, "stats": self.stats, "notes": self.notes[-40:],
                "steps": [{"key": k, "label": label, "state": self.steps.get(k, "skipped")} for k, label in PHASES],
                "idle": not (self.started or self.running)}


def build(bot, *, log):
    state, tasks = State(), []

    def guild_of(value, what):
        gid = str(value or "")
        guild = bot.get_guild(int(gid)) if SNOWFLAKE.match(gid) else None
        if guild is None:
            raise ValueError(f"The {what} server wasn't found - this account must be in it")
        return guild

    async def act_guilds(_p):
        rows = []
        for g in sorted(bot.guilds, key=lambda x: x.name.lower()):
            perms = getattr(getattr(g, "me", None), "guild_permissions", None)
            can = bool(perms and (perms.administrator or (perms.manage_channels and perms.manage_roles)))
            rows.append({"id": str(g.id), "name": g.name, "icon": g.icon.with_size(64).url if g.icon else None, "members": g.member_count,
                         "owner": g.owner_id == bot.user.id, "can_target": can, "community": "COMMUNITY" in g.features,
                         "roles": max(0, len(g.roles) - 1), "channels": len([c for c in g.channels if not isinstance(c, discord.Thread)]),
                         "emojis": len(g.emojis), "stickers": len(getattr(g, "stickers", []) or [])})
        return {"guilds": rows}

    async def act_start(p):
        if state.running:
            raise ValueError("A clone is already running on this account")
        if Route is None:
            raise ValueError("This discord.py-self version cannot make raw API calls")
        source, target = guild_of(p.get("source_id"), "source"), guild_of(p.get("target_id"), "target")
        if source.id == target.id:
            raise ValueError("Source and target must be different servers")
        opt = clean_options(p.get("options"))
        if (opt["delete_channels"] or opt["delete_roles"]) and not p.get("confirm"):
            raise ValueError("Confirmation required before deleting the target's channels / roles")
        if not any((opt["delete_channels"], opt["delete_roles"], opt["roles"], opt["categories"], opt["channels"], opt["emojis"], opt["info"])):
            raise ValueError("Choose at least one thing to clone")
        missing = missing_permissions(target, opt)
        if missing:
            raise ValueError(f"This account is missing {', '.join(missing)} in \"{target.name}\"")
        state.reset()
        state.running, state.started, state.source, state.target = True, time.time(), source.name, target.name
        for key, _ in PHASES:
            state.steps[key] = "pending"
        log("SYSTEM", f"Server clone started: {source.name} -> {target.name}", "warning")
        tasks[:] = [asyncio.ensure_future(Run(bot, state, source, target, opt).go())]
        return {"title": "Clone started"}

    async def act_status(_p):
        return state.view()

    async def act_cancel(_p):
        if not state.running:
            raise ValueError("Nothing is running")
        state.cancelled = True
        return {"title": "Stopping after the current step"}

    return {"clone_guilds": act_guilds, "clone_start": act_start, "clone_status": act_status, "clone_cancel": act_cancel}
