import json
import threading
from http.server import HTTPServer

import pytest
import urllib.error
import urllib.request

from tests.conftest import scenario_fetch, NEAR_DEP


TOKEN = "test-handler-token"


@pytest.fixture
def handler_env(monkeypatch):
    monkeypatch.setenv("FLIGHTCHECK_TOKEN", TOKEN)
    monkeypatch.delenv("FLIGHTAWARE_AEROAPI_KEY", raising=False)
    import flightcheck.check as chk
    from flightcheck.model import parse_time

    orig = chk.check_flight

    def wrapped(req, **kw):
        kw.setdefault("fetch", scenario_fetch())
        kw.setdefault("now", parse_time(NEAR_DEP))
        return orig(req, **kw)

    monkeypatch.setattr(chk, "check_flight", wrapped)
    return TOKEN


def test_auth_rejected_without_token(handler_env):
    from api.check import handle

    status, payload = handle("GET", {}, {"ident": "XX123", "date": "2027-03-15"}, None)
    assert status == 401
    assert payload["error"] == "unauthorized"


def test_auth_rejected_wrong_token(handler_env):
    from api.check import handle

    status, _ = handle(
        "GET",
        {"Authorization": "Bearer wrong"},
        {"ident": "XX123", "date": "2027-03-15"},
        None,
    )
    assert status == 401


def test_auth_rejected_when_token_unset(monkeypatch):
    monkeypatch.delenv("FLIGHTCHECK_TOKEN", raising=False)
    from api.check import handle

    status, _ = handle(
        "GET",
        {"Authorization": "Bearer anything"},
        {"ident": "XX123", "date": "2027-03-15"},
        None,
    )
    assert status == 401


def test_get_ok(handler_env):
    from api.check import handle

    status, payload = handle(
        "GET",
        {"Authorization": f"Bearer {TOKEN}"},
        {"ident": "XX123", "date": "2027-03-15", "origin": "XXA"},
        None,
    )
    assert status == 200
    assert payload["flight"]["ident_iata"] == "XX123"
    assert payload["prediction"]["band"] == "on_track"
    assert "calls" in payload and "cost_usd_est" in payload


def test_post_with_onward(handler_env):
    from api.check import handle

    status, payload = handle(
        "POST",
        {"Authorization": f"Bearer {TOKEN}"},
        {},
        {
            "ident": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "onward": {
                "ident": "YY456",
                "min_connection_min": 45,
                "separate_ticket": False,
            },
        },
    )
    assert status == 200
    assert payload["connection"]["band"] == "on_track"
    assert payload["connection"]["protected"] is True


def test_missing_fields_are_400(handler_env):
    from api.check import handle

    status, payload = handle(
        "POST",
        {"Authorization": f"Bearer {TOKEN}"},
        {},
        {"ident": "XX123"},
    )
    assert status == 400
    assert "date" in payload["error"]


def test_budget_exceeded_is_402(handler_env, monkeypatch):
    import flightcheck.check as chk
    from flightcheck.exceptions import BudgetExceeded

    def boom(*a, **k):
        raise BudgetExceeded("budget $0.005 reached")

    monkeypatch.setattr(chk, "check_flight", boom)
    from api.check import handle

    status, payload = handle(
        "POST",
        {"Authorization": f"Bearer {TOKEN}"},
        {},
        {"ident": "XX123", "date": "2027-03-15", "budget": 0.005},
    )
    assert status == 402
    assert "budget" in payload["error"]


def test_http_server_roundtrip(handler_env):
    import api.check as mod

    srv = HTTPServer(("127.0.0.1", 0), mod.handler)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/api/check?ident=XX123&date=2027-03-15&origin=XXA"
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(url, timeout=5)
        assert exc.value.code == 401
        threading.Thread(target=srv.handle_request, daemon=True).start()
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
        body = json.loads(urllib.request.urlopen(req, timeout=5).read())
        assert body["flight"]["ident_iata"] == "XX123"
    finally:
        srv.server_close()
