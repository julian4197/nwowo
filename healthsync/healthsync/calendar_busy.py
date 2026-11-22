"""Read the upcoming events of iCloud calendars (CalDAV) so training can be
planned around them. Writes calendar_busy.json: a list of
{start, end, allDay, title, calendar} for the next CALENDAR_DAYS days.

Env: ICLOUD_USER, ICLOUD_APP_PASSWORD (app-specific password from
appleid.apple.com), ICLOUD_CALENDARS (comma-separated names, empty = all),
CALENDAR_DAYS (default 14).
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

URL = "https://caldav.icloud.com/"


def enabled() -> bool:
    return bool(os.getenv("ICLOUD_USER") and os.getenv("ICLOUD_APP_PASSWORD"))


def _local(v: Any, tz: ZoneInfo) -> tuple[str, bool]:
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=tz)
        return v.astimezone(tz).isoformat(timespec="minutes"), False
    if isinstance(v, date):
        return v.isoformat(), True
    return str(v), False


def from_component(comp, calendar: str, tz: ZoneInfo) -> dict[str, Any] | None:
    """One VEVENT -> our dict. Skips cancelled and 'free' (transparent) events."""
    if str(comp.get("STATUS", "")).upper() == "CANCELLED":
        return None
    if str(comp.get("TRANSP", "")).upper() == "TRANSPARENT":
        return None
    start = comp.get("DTSTART")
    if start is None:
        return None
    start_v = start.dt
    end = comp.get("DTEND")
    if end is not None:
        end_v = end.dt
    elif comp.get("DURATION") is not None:
        end_v = start_v + comp.get("DURATION").dt
    else:
        end_v = start_v + (timedelta(days=1) if not isinstance(start_v, datetime) else timedelta(hours=1))
    s, all_day = _local(start_v, tz)
    e, _ = _local(end_v, tz)
    return {"start": s, "end": e, "allDay": all_day, "title": str(comp.get("SUMMARY", "")), "calendar": calendar}


def fetch(days: int | None = None) -> list[dict[str, Any]]:
    import caldav

    tz = ZoneInfo(os.getenv("TZ", "Europe/Berlin"))
    days = days or int(os.getenv("CALENDAR_DAYS", "14"))
    wanted = {c.strip().lower() for c in os.getenv("ICLOUD_CALENDARS", "").split(",") if c.strip()}
    start = datetime.combine(date.today(), time.min, tz)
    end = start + timedelta(days=days)
    client = caldav.DAVClient(url=URL, username=os.environ["ICLOUD_USER"], password=os.environ["ICLOUD_APP_PASSWORD"])
    out: list[dict[str, Any]] = []
    for cal in client.principal().calendars():
        name = cal.get_display_name() or ""
        if wanted and name.lower() not in wanted:
            continue
        try:
            events = cal.search(start=start, end=end, event=True, expand=True)
        except Exception as e:
            log.warning("Calendar %s: %s", name, e)
            continue
        for ev in events:
            for comp in ev.icalendar_instance.walk("VEVENT"):
                item = from_component(comp, name, tz)
                if item:
                    out.append(item)
    out.sort(key=lambda x: x["start"])
    log.info("iCloud calendars: %d events in the next %d days", len(out), days)
    return out
