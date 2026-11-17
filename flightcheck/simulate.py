"""Zero-cost replay of essential checkpoints against synthetic fixtures."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy
from typing import Optional
from zoneinfo import ZoneInfo

from .airports import tz_for_iata
from .events import detect_events, event_payload
from .model import UTC, normalize_ident, parse_date, parse_time
from .store import MemoryStore
from .track import load_track, start_track, tick
from .times import local_dict

SCENARIOS = (
    "on_time",
    "inbound_late_recovers",
    "big_delay",
    "gate_change",
    "cancelled_then_confirmed",
    "cancelled_then_reinstated",
    "diverted",
    "retimed",
    "upstream_outage",
)

DEFAULT_STD = "09:40"
BLOCK_H = 3


def _iso(t: dt.datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_hhmm(value: str) -> tuple[int, int]:
    parts = str(value).strip().replace(".", ":").split(":")
    return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0


def resolve_clock(data: dict) -> dict:
    origin = (data.get("origin") or "LHR").upper()
    dest = (data.get("destination") or "SIN").upper()
    origin_tz = data.get("origin_tz") or tz_for_iata(origin) or "UTC"
    dest_tz = data.get("dest_tz") or tz_for_iata(dest) or "UTC"
    date = parse_date(data.get("date") or "2027-03-15")
    hh, mm = _parse_hhmm(data.get("std_local") or data.get("std") or DEFAULT_STD)
    try:
        otz = ZoneInfo(origin_tz)
    except Exception:
        otz = UTC
        origin_tz = "UTC"
    std_local = dt.datetime.combine(date, dt.time(hh, mm), tzinfo=otz)
    std = std_local.astimezone(UTC)
    sta = std + dt.timedelta(hours=float(data.get("block_hours") or BLOCK_H))
    ident = normalize_ident(data.get("flight") or data.get("label") or "XX123")
    return {
        "ident": ident,
        "origin": origin,
        "destination": dest,
        "origin_tz": origin_tz,
        "dest_tz": dest_tz,
        "date": date.isoformat(),
        "std": std,
        "sta": sta,
        "std_local": std_local,
    }


def _template(clock: dict) -> dict:
    std, sta = clock["std"], clock["sta"]
    off = std + dt.timedelta(minutes=12)
    on = sta - dt.timedelta(minutes=12)
    inbound_in = std - dt.timedelta(minutes=50)
    return {
        "flights": [{
            "ident": "XXX" + clock["ident"][2:] if clock["ident"][:2].isalpha() else clock["ident"],
            "ident_icao": "XXX" + clock["ident"][2:] if len(clock["ident"]) > 2 else clock["ident"],
            "ident_iata": clock["ident"],
            "fa_flight_id": f"{clock['ident']}-sim-0001",
            "registration": "N123XX",
            "inbound_fa_flight_id": f"{clock['ident']}-sim-in",
            "cancelled": False,
            "diverted": False,
            "origin": {
                "code_iata": clock["origin"],
                "timezone": clock["origin_tz"],
                "name": "Example Hub",
                "city": "Example",
            },
            "destination": {
                "code_iata": clock["destination"],
                "timezone": clock["dest_tz"],
                "name": "Example Hub",
                "city": "Example",
            },
            "scheduled_out": _iso(std),
            "estimated_out": _iso(std),
            "actual_out": None,
            "scheduled_off": _iso(off),
            "estimated_off": _iso(off),
            "actual_off": None,
            "scheduled_on": _iso(on),
            "estimated_on": _iso(on),
            "actual_on": None,
            "scheduled_in": _iso(sta),
            "estimated_in": _iso(sta),
            "actual_in": None,
            "status": "Scheduled",
            "progress_percent": None,
            "aircraft_type": "B738",
            "baggage_claim": "4",
            "gate_origin": "A12",
            "gate_destination": "B4",
            "terminal_origin": "1",
            "terminal_destination": "2",
        }]
    }


def _inbound(clock: dict, *, late_min: int = 0, cancelled: bool = False) -> dict:
    arr = clock["std"] - dt.timedelta(minutes=50) + dt.timedelta(minutes=late_min)
    return {
        "flights": [{
            "ident": clock["ident"][:-1] + "2" if clock["ident"][-1:].isdigit() else clock["ident"] + "I",
            "ident_iata": clock["ident"][:-1] + "2" if clock["ident"][-1:].isdigit() else clock["ident"] + "I",
            "fa_flight_id": f"{clock['ident']}-sim-in",
            "cancelled": cancelled,
            "diverted": False,
            "status": "Cancelled" if cancelled else "Arrived",
            "origin": {"code_iata": "SIN", "timezone": "Asia/Singapore"},
            "destination": {
                "code_iata": clock["origin"],
                "timezone": clock["origin_tz"],
            },
            "scheduled_in": _iso(clock["std"] - dt.timedelta(minutes=50)),
            "estimated_in": _iso(arr),
            "actual_in": _iso(arr) if not cancelled else None,
        }]
    }


def _mutate(clock: dict, scenario: str, now: dt.datetime, checkpoint: str) -> tuple[int, dict] | tuple:
    body = _template(clock)
    f = body["flights"][0]
    inbound = _inbound(clock)
    std, sta = clock["std"], clock["sta"]

    if scenario == "on_time":
        if now >= std:
            f["actual_out"] = _iso(std)
            f["actual_off"] = _iso(std + dt.timedelta(minutes=12))
            f["status"] = "En Route"
            f["progress_percent"] = 55
        if now >= sta:
            f["actual_on"] = _iso(sta)
            f["actual_in"] = _iso(sta + dt.timedelta(minutes=8))
            f["status"] = "Arrived"
            f["progress_percent"] = 100

    elif scenario == "inbound_late_recovers":
        if checkpoint in ("t_minus_3h",) or (std - dt.timedelta(hours=3) <= now < std - dt.timedelta(hours=1)):
            inbound = _inbound(clock, late_min=70)
        if now >= std:
            f["actual_out"] = _iso(std)
            f["actual_off"] = _iso(std + dt.timedelta(minutes=12))
            f["status"] = "En Route"
        if now >= sta:
            f["actual_on"] = _iso(sta)
            f["actual_in"] = _iso(sta)

    elif scenario == "big_delay":
        delay = dt.timedelta(minutes=95)
        f["estimated_out"] = _iso(std + delay)
        f["estimated_in"] = _iso(sta + delay)
        if now >= std + delay:
            f["actual_out"] = _iso(std + delay)
            f["actual_off"] = _iso(std + delay + dt.timedelta(minutes=12))
            f["status"] = "En Route"
        if now >= sta + delay:
            f["actual_on"] = _iso(sta + delay)
            f["actual_in"] = _iso(sta + delay)

    elif scenario == "gate_change":
        if now >= std - dt.timedelta(hours=6):
            f["gate_origin"] = "C7"
        if now >= std:
            f["actual_off"] = _iso(std + dt.timedelta(minutes=12))
            f["status"] = "En Route"
        if now >= sta:
            f["actual_on"] = _iso(sta)
            f["actual_in"] = _iso(sta)

    elif scenario == "cancelled_then_confirmed":
        if now >= std - dt.timedelta(hours=6):
            f["cancelled"] = True
            f["status"] = "Cancelled"

    elif scenario == "cancelled_then_reinstated":
        # Only the T−6h sighting is cancelled; the 30-min confirm sees it reinstated.
        if std - dt.timedelta(hours=6) <= now < std - dt.timedelta(hours=6) + dt.timedelta(minutes=20):
            f["cancelled"] = True
            f["status"] = "Cancelled"
        elif now >= std:
            f["actual_out"] = _iso(std)
            f["actual_off"] = _iso(std + dt.timedelta(minutes=12))
            f["status"] = "En Route"
        if now >= sta and not f["cancelled"]:
            f["actual_on"] = _iso(sta)
            f["actual_in"] = _iso(sta + dt.timedelta(minutes=8))
            f["status"] = "Arrived"

    elif scenario == "diverted":
        if now >= std:
            f["actual_off"] = _iso(std + dt.timedelta(minutes=12))
            f["status"] = "En Route"
        if now >= std + dt.timedelta(hours=1):
            f["diverted"] = True
            f["status"] = "Diverted"
            f["destination"]["code_iata"] = "CDG"

    elif scenario == "retimed":
        if now >= std - dt.timedelta(hours=6):
            f["estimated_out"] = _iso(std + dt.timedelta(minutes=40))
            f["estimated_in"] = _iso(sta + dt.timedelta(minutes=40))
        if now >= std + dt.timedelta(minutes=40):
            f["actual_off"] = _iso(std + dt.timedelta(minutes=52))
            f["status"] = "En Route"
        if now >= sta + dt.timedelta(minutes=40):
            f["actual_on"] = _iso(sta + dt.timedelta(minutes=40))
            f["actual_in"] = _iso(sta + dt.timedelta(minutes=48))

    elif scenario == "upstream_outage":
        return "outage", None, None

    mapping = {
        f"/flights/{clock['ident']}": (200, body),
        f"/flights/{body['flights'][0]['ident_icao']}": (200, body),
        f"/flights/{body['flights'][0]['inbound_fa_flight_id']}": (200, inbound),
    }
    return mapping, body, inbound


def _fetch_for(mapping: dict, now: dt.datetime):
    def fetch(path, params):
        if path in mapping:
            return mapping[path]
        for prefix in sorted(mapping, key=len, reverse=True):
            if path.startswith(prefix):
                return mapping[prefix]
        return 404, {"title": "Not Found"}
    return fetch


def _outage_fetch(ident, now):
    return _fetch_for({f"/flights/{ident}": (503, {"title": "unavailable"})}, now)


def run_simulate(data: dict) -> dict:
    scenario = str(data.get("scenario") or "on_time").strip().lower()
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario (choose one of {', '.join(SCENARIOS)})")
    clock = resolve_clock(data)
    ident, std, sta = clock["ident"], clock["std"], clock["sta"]
    origin = clock["origin"]
    date = clock["date"]
    store = MemoryStore()
    captured: list[dict] = []

    def deliver(url, payload, secret, **_k):
        captured.append(payload)
        return {"ok": True, "http": 200, "attempts": 1}

    start_now = std - dt.timedelta(hours=30)
    if scenario == "upstream_outage":
        fetch0 = _outage_fetch(ident, start_now)
    else:
        mapping, _, _ = _mutate(clock, scenario, start_now, "t_minus_24h")
        fetch0 = _fetch_for(mapping, start_now)

    started = start_track(
        {
            "flight": ident,
            "date": date,
            "origin": origin,
            "webhook_url": "https://example.test/sink",
            "ref": data.get("ref") or "simulate",
            "alert_delay_min": data.get("alert_delay_min") or 45,
        },
        store=store,
        now=start_now,
        fetch=fetch0,
        deliver=deliver,
        webhook_secret="sim-secret",
    )
    start_rec = load_track(store, started["tracking_id"]) or {}
    start_snap = start_rec.get("last_snapshot") or {}
    start_calls = len(start_snap.get("calls") or [])
    timeline = []
    prev_snap = None
    now = start_now
    steps = 0
    rec_id = started["tracking_id"]
    while steps < 16:
        rec = load_track(store, rec_id)
        if not rec:
            break
        nxt = rec.get("next_check_at")
        checkpoint = rec.get("checkpoint")
        if rec.get("ended") and steps > 0:
            break
        if nxt:
            now = parse_time(nxt) or now
        else:
            now = now + dt.timedelta(minutes=15)
        mapping_or_flag = _mutate(clock, scenario, now, checkpoint or "")
        if mapping_or_flag[0] == "outage":
            fetch = _outage_fetch(ident, now)
            would_calls = [{"path": f"/flights/{ident}", "would": True, "http": 503}]
        else:
            mapping, body, inbound = mapping_or_flag
            fetch = _fetch_for(mapping, now)
            would_calls = [{"path": p, "would": True, "http": 200} for p in mapping]
        captured.clear()
        summary = tick(
            store=store, now=now, fetch=fetch, deliver=deliver,
            webhook_secret="sim-secret", budget=5.0,
        )
        rec = load_track(store, rec_id) or rec
        snap = rec.get("last_snapshot") or {}
        names = [p.get("event") for p in captured] or detect_events(prev_snap, snap, first=False)
        if not names:
            event_label = "silent"
            payloads = []
        else:
            event_label = ", ".join(names)
            payloads = list(captured) or [event_payload(n, rec, snap) for n in names]
        local = None
        if rec.get("next_check_local"):
            local = rec["next_check_local"]
        actual_calls = list(snap.get("calls") or [])
        timeline.append({
            "at": local_dict(now, clock["origin_tz"], origin),
            "checkpoint": snap.get("checkpoint") or checkpoint,
            "phase": snap.get("phase"),
            "band": snap.get("band"),
            "status": snap.get("status"),
            "summary": snap.get("summary"),
            "calls": len(actual_calls),
            "call_paths": [c.get("path") for c in actual_calls],
            "calls_would": would_calls[:3],
            "event": event_label,
            "payloads": payloads,
            "tick": {"ran": summary.get("ran"), "events": summary.get("events")},
        })
        prev_snap = snap
        steps += 1
        if rec.get("ended") or (snap.get("error") and rec.get("fail_count", 0) >= 3):
            break
        if not rec.get("next_check_at"):
            break

    calls_total = start_calls + sum(int(step.get("calls") or 0) for step in timeline)
    return {
        "ok": True,
        "scenario": scenario,
        "flight": ident,
        "origin": origin,
        "destination": clock["destination"],
        "date": date,
        "std_human": local_dict(clock["std"], clock["origin_tz"], origin),
        "tracking_id": rec_id,
        "cost_usd": 0.0,
        "calls_total": calls_total,
        "timeline": timeline,
    }


