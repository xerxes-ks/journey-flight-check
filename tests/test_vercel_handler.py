import json
import os
import threading
from http.server import HTTPServer

import pytest
import urllib.error
import urllib.request

from tests.conftest import scenario_fetch, NEAR_DEP, make_fetch


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

    status, payload = handle("GET", {}, {"flight": "XX123", "date": "2027-03-15"}, None)
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"
    assert payload["error"]["retryable"] is False


def test_auth_rejected_wrong_token(handler_env):
    from api.check import handle

    status, _ = handle(
        "GET",
        {"Authorization": "Bearer wrong"},
        {"flight": "XX123", "date": "2027-03-15"},
        None,
    )
    assert status == 401


def test_auth_rejected_when_token_unset(monkeypatch):
    monkeypatch.delenv("FLIGHTCHECK_TOKEN", raising=False)
    from api.check import handle

    status, _ = handle(
        "GET",
        {"Authorization": "Bearer anything"},
        {"flight": "XX123", "date": "2027-03-15"},
        None,
    )
    assert status == 401


def test_get_ok(handler_env):
    from api.check import handle

    status, payload = handle(
        "GET",
        {"Authorization": f"Bearer {TOKEN}"},
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        None,
    )
    assert status == 200
    assert payload["flight"]["ident_iata"] == "XX123"
    assert payload["prediction"]["band"] == "on_track"
    assert "calls" in payload and "cost_usd_est" in payload
    assert payload["summary"]
    assert payload["flight"]["scheduled_out"]["tz"] == "America/New_York"


def test_post_with_onward(handler_env):
    from api.check import handle

    status, payload = handle(
        "POST",
        {"Authorization": f"Bearer {TOKEN}"},
        {},
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "onward": {
                "flight": "YY456",
                "date": "2027-03-15",
                "min_connect_min": 45,
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
        {"flight": "XX123"},
    )
    assert status == 400
    assert "date" in payload["error"]["message"]
    assert payload["error"]["retryable"] is False


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
        {"flight": "XX123", "date": "2027-03-15", "budget": 0.005},
    )
    assert status == 402
    assert payload["error"]["code"] == "budget_exceeded"
    assert payload["error"]["retryable"] is False


def test_upstream_error_is_structured_json_not_bare_502(handler_env, monkeypatch):
    import flightcheck.check as chk
    from flightcheck.model import parse_time

    orig = chk.check_flight

    def wrapped(req, **kw):
        kw["fetch"] = make_fetch({"/flights/XX123": (503, {"title": "unavailable"})})
        kw.setdefault("now", parse_time(NEAR_DEP))
        return orig(req, **kw)

    monkeypatch.setattr(chk, "check_flight", wrapped)
    from api.check import handle

    status, payload = handle(
        "POST",
        {"Authorization": f"Bearer {TOKEN}"},
        {},
        {"flight": "XX123", "date": "2027-03-15"},
    )
    assert isinstance(payload, dict)
    assert payload.get("error", {}).get("code") == "upstream_error"
    assert payload["error"]["retryable"] is True
    assert "503" in payload["error"]["message"] or payload["error"].get("http") == 503
    # Not a bare 502 with a string body.
    assert status != 502 or isinstance(payload.get("error"), dict)


def test_vercel_has_no_single_entrypoint():
    import tomllib

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "pyproject.toml"), "rb") as fh:
        data = tomllib.load(fh)
    vercel = (data.get("tool") or {}).get("vercel") or {}
    assert "entrypoint" not in vercel
    import api.check as mod
    assert hasattr(mod, "handler")


def test_http_server_roundtrip(handler_env):
    import api.check as mod

    srv = HTTPServer(("127.0.0.1", 0), mod.handler)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/api/check?flight=XX123&date=2027-03-15&origin=XXA"
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
