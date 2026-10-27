import asyncio, json, logging, time, discord
from datetime import datetime

from core import config, db, settings
from core.allocation import timeZone
from core.calendar_links import calendar_url

log = logging.getLogger("logibot")

EVERYONE = discord.AllowedMentions(everyone=True)
NOTIFY = discord.AllowedMentions(roles=True, users=True)
QUIET = discord.AllowedMentions.none()

REQUIRED_PERMS = [
    "view_channel", "manage_channels", "manage_roles", "send_messages", "send_messages_in_threads",
    "create_public_threads", "manage_threads", "embed_links", "mention_everyone", "read_message_history",
]


def is_owner(user) -> bool:
    return user.id in config.OWNER_IDS


def is_admin(member) -> bool:
    return is_owner(member) or member.guild_permissions.manage_guild


def is_manager(member) -> bool:
    if is_admin(member):
        return True
    return any(role.id in settings.get(member.guild.id)["roles"] for role in member.roles)


def name_of(guild, userId, fallback=None):
    member = guild.get_member(userId) if userId else None
    return member.display_name if member else (fallback or f"User {userId}")


def event_by_thread(thread_id):
    return db.q1("SELECT * FROM events WHERE thread_id=?", (thread_id,))


def get_event(event_id):
    return db.q1("SELECT * FROM events WHERE id=?", (event_id,))


def officer_ping(guild_id):
    return " ".join(f"<@&{role}>" for role in sorted(settings.get(guild_id)["roles"])) or "Officership"


def missing_perms(guild) -> list[str]:
    have = guild.me.guild_permissions
    return [perm.replace("_", " ") for perm in REQUIRED_PERMS if not getattr(have, perm)]


async def dm_user(bot, user_id, text):
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(text)
        await asyncio.sleep(0.2)
    except discord.HTTPException:
        pass


async def get_thread(bot, thread_id):
    return bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)


def is_social(eventRow) -> bool:
    return (eventRow["kind"] or "event") == "social"


def markers_of(eventRow) -> list[str]:
    return db.loads(eventRow["markers"])


def going_lines(eventRow, limit=1000) -> str:
    rows = db.q("SELECT user_id, details FROM signups WHERE event_id=? AND role='going' ORDER BY rowid", (eventRow["id"],))
    out, used = [], 0
    for number, row in enumerate(rows):
        extra = ", ".join(f"{key}: {value}" for key, value in (json.loads(row["details"]) if row["details"] else {}).items() if key in markers_of(eventRow))
        line = f"- <@{row['user_id']}>" + (f" - {extra}" if extra else "")
        if used + len(line) + 1 > limit:
            out.append(f"...and {len(rows) - number} more")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out) or "*Nobody yet*"


def conflicts_for(eventRow, user_id) -> list:
    day = datetime.fromtimestamp(eventRow["start_ts"], timeZone).date()
    rows = db.q(
        "SELECT e.* FROM events e JOIN signups s ON s.event_id=e.id "
        "WHERE s.user_id=? AND e.guild_id=? AND e.id!=? AND e.cancelled=0 AND e.start_ts>? ORDER BY e.start_ts",
        (user_id, eventRow["guild_id"], eventRow["id"], int(time.time())),
    )
    return [row for row in rows if datetime.fromtimestamp(row["start_ts"], timeZone).date() == day]


def conflict_text(eventRow, user_id) -> str:
    clash = conflicts_for(eventRow, user_id)
    if not clash:
        return ""
    names = ", ".join(f"**{row['title']}** (<#{row['thread_id']}>, <t:{row['start_ts']}:t>)" for row in clash)
    return f"Heads up: **{eventRow['title']}** is on the same day as {names}, which you're also signed up for. Check you can do both."


def event_embed(eventRow, status=None):
    cancelled, social = bool(eventRow["cancelled"]), is_social(eventRow)
    if social:
        desc = "Press **Going** to let everyone know you're coming."
        if markers_of(eventRow):
            desc += " I'll DM you for: " + ", ".join(markers_of(eventRow)) + "."
    else:
        desc = "Need a lift, or can you offer one? Press a button below and I'll DM you a few questions."
    embed = discord.Embed(
        title=("CANCELLED - " if cancelled else "") + eventRow["title"],
        url=eventRow["fb_link"] or None,
        colour=discord.Colour.dark_grey() if cancelled else (discord.Colour.purple() if social else discord.Colour.dark_green()),
        description=None if cancelled else desc,
    )
    start, close = eventRow["start_ts"], eventRow["close_ts"]
    embed.add_field(name="When", value=f"<t:{start}:F> (<t:{start}:R>)", inline=False)
    embed.add_field(name="Where", value=eventRow["location"], inline=False)
    if eventRow["fb_link"]:
        embed.add_field(name="Facebook event", value=eventRow["fb_link"], inline=False)
    if not social:
        embed.add_field(name="Sign-ups close", value=f"<t:{close}:F> (<t:{close}:R>)", inline=False)
    if not cancelled:
        embed.add_field(name="Calendar", value=f"[Add to Google Calendar]({calendar_url(eventRow)})", inline=False)
    if social and not cancelled:
        embed.add_field(name="Who's going", value=going_lines(eventRow), inline=False)
    elif status and not cancelled:
        embed.add_field(name="So far", value=status, inline=False)
    return embed


