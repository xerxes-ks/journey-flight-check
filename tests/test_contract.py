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
    assert {"status", "band", "final", "summary"}.issubset(required_out)
