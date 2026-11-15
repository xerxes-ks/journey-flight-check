"""Strip credentials from anything that might be returned, printed, or logged."""

from __future__ import annotations

import os
import re

_KEY_ENVS = ("FLIGHTAWARE_AEROAPI_KEY", "FLIGHTCHECK_TOKEN")
_HEADERISH = re.compile(
    r"(?i)(x-apikey|api[_-]?key|authorization|bearer)\s*[:=]\s*\S+"
)


def secret_values(*extra: str | None) -> list[str]:
    out: list[str] = []
    for item in extra:
        if item:
            out.append(item)
    for name in _KEY_ENVS:
        val = os.environ.get(name)
        if val:
            out.append(val)
    # Longest first so overlapping secrets redact fully.
    out.sort(key=len, reverse=True)
    return out


def redact(text, *extra: str | None) -> str:
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    for secret in secret_values(*extra):
        if secret:
            s = s.replace(secret, "[redacted]")
    s = _HEADERISH.sub(lambda m: m.group(1) + "=[redacted]", s)
    return s
