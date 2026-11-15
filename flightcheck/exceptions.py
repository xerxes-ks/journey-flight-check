"""Errors raised by the library. Messages never include credentials or raw upstream bodies."""


class CheckError(RuntimeError):
    """Base error for a flight check that did not complete."""


class BudgetExceeded(CheckError):
    """The configured per-call AeroAPI budget would be exceeded by the next request."""


class AeroAPIError(CheckError):
    """Upstream AeroAPI returned a non-success status. The body is discarded."""

    def __init__(self, http_status: int):
        self.http_status = http_status
        super().__init__(f"AeroAPI HTTP {http_status}")
