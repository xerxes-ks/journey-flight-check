"""Route table: dispatcher entrypoint, not api.check:handler."""

from __future__ import annotations

import json
import os

import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DISPATCHER = "api.index:handler"
FORBIDDEN = "api.check:handler"


def test_pyproject_entrypoint_is_the_dispatcher():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        data = tomllib.load(fh)
    vercel = (data.get("tool") or {}).get("vercel") or {}
    assert vercel.get("entrypoint") == DISPATCHER, vercel
    assert vercel.get("entrypoint") != FORBIDDEN


def test_vercel_json_declares_dispatcher_and_cron():
    with open(os.path.join(ROOT, "vercel.json")) as fh:
        cfg = json.load(fh)
    fns = cfg["functions"]
    assert "api/index.py" in fns
    assert int(fns["api/index.py"]["maxDuration"]) == 30
    crons = cfg.get("crons") or []
    assert any(c.get("path") == "/api/tick" and c.get("schedule") == "*/5 * * * *" for c in crons)
    sources = {r["source"] for r in cfg.get("rewrites") or []}
    assert "/test" in sources
    assert "/api/track/:id" in sources
    assert "/api/tracked/:id/events" in sources


def test_dispatcher_module_exports_handler():
    import api.index as mod

    assert hasattr(mod, "handler")
    assert hasattr(mod, "handle")
    from flightcheck.dispatch import ROUTES

    paths = [r[1] for r in ROUTES]
    assert "/api/health" in paths
    assert "/api/check" in paths
    assert "/api/tick" in paths


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


def test_dispatch_health_vs_check_vs_tick(monkeypatch):
    from flightcheck.dispatch import dispatch
    from flightcheck.store import reset_process_memory, use_store

    monkeypatch.setenv("FLIGHTCHECK_TOKEN", "tok")
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    monkeypatch.setenv("FLIGHTCHECK_STORAGE", "memory")
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    use_store(reset_process_memory())
    h_status, h_payload = dispatch("GET", {}, {}, {}, "/api/health")
    c_status, c_payload = dispatch("GET", {}, {}, {}, "/api/check")
    t_status, t_payload = dispatch(
        "GET", {"Authorization": "Bearer cron-secret"}, {}, {}, "/api/tick"
    )
    n_status, _ = dispatch("GET", {}, {}, {}, "/api/nope")
    assert h_status == 200
    assert h_payload.get("ok") is True
    assert "storage" in h_payload
    assert c_status == 401
    assert c_payload["error"]["code"] == "unauthorized"
    assert t_status == 200, t_payload
    assert t_payload.get("ok") is True
    assert n_status == 404
    use_store(None)


def test_console_has_check_form_and_fixtures_on_by_default():
    html = open(os.path.join(ROOT, "public/test/index.html")).read()
    assert 'id="fixtures"' in html
    assert "checked" in html
    assert "X-Flightcheck-Mode" in html
    assert 'id="chkFlight"' in html
    assert "/api/check" in html
