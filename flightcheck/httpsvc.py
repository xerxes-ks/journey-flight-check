"""Shared HTTP helpers for Vercel Python functions."""

from __future__ import annotations

import json
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from .exceptions import CheckError, error_dict, http_status_for
from .sanitize import redact
from .store import StorageNotConfigured


def bearer_ok(header: str | None, token: str) -> bool:
    if not token:
        return False
    if not header or not header.startswith("Bearer "):
        return False
    return secrets.compare_digest(header[7:], token)


def authorized(headers: dict) -> bool:
    token = os.environ.get("FLIGHTCHECK_TOKEN") or ""
    header = headers.get("Authorization") or headers.get("authorization")
    return bearer_ok(header, token)


def cron_authorized(headers: dict) -> bool:
    secret = os.environ.get("CRON_SECRET") or ""
    header = headers.get("Authorization") or headers.get("authorization")
    return bearer_ok(header, secret)


def parse_query(path: str) -> dict:
    raw = parse_qs(urlparse(path).query, keep_blank_values=True)
    return {k: v[0] if v else "" for k, v in raw.items()}


def json_error(code: str, message: str, http: int = 400) -> tuple[int, dict]:
    return http, {"ok": False, "error": error_dict(code, message)}


def wrap_errors(fn):
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except StorageNotConfigured as exc:
            return 503, {"ok": False, "error": exc.to_dict()}
        except CheckError as exc:
            status = http_status_for(exc.code, default=200, http_status=exc.http_status)
            payload = {"ok": False, "error": exc.to_dict()}
            payload["error"]["message"] = redact(exc)
            return status, payload
        except ValueError as exc:
            return 400, {"ok": False, "error": error_dict("bad_request", redact(exc))}
        except Exception as exc:
            return 200, {"ok": False, "error": error_dict("error", redact(exc))}
    return inner


def make_handler(handle_fn, name: str = "api"):
    class handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (name, fmt % args))

        def _read_raw(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return b""
            return self.rfile.read(length)

        def _parse_json(self, raw: bytes):
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
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def _run(self, method, body, raw=b""):
            if body is None:
                self._send(400, {"ok": False, "error": error_dict("bad_request", "invalid JSON")})
                return
            query = parse_query(self.path)
            status, payload = handle_fn(
                method, dict(self.headers), query, body, path=self.path, raw=raw,
            )
            self._send(status, payload)

        def do_GET(self):
            self._run("GET", {}, b"")

        def do_POST(self):
            raw = self._read_raw()
            self._run("POST", self._parse_json(raw), raw)

        def do_DELETE(self):
            raw = self._read_raw()
            self._run("DELETE", self._parse_json(raw) or {}, raw)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Allow", "GET, POST, DELETE, OPTIONS")
            self.end_headers()

    handler.__name__ = "handler"
    return handler
