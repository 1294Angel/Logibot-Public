import asyncio, json, time, discord
from datetime import datetime

from core import config, db, settings
from core.allocation import timeZone, parse_local, resolve_entries, signup_close
from features.cars import apply_edit, late_join, start_withdraw, withdraw
from features.presets import edit_preset_flow, occurrence_flow, recurring_entry
from core.ui import (
    NEEDS, Cancelled, ConfirmView, MARKER_PROMPT, ask, ask_cutoff, check_clock, check_date, check_link,
    check_link_optional, check_need, check_seats, choose, confirm, link_or_none, make_cutoff_checker, parse_markers,
    pick_date, pick_time,
)
from core.util import (
    NOTIFY, QUIET, dm_user, event_by_thread, event_embed, get_event, get_forum, get_thread,
    conflict_text, is_manager, is_social, log, log_action, markers_of, missing_perms, name_of, notify_officers, refresh_event_post, signup_status,
)

RESTART_TEXT = "Bot died... Not ideal, restart the conversation"

DRIVER_ACK = (
    "You understand you will need to discuss details such as expected fuel cost, pickup times "
    "and locations, and how much kit you can realistically take with the number of people you "
    "have said you can bring."
)
PASSENGER_ACK = (
    "You understand you will need to discuss details such as expected fuel prices, what kit the "
    "driver can fit and so on with the driver themselves - and the driver has last say over "
    "matters with their car. If there are any issues, bring them up with Officership so they "
    "may be resolved."
)
CONFIRM_LINE = "**If all is correct press Yes.** Any issues, bring them up with Officership so they can be handled."
AREA_HELP = "Give a vague postcode (e.g. `CA1`) or a nearby landmark - not your full address. It's only used to group people who live near each other."


def unix_now():
    return time.time()


def get_profile(guild_id, user_id):
    return db.q1("SELECT * FROM profiles WHERE guild_id=? AND user_id=?", (guild_id, user_id))


def save_profile(guild_id, user_id, **fields):
    allowed = {"capacity", "avoid", "organised", "address", "kit", "area", "need"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown profile fields: {sorted(unknown)}")
    db.run("INSERT OR IGNORE INTO profiles (guild_id, user_id) VALUES (?,?)", (guild_id, user_id))
    fields["updated_at"] = int(unix_now())
    sets = ", ".join(f"{key}=?" for key in fields)
    db.run(f"UPDATE profiles SET {sets} WHERE guild_id=? AND user_id=?", (*fields.values(), guild_id, user_id))


def save_signup(event_id, user_id, role, **fields):
    db.run(
        "INSERT OR REPLACE INTO signups (event_id, user_id, role, capacity, avoid, organised, address, kit, area, need, details) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, user_id, role, fields.get("capacity"), fields.get("avoid", "[]"), fields.get("organised", "[]"),
         fields.get("address"), fields.get("kit"), fields.get("area"), fields.get("need", "both"), fields.get("details")),
    )


def show_matches(guild, entries):
    if not entries:
        return "*nobody*"
    lines = []
    for entry in entries:
        if entry["id"]:
            lines.append(f"{entry['label']} - **{name_of(guild, entry['id'])}**")
        else:
            lines.append(f"{entry['label']} - no matching server member, saved as text")
    return "\n".join(lines)


def organised_problem(guild, text, seats):
    entries = resolve_entries(guild, text)
    bad = []
    if len(entries) > seats:
        bad.append(f"You listed {len(entries)} people but only have {seats} seat(s).")
    unknownNames = [entry["label"] for entry in entries if not entry["id"]]
    if unknownNames:
        bad.append("I couldn't find these people in the server: " + ", ".join(f"**{name}**" for name in unknownNames) + ".")
    if not bad:
        return None
    return "\n".join(bad) + "\nPlease retype the whole list (at most " + str(seats) + " names, each a Steam ID, username or server nickname), or press **Nobody**."


