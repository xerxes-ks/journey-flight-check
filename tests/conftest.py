"""Shared fixtures. All AeroAPI traffic is injected; tests never open a network socket.

The fake fetch rejects start/end the way AeroAPI does (~47h ahead, 10 days back)
so an unclamped window cannot silently succeed.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from copy import deepcopy

import pytest

from flightcheck.model import UTC, WINDOW_END_MAX_AHEAD_H, WINDOW_START_MAX_PAST_DAYS, parse_time

FX = os.path.join(os.path.dirname(__file__), "fixtures", "aeroapi")

NEAR_DEP = "2027-03-15T16:30:00Z"  # T-90 min vs 18:00Z STD
FAR_DEP = "2027-03-15T08:00:00Z"   # T-10 h, outside the 6 h inbound window


def load_fx(name: str) -> dict:
    with open(os.path.join(FX, name)) as fh:
        return json.load(fh)


def aeroapi_window_error(params: dict, now: dt.datetime) -> str | None:
    """Mirror AeroAPI's documented start/end bounds (end ≤ now+~2 days)."""
    if not params:
        return None
    if "start" not in params and "end" not in params:
        return None
    start = parse_time(params.get("start"))
    end = parse_time(params.get("end"))
    now = now.astimezone(UTC)
    ahead = dt.timedelta(hours=WINDOW_END_MAX_AHEAD_H)
    past = dt.timedelta(days=WINDOW_START_MAX_PAST_DAYS)
    if end and end > now + ahead:
        return "end is more than 2 days in the future"
    if start and start > now + ahead:
        return "start is more than 2 days in the future"
    if start and start < now - past:
        return "start is more than 10 days in the past"
    if end and end < now - past:
        return "end is more than 10 days in the past"
    return None


def make_fetch(mapping: dict[str, tuple[int, dict]], now=NEAR_DEP):
    """Exact path match, then longest prefix. Tests never hit the network."""
    now_dt = parse_time(now) if not isinstance(now, dt.datetime) else now

    def fetch(path, params):
        bad = aeroapi_window_error(params or {}, now_dt)
        if bad:
            return 400, {"title": "Invalid request", "detail": bad, "status": 400}
        if path in mapping:
            status, body = mapping[path]
            return status, deepcopy(body)
        for prefix in sorted(mapping, key=len, reverse=True):
            if path.startswith(prefix):
                status, body = mapping[prefix]
                return status, deepcopy(body)
        return 404, {"title": "Not Found"}

    return fetch


def scenario_fetch(*, flight="xx123.json", inbound="xx122.json", onward="yy456.json", now=NEAR_DEP):
    return make_fetch({
        "/flights/XX123": (200, load_fx(flight)),
        "/flights/XXX123": (200, load_fx(flight)),
        "/flights/XXX122-1700000000-airline-0002": (200, load_fx(inbound)),
        "/flights/YY456": (200, load_fx(onward)),
        "/flights/YYY456": (200, load_fx(onward)),
        "/flights/XX754": (200, load_fx("xx754-del.json")),
        "/flights/XXX754": (200, load_fx("xx754-del.json")),
    }, now=now)


@pytest.fixture
def fetch_on_time():
    return scenario_fetch()
