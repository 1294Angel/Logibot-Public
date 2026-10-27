import hashlib, json, discord

from discord import app_commands

from core import settings
from core.util import log

AUDIENCE = {
    "event create": "Officers", "event edit": "Officers", "event cancel": "Officers", "event closenow": "Officers",
    "event refresh": "Officers", "event recurring": "Officers", "preset": "Officers", "status": "Admins", "setup": "Admins", "initialise": "Admins", "config": "Admins",
}
SECTIONS = ("Everyone", "Officers", "Admins")

BUTTON_HELP = [
    "**Driver / Passenger** - sign up for lifts; I'll DM you a few questions. Press again to edit your answers.",
    "**Going, no help** - you're attending and don't need a lift. If a driver is taking you, ask them to add your name or ID to their sign-up.",
    "**Edit my details** - change your answers any time. After the cutoff, anyone it affects is told.",
    "**Pull out** - drop out. You can't be both a driver and a passenger; pull out, then sign up again.",
    "**Going / Can't make it** - the buttons on socials.",
]


def command_entries(commands):
    entries = {section: [] for section in SECTIONS}
    for command in commands:
        subs = command.commands if isinstance(command, app_commands.Group) else [command]
        for subCommand in subs:
            full = f"{command.name} {subCommand.name}" if isinstance(command, app_commands.Group) else command.name
            level = AUDIENCE.get(full) or AUDIENCE.get(command.name) or "Everyone"
            entries[level].append((full, subCommand.description))
    return entries


def help_text(commands) -> str:
    entries = command_entries(commands)
    lines = ["**LogiBot commands**"]
    for section in SECTIONS:
        if entries[section]:
            lines.append(f"__{section}__")
            lines += [f"`/{name}` - {desc}" for name, desc in sorted(entries[section])]
    lines += ["", "__On event and social posts__"] + [f"- {line}" for line in BUTTON_HELP]
    return "\n".join(lines)


def officer_command_text(commands, room) -> str:
    entries = command_entries(commands)
    lines = ["**Everyone can use:** " + ", ".join(f"`/{cmd_name}`" for cmd_name, _ in sorted(entries["Everyone"])) + " (run `/help` for what each does)"]
    for section in ("Officers", "Admins"):
        if entries[section]:
            lines += ["", f"__{section}__"]
            lines += [f"`/{name}` - {desc[:70]}" for name, desc in sorted(entries[section])]
    text = "\n".join(lines)
    if len(text) > room:
        text = text[: room - 40].rsplit("\n", 1)[0] + "\n" + "...and more - see `/help`."
    return text


def build_embeds(commands):
    newline = "\n"
    overview = discord.Embed(
        title="LogiBot - officer guide", colour=discord.Colour.blurple(),
        description=(
            "This channel is only visible to officers. It's where I tell you what needs attention. "
            "Messages **with a ping** need someone to act; messages without a ping are just for information. The separate **action log** channel is a quiet record of everything that was done and who did it - it never pings."
        ),
    )
    overview.add_field(name="Creating things", inline=False, value=newline.join([
        "`/event create` - I'll DM you the questions. Everything is a dropdown or a button.",
        "- **Event** - lifts organised: drivers, passengers, cars and a sign-up cutoff.",
        "- **Social** - a Going button plus extra questions you choose (e.g. time of arrival).",
        "- **Re-occurring** - a repeating series (e.g. weekly training) with its own forum and a saved template of statements and polls.",
        "By default sign-ups close at **9AM on the Monday before**; you can change that any time.",
    ]))
    overview.add_field(name="How lifts work", inline=False, value=newline.join([
        "- People press **Driver** or **Passenger** and answer my DMs. Drivers can name people they've already organised, and people they won't take (kept private).",
        "- Nobody is put in a car until the cutoff. Then I sort everyone, keep each person in the **same car both ways** where I can, and post the cars.",
        "- 24 hours before the cutoff I ping everyone and send you a preview of anyone without a lift or split between two cars.",
    ]))
    overview.add_field(name="Running the cars post", inline=False, value=newline.join([
        "Buttons under the cars post (officers only): **Move** (one trip or both), **Swap**, **Add person** (even if they never signed up) and **Remove person**.",
        "Moving someone into a car whose driver won't take them is refused - I won't say why. I DM everyone a change affects.",
    ]))
    overview.add_field(name="What you'll see here", inline=False, value=newline.join([
        "- A driver pulled out (ping): I re-sort their passengers and list who has no space.",
        "- A passenger pulled out (no ping).",
        "- Someone was split between two cars, or has no lift (ping).",
        "- A late sign-up after the cutoff (ping).",
        "- Restart check: I repaired stale posts (no ping).",
        "- I auto-posted a re-occurring event (no ping).",
    ]))
    tips = discord.Embed(title="Re-occurring series and tips", colour=discord.Colour.blurple(), description=newline.join([
        "**New series:** `/event create` - Re-occurring - New. I make a hidden forum and a draft post. Add statements, rules, images and **polls** there, then press **Finished**. I then DM you to post the first one.",
        "**More posts:** a series with a repeat schedule posts itself (weekly ones on the Monday of the previous one's week, others the morning after the previous one) - switch that off with `/preset auto`. For anything else use `/event recurring` (type to search).",
        "**Checking one:** `/preset show` lists everything saved in a series: schedule, extra questions and every template message, poll and file.",
        "**Changing one:** `/preset edit` changes the details, or opens a private copy of the template. Posts already made are never changed.",
        "**Removing one:** `/preset delete` can delete the series and its forum, archive the forum (read-only, posts kept), or delete only the preset.",
        "",
        "**Tips**",
        "- `/event refresh` in an event's thread repairs its posts. On every restart I also check each series and repair anything broken (missing forum, hidden forum, missing guide post, a series that never posted).",
        "- Private drafts for new series and template edits are deleted automatically if they aren't finished within 48 hours.",
        "- Renaming or moving my channels is safe: `/config rename`, `/config move`, `/config link`.",
        "- Members can use `/help`.",
        "- This guide updates itself when I'm updated, and `/config guide` re-posts it if you ever delete it.",
    ]))
    room = 5800 - len(overview) - len(tips) - len("Commands")
    cmds = discord.Embed(title="Commands", description=officer_command_text(commands, max(room, 400)), colour=discord.Colour.blurple())
    return [overview, cmds, tips]


def digest(embeds) -> str:
    return hashlib.sha256(json.dumps([embed.to_dict() for embed in embeds], sort_keys=True).encode()).hexdigest()


async def ensure_officer_guide(bot, guild, force=False):
    guildConfig = settings.get(guild.id)
    channel = guild.get_channel(guildConfig["log_channel"]) if guildConfig["log_channel"] else None
    if channel is None:
        return None
    embeds = build_embeds(bot.tree.get_commands(guild=guild))
    current = digest(embeds)
    message = None
    if guildConfig["guide_id"]:
        try:
            message = await channel.fetch_message(guildConfig["guide_id"])
        except discord.NotFound:
            message = None
    if message and guildConfig["guide_hash"] == current and not force:
        return message
    try:
        if message:
            await message.edit(embeds=embeds)
        else:
            message = await channel.send(embeds=embeds)
            try:
                await message.pin()
            except discord.HTTPException:
                await channel.send("I couldn't pin the guide above. Give me the **Pin Messages** (or Manage Messages) permission, then run `/config guide` to pin it.")
    except discord.HTTPException:
        log.exception("couldn't post the officer guide in %s", guild.name)
        return None
    settings.save(guild.id, guide_id=message.id, guide_hash=current)
    return message
