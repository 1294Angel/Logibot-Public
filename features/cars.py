import discord

from core import config, db, settings
from core.allocation import LEGS, allocate, avoids, choose_cars, is_mixed, lacks_lift, resolve_entries
from core.util import (
    NOTIFY, QUIET, dm_user, event_by_thread, get_event, get_thread, is_manager, name_of,
    notify_officers, refresh_event_post, log, log_action,
)

BLOCKED = "This user cannot be moved into this car."
LEG_NAMES = {"to": "to the event", "back": "back from the event"}
NEED_NAMES = {"to": "a lift there", "back": "a lift back", "both": "lifts both ways"}


def needed(row):
    return LEGS[row["need"]]


def leg_car(row, leg):
    return row["driver_id"] if leg == "to" else row["return_id"]


def missing_legs(row):
    return [leg for leg in needed(row) if not leg_car(row, leg)]


def is_split(row):
    return is_mixed({"need": row["need"], "to": row["driver_id"], "back": row["return_id"]})


def get_row(row_id):
    return db.q1("SELECT * FROM assignments WHERE id=?", (row_id,))


def set_leg(row_id, leg, car):
    column = "driver_id" if leg == "to" else "return_id"
    db.run(f"UPDATE assignments SET {column}=? WHERE id=?", (car, row_id))


def seat_use(event_id, driverId, leg):
    column = "driver_id" if leg == "to" else "return_id"
    return db.q1(
        f"SELECT COUNT(*) c FROM assignments WHERE event_id=? AND {column}=? AND need IN ('both', ?)",
        (event_id, driverId, leg),
    )["c"]


def drivers_state(eventRow, exclude=()):
    drivers, used = {}, {}
    for row in db.q("SELECT * FROM signups WHERE event_id=? AND role='driver'", (eventRow["id"],)):
        userId = row["user_id"]
        if userId in exclude:
            continue
        drivers[userId] = {"capacity": row["capacity"] or 0, "avoid": db.loads(row["avoid"]), "area": row["area"]}
        used[userId] = {"to": seat_use(eventRow["id"], userId, "to"), "back": seat_use(eventRow["id"], userId, "back")}
    return drivers, used


def auto_place(eventRow, row, legs, exclude=()):
    drivers, used = drivers_state(eventRow, exclude)
    kept = [leg for leg in needed(row) if leg not in legs]
    prefer = leg_car(row, kept[0]) if kept else None
    person = {"user_id": row["user_id"], "label": row["label"], "need": row["need"], "area": row["area"]}
    to, back = choose_cars(drivers, used, person, settings.get(eventRow["guild_id"])["area_matching"], legs=legs, prefer=prefer)
    if "to" in legs and to:
        set_leg(row["id"], "to", to)
    if "back" in legs and back:
        set_leg(row["id"], "back", back)
    return get_row(row["id"])


def capacity_warning(guild, eventRow, dests):
    out = ""
    for driverId in {dest for dest in dests if dest}:
        signup = db.q1("SELECT capacity FROM signups WHERE event_id=? AND user_id=?", (eventRow["id"], driverId))
        if signup and any(seat_use(eventRow["id"], driverId, leg) > signup["capacity"] for leg in ("to", "back")):
            out += f"\n{name_of(guild, driverId)}'s car is now over its stated capacity ({signup['capacity']})."
    return out


def blocked(eventRow, row, dest):
    if not dest:
        return False
    driver = db.q1("SELECT avoid FROM signups WHERE event_id=? AND user_id=?", (eventRow["id"], dest))
    return bool(driver) and avoids(db.loads(driver["avoid"]), row["user_id"], row["label"])


def sort_cars(guild, eventRow):
    rows = db.q("SELECT * FROM signups WHERE event_id=?", (eventRow["id"],))
    drivers = [
        {"user_id": row["user_id"], "capacity": row["capacity"] or 0, "avoid": db.loads(row["avoid"]),
         "organised": db.loads(row["organised"]), "area": row["area"]}
        for row in rows if row["role"] == "driver"
    ]
    passengers = [
        {"user_id": row["user_id"], "label": name_of(guild, row["user_id"]), "kit": row["kit"] or "",
         "need": row["need"] or "both", "area": row["area"]}
        for row in rows if row["role"] == "passenger"
    ]
    places = allocate(drivers, passengers, settings.get(eventRow["guild_id"])["area_matching"])
    for person in places:
        if person["user_id"]:
            person["label"] = name_of(guild, person["user_id"], person["label"])
    return places


def list_problems(places):
    split = [person["label"] for person in places if is_mixed(person)]
    lost = [person["label"] for person in places if lacks_lift(person)]
    out = []
    if split:
        out.append(f"Split between two cars (to/back): **{', '.join(split)}**")
    if lost:
        out.append(f"No lift yet: **{', '.join(lost)}**")
    return "\n".join(out)


async def preview_for_officers(bot, eventRow):
    guild = bot.get_guild(eventRow["guild_id"])
    issues = list_problems(sort_cars(guild, eventRow))
    body = issues or "Everyone currently fits in a car, same car both ways."
    await notify_officers(bot, eventRow, f"Sign-ups close <t:{eventRow['close_ts']}:R>. If they closed right now:\n{body}")


