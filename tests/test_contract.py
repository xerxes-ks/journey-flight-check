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
    # Resolve $defs from the parent document.
    return jsonschema.Draft202012Validator(
        {"$defs": schema["$defs"], **output},
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )


CASES = [
    ("on_time", {"ident": "XX123", "date": "2027-03-15", "origin": "XXA"}, scenario_fetch()),
    (
        "onward",
        {
            "ident": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "onward": {"ident": "YY456", "min_connection_min": 45, "separate_ticket": True},
        },
        scenario_fetch(),
    ),
    (
        "inbound_late",
        {"ident": "XX123", "date": "2027-03-15", "origin": "XXA"},
        scenario_fetch(inbound="xx122-late.json"),
    ),
    (
        "cancelled",
        {"ident": "XX123", "date": "2027-03-15"},
        scenario_fetch(flight="xx123-cancelled.json"),
    ),
    (
        "diverted",
        {"ident": "XX123", "date": "2027-03-15"},
        scenario_fetch(flight="xx123-diverted.json"),
    ),
    (
        "schedule_only",
        {"ident": "XX123", "date": "2027-03-15"},
        make_fetch({"/flights/": (200, load_fx("empty.json"))}),
    ),
]


@pytest.mark.parametrize("name,payload,fetch", CASES, ids=[c[0] for c in CASES])
def test_output_matches_schema(schema, name, payload, fetch):
    result = check_flight(payload, fetch=fetch, now=parse_time(NEAR_DEP))
    data = result.to_dict()
    validator(schema).validate(data)


def test_input_example_matches_schema(schema):
    example = {
        "ident": "XX123",
        "date": "2026-12-10",
        "origin": "XXA",
        "onward": {
            "ident": "YY456",
            "min_connection_min": 60,
            "terminal_change": True,
            "bag_recheck": True,
            "separate_ticket": True,
        },
        "budget_usd": 0.05,
        "min_turn": 30,
    }
    jsonschema.Draft202012Validator(
        {"$defs": schema["$defs"], **schema["inputSchema"]},
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    ).validate(example)
