import time, discord, discord

from core import db
from features.cars import ManageView, cars_embed, close_event
from features.flows import SignupView, SocialView, launch
from features.presets import DraftView, ensure_guide_post, forum_slug, has_schedule
from core.util import event_embed, get_category, get_event, get_thread, is_social, log, notify_log, signup_status

def embed_sig(embed):
    return (embed.title, embed.description, tuple((field.name, field.value) for field in embed.fields))


def message_buttons(message):
    return {comp.custom_id for row in message.components for comp in getattr(row, "children", []) if getattr(comp, "custom_id", None)}


async def sync_message(thread, message, embed, view):
    stale = []
    if message_buttons(message) != ({comp.custom_id for comp in view.children} if view else set()):
        stale.append("buttons")
    if not message.embeds or embed_sig(message.embeds[0]) != embed_sig(embed):
        stale.append("details")
    if not stale:
        return None
    try:
        await message.edit(embed=embed, view=view)
    except discord.HTTPException:
        await thread.edit(archived=False)
        await message.edit(embed=embed, view=view)
    return " & ".join(stale)


async def reconcile_event(bot, guild, eventRow, now, note):
    try:
        thread = await get_thread(bot, eventRow["thread_id"])
    except discord.NotFound:
        db.run("UPDATE events SET cancelled=1, closed=1, reminded=1, preclose_pinged=1 WHERE id=?", (eventRow["id"],))
        return note(eventRow, "its thread was deleted, so I've stopped tracking it")
    social, over = is_social(eventRow), now >= eventRow["start_ts"]

    if social and (not eventRow["closed"] or eventRow["close_ts"] != eventRow["start_ts"]):
        db.run("UPDATE events SET closed=1, preclose_pinged=1, close_ts=start_ts WHERE id=?", (eventRow["id"],))
        note(eventRow, "corrected the social's settings (socials have no cutoff)")
    if over and not (eventRow["closed"] and eventRow["reminded"]):
        db.run("UPDATE events SET closed=1, preclose_pinged=1, reminded=1 WHERE id=?", (eventRow["id"],))
    elif not over and not social and eventRow["closed"] and not eventRow["cars_message_id"]:
        db.run("UPDATE events SET closed=0 WHERE id=?", (eventRow["id"],))
        await close_event(bot, get_event(eventRow["id"]))
        note(eventRow, "the cars were never posted (stopped mid-close), so I sorted and posted them")
    eventRow = get_event(eventRow["id"])

    try:
        message = await thread.fetch_message(thread.id)
    except discord.NotFound:
        note(eventRow, "the original event post is missing - I can't recreate it, you may want to re-post the event")
    else:
        view = None if over else (SocialView() if social else SignupView())
        stale = await sync_message(thread, message, event_embed(eventRow, signup_status(eventRow)), view)
        if stale:
            note(eventRow, f"refreshed the event post ({stale})" + (" and removed its buttons - the event has passed" if over else ""))

    if eventRow["cars_message_id"] and not social:
        guild = bot.get_guild(eventRow["guild_id"])
        try:
            cmsg = await thread.fetch_message(eventRow["cars_message_id"])
        except discord.NotFound:
            if not over:
                new = await thread.send(embed=cars_embed(guild, eventRow), view=ManageView())
                db.run("UPDATE events SET cars_message_id=? WHERE id=?", (new.id, eventRow["id"]))
                note(eventRow, "the cars post had been deleted, so I re-posted it")
        else:
            stale = await sync_message(thread, cmsg, cars_embed(guild, eventRow), None if over else ManageView())
            if stale:
                note(eventRow, f"refreshed the cars post ({stale})")


