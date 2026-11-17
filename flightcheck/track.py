"""Flight tracking: persist checkpoints, tick due work, fan out events."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import uuid
from typing import Optional

from . import check as check_mod
from .cadence import CHECKPOINTS
from .events import deliver_webhook, detect_events, event_payload, require_webhook_url
from .exceptions import BudgetExceeded, Conflict, IsolationError, error_dict
from .model import UTC, DEFAULT_BUDGET_USD, normalize_ident, parse_date, parse_time
from .money import HTTP_BUDGET_CAP, MAX_DAILY_BUDGET_USD, parse_money, request_budget
from .store import (
    EVENTS_MAX,
    FIXTURE_NAMESPACE,
    LIVE_NAMESPACE,
    PENDING_MAX,
    SINK_MAX,
    Storage,
    get_store,
    namespaced_store,
)

DEFAULT_DAILY_BUDGET = 0.50
CLAIM_PATH = "locks/tick.json"
CLAIM_TTL_S = 45
PENDING_TTL = dt.timedelta(days=7)
CHECKPOINT_RANK = {name: i for i, name in enumerate(CHECKPOINTS)}


def request_namespace(headers: dict | None = None) -> str:
    from .mode import is_fixtures
    return FIXTURE_NAMESPACE if is_fixtures(headers) else LIVE_NAMESPACE


def _idemp_hash(flight: str, date: str, origin: str | None, webhook_url: str) -> str:
    raw = "|".join([
        normalize_ident(flight),
        str(date)[:10],
        (origin or "").upper(),
        (webhook_url or "").strip(),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _config_fp(data: dict) -> str:
    onward = data.get("onward") if isinstance(data.get("onward"), dict) else {}
    blob = {
        "ref": str(data.get("ref") or ""),
        "policy": str(data.get("cadence") or data.get("policy") or "essential"),
        "alert_delay_min": int(data.get("alert_delay_min") or 45),
        "onward_flight": normalize_ident(str(onward.get("flight") or onward.get("ident") or "")),
        "onward_date": str(onward.get("date") or "")[:10],
        "onward_origin": str(onward.get("origin") or "").upper(),
    }
    return hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()[:24]


def _group_key(flight: str, date: str, origin: str | None) -> str:
    return hashlib.sha256(
        f"{normalize_ident(flight)}|{str(date)[:10]}|{(origin or '').upper()}".encode()
    ).hexdigest()[:24]


def track_path(tid: str) -> str:
    return f"tracks/{tid}.json"


def event_path(tid: str, ts: str, seq: int) -> str:
    return f"events/{tid}/{ts}-{seq}.json"


def pending_path(tid: str, event_id: str) -> str:
    return f"pending/{tid}/{event_id}.json"


def sink_path(ts: str, seq: int = 0) -> str:
    return f"sink/{ts}-{seq}.json"


def budget_path(now: dt.datetime) -> str:
    day = now.astimezone(UTC).strftime("%Y-%m-%d")
    return f"budget/{day}.json"


def new_tracking_id() -> str:
    return "trk_" + uuid.uuid4().hex[:20]


def make_event_id(tid: str, event: str, checked_at: str, checkpoint: str) -> str:
    raw = f"{tid}|{event}|{checked_at or ''}|{checkpoint or ''}"
    return "evt_" + hashlib.sha256(raw.encode()).hexdigest()[:20]


def daily_budget() -> float:
    raw = os.environ.get("FLIGHTCHECK_DAILY_BUDGET_USD")
    if raw in (None, ""):
        return DEFAULT_DAILY_BUDGET
    try:
        return parse_money(raw, DEFAULT_DAILY_BUDGET, max_value=MAX_DAILY_BUDGET_USD, field="daily_budget")
    except Exception:
        return 0.0


def spent_today(store: Storage, now: dt.datetime) -> float:
    rec = store.get_json(budget_path(now)) or {}
    return float(rec.get("spent") or 0)


def add_spend(store: Storage, now: dt.datetime, amount: float) -> float:
    path = budget_path(now)
    rec = store.get_json(path) or {"spent": 0.0, "version": 0}
    if amount <= 0:
        return float(rec.get("spent") or 0)
    expected = int(rec.get("version") or 0)
    nxt = {"spent": round(float(rec.get("spent") or 0) + float(amount), 6), "version": expected}
    if not store.put_json_if_current(path, nxt, expected):
        rec = store.get_json(path) or rec
        return float(rec.get("spent") or 0)
    return float(nxt["spent"])


def reserve_spend(store: Storage, now: dt.datetime, amount: float, cap: float) -> bool:
    """Atomically reserve *amount* against the daily cap. Never refunds."""
    cost = float(amount or 0)
    if cost <= 0:
        return True
    limit = float(cap)
    if not (limit > 0):
        return False
    path = budget_path(now)
    for _ in range(8):
        rec = store.get_json(path) or {"spent": 0.0, "version": 0}
        spent = float(rec.get("spent") or 0)
        if spent + cost > limit + 1e-9:
            return False
        expected = int(rec.get("version") or 0)
        nxt = {"spent": round(spent + cost, 6), "version": expected}
        if store.put_json_if_current(path, nxt, expected):
            return True
    return False


def load_track(store: Storage, tid: str) -> Optional[dict]:
    return store.get_json(track_path(tid))


def save_track(store: Storage, rec: dict) -> bool:
    """Write track JSON. Skips if a newer version is already stored."""
    tid = rec["tracking_id"]
    expected = int(rec.get("version") or 0)
    return store.put_json_if_current(track_path(tid), rec, expected)


def append_event(store: Storage, tid: str, payload: dict) -> None:
    ts = (payload.get("checked_at") or dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    ts = str(ts).replace(":", "").replace("-", "")
    existing = store.list_prefix(f"events/{tid}/")
    seq = len(existing) + 1
    store.put_json(event_path(tid, ts, seq), payload)
    all_paths = store.list_prefix(f"events/{tid}/")
    extra = all_paths[:-EVENTS_MAX] if len(all_paths) > EVENTS_MAX else []
    for path in extra:
        store.delete(path)


def list_event_log(store: Storage, tid: str) -> list[dict]:
    paths = store.list_prefix(f"events/{tid}/")
    out = []
    for path in reversed(paths):
        item = store.get_json(path)
        if item:
            out.append(item)
        if len(out) >= EVENTS_MAX:
            break
    return out


def public_track(rec: dict) -> dict:
    snap = rec.get("last_snapshot") or {}
    return {
        "tracking_id": rec.get("tracking_id"),
        "flight": rec.get("flight"),
        "date": rec.get("date"),
        "origin": rec.get("origin"),
        "ref": rec.get("ref") or "",
        "checkpoint": rec.get("checkpoint"),
        "phase": rec.get("phase") or snap.get("phase"),
        "band": rec.get("band") or snap.get("band"),
        "impact_band": rec.get("impact_band") if rec.get("impact_band") is not None else snap.get("impact_band"),
        "next_check_local": rec.get("next_check_local"),
        "next_check_at": rec.get("next_check_at"),
        "ended": bool(rec.get("ended")),
        "summary": (snap.get("summary") if isinstance(snap, dict) else None),
        "namespace": rec.get("namespace") or LIVE_NAMESPACE,
    }


def _find_idemp(store: Storage, flight, date, origin, webhook) -> Optional[dict]:
    want = _idemp_hash(flight, date, origin, webhook)
    for path in store.list_prefix("tracks/"):
        rec = store.get_json(path)
        if rec and rec.get("idemp") == want:
            return rec
    return None


def _assert_isolation(fetch, namespace: str) -> None:
    if getattr(fetch, "_flightcheck_fixtures", False) and namespace != FIXTURE_NAMESPACE:
        raise IsolationError(
            "fixture tracking cannot use the live store; pass namespace=fixtures"
        )


def _spend_guard(store: Storage, now: dt.datetime, cap: float):
    def guard(amount: float) -> bool:
        return reserve_spend(store, now, amount, cap)
    return guard


def start_track(
    data: dict,
    *,
    store: Storage | None = None,
    now: dt.datetime | None = None,
    fetch=None,
    deliver=None,
    webhook_secret: str | None = None,
    namespace: str | None = None,
    budget: float | None = None,
    validate_webhook: bool = True,
) -> dict:
    now = now or dt.datetime.now(UTC)
    flight = data.get("flight") or data.get("ident")
    date = data.get("date")
    webhook = (data.get("webhook_url") or "").strip()
    if not flight or not date:
        raise ValueError("flight and date are required")
    date = parse_date(date).isoformat()
    if not webhook:
        raise ValueError("webhook_url is required")
    per_request = min(request_budget(data, DEFAULT_BUDGET_USD), HTTP_BUDGET_CAP)
    ns = namespace or LIVE_NAMESPACE
    _assert_isolation(fetch, ns)
    store = namespaced_store(store or get_store(), ns)
    origin = (str(data["origin"]).upper() if data.get("origin") else None)
    existing = _find_idemp(store, flight, date, origin, webhook)
    if existing:
        fp = _config_fp(data)
        if existing.get("config_fp") == fp:
            return {
                "ok": True,
                "idempotent": True,
                "tracking_id": existing["tracking_id"],
                "next_check_local": existing.get("next_check_local"),
                "checkpoint": existing.get("checkpoint"),
            }
        raise Conflict(
            "subscription already exists with a different ref, onward, policy or threshold"
        )
    if validate_webhook:
        require_webhook_url(webhook)
    tid = new_tracking_id()
    rec = {
        "tracking_id": tid,
        "flight": normalize_ident(str(flight)),
        "date": str(date)[:10],
        "origin": origin,
        "policy": str(data.get("cadence") or data.get("policy") or "essential"),
        "alert_delay_min": int(data.get("alert_delay_min") or 45),
        "onward": data.get("onward") if isinstance(data.get("onward"), dict) else None,
        "webhook_url": webhook,
        "ref": str(data.get("ref") or ""),
        "created_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checkpoint": "t_minus_24h",
        "attempts": 0,
        "phase": "scheduled",
        "band": "unknown",
        "impact_band": None,
        "next_check_local": None,
        "next_check_at": None,
        "last_snapshot": None,
        "fail_count": 0,
        "ended": False,
        "previous_gate": None,
        "idemp": _idemp_hash(flight, date, origin, webhook),
        "config_fp": _config_fp(data),
        "namespace": ns,
        "event_seq": 0,
        "version": 0,
    }
    cap = daily_budget() if budget is None else float(budget)
    result = _run_check(
        rec, now=now, fetch=fetch,
        spend_guard=_spend_guard(store, now, cap),
        budget_usd=per_request,
    )
    err = getattr(result, "error", None) or {}
    cost = float(getattr(result, "cost_usd_est", 0) or 0)
    if err.get("code") == "budget_exceeded" and cost == 0:
        if per_request <= 0:
            raise BudgetExceeded("budget too small")
        raise BudgetExceeded("daily budget reached")
    _apply_result(
        store, rec, result, now=now, first=True,
        deliver=deliver, webhook_secret=webhook_secret,
        charge=False,
    )
    return {
        "ok": True,
        "idempotent": False,
        "tracking_id": tid,
        "next_check_local": rec.get("next_check_local"),
        "checkpoint": rec.get("checkpoint"),
    }


def untrack(tid: str, *, store: Storage | None = None, namespace: str | None = None) -> dict:
    store = namespaced_store(store or get_store(), namespace or LIVE_NAMESPACE)
    rec = load_track(store, tid)
    if not rec:
        return {"ok": False, "error": error_dict("not_found", "tracking_id not found")}
    store.delete(track_path(tid))
    return {"ok": True, "tracking_id": tid}


def list_tracks(*, store: Storage | None = None, namespace: str | None = None) -> list[dict]:
    store = namespaced_store(store or get_store(), namespace or LIVE_NAMESPACE)
    out = []
    for path in store.list_prefix("tracks/"):
        rec = store.get_json(path)
        if rec:
            out.append(public_track(rec))
    out.sort(key=lambda r: (r.get("date") or "", r.get("flight") or ""))
    return out


def _run_check(rec: dict, *, now: dt.datetime, fetch=None, adapter=None, spend_guard=None, budget_usd=None):
    payload = {
        "flight": rec["flight"],
        "date": rec["date"],
        "origin": rec.get("origin"),
        "cadence": rec.get("policy") or "essential",
        "alert_delay_min": rec.get("alert_delay_min") or 45,
        "previous_checkpoint": rec.get("checkpoint"),
        "previous_phase": rec.get("phase"),
        "attempts": rec.get("attempts") or 0,
        "previous_gate": rec.get("previous_gate"),
    }
    if rec.get("onward"):
        payload["onward"] = rec["onward"]
    if budget_usd is not None:
        payload["budget_usd"] = budget_usd
    return check_mod.check_flight(
        payload, fetch=fetch, adapter=adapter, now=now, spend_guard=spend_guard,
    )


def _next_at_utc(result: dict, now: dt.datetime) -> Optional[str]:
    nxt = result.get("next_check_local")
    if not nxt:
        return None
    local = nxt.get("local") if isinstance(nxt, dict) else None
    t = parse_time(local)
    if t is None:
        minutes = result.get("next_check_in_minutes")
        if minutes:
            t = now + dt.timedelta(minutes=int(minutes))
    if t is None:
        return None
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _checkpoint_ok(old: str | None, new: str | None) -> str | None:
    if not new:
        return old
    if not old:
        return new
    if CHECKPOINT_RANK.get(new, -1) >= CHECKPOINT_RANK.get(old, -1):
        return new
    return old


def _apply_result(
    store: Storage,
    rec: dict,
    result,
    *,
    now: dt.datetime,
    first: bool = False,
    deliver=None,
    webhook_secret: str | None = None,
    charge: bool = False,
) -> list[str]:
    data = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    fresh = load_track(store, rec["tracking_id"])
    if fresh and int(fresh.get("version") or 0) > int(rec.get("version") or 0):
        rec.clear()
        rec.update(fresh)
        return []
    prev = rec.get("last_snapshot")
    err = data.get("error") or {}
    if err.get("retryable"):
        rec["fail_count"] = int(rec.get("fail_count") or 0) + 1
    else:
        rec["fail_count"] = 0
    names = detect_events(prev, data, first=first, fail_count=int(rec.get("fail_count") or 0))
    rec["last_snapshot"] = data
    rec["checkpoint"] = _checkpoint_ok(rec.get("checkpoint"), data.get("checkpoint"))
    rec["attempts"] = data.get("attempts") or 0
    rec["phase"] = data.get("phase")
    rec["band"] = data.get("band")
    rec["impact_band"] = data.get("impact_band")
    rec["next_check_local"] = data.get("next_check_local")
    rec["next_check_at"] = _next_at_utc(data, now)
    rec["ended"] = bool(data.get("final"))
    gate = ((data.get("flight") or {}).get("gate_origin")) or rec.get("previous_gate")
    rec["previous_gate"] = gate
    if not save_track(store, rec):
        return []
    if charge:
        add_spend(store, now, float(data.get("cost_usd_est") or 0))
    secret = webhook_secret if webhook_secret is not None else os.environ.get("FLIGHTCHECK_WEBHOOK_SECRET") or ""
    sent = []
    for name in names:
        seq = int(rec.get("event_seq") or 0) + 1
        rec["event_seq"] = seq
        payload = event_payload(name, rec, data)
        eid = make_event_id(rec["tracking_id"], name, payload.get("checked_at") or "", payload.get("checkpoint") or "")
        payload["event_id"] = eid
        payload["seq"] = seq
        append_event(store, rec["tracking_id"], payload)
        queued = enqueue_pending(store, rec, payload, now=now)
        if queued.get("acked"):
            sent.append(name)
            continue
        dest = rec.get("webhook_url")
        if dest:
            out = _send_webhook(dest, payload, secret, deliver=deliver)
            if out.get("ok"):
                ack_pending(store, rec["tracking_id"], eid)
        sent.append(name)
    save_track(store, rec)
    return sent


def enqueue_pending(store: Storage, rec: dict, payload: dict, *, now: dt.datetime) -> dict:
    tid = rec["tracking_id"]
    eid = payload["event_id"]
    path = pending_path(tid, eid)
    existing = store.get_json(path)
    if existing and existing.get("acked"):
        return existing
    item = {
        "event_id": eid,
        "seq": payload.get("seq"),
        "tracking_id": tid,
        "url": rec.get("webhook_url"),
        "payload": payload,
        "acked": False,
        "attempts": int((existing or {}).get("attempts") or 0),
        "created_at": (existing or {}).get("created_at") or now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": int((existing or {}).get("version") or 0),
    }
    store.put_json_if_current(path, item, int(item["version"]))
    paths = store.list_prefix(f"pending/{tid}/")
    extra = paths[:-PENDING_MAX] if len(paths) > PENDING_MAX else []
    for old in extra:
        store.delete(old)
    return item


def ack_pending(store: Storage, tid: str, event_id: str) -> None:
    path = pending_path(tid, event_id)
    rec = store.get_json(path)
    if not rec:
        return
    rec["acked"] = True
    rec["payload"] = None
    store.put_json_if_current(path, rec, int(rec.get("version") or 0))


def flush_pending(store: Storage, *, now: dt.datetime, deliver=None, webhook_secret: str | None = None) -> int:
    secret = webhook_secret if webhook_secret is not None else os.environ.get("FLIGHTCHECK_WEBHOOK_SECRET") or ""
    recovered = 0
    for path in store.list_prefix("pending/"):
        item = store.get_json(path)
        if not item:
            continue
        created = parse_time(item.get("created_at"))
        if created and (now - created) > PENDING_TTL:
            store.delete(path)
            continue
        if item.get("acked"):
            continue
        payload = item.get("payload") or {}
        dest = item.get("url") or ""
        if not dest or not payload:
            continue
        out = _send_webhook(dest, payload, secret, deliver=deliver)
        item["attempts"] = int(item.get("attempts") or 0) + 1
        if out.get("ok"):
            item["acked"] = True
            item["payload"] = None
            recovered += 1
        store.put_json_if_current(path, item, int(item.get("version") or 0))
    return recovered


def _send_webhook(url: str, payload: dict, secret: str, *, deliver=None) -> dict:
    if deliver is None:
        scheme = str(url or "").split(":", 1)[0].lower()
        if scheme not in ("http", "https"):
            return {"ok": False, "http": 0, "attempts": 0, "skipped": "internal"}
        sender = deliver_webhook
    else:
        sender = deliver
    try:
        out = sender(url, payload, secret)
    except Exception:
        return {"ok": False, "http": 0, "attempts": 1}
    if isinstance(out, dict):
        return out
    return {"ok": bool(out), "http": 0, "attempts": 1}


def acquire_tick_claim(store: Storage, now: dt.datetime, holder: str) -> bool:
    rec = store.get_json(CLAIM_PATH)
    if rec:
        exp = parse_time(rec.get("expires_at"))
        have = rec.get("holder") or ""
        if exp and exp > now and have and have != holder:
            return False
        expected = int(rec.get("version") or 0)
    else:
        expected = 0
    nxt = {
        "holder": holder,
        "expires_at": (now + dt.timedelta(seconds=CLAIM_TTL_S)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": expected,
    }
    return store.put_json_if_current(CLAIM_PATH, nxt, expected)


def release_tick_claim(store: Storage, holder: str) -> None:
    rec = store.get_json(CLAIM_PATH)
    if rec and rec.get("holder") == holder:
        store.delete(CLAIM_PATH)


class _LookupCache:
    """One AeroAPI lookup per ident/date/origin, shared across subscribers."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = getattr(inner, "calls", [])
        self.key = getattr(inner, "key", None)
        self.spend_guard = getattr(inner, "spend_guard", None)
        self._cache = {}
        self._inbound = {}

    @property
    def spent(self):
        return float(getattr(self.inner, "spent", 0.0) or 0)

    def lookup(self, ident, date, **kw):
        key = (normalize_ident(ident), str(date)[:10], (kw.get("origin") or ""), bool(kw.get("fetch_inbound")))
        if key not in self._cache:
            self._cache[key] = self.inner.lookup(ident, date, **kw)
        return self._cache[key]

    def attach_inbound(self, flight, **kw):
        fid = getattr(flight, "inbound_fa_flight_id", "") or ""
        if fid and fid in self._inbound:
            flight.inbound = self._inbound[fid]
            return flight
        out = self.inner.attach_inbound(flight, **kw)
        if fid:
            self._inbound[fid] = getattr(flight, "inbound", None)
        return out