def fmt_date(timestamp):
    return datetime.fromtimestamp(timestamp, timeZone).strftime("%d/%m/%Y %H:%M")


async def run_driver(bot, user, dmChannel, guild, eventRow):
    areas = settings.get(guild.id)["area_matching"]
    prev = dict(get_profile(guild.id, user.id) or {})
    total = 5 if areas else 4
    while True:
        seatCapacity = await choose(
            dmChannel, user, f"**1/{total}** How many passengers can you take? (With kit in mind, so be conservative)",
            [(f"{index} passenger{'s' if index != 1 else ''}", str(index)) for index in range(13)],
            prev=prev.get("capacity"), placeholder="Number of passengers...",
        )
        avoid_txt = await ask(
            bot, user, dmChannel,
            f"**2/{total}** Is there anyone you don't want to take? This will **not** be shared publicly, but it is "
            "stored so it can be reused next time, and may be seen by Officership in the event of bugs or a "
            "grievance. Give Steam ID, username or server nickname - separate several with commas - or press **Nobody**.",
            prev.get("avoid"), quick=[("Nobody", "none")], maxLength=500,
        )
        org_txt = await ask(
            bot, user, dmChannel,
            f"**3/{total}** Is there anyone you've already organised to take? (Steam ID, username or server nickname, "
            "separated by commas, or press **Nobody**). They don't need to have signed up - they'll be guaranteed a seat in your car.",
            prev.get("organised"), quick=[("Nobody", "none")], maxLength=500,
            validate=lambda text: organised_problem(guild, text, int(seatCapacity)),
        )
        addr = await ask(bot, user, dmChannel, f"**4/{total}** What is your ideal pickup address?", prev.get("address"))
        area = await ask(bot, user, dmChannel, f"**5/5** Roughly where will you be setting off from? {AREA_HELP}", prev.get("area")) if areas else None

        avoid, organised = resolve_entries(guild, avoid_txt), resolve_entries(guild, org_txt)
        summary = (
            f"**Please check your answers**\nPassengers: **{seatCapacity}**\n\n"
            f"Won't take:\n{show_matches(guild, avoid)}\n\n"
            f"Already organised:\n{show_matches(guild, organised)}\n\n"
            f"Pickup address: **{addr}** (shown in the cars post once lifts are sorted)\n"
            + (f"Area: **{area}**\n" if area else "")
            + f"\n{DRIVER_ACK}\n\n{CONFIRM_LINE}"
        )
        prev = {"capacity": seatCapacity, "avoid": avoid_txt, "organised": org_txt, "address": addr, "area": area}
        if await confirm(dmChannel, user, summary):
            save_profile(guild.id, user.id, capacity=int(seatCapacity), avoid=avoid_txt, organised=org_txt, address=addr, **({"area": area} if area else {}))
            save_signup(eventRow["id"], user.id, "driver", capacity=int(seatCapacity), avoid=db.dumps(avoid), organised=db.dumps(organised), address=addr, area=area)
            return


async def run_passenger(bot, user, dmChannel, guild, eventRow):
    areas = settings.get(guild.id)["area_matching"]
    prev = dict(get_profile(guild.id, user.id) or {})
    total = 3 if areas else 2
    while True:
        need = await choose(
            dmChannel, user, f"**1/{total}** Which lifts do you need? (We'll try to keep you in the same car both ways.)",
            [("A lift there only", "to"), ("A lift back only", "back"), ("Both ways", "both")],
            prev=prev.get("need"), placeholder="Lifts needed...",
        )
        kit = await ask(bot, user, dmChannel, f"**2/{total}** What kit are you bringing?", prev.get("kit"))
        area = await ask(bot, user, dmChannel, f"**3/3** Roughly where would you want picking up from? {AREA_HELP}", prev.get("area")) if areas else None
        prev = {"need": need, "kit": kit, "area": area}
        summary = f"Lift needed: **{need}**\nKit: **{kit}**\n" + (f"Area: **{area}**\n" if area else "") + f"\n{PASSENGER_ACK}\n\n{CONFIRM_LINE}"
        if await confirm(dmChannel, user, summary):
            save_profile(guild.id, user.id, kit=kit, need=need, **({"area": area} if area else {}))
            save_signup(eventRow["id"], user.id, "passenger", kit=kit, need=need, area=area)
            return


