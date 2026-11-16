"""Essential cadence, phase, impact, partial budget, and summary grammar."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy

from flightcheck import check_flight, essential_plan
from flightcheck.cadence import poll_interval
from flightcheck.model import UTC, parse_time
from tests.conftest import NEAR_DEP, load_fx, make_fetch, scenario_fetch

STD = parse_time("2027-12-12T21:25:00Z")  # 02:55 origin-local on 13 Dec
STA = parse_time("2027-12-13T04:55:00Z")


def test_essential_plan_0255_sequence():
    times = []
    now = STD - dt.timedelta(days=10)
    prev, att = None, 0
    actual_off = actual_on = None
    steps = 0
    while steps < 20:
        plan = essential_plan(
            now=now, std=STD, sta=STA,
            expected_off=STD, expected_arr=STA,
            actual_off=actual_off, actual_on=actual_on,
            previous_checkpoint=prev, attempts=att,
        )
        times.append((plan.checkpoint, now.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")))
        if plan.final:
            break
        prev, att = plan.checkpoint, plan.attempts
        if plan.checkpoint == "takeoff":
            actual_off = STD + dt.timedelta(minutes=12)
        if plan.checkpoint == "landing":
            actual_on = STA
        now = plan.next_at
        steps += 1
    names = [c for c, _ in times]
    assert names[0] == "t_minus_24h"
    assert "t_minus_6h" in names
    assert "t_minus_3h" in names
    assert "t_minus_1h" in names
    assert "takeoff" in names
    assert names[-1] == "done"
    assert 6 <= len(names) <= 9


def test_too_early_until_t_minus_24h():
    r = check_flight(
        {"flight": "XX754", "date": "2027-12-13", "origin": "DEL"},
        fetch=scenario_fetch(now="2027-12-11T12:00:00Z"),
        now=parse_time("2027-12-11T12:00:00Z"),
    )
    assert r.flight.status == "too_early"
    assert r.checkpoint == "t_minus_24h"
    assert r.calls == []
    # STD unknown: T is origin-local midnight of the ticket date.
    assert r.next_check_local["local"].startswith("2027-12-12T00:00")


def test_inbound_only_at_t_minus_3h_and_1h():
    t6 = parse_time("2027-03-15T12:00:00Z")  # T-6h vs XX123 STD 18:00Z
    r6 = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=scenario_fetch(now=t6),
        now=t6,
    )
    assert r6.checkpoint == "t_minus_6h"
    assert r6.inbound is None
    assert len(r6.calls) == 1

    t3 = parse_time(NEAR_DEP)  # T-90 min, in the T-3h window
    r3 = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=scenario_fetch(),
        now=t3,
    )
    assert r3.checkpoint == "t_minus_3h"
    assert r3.inbound is not None
    assert len(r3.calls) == 2


def test_takeoff_retry_then_landing():
    std = parse_time("2027-03-15T18:00:00Z")
    takeoff_at = std + dt.timedelta(minutes=30)
    body = deepcopy(load_fx("xx123.json"))
    body["flights"][0]["inbound_fa_flight_id"] = None
    r1 = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=make_fetch({"/flights/XX123": (200, body)}),
        now=takeoff_at,
    )
    assert r1.checkpoint == "takeoff"
    assert r1.attempts == 1
    assert r1.phase in ("scheduled", "at_gate_late")
    assert r1.next_check_in_minutes == 30

    r2 = check_flight(
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "previous_checkpoint": r1.checkpoint,
            "attempts": r1.attempts,
        },
        fetch=make_fetch({"/flights/XX123": (200, body)}),
        now=takeoff_at + dt.timedelta(minutes=30),
    )
    assert r2.checkpoint == "takeoff"
    assert r2.next_check_local is not None
    assert r2.next_check_in_minutes > 0


def test_taxiing_is_not_in_the_air():
    body = deepcopy(load_fx("xx123.json"))
    f = body["flights"][0]
    f["actual_out"] = "2027-03-15T18:05:00Z"
    f["actual_off"] = None
    f["inbound_fa_flight_id"] = None
    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=make_fetch({"/flights/XX123": (200, body)}),
        now=parse_time("2027-03-15T18:10:00Z"),
    )
    assert r.phase == "taxiing"
    assert r.flight.status == "scheduled"
    assert "taxiing" in r.summary
    assert "in the air" not in r.summary
    assert r.flight.to_dict()["actual_out"]["local"]
    assert r.flight.to_dict()["actual_off"] is None


def test_airborne_exposes_runway_times_and_status_text():
    body = deepcopy(load_fx("xx123.json"))
    f = body["flights"][0]
    f["actual_out"] = "2027-03-15T18:05:00Z"
    f["actual_off"] = "2027-03-15T18:12:00Z"
    f["progress_percent"] = 40
    f["status"] = "En Route"
    f["inbound_fa_flight_id"] = None
    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=make_fetch({"/flights/XX123": (200, body)}),
        now=parse_time("2027-03-15T18:20:00Z"),
    )
    assert r.phase == "airborne"
    assert r.flight.status == "active"
    assert r.flight.status_text == "En Route"
    assert r.flight.progress_percent == 40
    assert r.flight.act_off is not None
    assert "in the air" in r.summary


def test_cancelled_rechecks_then_final():
    first = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=scenario_fetch(flight="xx123-cancelled.json"),
        now=parse_time(NEAR_DEP),
    )
    assert first.flight.cancelled
    assert first.final is False
    assert "shows as cancelled" in first.summary
    assert first.next_check_in_minutes == 30
    second = check_flight(
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "previous_checkpoint": first.checkpoint,
            "attempts": first.attempts,
        },
        fetch=scenario_fetch(flight="xx123-cancelled.json"),
        now=parse_time(NEAR_DEP) + dt.timedelta(minutes=30),
    )
    assert second.final is True
    assert second.checkpoint == "done"
    assert second.next_check_local is None


def test_inbound_cancelled_raises_watch():
    inbound = deepcopy(load_fx("xx122.json"))
    inbound["flights"][0]["cancelled"] = True
    inbound["flights"][0]["status"] = "Cancelled"
    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=scenario_fetch(inbound="xx122.json"),
        now=parse_time(NEAR_DEP),
    )
    # inject cancelled inbound via custom fetch
    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=make_fetch({
            "/flights/XX123": (200, load_fx("xx123.json")),
            "/flights/XXX123": (200, load_fx("xx123.json")),
            "/flights/XXX122-1700000000-airline-0002": (200, inbound),
        }),
        now=parse_time(NEAR_DEP),
    )
    assert r.inbound and r.inbound.cancelled
    assert r.prediction.band in ("watch", "at_risk")
    assert "cancelled" in " ".join(r.prediction.reasons)


def test_self_transfer_defaults_mct_90_and_impact_band():
    r = check_flight(
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "onward": {
                "flight": "YY456",
                "date": "2027-03-15",
                "separate_ticket": True,
            },
        },
        fetch=scenario_fetch(),
        now=parse_time(NEAR_DEP),
    )
    assert r.connection.required_min == 90
    assert r.impact_band == r.connection.band
    assert r.band == r.prediction.band


def test_alert_delay_min_controls_punctuality_band():
    body = deepcopy(load_fx("xx123-airline-delay.json"))
    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA", "alert_delay_min": 15},
        fetch=make_fetch({
            "/flights/XX123": (200, body),
            "/flights/XXX123": (200, body),
            "/flights/XXX122-1700000000-airline-0002": (200, load_fx("xx122.json")),
        }),
        now=parse_time(NEAR_DEP),
    )
    assert r.prediction.arr_delay_min == 20
    assert r.prediction.band == "at_risk"


def test_partial_result_when_inbound_blows_budget():
    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA", "budget": 0.005},
        fetch=scenario_fetch(),
        now=parse_time(NEAR_DEP),
    )
    assert r.flight.ident_iata == "XX123"
    assert r.warning["code"] == "budget_exceeded"
    assert r.ok is True
    assert len(r.calls) == 1


def test_retryable_error_summary_and_15_min_retry():
    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=make_fetch({"/flights/XX123": (503, {"title": "engine fault detail xyz"})}),
        now=parse_time(NEAR_DEP),
    )
    assert r.error["retryable"] is True
    assert "engine fault" not in r.summary
    assert "temporarily unavailable" in r.summary
    assert r.next_check_in_minutes == 15


def test_connection_at_risk_does_not_lowercase_ident():
    r = check_flight(
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "onward": {
                "flight": "YY456",
                "date": "2027-03-15",
                "min_connect_min": 60,
                "terminal_change": True,
                "terminal_change_min": 20,
                "bags_recheck": True,
                "checkin_close_min": 45,
                "separate_ticket": True,
            },
        },
        fetch=scenario_fetch(inbound="xx122-late.json"),
        now=parse_time(NEAR_DEP),
    )
    assert r.connection.band == "at_risk"
    assert "YY456" in r.summary
    assert "yY456" not in r.summary
    assert "incl." not in r.summary
    assert "including" in r.connection.explain


def test_gate_change_and_retime_in_summary():
    body = deepcopy(load_fx("xx123.json"))
    f = body["flights"][0]
    f["estimated_out"] = "2027-03-15T18:40:00Z"  # +40 min
    f["gate_origin"] = "C7"
    f["inbound_fa_flight_id"] = None
    r = check_flight(
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "previous_gate": "A12",
        },
        fetch=make_fetch({"/flights/XX123": (200, body)}),
        now=parse_time(NEAR_DEP),
    )
    low = r.summary.lower()
    assert "c7" in low
    assert "a12" in low
    assert "retimed" in low


def test_dense_cadence_still_available():
    dep = dt.datetime(2027, 3, 15, 18, 0, tzinfo=UTC)
    now = dep - dt.timedelta(hours=2)
    assert poll_interval(dep, now, False) == 15
    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA", "cadence": "dense"},
        fetch=scenario_fetch(),
        now=parse_time(NEAR_DEP),
    )
    assert r.next_check_in_minutes == 15
