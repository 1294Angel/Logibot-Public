import calendar, io, json, os, discord
from datetime import date, datetime, timedelta
from time import time as unix_now

from core import config, db, settings
from core.allocation import timeZone, parse_local, signup_close
from core.ui import (
    SKIP, Cancelled, ask, ask_cutoff, check_link_optional, choose, confirm, link_or_none, MARKER_PROMPT,
    parse_markers, pick_date, pick_interval, pick_time,
)
from core.util import (
    QUIET, dm_user, event_embed, get_category, get_event, get_thread, is_manager, log, log_action, name_of, notify_log, signup_status,
)

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_TEMPLATE_MESSAGES = 15
STYLE_NAMES = {"social": "going + extra questions", "event": "lift sign-ups"}


def get_preset(preset_id):
    return db.q1("SELECT * FROM presets WHERE id=?", (preset_id,))


def ready_presets(guild_id):
    return db.q("SELECT * FROM presets WHERE guild_id=? AND status='ready' ORDER BY name COLLATE NOCASE", (guild_id,))


def search_presets(guild_id, text):
    text = (text or "").strip().lower()
    rows = ready_presets(guild_id)
    return rows if text in ("", "*") else [row for row in rows if text in row["name"].lower()]


async def preset_autocomplete(interaction: discord.Interaction, current: str):
    rows = search_presets(interaction.guild_id, current)[:25]
    return [discord.app_commands.Choice(name=(f"{row['name']} ({describe_schedule(row)})" if has_schedule(row) else row["name"])[:100], value=str(row["id"])) for row in rows]


def _get(preset, key, default=None):
    try:
        value = preset[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def month_add(start, months):
    year, month = divmod(start.year * 12 + start.month - 1 + months, 12)
    month += 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def ordinal(number):
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def has_schedule(preset):
    unit, clock = _get(preset, "repeat", "week"), _get(preset, "time")
    if not clock:
        return False
    if unit == "week":
        return _get(preset, "weekday") is not None
    return unit in ("month", "year", "days") and bool(_get(preset, "anchor"))


def describe_schedule(preset):
    if not has_schedule(preset):
        return "no fixed schedule"
    unit, clock = _get(preset, "repeat", "week"), preset["time"]
    if unit == "week":
        return f"every {WEEKDAYS[preset['weekday']]} at {clock}"
    anchor = date.fromisoformat(preset["anchor"])
    if unit == "month":
        return f"the {ordinal(anchor.day)} of every month at {clock}"
    if unit == "year":
        return f"every year on {anchor.day} {anchor:%B} at {clock}"
    number = _get(preset, "repeat_every", 1)
    return f"every {number} day{'s' if number != 1 else ''} at {clock} (from {anchor.day} {anchor:%b %Y})"


def next_occurrences(preset, now_dt, count=6):
    if not has_schedule(preset):
        return []
    hours, minutes = (int(part) for part in preset["time"].split(":"))
    unit = _get(preset, "repeat", "week")

    def at(dateTime):
        return datetime(dateTime.year, dateTime.month, dateTime.day, hours, minutes, tzinfo=timeZone)

    out = []
    if unit == "week":
        day = now_dt.date()
        while len(out) < count:
            if day.weekday() == preset["weekday"] and at(day) > now_dt:
                out.append(at(day))
            day += timedelta(days=1)
        return out
    anchor = date.fromisoformat(preset["anchor"])
    every = max(1, _get(preset, "repeat_every", 1))
    for step in range(0, 5000):
        if len(out) >= count:
            break
        if unit == "days":
            dateTime = anchor + timedelta(days=step * every)
        elif unit == "month":
            dateTime = month_add(anchor, step)
        else:
            dateTime = month_add(anchor, 12 * step)
        if at(dateTime) > now_dt:
            out.append(at(dateTime))
    return out


def occurrence_pairs(preset, now_dt, count=8):
    unit = _get(preset, "repeat", "week")
    back = {"week": 14, "days": 2 * max(1, _get(preset, "repeat_every", 1)), "month": 70, "year": 800}.get(unit, 14)
    occurrences = next_occurrences(preset, now_dt - timedelta(days=back), count + 12)
    pairs = []
    for index, occurrence in enumerate(occurrences):
        if occurrence > now_dt and len(pairs) < count:
            pairs.append((occurrences[index - 1] if index else None, occurrence))
    return pairs


def post_time(preset, prev):
    if prev is None:
        return None
    if _get(preset, "repeat", "week") == "week":
        monday = prev.date() - timedelta(days=prev.weekday())
        return datetime(monday.year, monday.month, monday.day, 9, 0, tzinfo=timeZone)
    day = prev.date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, 9, 0, tzinfo=timeZone)


