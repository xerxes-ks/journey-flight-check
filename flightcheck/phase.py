"""Movement phase from gate and runway times. Independent of AeroAPI status text."""

from __future__ import annotations

import datetime as dt

from .model import Flight, UTC


def phase_for(flight: Flight, now: dt.datetime) -> str:
    if flight.status in ("too_early", "unknown"):
        return "scheduled"
    if flight.act_in:
        return "at_arrival_gate"
    if flight.act_on:
        return "landed"
    if flight.act_off:
        return "airborne"
    if flight.act_out:
        return "taxiing"
    std = flight.sched_out
    if std is not None:
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        if now.astimezone(std.tzinfo) >= std:
            return "at_gate_late"
    return "scheduled"