async def close_event(bot, eventRow):
    guild = bot.get_guild(eventRow["guild_id"])
    if not db.claim("UPDATE events SET closed=1 WHERE id=? AND closed=0", (eventRow["id"],)):
        return
    places = sort_cars(guild, eventRow)
    db.run("DELETE FROM assignments WHERE event_id=?", (eventRow["id"],))
    for person in places:
        db.run(
            "INSERT INTO assignments (event_id, user_id, label, kit, driver_id, return_id, need, area, locked) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (eventRow["id"], person["user_id"], person["label"], person["kit"], person["to"], person["back"], person["need"], person["area"], person["locked"]),
        )
    eventRow = get_event(eventRow["id"])
    issues = list_problems(places)
    thread = await get_thread(bot, eventRow["thread_id"])
    note = "Some people need a lift - Officership have been told." if any(lacks_lift(person) for person in places) else "Everyone has a lift."
    message = await thread.send(
        f"**Sign-ups closed - lifts are sorted!** {note} Officers can adjust with the buttons below.",
        embed=cars_embed(guild, eventRow), view=ManageView(),
    )
    db.run("UPDATE events SET cars_message_id=? WHERE id=?", (message.id, eventRow["id"]))
    if issues:
        await notify_officers(bot, eventRow, f"Cars are sorted but need your attention:\n{issues}")
    userIds = {row["user_id"] for row in db.q("SELECT user_id FROM signups WHERE event_id=? AND role!='going'", (eventRow["id"],))}
    userIds |= {row["user_id"] for row in db.q("SELECT user_id FROM assignments WHERE event_id=?", (eventRow["id"],)) if row["user_id"]}
    for userId in userIds:
        await dm_user(bot, userId, f"Lifts for **{eventRow['title']}** are sorted. {car_text_for(guild, eventRow, userId)} See {thread.mention}.")
    driverCount = db.q1("SELECT COUNT(*) c FROM signups WHERE event_id=? AND role='driver'", (eventRow["id"],))["c"]
    warn = " Needs attention: " + issues.replace("\n", "; ") if issues else ""
    await log_action(bot, eventRow["guild_id"], f"Sign-ups closed and cars sorted for **{eventRow['title']}**: {driverCount} driver(s), {len(places)} passenger(s).{warn}")
    await refresh_event_post(bot, eventRow)


def who_line(row):
    who = f"<@{row['user_id']}>" if row["user_id"] else row["label"]
    return f"- {who}" + (f" - {row['kit']}" if row["kit"] else "")


def trip_tag(row, driverId):
    if row["need"] == "to":
        return " *(to only)*"
    if row["need"] == "back":
        return " *(back only)*"
    if row["driver_id"] == row["return_id"]:
        return ""
    return " *(to only here)*" if row["driver_id"] == driverId else " *(back only here)*"


def cars_embed(guild, eventRow):
    drivers = db.q("SELECT * FROM signups WHERE event_id=? AND role='driver'", (eventRow["id"],))
    rows = db.q("SELECT * FROM assignments WHERE event_id=? ORDER BY id", (eventRow["id"],))
    parts = [] if drivers else ["*No drivers signed up.*"]
    public_addresses = settings.get(eventRow["guild_id"])["public_addresses"]
    if drivers and not public_addresses:
        parts.append("*Pickup addresses are sent by DM to the people in each car.*")
    for driver in drivers:
        userId, seatCapacity = driver["user_id"], driver["capacity"]
        mine = [row for row in rows if (row["driver_id"] == userId and "to" in needed(row)) or (row["return_id"] == userId and "back" in needed(row))]
        to_n, back_n = seat_use(eventRow["id"], userId, "to"), seat_use(eventRow["id"], userId, "back")
        seats = f"{to_n}/{seatCapacity} seats" if to_n == back_n else f"to {to_n}/{seatCapacity} - back {back_n}/{seatCapacity} seats"
        warn = " over capacity" if max(to_n, back_n) > seatCapacity else ""
        lines = [f"**<@{userId}>** - {seats}{warn}"]
        if public_addresses:
            lines.append(f"{driver['address']}")
        lines += [who_line(row) + trip_tag(row, userId) for row in mine] or ["*No passengers yet*"]
        parts.append("\n".join(lines))
    lost = [row for row in rows if missing_legs(row)]
    if lost:
        parts.append("**Needs a lift (waiting list)**\n" + "\n".join(
            who_line(row) + f" *({' & '.join(LEG_NAMES[leg] for leg in missing_legs(row))})*" for row in lost))
    return discord.Embed(title=f"Cars for {eventRow['title']}", description="\n\n".join(parts)[:4000], colour=discord.Colour.gold())


