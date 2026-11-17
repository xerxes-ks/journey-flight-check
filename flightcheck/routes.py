"""HTTP handlers for tracking, tick, simulate, webhook-sink, health."""

from __future__ import annotations

import datetime as dt
import json
import os
import re

from .events import verify_signature
from .exceptions import UNAUTH_MESSAGE, error_dict, http_status_for
from .health import health_payload
from .httpsvc import authorized, cron_authorized, wrap_errors
from .mode import fixture_fetch, is_fixtures, resolve_now
from .simulate import SCENARIOS, run_simulate
from .store import get_store
from .track import (
    list_event_log,
    list_sink,
    list_tracks,
    load_track,
    public_track,
    push_sink,
    request_namespace,
    start_track,
    tick,
    untrack,
)

TRACK_ID = re.compile(r"^/api/track/([^/]+)/?$")
EVENTS_ID = re.compile(r"^/api/tracked/([^/]+)/events/?$")
UNTRACK_POST = re.compile(r"^/api/untrack/([^/]+)/?$")
TRACKED_ID = re.compile(r"^/api/tracked/([^/]+)/?$")


def _tid(*candidates) -> str:
    for c in candidates:
        if c:
            return str(c).strip()
    return ""


def _store_for(headers):
    from .store import namespaced_store
    return namespaced_store(get_store(), request_namespace(headers))


def _untrack_status(tid: str, headers=None):
    ns = request_namespace(headers)
    out = untrack(tid, store=_store_for(headers), namespace=ns)
    if not out.get("ok"):
        err = out.get("error") or {}
        code = err.get("code") or "not_found"
        return http_status_for(code, default=404), out
    return 200, out


def handle_health(method, headers, query, body, path="", raw=b""):
    if method != "GET":
        return 405, {"ok": False, "error": error_dict("method_not_allowed", "GET only")}
    return 200, health_payload()


@wrap_errors
def handle_track(method, headers, query, body, path="", raw=b""):
    if not authorized(headers):
        return 401, {"ok": False, "error": error_dict("unauthorized", UNAUTH_MESSAGE)}
    path_only = (path or "").split("?")[0]
    m = TRACK_ID.match(path_only)
    tid = _tid(m.group(1) if m else "", query.get("id"), (body or {}).get("tracking_id"))
    if method == "POST" and path_only.rstrip("/") in ("/api/track",):
        now = resolve_now(headers, body, query)
        ns = request_namespace(headers)
        track_kw = {"store": _store_for(headers), "namespace": ns}
        if is_fixtures(headers):
            track_kw["fetch"] = fixture_fetch(now=now)
        if now is not None:
            track_kw["now"] = now
        out = start_track(body or {}, **track_kw)
        return 200, out
    if method == "DELETE" and tid:
        return _untrack_status(tid, headers)
    return 405, {"ok": False, "error": error_dict("method_not_allowed", "POST /api/track or DELETE /api/track/{id}")}


@wrap_errors
def handle_untrack(method, headers, query, body, path="", raw=b""):
    if not authorized(headers):
        return 401, {"ok": False, "error": error_dict("unauthorized", UNAUTH_MESSAGE)}
    m = UNTRACK_POST.match((path or "").split("?")[0])
    tid = _tid(m.group(1) if m else "", query.get("id"), (body or {}).get("tracking_id"))
    if method not in ("POST", "DELETE") or not tid:
        return 400, {"ok": False, "error": error_dict("bad_request", "tracking_id required")}
    return _untrack_status(tid, headers)


