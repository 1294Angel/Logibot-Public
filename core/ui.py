import asyncio, discord
from datetime import datetime, timedelta
from time import time as unix_now

from core import config
from core.allocation import timeZone, parse_local, signup_close

TIMED_OUT = "I timed out waiting for you. Press the button again to start over."
SKIP = "__skip__"


class Cancelled(Exception):
    pass


def check_seats(answer):
    if not answer.isdigit() or not 0 <= int(answer) <= 12:
        return "Please reply with a whole number (0-12)."


NEEDS = {"to": "to", "there": "to", "back": "back", "home": "back", "from": "back", "both": "both", "return": "both"}


def check_need(answer):
    if answer.lower() not in NEEDS:
        return "Reply `to`, `back` or `both`."


def check_date(answer):
    try:
        datetime.strptime(answer, "%d/%m/%Y")
    except ValueError:
        return "Use DD/MM/YYYY and make it a real date (e.g. 28/09/2026)."


def check_clock(answer):
    try:
        datetime.strptime(answer, "%H:%M")
    except ValueError:
        return "Use 24h HH:MM (e.g. 09:00)."


def check_link(answer):
    if not answer.startswith(("http://", "https://")):
        return "The link must start with http:// or https://"


NO_LINK = ("none", "no", "n/a", "na", "-")


def check_link_optional(answer):
    if answer.lower() not in NO_LINK:
        return "Send a link starting with http:// or https://, or press **No link**." if check_link(answer) else None


def link_or_none(answer):
    return None if answer.lower() in NO_LINK else answer


def parse_markers(text):
    if text.strip().lower() in ("none", "no", "-"):
        return []
    out = []
    for marker in (part.strip()[:45] for part in text.split(",")):
        if marker and marker.lower() not in {other.lower() for other in out}:
            out.append(marker)
    return out[:5]


MARKER_PROMPT = (
    "Anything you want people to tell you when they say they're going? e.g. `Time of arrival, Dietary needs`. "
    "Separate with commas (max 5), or press **No extra details**."
)


class QuickView(discord.ui.View):

    def __init__(self, user_id, buttons):
        super().__init__(timeout=config.QUESTION_TIMEOUT)
        self.user_id, self.value = user_id, None
        for label, value in buttons[:20]:
            button = discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary)
            button.callback = self._answer(value)
            self.add_item(button)

    def _answer(self, value):
        async def secondCar(interaction):
            self.value = value
            await interaction.response.defer()
            self.stop()
        return secondCar

    async def interaction_check(self, interaction):
        return interaction.user.id == self.user_id


class ChoiceView(discord.ui.View):

    def __init__(self, user_id, options, placeholder, prev):
        super().__init__(timeout=config.QUESTION_TIMEOUT)
        self.user_id, self.value, self.labels = user_id, None, {}
        opts = []
        for option in options[:25]:
            label, value = option[0], str(option[1])
            self.labels[value] = label
            opts.append(discord.SelectOption(label=label[:100], value=value, description=(option[2][:100] if len(option) > 2 and option[2] else None)))
        keep = self.labels.get(str(prev)) if prev is not None else None
        selectMenu = discord.ui.Select(placeholder=(f"Current: {keep}"[:150] if keep else placeholder), options=opts)
        selectMenu.callback = self._picked
        self.add_item(selectMenu)
        self.select = selectMenu
        if keep:
            keep = discord.ui.Button(label=f"Keep: {keep}"[:80], style=discord.ButtonStyle.secondary)
            keep.callback = self._keep(str(prev))
            self.add_item(keep)

    def _keep(self, value):
        async def secondCar(interaction):
            self.value = value
            await interaction.response.defer()
            self.stop()
        return secondCar

    async def _picked(self, interaction):
        self.value = self.select.values[0]
        await interaction.response.defer()
        self.stop()

    async def interaction_check(self, interaction):
        return interaction.user.id == self.user_id


class ConfirmView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=config.QUESTION_TIMEOUT)
        self.user_id = user_id
        self.value = None

    async def interaction_check(self, interaction):
        return interaction.user.id == self.user_id

    async def _finish(self, interaction, value, text):
        self.value = value
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(text)
        self.stop()

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes(self, interaction, button):
        await self._finish(interaction, True, "Saved - thanks!")

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def no(self, interaction, button):
        await self._finish(interaction, False, "No problem, let's go through it again.")


async def _remove_buttons(sent):
    if sent is not None:
        try:
            await sent.edit(view=None)
        except (discord.HTTPException, AttributeError):
            pass


