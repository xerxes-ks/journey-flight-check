"""Recheck cadence helper for callers that poll. This package does not schedule itself."""

from __future__ import annotations

import datetime as dt
from typing import Optional, Union

DateTime = dt.datetime

# (hours before departure at least, poll every N minutes). None = do not poll.
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
) -> Optional[int]:
    """Minutes between polls, or None for 'do not poll'.

    none beyond T-48h, 3-hourly to T-6h, hourly to T-3h, 15-min to departure,
    30-60 min airborne (30 if block time is 4h or less), stop at landing.
    """
    if landed:
        return None
    to_dep = (sched_dep - now).total_seconds() / 3600
    if to_dep < 0:
        if sched_arr is not None:
            block = sched_arr - sched_dep
            return 30 if block <= dt.timedelta(hours=4) else 60
        return 30
    for hours, every in CADENCE:
        if to_dep >= hours:
            return every
    return 15