async def signup_flow(bot, user, dmChannel, guild, eventRow, role):
    eventRow = get_event(eventRow["id"])
    if not eventRow or eventRow["cancelled"] or unix_now() >= eventRow["start_ts"]:
        return await dmChannel.send("That event is no longer open.")
    old = db.q1("SELECT * FROM signups WHERE event_id=? AND user_id=?", (eventRow["id"], user.id))
    if old and old["role"] in ("driver", "passenger") and old["role"] != role:
        return await dmChannel.send(f"You're signed up as a {old['role']}. Pull out first, then sign up as a {role} - you can't be both.")
    await (run_driver if role == "driver" else run_passenger)(bot, user, dmChannel, guild, eventRow)
    eventRow = get_event(eventRow["id"])
    editing = bool(old and old["role"] == role)
    if eventRow["closed"]:
        if editing:
            new = db.q1("SELECT * FROM signups WHERE event_id=? AND user_id=?", (eventRow["id"], user.id))
            await apply_edit(bot, eventRow, user, role, old, new)
            await dmChannel.send("Updated. Anyone this changes things for has been told.")
        else:
            await late_join(bot, eventRow, user, role)
            await dmChannel.send(
                f"You're down as a **{role}** for **{eventRow['title']}**. Sign-ups had already closed, so I've slotted you in "
                "where I can and told Officership - I'll DM you if anything changes."
            )
    elif editing:
        await dmChannel.send(f"Your details for **{eventRow['title']}** are updated. Lifts are sorted when sign-ups close (<t:{eventRow['close_ts']}:F>).")
    else:
        await dmChannel.send(
            f"You're down as a **{role}** for **{eventRow['title']}**. Lifts get sorted when sign-ups close "
            f"(<t:{eventRow['close_ts']}:F>) and I'll DM you then. Press your button again in the event post to update your answers."
        )
    if not editing:
        warn = conflict_text(eventRow, user.id)
        if warn:
            await dmChannel.send(warn)
    verb = f"updated their {role} details for" if editing else f"signed up as a {role} for"
    await log_action(bot, eventRow["guild_id"], f"{user.display_name} {verb} **{eventRow['title']}**.")
    await refresh_event_post(bot, eventRow)


