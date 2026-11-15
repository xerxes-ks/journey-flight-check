"""Adapter: budget guard, date window, instance picking, inbound window."""

import pytest

from flightcheck import BudgetExceeded, check_flight
from flightcheck.adapters.aeroapi import AeroAPIAdapter
from flightcheck.exceptions import AeroAPIError
from flightcheck.model import parse_time
from tests.conftest import NEAR_DEP, load_fx, make_fetch, scenario_fetch


def test_budget_exceeded_stops_before_second_call():
    ad = AeroAPIAdapter("test-key", fetch=scenario_fetch(), budget_usd=0.005)
    with pytest.raises(BudgetExceeded):
        ad.lookup("XX123", "2027-03-15", origin="XXA", now=parse_time(NEAR_DEP))
    assert ad.spent <= 0.005
    assert len(ad.calls) == 1


def test_budget_exceeded_via_check_flight():
    with pytest.raises(BudgetExceeded):
        check_flight(
            {"ident": "XX123", "date": "2027-03-15", "origin": "XXA", "budget": 0.005},
            fetch=scenario_fetch(),
            now=parse_time(NEAR_DEP),
        )


def test_non_404_http_error_is_aeroapi_error():
    fetch = make_fetch({"/flights/XX123": (500, {"title": "boom"})})
    ad = AeroAPIAdapter("test-key", fetch=fetch, budget_usd=0.05)
    with pytest.raises(AeroAPIError) as exc:
        ad.lookup("XX123", "2027-03-15", now=parse_time(NEAR_DEP))
    assert exc.value.http_status == 500
    assert "500" in str(exc.value)
    assert "boom" not in str(exc.value)


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
