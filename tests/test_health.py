"""GET /api/health — no auth, booleans only."""

from __future__ import annotations

from flightcheck.health import health_payload
from flightcheck.routes import handle_health
from flightcheck.store import use_store, MemoryStore


def test_health_no_auth_booleans(monkeypatch):
    monkeypatch.delenv("FLIGHTAWARE_AEROAPI_KEY", raising=False)
    monkeypatch.setenv("FLIGHTCHECK_TOKEN", "tok")
    monkeypatch.setenv("FLIGHTCHECK_WEBHOOK_SECRET", "wh")
    monkeypatch.setenv("CRON_SECRET", "cron")
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.setenv("FLIGHTCHECK_STORAGE", "memory")
    monkeypatch.setenv("FLIGHTCHECK_MODE", "fixtures")
    monkeypatch.setenv("VERCEL_GIT_COMMIT_SHA", "deadbeef")
    use_store(None)
    status, payload = handle_health("GET", {}, {}, {})
    assert status == 200
    assert payload["ok"] is True
    assert payload["storage"] == "memory"
    assert payload["mode"] == "fixtures"
    assert payload["sha"] == "deadbeef"
    assert payload["version"]
    for key in (
        "aeroapi_key_configured",
        "token_configured",
        "webhook_secret_configured",
        "cron_secret_configured",
    ):
        assert isinstance(payload[key], bool)
    assert payload["aeroapi_key_configured"] is False
    assert payload["token_configured"] is True
    assert payload["webhook_secret_configured"] is True
    assert payload["cron_secret_configured"] is True
    assert payload.get("token") is None
    assert "wh" not in str(payload.values())
    assert "cron" not in str(payload.values())
    assert "deadbeef" == payload["sha"]


def test_health_storage_none(monkeypatch):
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.delenv("FLIGHTCHECK_STORAGE", raising=False)
    monkeypatch.delenv("KV_REST_API_URL", raising=False)
    monkeypatch.delenv("KV_REST_API_TOKEN", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    use_store(None)
    payload = health_payload()
    assert payload["storage"] == "none"


def test_health_reports_blob(monkeypatch):
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_x_y")
    monkeypatch.delenv("FLIGHTCHECK_STORAGE", raising=False)
    use_store(None)
    assert health_payload()["storage"] == "blob"
    use_store(MemoryStore())
    assert health_payload()["storage"] == "memory"
    use_store(None)
