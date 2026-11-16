"""Outputs of check_flight validate against schema/check_flight.tool.json."""

import json
import os

import jsonschema
import pytest

from flightcheck import check_flight
from flightcheck.model import parse_time
from tests.conftest import NEAR_DEP, scenario_fetch, make_fetch, load_fx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(ROOT, "schema", "check_flight.tool.json")


@pytest.fixture(scope="module")
def schema():
    with open(SCHEMA_PATH) as fh:
        return json.load(fh)


def validator(schema):
    output = schema["outputSchema"]
    return jsonschema.Draft202012Validator(
        {"$defs": schema["$defs"], **output},
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )


CASES = [
    ("on_time", {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"}, scenario_fetch()),
    (
        "onward",
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "onward": {
                "flight": "YY456",
                "date": "2027-03-15",
                "min_connect_min": 45,
                "separate_ticket": True,
            },
        },
        scenario_fetch(),
    ),
    (
        "inbound_late",
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        scenario_fetch(inbound="xx122-late.json"),
    ),
    (
        "cancelled",
        {"flight": "XX123", "date": "2027-03-15"},
        scenario_fetch(flight="xx123-cancelled.json"),
    ),
    (
        "diverted",
        {"flight": "XX123", "date": "2027-03-15"},
        scenario_fetch(flight="xx123-diverted.json"),
    ),
    (
        "unknown",
        {"flight": "XX123", "date": "2027-03-15"},
        make_fetch({"/flights/": (200, load_fx("empty.json"))}),
    ),
    (
        "too_early",
        {"flight": "XX123", "date": "2027-04-01", "origin": "LAX"},
        scenario_fetch(),
    ),
]


@pytest.mark.parametrize("name,payload,fetch", CASES, ids=[c[0] for c in CASES])
def test_output_matches_schema(schema, name, payload, fetch):
    result = check_flight(payload, fetch=fetch, now=parse_time(NEAR_DEP))
    data = result.to_dict()
    validator(schema).validate(data)


def test_input_example_matches_schema(schema):
    example = {
        "flight": "XX123",
        "date": "2026-12-10",
        "origin": "LAX",
        "onward": {
            "flight": "YY456",
            "date": "2026-12-10",
            "min_connect_min": 60,
            "bags_recheck": True,
            "separate_ticket": True,
        },
    }
    jsonschema.Draft202012Validator(
        {"$defs": schema["$defs"], **schema["inputSchema"]},
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    ).validate(example)


def test_tool_schema_is_tiny(schema):
    props = set(schema["inputSchema"]["properties"])
    assert props == {"flight", "date", "origin", "onward"}
    assert schema["inputSchema"]["required"] == ["flight", "date"]
    onward = set(schema["inputSchema"]["properties"]["onward"]["properties"])
    assert onward == {
        "flight", "date", "origin", "min_connect_min", "separate_ticket", "bags_recheck",
        "terminal_change", "terminal_change_min", "checkin_close_min",
    }
    required_out = set(schema["outputSchema"]["required"])
    assert {"status", "band", "final", "summary", "checked_at", "next_poll_min"}.issubset(required_out)
    err_req = set(schema["$defs"]["error"]["required"])
    assert {"code", "message", "retryable"}.issubset(err_req)
    codes = set(schema["$defs"]["error"]["properties"]["code"]["enum"])
    assert {"flight_not_found", "timeout", "rate_limited", "upstream_error"}.issubset(codes)
    assert "checked_at" in schema["outputSchema"]["properties"]


def test_next_poll_min_aliases_minutes(schema):
    result = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=scenario_fetch(),
        now=parse_time(NEAR_DEP),
    )
    data = result.to_dict()
    assert data["next_poll_min"] == data["next_check_in_minutes"]
    assert data["next_poll_min"] == 15
    assert data["checked_at"] == NEAR_DEP
    validator(schema).validate(data)


def test_cancelled_next_poll_min_is_null(schema):
    result = check_flight(
        {"flight": "XX123", "date": "2027-03-15"},
        fetch=scenario_fetch(flight="xx123-cancelled.json"),
        now=parse_time(NEAR_DEP),
    )
    data = result.to_dict()
    assert data["final"] is True
    assert data["next_poll_min"] is None
    validator(schema).validate(data)


def test_response_never_contains_api_key(schema, monkeypatch):
    key = "super-secret-test-key-abcxyz"
    monkeypatch.setenv("FLIGHTAWARE_AEROAPI_KEY", key)
    result = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=scenario_fetch(),
        now=parse_time(NEAR_DEP),
        key=key,
    )
    data = result.to_dict()
    blob = json.dumps(data)
    assert key not in blob
    assert "x-apikey" not in blob.lower()
    for call in data["calls"]:
        params_blob = json.dumps(call.get("params") or {})
        assert key not in params_blob
        assert "apikey" not in params_blob.lower()
    validator(schema).validate(data)


def test_too_early_next_check_is_not_null(schema):
    result = check_flight(
        {"flight": "XX123", "date": "2027-04-01", "origin": "LAX"},
        fetch=scenario_fetch(),
        now=parse_time(NEAR_DEP),
    )
    data = result.to_dict()
    assert data["status"] == "too_early"
    assert data["final"] is False
    assert data["next_check_local"] is not None
    assert data["next_poll_min"] is not None
    assert data["checked_at"].endswith("Z")
    validator(schema).validate(data)


def test_not_found_is_not_on_track(schema):
    result = check_flight(
        {"flight": "XX123", "date": "2027-03-15"},
        fetch=make_fetch({"/flights/": (200, load_fx("empty.json"))}),
        now=parse_time(NEAR_DEP),
    )
    data = result.to_dict()
    assert data["ok"] is False
    assert data["status"] == "unknown"
    assert data["band"] == "unknown"
    assert data["error"]["code"] == "flight_not_found"
    assert data["error"]["retryable"] is False
    assert data["next_poll_min"] is not None
    validator(schema).validate(data)
