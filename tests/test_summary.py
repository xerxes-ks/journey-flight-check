"""Summary grammar, top-level fields, landed/airborne wording, connection dates."""

from copy import deepcopy

from flightcheck import check_flight
from flightcheck.model import parse_time
from tests.conftest import NEAR_DEP, load_fx, make_fetch, scenario_fetch


def run(fetch, **extra):
    payload = {
        "flight": "XX123",
        "date": "2027-03-15",
        "origin": "XXA",
        "budget": 0.05,
        **extra,
    }
    return check_flight(payload, fetch=fetch, now=parse_time(NEAR_DEP))


def test_on_track_summary_uses_human_band():
    r = run(scenario_fetch())
    assert "on track" in r.summary
    assert "worth watching" not in r.summary
    assert r.status == "scheduled"
    assert r.band == "on_track"
    assert r.final is False
    d = r.to_dict()
    assert d["status"] == "scheduled"
    assert d["band"] == "on_track"
    assert d["final"] is False


def test_watch_and_at_risk_summaries_are_not_run_ons():
    r = run(scenario_fetch(flight="xx123-airline-delay.json"))
    assert r.prediction.band == "watch"
    assert "worth watching" in r.summary
    assert "Expected departure delay 20 min." in r.summary
    assert "worth watching:" not in r.summary

    late = run(scenario_fetch(inbound="xx122-late.json"))
    assert late.prediction.band == "at_risk"
    assert "at risk." in late.summary
    assert "Expected departure delay 55 min." in late.summary
    # reason and delay are separate sentences, not glued together
    assert "time) expected" not in late.summary.lower()


def test_airborne_summary_includes_destination_local_arrival():
    body = load_fx("xx123.json")
    f = body["flights"][0]
    f["actual_out"] = "2027-03-15T18:05:00Z"
    f["estimated_in"] = "2027-03-15T21:40:00Z"
    f["inbound_fa_flight_id"] = None
    r = run(make_fetch({"/flights/XX123": (200, body)}))
    assert r.flight.status == "active"
    assert "in the air" in r.summary
    assert "estimated arrival" in r.summary
    assert "XXB time" in r.summary
    assert "16:40" in r.summary


def test_landed_summary_includes_terminal_gate_belt():
    body = load_fx("xx123.json")
    f = body["flights"][0]
    f["actual_out"] = "2027-03-15T18:10:00Z"
    f["actual_in"] = "2027-03-15T21:12:00Z"
    f["inbound_fa_flight_id"] = None
    r = run(make_fetch({"/flights/XX123": (200, body)}))
    assert r.final
    assert "landed" in r.summary
    assert "terminal 2" in r.summary
    assert "gate B4" in r.summary
    assert "baggage belt 4" in r.summary
    assert "XXB time" in r.summary


def test_connection_explain_includes_day_and_date():
    onward = deepcopy(load_fx("yy456.json"))
    onward["flights"][0]["scheduled_out"] = "2027-03-16T06:30:00Z"  # 01:30 CDT Tue 16 Mar
    onward["flights"][0]["estimated_out"] = "2027-03-16T06:30:00Z"
    fetch = make_fetch({
        "/flights/XX123": (200, load_fx("xx123.json")),
        "/flights/XXX123": (200, load_fx("xx123.json")),
        "/flights/XXX122-1700000000-airline-0002": (200, load_fx("xx122.json")),
        "/flights/YY456": (200, onward),
        "/flights/YYY456": (200, onward),
    })
    r = run(fetch, onward={"flight": "YY456", "date": "2027-03-16", "min_connect_min": 45})
    assert r.connection
    assert "16 Mar" in r.connection.explain
    assert "15 Mar" in r.connection.explain or "Mon 15" in r.connection.explain
    assert "connection to yy456" in r.summary.lower()


def test_separate_ticket_applies_checkin_close_min():
    r = run(
        scenario_fetch(),
        onward={
            "flight": "YY456",
            "date": "2027-03-15",
            "min_connect_min": 45,
            "separate_ticket": True,
            "checkin_close_min": 45,
        },
    )
    # STD in 21:00Z, onward 22:30Z, required 45 + close 45 → latest 21:00, slack 0
    assert r.connection
    assert r.connection.slack_min == 0
    assert "check-in/bag-drop closes 45 min" in r.connection.explain
    assert "Separate tickets" in r.connection.explain


def test_terminal_change_in_margin_and_schema_fields():
    r = run(
        scenario_fetch(),
        onward={
            "flight": "YY456",
            "date": "2027-03-15",
            "min_connect_min": 45,
            "terminal_change": True,
            "terminal_change_min": 25,
        },
    )
    assert r.connection.required_min == 70
    assert "terminal change (25 min)" in r.connection.explain


def test_next_check_without_origin_is_labelled_utc():
    r = check_flight(
        {"flight": "XX123", "date": "2027-04-01"},
        fetch=lambda *a, **k: (200, {"flights": []}),
        now=parse_time("2027-03-15T16:30:00Z"),
    )
    assert r.flight.status == "too_early"
    assert r.next_check_local is not None
    assert r.next_check_local["tz"] == "UTC"
    assert "(UTC)" in r.next_check_local["human"]
