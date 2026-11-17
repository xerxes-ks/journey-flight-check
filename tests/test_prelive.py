"""Pre-live hardening: isolation, budgets, idempotency, webhooks, concurrency."""

from __future__ import annotations

import datetime as dt

import pytest

from flightcheck.check import check_flight, parse_request
from flightcheck.events import deliver_webhook
from flightcheck.exceptions import BudgetExceeded, Conflict, InvalidBudget, IsolationError
from flightcheck.mode import fixture_fetch
from flightcheck.model import parse_time
from flightcheck.money import parse_money
from flightcheck.routes import handle_tick, handle_track, handle_tracked
from flightcheck.store import MemoryStore, namespaced_store
from flightcheck.track import (
    load_track,
    reserve_spend,
    save_track,
    spent_today,
    start_track,
    tick,
    untrack,
)
from tests.conftest import NEAR_DEP, load_fx, scenario_fetch

TOKEN = "prelive-token"
NOW = parse_time("2027-03-14T12:00:00Z")


class InterleaveStore(MemoryStore):
    """Shared backing store that runs a rival worker between CAS read and write."""

    def __init__(self):
        super().__init__()
        self.on_before_cas = None
        self._in_hook = False

    def put_json_if_current(self, path, obj, expected_version):
        if self.on_before_cas and not self._in_hook:
            self._in_hook = True
            try:
                self.on_before_cas(path)
            finally:
                self._in_hook = False
        return super().put_json_if_current(path, obj, expected_version)


def _payload(**extra):
    data = {
        "flight": extra.pop("flight", "XX123"),
        "date": extra.pop("date", "2027-03-15"),
        "origin": extra.pop("origin", "XXA"),
        "webhook_url": extra.pop("webhook_url", "https://example.test/hook"),
        "ref": extra.pop("ref", "trip-1"),
    }
    data.update(extra)
    return data


def _start(store, *, namespace="live", fetch=None, deliver=None, now=None, budget=None, **extra):
    delivered = []

    def _deliver(url, payload, secret, **_k):
        delivered.append(payload)
        if deliver:
            return deliver(url, payload, secret)
        return {"ok": True, "http": 200, "attempts": 1}

    out = start_track(
        _payload(**extra),
        store=store,
        now=now or NOW,
        fetch=fetch or scenario_fetch(),
        deliver=_deliver,
        webhook_secret="whsec",
        namespace=namespace,
        budget=budget,
    )
    return out, delivered


def test_parse_money_rejects_nan_inf_and_keeps_zero():
    assert parse_money(0, 0.05) == 0.0
    assert parse_money("0", 0.05) == 0.0
    assert parse_money(None, 0.05) == 0.05
    for bad in (float("nan"), float("inf"), float("-inf"), "NaN", "Infinity", "-Infinity", "inf"):
        with pytest.raises(InvalidBudget):
            parse_money(bad, 0.05)
    req = parse_request({"flight": "XX123", "date": "2027-03-15", "budget": 0})
    assert req.budget_usd == 0.0
    assert req.cost_per_call == 0.005


def test_budget_zero_means_zero_provider_calls():
    calls = []

    def fetch(path, params):
        calls.append(path)
        return scenario_fetch()(path, params)

    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA", "budget": 0},
        fetch=fetch,
        now=parse_time(NEAR_DEP),
    )
    assert calls == []
    assert r.error and r.error["code"] == "budget_exceeded"


def test_malformed_budget_makes_no_provider_calls():
    calls = []

    def fetch(path, params):
        calls.append(path)
        return 200, {}

    with pytest.raises(InvalidBudget):
        check_flight(
            {"flight": "XX123", "date": "2027-03-15", "budget": "NaN"},
            fetch=fetch,
            now=parse_time(NEAR_DEP),
        )
    assert calls == []


def test_http_and_library_ignore_client_unit_price():
    req = parse_request({"flight": "XX123", "date": "2027-03-15", "cost_per_call": 9.99})
    assert req.cost_per_call == 0.005


def test_daily_cap_covers_registration_and_check(monkeypatch):
    store = MemoryStore()
    monkeypatch.setenv("FLIGHTCHECK_DAILY_BUDGET_USD", "0")
    with pytest.raises(BudgetExceeded):
        _start(store, now=parse_time(NEAR_DEP), fetch=scenario_fetch(), budget=0)
    assert list(store.list_prefix("tracks/")) == []


def test_overlapping_reservations_cannot_exceed_cap():
    store = InterleaveStore()
    now = parse_time("2027-03-15T00:00:00Z")
    assert reserve_spend(store, now, 0.40, 0.50) is True

    def rival(_path):
        reserve_spend(store, now, 0.10, 0.50)

    store.on_before_cas = rival
    assert reserve_spend(store, now, 0.10, 0.50) is False
    assert spent_today(store, now) == pytest.approx(0.50)


def test_ambiguous_lookup_is_still_charged():
    store = MemoryStore()
    now = parse_time(NEAR_DEP)
    calls = []

    def fetch(path, params):
        calls.append(path)
        return 500, {"title": "upstream"}

    def guard(amount):
        return reserve_spend(store, now, amount, 0.50)

    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "XXA"},
        fetch=fetch,
        now=now,
        spend_guard=guard,
    )
    assert calls
    assert r.error
    assert spent_today(store, now) == pytest.approx(0.005)


