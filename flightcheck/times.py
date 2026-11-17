"""Local-time formatting. Callers should never convert to UTC.

Every public timestamp is an ISO-8601 string with the airport's offset,
plus the IANA tz name and a short human form like
'11:20 Tue 9 Mar (XXC time)'.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def zone(tzname: Optional[str]) -> dt.tzinfo:
    name = tzname or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:
        return UTC


def as_local(t: Optional[dt.datetime], tzname: Optional[str]) -> Optional[dt.datetime]:
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(zone(tzname))


def human_form(t: dt.datetime, airport: str = "", tzname: str = "") -> str:
    loc = as_local(t, tzname) or t
    if airport:
        label = f" ({airport} time)"
    elif tzname and tzname != "UTC":
        label = f" ({tzname} time)"
    else:
        label = " (UTC)"
    return (
        f"{loc:%H:%M} {DOW[loc.weekday()]} {loc.day} {MON[loc.month - 1]}"
        f"{label}"
    )


def format_delay(minutes: int) -> str:
    """Human delay: minutes, or hours and minutes once the delay is 90 min or more."""
    m = abs(int(minutes))
    if m < 90:
        return f"{m} min"
    hours, mins_ = divmod(m, 60)
    if mins_ == 0:
        return f"{hours} h"
    return f"{hours} h {mins_} min"


def clock(
    t: Optional[dt.datetime],
    tzname: Optional[str],
    airport: str = "",
    *,
    vs: Optional[dt.datetime] = None,
) -> str:
    if t is None:
        return "unknown"
    loc = as_local(t, tzname)
    if loc is None:
        return "unknown"
    suffix = f" ({airport} time)" if airport else ""
    text = f"{loc:%H:%M}"
    if vs is not None:
        vs_loc = as_local(vs, tzname)
        if vs_loc is not None and loc.date() != vs_loc.date():
            text = f"{loc:%H:%M} {DOW[loc.weekday()]} {loc.day} {MON[loc.month - 1]}"
    return f"{text}{suffix}"


def local_dict(t: Optional[dt.datetime], tzname: Optional[str], airport: str = "") -> Optional[dict]:
    if t is None:
        return None
    loc = as_local(t, tzname)
    if loc is None:
        return None
    tz = tzname or "UTC"
    return {
        "local": loc.isoformat(timespec="seconds"),
        "tz": tz,
        "human": human_form(t, airport, tz),
    }


def parse_now(value) -> dt.datetime:
    """CLI/test `now`: ISO with offset or Z, or a naive timestamp treated as UTC."""
    if value is None or value == "":
        return dt.datetime.now(UTC)
    if isinstance(value, dt.datetime):
        t = value
    else:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(UTC)


def parse_now_maybe(value):
    if value is None or value == "":
        return None
    return parse_now(value)
