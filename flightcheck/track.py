"""Flight tracking: persist checkpoints, tick due work, fan out events."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import uuid
from typing import Optional

from . import check as check_mod
from .events import deliver_webhook, detect_events, event_payload
from .exceptions import error_dict
from .model import UTC, normalize_ident, parse_date, parse_time
from .store import EVENTS_MAX, SINK_MAX, Storage, get_store

DEFAULT_DAILY_BUDGET = 0.50


def _idemp_hash(flight: str, date: str, origin: str | None, webhook_url: str) -> str:
    raw = "|".join([
        normalize_ident(flight),
        str(date)[:10],
        (origin or "").upper(),
        (webhook_url or "").strip(),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _group_key(flight: str, date: str, origin: str | None) -> str:
    return hashlib.sha256(
        f"{normalize_ident(flight)}|{str(date)[:10]}|{(origin or '').upper()}".encode()
    ).hexdigest()[:24]


def track_path(tid: str) -> str:
    return f"tracks/{tid}.json"


def event_path(tid: str, ts: str, seq: int) -> str:
    return f"events/{tid}/{ts}-{seq}.json"


def sink_path(ts: str, seq: int = 0) -> str:
    return f"sink/{ts}-{seq}.json"


def budget_path(now: dt.datetime) -> str:
    day = now.astimezone(UTC).strftime("%Y-%m-%d")
    return f"budget/{day}.json"


def new_tracking_id() -> str:
    return "trk_" + uuid.uuid4().hex[:20]


def daily_budget() -> float:
    raw = os.environ.get("FLIGHTCHECK_DAILY_BUDGET_USD")
    if raw in (None, ""):
        return DEFAULT_DAILY_BUDGET
    return float(raw)


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
    }


def _find_idemp(store: Storage, flight, date, origin, webhook) -> Optional[dict]:
    want = _idemp_hash(flight, date, origin, webhook)
    for path in store.list_prefix("tracks/"):
        rec = store.get_json(path)
        if rec and rec.get("idemp") == want:
            return rec
    return None


def start_track(
    data: dict,
    *,
    store: Storage | None = None,
    now: dt.datetime | None = None,
    fetch=None,
    deliver=None,
    webhook_secret: str | None = None,
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
    store = store or get_store()
    origin = (str(data["origin"]).upper() if data.get("origin") else None)
    existing = _find_idemp(store, flight, date, origin, webhook)
    if existing:
        return {
            "ok": True,
            "idempotent": True,
            "tracking_id": existing["tracking_id"],
            "next_check_local": existing.get("next_check_local"),
            "checkpoint": existing.get("checkpoint"),
        }
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
        "version": 0,
    }
    result = _run_check(rec, now=now, fetch=fetch)
    _apply_result(
        store, rec, result, now=now, first=True,
        deliver=deliver, webhook_secret=webhook_secret,
    )
    return {
        "ok": True,
        "idempotent": False,
        "tracking_id": tid,
        "next_check_local": rec.get("next_check_local"),
        "checkpoint": rec.get("checkpoint"),
    }


def untrack(tid: str, *, store: Storage | None = None) -> dict:
    store = store or get_store()
    rec = load_track(store, tid)
    if not rec:
        return {"ok": False, "error": error_dict("not_found", "tracking_id not found")}
    store.delete(track_path(tid))
    return {"ok": True, "tracking_id": tid}


def list_tracks(*, store: Storage | None = None) -> list[dict]:
    store = store or get_store()
    out = []
    for path in store.list_prefix("tracks/"):
        rec = store.get_json(path)
        if rec:
            out.append(public_track(rec))
    out.sort(key=lambda r: (r.get("date") or "", r.get("flight") or ""))
    return out


def _run_check(rec: dict, *, now: dt.datetime, fetch=None, adapter=None):
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
    return check_mod.check_flight(payload, fetch=fetch, adapter=adapter, now=now)


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


def _apply_result(
    store: Storage,
    rec: dict,
    result,
    *,
    now: dt.datetime,
    first: bool = False,
    deliver=None,
    webhook_secret: str | None = None,
    charge: bool = True,
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
    rec["checkpoint"] = data.get("checkpoint") or rec.get("checkpoint")
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
        payload = event_payload(name, rec, data)
        append_event(store, rec["tracking_id"], payload)
        dest = rec.get("webhook_url")
        if dest:
            sender = deliver or deliver_webhook
            sender(dest, payload, secret)
        sent.append(name)
    return sent


class _LookupCache:
    """One AeroAPI lookup per ident/date/origin, shared across subscribers."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = getattr(inner, "calls", [])
        self.key = getattr(inner, "key", None)
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
) -> dict:
    store = store or get_store()
    now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    cap = daily_budget() if budget is None else float(budget)
    due: list[dict] = []
    for path in store.list_prefix("tracks/"):
        rec = store.get_json(path)
        if not rec or rec.get("ended"):
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
    events = 0
    adapter = None
    if due:
        lead = due[0]
        req = check_mod.parse_request({
            "flight": lead["flight"], "date": lead["date"], "origin": lead.get("origin"),
        })
        inner = check_mod.make_adapter(req, fetch=fetch)
        adapter = _LookupCache(inner)
    for members in groups.values():
        already = spent_today(store, now)
        if already >= cap - 1e-9:
            skipped_budget += len(members)
            continue
        before_spent = float(getattr(adapter, "spent", 0.0) or 0)
        for rec in members:
            if spent_today(store, now) >= cap - 1e-9:
                skipped_budget += 1
                continue
            result = _run_check(rec, now=now, fetch=fetch, adapter=adapter)
            names = _apply_result(
                store, rec, result, now=now, first=False,
                deliver=deliver, webhook_secret=webhook_secret,
                charge=False,
            )
            ran += 1
            events += len(names)
        delta = float(getattr(adapter, "spent", 0.0) or 0) - before_spent
        if delta > 0:
            add_spend(store, now, delta)
    return {
        "ok": True,
        "due": len(due),
        "ran": ran,
        "skipped_budget": skipped_budget,
        "events": events,
        "spent_usd": spent_today(store, now),
        "budget_usd": cap,
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


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
