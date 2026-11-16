"""Vercel Python Function: GET/POST /api/check.

Requires Authorization: Bearer $FLIGHTCHECK_TOKEN. Returns structured JSON.
Enforces a per-call AeroAPI budget cap. Stateless.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from flightcheck import check as flightcheck_svc  # noqa: E402
from flightcheck.exceptions import BudgetExceeded, CheckError, error_dict  # noqa: E402
from flightcheck.sanitize import redact  # noqa: E402

HTTP_BUDGET_CAP = 0.50

# Application error codes → HTTP. Upstream 5xx stay 200 with a structured
# error object so a tool-calling client always gets JSON, never a bare 502.
ERROR_HTTP = {
    "budget_exceeded": 402,
    "not_found": 404,
    "bad_request": 400,
    "rate_limited": 429,
}


def _authorized(header: str | None) -> bool:
    token = os.environ.get("FLIGHTCHECK_TOKEN") or ""
    if not token:
        return False
    if not header or not header.startswith("Bearer "):
        return False
    return secrets.compare_digest(header[7:], token)


def _parse_query(path: str) -> dict:
    raw = parse_qs(urlparse(path).query, keep_blank_values=True)
    return {k: v[0] if v else "" for k, v in raw.items()}


def handle(method: str, headers: dict, query: dict, body: dict | None):
    """Pure handler used by the Vercel class and by tests. Returns (status, payload)."""
    auth = headers.get("Authorization") or headers.get("authorization")
    if not _authorized(auth):
        return 401, {"ok": False, "error": error_dict("unauthorized", "unauthorized")}
    data = dict(query or {})
    if method == "POST":
        if body:
            nested = body.get("onward") if isinstance(body.get("onward"), dict) else {}
            data.update({k: v for k, v in body.items() if k != "onward"})
            if nested:
                existing = data.get("onward") if isinstance(data.get("onward"), dict) else {}
                data["onward"] = {**existing, **nested}
            elif body.get("onward"):
                data["onward"] = body["onward"]
    if not (data.get("flight") or data.get("ident")) or not data.get("date"):
        return 400, {"ok": False, "error": error_dict("bad_request", "flight and date are required")}
    try:
        req = flightcheck_svc.parse_request(data)
    except (ValueError, TypeError, KeyError) as exc:
        return 400, {"ok": False, "error": error_dict("bad_request", redact(exc))}
    req.budget_usd = min(float(req.budget_usd), HTTP_BUDGET_CAP)
    try:
        result = flightcheck_svc.check_flight(req)
    except BudgetExceeded as exc:
        err = error_dict("budget_exceeded", redact(exc))
        err["budget_usd"] = req.budget_usd
        return 402, {"ok": False, "error": err}
    except CheckError as exc:
        payload = {"ok": False, "error": exc.to_dict()}
        payload["error"]["message"] = redact(exc)
        return ERROR_HTTP.get(exc.code, 200), payload
    except Exception as exc:
        return 200, {"ok": False, "error": error_dict("upstream_error", redact(exc))}
    payload = result.to_dict()
    http = 200
    if result.error and result.error.get("code") in ERROR_HTTP:
        http = ERROR_HTTP[result.error["code"]]
    return http, payload


class handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("api/check %s\n" % (fmt % args))

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode())
        except json.JSONDecodeError:
            return None

    def _send(self, status, payload):
        blob = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        query = _parse_query(self.path)
        status, payload = handle("GET", dict(self.headers), query, None)
        self._send(status, payload)

    def do_POST(self):
        body = self._read_json()
        if body is None:
            self._send(400, {"ok": False, "error": error_dict("bad_request", "invalid JSON")})
            return
        query = _parse_query(self.path)
        status, payload = handle("POST", dict(self.headers), query, body)
        self._send(status, payload)