async def ask_schedule(bot, user, dmChannel, prev=None):
    prev = prev or {}
    repeat = await choose(
        dmChannel, user, "**How often does it repeat?**",
        [("Every week", "week"), ("Every month", "month"), ("Every year", "year"), ("Every X days", "days"), ("It doesn't repeat on a schedule", "none")],
        prev=prev.get("repeat"), placeholder="How often...",
    )
    weekday, anchor, every = None, None, 1
    if repeat == "week":
        weekday = int(await choose(
            dmChannel, user, "**Which day of the week?**", [(day, str(index)) for index, day in enumerate(WEEKDAYS)],
            prev=None if prev.get("weekday") is None else str(prev["weekday"]), placeholder="Day...",
        ))
    elif repeat in ("month", "year", "days"):
        if repeat == "days":
            every = await pick_interval(bot, user, dmChannel, prev=prev.get("repeat_every"))
        what = {"month": "Later ones fall on the same day of each month.", "year": "Later ones fall on the same date each year.", "days": "I'll count the days from it."}[repeat]
        picked = await pick_date(bot, user, dmChannel, f"**What date is the next one?** {what}")
        anchor = datetime.strptime(picked, "%d/%m/%Y").date().isoformat()
    clock = await pick_time(bot, user, dmChannel, "**Usual start time?**", prev=prev.get("time"), allow_none=True)
    return {"repeat": repeat, "repeat_every": every, "weekday": weekday, "anchor": anchor, "time": clock}


def squash(text, keep, separator):
    out, gap = "", False
    for char in text:
        if keep(char):
            out += char
            gap = False
        elif not gap:
            out += separator
            gap = True
    return out


def safe_name(text):
    return squash(text, lambda char: char.isascii() and (char.isalnum() or char in "._-"), "_")[:80] or "file"


async def capture_message(message, preset_id, index, budget):
    item = {"content": message.content or "", "embeds": [embed.to_dict() for embed in message.embeds if embed.type == "rich"], "poll": None, "files": []}
    if getattr(message, "poll", None):
        poll = message.poll
        hours = max(1, min(768, round(poll.duration.total_seconds() / 3600))) if poll.duration else 168
        item["poll"] = {"question": str(poll.question), "answers": [attachment.text for attachment in poll.answers], "multiple": bool(poll.multiple), "hours": hours}
    for number, attachment in enumerate(message.attachments):
        if attachment.size > MAX_FILE_BYTES or budget["left"] < attachment.size:
            continue
        file_id = db.run(
            "INSERT INTO preset_files (preset_id, filename, spoiler, data) VALUES (?,?,?,?)",
            (preset_id, attachment.filename, int(attachment.is_spoiler()), await attachment.read()),
        )
        budget["left"] -= attachment.size
        item["files"].append({"file_id": file_id, "filename": attachment.filename, "spoiler": attachment.is_spoiler()})
    return item if (item["content"] or item["embeds"] or item["poll"] or item["files"]) else None


async def capture_draft(thread, preset_id):
    items, budget = [], {"left": MAX_TOTAL_BYTES}
    async for message in thread.history(limit=100, oldest_first=True):
        if message.id == thread.id or message.type not in (discord.MessageType.default, discord.MessageType.reply):
            continue
        item = await capture_message(message, preset_id, len(items), budget)
        if item:
            items.append(item)
        if len(items) >= MAX_TEMPLATE_MESSAGES:
            break
    return items


async def copy_template(thread, preset):
    for item in db.loads(preset["content"]):
        try:
            kwargs = {"allowed_mentions": QUIET}
            if item.get("content"):
                kwargs["content"] = item["content"][:2000]
            if item.get("embeds"):
                kwargs["embeds"] = [discord.Embed.from_dict(embed) for embed in item["embeds"][:10]]
            if item.get("poll"):
                item_poll = item["poll"]
                poll = discord.Poll(question=item_poll["question"][:300], duration=timedelta(hours=item_poll["hours"]), multiple=item_poll["multiple"])
                for answer in item_poll["answers"][:10]:
                    poll.add_answer(text=answer[:55])
                kwargs["poll"] = poll
            files = [discord.File(source, filename=file["filename"], spoiler=file.get("spoiler", False)) for file in item.get("files", []) if (source := template_file(file)) is not None]
            if files:
                kwargs["files"] = files
            if len(kwargs) > 1:
                await thread.send(**kwargs)
        except (discord.HTTPException, OSError, ValueError):
            log.exception("couldn't copy a template message into %s", thread.id)


