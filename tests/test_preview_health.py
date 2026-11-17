"""Network-mocked checks for the preview health probe."""

import json
import urllib.error

import pytest

from scripts import preview_health

EXPECTED_SHA = "a" * 40


def payload(**updates):
    result = {
        "ok": True,
        "version": "1.0.0",
        "sha": EXPECTED_SHA,
        "storage": "memory",
        "aeroapi_key_configured": False,
        "token_configured": True,
        "webhook_secret_configured": True,
        "cron_secret_configured": True,
        "mode": "live",
    }
    result.update(updates)
    return result


class Response:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def use_response(monkeypatch, status=200, body=b"{}"):
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            if status >= 400:
                raise urllib.error.HTTPError(request.full_url, status, "error", {}, None)
            return Response(status, body)

    monkeypatch.setattr(preview_health.urllib.request, "build_opener", lambda *_: Opener())
    return requests


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_block_is_failure(monkeypatch, status):
    use_response(monkeypatch, status=status)
    with pytest.raises(preview_health.HealthCheckError, match="Vercel Authentication"):
        preview_health.probe_health("https://journey-flight-check.vercel.app")


def test_html_is_failure_without_printing_body(monkeypatch):
    use_response(monkeypatch, body=b"<html>Protected by Vercel</html>")
    with pytest.raises(preview_health.HealthCheckError, match="non-JSON") as error:
        preview_health.probe_health("journey-flight-check.vercel.app")
    assert "Protected by Vercel" not in str(error.value)


def test_non_200_ok_payload_is_failure(monkeypatch):
    use_response(monkeypatch, status=503, body=json.dumps(payload()).encode())
    with pytest.raises(preview_health.HealthCheckError, match="HTTP 503"):
        preview_health.probe_health("journey-flight-check.vercel.app")


def test_stale_sha_is_failure(monkeypatch):
    use_response(monkeypatch, body=json.dumps(payload()).encode())
    with pytest.raises(preview_health.HealthCheckError, match="does not match"):
        preview_health.probe_health("journey-flight-check.vercel.app", "b" * 40)


def test_invalid_mode_is_failure(monkeypatch):
    use_response(monkeypatch, body=json.dumps(payload(mode="testing")).encode())
    with pytest.raises(preview_health.HealthCheckError, match="invalid mode"):
        preview_health.probe_health("journey-flight-check.vercel.app")


def test_oversized_health_response_is_failure(monkeypatch):
    use_response(monkeypatch, body=b" " * (16 * 1024 + 1))
    with pytest.raises(preview_health.HealthCheckError, match="too large"):
        preview_health.probe_health("journey-flight-check.vercel.app")


def test_valid_health_contract_and_sha_pass(monkeypatch):
    use_response(monkeypatch, body=json.dumps(payload()).encode())
    preview_health.probe_health("https://journey-flight-check.vercel.app/", EXPECTED_SHA)


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.example",
        "https://journey-flight-check.vercel.app.evil.example",
        "https://user:pass@journey-flight-check.vercel.app",
        "https://journey-flight-check.vercel.app/?token=secret",
        "https://journey-flight-check.vercel.app/#fragment",
        "http://journey-flight-check.vercel.app",
    ],
)
def test_untrusted_or_credential_bearing_url_rejected_before_request(monkeypatch, url):
    monkeypatch.setenv("VERCEL_AUTOMATION_BYPASS_SECRET", "do-not-leak")
    requests = use_response(monkeypatch, body=json.dumps(payload()).encode())
    with pytest.raises(preview_health.HealthCheckError):
        preview_health.probe_health(url)
    assert requests == []


def test_redirect_is_rejected_without_forwarding_secret(monkeypatch):
    monkeypatch.setenv("VERCEL_AUTOMATION_BYPASS_SECRET", "do-not-leak")
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            assert request.get_header("X-vercel-protection-bypass") == "do-not-leak"
            redirect_handler = next(
                handler
                for handler in handlers
                if isinstance(handler, preview_health._NoRedirect)
            )
            redirect_handler.redirect_request(
                request, None, 302, "Found", {}, "https://attacker.example/steal"
            )
            raise AssertionError("redirect target must never be requested")

    handlers = []
    def build_opener(*items):
        handlers.extend(items)
        return Opener()

    monkeypatch.setattr(preview_health.urllib.request, "build_opener", build_opener)
    with pytest.raises(preview_health.HealthCheckError, match="redirected"):
        preview_health.probe_health("https://journey-flight-check.vercel.app")
    assert len(requests) == 1


def test_cli_missing_url_fails_without_probe(monkeypatch, capsys):
    for name in ("PREVIEW_URL", "VERCEL_URL", "DEPLOYMENT_URL", "EXPECTED_SHA", "REQUIRE_EXPECTED_SHA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(preview_health.sys, "argv", ["preview_health.py"])
    monkeypatch.setattr(preview_health, "assert_route_table", lambda: None)
    monkeypatch.setattr(preview_health, "probe_health", lambda *_: pytest.fail("probe must not run"))
    assert preview_health.main() == 1
    assert "no preview URL" in capsys.readouterr().err


def test_cli_missing_required_sha_fails_without_probe(monkeypatch, capsys):
    for name in ("VERCEL_URL", "DEPLOYMENT_URL", "EXPECTED_SHA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PREVIEW_URL", "journey-flight-check.vercel.app")
    monkeypatch.setenv("REQUIRE_EXPECTED_SHA", "true")
    monkeypatch.setattr(preview_health.sys, "argv", ["preview_health.py"])
    monkeypatch.setattr(preview_health, "assert_route_table", lambda: None)
    monkeypatch.setattr(preview_health, "probe_health", lambda *_: pytest.fail("probe must not run"))
    assert preview_health.main() == 1
    assert "expected SHA is required" in capsys.readouterr().err
