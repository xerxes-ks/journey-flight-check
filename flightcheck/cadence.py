"""Recheck cadence. Default is a sparse essential schedule (~7–10 calls).

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
    "t_minus_30m",
    "takeoff",
    "landing",
    "done",
)
INBOUND_CHECKPOINTS = frozenset({"t_minus_3h", "t_minus_1h"})
# T-24h and T-6h stay on STD. T-3h / T-1h / T-30m re-anchor to estimated departure.
PRE_STD = (
    ("t_minus_24h", dt.timedelta(hours=24)),
    ("t_minus_6h", dt.timedelta(hours=6)),
)
PRE_EST = (
    ("t_minus_3h", dt.timedelta(hours=3)),
    ("t_minus_1h", dt.timedelta(hours=1)),
    ("t_minus_30m", dt.timedelta(minutes=30)),
)
TAKEOFF_AFTER = dt.timedelta(minutes=30)
LANDING_AFTER = dt.timedelta(minutes=10)
MIN_LANDING_GAP = dt.timedelta(minutes=20)
GROUND_RECHECK = dt.timedelta(minutes=30)
CANCEL_RECHECK = dt.timedelta(minutes=30)
RETRYABLE_RETRY = dt.timedelta(minutes=15)
MAX_AIRBORNE_CHECKS = 24

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
    """Dense cadence: minutes between polls, or None for 'do not poll'."""
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


def _pre_times(std: dt.datetime, anchor: dt.datetime):
    times = [(std - delta, name) for name, delta in PRE_STD]
    times.extend((anchor - delta, name) for name, delta in PRE_EST)
    times.sort(key=lambda x: x[0])
    return times


def _current_pre(now, times, takeoff_at):
    current_name = times[0][1]
    next_at = times[1][0] if len(times) > 1 else takeoff_at
    for i, (when, name) in enumerate(times):
        if now >= when:
            current_name = name
            next_at = times[i + 1][0] if i + 1 < len(times) else takeoff_at
    return current_name, next_at


def essential_plan(
    *,
    now: dt.datetime,
    std: Optional[dt.datetime],
    sta: Optional[dt.datetime] = None,
    expected_off: Optional[dt.datetime] = None,
    expected_arr: Optional[dt.datetime] = None,
    actual_off: Optional[dt.datetime] = None,
    actual_on: Optional[dt.datetime] = None,
    actual_in: Optional[dt.datetime] = None,
    cancelled: bool = False,
    diverted: bool = False,
    previous_checkpoint: Optional[str] = None,
    previous_phase: Optional[str] = None,
    attempts: int = 0,
) -> Plan:
    """Essential checkpoints. Retimes re-anchor T-3h / T-1h / T-30m to expected_off."""
    attempts = int(attempts or 0)
    now = now if now.tzinfo else now.replace(tzinfo=dt.timezone.utc)

    if diverted:
        return Plan("done", None, False, attempts, final=True)

    if std is None:
        name = "t_minus_24h"
        if cancelled:
            if attempts >= 1 or previous_checkpoint == "done":
                return Plan("done", None, False, max(attempts, 1), final=True)
            return Plan(previous_checkpoint if previous_checkpoint in CHECKPOINTS else name,
                        now + CANCEL_RECHECK, False, 1, final=False)
        return Plan(name, None, False, 0, final=False)

    std = _aware(std, now)
    sta = _aware(sta, now)
    expected_off = _aware(expected_off, now) or std
    expected_arr = _aware(expected_arr, now) or sta
    anchor = expected_off
    takeoff_at = anchor + TAKEOFF_AFTER
    landing_due = (expected_arr + LANDING_AFTER) if expected_arr else now + MIN_LANDING_GAP
    times = _pre_times(std, anchor)
    t24 = times[0][0]
    pre_name, pre_next = _current_pre(now, times, takeoff_at)

    if cancelled:
        if attempts >= 1 or previous_checkpoint == "done":
            return Plan("done", None, False, max(attempts, 1), final=True)
        name = pre_name if now >= t24 else "t_minus_24h"
        if previous_checkpoint in CHECKPOINTS and previous_checkpoint not in ("done",):
            name = previous_checkpoint if previous_checkpoint in (
                "t_minus_24h", "t_minus_6h", "t_minus_3h", "t_minus_1h", "t_minus_30m", "takeoff", "landing",
            ) else name
        if now >= t24:
            name = pre_name
        return Plan(name, now + CANCEL_RECHECK, False, 1, final=False)

    landed = actual_on is not None or actual_in is not None
    airborne = actual_off is not None and not landed

    if landed:
        return Plan("done", None, False, max(attempts, 1), final=True)

    if airborne:
        eta_plus = landing_due
        nxt = eta_plus
        if nxt <= now:
            nxt = now + MIN_LANDING_GAP
        elif (nxt - now) < MIN_LANDING_GAP:
            nxt = now + MIN_LANDING_GAP
        if previous_checkpoint not in ("takeoff", "landing"):
            return Plan("takeoff", nxt, False, 1, final=False)
        used = attempts + 1 if previous_checkpoint == "landing" else 1
        if used > MAX_AIRBORNE_CHECKS:
            nxt = now + dt.timedelta(minutes=60)
        return Plan("landing", nxt, False, used, final=False)

    if now < t24:
        return Plan("t_minus_24h", t24, False, 0, final=False)

    # Past the (possibly retimed) departure and still on the ground: 30-min
    # rechecks. Never jump to the landing slot while not airborne.
    dep_mark = expected_off if expected_off and expected_off > std else std
    if now >= dep_mark or now >= takeoff_at or previous_checkpoint == "takeoff":
        return Plan("takeoff", now + GROUND_RECHECK, False, max(attempts, 1), final=False)

    return Plan(
        pre_name,
        pre_next,
        pre_name in INBOUND_CHECKPOINTS,
        0,
        final=False,
    )
