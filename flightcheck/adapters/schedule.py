"""No live data: return a skeleton Flight. Used when no API key is set or AeroAPI has nothing."""

from __future__ import annotations

from ..model import Flight, normalize_ident, parse_date


class ScheduleAdapter:
    name = "schedule"

    def lookup(self, ident, date, *, origin=None, now=None, fetch_inbound=True, min_turn=30):
        ident = normalize_ident(ident)
        origin = (origin or "").upper()
        return Flight(
            ident=ident,
            ident_iata=ident,
            origin=origin,
            status="scheduled",
            source="schedule",
            live=False,
        )
