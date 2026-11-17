#!/usr/bin/env python3
"""Check the public health contract on an approved Journey flight preview."""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANONICAL_HOST = "journey-flight-check.vercel.app"
PREVIEW_HOST = re.compile(
    r"journey-flight-check-[a-z0-9-]+-karley12345-3615s-projects\.vercel\.app\Z"
)
SHA_PATTERN = re.compile(r"[0-9a-fA-F]{40}\Z")
STORAGE_KINDS = {"blob", "memory", "redis", "none"}
HEALTH_MODES = {"live", "fixtures"}
HEALTH_FLAGS = (
    "aeroapi_key_configured",
    "token_configured",
    "webhook_secret_configured",
    "cron_secret_configured",
)


class HealthCheckError(Exception):
    """A safe-to-print health check failure with no URL or response content."""


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


def _health_url(base: str) -> str:
    candidate = base.strip()
    if not candidate:
        raise HealthCheckError("preview URL is empty")
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        parsed = urllib.parse.urlsplit(candidate)
        port = parsed.port
    except ValueError:
        raise HealthCheckError("preview URL is invalid") from None
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or not (host == CANONICAL_HOST or PREVIEW_HOST.fullmatch(host))
    ):
        raise HealthCheckError("preview URL is not an approved Journey flight deployment URL")
    return f"https://{host}/api/health"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HealthCheckError("health endpoint redirected; probe rejected")


def _validate_expected_sha(expected_sha: str | None) -> str | None:
    if expected_sha is None or expected_sha == "":
        return None
    if not SHA_PATTERN.fullmatch(expected_sha):
        raise HealthCheckError("expected SHA must be 40 hexadecimal characters")
    return expected_sha.lower()


def probe_health(base: str, expected_sha: str | None = None) -> None:
    url = _health_url(base)
    expected = _validate_expected_sha(expected_sha)
    headers = {"Accept": "application/json"}
    bypass = (
        os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET")
        or os.environ.get("VERCEL_PROTECTION_BYPASS")
        or ""
    )
    if bypass:
        headers["x-vercel-protection-bypass"] = bypass
        headers["x-vercel-set-bypass-cookie"] = "true"
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            status = int(response.status)
            raw = response.read(16 * 1024 + 1)
    except HealthCheckError:
        raise
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise HealthCheckError("health endpoint request failed") from None

    if status != 200:
        if status in (401, 403):
            raise HealthCheckError(
                f"health endpoint returned HTTP {status}; Vercel Authentication may be blocking it"
            )
        raise HealthCheckError(f"health endpoint returned HTTP {status}")
    if len(raw) > 16 * 1024:
        raise HealthCheckError("health endpoint response is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HealthCheckError("health endpoint returned non-JSON content") from None
    if not isinstance(payload, dict):
        raise HealthCheckError("health endpoint returned an invalid health contract")
    if payload.get("ok") is not True:
        raise HealthCheckError("health contract did not report ok=true")
    if not isinstance(payload.get("version"), str) or not payload["version"]:
        raise HealthCheckError("health contract has an invalid version")
    if not isinstance(payload.get("sha"), str) or not SHA_PATTERN.fullmatch(payload["sha"]):
        raise HealthCheckError("health contract has an invalid SHA")
    if not isinstance(payload.get("storage"), str) or payload["storage"] not in STORAGE_KINDS:
        raise HealthCheckError("health contract has an invalid storage value")
    if not isinstance(payload.get("mode"), str) or payload["mode"] not in HEALTH_MODES:
        raise HealthCheckError("health contract has an invalid mode")
    if any(type(payload.get(flag)) is not bool for flag in HEALTH_FLAGS):
        raise HealthCheckError("health contract has invalid configuration flags")
    if expected and payload["sha"].lower() != expected:
        raise HealthCheckError("deployed SHA does not match expected SHA")


def main() -> int:
    try:
        assert_route_table()
    except (OSError, ValueError, SystemExit) as exc:
        if isinstance(exc, SystemExit):
            print(str(exc), file=sys.stderr)
            return int(exc.code or 1) if isinstance(exc.code, int) else 1
        print("route table validation failed", file=sys.stderr)
        return 1
    base = (
        os.environ.get("PREVIEW_URL")
        or os.environ.get("VERCEL_URL")
        or os.environ.get("DEPLOYMENT_URL")
        or (sys.argv[1] if len(sys.argv) > 1 else "")
    )
    if not base:
        print("no preview URL; health endpoint was not checked", file=sys.stderr)
        return 1
    expected_sha = os.environ.get("EXPECTED_SHA")
    if os.environ.get("REQUIRE_EXPECTED_SHA") == "true" and not expected_sha:
        print("expected SHA is required for this health check", file=sys.stderr)
        return 1
    try:
        probe_health(base, expected_sha)
    except HealthCheckError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("GET /api/health -> HTTP 200; health contract valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