def test_fixture_and_live_share_backing_store_but_not_tracks():
    backing = MemoryStore()
    live = backing
    fx = namespaced_store(backing, "fixtures")
    started, _ = _start(fx, namespace="fixtures", fetch=fixture_fetch(now=NOW), now=NOW)
    assert started["ok"] is True
    assert backing.get_json(f"fixtures/tracks/{started['tracking_id']}.json")
    assert backing.get_json(f"tracks/{started['tracking_id']}.json") is None
    live_tick = tick(store=live, namespace="live", now=NOW + dt.timedelta(hours=6), fetch=scenario_fetch())
    assert live_tick["due"] == 0
    assert live_tick["ran"] == 0
    rec = load_track(fx, started["tracking_id"])
    assert rec["checkpoint"] == "t_minus_24h"
    with pytest.raises(IsolationError):
        start_track(
            _payload(),
            store=live,
            namespace="live",
            fetch=fixture_fetch(now=NOW),
            now=NOW,
            deliver=lambda *a, **k: {"ok": True},
            webhook_secret="x",
        )


def test_operator_console_fixture_flow_uses_isolated_namespace(monkeypatch):
    monkeypatch.setenv("FLIGHTCHECK_TOKEN", TOKEN)
    monkeypatch.setenv("FLIGHTCHECK_STORAGE", "memory")
    monkeypatch.setenv("FLIGHTCHECK_WEBHOOK_SECRET", "whsec")
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.delenv("FLIGHTCHECK_MODE", raising=False)
    from flightcheck.store import reset_process_memory, use_store

    backing = reset_process_memory()
    use_store(backing)
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-Flightcheck-Mode": "fixtures",
    }
    status, started = handle_track(
        "POST",
        headers,
        {},
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "webhook_url": "https://example.test/sink",
            "now": "2027-03-14T18:00:00Z",
            "ref": "console",
        },
        path="/api/track",
    )
    assert status == 200, started
    tid = started["tracking_id"]
    assert backing.get_json(f"fixtures/tracks/{tid}.json")
    assert backing.get_json(f"tracks/{tid}.json") is None
    live_headers = {"Authorization": f"Bearer {TOKEN}"}
    status, listed = handle_tracked("GET", live_headers, {}, {}, path="/api/tracked")
    assert status == 200
    assert listed["count"] == 0
    status, ticked = handle_tick("POST", live_headers, {}, {}, path="/api/tick")
    assert status == 200
    assert ticked["due"] == 0
    rec = backing.get_json(f"fixtures/tracks/{tid}.json")
    assert rec and rec.get("namespace") == "fixtures"
    use_store(None)


def test_exact_retry_reuses_track_changed_fingerprint_conflicts():
    store = MemoryStore()
    a, _ = _start(store, ref="trip-1")
    b, _ = _start(store, ref="trip-1")
    assert b["idempotent"] is True
    assert b["tracking_id"] == a["tracking_id"]
    with pytest.raises(Conflict) as exc:
        _start(store, ref="trip-2")
    assert exc.value.http_status == 409
    with pytest.raises(Conflict):
        _start(store, ref="trip-1", onward={"flight": "YY456", "date": "2027-03-16"})
    with pytest.raises(Conflict):
        _start(store, ref="trip-1", cadence="dense")
    untrack(a["tracking_id"], store=store)
    c, _ = _start(store, ref="trip-2")
    assert c["idempotent"] is False
    assert c["tracking_id"] != a["tracking_id"]


def test_http_conflict_is_409(monkeypatch):
    monkeypatch.setenv("FLIGHTCHECK_TOKEN", TOKEN)
    monkeypatch.setenv("FLIGHTCHECK_STORAGE", "memory")
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    from flightcheck.store import reset_process_memory, use_store

    use_store(reset_process_memory())
    headers = {"Authorization": f"Bearer {TOKEN}", "X-Flightcheck-Mode": "fixtures"}
    body = {
        "flight": "XX123",
        "date": "2027-03-15",
        "origin": "XXA",
        "webhook_url": "https://example.test/hook",
        "ref": "one",
        "now": "2027-03-14T18:00:00Z",
    }
    status, first = handle_track("POST", headers, {}, body, path="/api/track")
    assert status == 200, first
    body2 = dict(body)
    body2["ref"] = "two"
    status, payload = handle_track("POST", headers, {}, body2, path="/api/track")
    assert status == 409
    assert payload["error"]["code"] == "conflict"
    use_store(None)


