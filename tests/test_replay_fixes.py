"""Regression tests for replay findings on 20e4036 (synthetic idents only)."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy

from flightcheck import check_flight, essential_plan
from flightcheck.model import parse_time
from tests.conftest import NEAR_DEP, load_fx, make_fetch, scenario_fetch

STD = parse_time("2027-03-15T18:00:00Z")
STA = parse_time("2027-03-15T21:00:00Z")


def _airborne_body(*, eta: str, actual_off: str = "2027-03-15T18:12:00Z"):
    body = deepcopy(load_fx("xx123.json"))
    f = body["flights"][0]
    f["actual_out"] = "2027-03-15T18:05:00Z"
    f["actual_off"] = actual_off
    f["actual_on"] = None
    f["actual_in"] = None
    f["estimated_in"] = eta
    f["estimated_on"] = eta
    f["status"] = "En Route"
    f["progress_percent"] = 55
    f["inbound_fa_flight_id"] = None
    return body


def test_1_stale_eta_retries_at_least_20_min_and_never_final_while_airborne():
    stale = _airborne_body(eta="2027-03-15T20:50:00Z")
    now = parse_time("2027-03-15T21:05:00Z")  # ETA already 15 min in the past
    r1 = check_flight(
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "previous_checkpoint": "takeoff",
            "previous_phase": "airborne",
            "attempts": 1,
        },
        fetch=make_fetch({"/flights/XX123": (200, stale)}),
        now=now,
    )
    assert r1.phase == "airborne"
    assert r1.final is False
    assert r1.checkpoint == "landing"
    assert r1.next_check_in_minutes >= 20
    assert r1.next_check_local is not None

    r2 = check_flight(
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "previous_checkpoint": r1.checkpoint,
            "previous_phase": r1.phase,
            "attempts": r1.attempts,
        },
        fetch=make_fetch({"/flights/XX123": (200, stale)}),
        now=now + dt.timedelta(minutes=r1.next_check_in_minutes),
    )
    assert r2.final is False
    assert r2.phase == "airborne"
    gap = (parse_time(r2.checked_at) - parse_time(r1.checked_at)).total_seconds() / 60
    assert gap >= 20

    fresh = _airborne_body(eta="2027-03-15T22:00:00Z")
    r3 = check_flight(
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "previous_checkpoint": r2.checkpoint,
            "previous_phase": r2.phase,
            "attempts": r2.attempts,
        },
        fetch=make_fetch({"/flights/XX123": (200, fresh)}),
        now=parse_time(r2.checked_at),
    )
    assert r3.final is False
    assert r3.phase == "airborne"
    # Follow the updated ETA (+10), not a 1-min retry.
    assert r3.next_check_in_minutes >= 20
    eta_plus = parse_time("2027-03-15T22:10:00Z")
    nxt = parse_time(r3.next_check_local["local"])
    assert nxt >= eta_plus - dt.timedelta(minutes=1)


def test_1_airborne_checks_are_capped_never_marked_final():
    body = _airborne_body(eta="2027-03-15T20:00:00Z")
    now = parse_time("2027-03-15T21:00:00Z")
    r = check_flight(
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "previous_checkpoint": "landing",
            "previous_phase": "airborne",
            "attempts": 30,
        },
        fetch=make_fetch({"/flights/XX123": (200, body)}),
        now=now,
    )
    assert r.final is False
    assert r.checkpoint == "landing"
    assert r.next_check_in_minutes >= 20


def test_2_past_std_at_gate_is_departure_delayed_not_on_track():
    body = deepcopy(load_fx("xx123.json"))
    body["flights"][0]["inbound_fa_flight_id"] = None
    now = STD + dt.timedelta(minutes=25)
    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=make_fetch({"/flights/XX123": (200, body)}),
        now=now,
    )
    assert r.phase == "at_gate_late"
    assert r.checkpoint == "takeoff"
    assert r.final is False
    assert r.next_check_in_minutes == 30
    low = r.summary.lower()
    assert "departure delayed" in low
    assert "25 min" in low
    assert "on track, departing" not in low
    assert r.prediction.dep_delay_min >= 25


def test_2_ground_rechecks_do_not_jump_to_landing():
    body = deepcopy(load_fx("xx123.json"))
    body["flights"][0]["inbound_fa_flight_id"] = None
    takeoff_at = STD + dt.timedelta(minutes=30)
    r1 = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=make_fetch({"/flights/XX123": (200, body)}),
        now=takeoff_at,
    )
    r2 = check_flight(
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "previous_checkpoint": r1.checkpoint,
            "previous_phase": r1.phase,
            "attempts": r1.attempts,
        },
        fetch=make_fetch({"/flights/XX123": (200, body)}),
        now=takeoff_at + dt.timedelta(minutes=30),
    )
    assert r1.checkpoint == "takeoff"
    assert r2.checkpoint == "takeoff"
    assert r2.next_check_in_minutes == 30
    assert r2.phase == "at_gate_late"


def test_3_t_minus_30m_is_flight_only():
    now = STD - dt.timedelta(minutes=30)
    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=scenario_fetch(now=now),
        now=now,
    )
    assert r.checkpoint == "t_minus_30m"
    assert r.inbound is None
    assert len(r.calls) == 1
    paths = [c["path"] for c in r.calls]
    assert any(p.endswith("/XX123") or p.endswith("/XXX123") for p in paths)
    assert not any("XXX122" in p for p in paths)


def test_4_landing_that_sees_onblock_is_final():
    body = deepcopy(load_fx("xx123.json"))
    f = body["flights"][0]
    f["actual_out"] = "2027-03-15T18:10:00Z"
    f["actual_off"] = "2027-03-15T18:12:00Z"
    f["actual_on"] = "2027-03-15T21:00:00Z"
    f["actual_in"] = "2027-03-15T21:08:00Z"
    f["inbound_fa_flight_id"] = None
    published = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=make_fetch({"/flights/XX123": (200, body)}),
        now=parse_time("2027-03-15T21:10:00Z"),
    )
    assert published.final is True
    assert published.checkpoint == "done"
    assert published.next_check_local is None
    assert "gate B4" in published.summary
    assert "baggage belt 4" in published.summary

    f["actual_in"] = None
    f["gate_destination"] = None
    f["baggage_claim"] = None
    f["terminal_destination"] = None
    unpublished = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=make_fetch({"/flights/XX123": (200, body)}),
        now=parse_time("2027-03-15T21:10:00Z"),
    )
    assert unpublished.final is True
    assert unpublished.checkpoint == "done"
    assert unpublished.next_check_local is None
    assert "gate B4" not in unpublished.summary
    assert "baggage belt" not in unpublished.summary.lower()


def test_5_onward_skipped_outside_47h_and_except_at_t3_landed_or_watch():
    onward = {
        "flight": "YY456",
        "date": "2027-03-20",
        "origin": "XXB",
        "min_connect_min": 45,
    }
    t3 = parse_time(NEAR_DEP)
    called = []

    def fetch(path, params):
        called.append(path)
        if "YY456" in path or "YYY456" in path:
            return 200, load_fx("yy456.json")
        if "XXX122" in path:
            return 200, load_fx("xx122.json")
        return 200, load_fx("xx123.json")

    r_far = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA", "onward": onward},
        fetch=fetch,
        now=t3,
    )
    assert r_far.connection is None
    assert not any("YY456" in p or "YYY456" in p for p in called)

    called.clear()
    near_onward = {"flight": "YY456", "date": "2027-03-15", "origin": "XXB", "min_connect_min": 45}
    t6 = parse_time("2027-03-15T12:00:00Z")
    r6 = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA", "onward": near_onward},
        fetch=fetch,
        now=t6,
    )
    assert r6.checkpoint == "t_minus_6h"
    assert r6.prediction.band == "on_track"
    assert r6.connection is None
    assert not any("YY456" in p or "YYY456" in p for p in called)

    called.clear()
    r3 = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA", "onward": near_onward},
        fetch=fetch,
        now=t3,
    )
    assert r3.checkpoint == "t_minus_3h"
    assert r3.connection is not None
    assert any("YY456" in p or "YYY456" in p for p in called)

    called.clear()
    delayed = deepcopy(load_fx("xx123-airline-delay.json"))
    delayed["flights"][0]["inbound_fa_flight_id"] = None
    r_watch = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA", "onward": near_onward},
        fetch=make_fetch({
            "/flights/XX123": (200, delayed),
            "/flights/YY456": (200, load_fx("yy456.json")),
            "/flights/YYY456": (200, load_fx("yy456.json")),
        }),
        now=t6,
    )
    assert r_watch.prediction.band in ("watch", "at_risk")
    assert r_watch.connection is not None


def test_6_retime_says_moved_from_local_clocks():
    body = deepcopy(load_fx("xx123.json"))
    f = body["flights"][0]
    f["estimated_out"] = "2027-03-15T20:00:00Z"  # 16:00 XXA vs 14:00
    f["inbound_fa_flight_id"] = None
    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=make_fetch({"/flights/XX123": (200, body)}),
        now=parse_time(NEAR_DEP),
    )
    assert "Moved from 14:00 to 16:00 (XXA time)" in r.summary


def test_7_estimate_retime_reanchors_t3_t1_t30():
    std, sta = STD, STA
    est = std + dt.timedelta(hours=2)
    now = std - dt.timedelta(minutes=30)  # T-30m of STD, T-2.5h of estimate
    plan = essential_plan(
        now=now, std=std, sta=sta,
        expected_off=est, expected_arr=sta + dt.timedelta(hours=2),
    )
    assert plan.checkpoint == "t_minus_3h"
    assert plan.next_at == est - dt.timedelta(hours=1)
    later = essential_plan(
        now=est - dt.timedelta(minutes=30), std=std, sta=sta,
        expected_off=est, expected_arr=sta + dt.timedelta(hours=2),
    )
    assert later.checkpoint == "t_minus_30m"


def test_8_summary_leads_with_connection_then_delay():
    r = check_flight(
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "onward": {"flight": "YY456", "date": "2027-03-15", "min_connect_min": 45},
        },
        fetch=scenario_fetch(),
        now=parse_time(NEAR_DEP),
    )
    assert r.connection is not None
    assert r.summary.lower().startswith("connection to yy456 is safe")
    assert "on track" in r.summary.lower()

    late = check_flight(
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "onward": {
                "flight": "YY456",
                "date": "2027-03-15",
                "min_connect_min": 60,
                "terminal_change": True,
                "bags_recheck": True,
                "separate_ticket": True,
            },
        },
        fetch=scenario_fetch(inbound="xx122-late.json"),
        now=parse_time(NEAR_DEP),
    )
    assert late.connection.band == "at_risk"
    assert late.summary.lower().startswith("connection to yy456 is at risk")
    assert "expected departure delay" in late.summary.lower() or "at risk" in late.summary.lower()


def test_9a_summary_mentions_skipped_data_when_over_budget():
    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA", "budget": 0.005},
        fetch=scenario_fetch(),
        now=parse_time(NEAR_DEP),
    )
    assert r.warning["code"] == "budget_exceeded"
    assert "skipped" in r.summary.lower()


def test_9b_budget_below_one_call_returns_error_not_exception():
    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA", "budget": 0.001},
        fetch=scenario_fetch(),
        now=parse_time(NEAR_DEP),
    )
    assert r.error is not None
    assert r.error["code"] == "budget_exceeded"
    assert r.ok is False
    assert r.calls == []


def test_9c_retime_sentence_is_once_and_capitalized():
    body = deepcopy(load_fx("xx123.json"))
    f = body["flights"][0]
    f["estimated_out"] = "2027-03-15T20:00:00Z"
    f["inbound_fa_flight_id"] = None
    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=make_fetch({"/flights/XX123": (200, body)}),
        now=parse_time(NEAR_DEP),
    )
    assert r.summary.lower().count("moved from") == 1
    assert "Moved from" in r.summary
    assert "14:00 to 16:00" in r.summary


def test_9d_cancelled_and_error_keep_the_current_checkpoint():
    cancelled = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=scenario_fetch(flight="xx123-cancelled.json"),
        now=parse_time(NEAR_DEP),
    )
    assert cancelled.flight.cancelled
    assert cancelled.checkpoint == "t_minus_3h"
    assert cancelled.final is False

    timed_out = check_flight(
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "previous_checkpoint": "t_minus_1h",
            "attempts": 0,
        },
        fetch=lambda path, params: (_ for _ in ()).throw(TimeoutError("hang")),
        now=parse_time(NEAR_DEP),
    )
    assert timed_out.error["code"] == "timeout"
    assert timed_out.checkpoint == "t_minus_1h"
    assert timed_out.final is False


def test_9e_timeout_message_is_a_plain_sentence():
    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=lambda path, params: (_ for _ in ()).throw(TimeoutError("timeout")),
        now=parse_time(NEAR_DEP),
    )
    msg = r.error["message"]
    assert msg == "The live status request timed out."
    assert "AeroAPI timeout" not in msg
    assert msg[0].isupper() and msg.endswith(".")


def test_9f_no_call_before_t_minus_24h():
    called = []

    def fetch(path, params):
        called.append(path)
        return 200, load_fx("xx123.json")

    # 21 Jun 00:00 BST, STD 09:40 LHR on 22 Jun — before T−24h.
    r = check_flight(
        {"flight": "XX123", "date": "2027-06-22", "origin": "LHR"},
        fetch=fetch,
        now=parse_time("2027-06-20T23:00:00Z"),
    )
    assert called == []
    assert r.flight.status == "too_early"
    assert r.checkpoint == "t_minus_24h"
    assert r.calls == []
    assert r.next_check_local is not None
    assert r.next_check_in_minutes > 0
