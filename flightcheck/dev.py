"""Stdlib HTTP server: /test, /api/*, threaded so webhooks can loop back.

    FLIGHTCHECK_STORAGE=memory FLIGHTCHECK_MODE=fixtures \\
      FLIGHTCHECK_TOKEN=dev FLIGHTCHECK_WEBHOOK_SECRET=dev \\
      python3 -m flightcheck.dev
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flightcheck.dispatch import dispatch  # noqa: E402
from flightcheck.httpsvc import parse_query  # noqa: E402

PUBLIC = ROOT / "public"


class DevHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("dev %s\n" % (fmt % args))

    def _read_raw(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _send_json(self, status, payload):
        blob = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _send_file(self, path: Path, content_type: str):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _static(self) -> bool:
        p = urlparse(self.path).path
        if p in ("/test", "/test/", "/test/index.html"):
            self._send_file(PUBLIC / "test" / "index.html", "text/html; charset=utf-8")
            return True
        return False

    def _run(self, method: str, body, raw: bytes):
        if method == "GET" and self._static():
            return
        if body is None:
            self._send_json(400, {"ok": False, "error": {"code": "bad_request", "message": "invalid JSON", "retryable": False}})
            return
        query = parse_query(self.path)
        extra = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        query.update({k: v[0] if v else "" for k, v in extra.items()})
        status, payload = dispatch(method, dict(self.headers), query, body, self.path, raw)
        self._send_json(status, payload)

    def do_GET(self):
        self._run("GET", {}, b"")

    def do_POST(self):
        raw = self._read_raw()
        try:
            body = json.loads(raw.decode()) if raw else {}
        except json.JSONDecodeError:
            body = None
        self._run("POST", body, raw)

    def do_DELETE(self):
        raw = self._read_raw()
        try:
            body = json.loads(raw.decode()) if raw else {}
        except json.JSONDecodeError:
            body = {}
        self._run("DELETE", body, raw)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, POST, DELETE, OPTIONS")
        self.end_headers()


def serve(host: str = "127.0.0.1", port: int = 8000):
    httpd = ThreadingHTTPServer((host, port), DevHandler)
    return httpd


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT") or (argv[0] if argv else 8000))
    httpd = serve(host, port)
    sys.stderr.write(f"flightcheck dev http://{host}:{port}  (Ctrl-C to stop)\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