def car_text_for(guild, eventRow, user_id):
    driver = db.q1("SELECT * FROM signups WHERE event_id=? AND user_id=? AND role='driver'", (eventRow["id"], user_id))
    if driver:
        mine = db.q(
            "SELECT * FROM assignments WHERE event_id=? AND ((driver_id=? AND need IN ('to','both')) OR (return_id=? AND need IN ('back','both')))",
            (eventRow["id"], user_id, user_id),
        )
        who = ", ".join(row["label"] + trip_tag(row, user_id).replace("*", "") for row in mine) or "nobody yet"
        return f"You're driving. Passengers: **{who}**."
    rows = db.q("SELECT * FROM assignments WHERE event_id=? AND user_id=?", (eventRow["id"], user_id))
    if not rows:
        going = db.q1("SELECT 1 FROM signups WHERE event_id=? AND user_id=? AND role='going'", (eventRow["id"], user_id))
        return "You're down as going under your own steam." if going else "You aren't in a car."
    row = rows[0]

    def car(driverId):
        signup = db.q1("SELECT address FROM signups WHERE event_id=? AND user_id=?", (eventRow["id"], driverId))
        return f"**{name_of(guild, driverId)}**'s car (pickup: {signup['address'] if signup else 'ask them'})"

    if row["need"] == "both" and row["driver_id"] and row["driver_id"] == row["return_id"]:
        return f"You're in {car(row['driver_id'])}, both ways. Sort fuel, timings and kit with them directly."
    lines = []
    for leg in needed(row):
        driverId = leg_car(row, leg)
        lines.append(f"{LEG_NAMES[leg].capitalize()}: " + (car(driverId) if driverId else "no lift yet - Officership have been told"))
    return "\n".join(lines)


async def refresh_cars_post(bot, eventRow):
    eventRow = get_event(eventRow["id"])
    if not eventRow["cars_message_id"]:
        return
    guild = bot.get_guild(eventRow["guild_id"])
    thread = await get_thread(bot, eventRow["thread_id"])
    message = await thread.fetch_message(eventRow["cars_message_id"])
    await message.edit(embed=cars_embed(guild, eventRow))


async def tell_everyone_affected(bot, eventRow, user_ids, prefix="Update for"):
    guild = bot.get_guild(eventRow["guild_id"])
    for userId in {user for user in user_ids if user}:
        await dm_user(bot, userId, f"{prefix} **{eventRow['title']}**: {car_text_for(guild, eventRow, userId)}")


async def fill_waitlist(bot, eventRow):
    eventRow = get_event(eventRow["id"])
    if not eventRow["closed"] or eventRow["cancelled"]:
        return
    guild = bot.get_guild(eventRow["guild_id"])
    thread = await get_thread(bot, eventRow["thread_id"])
    for row in db.q("SELECT * FROM assignments WHERE event_id=? ORDER BY id", (eventRow["id"],)):
        legs = missing_legs(row)
        if not legs:
            continue
        placed = auto_place(eventRow, row, legs)
        got = [leg for leg in legs if leg_car(placed, leg)]
        if not got:
            continue
        await tell_everyone_affected(bot, eventRow, [row["user_id"], *[leg_car(placed, leg) for leg in got]], "A seat came up for")
        if is_split(placed):
            await notify_officers(bot, eventRow, f"**{row['label']}** had to be split between two cars (to/back).")
    await refresh_cars_post(bot, eventRow)


async def late_join(bot, eventRow, user, role):
    eventRow = get_event(eventRow["id"])
    guild = bot.get_guild(eventRow["guild_id"])
    sign = db.q1("SELECT * FROM signups WHERE event_id=? AND user_id=?", (eventRow["id"], user.id))
    if role == "driver":
        db.run("DELETE FROM assignments WHERE event_id=? AND user_id=?", (eventRow["id"], user.id))
        await notify_officers(bot, eventRow, f"Late sign-up: **{user.display_name}** is now driving ({sign['capacity']} seats).")
        await log_action(bot, eventRow["guild_id"], f"{user.display_name} joined late as a driver ({sign['capacity']} seats) for **{eventRow['title']}**.")
        await fill_waitlist(bot, eventRow)
        return
    db.run("DELETE FROM assignments WHERE event_id=? AND user_id=?", (eventRow["id"], user.id))
    rowId = db.run(
        "INSERT INTO assignments (event_id, user_id, label, kit, need, area, locked) VALUES (?,?,?,?,?,?,0)",
        (eventRow["id"], user.id, user.display_name, sign["kit"] or "", sign["need"] or "both", sign["area"]),
    )
    row = auto_place(eventRow, get_row(rowId), list(needed(get_row(rowId))))
    status = "no space - on the waiting list" if missing_legs(row) else "placed automatically"
    extra = " Split between two cars." if is_split(row) else ""
    await notify_officers(bot, eventRow, f"Late sign-up: **{user.display_name}** ({NEED_NAMES[row['need']]}) - {status}.{extra}")
    await log_action(bot, eventRow["guild_id"], f"{user.display_name} joined late as a passenger for **{eventRow['title']}** - {status}.{extra}")
    await dm_user(bot, user.id, f"{car_text_for(guild, eventRow, user.id)}")
    await tell_everyone_affected(bot, eventRow, [leg_car(row, leg) for leg in needed(row)])
    await refresh_cars_post(bot, eventRow)


