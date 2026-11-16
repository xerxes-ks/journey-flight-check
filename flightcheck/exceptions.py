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


def retryable_for(code: str) -> bool:
    return (code or "") in RETRYABLE_CODES


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
        super().__init__(message, code="budget_exceeded")


class AeroAPIError(CheckError):
    """Upstream AeroAPI returned a non-success status."""

    def __init__(self, http_status: int, snippet: str = "", code: str | None = None):
        code = code or error_code_for_http(http_status)
        if code == "timeout":
            msg = "AeroAPI timeout"
        else:
            msg = f"AeroAPI HTTP {http_status}"
        if snippet:
            msg = f"{msg}: {snippet}"
        super().__init__(msg, code=code, http_status=http_status, snippet=snippet)
