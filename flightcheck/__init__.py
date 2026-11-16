"""flightcheck: live flight status, delay prediction, and connection-risk scoring.

v0.1 is stateless. Scheduling, messaging, and trip manifests belong to callers.
Times in the public API are origin/destination local; callers should not convert to UTC.
"""

from .cadence import essential_plan, poll_interval
from .check import check_flight
from .exceptions import AeroAPIError, BudgetExceeded, CheckError
from .model import CheckRequest, OnwardRequest, Prior

__version__ = "0.1.0"

__all__ = [
    "AeroAPIError",
    "BudgetExceeded",
    "CheckError",
    "CheckRequest",
    "OnwardRequest",
    "Prior",
    "check_flight",
    "essential_plan",
    "poll_interval",
    "__version__",
]