def test_failed_callback_retries_without_paid_lookup():
    store = MemoryStore()
    attempts = []

    def deliver(url, payload, secret, **_k):
        attempts.append(payload["event_id"])
        if len(attempts) == 1:
            return {"ok": False, "http": 503, "attempts": 1}
        return {"ok": True, "http": 200, "attempts": 1}

    started, _ = _start(store, deliver=deliver, now=NOW)
    tid = started["tracking_id"]
    rec = load_track(store, tid)
    rec["next_check_at"] = "2099-01-01T00:00:00Z"
    save_track(store, rec)
    fetches = []

    def fetch(path, params):
        fetches.append(path)
        return scenario_fetch()(path, params)

    out = tick(store=store, now=NOW, fetch=fetch, deliver=deliver, webhook_secret="whsec", budget=1.0)
    assert out["ran"] == 0
    assert fetches == []
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    pending = store.get_json(f"pending/{tid}/{attempts[0]}.json")
    assert pending["acked"] is True
    out2 = tick(store=store, now=NOW, fetch=fetch, deliver=deliver, webhook_secret="whsec", budget=1.0)
    assert out2["ran"] == 0
    assert len(attempts) == 2


def test_deliver_webhook_requires_https_allowlist_and_secret(monkeypatch):
    posted = []

    def post(url, body, headers, timeout=3):
        posted.append(url)
        return 200

    monkeypatch.delenv("FLIGHTCHECK_WEBHOOK_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("VERCEL_URL", raising=False)
    skipped = deliver_webhook("https://example.test/hook", {"event": "landed"}, "sec")
    assert skipped["skipped"] == "host_not_allowed"
    monkeypatch.setenv("FLIGHTCHECK_WEBHOOK_ALLOWED_HOSTS", "example.test")
    no_secret = deliver_webhook("https://example.test/hook", {"event": "landed"}, "")
    assert no_secret["skipped"] == "missing_secret"
    http = deliver_webhook("http://example.test/hook", {"event": "landed"}, "sec")
    assert http["skipped"] == "https_required"
    assert posted == []
    ok = deliver_webhook(
        "https://example.test/hook",
        {"event": "landed"},
        "sec",
        post=post,
        sleep=lambda _s: None,
    )
    assert ok["ok"] is True
    assert posted == ["https://example.test/hook"]


def test_overlapping_ticks_do_not_duplicate_paid_work():
    store = InterleaveStore()
    now = parse_time(NEAR_DEP)
    started, _ = _start(store, now=now, fetch=scenario_fetch())
    rec = load_track(store, started["tracking_id"])
    rec["next_check_at"] = "2020-01-01T00:00:00Z"
    rec["ended"] = False
    save_track(store, rec)
    fetches = []

    def fetch(path, params):
        fetches.append(path)
        return scenario_fetch(now=NEAR_DEP)(path, params)

    def rival(_path):
        if _path != "locks/tick.json":
            return
        tick(store=store, now=now, fetch=fetch, deliver=lambda *a, **k: {"ok": True}, budget=1.0)

    store.on_before_cas = rival
    out = tick(store=store, now=now, fetch=fetch, deliver=lambda *a, **k: {"ok": True}, budget=1.0)
    assert out["ok"] is True
    flight_lookups = [p for p in fetches if "XX123" in p or "XXX123" in p]
    assert len(flight_lookups) <= 1
    loaded = load_track(store, started["tracking_id"])
    assert CHECKPOINT_ORDER(loaded["checkpoint"]) >= CHECKPOINT_ORDER("t_minus_24h")
    spent = spent_today(store, now)
    assert spent >= 0
    assert spent <= 0.05 + 1e-9


def CHECKPOINT_ORDER(name):
    from flightcheck.cadence import CHECKPOINTS
    return list(CHECKPOINTS).index(name) if name in CHECKPOINTS else -1


def test_checkpoint_does_not_move_backwards():
    store = MemoryStore()
    started, _ = _start(store, now=parse_time(NEAR_DEP), fetch=scenario_fetch())
    rec = load_track(store, started["tracking_id"])
    rec["checkpoint"] = "landing"
    rec["next_check_at"] = "2020-01-01T00:00:00Z"
    rec["ended"] = False
    save_track(store, rec)
    tick(store=store, now=parse_time(NEAR_DEP), fetch=scenario_fetch(), deliver=lambda *a, **k: {"ok": True})
    loaded = load_track(store, started["tracking_id"])
    assert CHECKPOINT_ORDER(loaded["checkpoint"]) >= CHECKPOINT_ORDER("landing")


def test_onward_reason_untracked_and_not_found():
    early = check_flight(
        {
            "flight": "XX123",
            "date": "2027-04-01",
            "origin": "LAX",
            "onward": {"flight": "YY456", "date": "2027-04-01"},
        },
        fetch=scenario_fetch(),
        now=parse_time(NEAR_DEP),
    )
    assert early.connection is None
    assert early.connection_reason == "untracked"

    def fetch(path, params):
        if "ZZ999" in path or "ZZZ999" in path:
            return 404, {"title": "Not Found"}
        if path.startswith("/flights/XX123") or path.startswith("/flights/XXX123"):
            return 200, load_fx("xx123.json")
        if "XXX122" in path:
            return 200, load_fx("xx122.json")
        return 404, {"title": "Not Found"}

    missing = check_flight(
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "onward": {"flight": "ZZ999", "date": "2027-03-15", "origin": "XXB"},
        },
        fetch=fetch,
        now=parse_time(NEAR_DEP),
    )
    assert missing.connection is None
    assert missing.connection_reason == "not_found"