def signup_status(eventRow) -> str:
    going = db.q1("SELECT COUNT(*) c FROM signups WHERE event_id=? AND role='going'", (eventRow["id"],))["c"]
    if is_social(eventRow):
        return f"{going} going"
    drivers = db.q("SELECT capacity FROM signups WHERE event_id=? AND role='driver'", (eventRow["id"],))
    passengerCount = db.q1("SELECT COUNT(*) c FROM signups WHERE event_id=? AND role='passenger'", (eventRow["id"],))["c"]
    seats = sum(driver["capacity"] or 0 for driver in drivers)
    return f"{len(drivers)} driver(s), {seats} seat(s) - {passengerCount} passenger(s) - {going} going (no help needed)"


async def refresh_event_post(bot, eventRow):
    eventRow = get_event(eventRow["id"])
    try:
        thread = await get_thread(bot, eventRow["thread_id"])
        message = await thread.fetch_message(thread.id)
        await message.edit(embed=event_embed(eventRow, signup_status(eventRow)))
    except discord.HTTPException:
        log.warning("couldn't refresh event post %s", eventRow["id"])


async def get_category(guild):
    guildConfig = settings.get(guild.id)
    categoryChannel = guild.get_channel(guildConfig["category_id"]) if guildConfig["category_id"] else None
    if not isinstance(categoryChannel, discord.CategoryChannel):
        categoryChannel = discord.utils.get(guild.categories, name=guildConfig["category"]) or await guild.create_category(guildConfig["category"])
        settings.save(guild.id, category=categoryChannel.name, category_id=categoryChannel.id)
    return categoryChannel


GUIDES = {
    "event": (
        "Start here - how event lifts work",
        "Each event gets its own post here.\n\n"
        "- Press **Driver** or **Passenger** on an event and I'll DM you a few questions. Your answers are remembered for next time.\n"
        "- Say whether you need a lift **to** the event, **back**, or **both**. We try to keep you in the same car both ways.\n"
        "- Sign-ups close at the cutoff shown on the post; lifts are then sorted and posted in the thread, and I DM everyone.\n"
        "- **Going, no help** marks you as attending without needing a lift. If a driver is already taking you, ask them to add your name or ID to their sign-up so your seat is counted.\n"
        "- **Edit my details** lets you change your answers any time. After the cutoff, anyone it affects is told. You can't be both a driver and a passenger - **Pull out** first, then sign up again.\n"
        "- **Pull out** if plans change - use it as soon as you know.\n"
        "- Problems or grievances: speak to Officership.",
    ),
    "social": (
        "Start here - how socials work",
        "Each social gets its own post here.\n\n"
        "- Press **Going** to let everyone know you're coming. Some socials ask a couple of extra questions (like time of arrival) - I'll DM you.\n"
        "- Press **Going** again to update your answers, or **Can't make it** to come off the list.",
    ),
}


async def get_forum(guild, kind="event"):
    guildConfig = settings.get(guild.id)
    social = kind == "social"
    channel = guild.get_channel(guildConfig["social_forum_id" if social else "event_forum_id"] or 0)
    if isinstance(channel, discord.ForumChannel):
        return channel
    name = guildConfig["social_forum"] if social else guildConfig["forum"]
    userIds = {"social_forum_id": None, "event_forum_id": None}
    for chan in guild.forums:
        if chan.name == name and chan.category and chan.category.name == guildConfig["category"]:
            userIds["social_forum_id" if social else "event_forum_id"] = chan.id
            settings.save(guild.id, **{key: value for key, value in userIds.items() if value})
            return chan
    forum = await guild.create_forum(
        name, category=await get_category(guild), default_auto_archive_duration=10080,
        topic="Socials - press Going." if social else "Lift sharing for events - press Driver or Passenger.",
    )
    settings.save(guild.id, **{"social_forum_id" if social else "event_forum_id": forum.id})
    title, body = GUIDES[kind]
    try:
        created = await forum.create_thread(name=title, content=body)
        await created.thread.edit(pinned=True)
    except discord.HTTPException:
        log.warning("couldn't create/pin the guide post in %s", name)
    return forum


