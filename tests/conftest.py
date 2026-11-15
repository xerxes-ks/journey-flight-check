"""Shared fixtures. All AeroAPI traffic is injected; tests never open a network socket."""

from __future__ import annotations

import json
import os
from copy import deepcopy

import pytest

FX = os.path.join(os.path.dirname(__file__), "fixtures", "aeroapi")

NEAR_DEP = "2027-03-15T16:30:00Z"  # T-90 min vs 18:00Z STD
FAR_DEP = "2027-03-15T08:00:00Z"   # T-10 h, outside the 6 h inbound window


def load_fx(name: str) -> dict:
    with open(os.path.join(FX, name)) as fh:
        return json.load(fh)


def make_fetch(mapping: dict[str, tuple[int, dict]]):
    """Exact path match, then longest prefix. Tests never hit the network."""

    def fetch(path, params):
        if path in mapping:
            status, body = mapping[path]
            return status, deepcopy(body)
        for prefix in sorted(mapping, key=len, reverse=True):
            if path.startswith(prefix):
                status, body = mapping[prefix]
                return status, deepcopy(body)
        return 404, {}

    return fetch


def scenario_fetch(*, flight="xx123.json", inbound="xx122.json", onward="yy456.json"):
    return make_fetch({
        "/flights/XX123": (200, load_fx(flight)),
        "/flights/XXX123": (200, load_fx(flight)),
        "/flights/XXX122-1700000000-airline-0002": (200, load_fx(inbound)),
        "/flights/YY456": (200, load_fx(onward)),
        "/flights/YYY456": (200, load_fx(onward)),
    })


@pytest.fixture
def fetch_on_time():
    return scenario_fetch()
