"""Route table: each api/*.py is its own function. No pyproject entrypoint."""

from __future__ import annotations

import json
import os

import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXPECTED = {
    "api/health.py": "/api/health",
    "api/check.py": "/api/check",
    "api/track.py": "/api/track",
    "api/untrack.py": "/api/untrack",
    "api/tracked.py": "/api/tracked",
    "api/events.py": "/api/tracked/{id}/events",
    "api/tick.py": "/api/tick",
    "api/simulate.py": "/api/simulate",
    "api/webhook-sink.py": "/api/webhook-sink",
}


def test_pyproject_does_not_pin_a_single_python_entrypoint():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        data = tomllib.load(fh)
    vercel = (data.get("tool") or {}).get("vercel") or {}
    assert "entrypoint" not in vercel, (
        "[tool.vercel] entrypoint funnels every /api/* path through one handler "
        f"({vercel.get('entrypoint')}); health and cron then 401."
    )


def test_vercel_json_declares_each_function_and_cron():
    with open(os.path.join(ROOT, "vercel.json")) as fh:
        cfg = json.load(fh)
    fns = cfg["functions"]
    for path in EXPECTED:
        assert path in fns, path
        assert int(fns[path]["maxDuration"]) == 30
    crons = cfg.get("crons") or []
    assert any(c.get("path") == "/api/tick" and c.get("schedule") == "*/5 * * * *" for c in crons)
    sources = {r["source"] for r in cfg.get("rewrites") or []}
    assert "/test" in sources
    assert "/api/track/:id" in sources
    assert "/api/tracked/:id/events" in sources


def test_each_api_module_exports_handler():
    import importlib

    modules = {
        "api.health": "api/health.py",
        "api.check": "api/check.py",
        "api.track": "api/track.py",
        "api.untrack": "api/untrack.py",
        "api.tracked": "api/tracked.py",
        "api.events": "api/events.py",
        "api.tick": "api/tick.py",
        "api.simulate": "api/simulate.py",
    }
    for modname, path in modules.items():
        mod = importlib.import_module(modname)
        assert hasattr(mod, "handler"), path
        assert hasattr(mod, "handle"), path
    # hyphenated filename is not a Python module name
    sink_path = os.path.join(ROOT, "api", "webhook-sink.py")
    text = open(sink_path).read()
    assert "handler = make_handler" in text
    assert "handle_webhook_sink" in text


def test_health_does_not_require_auth():
    from flightcheck.routes import handle_health

    status, payload = handle_health("GET", {}, {}, {})
    assert status == 200
    assert payload["ok"] is True
    assert payload["storage"] in ("blob", "memory", "redis", "none")
    assert "error" not in payload or payload.get("error") is None


def test_tick_get_accepts_cron_bearer(monkeypatch):
    from flightcheck.routes import handle_tick
    from flightcheck.store import reset_process_memory, use_store

    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    monkeypatch.setenv("FLIGHTCHECK_STORAGE", "memory")
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    use_store(reset_process_memory())
    status, payload = handle_tick(
        "GET",
        {"Authorization": "Bearer cron-secret"},
        {},
        {},
        path="/api/tick",
    )
    assert status == 200, payload
    assert payload["ok"] is True
    use_store(None)


def test_tick_get_rejects_missing_secret(monkeypatch):
    from flightcheck.routes import handle_tick

    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    monkeypatch.delenv("FLIGHTCHECK_TOKEN", raising=False)
    status, payload = handle_tick("GET", {}, {}, {}, path="/api/tick")
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


def test_check_still_requires_token_and_is_not_health(monkeypatch):
    from api.check import handle as check_handle
    from flightcheck.routes import handle_health

    monkeypatch.setenv("FLIGHTCHECK_TOKEN", "tok")
    h_status, h_payload = handle_health("GET", {}, {}, {})
    c_status, c_payload = check_handle("GET", {}, {}, None)
    assert h_status == 200
    assert h_payload.get("ok") is True
    assert "storage" in h_payload
    assert c_status == 401
    assert c_payload["error"]["code"] == "unauthorized"


def test_console_has_check_form_and_fixtures_on_by_default():
    html = open(os.path.join(ROOT, "public/test/index.html")).read()
    assert 'id="fixtures"' in html
    assert "checked" in html
    assert "X-Flightcheck-Mode" in html
    assert 'id="chkFlight"' in html
    assert "/api/check" in html
