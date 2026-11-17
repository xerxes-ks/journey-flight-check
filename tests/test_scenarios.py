"""Generic scenario coverage: on time, inbound late, missed MCT, cancelled, diverted, unknown."""

from flightcheck import check_flight
from flightcheck.model import parse_time
from tests.conftest import NEAR_DEP, FAR_DEP, scenario_fetch, load_fx, make_fetch


def run(fetch, **extra):
    payload = {
        "flight": "XX123",
        "date": "2027-03-15",
        "origin": "XXA",
        "budget": 0.05,
        **extra,
    }
    return check_flight(payload, fetch=fetch, now=parse_time(NEAR_DEP))


def test_on_time():
    r = run(scenario_fetch())
    f = r.flight
    assert f.ident_iata == "XX123"
    assert f.origin == "XXA" and f.destination == "XXB"
    assert f.gate_origin == "A12" and f.terminal_origin == "1"
    assert f.baggage_claim == "4" and f.aircraft_type == "B738"
    assert f.registration == "N123XX"
    assert f.status == "scheduled" and not f.cancelled and not f.diverted
    assert f.sched_out.strftime("%Y-%m-%dT%H:%M:%SZ") == "2027-03-15T18:00:00Z"
    assert r.inbound.ident == "XX122"
    assert r.prediction.band == "on_track"
    assert r.prediction.dep_delay_min == 0
    assert r.ok
    assert r.to_dict()["checked_at"] == "2027-03-15T16:30:00Z"
    assert r.to_dict()["next_poll_min"] == 30
    assert r.to_dict()["checkpoint"] == "t_minus_3h"
    d = r.flight.to_dict()
    so = d["scheduled_out"]
    assert so["tz"] == "America/New_York"
    assert so["local"].startswith("2027-03-15T14:00:00")
    assert "Z" not in so["local"]
    assert "XXA time" in so["human"]
    assert d["scheduled_in"]["tz"] == "America/Chicago"
    assert "UTC" not in r.prediction.reasons[0] if r.prediction.reasons else True
    assert r.summary
    assert r.next_check_local is not None
    assert r.next_check_in_minutes == 30  # T-90 min → next essential checkpoint T-1h


def test_on_time_connection_protected():
    r = run(scenario_fetch(), onward={
        "flight": "YY456",
        "date": "2027-03-15",
        "min_connect_min": 45,
        "separate_ticket": False,
    })
    assert r.connection.band == "on_track"
    assert r.connection.protected
    assert r.connection.slack_min == 45
    assert r.connection.required_min == 45
    assert "Separate tickets" not in r.connection.explain
    assert "connection to yy456" in r.summary.lower()
    assert "on track" in r.summary.lower()
    assert r.status == "scheduled"
    assert r.band == "on_track"
    assert r.final is False


def test_inbound_late_predicts_from_inbound():
    r = run(scenario_fetch(inbound="xx122-late.json"))
    # inbound gate 18:20Z + 35 min default turn = 18:55Z vs STD 18:00Z → +55
    assert r.prediction.dep_delay_min == 55
    assert r.prediction.arr_delay_min == 55
    assert r.prediction.band == "at_risk"
    assert "XX122" in r.prediction.reasons[0]
    assert "18:20" in r.prediction.reasons[0] or "14:20" in r.prediction.reasons[0]
    assert r.inbound.ident == "XX122"
    assert "Z" not in r.prediction.reasons[0]


def test_airline_estimate_used_when_larger_than_inbound():
    r = run(scenario_fetch(flight="xx123-airline-delay.json", inbound="xx122.json"))
    assert r.prediction.dep_delay_min == 20
    assert "airline now shows departure" in r.prediction.reasons[0]


def test_historical_prior_can_win():
    r = run(scenario_fetch(), prior={"dep_median": 40, "arr_median": 35, "n": 12})
    assert r.prediction.dep_delay_min == 40
    assert "usually leaves" in r.prediction.reasons[0]


def test_missed_mct_separate_ticket():
    r = run(
        scenario_fetch(inbound="xx122-late.json"),
        onward={
            "flight": "YY456",
            "date": "2027-03-15",
            "min_connect_min": 60,
            "terminal_change": True,
            "terminal_change_min": 20,
            "bags_recheck": True,
            "checkin_close_min": 45,
            "separate_ticket": True,
        },
    )
    c = r.connection
    assert c.band == "at_risk"
    assert not c.protected
    assert c.required_min == 80
    assert c.slack_min < 0
    assert "Separate tickets" in c.explain