async def post_occurrence(bot, guild, preset, start, close, title, location, byUser=None):
    from features.flows import SignupView, SocialView

    social = preset["kind"] == "social"
    start_ts = int(start.timestamp())
    close_ts = start_ts if social else int(close.timestamp())
    forum = guild.get_channel(preset["forum_id"]) if preset["forum_id"] else None
    if not isinstance(forum, discord.ForumChannel):
        raise RuntimeError("the series' forum no longer exists")
    eventId = db.run(
        "INSERT INTO events (guild_id, title, fb_link, location, start_ts, close_ts, custom_cutoff, kind, markers, closed, preclose_pinged, preset_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (guild.id, title, None, location, start_ts, close_ts, 1, preset["kind"], preset["markers"], int(social), int(social), preset["id"]),
    )
    eventRow = get_event(eventId)
    try:
        created = await forum.create_thread(
            name=f"{title} - {start.astimezone(timeZone):%d %b %Y}"[:100],
            embed=event_embed(eventRow, signup_status(eventRow)), view=SocialView() if social else SignupView(),
            auto_archive_duration=10080,
        )
    except discord.HTTPException:
        db.run("DELETE FROM events WHERE id=?", (eventId,))
        raise
    db.run("UPDATE events SET thread_id=? WHERE id=?", (created.thread.id, eventId))
    await copy_template(created.thread, preset)
    await log_action(bot, guild.id, f"{preset['name']}: posted **{title}** for <t:{start_ts}:f> ({created.thread.jump_url}) " + (f"by {byUser}." if byUser else "automatically."))
    return get_event(eventId), created.thread


def hidden_overwrites(guild, creator_id):
    outwardWord = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    officer = discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages_in_threads=True)
    for rowId in settings.get(guild.id)["roles"]:
        role = guild.get_role(rowId)
        if role:
            outwardWord[role] = officer
    member = guild.get_member(creator_id)
    if member:
        outwardWord[member] = discord.PermissionOverwrite(
            view_channel=True, read_message_history=True, send_messages_in_threads=True, create_public_threads=True,
            attach_files=True, embed_links=True, send_polls=True,
        )
    outwardWord[guild.me] = discord.PermissionOverwrite(
        view_channel=True, send_messages=True, send_messages_in_threads=True, create_public_threads=True, manage_threads=True,
        embed_links=True, attach_files=True, send_polls=True, read_message_history=True,
    )
    return outwardWord


def forum_slug(name):
    return squash(name.lower(), lambda char: char.isascii() and char.isalnum(), "-").strip("-")[:90] or "series"


class DraftView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Finished - save my preset", style=discord.ButtonStyle.success, custom_id="logi:draft_done")
    async def done(self, interaction, button):
        await finish_draft(interaction)

    @discord.ui.button(label="Discard this draft", style=discord.ButtonStyle.danger, custom_id="logi:draft_discard")
    async def discard(self, interaction, button):
        await discard_draft(interaction)


def draft_for(interaction):
    row = db.q1("SELECT * FROM presets WHERE draft_thread_id=?", (interaction.channel_id,))
    return row or db.q1(
        "SELECT * FROM presets WHERE (status='draft' AND created_by=?) OR editor_id=? ORDER BY id DESC LIMIT 1",
        (interaction.user.id, interaction.user.id),
    )


def prune_files(preset_id, items):
    keep = [file["file_id"] for item in items for file in item.get("files", []) if "file_id" in file]
    marks = ",".join("?" * len(keep))
    db.run(f"DELETE FROM preset_files WHERE preset_id=?{f' AND id NOT IN ({marks})' if keep else ''}", (preset_id, *keep))


def template_file(file):
    if "file_id" in file:
        row = db.q1("SELECT data FROM preset_files WHERE id=?", (file["file_id"],))
        return io.BytesIO(row["data"]) if row else None
    if file.get("path") and os.path.exists(file["path"]):
        return file["path"]
    return None


def migrate_template_files():
    moved = 0
    for preset in db.q("SELECT id, content FROM presets"):
        items, changed = db.loads(preset["content"]), False
        for item in items:
            for file in item.get("files", []):
                if "path" in file and "file_id" not in file and os.path.exists(file["path"]):
                    with open(file["path"], "rb") as fileHandle:
                        file["file_id"] = db.run(
                            "INSERT INTO preset_files (preset_id, filename, spoiler, data) VALUES (?,?,?,?)",
                            (preset["id"], file["filename"], int(file.get("spoiler", False)), fileHandle.read()),
                        )
                    del file["path"]
                    changed, moved = True, moved + 1
        if changed:
            db.run("UPDATE presets SET content=? WHERE id=?", (db.dumps(items), preset["id"]))
    return moved


