import os, platform, subprocess, time, discord

from core import config, db
from core.util import missing_perms

def git_version() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = [f"{days}d" if days else "", f"{hours}h" if hours or days else "", f"{minutes}m"]
    return " ".join(part for part in parts if part)


def file_size(path: str) -> str:
    try:
        size = os.path.getsize(path)
    except OSError:
        return "unknown"
    return f"{size / 1024:.0f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"


def status_text(bot, guild) -> str:
    now = int(time.time())
    guildId = guild.id
    live = db.q1("SELECT COUNT(*) c FROM events WHERE guild_id=? AND cancelled=0 AND start_ts>?", (guildId, now))["c"]
    signups = db.q1(
        "SELECT COUNT(*) c FROM signups WHERE event_id IN (SELECT id FROM events WHERE guild_id=? AND cancelled=0 AND start_ts>?)",
        (guildId, now),
    )["c"]
    presets = {row["status"]: row["c"] for row in db.q("SELECT status, COUNT(*) c FROM presets WHERE guild_id=? GROUP BY status", (guildId,))}
    auto = db.q1("SELECT COUNT(*) c FROM presets WHERE guild_id=? AND status='ready' AND auto_enabled=1", (guildId,))["c"]
    last_tick = getattr(bot, "last_tick", None)
    gone = missing_perms(guild)
    lines = [
        f"**LogiBot status**",
        f"Version: `{git_version()}` - Python {platform.python_version()} - discord.py {discord.__version__}",
        f"Uptime: {duration(now - getattr(bot, 'started_at', now))} - latency {round(bot.latency * 1000)} ms - {len(bot.guilds)} server(s)",
        f"Scheduler last ran: {f'<t:{last_tick}:R>' if last_tick else 'not yet'}",
        f"Open DM conversations: {len(bot.active)}",
        f"This server: {live} upcoming event(s), {signups} sign-up(s), series: {presets.get('ready', 0)} ready ({auto} auto-posting), {presets.get('draft', 0)} draft, {presets.get('archived', 0)} archived",
        f"Database: {file_size(config.DB_PATH)}",
        "Permissions: all present " if not gone else "Missing permissions: " + ", ".join(gone),
    ]
    return "\n".join(lines)


def event_summary(eventRow) -> str:
    drivers = db.q("SELECT capacity FROM signups WHERE event_id=? AND role='driver'", (eventRow["id"],))
    passengers = db.q1("SELECT COUNT(*) c FROM signups WHERE event_id=? AND role='passenger'", (eventRow["id"],))["c"]
    going = db.q1("SELECT COUNT(*) c FROM signups WHERE event_id=? AND role='going'", (eventRow["id"],))["c"]
    unposted = [
        row for row in db.q("SELECT * FROM assignments WHERE event_id=?", (eventRow["id"],))
        if (row["need"] in ("to", "both") and not row["driver_id"]) or (row["need"] in ("back", "both") and not row["return_id"])
    ]
    parts = [f"**{eventRow['title']}** has happened:"]
    if (eventRow["kind"] or "event") == "social":
        parts.append(f"{going} said they were going.")
    else:
        parts.append(f"{len(drivers)} driver(s) offering {sum(driver['capacity'] or 0 for driver in drivers)} seat(s), {passengers} passenger(s), {going} going without a lift.")
        if unposted:
            parts.append(f"{len(unposted)} still had no lift.")
    return " ".join(parts)