def _log_overwrites(guild):
    outwardWord = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    for rowId in settings.get(guild.id)["roles"]:
        role = guild.get_role(rowId)
        if role:
            outwardWord[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    outwardWord[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, mention_everyone=True)
    return outwardWord


async def get_log_channel(guild):
    guildConfig = settings.get(guild.id)
    channel = guild.get_channel(guildConfig["log_channel"]) if guildConfig["log_channel"] else None
    if channel:
        return channel
    categoryChannel = await get_category(guild)
    channel = discord.utils.get(categoryChannel.text_channels, name=config.LOG_CHANNEL_NAME) or await guild.create_text_channel(
        config.LOG_CHANNEL_NAME, category=categoryChannel, overwrites=_log_overwrites(guild),
        topic="Private to officers: lift-share alerts (pull-outs, mixed cars, no space).",
    )
    settings.save(guild.id, log_channel=channel.id)
    return channel


async def get_action_channel(guild):
    guildConfig = settings.get(guild.id)
    channel = guild.get_channel(guildConfig["action_channel"]) if guildConfig["action_channel"] else None
    if channel:
        return channel
    categoryChannel = await get_category(guild)
    channel = discord.utils.get(categoryChannel.text_channels, name=config.ACTION_LOG_CHANNEL_NAME) or await guild.create_text_channel(
        config.ACTION_LOG_CHANNEL_NAME, category=categoryChannel, overwrites=_log_overwrites(guild),
        topic="Private to officers: a quiet record of what has been done (no pings).",
    )
    settings.save(guild.id, action_channel=channel.id)
    return channel


async def sync_log_permissions(guild):
    guildConfig = settings.get(guild.id)
    for key in ("log_channel", "action_channel"):
        channel = guild.get_channel(guildConfig[key]) if guildConfig[key] else None
        if channel:
            await channel.edit(overwrites=_log_overwrites(guild))


async def log_action(bot, guild_id, text):
    try:
        db.remember(guild_id, text)
    except Exception:
        log.exception("couldn't remember action")
    try:
        channel_id = settings.get(guild_id)["action_channel"]
        if not channel_id:
            return
        guild = bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild else None
        if channel is None:
            return
        await channel.send(f"<t:{int(time.time())}:T> {text}"[:1900], allowed_mentions=QUIET)
    except Exception:
        log.exception("couldn't write to the action log")


async def notify_log(bot, guild, text, ping=False):
    try:
        message = (officer_ping(guild.id) + " " if ping else "") + text
        await (await get_log_channel(guild)).send(message, allowed_mentions=NOTIFY)
    except discord.HTTPException:
        log.exception("officer log channel unavailable")


async def notify_officers(bot, eventRow, text, ping=True):
    guild = bot.get_guild(eventRow["guild_id"])
    message = f"{officer_ping(eventRow['guild_id']) + ' ' if ping else ''}**{eventRow['title']}** (<#{eventRow['thread_id']}>)\n{text}"
    try:
        await (await get_log_channel(guild)).send(message, allowed_mentions=NOTIFY)
    except discord.HTTPException:
        log.exception("officer log channel unavailable")
        if ping:
            await (await get_thread(bot, eventRow["thread_id"])).send(message, allowed_mentions=NOTIFY)


FRIENDLY_ERROR = "Something went wrong on my side - please try again, and tell Officership if it keeps happening."


async def _apologise(interaction, error, what):
    log.error("%s failed", what, exc_info=error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(FRIENDLY_ERROR, ephemeral=True)
        else:
            await interaction.response.send_message(FRIENDLY_ERROR, ephemeral=True)
    except discord.HTTPException:
        pass
    if interaction.guild_id:
        await log_action(interaction.client, interaction.guild_id, f"{what} failed for {interaction.user.display_name}: {type(error).__name__}: {str(error)[:200]}")


async def view_error(self, interaction, error, item):
    await _apologise(interaction, error, f"A button ({getattr(item, 'label', None) or type(item).__name__})")


async def modal_error(self, interaction, error):
    await _apologise(interaction, error, "A form")


async def command_error(interaction, error):
    original = getattr(error, "original", error)
    name = interaction.command.qualified_name if interaction.command else "a command"
    await _apologise(interaction, original, f"/{name}")


discord.ui.View.on_error = view_error
discord.ui.Modal.on_error = modal_error
