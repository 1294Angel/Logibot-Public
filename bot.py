import inspect
import io, logging, discord
from datetime import datetime
from time import time as unix_now
from typing import Literal

from discord import app_commands
from discord.ext import commands, tasks

from core import config, db, settings
from features.cars import ManageView, car_text_for, close_event, preview_for_officers
from features.flows import CancelConfirm, SignupView, SocialView, launch, restore_sessions
from features.presets import (
    DraftView, auto_post_presets, delete_preset, describe_template, expire_drafts, migrate_template_files, describe_schedule, get_preset, has_schedule, occurrence_pairs, post_time, preset_autocomplete,
    ready_presets,
)
from core.calendar_links import ics_text
from features.guide import ensure_officer_guide, help_text
from features.mine import my_details_text, my_lifts_text
from features.status import event_summary, status_text
from features.startup import reconcile_event, reconcile_events
from core.allocation import timeZone
from core.ui import check_clock
from core.util import (
    EVERYONE, command_error, get_action_channel, get_category, get_forum, is_admin, log_action, dm_user, event_by_thread, get_event, get_log_channel, get_thread, is_manager, is_social, log,
    missing_perms, signup_status, sync_log_permissions,
)

async def process_event(bot, eventRow, now):
    if not eventRow["thread_id"] or not bot.get_guild(eventRow["guild_id"]):
        return
    if now >= eventRow["start_ts"]:
        db.run("UPDATE events SET closed=1, preclose_pinged=1, reminded=1 WHERE id=?", (eventRow["id"],))
        return
    if not eventRow["closed"]:
        if not eventRow["preclose_pinged"] and now >= eventRow["close_ts"] - 86400:
            db.run("UPDATE events SET preclose_pinged=1 WHERE id=?", (eventRow["id"],))
            if now < eventRow["close_ts"]:
                thread = await get_thread(bot, eventRow["thread_id"])
                await thread.send(
                    f"@everyone Sign-ups for **{eventRow['title']}** close <t:{eventRow['close_ts']}:R>! "
                    "Press Driver or Passenger above to get a lift sorted.",
                    allowed_mentions=EVERYONE,
                )
                await preview_for_officers(bot, eventRow)
                await log_action(bot, eventRow["guild_id"], f"Posted the sign-up reminder for **{eventRow['title']}** (closes <t:{eventRow['close_ts']}:R>).")
        if now >= eventRow["close_ts"]:
            await close_event(bot, eventRow)
        else:
            return
    eventRow = get_event(eventRow["id"])
    if not eventRow["reminded"] and now >= eventRow["start_ts"] - 86400:
        db.run("UPDATE events SET reminded=1 WHERE id=?", (eventRow["id"],))
        if now < eventRow["start_ts"]:
            guild = bot.get_guild(eventRow["guild_id"])
            userIds = {row["user_id"] for row in db.q("SELECT user_id FROM signups WHERE event_id=?", (eventRow["id"],))}
            userIds |= {row["user_id"] for row in db.q("SELECT user_id FROM assignments WHERE event_id=?", (eventRow["id"],)) if row["user_id"]}
            thread = await get_thread(bot, eventRow["thread_id"])
            mentions = [f"<@{index}>" for index in sorted(userIds)]
            for index in range(0, len(mentions), 40):
                await thread.send(
                    f"**{eventRow['title']}** is <t:{eventRow['start_ts']}:R>! " + " ".join(mentions[index:index + 40]),
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            await log_action(bot, eventRow["guild_id"], f"Sent the 24 hour reminder for **{eventRow['title']}** to {len(userIds)} people.")
            for userId in userIds:
                extra = "" if is_social(eventRow) else f" {car_text_for(guild, eventRow, userId)}"
                await dm_user(bot, userId, f"Reminder: **{eventRow['title']}** is <t:{eventRow['start_ts']}:R>.{extra}")


_last_maintenance = 0
_last_backup = 0


async def summarise_finished(bot, now):
    for eventRow in db.q("SELECT * FROM events WHERE summarised=0 AND cancelled=0 AND start_ts<? AND thread_id IS NOT NULL", (now,)):
        db.run("UPDATE events SET summarised=1 WHERE id=?", (eventRow["id"],))
        await log_action(bot, eventRow["guild_id"], event_summary(eventRow))


async def guarded(label, step, *args):
    try:
        result = step(*args)
        return await result if inspect.isawaitable(result) else result
    except Exception:
        log.exception("%s failed", label)


def run_maintenance(now):
    eventsPurged, profilesPurged = db.purge(now)
    log.info("maintenance: purged %s events, %s profiles", eventsPurged, profilesPurged)


def run_backup():
    log.info("backup written to %s", db.backup())


@tasks.loop(minutes=1)
async def tick():
    global _last_maintenance, _last_backup
    now = int(unix_now())
    for eventRow in db.q("SELECT * FROM events WHERE reminded=0 AND cancelled=0"):
        await guarded(f"scheduler for event {eventRow['id']}", process_event, tick.bot, eventRow, now)
    tick.bot.last_tick = now
    await guarded("summaries", summarise_finished, tick.bot, now)
    await guarded("automatic posting", auto_post_presets, tick.bot, now)
    await guarded("expiring drafts", expire_drafts, tick.bot, now)
    if now - _last_maintenance >= 86400:
        _last_maintenance = now
        await guarded("maintenance", run_maintenance, now)
    if now - _last_backup >= config.BACKUP_EVERY_HOURS * 3600:
        _last_backup = now
        await guarded("backup", run_backup)


@tick.before_loop
async def _wait_ready():
    await tick.bot.wait_until_ready()


class LogiBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.active = set()
        self.started_at = int(unix_now())
        self.last_tick = None
        self._started = False

    async def setup_hook(self):
        self.add_view(SignupView())
        self.add_view(SocialView())
        self.add_view(DraftView())
        self.add_view(ManageView())
        self.tree.error(command_error)
        await self.tree.sync()
        tick.bot = self
        tick.start()

    async def sync_guild(self, guild):
        try:
            for command in (event_group, preset_group, config_group, forgetme, setup_cmd, initialise_cmd, help_cmd, status_cmd, mylifts_cmd, mydetails_cmd):
                self.tree.add_command(command, guild=guild, override=True)
            await self.tree.sync(guild=guild)
        except discord.HTTPException:
            log.exception("couldn't sync commands for %s", guild.name)

    async def on_guild_join(self, guild):
        await self.sync_guild(guild)

    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        for guild in self.guilds:
            await self.sync_guild(guild)
        for guild in self.guilds:
            if settings.get(guild.id)["log_channel"] and not missing_perms(guild):
                try:
                    await get_action_channel(guild)
                except discord.HTTPException:
                    log.warning("couldn't set up the action log for %s", guild.name)
        for guild in self.guilds:
            gone = missing_perms(guild)
            if gone:
                log.warning("%s is missing permissions: %s", guild.name, ", ".join(gone))
        if not self._started:
            self._started = True
            if db.RESTORED_FROM:
                log.warning("the database was missing and was restored from %s", db.RESTORED_FROM)
                for guild in self.guilds:
                    await log_action(self, guild.id, f"The database was missing at startup, so I restored it from `{db.RESTORED_FROM}`. Anything saved after that backup was lost.")
            moved = migrate_template_files()
            if moved:
                log.info("moved %s template file(s) into the database", moved)
            await reconcile_events(self)
            for guild in self.guilds:
                await ensure_officer_guide(self, guild)
            await restore_sessions(self)


event_group = app_commands.Group(name="event", description="Manage lift-share events", guild_only=True)


async def officer_in_event_thread(interaction):
    if not is_manager(interaction.user):
        await interaction.response.send_message("Only officers can do that.", ephemeral=True)
        return None
    eventRow = event_by_thread(interaction.channel_id)
    if not eventRow or eventRow["cancelled"]:
        await interaction.response.send_message("Run this inside an active event's thread.", ephemeral=True)
        return None
    return eventRow


@event_group.command(name="create", description="Create an event - I'll DM you for the details")
async def event_create(interaction: discord.Interaction):
    bot = interaction.client
    if not is_manager(interaction.user):
        return await interaction.response.send_message("Only officers can create events.", ephemeral=True)
    gone = missing_perms(interaction.guild)
    if gone:
        return await interaction.response.send_message("I'm missing permissions here: " + ", ".join(gone), ephemeral=True)
    if interaction.user.id in bot.active:
        return await interaction.response.send_message("Check your DMs - we're already mid-conversation.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        await launch(bot, interaction.user, "create", interaction.guild_id, intro="Let's set up the event. Reply to each question below.")
    except discord.Forbidden:
        return await interaction.followup.send("I can't DM you - enable DMs from server members and retry.", ephemeral=True)
    await interaction.followup.send("Check your DMs ", ephemeral=True)


@event_group.command(name="edit", description="Run in an event's thread: change its details (I'll DM you)")
async def event_edit(interaction: discord.Interaction):
    bot = interaction.client
    eventRow = await officer_in_event_thread(interaction)
    if not eventRow:
        return
    if interaction.user.id in bot.active:
        return await interaction.response.send_message("Check your DMs - we're already mid-conversation.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        await launch(bot, interaction.user, "edit", interaction.guild_id, eventRow["id"], intro=f"Editing **{eventRow['title']}**. Reply `keep` to leave a field as it is.")
    except discord.Forbidden:
        return await interaction.followup.send("I can't DM you - enable DMs from server members and retry.", ephemeral=True)
    await interaction.followup.send("Check your DMs ", ephemeral=True)


@event_group.command(name="cancel", description="Run in an event's thread: cancel the event and tell everyone")
async def event_cancel(interaction: discord.Interaction):
    eventRow = await officer_in_event_thread(interaction)
    if eventRow:
        await interaction.response.send_message(
            f"Cancel **{eventRow['title']}**? Everyone signed up will be told and the lift arrangements dropped.",
            view=CancelConfirm(interaction.client, eventRow, interaction.user), ephemeral=True,
        )


@event_group.command(name="closenow", description="Run in an event's thread: close sign-ups and sort the cars now")
async def event_closenow(interaction: discord.Interaction):
    eventRow = await officer_in_event_thread(interaction)
    if not eventRow:
        return
    if is_social(eventRow):
        return await interaction.response.send_message("Socials have no sign-up cutoff or cars.", ephemeral=True)
    if eventRow["closed"]:
        return await interaction.response.send_message("Sign-ups are already closed.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    await close_event(interaction.client, eventRow)
    await interaction.followup.send("Done.")


@event_group.command(name="list", description="Show the events that are currently open")
async def event_list(interaction: discord.Interaction):
    rows = db.q(
        "SELECT * FROM events WHERE guild_id=? AND cancelled=0 AND start_ts > ? ORDER BY start_ts",
        (interaction.guild_id, int(unix_now())),
    )
    if not rows:
        return await interaction.response.send_message("No upcoming events.", ephemeral=True)
    lines = [
        f"**{event['title']}** - <t:{event['start_ts']}:f>\n<#{event['thread_id']}> - "
        + ("social" if is_social(event) else "sign-ups closed" if event["closed"] else f"closes <t:{event['close_ts']}:R>") + f" - {signup_status(event)}"
        for event in rows
    ]
    await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)


@app_commands.command(name="forgetme", description="Delete the answers I've remembered about you in this server")
@app_commands.guild_only()
async def forgetme(interaction: discord.Interaction):
    guildId, userId = interaction.guild_id, interaction.user.id
    db.run("DELETE FROM profiles WHERE guild_id=? AND user_id=?", (guildId, userId))
    past = [row["id"] for row in db.q("SELECT id FROM events WHERE guild_id=? AND start_ts < ?", (guildId, int(unix_now())))]
    for eventId in past:
        db.run("DELETE FROM signups WHERE event_id=? AND user_id=?", (eventId, userId))
        db.run("DELETE FROM assignments WHERE event_id=? AND user_id=?", (eventId, userId))
    await log_action(interaction.client, guildId, f"{interaction.user.display_name} cleared their remembered answers and past-event records.")
    await interaction.response.send_message(
        "Your remembered answers and your records from past events are deleted. For upcoming events use the "
        "Pull out button (or ask Officership) - I can't drop you from a live lift plan silently.",
        ephemeral=True,
    )


@app_commands.command(name="setup", description="Set this server's officer roles, forum and options")
@app_commands.describe(
    officer_role="A role that can create events, adjust cars and gets pinged on pull-outs",
    officer_role_2="Another officer role", officer_role_3="Another officer role",
    category="Category for the forum and officer channel (created if missing)",
    forum="Forum for events with lifts (created if missing)",
    social_forum="Forum for socials (created if missing)",
    area_matching="Ask for a vague postcode/landmark and group people who live near each other",
)
@app_commands.guild_only()
async def setup_cmd(
    interaction: discord.Interaction, officer_role: discord.Role = None, officer_role_2: discord.Role = None,
    officer_role_3: discord.Role = None, category: str = None, forum: str = None, social_forum: str = None, area_matching: bool = None,
):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Only server admins (Manage Server) can do that.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    roles = {role.id for role in (officer_role, officer_role_2, officer_role_3) if role}
    settings.save(interaction.guild_id, roles=roles or None, category=category, forum=forum, social_forum=social_forum, area_matching=area_matching)
    guildConfig = settings.get(interaction.guild_id)
    gone = missing_perms(interaction.guild)
    notes = []
    if gone:
        notes.append("I'm missing permissions: " + ", ".join(gone))
    else:
        try:
            channel = await get_log_channel(interaction.guild)
            actionChannel = await get_action_channel(interaction.guild)
            await sync_log_permissions(interaction.guild)
            await ensure_officer_guide(interaction.client, interaction.guild)
            notes.append(f"Officer channel: {channel.mention} | Action log: {actionChannel.mention} (both visible to officers only)")
            await log_action(interaction.client, interaction.guild_id, f"{interaction.user.display_name} ran /setup (officer roles: {', '.join(str(role) for role in sorted(guildConfig['roles'])) or 'none'}, area matching {'on' if guildConfig['area_matching'] else 'off'}).")
        except discord.HTTPException as errorText:
            notes.append(f"Couldn't set up the officer channel: {errorText}")
    await interaction.followup.send(
        f"Officer roles: {' '.join(f'<@&{role}>' for role in sorted(guildConfig['roles'])) or '*none (Manage Server only)*'}\n"
        f"Events forum: **{guildConfig['category']} / {guildConfig['forum']}** | Socials forum: **{guildConfig['social_forum']}**\n"
        f"Area matching: **{'on' if guildConfig['area_matching'] else 'off'}**\n" + "\n".join(notes),
        allowed_mentions=discord.AllowedMentions.none(),
    )


@app_commands.command(name="initialise", description="Create the category, event and social forums and the officers' channel")
@app_commands.guild_only()
async def initialise_cmd(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Only server admins (Manage Server) can do that.", ephemeral=True)
    gone = missing_perms(interaction.guild)
    if gone:
        return await interaction.response.send_message("I'm missing permissions: " + ", ".join(gone), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    try:
        category = await get_category(guild)
        events = await get_forum(guild, "event")
        socials = await get_forum(guild, "social")
        officers = await get_log_channel(guild)
        actions = await get_action_channel(guild)
        await sync_log_permissions(guild)
        await ensure_officer_guide(interaction.client, guild)
        await log_action(interaction.client, guild.id, f"{interaction.user.display_name} ran /initialise.")
    except discord.HTTPException as errorText:
        return await interaction.followup.send(f"Something failed part-way: {errorText}. Run it again once that's fixed - it won't duplicate anything.")
    await interaction.followup.send(
        "\n".join([
            f"All set in **{category.name}**:",
            f"{events.mention} (events with lifts)",
            f"{socials.mention} (socials)",
            f"{officers.mention} (officers only, alerts)",
            f"{actions.mention} (officers only, quiet record of actions)",
            "",
            "Next: `/setup` to choose officer roles, then `/event create`. Running this again is safe - existing channels are reused.",
        ])
    )


@event_group.command(name="calendar", description="Run in an event's thread: get a calendar file for it")
async def event_calendar(interaction: discord.Interaction):
    eventRow = event_by_thread(interaction.channel_id)
    if not eventRow or eventRow["cancelled"]:
        return await interaction.response.send_message("Run this inside an event's thread.", ephemeral=True)
    data = io.BytesIO(ics_text(eventRow).encode("utf-8"))
    await interaction.response.send_message(
        "Open this file to add it to your calendar (it's also linked on the post).",
        file=discord.File(data, filename="event.ics"), ephemeral=True,
    )


@event_group.command(name="refresh", description="Run in an event's thread: re-check and repair its posts")
async def event_refresh(interaction: discord.Interaction):
    eventRow = await officer_in_event_thread(interaction)
    if not eventRow:
        return
    await interaction.response.defer(ephemeral=True)
    lines = []
    await reconcile_event(interaction.client, interaction.guild, eventRow, int(unix_now()), lambda event, text: lines.append(f"- {text}"))
    await interaction.followup.send("\n".join(lines) or "Everything was already up to date.")


config_group = app_commands.Group(name="config", description="Admin: manage LogiBot's channels, roles and options", guild_only=True)

TARGETS = Literal["category", "events_forum", "socials_forum", "officers_channel", "action_log"]
TARGET_NAMES = {
    "category": "category", "events_forum": "events forum", "socials_forum": "socials forum",
    "officers_channel": "officers channel", "action_log": "action log",
}


async def admin_only(interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("Only server admins (Manage Server) can do that.", ephemeral=True)
        return False
    return True


def channel_for(guild, target):
    guildConfig = settings.get(guild.id)
    channelId = {
        "category": guildConfig["category_id"], "events_forum": guildConfig["event_forum_id"],
        "socials_forum": guildConfig["social_forum_id"], "officers_channel": guildConfig["log_channel"], "action_log": guildConfig["action_channel"],
    }[target]
    return guild.get_channel(channelId) if channelId else None


@config_group.command(name="show", description="Show where everything is and who the officers are")
async def config_show(interaction: discord.Interaction):
    if not await admin_only(interaction):
        return
    guild, guildConfig = interaction.guild, settings.get(interaction.guild_id)

    def where(target, fallback):
        channel = channel_for(guild, target)
        return channel.mention if channel else f"*not created yet* (will be `{fallback}`)"

    roles = " ".join(f"<@&{role}>" for role in sorted(guildConfig["roles"])) or "*none (Manage Server only)*"
    await interaction.response.send_message(
        "\n".join([
            f"**Officer roles:** {roles}",
            f"**Category:** {where('category', guildConfig['category'])}",
            f"**Events forum:** {where('events_forum', guildConfig['forum'])}",
            f"**Socials forum:** {where('socials_forum', guildConfig['social_forum'])}",
            f"**Officers channel:** {where('officers_channel', config.LOG_CHANNEL_NAME)}",
            f"**Action log:** {where('action_log', config.ACTION_LOG_CHANNEL_NAME)}",
            f"**Area matching:** {'on' if guildConfig['area_matching'] else 'off'}",
        ]),
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )


@config_group.command(name="rename", description="Rename the category, a forum or the officers channel")
@app_commands.describe(target="What to rename", new_name="The new name")
async def config_rename(interaction: discord.Interaction, target: TARGETS, new_name: str):
    if not await admin_only(interaction):
        return
    channel = channel_for(interaction.guild, target)
    if not channel:
        return await interaction.response.send_message("That doesn't exist yet - run /initialise first, or /config link an existing one.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        await channel.edit(name=new_name)
    except discord.HTTPException as errorText:
        return await interaction.followup.send(f"Discord refused: {errorText}")
    saved = {"category": "category", "events_forum": "forum", "socials_forum": "social_forum"}.get(target)
    if saved:
        settings.save(interaction.guild_id, **{saved: channel.name})
    await log_action(interaction.client, interaction.guild_id, f"{interaction.user.display_name} renamed the {TARGET_NAMES[target]} to {channel.name}.")
    await interaction.followup.send(f"Renamed the {TARGET_NAMES[target]} to {channel.mention}.")


@config_group.command(name="move", description="Move a forum or the officers channel to another category, or change its position")
@app_commands.describe(
    target="What to move", category="The category to move it into (leave empty to stay where it is)",
    position="Position in the category, 0 = top",
)
async def config_move(
    interaction: discord.Interaction, target: Literal["events_forum", "socials_forum", "officers_channel", "action_log"],
    category: discord.CategoryChannel = None, position: int = None,
):
    if not await admin_only(interaction):
        return
    channel = channel_for(interaction.guild, target)
    if not channel:
        return await interaction.response.send_message("That doesn't exist yet - run /initialise first, or /config link an existing one.", ephemeral=True)
    if category is None and position is None:
        return await interaction.response.send_message("Give a category, a position, or both.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    kwargs = {}
    if category is not None:
        kwargs["category"] = category
    if position is not None:
        kwargs["position"] = max(0, position)
    try:
        await channel.edit(**kwargs)
    except discord.HTTPException as errorText:
        return await interaction.followup.send(f"Discord refused: {errorText}")
    await log_action(interaction.client, interaction.guild_id, f"{interaction.user.display_name} moved the {TARGET_NAMES[target]} ({channel.name})" + (f" into {category.name}." if category else "."))
    await interaction.followup.send(f"Moved {channel.mention}" + (f" into **{category.name}**." if category else "."))


@config_group.command(name="link", description="Point LogiBot at a channel that already exists instead of its own")
@app_commands.describe(target="What this channel should be used as", channel="The existing channel")
async def config_link(interaction: discord.Interaction, target: TARGETS, channel: discord.ForumChannel | discord.TextChannel | discord.CategoryChannel):
    if not await admin_only(interaction):
        return
    wanted = {
        "category": discord.CategoryChannel, "events_forum": discord.ForumChannel,
        "socials_forum": discord.ForumChannel, "officers_channel": discord.TextChannel, "action_log": discord.TextChannel,
    }[target]
    if not isinstance(channel, wanted):
        return await interaction.response.send_message(f"The {TARGET_NAMES[target]} must be a {wanted.__name__.replace('Channel', '').lower() or 'channel'} channel.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    guildId = interaction.guild_id
    note = ""
    if target == "category":
        settings.save(guildId, category=channel.name, category_id=channel.id)
    elif target == "events_forum":
        settings.save(guildId, forum=channel.name, event_forum_id=channel.id)
    elif target == "socials_forum":
        settings.save(guildId, social_forum=channel.name, social_forum_id=channel.id)
    elif target == "action_log":
        settings.save(guildId, action_channel=channel.id)
        try:
            await sync_log_permissions(interaction.guild)
            note = " I've made it visible to officers only."
        except discord.HTTPException as errorText:
            note = f" I couldn't lock it to officers ({errorText}) - check its permissions."
    else:
        settings.save(guildId, log_channel=channel.id)
        try:
            await sync_log_permissions(interaction.guild)
            await ensure_officer_guide(interaction.client, interaction.guild)
            note = " I've made it visible to officers only and posted the officer guide there."
        except discord.HTTPException as errorText:
            note = f" I couldn't lock it to officers ({errorText}) - check its permissions."
    await log_action(interaction.client, guildId, f"{interaction.user.display_name} linked the {TARGET_NAMES[target]} to {channel.mention}.")
    await interaction.followup.send(f"The {TARGET_NAMES[target]} is now {channel.mention}.{note}")


@config_group.command(name="officer_add", description="Let a role act as officers (create events, change cars, see the officers channel)")
async def config_officer_add(interaction: discord.Interaction, role: discord.Role):
    if not await admin_only(interaction):
        return
    roles = settings.get(interaction.guild_id)["roles"] | {role.id}
    settings.save(interaction.guild_id, roles=roles)
    await interaction.response.defer(ephemeral=True)
    await sync_log_permissions(interaction.guild)
    await log_action(interaction.client, interaction.guild_id, f"{interaction.user.display_name} made the role {role.name} officers.")
    await interaction.followup.send(f"{role.mention} are now officers.", allowed_mentions=discord.AllowedMentions.none())


@config_group.command(name="officer_remove", description="Stop a role acting as officers")
async def config_officer_remove(interaction: discord.Interaction, role: discord.Role):
    if not await admin_only(interaction):
        return
    roles = settings.get(interaction.guild_id)["roles"] - {role.id}
    settings.save(interaction.guild_id, roles=roles)
    await interaction.response.defer(ephemeral=True)
    await sync_log_permissions(interaction.guild)
    await log_action(interaction.client, interaction.guild_id, f"{interaction.user.display_name} removed the role {role.name} from officers.")
    await interaction.followup.send(f"{role.mention} are no longer officers.", allowed_mentions=discord.AllowedMentions.none())


@config_group.command(name="addresses", description="Choose whether pickup addresses show in the cars post or are only sent by DM")
@app_commands.describe(public="True: show addresses in the cars post. False: send them by DM to each car's members only")
async def config_addresses(interaction: discord.Interaction, public: bool):
    if not await admin_only(interaction):
        return
    settings.save(interaction.guild_id, public_addresses=public)
    await log_action(interaction.client, interaction.guild_id, f"{interaction.user.display_name} set pickup addresses to {'public in the cars post' if public else 'DM only'}.")
    await interaction.response.send_message(
        "Pickup addresses will " + ("show in the cars post." if public else "only be sent by DM to the people in each car.") + " The cars post updates the next time it changes.",
        ephemeral=True,
    )


@config_group.command(name="guide", description="Post (or refresh) the pinned officer guide in the officers channel")
async def config_guide(interaction: discord.Interaction):
    if not await admin_only(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    message = await ensure_officer_guide(interaction.client, interaction.guild, force=True)
    if not message:
        return await interaction.followup.send("I couldn't post it - run /initialise first, and check I can send messages in the officers channel.")
    await interaction.followup.send(f"Done: {message.jump_url}")


@config_group.command(name="area_matching", description="Turn grouping by pickup area on or off")
async def config_area(interaction: discord.Interaction, enabled: bool):
    if not await admin_only(interaction):
        return
    settings.save(interaction.guild_id, area_matching=enabled)
    await log_action(interaction.client, interaction.guild_id, f"{interaction.user.display_name} turned area matching {'on' if enabled else 'off'}.")
    await interaction.response.send_message(f"Area matching is now **{'on' if enabled else 'off'}**.", ephemeral=True)


@event_group.command(name="recurring", description="Post a new occurrence of a saved re-occurring event")
@app_commands.describe(preset="Start typing to search your saved presets")
@app_commands.autocomplete(preset=preset_autocomplete)
async def event_recurring(interaction: discord.Interaction, preset: str):
    bot = interaction.client
    if not is_manager(interaction.user):
        return await interaction.response.send_message("Only officers can create events.", ephemeral=True)
    found = get_preset(int(preset)) if preset.isdigit() else None
    if not found or found["guild_id"] != interaction.guild_id or found["status"] != "ready":
        return await interaction.response.send_message("Pick a preset from the list that appears as you type.", ephemeral=True)
    if interaction.user.id in bot.active:
        return await interaction.response.send_message("Check your DMs - we're already mid-conversation.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        await launch(bot, interaction.user, "occurrence", interaction.guild_id, role=str(found["id"]), intro=f"Posting a new **{found['name']}**. Answer each question below.")
    except discord.Forbidden:
        return await interaction.followup.send("I can't DM you - enable DMs from server members and retry.", ephemeral=True)
    await interaction.followup.send("Check your DMs ", ephemeral=True)


preset_group = app_commands.Group(name="preset", description="Manage saved re-occurring event presets", guild_only=True)


@preset_group.command(name="list", description="Show every saved re-occurring event preset")
async def preset_list(interaction: discord.Interaction):
    if not is_manager(interaction.user):
        return await interaction.response.send_message("Only officers can do that.", ephemeral=True)
    rows = ready_presets(interaction.guild_id)
    drafts = db.q("SELECT name FROM presets WHERE guild_id=? AND status='draft'", (interaction.guild_id,))
    archived = db.q("SELECT name FROM presets WHERE guild_id=? AND status='archived'", (interaction.guild_id,))
    if not rows and not drafts and not archived:
        return await interaction.response.send_message("No presets yet. Run `/event create` and choose Re-occurring.", ephemeral=True)
    lines = []
    for row in rows:
        slot = describe_schedule(row)
        auto = "posts itself" if row["auto_enabled"] else "manual"
        lines.append(f"**{row['name']}** - {row['kind']} style, {slot}, {auto}, <#{row['forum_id']}>")
    lines += [f"*{draft['name']}* - draft, not finished" for draft in drafts]
    lines += [f"*{arch['name']}* - archived" for arch in archived]
    await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)


class DeletePresetConfirm(discord.ui.View):
    def __init__(self, preset, guild):
        super().__init__(timeout=120)
        self.preset, self.guild = preset, guild

    async def _do(self, interaction, mode):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Working on it...", view=self)
        self.stop()
        text = await delete_preset(interaction.client, self.guild, self.preset, mode, interaction.user.display_name)
        await interaction.edit_original_response(content=text)

    @discord.ui.button(label="Delete preset and its forum", style=discord.ButtonStyle.danger)
    async def everything(self, interaction, button):
        await self._do(interaction, "all")

    @discord.ui.button(label="Archive forum (keep posts, read-only)", style=discord.ButtonStyle.primary)
    async def archive(self, interaction, button):
        await self._do(interaction, "archive")

    @discord.ui.button(label="Delete preset only (keep forum)", style=discord.ButtonStyle.secondary)
    async def preset_only(self, interaction, button):
        await self._do(interaction, "keep")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Nothing was deleted.", view=self)
        self.stop()


@preset_group.command(name="show", description="Show what's saved in a preset: schedule, extra questions and every template message")
@app_commands.describe(preset="Start typing to search")
@app_commands.autocomplete(preset=preset_autocomplete)
async def preset_show(interaction: discord.Interaction, preset: str):
    if not is_manager(interaction.user):
        return await interaction.response.send_message("Only officers can do that.", ephemeral=True)
    found = get_preset(int(preset)) if preset.isdigit() else None
    if not found or found["guild_id"] != interaction.guild_id:
        return await interaction.response.send_message("Pick a preset from the list that appears as you type.", ephemeral=True)
    markers = ", ".join(db.loads(found["markers"])) or "none"
    head = "\n".join([
        f"**{found['name']}** ({'going + extra questions' if found['kind'] == 'social' else 'lift sign-ups'})",
        f"Repeats: {describe_schedule(found)} - {'posts itself' if found['auto_enabled'] else 'manual'}",
        f"Usual location: {found['location']} - Extra questions: {markers}",
        f"Forum: <#{found['forum_id']}>",
        "",
        "**Saved template** (copied onto every new post):",
    ])
    await interaction.response.send_message((head + "\n" + describe_template(found))[:2000], ephemeral=True)


@preset_group.command(name="delete", description="Delete or archive a saved preset, and optionally its forum")
@app_commands.describe(preset="Start typing to search")
@app_commands.autocomplete(preset=preset_autocomplete)
async def preset_delete(interaction: discord.Interaction, preset: str):
    if not is_manager(interaction.user):
        return await interaction.response.send_message("Only officers can do that.", ephemeral=True)
    found = get_preset(int(preset)) if preset.isdigit() else None
    if not found or found["guild_id"] != interaction.guild_id:
        return await interaction.response.send_message("Pick a preset from the list that appears as you type.", ephemeral=True)
    soon = db.q1("SELECT COUNT(*) c FROM events WHERE preset_id=? AND cancelled=0 AND start_ts>?", (found["id"], int(unix_now())))["c"]
    await interaction.response.send_message(
        f"What should happen to **{found['name']}**? It has {soon} upcoming post(s)." + "\n"
        + "- **Delete preset and its forum**: removes the template and the whole forum with every post in it. This can't be undone." + "\n"
        + "- **Archive**: the forum becomes read-only, every post is closed, and nothing new is posted. The posts stay readable." + "\n"
        + "- **Delete preset only**: removes the template but leaves the forum and its posts alone.",
        view=DeletePresetConfirm(found, interaction.guild), ephemeral=True,
    )


@preset_group.command(name="auto", description="Turn automatic posting of a preset on or off")
@app_commands.describe(preset="Start typing to search", enabled="Post each one automatically, ahead of time")
@app_commands.autocomplete(preset=preset_autocomplete)
async def preset_auto(interaction: discord.Interaction, preset: str, enabled: bool):
    if not is_manager(interaction.user):
        return await interaction.response.send_message("Only officers can do that.", ephemeral=True)
    found = get_preset(int(preset)) if preset.isdigit() else None
    if not found or found["guild_id"] != interaction.guild_id:
        return await interaction.response.send_message("Pick a preset from the list that appears as you type.", ephemeral=True)
    if enabled and not has_schedule(found):
        return await interaction.response.send_message("This preset has no repeat schedule yet - set one with `/preset edit` first.", ephemeral=True)
    db.run("UPDATE presets SET auto_enabled=? WHERE id=?", (int(enabled), found["id"]))
    await log_action(interaction.client, interaction.guild_id, f"{interaction.user.display_name} turned automatic posting {'on' if enabled else 'off'} for **{found['name']}**.")
    if not enabled:
        return await interaction.response.send_message(f"**{found['name']}** will no longer post automatically.", ephemeral=True)
    now = datetime.now(timeZone)
    lines = []
    for prev, occurrence in occurrence_pairs(get_preset(found["id"]), now, 3):
        due = post_time(found, prev)
        when = "straight away" if due is None or due <= now else f"<t:{int(due.timestamp())}:f>"
        lines.append(f"- {occurrence:%a %d %b %H:%M} - posted {when}")
    await interaction.response.send_message(
        f"**{found['name']}** repeats {describe_schedule(found)}. Weekly series post on the Monday of the previous one's week; "
        "other repeats post the morning after the previous one." + "\n" + "\n".join(lines), ephemeral=True)


@app_commands.command(name="status", description="Show how the bot is doing: uptime, scheduler, permissions, counts")
@app_commands.guild_only()
async def status_cmd(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Only server admins (Manage Server) can do that.", ephemeral=True)
    await interaction.response.send_message(status_text(interaction.client, interaction.guild), ephemeral=True)


@app_commands.command(name="mylifts", description="See what you're signed up for here, and which car you're in")
@app_commands.guild_only()
async def mylifts_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(my_lifts_text(interaction.guild, interaction.user.id), ephemeral=True)


@app_commands.command(name="mydetails", description="See the answers I've saved for you in this server")
@app_commands.guild_only()
async def mydetails_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(my_details_text(interaction.guild_id, interaction.user.id), ephemeral=True)


@app_commands.command(name="help", description="List every LogiBot command and what the buttons do")
async def help_cmd(interaction: discord.Interaction):
    cmds = interaction.client.tree.get_commands(guild=interaction.guild) if interaction.guild else interaction.client.tree.get_commands()
    await interaction.response.send_message(embed=discord.Embed(description=help_text(cmds)[:4000], colour=discord.Colour.blurple()), ephemeral=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    LogiBot().run(config.TOKEN, log_handler=None)
