"""Fixture mode, `now` on check/tick, storage_not_configured HTTP, signed sink."""

from __future__ import annotations

import datetime as dt
import json

from flightcheck.events import sign_body
from flightcheck.mode import is_fixtures, resolve_now
from flightcheck.routes import handle_tick, handle_track, handle_webhook_sink
from flightcheck.store import MemoryStore, reset_process_memory, use_store
from flightcheck.track import list_event_log


TOKEN = "fixture-token"


def _auth(extra=None):
    h = {"Authorization": f"Bearer {TOKEN}"}
    if extra:
        h.update(extra)
    return h


def test_resolve_now_live_ignores(monkeypatch):
    monkeypatch.setenv("FLIGHTCHECK_TOKEN", TOKEN)
    monkeypatch.setenv("FLIGHTCHECK_MODE", "live")
    assert resolve_now(_auth({"X-Flightcheck-Mode": "nope"}), {"now": "2027-03-14T14:00:00", "origin": "XXA"}, {}) is None
    monkeypatch.setenv("FLIGHTCHECK_MODE", "fixtures")
    t = resolve_now({}, {"now": "2027-03-14T14:00:00", "origin": "XXA"}, {})
    assert t is not None
    assert t.utcoffset() == dt.timedelta(0)


def test_header_fixtures_requires_token(monkeypatch):
    monkeypatch.setenv("FLIGHTCHECK_TOKEN", TOKEN)
    monkeypatch.setenv("FLIGHTCHECK_MODE", "live")
    assert is_fixtures({"X-Flightcheck-Mode": "fixtures"}) is False
    assert is_fixtures(_auth({"X-Flightcheck-Mode": "fixtures"})) is True


def test_check_fixtures_now_too_early(monkeypatch):
    monkeypatch.setenv("FLIGHTCHECK_TOKEN", TOKEN)
    monkeypatch.setenv("FLIGHTCHECK_MODE", "live")
    from api.check import handle

    status, payload = handle(
        "POST",
        _auth({"X-Flightcheck-Mode": "fixtures"}),
        {},
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "now": "2027-03-01T12:00:00Z",
        },
    )
    assert status == 200
    assert payload["status"] == "too_early"
    assert payload.get("cost_usd_est") == 0.0


def test_live_check_drops_now(monkeypatch):
    monkeypatch.setenv("FLIGHTCHECK_TOKEN", TOKEN)
    monkeypatch.setenv("FLIGHTCHECK_MODE", "live")
    monkeypatch.delenv("FLIGHTAWARE_AEROAPI_KEY", raising=False)
    from api.check import handle
    from tests.conftest import scenario_fetch, NEAR_DEP
    from flightcheck.model import parse_time
    import flightcheck.check as chk

    orig = chk.check_flight

    def wrapped(req, **kw):
        assert kw.get("now") is None
        assert getattr(req, "now", None) is None
        kw.setdefault("fetch", scenario_fetch())
        kw["now"] = parse_time(NEAR_DEP)
        return orig(req, **kw)

    monkeypatch.setattr(chk, "check_flight", wrapped)
    status, payload = handle(
        "POST",
        _auth(),
        {},
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA", "now": "1999-01-01T00:00:00Z"},
    )
    assert status == 200
    assert payload["status"] != "too_early"


def test_track_tick_events_sink_with_now(monkeypatch):
    monkeypatch.setenv("FLIGHTCHECK_TOKEN", TOKEN)
    monkeypatch.setenv("FLIGHTCHECK_WEBHOOK_SECRET", "whsec")
    monkeypatch.setenv("FLIGHTCHECK_STORAGE", "memory")
    monkeypatch.setenv("FLIGHTCHECK_MODE", "fixtures")
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    use_store(None)
    store = reset_process_memory()
    use_store(store)
    monkeypatch.setattr(
        "flightcheck.track.deliver_webhook",
        lambda *a, **k: {"ok": True, "http": 200, "attempts": 1},
    )
    fx = {"X-Flightcheck-Mode": "fixtures"}
    status, started = handle_track(
        "POST",
        _auth(fx),
        {},
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "webhook_url": "https://example.test/sink",
            "now": "2027-03-14T18:00:00Z",
        },
        path="/api/track",
    )
    assert status == 200, started
    tid = started["tracking_id"]
    status, ticked = handle_tick(
        "POST",
        _auth(fx),
        {},
        {"now": "2027-03-15T15:00:00Z"},
        path="/api/tick",
    )
    assert status == 200, ticked
    events = list_event_log(store, tid)
    assert any(e.get("event") == "tracking_started" for e in events)
    use_store(None)


def test_storage_not_configured_http(monkeypatch):
    monkeypatch.setenv("FLIGHTCHECK_TOKEN", TOKEN)
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.delenv("FLIGHTCHECK_STORAGE", raising=False)
    monkeypatch.delenv("KV_REST_API_URL", raising=False)
    monkeypatch.delenv("KV_REST_API_TOKEN", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    use_store(None)
    status, payload = handle_track(
        "POST",
        _auth(),
        {},
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "webhook_url": "https://example.test/hook",
        },
        path="/api/track",
    )
    assert status == 503
    assert payload["error"]["code"] == "storage_not_configured"


def test_sink_verifies_raw_bytes(monkeypatch):
    monkeypatch.setenv("FLIGHTCHECK_TOKEN", TOKEN)
    monkeypatch.setenv("FLIGHTCHECK_WEBHOOK_SECRET", "whsec")
    monkeypatch.setenv("FLIGHTCHECK_STORAGE", "memory")
    use_store(MemoryStore())
    payload = {"event": "tracking_started", "tracking_id": "trk_x", "flight": "XX123"}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    sig = sign_body(raw, "whsec")
    status, out = handle_webhook_sink(
        "POST",
        {"X-Flightcheck-Signature": sig},
        {},
        payload,
        path="/api/webhook-sink",
        raw=raw,
    )
    assert status == 200
    assert out["signature_ok"] is True
    bad = handle_webhook_sink(
        "POST",
        {"X-Flightcheck-Signature": "sha256=deadbeef"},
        {},
        payload,
        path="/api/webhook-sink",
        raw=raw,
    )
    assert bad[1]["signature_ok"] is False
    use_store(None)
