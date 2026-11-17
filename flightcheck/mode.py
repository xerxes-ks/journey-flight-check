"""Fixture vs live mode, and a bundled synthetic AeroAPI fetch."""

from __future__ import annotations

import datetime as dt
import json
import os
from copy import deepcopy
from pathlib import Path

from .airports import tz_for_iata
from .httpsvc import authorized
from .model import UTC, WINDOW_END_MAX_AHEAD_H, WINDOW_START_MAX_PAST_DAYS, parse_time
from .times import parse_now, zone

FX_DIR = Path(__file__).resolve().parent / "fixtures" / "aeroapi"


def env_mode() -> str:
    raw = (os.environ.get("FLIGHTCHECK_MODE") or "").strip().lower()
    if raw in ("fixtures", "fixture", "synthetic"):
        return "fixtures"
    return "live"


def is_fixtures(headers: dict | None = None) -> bool:
    if env_mode() == "fixtures":
        return True
    headers = headers or {}
    mode = headers.get("X-Flightcheck-Mode") or headers.get("x-flightcheck-mode") or ""
    if str(mode).strip().lower() != "fixtures":
        return False
    return authorized(headers)


def resolve_now(headers: dict | None, data: dict | None, query: dict | None = None):
    """ISO `now`, optionally origin-local when naive. Live mode ignores it."""
    if not is_fixtures(headers):
        return None
    data = data or {}
    query = query or {}
    raw = data.get("now") or query.get("now")
    if not raw:
        return None
    origin = data.get("origin") or query.get("origin")
    t = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if t.tzinfo is None:
        tzname = tz_for_iata(origin) if origin else "UTC"
        t = t.replace(tzinfo=zone(tzname))
    return t.astimezone(UTC)


def _load(name: str) -> dict:
    with open(FX_DIR / name) as fh:
        return json.load(fh)


def _window_error(params: dict, now: dt.datetime) -> str | None:
    if not params or ("start" not in params and "end" not in params):
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


def fixture_fetch(now=None):
    """Synthetic AeroAPI: XX123 / YY456 / XX321 only."""
    now_dt = parse_now(now) if now is not None else dt.datetime.now(UTC)
    mapping = {
        "/flights/XX123": _load("xx123.json"),
        "/flights/XXX123": _load("xx123.json"),
        "/flights/XXX122-1700000000-airline-0002": _load("xx122.json"),
        "/flights/YY456": _load("yy456.json"),
        "/flights/YYY456": _load("yy456.json"),
        "/flights/XX321": _load("xx321.json"),
        "/flights/XXX321": _load("xx321.json"),
    }

    def fetch(path, params):
        bad = _window_error(params or {}, now_dt)
        if bad:
            return 400, {"title": "Invalid request", "detail": bad, "status": 400}
        body = None
        if path in mapping:
            body = mapping[path]
        else:
            for prefix in sorted(mapping, key=len, reverse=True):
                if path.startswith(prefix):
                    body = mapping[prefix]
                    break
        if body is None:
            return 404, {"title": "Not Found"}
        return 200, _filter_window(deepcopy(body), params or {})

    return fetch


def _filter_window(body: dict, params: dict) -> dict:
    if not isinstance(body, dict) or "flights" not in body:
        return body
    start = parse_time((params or {}).get("start"))
    end = parse_time((params or {}).get("end"))
    if not start and not end:
        return body
    kept = []
    for f in body.get("flights") or []:
        t = parse_time((f or {}).get("scheduled_out") or (f or {}).get("scheduled_off"))
        if t is None:
            kept.append(f)
            continue
        if start and t < start:
            continue
        if end and t >= end:
            continue
        kept.append(f)
    out = dict(body)
    out["flights"] = kept
    return out
