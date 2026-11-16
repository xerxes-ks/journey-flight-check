"""Dataclasses for requests, resolved flights, predictions, and connection risk."""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .times import UTC, local_dict

BANDS = ["on_track", "watch", "at_risk", "unknown"]
STATUSES = (
    "scheduled",
    "active",
    "landed",
    "cancelled",
    "diverted",
    "unknown",
    "too_early",
)

DEFAULT_BUDGET_USD = 0.05
DEFAULT_INBOUND_WINDOW_H = 6
DEFAULT_MCT = 45
DEFAULT_TERMINAL_CHANGE_MIN = 20
DEFAULT_CHECKIN_CLOSE_MIN = 45
DEFAULT_COST_PER_CALL = 0.005
TOO_EARLY_HOURS = 48
WINDOW_END_MAX_AHEAD_H = 47
WINDOW_START_MAX_PAST_DAYS = 10


def worst(*bands: str) -> str:
    return max((b for b in bands if b in BANDS), key=BANDS.index, default="on_track")


def parse_time(value) -> Optional[dt.datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        t = value
    else:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(UTC)


def parse_date(value) -> dt.date:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def mins(delta: dt.timedelta) -> int:
    return int(round(delta.total_seconds() / 60))


def normalize_ident(ident: str) -> str:
    return (ident or "").replace(" ", "").upper()


@dataclass
class Prior:
    dep_median: int = 0
    arr_median: int = 0
    n: int = 0
    source: str = ""


@dataclass
class OnwardRequest:
    ident: str
    date: Optional[str] = None
    origin: Optional[str] = None
    min_connection_min: int = DEFAULT_MCT
    terminal_change: bool = False
    terminal_change_min: int = DEFAULT_TERMINAL_CHANGE_MIN
    bag_recheck: bool = False
    checkin_close_min: int = DEFAULT_CHECKIN_CLOSE_MIN
    separate_ticket: bool = False


@dataclass
class CheckRequest:
    ident: str
    date: str
    origin: Optional[str] = None
    onward: Optional[OnwardRequest] = None
    budget_usd: float = DEFAULT_BUDGET_USD
    min_turn: Optional[int] = None
    inbound_window_h: float = DEFAULT_INBOUND_WINDOW_H
    prior: Optional[Prior] = None
    cost_per_call: float = DEFAULT_COST_PER_CALL
    now: Optional[dt.datetime] = None


@dataclass
class Inbound:
    ident: str
    est_arr: Optional[dt.datetime] = None
    status: str = ""
    dest_tz: str = "UTC"
    dest_iata: str = ""

    def to_dict(self) -> dict:
        return {
            "ident": self.ident,
            "est_arr": local_dict(self.est_arr, self.dest_tz, self.dest_iata),
            "status": self.status,
        }


@dataclass
class Flight:
    ident: str
    ident_iata: str = ""
    ident_icao: str = ""
    fa_flight_id: str = ""
    origin: str = ""
    destination: str = ""
    origin_tz: str = "UTC"
    dest_tz: str = "UTC"
    sched_out: Optional[dt.datetime] = None
    est_out: Optional[dt.datetime] = None
    act_out: Optional[dt.datetime] = None
    sched_in: Optional[dt.datetime] = None
    est_in: Optional[dt.datetime] = None
    act_in: Optional[dt.datetime] = None
    gate_origin: str = ""
    gate_destination: str = ""
    terminal_origin: str = ""
    terminal_destination: str = ""
    baggage_claim: str = ""
    aircraft_type: str = ""
    registration: str = ""
    status: str = "unknown"
    cancelled: bool = False
    diverted: bool = False
    divert_to: str = ""
    source: str = "none"
    live: bool = False
    inbound: Optional[Inbound] = None

    def to_dict(self) -> dict:
        o, d = self.origin, self.destination
        return {
            "ident": self.ident,
            "ident_iata": self.ident_iata,
            "ident_icao": self.ident_icao,
            "fa_flight_id": self.fa_flight_id,
            "origin": self.origin,
            "destination": self.destination,
            "scheduled_out": local_dict(self.sched_out, self.origin_tz, o),
            "estimated_out": local_dict(self.est_out, self.origin_tz, o),
            "actual_out": local_dict(self.act_out, self.origin_tz, o),
            "scheduled_in": local_dict(self.sched_in, self.dest_tz, d),
            "estimated_in": local_dict(self.est_in, self.dest_tz, d),
            "actual_in": local_dict(self.act_in, self.dest_tz, d),
            "gate_origin": self.gate_origin,
            "gate_destination": self.gate_destination,
            "terminal_origin": self.terminal_origin,
            "terminal_destination": self.terminal_destination,
            "baggage_claim": self.baggage_claim,
            "aircraft_type": self.aircraft_type,
            "registration": self.registration,
            "status": self.status,
            "cancelled": self.cancelled,
            "diverted": self.diverted,
            "divert_to": self.divert_to,
            "source": self.source,
            "live": self.live,
        }


@dataclass
class Prediction:
    dep_delay_min: int
    arr_delay_min: int
    pred_out: Optional[dt.datetime]
    pred_in: Optional[dt.datetime]
    band: str
    reasons: list
    final: bool = False
    schedule_only: bool = False
    airline_dep_delay: Optional[int] = None
    origin_tz: str = "UTC"
    dest_tz: str = "UTC"
    origin: str = ""
    destination: str = ""

    def to_dict(self) -> dict:
        return {
            "dep_delay_min": self.dep_delay_min,
            "arr_delay_min": self.arr_delay_min,
            "expected_out": local_dict(self.pred_out, self.origin_tz, self.origin),
            "expected_in": local_dict(self.pred_in, self.dest_tz, self.destination),
            "band": self.band,
            "reasons": list(self.reasons),
            "final": self.final,
            "schedule_only": self.schedule_only,
        }


@dataclass
class ConnectionRisk:
    band: str
    slack_min: int
    required_min: int
    protected: bool
    explain: str
    onward_ident: str = ""

    def to_dict(self) -> dict:
        return {
            "band": self.band,
            "slack_min": self.slack_min,
            "required_min": self.required_min,
            "protected": self.protected,
            "explain": self.explain,
            "onward_ident": self.onward_ident,
        }


@dataclass
class CallRecord:
    path: str
    params: dict
    http: int
    cost_usd: float
    ms: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CheckResult:
    flight: Flight
    inbound: Optional[Inbound]
    prediction: Prediction
    connection: Optional[ConnectionRisk]
    calls: list = field(default_factory=list)
    cost_usd_est: float = 0.0
    ok: bool = True
    summary: str = ""
    next_check_local: Optional[dict] = None
    next_check_in_minutes: Optional[int] = None
    error: Optional[dict] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "flight": self.flight.to_dict(),
            "inbound": self.inbound.to_dict() if self.inbound else None,
            "prediction": self.prediction.to_dict(),
            "connection": self.connection.to_dict() if self.connection else None,
            "next_check_local": self.next_check_local,
            "next_check_in_minutes": self.next_check_in_minutes,
            "calls": [c.to_dict() if hasattr(c, "to_dict") else c for c in self.calls],
            "cost_usd_est": self.cost_usd_est,
            "error": self.error,
        }
