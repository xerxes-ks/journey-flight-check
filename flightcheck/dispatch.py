"""Single request router for the Vercel Python entrypoint and the local server.

Vercel’s current Python runtime requires `[tool.vercel] entrypoint`. Point
that at this dispatcher (`api.index:handler`), never at `api.check:handler`.
"""

from __future__ import annotations

from api.check import handle as handle_check
from .routes import (
    handle_events,
    handle_health,
    handle_simulate,
    handle_tick,
    handle_track,
    handle_tracked,
    handle_untrack,
    handle_webhook_sink,
)

ROUTES = (
    ("GET", "/api/health"),
    ("GET/POST", "/api/check"),
    ("POST", "/api/track"),
    ("DELETE", "/api/track/{id}"),
    ("POST", "/api/untrack"),
    ("GET", "/api/tracked"),
    ("GET", "/api/tracked/{id}/events"),
    ("GET/POST", "/api/tick"),
    ("GET/POST", "/api/simulate"),
    ("GET/POST", "/api/webhook-sink"),
)


def dispatch(method: str, headers: dict, query: dict, body, path: str = "", raw: bytes = b""):
    p = (path or "").split("?")[0].rstrip("/") or "/"
    if p == "/api/health":
        return handle_health(method, headers, query, body, path=path, raw=raw)
    if p == "/api/check":
        return handle_check(method, headers, query, body)
    if p == "/api/track" or p.startswith("/api/track/"):
        return handle_track(method, headers, query, body, path=path, raw=raw)
    if p == "/api/untrack" or p.startswith("/api/untrack/"):
        return handle_untrack(method, headers, query, body, path=path, raw=raw)
    if p.endswith("/events") and p.startswith("/api/tracked/"):
        return handle_events(method, headers, query, body, path=path, raw=raw)
    if p == "/api/events":
        return handle_events(method, headers, query, body, path=path, raw=raw)
    if p == "/api/tracked" or p.startswith("/api/tracked/"):
        return handle_tracked(method, headers, query, body, path=path, raw=raw)
    if p == "/api/tick":
        return handle_tick(method, headers, query, body, path=path, raw=raw)
    if p == "/api/simulate":
        return handle_simulate(method, headers, query, body, path=path, raw=raw)
    if p == "/api/webhook-sink":
        return handle_webhook_sink(method, headers, query, body, path=path, raw=raw)
    return 404, {"ok": False, "error": {"code": "not_found", "message": p, "retryable": False}}
