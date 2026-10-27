from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

STAMP = "%Y%m%dT%H%M%SZ"
DEFAULT_LENGTH = timedelta(hours=2)


def event_times(eventRow):
    start = datetime.fromtimestamp(eventRow["start_ts"], timezone.utc)
    return start, start + DEFAULT_LENGTH


def calendar_url(eventRow) -> str:
    start, end = event_times(eventRow)
    query = urlencode({
        "action": "TEMPLATE", "text": eventRow["title"], "dates": f"{start:{STAMP}}/{end:{STAMP}}",
        "location": eventRow["location"] or "", "details": eventRow["fb_link"] or "",
    })
    return "https://calendar.google.com/calendar/render?" + query


def ics_escape(text: str) -> str:
    backslash = "\\"
    return text.replace(backslash, backslash * 2).replace(";", backslash + ";").replace(",", backslash + ",").replace("\r", "").replace("\n", backslash + "n")


def ics_text(eventRow) -> str:
    start, end = event_times(eventRow)
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//LogiBot//EN", "BEGIN:VEVENT",
        f"UID:logibot-{eventRow['id']}-{eventRow['start_ts']}@logibot", f"DTSTAMP:{datetime.now(timezone.utc):{STAMP}}",
        f"DTSTART:{start:{STAMP}}", f"DTEND:{end:{STAMP}}",
        f"SUMMARY:{ics_escape(eventRow['title'])}", f"LOCATION:{ics_escape(eventRow['location'] or '')}",
    ]
    if eventRow["fb_link"]:
        lines.append("URL:" + eventRow["fb_link"])
    lines += ["END:VEVENT", "END:VCALENDAR"]
    crlf = "\r" + "\n"
    return crlf.join(lines) + crlf
