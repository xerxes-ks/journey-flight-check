"""Recheck cadence. Default is a sparse essential schedule (~7–9 calls).

Dense (the old 15-min poll table) is available as cadence='dense'.
This package does not schedule itself; callers follow next_check_*.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

DateTime = dt.datetime

CHECKPOINTS = (
    "t_minus_24h",
    "t_minus_6h",
    "t_minus_3h",
    "t_minus_1h",
    "takeoff",
    "landing",
    "done",
)
INBOUND_CHECKPOINTS = frozenset({"t_minus_3h", "t_minus_1h"})
PRE_DEP = (
    ("t_minus_24h", dt.timedelta(hours=24)),
    ("t_minus_6h", dt.timedelta(hours=6)),
    ("t_minus_3h", dt.timedelta(hours=3)),
    ("t_minus_1h", dt.timedelta(hours=1)),
)
TAKEOFF_AFTER = dt.timedelta(minutes=30)
LANDING_AFTER = dt.timedelta(minutes=10)
CANCEL_RECHECK = dt.timedelta(minutes=30)
RETRYABLE_RETRY = dt.timedelta(minutes=15)
MAX_LANDING_RETRIES = 2

CADENCE = [
    (48, None),
    (6, 180),
    (3, 60),
    (0, 15),
]


@dataclass
class Plan:
    checkpoint: str
    next_at: Optional[dt.datetime]
    fetch_inbound: bool
    attempts: int
    final: bool = False


def poll_interval(
    sched_dep: DateTime,
    now: DateTime,
    landed: bool = False,
    sched_arr: Optional[DateTime] = None,
    actual_dep: Optional[DateTime] = None,
    est_arr: Optional[DateTime] = None,
) -> Optional[int]:
    """Dense cadence: minutes between polls, or None for 'do not poll'.

    none beyond T-48h, 3-hourly to T-6h, hourly to T-3h, 15-min through
    scheduled departure until actual off, then 30-60 min airborne
    (30 if block time is 4h or less), 15 min once estimated arrival is
    within 45 min, stop at landing.
    """
    if landed:
        return None
    if actual_dep is not None:
        eta = est_arr or sched_arr
        if eta is not None and (eta - now).total_seconds() <= 45 * 60:
            return 15
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


def _aware(t: Optional[dt.datetime], vs: dt.datetime) -> Optional[dt.datetime]:
    if t is None:
        return None
    if t.tzinfo is None:
        return t.replace(tzinfo=vs.tzinfo or dt.timezone.utc)
    if vs.tzinfo:
        return t.astimezone(vs.tzinfo)
    return t


def inbound_for_checkpoint(checkpoint: str) -> bool:
    return checkpoint in INBOUND_CHECKPOINTS


def essential_plan(
    *,
    now: dt.datetime,
    std: Optional[dt.datetime],
    sta: Optional[dt.datetime] = None,
    expected_off: Optional[dt.datetime] = None,
    expected_arr: Optional[dt.datetime] = None,
    actual_off: Optional[dt.datetime] = None,
    actual_on: Optional[dt.datetime] = None,
    cancelled: bool = False,
    diverted: bool = False,
    previous_checkpoint: Optional[str] = None,
    attempts: int = 0,
) -> Plan:
    """Essential checkpoints relative to STD. Retimes use expected_off / expected_arr."""
    attempts = int(attempts or 0)
    now = now if now.tzinfo else now.replace(tzinfo=dt.timezone.utc)

    if diverted or actual_on is not None:
        return Plan("done", None, False, attempts, final=True)

    if cancelled:
        if previous_checkpoint == "done" or attempts >= 1:
            return Plan("done", None, False, max(attempts, 1), final=True)
        name = previous_checkpoint if previous_checkpoint in CHECKPOINTS else "t_minus_24h"
        return Plan(name, now + CANCEL_RECHECK, False, 1, final=False)

    if std is None:
        return Plan("t_minus_24h", None, False, 0, final=False)

    std = _aware(std, now)
    sta = _aware(sta, now)
    expected_off = _aware(expected_off, now) or std
    expected_arr = _aware(expected_arr, now) or sta
    takeoff_at = expected_off + TAKEOFF_AFTER
    landing_at = (expected_arr + LANDING_AFTER) if expected_arr else now + RETRYABLE_RETRY
    pre = [(std - delta, name) for name, delta in PRE_DEP]
    t24 = pre[0][0]
    airborne = actual_off is not None

    if now < t24:
        return Plan("t_minus_24h", t24, False, 0, final=False)

    if airborne:
        if previous_checkpoint == "landing" or now >= landing_at:
            used = attempts if previous_checkpoint == "landing" else 0
            if used >= MAX_LANDING_RETRIES:
                return Plan("done", None, False, used, final=True)
            return Plan("landing", landing_at, False, used + 1, final=False)
        return Plan("takeoff", landing_at, False, 0, final=False)

    # On the ground. Takeoff check is due at expected_off + 30 min.
    # One retry 30 min later, then switch to the landing check.
    if now >= takeoff_at or previous_checkpoint == "takeoff":
        first = previous_checkpoint != "takeoff" and attempts < 1
        retry_at = takeoff_at + TAKEOFF_AFTER
        if first and now < retry_at:
            return Plan("takeoff", retry_at, False, 1, final=False)
        return Plan("takeoff", landing_at, False, max(attempts, 1), final=False)

    current_name = pre[0][1]
    next_at = pre[1][0]
    for i, (when, name) in enumerate(pre):
        if now >= when:
            current_name = name
            next_at = pre[i + 1][0] if i + 1 < len(pre) else takeoff_at
    return Plan(
        current_name,
        next_at,
        current_name in INBOUND_CHECKPOINTS,
        0,
        final=False,
    )
