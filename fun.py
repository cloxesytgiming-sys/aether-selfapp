"""
Fun commands for AetherSelf (chat commands + the dashboard "Fun" tab).

    how<meter> [user]     howgay howsimp howcute howsmart howrich howlucky howcool howsus ...
    ship [user] [user]    compatibility + ship name
    hug/kiss/slap/pat/... action GIFs (nekos.best, waifu.pics fallback - no API key needed)
    fkick [user]          the *fun* kick (aliases: boot, punt) - `kick` stays the real moderation command
    8ball / flip / roll / rate / roast / compliment / choose / rps
    fun                   list everything

Meters and ships are stable for the same user on the same day (so spamming doesn't re-roll them).
Wired up from selfbot.py:   ACTIONS.update(fun.setup(bot, ...))
"""
import hashlib
import io
import random
import re
from datetime import date

import aiohttp
import discord

NEKOS = "https://nekos.best/api/v2/{}"
WAIFU = "https://api.waifu.pics/sfw/{}"

# name: (needs a target, sentence, emoji, waifu.pics fallback endpoint or None)
ACTIONS = {
    "hug": (True, "{a} hugs {b}", "", "hug"),
    "kiss": (True, "{a} kisses {b}", "", "kiss"),
    "kick": (True, "{a} kicks {b}", "", "kick"),
    "slap": (True, "{a} slaps {b}", "", "slap"),
    "pat": (True, "{a} pats {b}", "", "pat"),
    "cuddle": (True, "{a} cuddles {b}", "", "cuddle"),
    "poke": (True, "{a} pokes {b}", "", "poke"),
    "bite": (True, "{a} bites {b}", "", "bite"),
    "punch": (True, "{a} punches {b}", "", None),
    "tickle": (True, "{a} tickles {b}", "", None),
    "highfive": (True, "{a} high-fives {b}", "", "highfive"),
    "handhold": (True, "{a} holds hands with {b}", "", "handhold"),
    "feed": (True, "{a} feeds {b}", "", None),
    "yeet": (True, "{a} yeets {b}", "", "yeet"),
    "wave": (False, "{a} waves {b}", "", "wave"),
    "dance": (False, "{a} dances {b}", "", "dance"),
    "cry": (False, "{a} cries {b}", "", "cry"),
    "laugh": (False, "{a} laughs {b}", "", None),
    "blush": (False, "{a} blushes {b}", "", "blush"), 
    "wink": (False, "{a} winks {b}", "", "wink"),
    "smug": (False, "{a} looks smug {b}", "", "smug"),
    "pout": (False, "{a} pouts {b}", "", None),
    "stare": (False, "{a} stares {b}", "", None),
    "nom": (False, "{a} noms {b}", "", "nom"),
}
CHAT_NAME = {"kick": "fkick"}                      # `kick` is the real moderation command
CHAT_ALIASES = {"kick": ["boot", "punt"], "highfive": ["hi5"], "handhold": ["holdhands"]}