async def reconcile_preset(bot, guild, preset, note):
    forum = guild.get_channel(preset["forum_id"]) if preset["forum_id"] else None

    if preset["edit_forum_id"]:
        eforum = guild.get_channel(preset["edit_forum_id"])
        alive = False
        if eforum and preset["draft_thread_id"]:
            try:
                await get_thread(bot, preset["draft_thread_id"])
                alive = True
            except discord.NotFound:
                pass
        if not alive:
            if eforum:
                try:
                    await eforum.delete()
                except discord.HTTPException:
                    pass
            db.run("UPDATE presets SET edit_forum_id=NULL, editor_id=NULL, draft_thread_id=NULL WHERE id=?", (preset["id"],))
            note(preset, "an unfinished template edit had lost its private draft, so I cancelled it")

    if preset["status"] == "draft":
        thread = None
        if forum and preset["draft_thread_id"]:
            try:
                thread = await get_thread(bot, preset["draft_thread_id"])
            except discord.NotFound:
                thread = None
        if thread is None:
            if forum:
                try:
                    await forum.delete()
                except discord.HTTPException:
                    pass
            db.run("DELETE FROM presets WHERE id=?", (preset["id"],))
            return note(preset, "its unfinished draft had lost its private post, so I removed the series - create it again if you still want it")
        try:
            message = await thread.fetch_message(thread.id)
        except discord.NotFound:
            return
        if message_buttons(message) != {comp.custom_id for comp in DraftView().children}:
            await message.edit(view=DraftView())
            note(preset, "restored the Finished and Discard buttons on its draft")
        return

    if preset["status"] != "ready":
        return

    if not isinstance(forum, discord.ForumChannel):
        forum = await guild.create_forum(
            forum_slug(preset["name"]), category=await get_category(guild), default_auto_archive_duration=10080,
            topic=f"{preset['name']} - a re-occurring series.",
        )
        db.run("UPDATE presets SET forum_id=? WHERE id=?", (forum.id, preset["id"]))
        note(preset, f"its forum had been deleted, so I made a new one ({forum.mention})")
    elif forum.overwrites_for(guild.default_role).view_channel is False:
        await forum.set_permissions(guild.default_role, overwrite=None)
        note(preset, "its forum was still hidden from members, so I opened it")

    if await ensure_guide_post(forum, preset):
        note(preset, "its pinned guide post was missing, so I posted it again")

    if not db.q1("SELECT 1 FROM events WHERE preset_id=?", (preset["id"],)):
        if has_schedule(preset):
            if not preset["auto_enabled"]:
                db.run("UPDATE presets SET auto_enabled=1 WHERE id=?", (preset["id"],))
                note(preset, "it had never posted anything, so I switched on automatic posting - the first one goes up within a minute")
        elif preset["created_by"] not in bot.active and not db.q1("SELECT 1 FROM sessions WHERE user_id=?", (preset["created_by"],)):
            try:
                user = await bot.fetch_user(preset["created_by"])
                await launch(bot, user, "occurrence", preset["guild_id"], role=str(preset["id"]), intro=f"**{preset['name']}** doesn't have a post yet. Let's post the first one so people can sign up.")
                note(preset, f"it had never posted anything and has no schedule, so I've DM'd {user.display_name} to post the first one")
            except discord.HTTPException:
                note(preset, "it has never posted anything and has no schedule - post one with `/event recurring`")


async def reconcile_events(bot):
    now = int(time.time())
    notes = {}

    def note(eventRow, text):
        notes.setdefault(eventRow["guild_id"], []).append(f"- **{eventRow['title']}**: {text}")

    for eventRow in db.q("SELECT * FROM events WHERE cancelled=0 AND thread_id IS NOT NULL"):
        guild = bot.get_guild(eventRow["guild_id"])
        if not guild:
            continue
        try:
            await reconcile_event(bot, guild, eventRow, now, note)
        except Exception:
            log.exception("restart check failed for event %s", eventRow["id"])
    for preset_row in db.q("SELECT * FROM presets WHERE status IN ('draft', 'ready')"):
        guild = bot.get_guild(preset_row["guild_id"])
        if not guild:
            continue
        try:
            await reconcile_preset(bot, guild, preset_row, lambda preset, text: notes.setdefault(preset["guild_id"], []).append(f"- Series **{preset['name']}**: {text}"))
        except Exception:
            log.exception("restart check failed for series %s", preset_row["id"])
    for guildId, lines in notes.items():
        text = "**Restart check** - I tidied up after restarting:\n" + "\n".join(lines)
        db.remember(guildId, text)
        await notify_log(bot, bot.get_guild(guildId), text, ping=False)
    log.info("restart check done; %s server(s) had fixes", len(notes))
