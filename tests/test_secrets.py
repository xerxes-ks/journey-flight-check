"""The AeroAPI key and raw upstream bodies that might echo it never appear in output."""

import json

import pytest

from flightcheck import check_flight
from flightcheck.adapters.aeroapi import AeroAPIAdapter
from flightcheck.exceptions import AeroAPIError
from flightcheck.model import parse_time
from flightcheck.sanitize import redact
from tests.conftest import NEAR_DEP

KEY = "super-secret-test-key-abcxyz"


def _blob(*parts) -> str:
    return "\n".join(str(p) for p in parts)


def test_redact_strips_key_and_headerish():
    raw = f"x-apikey: {KEY}\nAuthorization: Bearer {KEY}\nerror invalid key {KEY}"
    out = redact(raw, KEY)
    assert KEY not in out
    assert "[redacted]" in out


def test_key_never_leaks_from_http_error_body(monkeypatch):
    monkeypatch.setenv("FLIGHTAWARE_AEROAPI_KEY", KEY)

    def leaking_fetch(path, params):
        return 401, {
            "title": "Unauthorized",
            "detail": f"invalid api key {KEY}",
            "x-apikey": KEY,
        }

    ad = AeroAPIAdapter(KEY, fetch=leaking_fetch, budget_usd=0.05)
    with pytest.raises(AeroAPIError) as exc:
        ad.lookup("XX123", "2027-03-15", now=parse_time(NEAR_DEP))
    blob = _blob(exc.value, exc.value.args, ad.calls, json.dumps(ad.calls))
    assert KEY not in blob
    assert "invalid api key" not in blob
    assert ad.calls[0]["http"] == 401


def test_key_never_leaks_from_check_flight_result(monkeypatch):
    monkeypatch.setenv("FLIGHTAWARE_AEROAPI_KEY", KEY)
    from tests.conftest import scenario_fetch

    r = check_flight(
        {"ident": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=scenario_fetch(),
        now=parse_time(NEAR_DEP),
        key=KEY,
    )
    blob = json.dumps(r.to_dict())
    assert KEY not in blob
    assert "x-apikey" not in blob.lower()


def test_key_never_leaks_when_fetch_raises(monkeypatch):
    monkeypatch.setenv("FLIGHTAWARE_AEROAPI_KEY", KEY)

    def fetch(path, params):
        raise RuntimeError(f"refused key={KEY}")

    r = check_flight(
        {"ident": "XX123", "date": "2027-03-15"},
        fetch=fetch,
        now=parse_time(NEAR_DEP),
        key=KEY,
    )
    blob = json.dumps(r.to_dict())
    assert KEY not in blob
    assert r.prediction.schedule_only


def test_handler_error_payload_redacts_key(monkeypatch):
    monkeypatch.setenv("FLIGHTCHECK_TOKEN", "handler-token")
    monkeypatch.setenv("FLIGHTAWARE_AEROAPI_KEY", KEY)

    def leaking_fetch(path, params):
        return 403, {"error": f"bad key {KEY}"}

    import flightcheck.check as chk

    orig = chk.make_adapter

    def fake_adapter(req, fetch=None, key=None):
        return orig(req, fetch=leaking_fetch, key=KEY)

    monkeypatch.setattr(chk, "make_adapter", fake_adapter)

    from api.check import handle

    status, payload = handle(
        "POST",
        {"Authorization": "Bearer handler-token"},
        {},
        {"ident": "XX123", "date": "2027-03-15"},
    )
    blob = json.dumps(payload)
    assert KEY not in blob
    assert status in (401, 402, 500, 502)
    assert "error" in payload
