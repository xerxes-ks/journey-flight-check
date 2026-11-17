"""A cron following next_check must never stall before a final result."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy

from flightcheck import check_flight
from flightcheck.model import parse_time
from tests.conftest import load_fx, make_fetch

STD = parse_time("2027-12-12T21:25:00Z")  # 02:55 IST on 13 Dec
STA = parse_time("2027-12-13T04:55:00Z")


def _evolving_fetch(now: dt.datetime):
    body = deepcopy(load_fx("xx754-del.json"))
    for f in body["flights"]:
        if not str(f.get("fa_flight_id", "")).endswith("0013"):
            continue
        std = parse_time(f["scheduled_out"])
        sta = parse_time(f["scheduled_in"])
        f["actual_out"] = None
        f["actual_in"] = None
        f["estimated_out"] = f["scheduled_out"]
        f["estimated_in"] = f["scheduled_in"]
        if now >= std:
            f["actual_out"] = std.strftime("%Y-%m-%dT%H:%M:%SZ")
            f["actual_off"] = (std + dt.timedelta(minutes=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
            f["status"] = "En Route"
        if now >= sta:
            f["actual_on"] = sta.strftime("%Y-%m-%dT%H:%M:%SZ")
            f["actual_in"] = (sta + dt.timedelta(minutes=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
            f["status"] = "Arrived"
    return make_fetch({
        "/flights/XX754": (200, body),
        "/flights/XXX754": (200, body),
    }, now=now)


def test_cron_follows_next_check_from_30_days_out_to_landing():
    now = STD - dt.timedelta(days=30)
    seen = set()
    checks = []
    steps = 0
    prev_cp, prev_att, prev_phase = None, 0, None
    while steps < 40:
        r = check_flight(
            {
                "flight": "XX754",
                "date": "2027-12-13",
                "origin": "DEL",
                "previous_checkpoint": prev_cp,
                "previous_phase": prev_phase,
                "attempts": prev_att,
            },
            fetch=_evolving_fetch(now),
            now=now,
        )
        seen.add(r.flight.status)
        checks.append(r.checkpoint)
        prev_cp, prev_att, prev_phase = r.checkpoint, r.attempts, r.phase
        if r.final:
            assert r.next_check_local is None
            assert r.flight.status == "landed"
            break
        assert r.next_check_local is not None, (
            f"cron stalled at {now.isoformat()} status={r.flight.status} error={r.error}"
        )
        assert r.next_check_in_minutes is not None and r.next_check_in_minutes > 0
        now = now + dt.timedelta(minutes=r.next_check_in_minutes)
        steps += 1
    else:
        raise AssertionError(f"cron did not reach landing in {steps} steps; last {now} seen={seen}")
    assert "too_early" in seen
    assert "landed" in seen
    assert seen & {"unknown", "scheduled", "active"}
    assert 6 <= steps <= 16
    assert "t_minus_24h" in checks
    assert "landing" in checks or "done" in checks


def test_gap_before_visibility_does_not_kill_cron():
    """02:55 local: midnight-48h call returns empty; next_check must still be set."""
    # DEL 13 Dec 00:00 IST − 48h = 11 Dec 00:00 IST = 10 Dec 18:30Z
    now = parse_time("2027-12-10T18:30:00Z")
    r = check_flight(
        {"flight": "XX754", "date": "2027-12-13", "origin": "DEL"},
        fetch=_evolving_fetch(now),
        now=now,
    )
    assert r.flight.status in ("unknown", "too_early")
    assert r.next_check_local is not None
    assert r.next_check_in_minutes and r.next_check_in_minutes > 0
    assert r.next_check_local["tz"] == "Asia/Kolkata"