def tick(
    *,
    store: Storage | None = None,
    now: dt.datetime | None = None,
    fetch=None,
    deliver=None,
    webhook_secret: str | None = None,
    budget: float | None = None,
    namespace: str | None = None,
) -> dict:
    ns = namespace or LIVE_NAMESPACE
    _assert_isolation(fetch, ns)
    store = namespaced_store(store or get_store(), ns)
    now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    cap = daily_budget() if budget is None else float(budget)
    holder = "tick_" + uuid.uuid4().hex[:12]
    claimed = acquire_tick_claim(store, now, holder)
    if not claimed:
        return {
            "ok": True,
            "due": 0,
            "ran": 0,
            "skipped_budget": 0,
            "skipped_claim": 1,
            "events": 0,
            "spent_usd": spent_today(store, now),
            "budget_usd": cap,
            "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    try:
        recovered = flush_pending(store, now=now, deliver=deliver, webhook_secret=webhook_secret)
        due: list[dict] = []
        for path in store.list_prefix("tracks/"):
            rec = store.get_json(path)
            if not rec or rec.get("ended"):
                continue
            if rec.get("namespace") == FIXTURE_NAMESPACE and ns == LIVE_NAMESPACE:
                continue
            nxt = rec.get("next_check_at")
            if not nxt:
                due.append(rec)
                continue
            when = parse_time(nxt)
            if when is None or when <= now:
                due.append(rec)
        groups: dict[str, list[dict]] = {}
        for rec in due:
            groups.setdefault(_group_key(rec["flight"], rec["date"], rec["origin"]), []).append(rec)
        ran = 0
        skipped_budget = 0
        events = recovered
        adapter = None
        guard = _spend_guard(store, now, cap)
        if due:
            lead = due[0]
            req = check_mod.parse_request({
                "flight": lead["flight"], "date": lead["date"], "origin": lead.get("origin"),
            })
            inner = check_mod.make_adapter(req, fetch=fetch, spend_guard=guard)
            adapter = _LookupCache(inner)
        for members in groups.values():
            already = spent_today(store, now)
            if already >= cap - 1e-9:
                skipped_budget += len(members)
                continue
            for rec in members:
                if spent_today(store, now) >= cap - 1e-9:
                    skipped_budget += 1
                    continue
                result = _run_check(rec, now=now, fetch=fetch, adapter=adapter, spend_guard=guard)
                data = result.to_dict() if hasattr(result, "to_dict") else dict(result)
                err = data.get("error") or {}
                if err.get("code") == "budget_exceeded" and float(data.get("cost_usd_est") or 0) == 0:
                    skipped_budget += 1
                    continue
                names = _apply_result(
                    store, rec, result, now=now, first=False,
                    deliver=deliver, webhook_secret=webhook_secret,
                    charge=False,
                )
                ran += 1
                events += len(names)
        return {
            "ok": True,
            "due": len(due),
            "ran": ran,
            "skipped_budget": skipped_budget,
            "skipped_claim": 0,
            "events": events,
            "spent_usd": spent_today(store, now),
            "budget_usd": cap,
            "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    finally:
        release_tick_claim(store, holder)


def push_sink(store: Storage, item: dict) -> None:
    ts = (item.get("received_at") or dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    stamp = str(ts).replace(":", "").replace("-", "")
    existing = store.list_prefix("sink/")
    seq = len(existing) + 1
    store.put_json(sink_path(stamp, seq), item)
    all_paths = store.list_prefix("sink/")
    extra = all_paths[:-SINK_MAX] if len(all_paths) > SINK_MAX else []
    for path in extra:
        store.delete(path)


def list_sink(store: Storage | None = None) -> list[dict]:
    store = store or get_store()
    out = []
    for path in reversed(store.list_prefix("sink/")):
        item = store.get_json(path)
        if item:
            out.append(item)
        if len(out) >= SINK_MAX:
            break
    return out