async def create_event_flow(bot, user, dmChannel, guild, eventRow, role):
    gone = missing_perms(guild)
    if gone:
        return await dmChannel.send("I'm missing permissions in that server: " + ", ".join(gone) + ". Fix those and try again.")
    while True:
        kind = await choose(
            dmChannel, user, "**What are you creating?**",
            [("Event - lifts get organised", "event"), ("Social - just track who's going", "social"),
             ("Re-occurring - a repeating event with saved presets", "recurring")],
            placeholder="Pick a type...",
        )
        if kind == "recurring":
            return await recurring_entry(bot, user, dmChannel, guild)
        social = kind == "social"
        title = await ask(bot, user, dmChannel, "**What's it called?** (e.g. Carlisle Show)", maxLength=100)
        location = await ask(bot, user, dmChannel, "**Where is it?** (e.g. Carlisle Castle)")
        link = link_or_none(await ask(bot, user, dmChannel, "**Facebook event link?** Paste it, or press **No link**.", validate=check_link_optional, quick=[("No link", "none")]))
        date = await pick_date(bot, user, dmChannel, "**Date?**")
        clock = await pick_time(bot, user, dmChannel, "**Start time?**")
        start = parse_local(date, clock)
        start_ts = int(start.timestamp())
        if social:
            markers = parse_markers(await ask(bot, user, dmChannel, f"**Extra details to track?** {MARKER_PROMPT}", quick=[("No extra details", "none")]))
            close, dflt, cutoff = start_ts, True, ""
        else:
            markers = []
            cutoffTime, dflt = await ask_cutoff(bot, user, dmChannel, start)
            close = int(cutoffTime.timestamp())
            cutoff = f"Sign-ups close: <t:{close}:F>\n"
        summary = (
            f"**{'Social' if social else 'Event'}: {title}**\nWhere: {location}\nWhen: <t:{start_ts}:F>\n"
            + (f"Facebook: {link}\n" if link else "") + cutoff
            + (f"I'll ask people for: {', '.join(markers)}\n" if markers else "")
            + "\nPost this to the forum?"
        )
        if await confirm(dmChannel, user, summary):
            break
    eventId = db.run(
        "INSERT INTO events (guild_id, title, fb_link, location, start_ts, close_ts, custom_cutoff, kind, markers, closed, preclose_pinged) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (guild.id, title, link, location, start_ts, close, 0 if dflt else 1, kind, db.dumps(markers), int(social), int(social)),
    )
    eventRow = get_event(eventId)
    forum = await get_forum(guild, kind)
    created = await forum.create_thread(
        name=f"{title} - {start.astimezone(timeZone):%d %b %Y}"[:100],
        embed=event_embed(eventRow, signup_status(eventRow)), view=SocialView() if social else SignupView(),
        auto_archive_duration=10080,
    )
    db.run("UPDATE events SET thread_id=? WHERE id=?", (created.thread.id, eventId))
    await dmChannel.send(f"{'Social' if social else 'Event'} posted: {created.thread.jump_url}")
    await log_action(bot, guild.id, f"{user.display_name} created the {kind} **{title}** ({created.thread.jump_url}).")


