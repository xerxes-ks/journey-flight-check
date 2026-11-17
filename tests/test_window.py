"""AeroAPI date-window clamp and too_early (no call beyond T-48h)."""

import datetime as dt

from flightcheck import check_flight
from flightcheck.adapters.aeroapi import date_window
from flightcheck.model import UTC, parse_time
from tests.conftest import NEAR_DEP, make_fetch, scenario_fetch


def test_date_window_end_clamped_to_now_plus_47h():
    now = parse_time("2027-03-15T16:30:00Z")
    # Ticket date two days out; unconstrained end would exceed AeroAPI's ~2-day cap.
    start, end = date_window(dt.date(2027, 3, 17), now, origin_tz="America/Los_Angeles")
    assert end <= now + dt.timedelta(hours=47) + dt.timedelta(seconds=1)
    assert (end - now) <= dt.timedelta(hours=47)


def test_too_early_skips_api():
    called = []

    def fetch(path, params):
        called.append((path, params))
        return 200, {"flights": []}

    r = check_flight(
        {"flight": "XX123", "date": "2027-03-22", "origin": "LAX"},
        fetch=fetch,
        now=parse_time("2027-03-15T16:30:00Z"),
    )
    assert called == []
    assert r.flight.status == "too_early"
    assert r.prediction.band == "unknown"
    assert r.ok is True
    assert r.calls == []
    assert r.next_check_local is not None
    assert r.next_check_local["tz"] == "America/Los_Angeles"
    assert r.next_check_in_minutes > 0
    # First lookup is local noon 22 Mar LAX minus 24h → noon 21 Mar local.
    assert "21 Mar" in r.next_check_local["human"] or "2027-03-21" in r.next_check_local["local"]
    assert "Live checks open" in r.summary
    assert "21 Mar" in r.summary or "LAX time" in r.summary
    assert r.cost_usd_est == 0
    assert r.summary.startswith("XX123")
    assert "24 hours" in r.summary
    # Jump to visibility (days away), not an hourly retry.
    assert r.next_check_in_minutes > 60


def test_too_early_cannot_origin_mismatch_without_aeroapi():
    """too_early skips AeroAPI, so a wrong origin cannot be rejected yet."""
    called = []

    def fetch(path, params):
        called.append(path)
        return 200, {"flights": []}

    r = check_flight(
        {"flight": "XX123", "date": "2027-03-22", "origin": "LHR"},
        fetch=fetch,
        now=parse_time("2027-03-15T16:30:00Z"),
    )
    assert called == []
    assert r.ok is True
    assert r.flight.status == "too_early"
    assert r.error is None or r.error.get("code") != "origin_mismatch"


def test_unclamped_window_is_rejected_by_fake_fetch():
    """If date_window forgot to clamp, AeroAPI-shaped fetch returns 400."""
    now = parse_time(NEAR_DEP)
    far_end = (now + dt.timedelta(hours=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fetch = make_fetch({"/flights/XX123": (200, {"flights": []})}, now=now)
    status, body = fetch("/flights/XX123", {
        "start": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": far_end,
        "ident_type": "designator",
    })
    assert status == 400
    assert "2 days" in body["detail"]


def test_clamped_lookup_near_horizon_does_not_400():
    now = parse_time("2027-03-15T16:30:00Z")
    r = check_flight(
        {"flight": "XX123", "date": "2027-03-17", "origin": "LAX"},
        fetch=scenario_fetch(now=now),
        now=now,
    )
    # Window was clamped (so fetch did not 400). The 15 Mar instance is not
    # on 17 Mar local, so this is a no-data/unknown result — not a 400.
    assert not any(c.get("http") == 400 for c in r.calls)
    if r.calls:
        end = parse_time(r.calls[0]["params"]["end"])
        assert end <= now + dt.timedelta(hours=47) + dt.timedelta(seconds=1)