# key: (label, emoji, four tier lines for 0-24 / 25-49 / 50-74 / 75-100)
METERS = {
    "gay": ("gay", "🏳️‍🌈", ("Straight as a ruler 📏", "A little rainbow energy 🌈", "Definitely sparkling ✨", "Full pride parade! 🎉")),
    "simp": ("simp", "🥺", ("No simping, respect 😎", "Simps on special occasions 👀", "Certified simp 💘", "Simp lord, buys everyone's skins 👑")),
    "cute": ("cute", "🥰", ("Cute in a mysterious way", "Cute when smiling", "Seriously adorable 💕", "Too cute to be legal 😭")),
    "smart": ("smart", "🧠", ("Runs on vibes only", "Has moments of brilliance", "Big brain energy", "Galaxy brain 🌌")),
    "rich": ("rich", "💰", ("Broke but happy", "Comfortable", "Fancy pants 🎩", "Billionaire mindset 🤑")),
    "lucky": ("lucky", "🍀", ("Cursed 🌧️", "Average luck", "Lucky streak 🎰", "Four-leaf clover on legs 🍀")),
    "cool": ("cool", "😎", ("Try-hard detected", "Mildly cool", "Pretty cool ngl", "Ice cold legend 🧊")),
    "sus": ("sus", "📮", ("Totally innocent", "Slightly sus 🤨", "Very sus 👀", "EJECT THEM 🚨")),
    "dumb": ("dumb", "🤪", ("Genius mode", "Occasional brain lag", "Loading… please wait", "Brain.exe stopped working 💀")),
    "toxic": ("toxic", "☣️", ("Pure sunshine ☀️", "Slightly salty 🧂", "Quite toxic", "Walking biohazard ☢️")),
    "based": ("based", "🗿", ("Cringe", "Mid", "Based", "Ultra based 🗿")),
    "savage": ("savage", "🔥", ("Harmless kitten 🐱", "Has a bite", "Savage", "Absolutely ruthless 🔥")),
    "evil": ("evil", "😈", ("Angel 😇", "Mischievous", "Villain arc loading", "Final boss 👿")),
    "nerd": ("nerd", "🤓", ("Touches grass daily", "Knows some trivia", "Nerd alert 🤓", "Actually… 🤓☝️")),
    "lazy": ("lazy", "😴", ("Hyperactive", "Moves when needed", "Professional napper", "Horizontal lifestyle 🛌")),
    "drunk": ("drunk", "🍺", ("Sober as a judge", "Tipsy", "Properly drunk", "Seeing double 🥴")),
}
METER_ALIASES = {"gay": ["gaymeter"], "simp": ["simpmeter"]}

BALL = ["It is certain.", "Without a doubt.", "Yes, definitely.", "Most likely.", "Signs point to yes.", "Reply hazy, try again.",
        "Ask again later.", "Better not tell you now.", "Don't count on it.", "My sources say no.", "Very doubtful.", "Absolutely not."]
ROASTS = ["{n} has the energy of a phone at 1% battery.", "{n}'s WiFi password is probably 'password'.",
          "{n} brings a spoon to a knife fight and still loses.", "If laziness were an Olympic sport, {n} would send a representative.",
          "{n} is the human version of the 'Skip Intro' button.", "{n} has main-character syndrome with NPC dialogue.",
          "{n} gets lost in a one-room apartment.", "{n} types 'brb' and disappears for three business days."]
COMPLIMENTS = ["{n} is the reason the group chat stays alive. 💖", "{n} has main character energy. ✨", "{n} makes everything a little better. 🌟",
               "{n} is built different, in a good way. 💪", "{n} could make a Monday feel like Friday. 🎉",
               "Everyone's lucky to know {n}. 🍀", "{n} has elite vibes. 😎", "{n} is a certified legend. 👑"]
RPS = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


# ------------------------------------------------------------------ pure helpers
def _num(*parts):
    seed = ":".join(str(p) for p in parts) + ":" + date.today().isoformat()
    return int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) % 101


