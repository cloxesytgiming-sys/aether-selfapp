"""
Channel moderation: lock, unlock, channelcreate, channelrename.
Chat:       !lock [#channel] [reason]     !unlock [#channel] [reason]
            !channelcreate [text|voice|stage|category|forum] <name>      (aliases: chcreate, mkchannel)
            !channelrename [#channel] <new name>                         (alias: chrename)
Dashboard:  Commands -> Moderation -> "Channel tools" (actions chan_list / chan_lock / chan_unlock / chan_create / chan_rename)

The account needs Manage Channels (and Manage Roles to change @everyone's permissions) in that server.
Lock = @everyone can no longer send messages (text) / connect (voice, stage); unlock removes that override again.
"""
import re

import discord

KINDS = ("text", "voice", "stage", "category", "forum")
SNOWFLAKE = re.compile(r"^\d{5,25}$")
LOCK_FLAGS_TEXT = ("send_messages", "send_messages_in_threads", "create_public_threads", "create_private_threads")


def kind_of(channel):
    if isinstance(channel, discord.CategoryChannel):
        return "category"
    if isinstance(channel, discord.StageChannel):
        return "stage"
    if isinstance(channel, discord.VoiceChannel):
        return "voice"
    if isinstance(channel, getattr(discord, "ForumChannel", ())):
        return "forum"
    return "text"


def _set(overwrite, name, value):
    try:
        setattr(overwrite, name, value)
    except AttributeError:  # flag not known to this library version
        pass


def is_locked(channel):
    ow = channel.overwrites_for(channel.guild.default_role)
    return ow.send_messages is False or ow.connect is False


def clean_name(name):
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    if not 1 <= len(name) <= 100:
        raise ValueError("The channel name must be 1-100 characters")
    return name


def explain(exc):
    if isinstance(exc, discord.Forbidden):
        return "Discord says this account lacks the permission (it needs Manage Channels / Manage Roles there)."
    if isinstance(exc, discord.HTTPException):
        return f"Discord refused: {str(getattr(exc, 'text', '') or exc)[:160]}"
    return str(exc)


async def set_lock(channel, locked, reason=""):
    role = channel.guild.default_role
    ow = channel.overwrites_for(role)
    value = False if locked else None
    kind = kind_of(channel)
    if kind in ("voice", "stage"):
        _set(ow, "connect", value)
    elif kind == "category":
        for flag in (*LOCK_FLAGS_TEXT, "connect"):
            _set(ow, flag, value)
    else:
        for flag in LOCK_FLAGS_TEXT:
            _set(ow, flag, value)
    why = ("Locked" if locked else "Unlocked") + " from AetherSelf" + (f": {reason}" if reason else "")
    await channel.set_permissions(role, overwrite=None if ow.is_empty() else ow, reason=why[:480])


async def create_channel(guild, name, kind="text", category_id=None, reason=""):
    name = clean_name(name)
    if kind not in KINDS:
        raise ValueError("Type must be text, voice, stage, category or forum")
    parent = None
    if category_id:
        parent = guild.get_channel(int(category_id))
        if not isinstance(parent, discord.CategoryChannel):
            raise ValueError("That category doesn't exist")
    why = ("Created from AetherSelf" + (f": {reason}" if reason else ""))[:480]
    if kind == "category":
        return await guild.create_category(name, reason=why)
    if kind == "voice":
        return await guild.create_voice_channel(name, category=parent, reason=why)
    if kind == "stage":
        return await guild.create_stage_channel(name, category=parent, reason=why)
    if kind == "forum":
        return await guild.create_forum(name, category=parent, reason=why)
    return await guild.create_text_channel(name, category=parent, reason=why)


async def rename_channel(channel, name, reason=""):
    name = clean_name(name)
    old = channel.name
    await channel.edit(name=name, reason=("Renamed from AetherSelf" + (f": {reason}" if reason else ""))[:480])
    return old


