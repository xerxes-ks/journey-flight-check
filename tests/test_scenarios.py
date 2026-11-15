"""Generic scenario coverage: on time, inbound late, missed MCT, cancelled, diverted, schedule-only."""

from flightcheck import check_flight
from flightcheck.model import parse_time
from tests.conftest import NEAR_DEP, FAR_DEP, scenario_fetch, load_fx, make_fetch


def run(fetch, **extra):
    payload = {
        "ident": "XX123",
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
    assert not r.prediction.schedule_only
    d = r.flight.to_dict()
    assert d["scheduled_out"]["timezone"] == "America/New_York"
    assert d["scheduled_out"]["local"].startswith("2027-03-15T14:00:00")
    assert d["scheduled_in"]["timezone"] == "America/Chicago"


def test_on_time_connection_protected():
    r = run(scenario_fetch(), onward={
        "ident": "YY456",
        "min_connection_min": 45,
        "separate_ticket": False,
    })
    assert r.connection.band == "on_track"
    assert r.connection.protected
    assert r.connection.slack_min == 45  # 22:30 - 45 MCT - 21:00 pred
    assert r.connection.required_min == 45
    assert "Separate tickets" not in r.connection.explain


def test_inbound_late_predicts_from_inbound():
    r = run(scenario_fetch(inbound="xx122-late.json"))
    # inbound gate 18:20Z + 30 min turn = 18:50Z vs STD 18:00Z → +50
    assert r.prediction.dep_delay_min == 50
    assert r.prediction.arr_delay_min == 50
    assert r.prediction.band == "at_risk"
    assert "XX122" in r.prediction.reasons[0]
    assert r.inbound.ident == "XX122"


def test_airline_estimate_used_when_larger_than_inbound():
    r = run(scenario_fetch(flight="xx123-airline-delay.json", inbound="xx122.json"))
    # inbound on time (17:00+30=17:30 vs 18:00 → 0); airline +20
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
            "ident": "YY456",
            "min_connection_min": 60,
            "terminal_change": True,
            "terminal_change_min": 20,
            "bag_recheck": True,
            "checkin_close_min": 45,
            "separate_ticket": True,
        },
    )
    c = r.connection
    assert c.band == "at_risk"
    assert not c.protected
    assert c.required_min == 80  # 60 MCT + 20 terminal change
    # latest = 22:30 - (45 close + 80 required) = 20:25; pred in = 21:50; slack = -85
    assert c.slack_min < 0
    assert "Separate tickets" in c.explain


def test_cancelled():
    r = run(scenario_fetch(flight="xx123-cancelled.json"))
    assert r.flight.cancelled and r.flight.status == "cancelled"
    assert r.prediction.band == "at_risk"
    assert r.prediction.reasons[0].startswith("cancelled")
    r2 = run(
        scenario_fetch(flight="xx123-cancelled.json"),
        onward={"ident": "YY456", "min_connection_min": 45},
    )
    assert r2.connection.band == "at_risk"
    assert "cancelled" in r2.connection.explain


def test_diverted():
    r = run(scenario_fetch(flight="xx123-diverted.json"))
    assert r.flight.diverted and r.flight.status == "diverted"
    assert r.flight.divert_to == "XXC"
    assert r.prediction.band == "at_risk"
    assert "diverted to XXC" in r.prediction.reasons[0]


def test_schedule_only_when_no_live_data():
    fetch = make_fetch({"/flights/": (200, load_fx("empty.json"))})
    r = run(fetch)
    assert r.flight.source == "schedule" and not r.flight.live
    assert r.prediction.schedule_only
    assert "timetable only" in r.prediction.reasons[0]
    assert r.prediction.band == "on_track"
    assert r.inbound is None


def test_schedule_only_on_404():
    fetch = make_fetch({"/flights/XX123": (404, {"title": "not found"})})
    r = run(fetch)
    assert r.prediction.schedule_only
    assert r.flight.source == "schedule"


def test_no_inbound_lookup_far_from_departure():
    r = check_flight(
        {"ident": "XX123", "date": "2027-03-15", "origin": "XXA"},
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
    f["actual_in"] = "2027-03-15T21:12:00Z"
    f["inbound_fa_flight_id"] = None
    fetch = make_fetch({"/flights/XX123": (200, body)})
    r = run(fetch)
    assert r.flight.status == "landed"
    assert r.prediction.final
    assert r.prediction.dep_delay_min == 10
    assert r.prediction.arr_delay_min == 12