async def edit_event_flow(bot, user, dmChannel, guild, eventRow, role):
    eventRow = get_event(eventRow["id"])
    if eventRow["cancelled"]:
        return await dmChannel.send("That event has been cancelled.")
    social = is_social(eventRow)
    old_ts = datetime.fromtimestamp(eventRow["start_ts"], timeZone)
    while True:
        title = await ask(bot, user, dmChannel, "**Title?**", eventRow["title"], maxLength=100)
        location = await ask(bot, user, dmChannel, "**Location?**", eventRow["location"])
        link = link_or_none(await ask(bot, user, dmChannel, "**Facebook link?** (or `none`)", eventRow["fb_link"] or "none", check_link_optional, quick=[("No link", "none")]))
        date = await ask(bot, user, dmChannel, "**Date?** (DD/MM/YYYY)", old_ts.strftime("%d/%m/%Y"), check_date)
        clock = await ask(bot, user, dmChannel, "**Start time?** (24h HH:MM)", old_ts.strftime("%H:%M"), check_clock)
        markers = parse_markers(await ask(bot, user, dmChannel, f"**Extra details to track?** {MARKER_PROMPT}", ", ".join(markers_of(eventRow)) or "none", quick=[("No extra details", "none")])) if social else []
        start = parse_local(date, clock)
        now = unix_now()
        eventRow = get_event(eventRow["id"])
        if eventRow["closed"]:
            if start.timestamp() <= now:
                await dmChannel.send("That can't be moved into the past - let's go again.")
                continue
            close, custom = (int(start.timestamp()) if social else eventRow["close_ts"]), eventRow["custom_cutoff"]
            cutoff = "" if social else f"Sign-ups closed (<t:{close}:F>) - cutoff unchanged"
        else:
            default = signup_close(start)
            prev = fmt_date(eventRow["close_ts"]) if eventRow["custom_cutoff"] else (default.strftime("%d/%m/%Y %H:%M") if default.timestamp() > now else fmt_date(eventRow["close_ts"]))
            quick = [(f"Use default: {default:%a %d %b %H:%M}", "default")] if default.timestamp() > now else []
            answer = await ask(
                bot, user, dmChannel,
                f"**Sign-up cutoff?** Currently <t:{eventRow['close_ts']}:F>. Press the default button for 9AM the Monday before the event, or type DD/MM/YYYY HH:MM.",
                prev, make_cutoff_checker(start, default, now, {"default"}), quick=quick,
            )
            cutoffTime = default if answer.lower() == "default" else parse_local(*answer.split(" ", 1))
            if cutoffTime.timestamp() >= start.timestamp():
                await dmChannel.send("The cutoff must be before the event starts - let's go again.")
                continue
            close = int(cutoffTime.timestamp())
            custom = 0 if cutoffTime == default else 1
            cutoff = f"Sign-ups close: <t:{close}:F>"
        start_ts = int(start.timestamp())
        changes = []
        for label, old, new in [("Title", eventRow["title"], title), ("Location", eventRow["location"], location), ("Facebook", eventRow["fb_link"], link)]:
            if old != new:
                changes.append(f"{label}: {old} - {new}")
        if start_ts != eventRow["start_ts"]:
            changes.append(f"When: <t:{eventRow['start_ts']}:F> - <t:{start_ts}:F>")
        if not social and close != eventRow["close_ts"]:
            changes.append(f"Sign-ups close: <t:{eventRow['close_ts']}:F> - <t:{close}:F>")
        if social and markers != markers_of(eventRow):
            changes.append(f"Extra details: {', '.join(markers_of(eventRow)) or 'none'} - {', '.join(markers) or 'none'}")
        if not changes:
            return await dmChannel.send("Nothing changed.")
        summary = "**Apply these changes?**\n" + "\n".join(changes) + (f"\n\n{cutoff}" if cutoff else "") + "\nEveryone signed up will be told."
        if await confirm(dmChannel, user, summary):
            break
    eventRow = get_event(eventRow["id"])
    db.run(
        "UPDATE events SET title=?, location=?, fb_link=?, start_ts=?, close_ts=?, custom_cutoff=?, markers=? WHERE id=?",
        (title, location, link, start_ts, close, custom, db.dumps(markers) if social else eventRow["markers"], eventRow["id"]),
    )
    now = unix_now()
    if not social and close != eventRow["close_ts"] and not eventRow["closed"]:
        db.run("UPDATE events SET preclose_pinged=? WHERE id=?", (int(close - 86400 <= now), eventRow["id"]))
    if start_ts != eventRow["start_ts"]:
        db.run("UPDATE events SET reminded=? WHERE id=?", (int(start_ts - 86400 <= now), eventRow["id"]))
    new = get_event(eventRow["id"])
    thread = await get_thread(bot, new["thread_id"])
    try:
        await thread.edit(name=f"{title} - {start.astimezone(timeZone):%d %b %Y}"[:100])
    except discord.HTTPException:
        pass
    await refresh_event_post(bot, new)
    await thread.send("**Updated** by " + user.display_name + "\n" + "\n".join(changes), allowed_mentions=QUIET)
    userIds = {row["user_id"] for row in db.q("SELECT user_id FROM signups WHERE event_id=?", (eventRow["id"],))}
    userIds |= {row["user_id"] for row in db.q("SELECT user_id FROM assignments WHERE event_id=?", (eventRow["id"],)) if row["user_id"]}
    for userId in userIds:
        await dm_user(bot, userId, f"**{new['title']}** has changed:\n" + "\n".join(changes))
    await log_action(bot, eventRow["guild_id"], f"{user.display_name} edited **{new['title']}**: " + "; ".join(changes))
    await dmChannel.send("Done - the post is updated and everyone signed up has been told.")