@wrap_errors
def handle_tracked(method, headers, query, body, path="", raw=b""):
    if not authorized(headers):
        return 401, {"ok": False, "error": error_dict("unauthorized", UNAUTH_MESSAGE)}
    if method != "GET":
        return 405, {"ok": False, "error": error_dict("method_not_allowed", "GET only")}
    path_only = (path or "").split("?")[0]
    m = EVENTS_ID.match(path_only)
    mid = TRACKED_ID.match(path_only)
    tid = _tid(m.group(1) if m else "", query.get("id"), mid.group(1) if mid else "")
    store = _store_for(headers)
    ns = request_namespace(headers)
    if m or (tid and "events" in path_only):
        rec = load_track(store, tid)
        if not rec:
            return 404, {"ok": False, "error": error_dict("not_found", "tracking_id not found")}
        return 200, {
            "ok": True,
            "tracking_id": rec["tracking_id"],
            "events": list_event_log(store, rec["tracking_id"]),
        }
    if tid:
        rec = load_track(store, tid)
        if not rec:
            return 404, {"ok": False, "error": error_dict("not_found", "tracking_id not found")}
        return 200, {"ok": True, "track": public_track(rec)}
    items = list_tracks(store=store, namespace=ns)
    return 200, {"ok": True, "count": len(items), "tracks": items}


@wrap_errors
def handle_events(method, headers, query, body, path="", raw=b""):
    if not authorized(headers):
        return 401, {"ok": False, "error": error_dict("unauthorized", UNAUTH_MESSAGE)}
    if method != "GET":
        return 405, {"ok": False, "error": error_dict("method_not_allowed", "GET only")}
    path_only = (path or "").split("?")[0]
    m = EVENTS_ID.match(path_only)
    tid = _tid(m.group(1) if m else "", query.get("id"))
    if not tid:
        return 400, {"ok": False, "error": error_dict("bad_request", "tracking_id required")}
    store = _store_for(headers)
    rec = load_track(store, tid)
    if not rec:
        return 404, {"ok": False, "error": error_dict("not_found", "tracking_id not found")}
    return 200, {
        "ok": True,
        "tracking_id": rec["tracking_id"],
        "events": list_event_log(store, rec["tracking_id"]),
    }


@wrap_errors
def handle_tick(method, headers, query, body, path="", raw=b""):
    if not (cron_authorized(headers) or authorized(headers)):
        return 401, {"ok": False, "error": error_dict("unauthorized", UNAUTH_MESSAGE)}
    if method not in ("GET", "POST"):
        return 405, {"ok": False, "error": error_dict("method_not_allowed", "GET or POST")}
    now = resolve_now(headers, body or {}, query)
    ns = request_namespace(headers)
    tick_kw = {"store": _store_for(headers), "namespace": ns}
    if is_fixtures(headers):
        tick_kw["fetch"] = fixture_fetch(now=now)
    if now is not None:
        tick_kw["now"] = now
    return 200, tick(**tick_kw)


@wrap_errors
def handle_simulate(method, headers, query, body, path="", raw=b""):
    if not authorized(headers):
        return 401, {"ok": False, "error": error_dict("unauthorized", UNAUTH_MESSAGE)}
    if method == "GET":
        return 200, {"ok": True, "scenarios": list(SCENARIOS)}
    if method != "POST":
        return 405, {"ok": False, "error": error_dict("method_not_allowed", "GET or POST")}
    return 200, run_simulate(body or {})


@wrap_errors
def handle_webhook_sink(method, headers, query, body, path="", raw=b""):
    if method == "GET":
        if not authorized(headers):
            return 401, {"ok": False, "error": error_dict("unauthorized", UNAUTH_MESSAGE)}
        items = list_sink(get_store())
        return 200, {"ok": True, "count": len(items), "deliveries": items}
    if method != "POST":
        return 405, {"ok": False, "error": error_dict("method_not_allowed", "GET or POST")}
    store = get_store()
    secret = os.environ.get("FLIGHTCHECK_WEBHOOK_SECRET") or ""
    sig = headers.get("X-Flightcheck-Signature") or headers.get("x-flightcheck-signature") or ""
    raw_bytes = raw if raw else b""
    if isinstance(raw_bytes, str):
        raw_bytes = raw_bytes.encode()
    if not raw_bytes and body is not None:
        raw_bytes = json.dumps(body, separators=(",", ":")).encode()
    ok = verify_signature(raw_bytes, sig, secret) if secret else True
    push_sink(store, {
        "received_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "signature_ok": ok,
        "event": (body or {}).get("event"),
        "tracking_id": (body or {}).get("tracking_id"),
        "payload": body,
    })
    return 200, {"ok": True, "signature_ok": ok}
