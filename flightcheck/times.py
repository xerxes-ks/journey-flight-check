"""Local-time formatting. Callers should never convert to UTC.

Every public timestamp is an ISO-8601 string with the airport's offset,
plus the IANA tz name and a short human form like
'02:55 Sun 13 Dec (DEL time)'.
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


def clock(t: Optional[dt.datetime], tzname: Optional[str], airport: str = "") -> str:
    if t is None:
        return "unknown"
    loc = as_local(t, tzname)
    if loc is None:
        return "unknown"
    suffix = f" ({airport} time)" if airport else ""
    return f"{loc:%H:%M}{suffix}"


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