def describe_template(preset):
    items = db.loads(preset["content"])
    if not items:
        return "*The template is empty - new posts will only have the sign-up buttons.*"
    lines = []
    for number, item in enumerate(items, 1):
        parts = []
        if item.get("content"):
            text = item["content"].replace("\n", " ")
            parts.append(f'"{text[:120]}{"..." if len(text) > 120 else ""}"')
        if item.get("poll"):
            poll = item["poll"]
            parts.append(f"poll \"{poll['question'][:80]}\": {', '.join(poll['answers'])} ({'multiple choice' if poll['multiple'] else 'single choice'}, runs {poll['hours']}h)")
        if item.get("embeds"):
            parts.append(f"{len(item['embeds'])} embed(s)")
        if item.get("files"):
            parts.append(", ".join(file["filename"] for file in item["files"]))
        lines.append(f"{number}. " + " - ".join(parts))
    return "\n".join(lines)


def guide_text(preset):
    action = "Press **Going** to say you're coming." if preset["kind"] == "social" else "Press **Driver** or **Passenger** to sort lifts."
    return f"Each {preset['name']} gets its own post here. {action}"


async def ensure_guide_post(forum, preset):
    title = f"About {preset['name']}"[:100]
    names = {thread.name for thread in forum.threads}
    async for thread in forum.archived_threads(limit=100):
        names.add(thread.name)
    if title in names:
        return False
    created = await forum.create_thread(name=title, content=guide_text(preset))
    await created.thread.edit(pinned=True)
    return True


async def tell_presser(interaction, text):
    try:
        await interaction.followup.send(text)
    except discord.HTTPException:
        pass
    if interaction.guild_id is not None:
        try:
            await interaction.user.send(text)
        except discord.HTTPException:
            pass