async def going_flow(bot, user, dmChannel, guild, eventRow, role):
    eventRow = get_event(eventRow["id"])
    if not eventRow or eventRow["cancelled"] or unix_now() >= eventRow["start_ts"]:
        return await dmChannel.send("That social is no longer open.")
    markers = markers_of(eventRow)
    row = db.q1("SELECT details FROM signups WHERE event_id=? AND user_id=?", (eventRow["id"], user.id))
    prev = json.loads(row["details"]) if row and row["details"] else {}
    while True:
        answers = {}
        for marker in markers:
            answers[marker] = await ask(bot, user, dmChannel, f"**{marker}?**", prev.get(marker))
        summary = "**You're going to " + eventRow["title"] + "**\n" + "\n".join(f"{key}: **{value}**" for key, value in answers.items()) + f"\n\n{CONFIRM_LINE}"
        prev = answers
        if await confirm(dmChannel, user, summary):
            break
    save_signup(eventRow["id"], user.id, "going", details=json.dumps(answers))
    await dmChannel.send(f"You're down as going to **{eventRow['title']}**. Press Going again to change your answers.")
    await log_action(bot, eventRow["guild_id"], f"{user.display_name} is going to **{eventRow['title']}**: " + ", ".join(f"{key}: {value}" for key, value in answers.items()))
    warn = conflict_text(eventRow, user.id)
    if warn:
        await dmChannel.send(warn)
    await refresh_event_post(bot, eventRow)


async def cancel_event(bot, eventRow, byUser):
    eventRow = get_event(eventRow["id"])
    db.run("UPDATE events SET cancelled=1, closed=1, reminded=1 WHERE id=?", (eventRow["id"],))
    eventRow = get_event(eventRow["id"])
    userIds = {row["user_id"] for row in db.q("SELECT user_id FROM signups WHERE event_id=?", (eventRow["id"],))}
    userIds |= {row["user_id"] for row in db.q("SELECT user_id FROM assignments WHERE event_id=?", (eventRow["id"],)) if row["user_id"]}
    thread = await get_thread(bot, eventRow["thread_id"])
    await refresh_event_post(bot, eventRow)
    for messageId in (eventRow["cars_message_id"],):
        if messageId:
            try:
                await (await thread.fetch_message(messageId)).edit(view=None)
            except discord.HTTPException:
                pass
    try:
        await (await thread.fetch_message(thread.id)).edit(view=None)
    except discord.HTTPException:
        pass
    mentions = [f"<@{index}>" for index in sorted(userIds)]
    await thread.send(f"**{eventRow['title']} has been cancelled** (by {byUser.display_name}). No lifts are needed.", allowed_mentions=QUIET)
    for index in range(0, len(mentions), 40):
        await thread.send(" ".join(mentions[index:index + 40]), allowed_mentions=discord.AllowedMentions(users=True))
    for userId in userIds:
        await dm_user(bot, userId, f"**{eventRow['title']}** has been cancelled, so any lift arrangements are off.")
    await notify_officers(bot, eventRow, f"Event cancelled by **{byUser.display_name}**; {len(userIds)} people told.")
    await log_action(bot, eventRow["guild_id"], f"{byUser.display_name} cancelled **{eventRow['title']}**; {len(userIds)} people told.")
    try:
        await thread.edit(locked=True, archived=True)
    except discord.HTTPException:
        pass