async def _next_answer(bot, user, dmChannel, view):
    messageTask = asyncio.ensure_future(bot.wait_for(
        "message", check=lambda message: message.author.id == user.id and message.channel.id == dmChannel.id, timeout=config.QUESTION_TIMEOUT))
    tasks = {messageTask}
    viewTask = None
    if view is not None and not view.is_finished():
        viewTask = asyncio.ensure_future(view.wait())
        tasks.add(viewTask)
    done, pending = await asyncio.wait(tasks, timeout=config.QUESTION_TIMEOUT + 5, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if viewTask in done and view.value is not None:
        return view.value, True
    if messageTask in done:
        try:
            return messageTask.result().content.strip(), False
        except asyncio.TimeoutError:
            pass
    await dmChannel.send(TIMED_OUT)
    raise Cancelled


async def ask(bot, user, dmChannel, prompt, prev=None, validate=None, quick=(), maxLength=200):
    buttons = list(quick)
    if prev not in (None, ""):
        buttons.insert(0, (f"Keep: {prev}", str(prev)))
    view = QuickView(user.id, buttons) if buttons else None
    sent = await dmChannel.send(prompt, view=view)
    try:
        while True:
            answer, pressed = await _next_answer(bot, user, dmChannel, view)
            if answer.lower() == "keep" and prev not in (None, ""):
                answer = str(prev)
            if not answer:
                await dmChannel.send("I need an answer to continue.")
                continue
            if len(answer) > maxLength:
                await dmChannel.send(f"That's too long - please keep it under {maxLength} characters (yours was {len(answer)}).")
                if pressed:
                    view = None
                continue
            errorText = validate(answer) if validate else None
            if errorText:
                await dmChannel.send(errorText)
                if pressed:
                    view = None
                continue
            return answer
    finally:
        await _remove_buttons(sent)


async def choose(dmChannel, user, prompt, options, prev=None, placeholder="Choose one..."):
    view = ChoiceView(user.id, options, placeholder, prev)
    sent = await dmChannel.send(prompt, view=view)
    await view.wait()
    if view.value is None:
        await dmChannel.send(TIMED_OUT)
        raise Cancelled
    try:
        await sent.edit(content=f"{prompt}\n- **{view.labels[view.value]}**", view=None)
    except (discord.HTTPException, AttributeError):
        pass
    return view.value


async def confirm(dmChannel, user, text):
    view = ConfirmView(user.id)
    await dmChannel.send(text, view=view)
    await view.wait()
    if view.value is None:
        await dmChannel.send(TIMED_OUT)
        raise Cancelled
    return view.value


async def pick_date(bot, user, dmChannel, prompt="**Date?**", weekday=None, prev=None, dates=None):
    today = datetime.now(timeZone).date()
    days, cur_day = [], today
    if dates:
        days = list(dates)[:24]
    while not dates and len(days) < 24 and (cur_day - today).days < 200:
        if weekday is None or cur_day.weekday() == weekday:
            days.append(cur_day)
        cur_day += timedelta(days=1)
    options = [(day.strftime("%a %d %b %Y"), day.strftime("%d/%m/%Y")) for day in days]
    options.append(("Another date (you'll type it)", SKIP))
    pick = await choose(dmChannel, user, prompt, options, prev=prev, placeholder="Pick a date...")
    if pick == SKIP:
        return await ask(bot, user, dmChannel, "Type the date as **DD/MM/YYYY**.", validate=check_date)
    return pick


async def pick_interval(bot, user, dmChannel, prompt="**Every how many days?**", prev=None):
    common = [2, 3, 4, 5, 6, 7, 10, 14, 21, 28, 30, 60, 90]
    options = [(f"Every {days} days", str(days)) for days in common] + [("Another number (you'll type it)", SKIP)]
    pick = await choose(dmChannel, user, prompt, options, prev=prev, placeholder="Pick how often...")
    if pick != SKIP:
        return int(pick)
    return int(await ask(bot, user, dmChannel, "Type the number of days (1-365).", validate=lambda text: None if text.isdigit() and 1 <= int(text) <= 365 else "Use a whole number from 1 to 365."))


async def pick_time(bot, user, dmChannel, prompt="**Start time?**", prev=None, allow_none=False):
    options = [(f"{hour:02d}:00", f"{hour:02d}:00") for hour in range(7, 24)]
    options.append(("Another time (you'll type it)", SKIP))
    if allow_none:
        options.append(("No fixed time", "none"))
    pick = await choose(dmChannel, user, prompt, options, prev=prev, placeholder="Pick a time...")
    if pick == SKIP:
        return await ask(bot, user, dmChannel, "Type the time as **HH:MM** (24 hour).", validate=check_clock)
    return None if pick == "none" else pick


def cutoff_prompt(default, now):
    if default.timestamp() > now:
        return (
            f"**Sign-up cutoff?** By default sign-ups close at <t:{int(default.timestamp())}:F> (9AM the Monday before). "
            "Press the button to use that, or type a different cutoff as DD/MM/YYYY HH:MM."
        )
    return (
        f"**Sign-up cutoff?** The usual cutoff (9AM the Monday before, <t:{int(default.timestamp())}:F>) has already "
        "passed, so I need one from you. Type it as DD/MM/YYYY HH:MM."
    )


def make_cutoff_checker(start, default, now, words):

    def check(answer):
        if answer.lower() in words:
            return None if default.timestamp() > now else "That default has already passed - give a date and time."
        try:
            date_part, _, time_part = answer.partition(" ")
            cutoff = parse_local(date_part, time_part)
        except ValueError:
            return "Use DD/MM/YYYY HH:MM, e.g. 21/09/2026 09:00."
        if cutoff.timestamp() <= now:
            return "That's in the past - give a future cutoff."
        if cutoff.timestamp() >= start.timestamp():
            return "The cutoff must be before the event starts."

    return check


async def ask_cutoff(bot, user, dmChannel, start):
    default, now = signup_close(start), unix_now()
    quick = [(f"Use default: {default:%a %d %b %H:%M}", "ok")] if default.timestamp() > now else []
    answer = await ask(bot, user, dmChannel, cutoff_prompt(default, now), validate=make_cutoff_checker(start, default, now, {"ok"}), quick=quick)
    if answer.lower() == "ok":
        return default, True
    return parse_local(*answer.split(" ", 1)), False
