"""Tracking: idempotent subscribe, tick dedupe, daily budget, signed webhooks, simulate."""

from __future__ import annotations

import datetime as dt

from flightcheck.events import deliver_webhook, sign_body, verify_signature
from flightcheck.model import parse_time
from flightcheck.simulate import run_simulate
from flightcheck.store import MemoryStore
from flightcheck.track import list_event_log, list_tracks, start_track, tick, untrack
from tests.conftest import NEAR_DEP, scenario_fetch


def _start(store, webhook="https://example.test/hook", **extra):
    delivered = []

    def deliver(url, payload, secret, **_k):
        delivered.append((url, payload, secret))
        return {"ok": True, "http": 200, "attempts": 1}

    out = start_track(
        {
            "flight": extra.get("flight", "XX123"),
            "date": extra.get("date", "2027-03-15"),
            "origin": extra.get("origin", "XXA"),
            "webhook_url": webhook,
            "ref": extra.get("ref", "trip-1"),
            **{k: v for k, v in extra.items() if k not in ("flight", "date", "origin", "ref")},
        },
        store=store,
        now=extra.get("now") or parse_time("2027-03-14T12:00:00Z"),
        fetch=extra.get("fetch") or scenario_fetch(),
        deliver=deliver,
        webhook_secret="whsec",
    )
    return out, delivered


def test_track_is_idempotent_for_same_ident_date_origin_webhook():
    store = MemoryStore()
    a, _ = _start(store)
    b, _ = _start(store)
    assert a["tracking_id"] == b["tracking_id"]
    assert b["idempotent"] is True
    assert len(list_tracks(store=store)) == 1


def test_untrack_removes_the_record():
    store = MemoryStore()
    a, _ = _start(store)
    gone = untrack(a["tracking_id"], store=store)
    assert gone["ok"] is True
    assert list_tracks(store=store) == []


def test_tick_dedupes_lookups_across_subscribers():
    store = MemoryStore()
    fetch = scenario_fetch(now=NEAR_DEP)
    now = parse_time(NEAR_DEP)
    _start(store, webhook="https://example.test/a", now=now, fetch=fetch)
    _start(store, webhook="https://example.test/b", now=now, fetch=fetch)
    calls_before = []

    def counting_fetch(path, params):
        calls_before.append(path)
        return fetch(path, params)

    due_now = now + dt.timedelta(hours=3)
    out = tick(store=store, now=due_now, fetch=counting_fetch, deliver=lambda *a, **k: {"ok": True}, budget=1.0)
    assert out["ran"] == 2
    flight_lookups = [p for p in calls_before if p.rstrip("/").endswith("XX123") or p.rstrip("/").endswith("XXX123")]
    # Shared cache: one ident lookup for both subscribers.
    assert len(flight_lookups) <= 1


def test_tick_skips_when_daily_budget_is_exhausted():
    store = MemoryStore()
    now = parse_time(NEAR_DEP)
    _start(store, now=now, fetch=scenario_fetch())
    out = tick(
        store=store,
        now=now + dt.timedelta(hours=6),
        fetch=scenario_fetch(),
        deliver=lambda *a, **k: {"ok": True},
        budget=0.0,
    )
    assert out["ran"] == 0
    assert out["skipped_budget"] >= 1


def test_webhook_hmac_signature_roundtrip():
    body = b'{"event":"landed","flight":"XX123"}'
    sig = sign_body(body, "whsec")
    assert sig.startswith("sha256=")
    assert verify_signature(body, sig, "whsec") is True
    assert verify_signature(body, "sha256=deadbeef", "whsec") is False
    posted = []

    def post(url, raw, headers):
        posted.append((url, raw, headers))
        return 200

    deliver_webhook(
        "https://example.test/sink",
        {"event": "landed", "flight": "XX123"},
        "whsec",
        post=post,
        sleep=lambda _s: None,
    )
    assert posted
    hdr = posted[0][2]["X-Flightcheck-Signature"]
    assert verify_signature(posted[0][1], hdr, "whsec")


def test_simulate_on_time_replays_essential_checkpoints_with_call_counts():
    out = run_simulate({
        "scenario": "on_time",
        "flight": "XX123",
        "date": "2027-06-22",
        "origin": "LHR",
        "destination": "SIN",
        "std_local": "09:40",
    })
    assert out["ok"] is True
    assert out["cost_usd"] == 0.0
    names = [step["checkpoint"] for step in out["timeline"]]
    assert "t_minus_24h" in names
    assert "t_minus_30m" in names
    assert "takeoff" in names
    assert names[-1] == "done"
    assert out["calls_total"] == 9
    assert all(step.get("calls", 0) >= 0 for step in out["timeline"])


def test_track_and_simulate_routes_require_token(monkeypatch):
    monkeypatch.setenv("FLIGHTCHECK_TOKEN", "route-token")
    monkeypatch.delenv("FLIGHTAWARE_AEROAPI_KEY", raising=False)
    from flightcheck.routes import handle_simulate, handle_track

    status, payload = handle_track("POST", {}, {}, {"flight": "XX123", "date": "2027-06-22"})
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"

    status, payload = handle_simulate("GET", {"Authorization": "Bearer route-token"}, {}, {})
    assert status == 200
    assert "on_time" in payload["scenarios"]


def test_event_log_records_tracking_started():
    store = MemoryStore()
    started, delivered = _start(store)
    events = list_event_log(store, started["tracking_id"])
    names = [e.get("event") for e in events]
    assert "tracking_started" in names
    assert delivered
    assert delivered[0][1]["event"] == "tracking_started"
