"""Recheck cadence helper for callers that poll. This package does not schedule itself."""

from __future__ import annotations

import datetime as dt
from typing import Optional

DateTime = dt.datetime

CADENCE = [
    (48, None),
    (6, 180),
    (3, 60),
    (0, 15),
]


def poll_interval(
    sched_dep: DateTime,
    now: DateTime,
    landed: bool = False,
    sched_arr: Optional[DateTime] = None,
    actual_dep: Optional[DateTime] = None,
) -> Optional[int]:
    """Minutes between polls, or None for 'do not poll' (T−48h or landed).

    none beyond T-48h, 3-hourly to T-6h, hourly to T-3h, 15-min through
    scheduled departure until actual off, then 30-60 min airborne
    (30 if block time is 4h or less), stop at landing.

    Airborne cadence keys off actual departure when known. After STD while
    still at the gate (no actual_dep), keep the 15-min cadence.
    """
    if landed:
        return None
    if actual_dep is not None:
        if sched_arr is not None and sched_dep is not None:
            block = sched_arr - sched_dep
            return 30 if block <= dt.timedelta(hours=4) else 60
        return 30
    to_dep = (sched_dep - now).total_seconds() / 3600
    if to_dep < 0:
        return 15
    for hours, every in CADENCE:
        if to_dep >= hours:
            return every
    return 15