def test_cancelled_is_final():
    r = run(scenario_fetch(flight="xx123-cancelled.json"), previous_checkpoint="t_minus_3h", attempts=1)
    assert r.flight.cancelled and r.flight.status == "cancelled"
    assert r.prediction.band == "at_risk"
    assert r.prediction.final is True
    assert r.next_check_local is None
    assert r.next_check_in_minutes is None
    assert r.to_dict()["next_poll_min"] is None
    assert r.to_dict()["checked_at"].endswith("Z")
    assert "cancelled" in r.prediction.reasons[0]
    r2 = run(
        scenario_fetch(flight="xx123-cancelled.json"),
        onward={"flight": "YY456", "date": "2027-03-15", "min_connect_min": 45},
    )
    assert r2.connection.band == "at_risk"
    assert "cancelled" in r2.connection.explain


def test_diverted_is_final():
    r = run(scenario_fetch(flight="xx123-diverted.json"))
    assert r.flight.diverted and r.flight.status == "diverted"
    assert r.flight.divert_to == "XXC"
    assert r.prediction.band == "at_risk"
    assert r.prediction.final is True
    assert r.next_check_local is None
    assert "diverted to XXC" in r.prediction.reasons[0]


def test_empty_response_is_unknown_not_on_track():
    fetch = make_fetch({"/flights/": (200, load_fx("empty.json"))})
    r = run(fetch)
    assert r.flight.status == "unknown"
    assert r.prediction.band == "unknown"
    assert r.ok is False
    assert r.error and r.error["code"] == "flight_not_found"
    assert r.error["retryable"] is False
    assert r.prediction.band == "unknown"
    assert r.to_dict()["next_poll_min"] == r.next_check_in_minutes
    assert r.to_dict()["checked_at"].endswith("Z")
    assert r.inbound is None
    assert r.next_check_local is not None
    assert r.next_check_in_minutes == 60
    assert r.status == "unknown"
    assert r.band == "unknown"
    assert r.final is False


def test_404_is_not_found():
    fetch = make_fetch({"/flights/XX123": (404, {"title": "not found", "detail": "no such flight"})})
    r = run(fetch)
    assert r.flight.status == "unknown"
    assert r.prediction.band == "unknown"
    assert r.ok is False
    assert r.error["code"] == "not_found"
    assert r.error["http"] == 404
    assert "not found" in (r.error.get("snippet") or r.error["message"]).lower()


def test_no_inbound_lookup_far_from_departure():
    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=scenario_fetch(),
        now=parse_time(FAR_DEP),
    )
    assert r.inbound is None
    assert len(r.calls) == 1
    assert r.calls[0]["path"].startswith("/flights/XX123")


def test_inbound_lookup_near_departure():
    r = run(scenario_fetch())
    paths = [c["path"] for c in r.calls]
    assert any("XXX122-1700000000-airline-0002" in p for p in paths)
    assert len(r.calls) == 2
    assert r.cost_usd_est == 0.01


def test_date_window_params_sent():
    r = run(scenario_fetch())
    params = r.calls[0]["params"]
    assert "start" in params and "end" in params
    assert params["ident_type"] == "designator"
    assert params["start"].endswith("Z")


def test_picks_matching_date_not_previous_day():
    r = run(scenario_fetch())
    assert r.flight.fa_flight_id == "XXX123-1700000000-airline-0001"
    assert r.flight.registration == "N123XX"


def test_airborne_uses_live_arrival():
    body = load_fx("xx123.json")
    f = body["flights"][0]
    f["actual_out"] = "2027-03-15T18:05:00Z"
    f["actual_off"] = "2027-03-15T18:12:00Z"
    f["estimated_in"] = "2027-03-15T21:40:00Z"
    f["inbound_fa_flight_id"] = None
    fetch = make_fetch({"/flights/XX123": (200, body)})
    r = run(fetch)
    assert r.flight.status == "active"
    assert r.prediction.arr_delay_min == 40
    assert "in the air" in r.prediction.reasons[0]


def test_landed_is_final():
    body = load_fx("xx123.json")
    f = body["flights"][0]
    f["actual_out"] = "2027-03-15T18:10:00Z"
    f["actual_off"] = "2027-03-15T18:12:00Z"
    f["actual_on"] = "2027-03-15T21:10:00Z"
    f["actual_in"] = "2027-03-15T21:12:00Z"
    f["inbound_fa_flight_id"] = None
    fetch = make_fetch({"/flights/XX123": (200, body)})
    r = run(fetch)
    assert r.flight.status == "landed"
    assert r.prediction.final is True
    assert r.final is True
    assert r.checkpoint == "done"
    assert r.next_check_local is None
    assert r.prediction.dep_delay_min == 10
    assert r.prediction.arr_delay_min == 12
