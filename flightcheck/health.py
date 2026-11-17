"""Public health snapshot. Booleans only — never secret values."""

from __future__ import annotations

import os

from . import __version__
from .mode import env_mode
from .store import storage_kind


def git_sha() -> str:
    return (
        os.environ.get("VERCEL_GIT_COMMIT_SHA")
        or os.environ.get("GIT_COMMIT_SHA")
        or os.environ.get("GIT_SHA")
        or ""
    )


def health_payload() -> dict:
    kind = storage_kind()
    return {
        "ok": True,
        "version": __version__,
        "sha": git_sha(),
        "storage": kind if kind in ("blob", "memory", "redis") else "none",
        "aeroapi_key_configured": bool(os.environ.get("FLIGHTAWARE_AEROAPI_KEY")),
        "token_configured": bool(os.environ.get("FLIGHTCHECK_TOKEN")),
        "webhook_secret_configured": bool(os.environ.get("FLIGHTCHECK_WEBHOOK_SECRET")),
        "cron_secret_configured": bool(os.environ.get("CRON_SECRET")),
        "mode": env_mode(),
    }