class WithdrawConfirm(discord.ui.View):
    def __init__(self, bot, eventRow, user, reason):
        super().__init__(timeout=120)
        self.bot, self.eventRow, self.user, self.reason = bot, eventRow, user, reason

    @discord.ui.button(label="Yes, I'm sure", style=discord.ButtonStyle.danger)
    async def go(self, interaction, button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Working on it...", view=self)
        self.stop()
        try:
            await withdraw(self.bot, self.eventRow, self.user, self.reason)
            await interaction.edit_original_response(content="Done. Officership have been told.")
        except Exception:
            log.exception("withdraw failed")
            await interaction.edit_original_response(content="Something went wrong - please tell Officership directly.")


async def start_withdraw(interaction: discord.Interaction, reason="out"):
    eventRow = event_by_thread(interaction.channel_id)
    userId = interaction.user.id
    if not eventRow or eventRow["cancelled"]:
        return await interaction.response.send_message("I don't recognise an active event here.", ephemeral=True)
    sign = db.q1("SELECT role FROM signups WHERE event_id=? AND user_id=?", (eventRow["id"], userId))
    if not sign and not db.q1("SELECT 1 FROM assignments WHERE event_id=? AND user_id=?", (eventRow["id"], userId)):
        return await interaction.response.send_message("You aren't signed up for this event.", ephemeral=True)
    if reason == "own" and sign and sign["role"] == "driver":
        return await interaction.response.send_message("That button is for passengers - use Pull out if you can't drive any more.", ephemeral=True)
    if reason == "own" and sign and sign["role"] == "going":
        return await interaction.response.send_message("You're already down as going without a lift.", ephemeral=True)
    text = "Pull out of this event? Everyone affected will be told." if reason == "out" else "Take yourself off the lift list because you've sorted your own transport?"
    await interaction.response.send_message(text, view=WithdrawConfirm(interaction.client, eventRow, interaction.user, reason), ephemeral=True)


async def withdraw(bot, eventRow, user, reason="out"):
    eventRow = get_event(eventRow["id"])
    guild = bot.get_guild(eventRow["guild_id"])
    userId, who = user.id, user.display_name
    sign = db.q1("SELECT * FROM signups WHERE event_id=? AND user_id=?", (eventRow["id"], userId))

    if sign and sign["role"] == "driver":
        db.run("DELETE FROM signups WHERE event_id=? AND user_id=?", (eventRow["id"], userId))
        report = [f"**{who}** pulled out as a **driver**."]
        if eventRow["closed"]:
            moved, stuck, split, newc = [], [], [], set()
            rows = db.q(
                "SELECT * FROM assignments WHERE event_id=? AND ((driver_id=? AND need IN ('to','both')) OR (return_id=? AND need IN ('back','both')))",
                (eventRow["id"], userId, userId),
            )
            for row in rows:
                legs = [leg for leg in needed(row) if leg_car(row, leg) == userId]
                for leg in legs:
                    set_leg(row["id"], leg, None)
                placed = auto_place(eventRow, get_row(row["id"]), legs, exclude=(userId,))
                newc |= {leg_car(placed, leg) for leg in legs if leg_car(placed, leg)}
                if any(leg_car(placed, leg) for leg in legs) and not missing_legs(placed):
                    moved.append(f"{row['label']} - {', '.join(name_of(guild, leg_car(placed, leg)) for leg in legs)}")
                if missing_legs(placed):
                    stuck.append(row["label"])
                if is_split(placed):
                    split.append(row["label"])
                if row["user_id"]:
                    tail = car_text_for(guild, eventRow, row["user_id"]) if not missing_legs(placed) else (
                        "There's no space in another car right now - Officership have been told and will sort you a lift.")
                    await dm_user(bot, row["user_id"], f"**{who}** pulled out as a driver for **{eventRow['title']}**. {tail}")
            await tell_everyone_affected(bot, eventRow, newc, "A passenger was moved into your car for")
            if moved:
                report.append("Re-sorted: " + "; ".join(moved))
            if stuck:
                report.append(f"No space for: **{', '.join(stuck)}** - they need a lift sorting manually.")
            if split:
                report.append(f"Had to be split between two cars: **{', '.join(split)}**")
        await notify_officers(bot, eventRow, "\n".join(report), ping=True)
    else:
        mine = db.q("SELECT * FROM assignments WHERE event_id=? AND user_id=?", (eventRow["id"], userId))
        db.run("DELETE FROM signups WHERE event_id=? AND user_id=?", (eventRow["id"], userId))
        db.run("DELETE FROM assignments WHERE event_id=? AND user_id=?", (eventRow["id"], userId))
        drivers = {car for mine_row in mine for car in (mine_row["driver_id"], mine_row["return_id"]) if car}
        if reason == "own":
            verb = "doesn't need a lift any more but is still going"
        elif sign and sign["role"] == "going":
            verb = "is no longer going"
        else:
            verb = "pulled out as a passenger"
        for driver in drivers:
            await dm_user(bot, driver, f"**{who}** {verb} (**{eventRow['title']}**). That frees a seat in your car.")
        if drivers or not (sign and sign["role"] == "going"):
            await notify_officers(bot, eventRow, f"**{who}** {verb}.", ping=False)
        if reason == "own":
            db.run("INSERT OR REPLACE INTO signups (event_id, user_id, role, need) VALUES (?,?,'going','both')", (eventRow["id"], userId))
            if eventRow["closed"]:
                for driver in db.q("SELECT user_id, organised FROM signups WHERE event_id=? AND role='driver'", (eventRow["id"],)):
                    if any(entry["id"] == userId for entry in db.loads(driver["organised"])):
                        db.run(
                            "INSERT INTO assignments (event_id, user_id, label, kit, driver_id, return_id, need, locked) VALUES (?,?,?,?,?,?,'both',1)",
                            (eventRow["id"], userId, who, "", driver["user_id"], driver["user_id"]),
                        )
                        break
        await fill_waitlist(bot, eventRow)
    what = "stopped needing a lift but is still going" if reason == "own" else ("pulled out as a driver" if sign and sign["role"] == "driver" else ("is no longer going" if sign and sign["role"] == "going" else "pulled out as a passenger"))
    await log_action(bot, eventRow["guild_id"], f"{who} {what} - **{eventRow['title']}**.")
    await refresh_cars_post(bot, eventRow)
    await refresh_event_post(bot, eventRow)


def entry_key(entry):
    return entry["id"] or entry["label"].lower()


def in_car(eventRow, driverId, leg):
    column = "driver_id" if leg == "to" else "return_id"
    return db.q(
        f"SELECT * FROM assignments WHERE event_id=? AND {column}=? AND need IN ('both', ?) ORDER BY id",
        (eventRow["id"], driverId, leg),
    )


async def apply_edit(bot, eventRow, user, role, old, new):
    eventRow = get_event(eventRow["id"])
    if role == "driver":
        await _driver_edit(bot, eventRow, user, old, new)
    else:
        await _passenger_edit(bot, eventRow, user, old, new)
    await log_action(bot, eventRow["guild_id"], f"{user.display_name} updated their {role} details for **{eventRow['title']}** after the cutoff.")
    await refresh_cars_post(bot, eventRow)
    await refresh_event_post(bot, eventRow)


async def _driver_edit(bot, eventRow, user, old, new):
    guild = bot.get_guild(eventRow["guild_id"])
    userId = user.id
    affectedIds, stuck, split = set(), [], []
    drivers = {car_row["user_id"] for car_row in db.q("SELECT user_id FROM signups WHERE event_id=? AND role='driver'", (eventRow["id"],))}

    oldk = {entry_key(entry) for entry in db.loads(old["organised"])}
    for entry in db.loads(new["organised"]):
        if entry_key(entry) in oldk or entry["id"] in drivers:
            continue
        if entry["id"]:
            row = db.q1("SELECT * FROM assignments WHERE event_id=? AND user_id=?", (eventRow["id"], entry["id"]))
        else:
            row = db.q1("SELECT * FROM assignments WHERE event_id=? AND user_id IS NULL AND lower(label)=?", (eventRow["id"], entry["label"].lower()))
        if row:
            affectedIds |= {row["driver_id"], row["return_id"]}
            for one_leg in needed(row):
                set_leg(row["id"], one_leg, userId)
            db.run("UPDATE assignments SET locked=1 WHERE id=?", (row["id"],))
        else:
            label = name_of(guild, entry["id"]) if entry["id"] else entry["label"]
            db.run(
                "INSERT INTO assignments (event_id, user_id, label, kit, driver_id, return_id, need, locked) VALUES (?,?,?,?,?,?,'both',1)",
                (eventRow["id"], entry["id"], label, "", userId, userId),
            )
        if entry["id"]:
            affectedIds.add(entry["id"])

    bump = {}

    def flag(car_row):
        bump[car_row["id"]] = [one_leg for one_leg in needed(car_row) if leg_car(car_row, one_leg) == userId]

    for car_row in {car_row["id"]: car_row for leg in ("to", "back") for car_row in in_car(eventRow, userId, leg)}.values():
        if not car_row["locked"] and avoids(db.loads(new["avoid"]), car_row["user_id"], car_row["label"]):
            flag(car_row)
    for leg in ("to", "back"):
        mine = in_car(eventRow, userId, leg)
        over = len([car_row for car_row in mine if car_row["id"] not in bump]) - (new["capacity"] or 0)
        for car_row in reversed([car_row for car_row in mine if not car_row["locked"] and car_row["id"] not in bump]):
            if over <= 0:
                break
            flag(car_row)
            over -= 1
    for rowId, legs in bump.items():
        car_row = get_row(rowId)
        for one_leg in legs:
            set_leg(rowId, one_leg, None)
        placed = auto_place(eventRow, get_row(rowId), sorted(legs), exclude=(userId,))
        affectedIds.add(car_row["user_id"])
        affectedIds |= {leg_car(placed, one_leg) for one_leg in needed(placed)}
        if missing_legs(placed):
            stuck.append(car_row["label"])
        if is_split(placed):
            split.append(car_row["label"])

    if (new["address"] or "") != (old["address"] or ""):
        affectedIds |= {car_row["user_id"] for leg in ("to", "back") for car_row in in_car(eventRow, userId, leg)}

    affectedIds.discard(userId)
    await tell_everyone_affected(bot, eventRow, affectedIds)
    over = any(seat_use(eventRow["id"], userId, one_leg) > (new["capacity"] or 0) for one_leg in ("to", "back"))
    notes = []
    if stuck:
        notes.append(f"No space for: **{', '.join(stuck)}** - they're on the waiting list.")
    if split:
        notes.append(f"Split between two cars: **{', '.join(split)}**")
    if over:
        notes.append(f"**{user.display_name}**'s car is over its stated seats because of people they've already organised.")
    if notes:
        await notify_officers(bot, eventRow, f"**{user.display_name}** updated their driver details after the cutoff:\n" + "\n".join(notes))
    if (new["capacity"] or 0) > (old["capacity"] or 0):
        await fill_waitlist(bot, eventRow)


async def _passenger_edit(bot, eventRow, user, old, new):
    guild = bot.get_guild(eventRow["guild_id"])
    userId = user.id
    rows = db.q("SELECT * FROM assignments WHERE event_id=? AND user_id=?", (eventRow["id"], userId))
    if not rows:
        return await late_join(bot, eventRow, user, "passenger")
    row = rows[0]
    oldn, newn, kit = row["need"], new["need"] or "both", new["kit"] or ""
    before = {leg_car(row, leg) for leg in needed(row)} - {None}
    parts = []
    if kit != (row["kit"] or ""):
        parts.append(f"kit is now: {kit or 'nothing listed'}")
    if newn != oldn:
        parts.append(f"now needs {NEED_NAMES[newn]}")
    db.run("UPDATE assignments SET kit=?, need=?, area=? WHERE id=?", (kit, newn, new["area"], row["id"]))
    freed = False
    if newn != oldn:
        for leg in set(LEGS[oldn]) - set(LEGS[newn]):
            set_leg(row["id"], leg, None)
            freed = True
        newly = [leg for leg in LEGS[newn] if leg not in LEGS[oldn]]
        for leg in newly:
            set_leg(row["id"], leg, None)
        if newly:
            auto_place(eventRow, get_row(row["id"]), newly)
    row = get_row(row["id"])
    after = {leg_car(row, leg) for leg in needed(row)} - {None}
    if parts:
        for driver in before | after:
            await dm_user(bot, driver, f"**{row['label']}** updated their details for **{eventRow['title']}**: {', '.join(parts)}.")
        if newn != oldn:
            await dm_user(bot, userId, f"Update for **{eventRow['title']}**: {car_text_for(guild, eventRow, userId)}")
    notes = []
    if missing_legs(row):
        notes.append("no space for the new trip - on the waiting list")
    if is_split(row):
        notes.append("split between two cars (to/back)")
    if notes:
        await notify_officers(bot, eventRow, f"**{row['label']}** changed their details after the cutoff: {'; '.join(notes)}.")
    if freed:
        await fill_waitlist(bot, eventRow)


def person_options(guild, eventRow):
    opts = []
    for row in db.q("SELECT * FROM assignments WHERE event_id=? ORDER BY id", (eventRow["id"],))[:25]:
        cursor = ", ".join(name_of(guild, leg_car(row, leg)) if leg_car(row, leg) else "no lift" for leg in needed(row))
        opts.append(discord.SelectOption(label=row["label"][:100], value=str(row["id"]), description=f"{row['need']}: {cursor}"[:100]))
    return opts


def car_options(guild, eventRow):
    opts = [discord.SelectOption(label="Needs a lift (unassigned)", value="0")]
    for driver in db.q("SELECT * FROM signups WHERE event_id=? AND role='driver'", (eventRow["id"],))[:24]:
        opts.append(discord.SelectOption(label=f"{name_of(guild, driver['user_id'])}'s car"[:100], value=str(driver["user_id"])))
    return opts


async def officer_change(bot, eventRow, who, note, notify):
    await refresh_cars_post(bot, eventRow)
    thread = await get_thread(bot, eventRow["thread_id"])
    await thread.send(f"{who}: {note}", allowed_mentions=QUIET)
    await log_action(bot, eventRow["guild_id"], f"{who} on **{eventRow['title']}**: {note}")
    await tell_everyone_affected(bot, eventRow, notify)


class OfficerView(discord.ui.View):

    def __init__(self, bot, eventRow):
        super().__init__(timeout=300)
        self.bot, self.eventRow = bot, eventRow
        self.guild = bot.get_guild(eventRow["guild_id"])

    def pick(self, placeholder, options, callback):
        selectMenu = discord.ui.Select(placeholder=placeholder, options=options)
        selectMenu.callback = callback
        self.add_item(selectMenu)
        return selectMenu

    async def done(self, interaction, text):
        for item in self.children:
            item.disabled = True
        if interaction.response.is_done():
            await interaction.edit_original_response(content=text, view=self)
        else:
            await interaction.response.edit_message(content=text, view=self)
        self.stop()


class MoveView(OfficerView):
    def __init__(self, bot, eventRow):
        super().__init__(bot, eventRow)
        self.person, self.trip = None, "both"
        self.pick("1. Who are you moving?", person_options(self.guild, eventRow), self.pick_person)
        self.pick("2. Which trip?", [
            discord.SelectOption(label="Both trips", value="both", default=True),
            discord.SelectOption(label="To the event only", value="to"),
            discord.SelectOption(label="Back from the event only", value="back"),
        ], self.pick_trip)
        self.pick("3. Move them to...", car_options(self.guild, eventRow), self.pick_dest)

    async def pick_person(self, interaction):
        self.person = int(interaction.data["values"][0])
        await interaction.response.defer()

    async def pick_trip(self, interaction):
        self.trip = interaction.data["values"][0]
        await interaction.response.defer()

    async def pick_dest(self, interaction):
        if self.person is None:
            return await interaction.response.send_message("Pick who to move first (step 1), then re-pick the destination.", ephemeral=True)
        dest = int(interaction.data["values"][0]) or None
        row = get_row(self.person)
        legs = [leg for leg in needed(row) if self.trip in ("both", leg)]
        if not legs:
            return await interaction.response.send_message(f"{row['label']} doesn't need that trip.", ephemeral=True)
        if blocked(self.eventRow, row, dest):
            return await interaction.response.send_message(BLOCKED, ephemeral=True)
        await interaction.response.defer()
        before = {leg_car(row, leg) for leg in legs}
        for leg in legs:
            set_leg(row["id"], leg, dest)
        db.run("UPDATE assignments SET locked=1 WHERE id=?", (row["id"],))
        where = f"{name_of(self.guild, dest)}'s car" if dest else "unassigned"
        trips = "" if len(legs) == len(needed(row)) else f" ({LEG_NAMES[legs[0]]} only)"
        await officer_change(self.bot, self.eventRow, interaction.user.display_name, f"moved **{row['label']}** to **{where}**{trips}.", [row["user_id"], dest, *before])
        await self.done(interaction, f"Moved **{row['label']}** to **{where}**{trips}.{capacity_warning(self.guild, self.eventRow, [dest])}")


class SwapView(OfficerView):
    def __init__(self, bot, eventRow):
        super().__init__(bot, eventRow)
        self.first = None
        self.pick("1. First person", person_options(self.guild, eventRow), self.pick_first)
        self.pick("2. Swap places with...", person_options(self.guild, eventRow), self.pick_second)

    async def pick_first(self, interaction):
        self.first = int(interaction.data["values"][0])
        await interaction.response.defer()

    async def pick_second(self, interaction):
        second = int(interaction.data["values"][0])
        if self.first is None:
            return await interaction.response.send_message("Pick the first person too.", ephemeral=True)
        if self.first == second:
            return await interaction.response.send_message("Pick two different people.", ephemeral=True)
        first_row, second_row = get_row(self.first), get_row(second)
        if first_row["need"] != second_row["need"]:
            return await interaction.response.send_message("They need different trips - use Move for those two.", ephemeral=True)
        if any(blocked(self.eventRow, first_row, leg_car(second_row, leg)) for leg in needed(first_row)) or any(blocked(self.eventRow, second_row, leg_car(first_row, leg)) for leg in needed(second_row)):
            return await interaction.response.send_message(BLOCKED, ephemeral=True)
        await interaction.response.defer()
        for leg in needed(first_row):
            firstCar, secondCar = leg_car(first_row, leg), leg_car(second_row, leg)
            set_leg(first_row["id"], leg, secondCar)
            set_leg(second_row["id"], leg, firstCar)
        db.run("UPDATE assignments SET locked=1 WHERE id IN (?,?)", (first_row["id"], second_row["id"]))
        cars = [leg_car(row, leg) for row in (first_row, second_row) for leg in needed(row)]
        await officer_change(self.bot, self.eventRow, interaction.user.display_name, f"swapped **{first_row['label']}** and **{second_row['label']}**.", [first_row["user_id"], second_row["user_id"], *cars])
        await self.done(interaction, f"Swapped **{first_row['label']}** and **{second_row['label']}**.")


class RemoveView(OfficerView):
    def __init__(self, bot, eventRow):
        super().__init__(bot, eventRow)
        self.pick("Who should be taken off the lift list?", person_options(self.guild, eventRow), self.remove)

    async def remove(self, interaction):
        await interaction.response.defer()
        row = get_row(int(interaction.data["values"][0]))
        db.run("DELETE FROM assignments WHERE id=?", (row["id"],))
        if row["user_id"]:
            db.run("DELETE FROM signups WHERE event_id=? AND user_id=? AND role='passenger'", (self.eventRow["id"], row["user_id"]))
            await dm_user(self.bot, row["user_id"], f"Officership have removed you from the lift list for **{self.eventRow['title']}**. Speak to them if that's unexpected.")
        await officer_change(self.bot, self.eventRow, interaction.user.display_name, f"removed **{row['label']}** from the lift list.", [row["driver_id"], row["return_id"]])
        await refresh_event_post(self.bot, self.eventRow)
        await fill_waitlist(self.bot, self.eventRow)
        await self.done(interaction, f"Removed **{row['label']}**.")


class AddModal(discord.ui.Modal, title="Add a passenger"):
    who = discord.ui.TextInput(label="Name, username, nickname or ID", max_length=100)
    kit = discord.ui.TextInput(label="Kit (optional)", required=False, max_length=200)

    def __init__(self, bot, eventRow):
        super().__init__()
        self.bot, self.eventRow = bot, eventRow

    async def on_submit(self, interaction):
        guild = self.bot.get_guild(self.eventRow["guild_id"])
        entry = (resolve_entries(guild, self.who.value) or [{"id": None, "label": self.who.value.strip()}])[0]
        userId = entry["id"]
        if userId and db.q1("SELECT 1 FROM assignments WHERE event_id=? AND user_id=?", (self.eventRow["id"], userId)):
            return await interaction.response.send_message(f"{name_of(guild, userId)} is already in a car - use Move instead.", ephemeral=True)
        if userId and db.q1("SELECT 1 FROM signups WHERE event_id=? AND user_id=? AND role='driver'", (self.eventRow["id"], userId)):
            return await interaction.response.send_message(f"{name_of(guild, userId)} is a driver for this event.", ephemeral=True)
        label = name_of(guild, userId) if userId else entry["label"]
        row = db.run(
            "INSERT INTO assignments (event_id, user_id, label, kit, need, locked) VALUES (?,?,?,?,'both',1)",
            (self.eventRow["id"], userId, label, self.kit.value.strip()),
        )
        await interaction.response.send_message(
            f"Added **{label}**. Choose the trips they need and which car they go in.",
            view=AddView(self.bot, self.eventRow, row, label), ephemeral=True,
        )


class AddView(OfficerView):
    def __init__(self, bot, eventRow, row_id, label):
        super().__init__(bot, eventRow)
        self.row_id, self.label, self.need = row_id, label, "both"
        self.pick("Trips needed", [
            discord.SelectOption(label="Lifts both ways", value="both", default=True),
            discord.SelectOption(label="To the event only", value="to"),
            discord.SelectOption(label="Back from the event only", value="back"),
        ], self.pick_need)
        self.pick("Put them in...", car_options(self.guild, eventRow), self.place)

    async def pick_need(self, interaction):
        self.need = interaction.data["values"][0]
        await interaction.response.defer()

    async def place(self, interaction):
        dest = int(interaction.data["values"][0]) or None
        db.run("UPDATE assignments SET need=?, driver_id=NULL, return_id=NULL WHERE id=?", (self.need, self.row_id))
        row = get_row(self.row_id)
        if blocked(self.eventRow, row, dest):
            return await interaction.response.send_message(BLOCKED, ephemeral=True)
        await interaction.response.defer()
        for leg in needed(row):
            set_leg(self.row_id, leg, dest)
        where = f"{name_of(self.guild, dest)}'s car" if dest else "unassigned"
        await officer_change(self.bot, self.eventRow, interaction.user.display_name, f"added **{self.label}** to **{where}**.", [row["user_id"], dest])
        await self.done(interaction, f"**{self.label}** is in **{where}**.{capacity_warning(self.guild, self.eventRow, [dest])}")


class ManageView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @staticmethod
    async def gate(interaction, need_people=True):
        if not is_manager(interaction.user):
            await interaction.response.send_message("Only officers can change the cars.", ephemeral=True)
            return None
        eventRow = event_by_thread(interaction.channel_id)
        if not eventRow or (need_people and not db.q1("SELECT 1 FROM assignments WHERE event_id=?", (eventRow["id"],))):
            await interaction.response.send_message("Nobody in the cars yet.", ephemeral=True)
            return None
        return eventRow

    @discord.ui.button(label="Move", style=discord.ButtonStyle.secondary, custom_id="logi:manage")
    async def manage(self, interaction, button):
        if eventRow := await self.gate(interaction):
            await interaction.response.send_message("Choose who to move, which trip, and where (first 25 people shown).", view=MoveView(interaction.client, eventRow), ephemeral=True)

    @discord.ui.button(label="Swap", style=discord.ButtonStyle.secondary, custom_id="logi:swap")
    async def swap(self, interaction, button):
        if eventRow := await self.gate(interaction):
            await interaction.response.send_message("Choose two people to swap (first 25 people shown).", view=SwapView(interaction.client, eventRow), ephemeral=True)

    @discord.ui.button(label="Add person", style=discord.ButtonStyle.secondary, custom_id="logi:add")
    async def add(self, interaction, button):
        if eventRow := await self.gate(interaction, need_people=False):
            await interaction.response.send_modal(AddModal(interaction.client, eventRow))

    @discord.ui.button(label="Remove person", style=discord.ButtonStyle.secondary, custom_id="logi:remove")
    async def remove(self, interaction, button):
        if eventRow := await self.gate(interaction):
            await interaction.response.send_message("Choose who to remove (first 25 people shown).", view=RemoveView(interaction.client, eventRow), ephemeral=True)

    @discord.ui.button(label="Pull out", style=discord.ButtonStyle.danger, custom_id="logi:withdraw2")
    async def withdraw(self, interaction, button):
        await start_withdraw(interaction)
