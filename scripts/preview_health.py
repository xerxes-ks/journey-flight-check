#!/usr/bin/env python3
"""Hit GET /api/health on a preview URL.

Uses VERCEL_AUTOMATION_BYPASS_SECRET / x-vercel-protection-bypass when set.
Exit 0 after printing JSON. Exit 2 if the body is check-handler 401 (the
prod routing bug). Exit 1 on other failures. If no URL is given, inspect
vercel.json + pyproject (same assertions as tests/test_vercel_routes.py).
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def assert_route_table() -> None:
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        data = tomllib.load(fh)
    vercel = (data.get("tool") or {}).get("vercel") or {}
    entry = vercel.get("entrypoint")
    if entry != "api.index:handler":
        raise SystemExit(
            f"pyproject [tool.vercel] entrypoint={entry!r}; "
            "must be api.index:handler (not api.check:handler)"
        )
    with open(os.path.join(ROOT, "vercel.json")) as fh:
        cfg = json.load(fh)
    fns = cfg.get("functions") or {}
    if "api/index.py" not in fns:
        raise SystemExit("vercel.json missing function api/index.py")
    if int(fns["api/index.py"].get("maxDuration") or 0) != 30:
        raise SystemExit("api/index.py maxDuration must be 30")


def fetch_health(base: str) -> tuple[int, dict | str]:
    url = base.rstrip("/") + "/api/health"
    headers = {"Accept": "application/json"}
    bypass = os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET") or os.environ.get("VERCEL_PROTECTION_BYPASS") or ""
    if bypass:
        headers["x-vercel-protection-bypass"] = bypass
        headers["x-vercel-set-bypass-cookie"] = "true"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = int(resp.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
        status = int(exc.code)
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = raw[:400]
    return status, body


def main() -> int:
    assert_route_table()
    base = (
        os.environ.get("PREVIEW_URL")
        or os.environ.get("VERCEL_URL")
        or os.environ.get("DEPLOYMENT_URL")
        or (sys.argv[1] if len(sys.argv) > 1 else "")
    )
    if not base:
        print("route table ok; no preview URL (set PREVIEW_URL)")
        return 0
    if not base.startswith("http"):
        base = "https://" + base
    status, body = fetch_health(base)
    print(f"GET {base.rstrip('/')}/api/health -> {status}")
    print(body if isinstance(body, str) else json.dumps(body, indent=2)[:2000])
    if isinstance(body, dict) and body.get("ok") is True and "storage" in body:
        return 0
    if isinstance(body, dict) and (body.get("error") or {}).get("code") == "unauthorized":
        print("routing bug: /api/health was served by the authed check handler", file=sys.stderr)
        return 2
    if status in (401, 403) or (isinstance(body, str) and "Protected by Vercel" in body):
        if os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET"):
            print("bypass secret present but preview still blocked", file=sys.stderr)
            return 1
        print("preview is behind Vercel Authentication; route table already asserted")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
