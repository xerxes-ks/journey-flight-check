"""Material-change events and signed webhook delivery."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from .exceptions import WebhookNotAllowed
from .model import mins, parse_time

EVENT_NAMES = (
    "tracking_started",
    "delay_likely",
    "delay_cleared",
    "retimed",
    "gate_changed",
    "cancelled_unconfirmed",
    "cancelled",
    "diverted",
    "departed",
    "landed",
    "connection_at_risk",
    "tracking_ended",
    "check_failed",
)

WATCHFUL = {"watch", "at_risk"}
LANDED_PHASES = {"landed", "at_arrival_gate"}
BACKOFF_S = (0.2, 0.6, 1.2)
DELIVER_TIMEOUT_S = 3


def sign_body(body: bytes | str, secret: str) -> str:
    if isinstance(body, str):
        body = body.encode()
    digest = hmac.new((secret or "").encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(body: bytes | str, header: str | None, secret: str) -> bool:
    if not header or not secret:
        return False
    expected = sign_body(body, secret)
    return hmac.compare_digest(header.strip(), expected)


def _gate(snap: dict) -> str:
    flight = (snap or {}).get("flight") or {}
    return str(flight.get("gate_origin") or "")


def _est_out(snap: dict):
    flight = (snap or {}).get("flight") or {}
    est = flight.get("estimated_out") or {}
    return est.get("local") if isinstance(est, dict) else None


def _sched_out(snap: dict):
    flight = (snap or {}).get("flight") or {}
    sch = flight.get("scheduled_out") or {}
    return parse_time(sch.get("local") if isinstance(sch, dict) else None)


def _est_out_dt(snap: dict):
    flight = (snap or {}).get("flight") or {}
    est = flight.get("estimated_out") or {}
    return parse_time(est.get("local") if isinstance(est, dict) else None)


def detect_events(prev: Optional[dict], result: dict, *, first: bool = False, fail_count: int = 0) -> list[str]:
    """Return event names for a new check versus the previous snapshot."""
    events: list[str] = []
    if first:
        events.append("tracking_started")
    prev = prev or {}
    err = result.get("error") or {}
    retryable = bool(err.get("retryable"))
    if retryable:
        if fail_count >= 3:
            events.append("check_failed")
        return events

    cancelled = bool((result.get("flight") or {}).get("cancelled") or result.get("status") == "cancelled")
    diverted = bool((result.get("flight") or {}).get("diverted") or result.get("status") == "diverted")
    prev_cancelled = bool((prev.get("flight") or {}).get("cancelled") or prev.get("status") == "cancelled")
    prev_diverted = bool((prev.get("flight") or {}).get("diverted") or prev.get("status") == "diverted")
    final = bool(result.get("final"))
    prev_final = bool(prev.get("final"))
    band = result.get("band") or "unknown"
    prev_band = prev.get("band") or "unknown"
    phase = result.get("phase") or "scheduled"
    prev_phase = prev.get("phase") or "scheduled"
    impact = result.get("impact_band")
    prev_impact = prev.get("impact_band")

    if cancelled and final and not (prev_cancelled and prev_final):
        events.append("cancelled")
    elif cancelled and not final and not prev_cancelled:
        events.append("cancelled_unconfirmed")

    if diverted and not prev_diverted:
        events.append("diverted")

    if phase == "airborne" and prev_phase != "airborne":
        events.append("departed")
    if phase in LANDED_PHASES and prev_phase not in LANDED_PHASES:
        events.append("landed")

    if band in WATCHFUL and prev_band not in WATCHFUL and band != "unknown":
        events.append("delay_likely")
    if band == "on_track" and prev_band in WATCHFUL:
        events.append("delay_cleared")

    if _gate(result) and _gate(prev) and _gate(result) != _gate(prev):
        events.append("gate_changed")

    est, sched = _est_out_dt(result), _sched_out(result)
    prev_est = _est_out_dt(prev)
    if est and sched and abs(mins(est - sched)) >= 30:
        if not prev_est or _est_out(result) != _est_out(prev):
            events.append("retimed")

    if impact == "at_risk" and prev_impact != "at_risk":
        events.append("connection_at_risk")

    if final and not prev_final:
        events.append("tracking_ended")
    return events


def times_block(result: dict) -> dict:
    flight = result.get("flight") or {}
    pred = result.get("prediction") or {}
    keys = (
        "scheduled_out", "estimated_out", "actual_out", "actual_off",
        "scheduled_in", "estimated_in", "actual_in", "actual_on",
    )
    out = {}
    for k in keys:
        raw = flight.get(k)
        if isinstance(raw, dict):
            out[k] = {"human": raw.get("human"), "local": raw.get("local"), "tz": raw.get("tz")}
    for k, src in (("expected_out", pred.get("expected_out")), ("expected_in", pred.get("expected_in"))):
        if isinstance(src, dict):
            out[k] = {"human": src.get("human"), "local": src.get("local"), "tz": src.get("tz")}
    nxt = result.get("next_check_local")
    if isinstance(nxt, dict):
        out["next_check"] = {"human": nxt.get("human"), "local": nxt.get("local"), "tz": nxt.get("tz")}
    return out


def event_payload(event: str, track: dict, result: dict) -> dict:
    flight = result.get("flight") or {}
    return {
        "event": event,
        "tracking_id": track.get("tracking_id"),
        "ref": track.get("ref") or "",
        "flight": track.get("flight") or flight.get("ident_iata") or flight.get("ident"),
        "checkpoint": result.get("checkpoint") or track.get("checkpoint"),
        "phase": result.get("phase"),
        "band": result.get("band"),
        "impact_band": result.get("impact_band"),
        "summary": result.get("summary") or "",
        "times": times_block(result),
        "checked_at": result.get("checked_at"),
        "snapshot": result,
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _host_from_url(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    return text.split("://")[-1].split("/")[0].split(":")[0].lower()


def webhook_allowed_hosts() -> set[str]:
    raw = os.environ.get("FLIGHTCHECK_WEBHOOK_ALLOWED_HOSTS") or ""
    hosts = {h.strip().lower() for h in raw.split(",") if h.strip()}
    for key in ("VERCEL_URL", "VERCEL_BRANCH_URL", "VERCEL_PROJECT_PRODUCTION_URL"):
        host = _host_from_url(os.environ.get(key) or "")
        if host:
            hosts.add(host)
    return hosts


def track_webhook_allowed_hosts() -> set[str]:
    """Hosts accepted at POST /api/track: allowlist plus the production host."""
    raw = os.environ.get("FLIGHTCHECK_WEBHOOK_ALLOWED_HOSTS") or ""
    hosts = {h.strip().lower() for h in raw.split(",") if h.strip()}
    prod = _host_from_url(os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or "")
    if prod:
        hosts.add(prod)
    return hosts


def require_webhook_url(url: str) -> None:
    """Reject a webhook_url that could not be delivered. Raises WebhookNotAllowed."""
    parsed = urllib.parse.urlparse(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    scheme = parsed.scheme.lower()
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    hosted = bool(os.environ.get("VERCEL_ENV"))
    if scheme != "https" and not (scheme == "http" and loopback and not hosted):
        raise WebhookNotAllowed("webhook_url must be HTTPS")
    allowed = track_webhook_allowed_hosts()
    if loopback and not hosted:
        allowed.add(host)
    if not host or host not in allowed:
        raise WebhookNotAllowed("webhook_url host is not allowed")


def _opener():
    return urllib.request.build_opener(_NoRedirect)


def post_json(url: str, body: bytes, headers: dict, timeout: int = DELIVER_TIMEOUT_S) -> int:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with _opener().open(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def deliver_webhook(
    url: str,
    payload: dict,
    secret: str,
    *,
    post: Callable = post_json,
    sleep: Callable = time.sleep,
    attempts: int = 1,
) -> dict:
    if not (secret or "").strip():
        return {"ok": False, "http": 0, "attempts": 0, "skipped": "missing_secret"}
    enforce = post is post_json
    if enforce:
        parsed = urllib.parse.urlparse(str(url or ""))
        host = (parsed.hostname or "").lower()
        scheme = parsed.scheme.lower()
        loopback = host in {"127.0.0.1", "localhost", "::1"}
        hosted = bool(os.environ.get("VERCEL_ENV"))
        if scheme != "https" and not (scheme == "http" and loopback and not hosted):
            return {"ok": False, "http": 0, "attempts": 0, "skipped": "https_required"}
        allowed = webhook_allowed_hosts()
        if loopback and not hosted:
            allowed.add(host)
        if not host or host not in allowed:
            return {"ok": False, "http": 0, "attempts": 0, "skipped": "host_not_allowed"}
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Flightcheck-Signature": sign_body(body, secret),
        "User-Agent": "flightcheck-webhook/1",
    }
    last_status = 0
    ok = False
    i = 0
    for i in range(max(1, attempts)):
        try:
            last_status = post(url, body, headers, timeout=DELIVER_TIMEOUT_S) if post is post_json else post(url, body, headers)
            if 200 <= int(last_status) < 300:
                ok = True
                break
        except Exception:
            last_status = 0
        if i + 1 < attempts:
            sleep(BACKOFF_S[min(i, len(BACKOFF_S) - 1)])
    return {"ok": ok, "http": last_status, "attempts": i + 1}
