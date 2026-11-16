"""HTTP route handlers used by api/*.py Vercel functions."""

from __future__ import annotations

import datetime as dt
import os

from .events import verify_signature
from .exceptions import error_dict
from .httpsvc import authorized, cron_authorized, wrap_errors
from .model import UTC
from .simulate import SCENARIOS, run_simulate
from .store import get_store
from .track import (
    list_event_log,
    list_sink,
    list_tracks,
    load_track,
    public_track,
    push_sink,
    start_track,
    tick,
    untrack,
)


def _need_auth(headers):
    if authorized(headers):
        return None
    return 401, {"ok": False, "error": error_dict("unauthorized", "unauthorized")}


@wrap_errors
def handle_track(method, headers, query, body, path=None):
    denied = _need_auth(headers)
    if denied:
        return denied
    if method == "DELETE":
        tid = (body or {}).get("tracking_id") or (body or {}).get("id") or (query or {}).get("id")
        if not tid and path:
            parts = [p for p in str(path).split("?")[0].split("/") if p]
            if "track" in parts:
                i = parts.index("track")
                if i + 1 < len(parts):
                    tid = parts[i + 1]
        if not tid:
            return 400, {"ok": False, "error": error_dict("bad_request", "tracking_id is required")}
        out = untrack(tid)
        return (404 if not out.get("ok") else 200), out
    if method != "POST":
        return 405, {"ok": False, "error": error_dict("bad_request", "POST or DELETE")}
    data = dict(query or {})
    if body:
        data.update(body)
    out = start_track(data)
    return 200, out


@wrap_errors
def handle_untrack(method, headers, query, body, path=None):
    denied = _need_auth(headers)
    if denied:
        return denied
    data = dict(body or {})
    tid = data.get("tracking_id") or data.get("id") or (query or {}).get("id")
    if not tid:
        return 400, {"ok": False, "error": error_dict("bad_request", "tracking_id is required")}
    out = untrack(tid)
    return (404 if not out.get("ok") else 200), out


@wrap_errors
def handle_tracked(method, headers, query, body, path=None):
    denied = _need_auth(headers)
    if denied:
        return denied
    tid = (query or {}).get("id")
    if not tid and path:
        parts = [p for p in str(path).split("?")[0].split("/") if p]
        if "tracked" in parts:
            i = parts.index("tracked")
            if i + 1 < len(parts) and parts[i + 1] != "events":
                tid = parts[i + 1]
    if tid:
        rec = load_track(get_store(), tid)
        if not rec:
            return 404, {"ok": False, "error": error_dict("not_found", "tracking_id not found")}
        return 200, {"ok": True, "track": public_track(rec)}
    return 200, {"ok": True, "tracks": list_tracks()}


@wrap_errors
def handle_events(method, headers, query, body, path=None):
    denied = _need_auth(headers)
    if denied:
        return denied
    tid = (query or {}).get("id")
    if not tid and path:
        parts = [p for p in str(path).split("?")[0].split("/") if p]
        if "tracked" in parts:
            i = parts.index("tracked")
            if i + 1 < len(parts):
                tid = parts[i + 1]
    if not tid:
        return 400, {"ok": False, "error": error_dict("bad_request", "tracking_id is required")}
    rec = load_track(get_store(), tid)
    if not rec:
        return 404, {"ok": False, "error": error_dict("not_found", "tracking_id not found")}
    return 200, {"ok": True, "tracking_id": tid, "events": list_event_log(get_store(), tid)}


@wrap_errors
def handle_tick(method, headers, query, body, path=None):
    if not cron_authorized(headers) and not authorized(headers):
        return 401, {"ok": False, "error": error_dict("unauthorized", "unauthorized")}
    now = None
    if (body or {}).get("now"):
        from .times import parse_now
        now = parse_now(body["now"])
    return 200, tick(now=now)


@wrap_errors
def handle_simulate(method, headers, query, body, path=None):
    denied = _need_auth(headers)
    if denied:
        return denied
    if method != "POST":
        return 200, {"ok": True, "scenarios": list(SCENARIOS)}
    return 200, run_simulate(body or {})


@wrap_errors
def handle_webhook_sink(method, headers, query, body, path=None):
    store = get_store()
    if method == "GET":
        denied = _need_auth(headers)
        if denied:
            return denied
        return 200, {"ok": True, "deliveries": list_sink(store)}
    raw = body if isinstance(body, (bytes, str)) else None
    import json
    if isinstance(body, dict):
        payload = body
        raw_bytes = json.dumps(body, separators=(",", ":")).encode()
    else:
        payload = {}
        raw_bytes = b""
    secret = os.environ.get("FLIGHTCHECK_WEBHOOK_SECRET") or ""
    sig = headers.get("X-Flightcheck-Signature") or headers.get("x-flightcheck-signature")
    ok_sig = verify_signature(raw_bytes, sig, secret) if secret else True
    item = {
        "ok_sig": ok_sig,
        "received_at": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": (payload or {}).get("event"),
        "tracking_id": (payload or {}).get("tracking_id"),
        "payload": payload,
    }
    if not ok_sig:
        return 401, {"ok": False, "error": error_dict("unauthorized", "bad webhook signature")}
    push_sink(store, item)
    return 200, {"ok": True}
