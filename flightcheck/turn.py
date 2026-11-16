"""Aircraft-aware default minimum turn (gate-in of inbound to push-back)."""

from __future__ import annotations

import datetime as dt
import re
from typing import Optional

WIDEBODY = re.compile(r"^(B77|B78|A33|A35|A38)", re.I)
NARROW_TURN = 35
WIDE_TURN = 60
LONG_HAUL = dt.timedelta(hours=5)


def default_min_turn(
    aircraft_type: str = "",
    sched_out: Optional[dt.datetime] = None,
    sched_in: Optional[dt.datetime] = None,
) -> int:
    ident = (aircraft_type or "").replace("-", "").replace(" ", "")
    if ident and WIDEBODY.match(ident):
        return WIDE_TURN
    if sched_out and sched_in and (sched_in - sched_out) > LONG_HAUL:
        return WIDE_TURN
    return NARROW_TURN