def bar(n, width=10):
    filled = round(max(0, min(100, n)) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def meter_text(key, user):
    label, emoji, tiers = METERS[key]
    n = _num(user.id, key)
    tier = tiers[min(n // 25, 3)]
    return f"{emoji} **{user.display_name}** is **{n}%** {label}\n`{bar(n)}` {n}%\n-# {tier}"


def ship_text(a, b):
    ids = sorted([a.id, b.id])
    n = _num(ids[0], ids[1], "ship")
    na, nb = a.display_name, b.display_name
    name = (na[: max(len(na) // 2, 1)] + nb[len(nb) // 2:]) or na
    tier = ("💔 Not meant to be", "😬 Better as friends", "🙂 There's potential", "💕 Great match", "💞 Soulmates")[min(n // 21, 4)]
    return f"💘 **{na}** × **{nb}**\nShip name: **{name}**\n`{bar(n)}` **{n}%**\n-# {tier}"


def action_text(name, a, b=None):
    _, sentence, emoji, _ = ACTIONS[name]
    if b is None:
        return f"{emoji} " + sentence.format(a=f"**{a.display_name}**", b="").strip()
    return f"{emoji} " + sentence.format(a=f"**{a.display_name}**", b=f"**{b.display_name}**")


async def get_gif(name):
    """First gif from nekos.best, falling back to waifu.pics. None if both fail."""
    fallback = ACTIONS.get(name, (0, 0, 0, None))[3]
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            try:
                async with s.get(NEKOS.format(name)) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        url = ((data.get("results") or [{}])[0]).get("url")
                        if url:
                            return url
            except Exception:
                pass
            if fallback:
                async with s.get(WAIFU.format(fallback)) as r:
                    if r.status == 200:
                        return (await r.json(content_type=None)).get("url")
    except Exception:
        pass
    return None


MAX_GIF_BYTES = 8 * 1024 * 1024


async def download(url):
    """Bytes of an image link, or None (too big / unreachable)."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(url) as r:
                if r.status == 200:
                    data = await r.read()
                    return data if 0 < len(data) <= MAX_GIF_BYTES else None
    except Exception:
        pass
    return None


async def gif_file(name):
    """-> (discord.File, link). The GIF is uploaded as a real attachment: nekos.best links don't embed in Discord.
    If the download fails the file is None and the link (if any) is sent as plain text instead."""
    url = await get_gif(name)
    if not url:
        return None, None
    data = await download(url)
    if not data:
        return None, url
    ext = re.search(r"\.(gif|png|jpe?g|webp)(?:\?|$)", url, re.I)
    return discord.File(io.BytesIO(data), filename=f"{name}.{ext.group(1).lower() if ext else 'gif'}"), url


def roll_text(spec):
    m = re.fullmatch(r"(\d{1,2})?d(\d{1,4})", (spec or "d6").lower().strip())
    if not m:
        return None
    count, sides = int(m.group(1) or 1), int(m.group(2))
    if count < 1 or sides < 2:
        return None
    rolls = [random.randint(1, sides) for _ in range(count)]
    return f"🎲 **{count}d{sides}** → {', '.join(map(str, rolls))}" + (f" (total **{sum(rolls)}**)" if count > 1 else "")


# ------------------------------------------------------------------ setup
def setup(bot, *, say, log, get_prefix, get_channel_any, mark):
    """Registers the chat commands and returns {action_name: handler} for the dashboard worker."""

    taken = set()

    def add(callback, name, aliases=()):
        if bot.get_command(name):
            return
        ok = [a for a in aliases if not bot.get_command(a) and a not in taken]
        bot.command(name=name, aliases=ok)(callback)
        taken.update([name, *ok])

    # ---- help integration (!help fun) ------------------------------------------------
    try:
        import extras
        if not getattr(extras.help_catalog, "_fun", False):
            original = extras.help_catalog

            def help_catalog(p):
                cat = original(p)
                cat["fun"] = ("Fun", "Meters, ships, hugs, kisses and games", [
                    (f"{p}howgay [user]", "also howsimp, howcute, howsmart, howrich, howlucky, howcool, howsus …"),
                    (f"{p}ship [user] [user]", "love compatibility + ship name"),
                    (f"{p}hug / kiss / slap / pat [user]", "anime action GIFs (cuddle, poke, bite, punch, tickle …)"),
                    (f"{p}fkick [user]", "the fun kick GIF (`kick` stays the moderation command)"),
                    (f"{p}8ball · flip · roll · rate · roast · compliment · choose · rps", "mini games"),
                    (f"{p}fun", "full list")])
                return cat
            help_catalog._fun = True
            extras.help_catalog = help_catalog
    except Exception as exc:  # the help page just won't list Fun
        print("fun help hook skipped:", repr(exc))

    # ---- user helper -----------------------------------------------------------------
    def me():
        return bot.user

    # ---- meters -----------------------------------------------------------------------
    def make_meter(key):
        async def cmd(ctx, target: discord.User = None):
            await say(ctx, meter_text(key, target or ctx.author))
        return cmd

    for key in METERS:
        add(make_meter(key), f"how{key}", [f"{key}meter"] if key in ("gay", "simp") else [])

    async def how(ctx, meter: str = "", target: discord.User = None):
        key = meter.lower()
        if key not in METERS:
            return await say(ctx, "Pick one: " + ", ".join(f"`{k}`" for k in METERS), delete_after=12)
        await say(ctx, meter_text(key, target or ctx.author))
    add(how, "how")

    # ---- ship ---------------------------------------------------------------------------
    async def ship(ctx, first: discord.User = None, second: discord.User = None):
        if first is None:
            return await say(ctx, f"Usage: `{get_prefix()}ship @user [@user]`", delete_after=8)
        await say(ctx, ship_text(first, second) if second else ship_text(ctx.author, first))
    add(ship, "ship")

    # ---- gif actions --------------------------------------------------------------------
    def make_action(name):
        needs_target = ACTIONS[name][0]

        async def cmd(ctx, target: discord.User = None):
            if needs_target and target is None:
                return await say(ctx, f"Mention someone to {name}: `{get_prefix()}{CHAT_NAME.get(name, name)} @user`", delete_after=8)
            text = action_text(name, ctx.author, target if (needs_target or target) else None)
            file, gif = await gif_file(name)
            if file:
                await say(ctx, text, file=file)
            else:
                await say(ctx, text + (f"\n{gif}" if gif else ""))
        return cmd

    for name in ACTIONS:
        add(make_action(name), CHAT_NAME.get(name, name), CHAT_ALIASES.get(name, []))

    # ---- mini games ---------------------------------------------------------------------
    async def eightball(ctx, *, question: str = ""):
        if not question.strip():
            return await say(ctx, "Ask a question first 🎱", delete_after=6)
        await say(ctx, f"🎱 **{question.strip()[:200]}**\n{random.choice(BALL)}")

    async def flip(ctx):
        await say(ctx, f"🪙 {random.choice(['Heads', 'Tails'])}!")

    async def roll(ctx, spec: str = "d6"):
        text = roll_text(spec)
        await say(ctx, text or "Use `2d6`, `d20` …", delete_after=None if text else 6)

    async def rate(ctx, *, thing: str = ""):
        thing = thing.strip()[:100] or ctx.author.display_name
        await say(ctx, f" I rate **{thing}** a **{_num(thing.lower(), 'rate') // 10}/10**")

    async def roast(ctx, target: discord.User = None):
        await say(ctx, "" + random.choice(ROASTS).format(n=(target or ctx.author).display_name))

    async def compliment(ctx, target: discord.User = None):
        await say(ctx, random.choice(COMPLIMENTS).format(n=(target or ctx.author).display_name))

    async def choose(ctx, *, options: str = ""):
        parts = [o.strip() for o in re.split(r"\||,", options) if o.strip()]
        if len(parts) < 2:
            return await say(ctx, f"Give me options: `{get_prefix()}choose pizza | burger | sushi`", delete_after=8)
        await say(ctx, f"I choose **{random.choice(parts)[:100]}**")

    async def rps(ctx, pick: str = ""):
        pick = pick.lower()
        if pick not in RPS:
            return await say(ctx, "Choose `rock`, `paper` or `scissors`.", delete_after=6)
        bot_pick = random.choice(list(RPS))
        outcome = "It's a tie!" if pick == bot_pick else ("You win! 🎉" if RPS_BEATS[pick] == bot_pick else "I win!")
        await say(ctx, f"{RPS[pick]} vs {RPS[bot_pick]} — {outcome}")

    add(eightball, "8ball", ["eightball"])
    add(flip, "flip", ["coinflip", "coin"])
    add(roll, "roll", ["dice"])
    add(rate, "rate")
    add(roast, "roast")
    add(compliment, "compliment")
    add(choose, "choose", ["pick"])
    add(rps, "rps")

    async def fun(ctx):
        p = get_prefix()
        lines = ["## ◈ __**Fun commands**__",
                 "- **Meters:** " + ", ".join(f"`{p}how{k}`" for k in METERS),
                 f"- **Ship:** `{p}ship @user [@user]`",
                 "- **GIF actions:** " + ", ".join(f"`{p}{CHAT_NAME.get(k, k)}`" for k, v in ACTIONS.items() if v[0]),
                 "- **Solo GIFs:** " + ", ".join(f"`{p}{k}`" for k, v in ACTIONS.items() if not v[0]),
                 f"- **Games:** `{p}8ball` `{p}flip` `{p}roll 2d6` `{p}rate` `{p}roast` `{p}compliment` `{p}choose a | b` `{p}rps`",
                 "-# Meters & ships stay the same for a user for the whole day."]
        await say(ctx, "\n".join(lines)[:1990])
    add(fun, "fun")

    # ---- dashboard action ---------------------------------------------------------------
    async def resolve(user_id):
        uid = str(user_id or "").strip()
        if not uid:
            return None
        if not uid.isdigit():
            raise ValueError("User IDs are numbers only")
        user = bot.get_user(int(uid))
        if user is None:
            try:
                user = await bot.fetch_user(int(uid))
            except discord.HTTPException:
                raise ValueError("Discord doesn't know that user ID")
        return user

    async def act_fun(p):
        kind, name = str(p.get("kind", "")), str(p.get("name", "")).lower()
        a = await resolve(p.get("user_id")) or me()
        gif, title, file = None, "", None
        if kind == "meter":
            if name not in METERS:
                raise ValueError("Unknown meter")
            text, title = meter_text(name, a), f"How {name}?"
        elif kind == "ship":
            b = await resolve(p.get("user2_id"))
            if b is None:
                raise ValueError("Enter two user IDs to ship (the first can be left empty for yourself)")
            text, title = ship_text(a, b), "Ship"
        elif kind == "action":
            if name not in ACTIONS:
                raise ValueError("Unknown action")
            b = await resolve(p.get("user2_id"))
            if ACTIONS[name][0] and b is None:
                raise ValueError(f"'{name}' needs a second user ID (the person receiving it)")
            text, title = action_text(name, a, b), name.capitalize()
            file, gif = await gif_file(name)
        elif kind == "misc":
            if name == "8ball":
                text = f"🎱 {random.choice(BALL)}"
            elif name == "flip":
                text = f"🪙 {random.choice(['Heads', 'Tails'])}!"
            elif name == "roll":
                text = roll_text(p.get("arg") or "d6") or "Use something like 2d6 or d20"
            elif name == "roast":
                text = "🔥 " + random.choice(ROASTS).format(n=a.display_name)
            elif name == "compliment":
                text = random.choice(COMPLIMENTS).format(n=a.display_name)
            else:
                raise ValueError("Unknown game")
            title = name.capitalize()
        else:
            raise ValueError("Unknown fun type")
        send_text = text + (f"\n{gif}" if gif and not file else "")
        channel_id = str(p.get("channel_id") or "").strip()
        if channel_id:
            channel = await get_channel_any(channel_id)
            await channel.send(send_text[:1990] + mark, **({"file": file} if file else {}))
            log("SYSTEM", f"Fun command '{kind}:{name}' sent from the dashboard")
        out = {"title": title, "text": re.sub(r"[*`]|^-# ", "", text, flags=re.M)}
        if gif:
            out["image"] = gif
        if channel_id:
            out["fields"] = [["Sent", "Posted in the selected channel"]]
        return out

    return {"fun": act_fun}
