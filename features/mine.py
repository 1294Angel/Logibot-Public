import time

from core import db
from features.cars import car_text_for

ROLE_NAMES = {"driver": "driving", "passenger": "needing a lift", "going": "going"}


def my_lifts_text(guild, user_id) -> str:
    now = int(time.time())
    rows = db.q(
        "SELECT e.*, s.role AS my_role FROM events e JOIN signups s ON s.event_id=e.id "
        "WHERE s.user_id=? AND e.guild_id=? AND e.cancelled=0 AND e.start_ts>? ORDER BY e.start_ts",
        (user_id, guild.id, now),
    )
    extra = db.q(
        "SELECT e.*, 'passenger' AS my_role FROM events e JOIN assignments a ON a.event_id=e.id "
        "WHERE a.user_id=? AND e.guild_id=? AND e.cancelled=0 AND e.start_ts>? "
        "AND e.id NOT IN (SELECT event_id FROM signups WHERE user_id=?) ORDER BY e.start_ts",
        (user_id, guild.id, now, user_id),
    )
    items = sorted([*rows, *extra], key=lambda row: row["start_ts"])
    if not items:
        return "You aren't signed up for anything coming up here."
    blocks = []
    for eventRow in items:
        head = f"**{eventRow['title']}** - <t:{eventRow['start_ts']}:f> - {ROLE_NAMES.get(eventRow['my_role'], eventRow['my_role'])} (<#{eventRow['thread_id']}>)"
        if eventRow["my_role"] == "going" or (eventRow["kind"] or "event") == "social":
            blocks.append(head)
        elif eventRow["closed"]:
            blocks.append(head + "\n" + car_text_for(guild, eventRow, user_id))
        else:
            blocks.append(head + "\n" + f"Lifts are sorted when sign-ups close <t:{eventRow['close_ts']}:R>.")
    return ("\n" + "\n").join(blocks)[:3900]


def my_details_text(guild_id, user_id) -> str:
    row = db.q1("SELECT * FROM profiles WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    if not row:
        return "I haven't saved any answers for you in this server yet. They're saved when you sign up as a driver or passenger."
    fields = [
        ("Lifts needed", row["need"]), ("Kit", row["kit"]), ("Passenger seats", row["capacity"]),
        ("Pickup address", row["address"]), ("Area", row["area"]),
        ("People you won't take (private)", row["avoid"]), ("People you've organised", row["organised"]),
    ]
    lines = [f"**{label}:** {value}" for label, value in fields if value not in (None, "")]
    lines.append("")
    lines.append("Press **Edit my details** on an event post to change them, or run `/forgetme` to delete them.")
    return "\n".join(["**What I've saved for you in this server** (only you can see this)", *lines])