async def finish_draft(interaction: discord.Interaction):
    preset = draft_for(interaction)
    if not preset:
        return await interaction.response.send_message("I can't find a draft to finish.", ephemeral=True)
    guild = interaction.client.get_guild(preset["guild_id"])
    member = guild.get_member(interaction.user.id)
    owner = interaction.user.id in (preset["created_by"], preset["editor_id"])
    if not owner and not (member and is_manager(member)):
        return await interaction.response.send_message("Only the person working on this draft (or an officer) can finish it.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    bot = interaction.client
    editing = bool(preset["edit_forum_id"])
    dforum = guild.get_channel(preset["edit_forum_id"] if editing else preset["forum_id"])
    try:
        thread = await get_thread(bot, preset["draft_thread_id"])
    except discord.NotFound:
        if editing:
            db.run("UPDATE presets SET edit_forum_id=NULL, editor_id=NULL, draft_thread_id=NULL WHERE id=?", (preset["id"],))
            return await interaction.followup.send("The draft post was deleted, so nothing was changed.")
        db.run("DELETE FROM presets WHERE id=?", (preset["id"],))
        return await interaction.followup.send("The draft post was deleted, so I've dropped this series. Create it again if you still want it.")
    items = await capture_draft(thread, preset["id"])
    prune_files(preset["id"], items)

    if editing:
        db.run("UPDATE presets SET content=?, edit_forum_id=NULL, editor_id=NULL, draft_thread_id=NULL WHERE id=?", (db.dumps(items), preset["id"]))
        await log_action(bot, preset["guild_id"], f"{interaction.user.display_name} updated the template of **{preset['name']}** ({len(items)} message(s)).")
        await tell_presser(interaction, f"**{preset['name']}**'s template now has {len(items)} message(s). New posts will use it; posts already made are unchanged.")
        if dforum:
            try:
                await dforum.delete()
            except discord.HTTPException:
                pass
        return

    sched = has_schedule(preset)
    db.run("UPDATE presets SET content=?, status='ready', draft_thread_id=NULL, auto_enabled=? WHERE id=?", (db.dumps(items), int(sched), preset["id"]))
    try:
        await dforum.set_permissions(guild.default_role, overwrite=None)
        await dforum.edit(topic=f"{preset['name']} - a re-occurring {STYLE_NAMES[preset['kind']]} series.")
        await ensure_guide_post(dforum, preset)
    except discord.HTTPException as errorText:
        log.warning("couldn't fully publish series %s: %s", preset["id"], errorText)
    await log_action(bot, preset["guild_id"], f"{interaction.user.display_name} saved the series **{preset['name']}** (repeats {describe_schedule(preset)}; {len(items)} template message(s)).")
    await tell_presser(
        interaction,
        f"**{preset['name']}** is saved with {len(items)} template message(s) and {dforum.mention} is open to everyone.\n"
        + ("It repeats " + describe_schedule(preset) + ", so I'll post each one myself - the first within a minute. Turn that off with `/preset auto`, or change anything with `/preset edit`."
         if sched else "I'm DMing you now to post the first one. Later: `/event recurring` to post more, or `/preset edit` to give it a schedule."),
    )
    try:
        await thread.delete()
    except discord.HTTPException as errorText:
        log.warning("couldn't delete the draft thread for series %s: %s", preset["id"], errorText)
    from features.flows import launch
    if not sched and interaction.user.id not in bot.active:
        try:
            await launch(
                bot, interaction.user, "occurrence", preset["guild_id"], role=str(preset["id"]),
                intro=f"**{preset['name']}** is saved. Let's post the first one so people can sign up - it will include your template (polls and all).",
            )
        except discord.HTTPException:
            log.warning("couldn't DM %s to post the first occurrence", interaction.user.id)


async def discard_draft(interaction: discord.Interaction):
    preset = draft_for(interaction)
    if not preset:
        return await interaction.response.send_message("I can't find a draft to discard.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    guild = interaction.client.get_guild(preset["guild_id"])
    editing = bool(preset["edit_forum_id"])
    forum = guild.get_channel(preset["edit_forum_id"] if editing else (preset["forum_id"] or 0))
    if editing:
        db.run("UPDATE presets SET edit_forum_id=NULL, editor_id=NULL, draft_thread_id=NULL WHERE id=?", (preset["id"],))
    else:
        db.run("DELETE FROM presets WHERE id=?", (preset["id"],))
    await log_action(interaction.client, preset["guild_id"], f"{interaction.user.display_name} discarded the {'edit of' if editing else 'draft for'} **{preset['name']}**.")
    await tell_presser(interaction, "Edit discarded - the template is unchanged." if editing else "Draft discarded and its forum removed.")
    if forum:
        try:
            await forum.delete()
        except discord.HTTPException:
            pass


async def create_draft(bot, guild, user, preset, editing):
    slug = ("edit-" if editing else "") + forum_slug(preset["name"])
    forum = await guild.create_forum(
        slug, category=await get_category(guild), overwrites=hidden_overwrites(guild, user.id),
        default_auto_archive_duration=10080, topic=f"{'Editing' if editing else 'Draft'} - {preset['name']}",
    )
    if editing:
        intro = (
            f"{user.mention} this is a private copy of **{preset['name']}**'s current template. Nobody else can see this forum.\n\n"
            "**Delete** any message you don't want, and **post** new statements, images or polls (use Discord's poll option) "
            "in this thread. Whatever is here when you press Finished becomes the new template.\n\n"
            f"If it isn't finished within {config.DRAFT_TIMEOUT_HOURS} hours I'll delete this copy and keep the old template."
        )
    else:
        intro = (
            f"{user.mention} this is your private draft for **{preset['name']}**. Nobody else can see this forum yet.\n\n"
            "Add everything you want on every post of this series **in this thread**: statements, rules, images, "
            "and polls (use Discord's poll option). I'll copy it all onto each new post.\n\n"
            "When you're done, press **Finished** below (or in our DMs).\n\n"
            f"If it isn't finished within {config.DRAFT_TIMEOUT_HOURS} hours I'll delete this draft automatically."
        )
    created = await forum.create_thread(
        name=f"{'Edit' if editing else 'Draft'}: {preset['name']}"[:100], content=intro,
        view=DraftView(), allowed_mentions=discord.AllowedMentions(users=True), auto_archive_duration=10080,
    )
    if editing:
        db.run("UPDATE presets SET edit_forum_id=?, editor_id=?, draft_thread_id=?, draft_started=? WHERE id=?", (forum.id, user.id, created.thread.id, int(unix_now()), preset["id"]))
        await copy_template(created.thread, preset)
    else:
        db.run("UPDATE presets SET forum_id=?, draft_thread_id=?, draft_started=? WHERE id=?", (forum.id, created.thread.id, int(unix_now()), preset["id"]))
    return created.thread


async def new_series_flow(bot, user, dmChannel, guild):
    taken = {row["name"].lower() for row in db.q("SELECT name FROM presets WHERE guild_id=?", (guild.id,))}

    def check_name(answer):
        if not 2 <= len(answer) <= 60:
            return "Use between 2 and 60 characters."
        if answer.lower() in taken:
            return "There's already a series with that name."

    while True:
        name = await ask(bot, user, dmChannel, "**What's this series called?** (e.g. Combat Training)", validate=check_name)
        style = await choose(dmChannel, user, "**What should each post offer?**", [
            ("Going + extra questions (like a social)", "social"), ("Lift sign-ups (like an event)", "event")])
        location = await ask(bot, user, dmChannel, "**Where is it usually held?**", quick=[("It varies", "TBC")])
        sched = await ask_schedule(bot, user, dmChannel)
        markers = parse_markers(await ask(bot, user, dmChannel, f"**Extra details to track?** {MARKER_PROMPT}", quick=[("No extra details", "none")])) if style == "social" else []
        summary = (
            f"**New series: {name}**\nPosts offer: {STYLE_NAMES[style]}\nUsually at: {location}\n"
            f"Repeats: {describe_schedule(sched)}\n"
            + (f"Asks people for: {', '.join(markers)}\n" if markers else "")
            + "\nI'll make a private draft post for you to set up. OK?"
        )
        if await confirm(dmChannel, user, summary):
            break
    presetId = db.run(
        "INSERT INTO presets (guild_id, name, kind, location, weekday, time, markers, content, created_by, status, repeat, repeat_every, anchor, auto_enabled) "
        "VALUES (?,?,?,?,?,?,?,?,?,'draft',?,?,?,?)",
        (guild.id, name, style, location, sched["weekday"], sched["time"], db.dumps(markers), db.dumps([]), user.id,
         sched["repeat"], sched["repeat_every"], sched["anchor"], int(has_schedule(sched))),
    )
    try:
        thread = await create_draft(bot, guild, user, get_preset(presetId), editing=False)
    except Exception as errorText:
        log.exception("couldn't open the draft for new series %s", presetId)
        db.run("DELETE FROM presets WHERE id=?", (presetId,))
        return await dmChannel.send(f"I couldn't create the private draft: {type(errorText).__name__}: {errorText}\nNothing was saved - try again once that's sorted.")
    await log_action(bot, guild.id, f"{user.display_name} started a new series **{name}** (private draft opened).")
    await dmChannel.send(
        f"Your private draft is ready: {thread.jump_url}. I've pinged you there.\n"
        "Add your statements, polls and images in that thread, then press **Finished** here when you're done.",
        view=DraftView(),
    )


async def edit_preset_flow(bot, user, dmChannel, guild, eventRow, role):
    preset = get_preset(int(role)) if role else None
    if not preset or preset["status"] != "ready":
        return await dmChannel.send("That preset isn't available any more.")
    if preset["edit_forum_id"]:
        return await dmChannel.send("Someone is already editing this preset's template - finish or discard that first.")
    taken = {row["name"].lower() for row in db.q("SELECT name FROM presets WHERE guild_id=? AND id!=?", (guild.id, preset["id"]))}

    def check_name(answer):
        if not 2 <= len(answer) <= 60:
            return "Use between 2 and 60 characters."
        if answer.lower() in taken:
            return "There's already a series with that name."

    while True:
        name = await ask(bot, user, dmChannel, "**Series name?**", preset["name"], check_name)
        style = await choose(dmChannel, user, "**What should each post offer?** (only affects new posts)", [
            ("Going + extra questions (like a social)", "social"), ("Lift sign-ups (like an event)", "event")], prev=preset["kind"])
        location = await ask(bot, user, dmChannel, "**Usual location?**", preset["location"], quick=[("It varies", "TBC")])
        osched = {key: _get(preset, key) for key in ("repeat", "repeat_every", "weekday", "anchor", "time")}
        osched["repeat"] = osched["repeat"] or "week"
        sched = await ask_schedule(bot, user, dmChannel, osched)
        markers = (
            parse_markers(await ask(bot, user, dmChannel, f"**Extra details to track?** {MARKER_PROMPT}", ", ".join(db.loads(preset["markers"])) or "none", quick=[("No extra details", "none")]))
            if style == "social" else []
        )
        template = await choose(dmChannel, user, "**What about the saved template** (statements, polls, images)?", [
            ("Keep it as it is", "keep"), ("Edit it in a private draft", "edit", "I'll make a hidden copy you can change")])
        changes = []
        for label, old, new in [("Name", preset["name"], name), ("Style", preset["kind"], style), ("Location", preset["location"], location),
                                ("Extra details", db.loads(preset["markers"]), markers)]:
            if old != new:
                shown = lambda value: value if value not in (None, [], "") else "none"
                changes.append(f"{label}: {shown(old)} - {shown(new)}")
        if describe_schedule(osched) != describe_schedule(sched):
            changes.append(f"Repeats: {describe_schedule(osched)} - {describe_schedule(sched)}")
        if template == "edit":
            changes.append("Template: I'll open a private draft for you to change")
        if not changes:
            return await dmChannel.send("Nothing to change.")
        if await confirm(dmChannel, user, "**Apply these changes?**" + "\n" + "\n".join(changes) + "\n" + "Posts already made are not affected."):
            break
    db.run(
        "UPDATE presets SET name=?, kind=?, location=?, weekday=?, time=?, markers=?, repeat=?, repeat_every=?, anchor=? WHERE id=?",
        (name, style, location, sched["weekday"], sched["time"], db.dumps(markers), sched["repeat"], sched["repeat_every"], sched["anchor"], preset["id"]),
    )
    if has_schedule(sched) and not has_schedule(osched):
        db.run("UPDATE presets SET auto_enabled=1 WHERE id=?", (preset["id"],))
    await log_action(bot, guild.id, f"{user.display_name} edited the series **{name}**: " + "; ".join(changes))
    forum = guild.get_channel(preset["forum_id"]) if preset["forum_id"] else None
    if forum and name != preset["name"]:
        try:
            await forum.edit(name=forum_slug(name), topic=f"{name} - a re-occurring {STYLE_NAMES[style]} series.")
        except discord.HTTPException:
            log.warning("couldn't rename the forum for preset %s", preset["id"])
    if template == "edit":
        try:
            thread = await create_draft(bot, guild, user, get_preset(preset["id"]), editing=True)
        except Exception as errorText:
            log.exception("couldn't open the edit draft for preset %s", preset["id"])
            return await dmChannel.send(f"Your changes are saved, but I couldn't open the private draft: {type(errorText).__name__}: {errorText}\nRun `/preset edit` again once that's sorted.")
        await dmChannel.send(
            f"Saved. Your private copy of the template is ready: {thread.jump_url} (I've pinged you there).\n"
            "Delete or add messages and polls, then press **Finished** here when you're done.",
            view=DraftView(),
        )
    else:
        await dmChannel.send("Saved. New posts will use the updated details.")


async def pick_preset(bot, user, dmChannel, guild):
    rows = ready_presets(guild.id)
    if not rows:
        await dmChannel.send("There are no saved presets yet.")
        return None
    text = "*"
    while True:
        if len(rows) > 25:
            text = await ask(bot, user, dmChannel, f"There are **{len(rows)}** presets. Type part of a name to search, or press **Show all**.", quick=[("Show all", "*")])
        found = search_presets(guild.id, text)
        if not found:
            await dmChannel.send("No presets match that - try again.")
            continue
        extra = "\n(Showing the first 25 - type a longer search to narrow it down.)" if len(found) > 25 else ""
        presetId = await choose(dmChannel, user, "**Which preset?**" + extra, [(match["name"], str(match["id"])) for match in found[:25]], placeholder="Pick a preset...")
        return get_preset(int(presetId))


async def occurrence_flow(bot, user, dmChannel, guild, eventRow, role):
    preset = get_preset(int(role)) if role else None
    if not preset or preset["status"] != "ready":
        return await dmChannel.send("That preset isn't available any more.")
    social = preset["kind"] == "social"
    while True:
        upcoming = [day.date() for day in next_occurrences(preset, datetime.now(timeZone), 24)]
        dstr = await pick_date(bot, user, dmChannel, f"**Which date is this {preset['name']}?**", dates=upcoming)
        clock = await pick_time(bot, user, dmChannel, "**Start time?**", prev=preset["time"])
        title = await ask(bot, user, dmChannel, "**Title for this one?**", prev=preset["name"], maxLength=100)
        location = await ask(bot, user, dmChannel, "**Where?**", prev=preset["location"])
        start = parse_local(dstr, clock)
        close = None if social else (await ask_cutoff(bot, user, dmChannel, start))[0]
        summary = (
            f"**{title}** ({preset['name']})\nWhere: {location}\nWhen: <t:{int(start.timestamp())}:F>\n"
            + ("" if social else f"Sign-ups close: <t:{int(close.timestamp())}:F>\n")
            + f"I'll also copy your saved template ({len(db.loads(preset['content']))} message(s)).\n\nPost it?"
        )
        if await confirm(dmChannel, user, summary):
            break
    clash = db.q1("SELECT thread_id FROM events WHERE preset_id=? AND start_ts=? AND cancelled=0", (preset["id"], int(start.timestamp())))
    if clash:
        return await dmChannel.send(f"That one is already posted: <#{clash['thread_id']}>")
    _, thread = await post_occurrence(bot, guild, preset, start, close, title, location, byUser=user.display_name)
    await dmChannel.send(f"Posted: {thread.jump_url}")


async def recurring_entry(bot, user, dmChannel, guild):
    if ready_presets(guild.id):
        how = await choose(dmChannel, user, "**Re-occurring event**", [
            ("Use a saved preset", "use", "Pick from your existing series"), ("Create a new series", "new", "Set up a new preset in a private draft")])
    else:
        how = "new"
        await dmChannel.send("You don't have any saved series yet, so let's make the first one.")
    if how == "new":
        return await new_series_flow(bot, user, dmChannel, guild)
    preset = await pick_preset(bot, user, dmChannel, guild)
    if preset:
        await occurrence_flow(bot, user, dmChannel, guild, None, str(preset["id"]))


async def auto_post_presets(bot, now):
    now_dt = datetime.fromtimestamp(now, timeZone)
    for preset in db.q("SELECT * FROM presets WHERE status='ready' AND auto_enabled=1"):
        guild = bot.get_guild(preset["guild_id"])
        if not guild:
            continue
        for prev, start in occurrence_pairs(preset, now_dt, count=8):
            due = post_time(preset, prev)
            if due is not None and now_dt < due:
                break
            start_ts = int(start.timestamp())
            if db.q1("SELECT 1 FROM events WHERE preset_id=? AND start_ts=?", (preset["id"], start_ts)):
                continue
            close = None
            if preset["kind"] == "event":
                close = signup_close(start)
                if close.timestamp() <= now:
                    close = datetime.fromtimestamp(min(now + 86400, start_ts - 3600), timeZone)
            try:
                eventRow, thread = await post_occurrence(bot, guild, preset, start, close, preset["name"], preset["location"])
            except Exception:
                log.exception("auto-posting %s failed", preset["name"])
                continue
            await notify_log(bot, guild, f"Auto-posted **{preset['name']}** for <t:{start_ts}:F>: {thread.jump_url}", ping=False)


async def delete_preset(bot, guild, preset, mode, byUser):
    forum = guild.get_channel(preset["forum_id"]) if preset["forum_id"] else None
    name = preset["name"]
    if mode == "all":
        db.run("UPDATE events SET cancelled=1, closed=1, reminded=1, preclose_pinged=1 WHERE preset_id=?", (preset["id"],))
        if forum:
            try:
                await forum.delete()
            except discord.HTTPException:
                log.warning("couldn't delete the forum for preset %s", preset["id"])
        db.run("DELETE FROM presets WHERE id=?", (preset["id"],))
        result = f"Deleted **{name}** and its forum, with every post in it."
    elif mode == "archive":
        db.run("UPDATE presets SET status='archived', auto_enabled=0 WHERE id=?", (preset["id"],))
        if forum:
            try:
                await forum.set_permissions(
                    guild.default_role, send_messages=False, send_messages_in_threads=False, create_public_threads=False, add_reactions=False,
                )
                await forum.edit(name=f"archived-{forum_slug(name)}"[:100])
                threads = list(forum.threads)
                async for thread in forum.archived_threads(limit=None):
                    threads.append(thread)
                for thread in threads:
                    try:
                        await thread.edit(archived=True, locked=True)
                    except discord.HTTPException:
                        pass
            except discord.HTTPException:
                log.warning("couldn't fully archive the forum for preset %s", preset["id"])
        result = f"Archived **{name}**: its forum is now read-only with every post closed, and it will no longer post anything."
    else:
        db.run("DELETE FROM presets WHERE id=?", (preset["id"],))
        result = f"Deleted the preset **{name}**. Its forum and old posts are untouched."
    if mode != "archive":
        db.run("DELETE FROM preset_files WHERE preset_id=?", (preset["id"],))
    await log_action(bot, guild.id, f"{byUser} {result[0].lower() + result[1:]}")
    return result


async def expire_drafts(bot, now):
    limit = config.DRAFT_TIMEOUT_HOURS * 3600
    for preset in db.q("SELECT * FROM presets WHERE status='draft' OR edit_forum_id IS NOT NULL"):
        if preset["draft_started"] is None:
            db.run("UPDATE presets SET draft_started=? WHERE id=?", (now, preset["id"]))
            continue
        if now - preset["draft_started"] < limit:
            continue
        guild = bot.get_guild(preset["guild_id"])
        editing = bool(preset["edit_forum_id"])
        forum_id = preset["edit_forum_id"] if editing else preset["forum_id"]
        forum = guild.get_channel(forum_id) if guild and forum_id else None
        if forum:
            try:
                await forum.delete()
            except discord.HTTPException:
                log.warning("couldn't delete the expired draft forum for preset %s", preset["id"])
        if editing:
            db.run("UPDATE presets SET edit_forum_id=NULL, editor_id=NULL, draft_thread_id=NULL WHERE id=?", (preset["id"],))
            message = f"Your private edit of **{preset['name']}** wasn't finished within {config.DRAFT_TIMEOUT_HOURS} hours, so I deleted it. The saved template is unchanged - run `/preset edit` to start again."
            who = preset["editor_id"]
        else:
            db.run("DELETE FROM presets WHERE id=?", (preset["id"],))
            db.run("DELETE FROM preset_files WHERE preset_id=?", (preset["id"],))
            message = f"Your private draft for the series **{preset['name']}** wasn't finished within {config.DRAFT_TIMEOUT_HOURS} hours, so I deleted it. Create it again whenever you're ready."
            who = preset["created_by"]
        await dm_user(bot, who, message)
        await log_action(bot, preset["guild_id"], f"The unfinished {'edit' if editing else 'draft'} of **{preset['name']}** expired after {config.DRAFT_TIMEOUT_HOURS} hours and was deleted.")
