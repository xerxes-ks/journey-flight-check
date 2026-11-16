"""Errors raised by the library. Messages never include credentials."""

from __future__ import annotations


def error_code_for_http(status: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        429: "rate_limited",
    }.get(int(status or 0), "upstream_error")


class CheckError(RuntimeError):
    """Base error for a flight check that did not complete."""

    def __init__(self, message: str, code: str = "error", http_status: int = 0, snippet: str = ""):
        self.code = code
        self.http_status = http_status
        self.snippet = snippet
        super().__init__(message)

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": str(self)}
        if self.http_status:
            out["http"] = self.http_status
        if self.snippet:
            out["snippet"] = self.snippet
        return out


class BudgetExceeded(CheckError):
    """The configured per-call AeroAPI budget would be exceeded by the next request."""

    def __init__(self, message: str):
        super().__init__(message, code="budget_exceeded")


class AeroAPIError(CheckError):
    """Upstream AeroAPI returned a non-success status."""

    def __init__(self, http_status: int, snippet: str = ""):
        code = error_code_for_http(http_status)
        msg = f"AeroAPI HTTP {http_status}"
        if snippet:
            msg = f"{msg}: {snippet}"
        super().__init__(msg, code=code, http_status=http_status, snippet=snippet)
