"""Adapter: budget guard, date window, instance picking, inbound window."""

import urllib.error

import pytest

from flightcheck import BudgetExceeded, check_flight
from flightcheck.adapters.aeroapi import AeroAPIAdapter, UPSTREAM_TIMEOUT_S
from flightcheck.exceptions import AeroAPIError, error_dict
from flightcheck.model import parse_time
from tests.conftest import NEAR_DEP, load_fx, make_fetch, scenario_fetch


def test_budget_exceeded_stops_before_second_call():
    ad = AeroAPIAdapter("test-key", fetch=scenario_fetch(), budget_usd=0.005)
    with pytest.raises(BudgetExceeded):
        ad.lookup("XX123", "2027-03-15", origin="XXA", now=parse_time(NEAR_DEP))
    assert ad.spent <= 0.005
    assert len(ad.calls) == 1


def test_budget_exceeded_via_check_flight():
    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA", "budget": 0.005},
        fetch=scenario_fetch(),
        now=parse_time(NEAR_DEP),
    )
    assert r.flight.ident_iata == "XX123"
    assert r.warning and r.warning["code"] == "budget_exceeded"
    assert r.ok is True
    assert r.inbound is None


def test_http_error_keeps_sanitized_snippet():
    fetch = make_fetch({"/flights/XX123": (500, {"title": "boom", "detail": "engine fault"})})
    ad = AeroAPIAdapter("test-key", fetch=fetch, budget_usd=0.05)
    with pytest.raises(AeroAPIError) as exc:
        ad.lookup("XX123", "2027-03-15", now=parse_time(NEAR_DEP))
    assert exc.value.http_status == 500
    assert exc.value.code == "upstream_error"
    assert "500" in str(exc.value)
    assert "boom" in str(exc.value)


def test_error_codes_mapped():
    cases = [(400, "bad_request"), (404, "not_found"), (429, "rate_limited"), (503, "upstream_error")]
    for status, code in cases:
        fetch = make_fetch({"/flights/XX123": (status, {"title": f"http-{status}"})})
        ad = AeroAPIAdapter("test-key", fetch=fetch, budget_usd=0.05)
        with pytest.raises(AeroAPIError) as exc:
            ad.lookup("XX123", "2027-03-15", now=parse_time(NEAR_DEP))
        assert exc.value.code == code
        r = check_flight(
            {"flight": "XX123", "date": "2027-03-15"},
            fetch=fetch,
            now=parse_time(NEAR_DEP),
        )
        assert r.error["code"] == code
        assert r.ok is False
        assert "retryable" in r.error
        assert r.error["retryable"] is (code in ("rate_limited", "upstream_error"))
        assert r.error["retryable"] is (code not in ("bad_request", "not_found"))


def test_cost_is_configurable():
    ad = AeroAPIAdapter("test-key", fetch=scenario_fetch(), budget_usd=1.0, cost_per_call=0.01)
    ad.lookup("XX123", "2027-03-15", origin="XXA", now=parse_time(NEAR_DEP))
    assert ad.spent == 0.02
    assert ad.calls[0]["cost_usd"] == 0.01


def test_to_flight_maps_documented_fields():
    raw = load_fx("xx123.json")["flights"][0]
    f = AeroAPIAdapter.to_flight(raw)
    assert f.aircraft_type == "B738"
    assert f.baggage_claim == "4"
    assert f.gate_destination == "B4"
    assert f.terminal_destination == "2"
    assert f.origin_tz == "America/New_York"


def test_min_turn_override():
    r = check_flight(
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "min_turn": 10,
        },
        fetch=scenario_fetch(inbound="xx122-late.json"),
        now=parse_time(NEAR_DEP),
    )
    # inbound 18:20Z + 10 min = 18:30Z vs 18:00Z → +30
    assert r.prediction.dep_delay_min == 30


def test_retryable_flags():
    assert error_dict("rate_limited", "x")["retryable"] is True
    assert error_dict("upstream_error", "x")["retryable"] is True
    assert error_dict("timeout", "x")["retryable"] is True
    assert error_dict("bad_request", "x")["retryable"] is False
    assert error_dict("not_found", "x")["retryable"] is False
    assert error_dict("budget_exceeded", "x")["retryable"] is False
    assert error_dict("unauthorized", "x")["retryable"] is False


def test_upstream_timeout_is_fail_fast():
    assert UPSTREAM_TIMEOUT_S == 8


def test_timeout_is_retryable():
    def fetch(path, params):
        raise TimeoutError("hang")

    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15"},
        fetch=fetch,
        now=parse_time(NEAR_DEP),
    )
    assert r.ok is False
    assert r.error["code"] == "timeout"
    assert r.error["retryable"] is True
    assert r.to_dict()["checked_at"].endswith("Z")


def test_urlerror_timed_out_is_timeout():
    def fetch(path, params):
        raise urllib.error.URLError(TimeoutError("timed out"))

    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15"},
        fetch=fetch,
        now=parse_time(NEAR_DEP),
    )
    assert r.error["code"] == "timeout"
    assert r.error["retryable"] is True
