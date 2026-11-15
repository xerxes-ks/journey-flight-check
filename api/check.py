"""Vercel Python Function: GET/POST /api/check.

Requires Authorization: Bearer $FLIGHTCHECK_TOKEN. Returns JSON. Enforces a
per-call AeroAPI budget cap. Stateless: no cron, no messaging, no trip store.
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
from flightcheck.exceptions import AeroAPIError, BudgetExceeded, CheckError  # noqa: E402
from flightcheck.sanitize import redact  # noqa: E402

HTTP_BUDGET_CAP = 0.50


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
        return 401, {"error": "unauthorized"}
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
    if not data.get("ident") or not data.get("date"):
        return 400, {"error": "ident and date are required"}
    try:
        req = flightcheck_svc.parse_request(data)
    except (ValueError, TypeError, KeyError) as exc:
        return 400, {"error": redact(exc)}
    req.budget_usd = min(float(req.budget_usd), HTTP_BUDGET_CAP)
    try:
        result = flightcheck_svc.check_flight(req)
    except BudgetExceeded as exc:
        return 402, {"error": redact(exc), "budget_usd": req.budget_usd}
    except AeroAPIError as exc:
        return 502, {"error": redact(exc)}
    except CheckError as exc:
        return 502, {"error": redact(exc)}
    except Exception as exc:
        return 500, {"error": redact(exc)}
    return 200, result.to_dict()


class handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Default access log can include the request line; keep it free of headers.
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
            self._send(400, {"error": "invalid JSON"})
            return
        query = _parse_query(self.path)
        status, payload = handle("POST", dict(self.headers), query, body)
        self._send(status, payload)