class CancelConfirm(discord.ui.View):
    def __init__(self, bot, eventRow, user):
        super().__init__(timeout=120)
        self.bot, self.eventRow, self.user = bot, eventRow, user

    @discord.ui.button(label="Yes, cancel the event", style=discord.ButtonStyle.danger)
    async def go(self, interaction, button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Cancelling...", view=self)
        self.stop()
        await cancel_event(self.bot, self.eventRow, interaction.user)
        await interaction.edit_original_response(content="Cancelled. Everyone has been told.")


FLOWS = {"preset_edit": edit_preset_flow, "occurrence": occurrence_flow, "going": going_flow, "signup": signup_flow, "create": create_event_flow, "edit": edit_event_flow}


async def _run(bot, user, dmChannel, kind, guild_id, event_id, role):
    try:
        guild = bot.get_guild(guild_id)
        eventRow = get_event(event_id) if event_id else None
        await FLOWS[kind](bot, user, dmChannel, guild, eventRow, role)
    except Cancelled:
        pass
    except Exception:
        log.exception("%s conversation failed", kind)
        try:
            await dmChannel.send("Something went wrong on my side - please tell Officership and try again.")
        except discord.HTTPException:
            pass
    finally:
        bot.active.discard(user.id)
        db.run("DELETE FROM sessions WHERE user_id=?", (user.id,))


async def launch(bot, user, kind, guild_id, event_id=None, role=None, intro="Let's get started. Reply to each question below."):
    dmChannel = await user.create_dm()
    await dmChannel.send(intro)
    bot.active.add(user.id)
    db.run("INSERT OR REPLACE INTO sessions (user_id, guild_id, kind, event_id, role) VALUES (?,?,?,?,?)", (user.id, guild_id, kind, event_id, role))
    bot.loop.create_task(_run(bot, user, dmChannel, kind, guild_id, event_id, role))


async def restore_sessions(bot):
    for sess in db.q("SELECT * FROM sessions"):
        try:
            user = await bot.fetch_user(sess["user_id"])
            await launch(bot, user, sess["kind"], sess["guild_id"], sess["event_id"], sess["role"], RESTART_TEXT)
        except Exception:
            log.warning("couldn't restore session for %s", sess["user_id"])
            db.run("DELETE FROM sessions WHERE user_id=?", (sess["user_id"],))


async def _begin(interaction, kind, role, intro, eventRow):
    bot = interaction.client
    if interaction.user.id in bot.active:
        return await interaction.response.send_message("Check your DMs - we're already mid-conversation.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        await launch(bot, interaction.user, kind, eventRow["guild_id"], eventRow["id"], role, intro)
    except discord.Forbidden:
        return await interaction.followup.send(
            "I can't DM you - enable DMs from server members in your privacy settings and press the button again.", ephemeral=True)
    await interaction.followup.send("Check your DMs ", ephemeral=True)


async def start_signup(interaction: discord.Interaction, role):
    eventRow = event_by_thread(interaction.channel_id)
    if not eventRow or eventRow["cancelled"]:
        return await interaction.response.send_message("This event isn't taking sign-ups.", ephemeral=True)
    if unix_now() >= eventRow["start_ts"]:
        return await interaction.response.send_message("This event has already started.", ephemeral=True)
    userId = interaction.user.id
    sign = db.q1("SELECT role FROM signups WHERE event_id=? AND user_id=?", (eventRow["id"], userId))
    held = sign["role"] if sign and sign["role"] in ("driver", "passenger") else None
    if held and held != role:
        return await interaction.response.send_message(
            f"You're signed up as a {held}. You can't be both - use Pull out, then sign up as a {role}.", ephemeral=True)
    closed = eventRow["closed"] or unix_now() >= eventRow["close_ts"]
    if closed and not held and db.q1("SELECT 1 FROM assignments WHERE event_id=? AND user_id=?", (eventRow["id"], userId)):
        return await interaction.response.send_message("You're already in a car - ask Officership if anything needs changing.", ephemeral=True)
    if held:
        intro = f"Updating your **{role}** details for **{eventRow['title']}**." + (
            " Anyone this changes things for will be told." if closed else "") + " Reply to each question below."
    else:
        intro = f"Signing you up as a **{role}** for **{eventRow['title']}**." + (
            " Sign-ups have closed, but I can still add you - I'll fit you in where I can and tell Officership." if closed else "") + " Reply to each question below."
    await _begin(interaction, "signup", role, intro, eventRow)


async def start_edit_details(interaction: discord.Interaction):
    eventRow = event_by_thread(interaction.channel_id)
    if not eventRow or eventRow["cancelled"]:
        return await interaction.response.send_message("This event isn't active.", ephemeral=True)
    sign = db.q1("SELECT role FROM signups WHERE event_id=? AND user_id=?", (eventRow["id"], interaction.user.id))
    if not sign or sign["role"] not in ("driver", "passenger"):
        return await interaction.response.send_message("Sign up as a Driver or Passenger first.", ephemeral=True)
    await start_signup(interaction, sign["role"])


async def start_going(interaction: discord.Interaction):
    eventRow = event_by_thread(interaction.channel_id)
    if not eventRow or eventRow["cancelled"]:
        return await interaction.response.send_message("This event isn't active.", ephemeral=True)
    if unix_now() >= eventRow["start_ts"]:
        return await interaction.response.send_message("This event has already started.", ephemeral=True)
    userId = interaction.user.id
    sign = db.q1("SELECT role FROM signups WHERE event_id=? AND user_id=?", (eventRow["id"], userId))
    if sign and sign["role"] == "going":
        return await interaction.response.send_message("You're already down as going.", ephemeral=True)
    if sign and sign["role"] == "driver":
        return await interaction.response.send_message("You're signed up as a driver - use Pull out first if you can't drive any more.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    if sign or db.q1("SELECT 1 FROM assignments WHERE event_id=? AND user_id=?", (eventRow["id"], userId)):
        await withdraw(interaction.client, eventRow, interaction.user, "own")
    else:
        save_signup(eventRow["id"], userId, "going")
        await refresh_event_post(interaction.client, eventRow)
    await log_action(interaction.client, eventRow["guild_id"], f"{interaction.user.display_name} is going to **{eventRow['title']}** without needing a lift.")
    await interaction.followup.send(
        "Marked as going - no lift needed. If a driver is already taking you, ask them to add your name or ID to their sign-up so your seat is counted."
        + ("\n" + conflict_text(eventRow, userId) if conflict_text(eventRow, userId) else ""),
        ephemeral=True,
    )


async def start_social_going(interaction: discord.Interaction):
    eventRow = event_by_thread(interaction.channel_id)
    if not eventRow or eventRow["cancelled"] or not is_social(eventRow):
        return await interaction.response.send_message("This social isn't active.", ephemeral=True)
    if unix_now() >= eventRow["start_ts"]:
        return await interaction.response.send_message("This has already started.", ephemeral=True)
    if markers_of(eventRow):
        return await _begin(interaction, "going", None, f"Marking you as going to **{eventRow['title']}**.", eventRow)
    save_signup(eventRow["id"], interaction.user.id, "going")
    await refresh_event_post(interaction.client, eventRow)
    await log_action(interaction.client, eventRow["guild_id"], f"{interaction.user.display_name} is going to **{eventRow['title']}**.")
    warn = conflict_text(eventRow, interaction.user.id)
    await interaction.response.send_message(
        "You're down as going! Press Can't make it if that changes." + ("\n" + warn if warn else ""), ephemeral=True)


class SocialView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Going", style=discord.ButtonStyle.success, custom_id="logi:s_going")
    async def going(self, interaction, button):
        await start_social_going(interaction)

    @discord.ui.button(label="Can't make it", style=discord.ButtonStyle.danger, custom_id="logi:s_out")
    async def out(self, interaction, button):
        await start_withdraw(interaction)


class SignupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Driver", style=discord.ButtonStyle.success, custom_id="logi:driver")
    async def driver(self, interaction, button):
        await start_signup(interaction, "driver")

    @discord.ui.button(label="Passenger", style=discord.ButtonStyle.primary, custom_id="logi:passenger")
    async def passenger(self, interaction, button):
        await start_signup(interaction, "passenger")

    @discord.ui.button(label="Going, no help", style=discord.ButtonStyle.secondary, custom_id="logi:going")
    async def going(self, interaction, button):
        await start_going(interaction)

    @discord.ui.button(label="Edit my details", style=discord.ButtonStyle.secondary, custom_id="logi:edit")
    async def edit(self, interaction, button):
        await start_edit_details(interaction)

    @discord.ui.button(label="Pull out", style=discord.ButtonStyle.danger, custom_id="logi:withdraw")
    async def withdraw(self, interaction, button):
        await start_withdraw(interaction)
