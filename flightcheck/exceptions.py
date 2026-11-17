"""Errors raised by the library. Messages never include credentials."""

from __future__ import annotations

# Codes a cron can branch on. 429/5xx/timeouts are retryable; 4xx (except 429)
# and empty results are not — follow next_poll_min instead.
RETRYABLE_CODES = frozenset({
    "rate_limited",
    "upstream_rate_limited",
    "upstream_error",
    "upstream_unavailable",
    "timeout",
    "error",
})

# Token-gated routes share this 401 message (including /api/check).
UNAUTH_MESSAGE = "missing or invalid token"

# Application error codes → HTTP. Upstream 5xx stay 200 with a structured
# error object so a tool-calling client always gets JSON, never a bare 502.
HTTP_FOR_CODE = {
    "unauthorized": 401,
    "bad_request": 400,
    "invalid_date": 400,
    "not_found": 404,
    "origin_mismatch": 404,
    "method_not_allowed": 405,
    "budget_exceeded": 402,
    "conflict": 409,
    "rate_limited": 429,
    "storage_not_configured": 503,
    "storage_error": 503,
    "forbidden": 403,
}


def retryable_for(code: str) -> bool:
    return (code or "") in RETRYABLE_CODES


def http_status_for(code: str, default: int = 200, http_status: int = 0) -> int:
    if http_status:
        return int(http_status)
    return HTTP_FOR_CODE.get(code or "", default)


def error_code_for_http(status: int) -> str:
    code = int(status or 0)
    if code in (408,):
        return "timeout"
    if code == 429:
        return "rate_limited"
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
    }.get(code, "upstream_error")


def error_dict(code: str, message: str, http: int = 0, snippet: str = "") -> dict:
    out = {
        "code": code,
        "message": str(message),
        "retryable": retryable_for(code),
    }
    if http:
        out["http"] = http
    if snippet:
        out["snippet"] = snippet
    return out


class CheckError(RuntimeError):
    """Base error for a flight check that did not complete."""

    def __init__(self, message: str, code: str = "error", http_status: int = 0, snippet: str = ""):
        self.code = code
        self.http_status = http_status
        self.snippet = snippet
        super().__init__(message)

    def to_dict(self) -> dict:
        return error_dict(self.code, str(self), self.http_status, self.snippet)


class BudgetExceeded(CheckError):
    """The configured per-call AeroAPI budget would be exceeded by the next request."""

    def __init__(self, message: str):
        super().__init__(message, code="budget_exceeded", http_status=402)


class InvalidBudget(CheckError):
    """A money limit was NaN, infinite, negative, or otherwise unusable."""

    def __init__(self, message: str = "budget must be a finite nonnegative number"):
        super().__init__(message, code="bad_request", http_status=400)


class Conflict(CheckError):
    """Idempotent retry with a different subscription fingerprint."""

    def __init__(self, message: str = "subscription configuration conflict"):
        super().__init__(message, code="conflict", http_status=409)


class IsolationError(CheckError):
    """Fixture tracking attempted against the live store namespace."""

    def __init__(self, message: str = "fixture tracking cannot use the live store"):
        super().__init__(message, code="forbidden", http_status=403)


class InvalidDate(CheckError):
    """Ticket date was not a YYYY-MM-DD calendar date."""

    def __init__(self, message: str = "invalid date"):
        super().__init__(message, code="invalid_date", http_status=400)


class OriginMismatch(CheckError):
    """Requested origin does not match any returned flight leg."""

    def __init__(self, ident: str, origin: str):
        super().__init__(
            f"No {ident} departure from {origin}",
            code="origin_mismatch",
            http_status=404,
        )


class StorageError(CheckError):
    """Blob/Redis config or permission failure. Not retryable."""

    def __init__(self, message: str, http_status: int = 503, snippet: str = ""):
        super().__init__(
            message,
            code="storage_error",
            http_status=http_status or 503,
            snippet=snippet,
        )


class AeroAPIError(CheckError):
    """Upstream AeroAPI returned a non-success status."""

    def __init__(self, http_status: int, snippet: str = "", code: str | None = None):
        code = code or error_code_for_http(http_status)
        if code == "timeout":
            msg = "The live status request timed out."
        else:
            msg = f"AeroAPI HTTP {http_status}"
            if snippet:
                msg = f"{msg}: {snippet}"
        super().__init__(msg, code=code, http_status=http_status, snippet=snippet)
