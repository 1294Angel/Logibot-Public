from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from core import config

timeZone = ZoneInfo(config.TIMEZONE)
LEGS = {"to": ("to",), "back": ("back",), "both": ("to", "back")}


def parse_local(date_str: str, time_str: str) -> datetime:
    dateTime = datetime.strptime(f"{date_str.strip()} {time_str.strip()}", "%d/%m/%Y %H:%M")
    return dateTime.replace(tzinfo=timeZone)


def signup_close(start: datetime) -> datetime:
    day = start.astimezone(timeZone).date() - timedelta(days=1)
    while day.weekday() != 0:
        day -= timedelta(days=1)
    return datetime.combine(day, time(config.SIGNUP_CLOSE_HOUR), tzinfo=timeZone)


def resolve_entries(guild, text: str) -> list[dict]:
    if not text or text.strip().lower() in {"none", "no", "n/a", "-", "nobody"}:
        return []
    out = []
    for raw in text.replace("\n", ",").replace(";", ",").split(","):
        name = raw.strip().lstrip("@")
        if not name:
            continue
        out.append({"id": find_member(guild, name), "label": name})
    return out


def member_names(member):
    return [name.lower() for name in (member.name, member.global_name, member.display_name) if name]


def mention_id(name):
    if name.startswith("<@") and name.endswith(">"):
        digits = name[2:-1].lstrip("!")
        if digits.isdigit():
            return int(digits)
    return None


def find_member(guild, name: str):
    low = name.lower()
    user_id = mention_id(name)
    if user_id is None and name.isdigit():
        user_id = int(name)
    if user_id is not None:
        member = guild.get_member(user_id)
        if member:
            return member.id
    exact = [member for member in guild.members if low in member_names(member)]
    if len(exact) == 1:
        return exact[0].id
    if not exact:
        part = [member for member in guild.members if any(low in part_name for part_name in member_names(member))]
        if len(part) == 1:
            return part[0].id
    return None


def avoids(entries: list[dict], user_id, label: str) -> bool:
    return any((entry["id"] and entry["id"] == user_id) or entry["label"].lower() == label.lower() for entry in entries)



def is_outward(word):
    letters = 0
    while letters < len(word) and "a" <= word[letters] <= "z":
        letters += 1
    rest = word[letters:]
    if rest and "a" <= rest[-1] <= "z":
        rest = rest[:-1]
    return 1 <= letters <= 2 and 1 <= len(rest) <= 2 and rest.isdigit() and rest.isascii()


def area_bits(text):
    low = (text or "").lower()
    chunks = "".join(char if char.isascii() and char.isalnum() else " " for char in low).split()
    outwardWord = next((word for word in chunks if is_outward(word)), None)
    outwardLetters = "".join(char for char in outwardWord if char.isalpha())[:2] if outwardWord else None
    letters_only = "".join(char if "a" <= char <= "z" else " " for char in low).split()
    words = {word for word in letters_only if len(word) >= 4}
    return outwardWord, outwardLetters, words


def area_score(text_a, text_b) -> int:
    outwardA, lettersA, wordsA = area_bits(text_a)
    outwardB, lettersB, wordsB = area_bits(text_b)
    if outwardA and outwardA == outwardB:
        return 3
    if lettersA and lettersA == lettersB:
        return 2
    return 1 if wordsA & wordsB else 0


def choose_cars(drivers, used, person, area_matching=False, legs=None, prefer=None):
    legs = tuple(legs or LEGS[person["need"]])

    def ok(driverId, leg):
        driver = drivers[driverId]
        return driver["capacity"] - used[driverId][leg] > 0 and not avoids(driver["avoid"], person["user_id"], person["label"])

    def key(driverId, legList):
        score = area_score(person.get("area"), drivers[driverId].get("area")) if area_matching else 0
        return (score, min(drivers[driverId]["capacity"] - used[driverId][cur_leg] for cur_leg in legList))

    if prefer in drivers and all(ok(prefer, cur_leg) for cur_leg in legs):
        return tuple(prefer if cur_leg in legs else None for cur_leg in ("to", "back"))
    if len(legs) == 2:
        same = [driver for driver in drivers if ok(driver, "to") and ok(driver, "back")]
        if same:
            best = max(same, key=lambda driver: key(driver, legs))
            return best, best
    out = {"to": None, "back": None}
    for leg in legs:
        cands = [driver for driver in drivers if ok(driver, leg)]
        out[leg] = max(cands, key=lambda driver: key(driver, (leg,))) if cands else None
    return out["to"], out["back"]


def allocate(drivers: list[dict], passengers: list[dict], area_matching=False) -> list[dict]:
    info = {driver["user_id"]: driver for driver in drivers}
    used = {driver["user_id"]: {"to": 0, "back": 0} for driver in drivers}
    pool = {person["user_id"]: person for person in passengers}
    placed, out = set(), []

    for driver in drivers:
        for entry in driver["organised"]:
            userId = entry["id"]
            if userId in info or userId in placed:
                continue
            person = pool.pop(userId, None) if userId else None
            need = person["need"] if person else "both"
            row = {
                "user_id": userId, "label": person["label"] if person else entry["label"], "kit": person["kit"] if person else "",
                "need": need, "area": person["area"] if person else None, "locked": 1,
                "to": driver["user_id"] if "to" in LEGS[need] else None,
                "back": driver["user_id"] if "back" in LEGS[need] else None,
            }
            for leg in LEGS[need]:
                used[driver["user_id"]][leg] += 1
            out.append(row)
            if userId:
                placed.add(userId)

    order = sorted(pool.values(), key=lambda person: person["need"] != "both")
    for person in order:
        to, back = choose_cars(info, used, person, area_matching)
        if to:
            used[to]["to"] += 1
        if back:
            used[back]["back"] += 1
        out.append({**person, "to": to, "back": back, "locked": 0})
    return out


def is_mixed(row) -> bool:
    return row["need"] == "both" and row["to"] and row["back"] and row["to"] != row["back"]


def lacks_lift(row) -> bool:
    return ("to" in LEGS[row["need"]] and not row["to"]) or ("back" in LEGS[row["need"]] and not row["back"])
