"""No live data: skeleton Flight with status unknown."""

from __future__ import annotations

from ..airports import tz_for_iata
from ..model import Flight, normalize_ident


class ScheduleAdapter:
    name = "schedule"

    def lookup(self, ident, date, *, origin=None, now=None, fetch_inbound=True, min_turn=None, **_kw):
        ident = normalize_ident(ident)
        origin = (origin or "").upper()
        return Flight(
            ident=ident,
            ident_iata=ident,
            origin=origin,
            origin_tz=tz_for_iata(origin) if origin else "UTC",
            status="unknown",
            source="none",
            live=False,
        )