def setup(bot, *, say, log, get_prefix, mark):
    """Registers the chat commands; returns {action: handler} for the dashboard worker."""
    taken = set()

    def add(callback, name, aliases=()):
        if bot.get_command(name):
            return
        ok = [a for a in aliases if not bot.get_command(a) and a not in taken]
        bot.command(name=name, aliases=ok)(callback)
        taken.update([name, *ok])

    # ---- help integration -------------------------------------------------------------
    try:
        import extras
        if not getattr(extras.help_catalog, "_chanmod", False):
            original = extras.help_catalog

            def help_catalog(p):
                cat = original(p)
                if "moderation" in cat:
                    title, blurb, cmds, *rest = cat["moderation"]
                    cat["moderation"] = (title, blurb, [*cmds,
                        (f"{p}lock [#channel] [reason]", "stop @everyone from sending / joining"), (f"{p}unlock [#channel]", "undo a lock"),
                        (f"{p}channelcreate [type] <name>", "create a text / voice / stage / category / forum channel"),
                        (f"{p}channelrename [#channel] <name>", "rename a channel (defaults to this one)")], *rest)
                return cat
            help_catalog._chanmod = True
            extras.help_catalog = help_catalog
    except Exception as exc:
        print("chanmod help hook skipped:", repr(exc))

    # ---- chat commands ------------------------------------------------------------------
    def split_channel(ctx, args):
        """'#chan some words' -> (channel, 'some words'); no channel given -> the current one."""
        parts = (args or "").strip().split(None, 1)
        if parts:
            m = re.match(r"^<#(\d+)>$", parts[0]) or re.match(r"^(\d{5,25})$", parts[0])
            if m:
                chan = ctx.guild.get_channel(int(m.group(1)))
                if chan is None:
                    raise ValueError("I can't find that channel in this server")
                return chan, (parts[1] if len(parts) > 1 else "")
        return ctx.channel, (args or "").strip()

    async def guarded(ctx, work):
        if ctx.guild is None:
            return await say(ctx, "This command only works inside a server.", delete_after=6)
        try:
            await work()
        except ValueError as exc:
            await say(ctx, f"❌ {exc}", delete_after=8)
        except discord.HTTPException as exc:
            await say(ctx, f"❌ {explain(exc)}", delete_after=10)

    async def lock(ctx, *, args: str = ""):
        async def work():
            chan, reason = split_channel(ctx, args)
            await set_lock(chan, True, reason)
            log("SYSTEM", f"Locked #{chan.name}", guild_name=ctx.guild.name, guild_id=str(ctx.guild.id))
            await say(ctx, f"🔒 Locked **#{chan.name}** for @everyone.", delete_after=8)
        await guarded(ctx, work)

    async def unlock(ctx, *, args: str = ""):
        async def work():
            chan, reason = split_channel(ctx, args)
            await set_lock(chan, False, reason)
            log("SYSTEM", f"Unlocked #{chan.name}", guild_name=ctx.guild.name, guild_id=str(ctx.guild.id))
            await say(ctx, f"🔓 Unlocked **#{chan.name}**.", delete_after=8)
        await guarded(ctx, work)

    async def channelcreate(ctx, *, args: str = ""):
        async def work():
            parts = args.strip().split(None, 1)
            kind, name = "text", args
            if parts and parts[0].lower() in KINDS:
                kind, name = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
            if not name.strip():
                raise ValueError(f"Usage: {get_prefix()}channelcreate [{'|'.join(KINDS)}] <name>")
            made = await create_channel(ctx.guild, name, kind, getattr(getattr(ctx.channel, "category", None), "id", None) if kind not in ("category",) else None)
            log("SYSTEM", f"Created {kind} channel #{made.name}", guild_name=ctx.guild.name, guild_id=str(ctx.guild.id))
            await say(ctx, f"✅ Created {kind} channel **{made.name}**" + (f" ({made.mention})" if kind in ("text", "forum") else "") + ".", delete_after=10)
        await guarded(ctx, work)

    async def channelrename(ctx, *, args: str = ""):
        async def work():
            chan, name = split_channel(ctx, args)
            if not name.strip():
                raise ValueError(f"Usage: {get_prefix()}channelrename [#channel] <new name>")
            old = await rename_channel(chan, name)
            log("SYSTEM", f"Renamed #{old} to {name.strip()[:60]}", guild_name=ctx.guild.name, guild_id=str(ctx.guild.id))
            await say(ctx, f"✅ Renamed **{old}** → **{name.strip()[:100]}**.", delete_after=10)
        await guarded(ctx, work)

    add(lock, "lock", ["lockdown"])
    add(unlock, "unlock")
    add(channelcreate, "channelcreate", ["chcreate", "mkchannel"])
    add(channelrename, "channelrename", ["chrename"])

    # ---- dashboard actions ---------------------------------------------------------------
    def guild_of(p):
        gid = str(p.get("guild_id", ""))
        guild = bot.get_guild(int(gid)) if SNOWFLAKE.match(gid) else None
        if guild is None:
            raise ValueError("Server not found")
        return guild

    def channel_of(p, guild):
        cid = str(p.get("channel_id", ""))
        chan = guild.get_channel(int(cid)) if SNOWFLAKE.match(cid) else None
        if chan is None:
            raise ValueError("Pick a channel")
        return chan

    async def guarded_act(coro):
        try:
            return await coro
        except discord.HTTPException as exc:
            raise ValueError(explain(exc))

    async def act_list(p):
        guild = guild_of(p)
        rows = []
        for c in sorted(guild.channels, key=lambda c: (getattr(c, "position", 0))):
            if isinstance(c, discord.Thread):
                continue
            cat = getattr(c, "category", None)
            rows.append({"id": str(c.id), "name": c.name, "kind": kind_of(c), "locked": is_locked(c),
                         "category": cat.name if cat else ""})
        return {"channels": rows[:500]}

    async def act_lock(p):
        guild = guild_of(p)
        chan = channel_of(p, guild)
        await guarded_act(set_lock(chan, True, str(p.get("reason") or "")[:200]))
        log("SYSTEM", f"Locked #{chan.name} from the dashboard", guild_name=guild.name, guild_id=str(guild.id))
        return {"title": "Locked", "fields": [["Channel", chan.name], ["Server", guild.name]]}

    async def act_unlock(p):
        guild = guild_of(p)
        chan = channel_of(p, guild)
        await guarded_act(set_lock(chan, False, str(p.get("reason") or "")[:200]))
        log("SYSTEM", f"Unlocked #{chan.name} from the dashboard", guild_name=guild.name, guild_id=str(guild.id))
        return {"title": "Unlocked", "fields": [["Channel", chan.name], ["Server", guild.name]]}

    async def act_create(p):
        guild = guild_of(p)
        made = await guarded_act(create_channel(guild, p.get("name"), str(p.get("kind") or "text"), str(p.get("category_id") or "") or None,
                                                str(p.get("reason") or "")[:200]))
        log("SYSTEM", f"Created #{made.name} from the dashboard", guild_name=guild.name, guild_id=str(guild.id))
        return {"title": "Channel created", "fields": [["Name", made.name], ["ID", str(made.id)], ["Type", kind_of(made)]]}

    async def act_rename(p):
        guild = guild_of(p)
        chan = channel_of(p, guild)
        new = clean_name(p.get("name"))
        old = await guarded_act(rename_channel(chan, new, str(p.get("reason") or "")[:200]))
        log("SYSTEM", f"Renamed #{old} from the dashboard", guild_name=guild.name, guild_id=str(guild.id))
        return {"title": "Channel renamed", "fields": [["Before", old], ["After", new]]}

    return {"chan_list": act_list, "chan_lock": act_lock, "chan_unlock": act_unlock, "chan_create": act_create, "chan_rename": act_rename}
