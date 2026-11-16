"""FlightAware AeroAPI v4 adapter. API only; never scrape the website.

Calls:
  GET /flights/{ident}?start=&end=&ident_type=designator
  GET /flights/{fa_flight_id}  (inbound aircraft, only inside the inbound window)

Every call is logged with an estimated cost. A hard budget stops the next call
when it would exceed the cap. Upstream error bodies are kept as a short
sanitized snippet (never the API key).
"""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional
from zoneinfo import ZoneInfo

from ..exceptions import AeroAPIError, BudgetExceeded

# Fail-fast so a caller with a ~10s timeout can treat a hang as retryable.
UPSTREAM_TIMEOUT_S = 8
from ..model import (
    DEFAULT_COST_PER_CALL,
    DEFAULT_INBOUND_WINDOW_H,
    UTC,
    WINDOW_END_MAX_AHEAD_H,
    WINDOW_START_MAX_PAST_DAYS,
    Flight,
    Inbound,
    normalize_ident,
    parse_date,
    parse_time,
)
from ..sanitize import redact
from .schedule import ScheduleAdapter

BASE = "https://aeroapi.flightaware.com/aeroapi"
SNIPPET_LEN = 240


def date_window(
    day: dt.date,
    now: dt.datetime,
    origin_tz: str = "UTC",
) -> tuple[dt.datetime, dt.datetime]:
    """Inclusive start / exclusive end, clamped to AeroAPI's ~2-day-ahead limit."""
    now = now.astimezone(UTC)
    try:
        tz = ZoneInfo(origin_tz) if origin_tz else UTC
        local_start = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
    except Exception:
        local_start = dt.datetime.combine(day, dt.time.min, tzinfo=UTC)
    local_end = local_start + dt.timedelta(days=1)
    start = local_start.astimezone(UTC) - dt.timedelta(hours=2)
    end = local_end.astimezone(UTC) + dt.timedelta(hours=2)
    max_end = now + dt.timedelta(hours=WINDOW_END_MAX_AHEAD_H)
    min_start = now - dt.timedelta(days=WINDOW_START_MAX_PAST_DAYS)
    if end > max_end:
        end = max_end
    if start < min_start:
        start = min_start
    if end <= start:
        end = start + dt.timedelta(minutes=1)
    return start, end


def _body_snippet(body, key=None) -> str:
    if not body:
        return ""
    if isinstance(body, dict):
        text = json.dumps(body, ensure_ascii=False)
    else:
        text = str(body)
    text = text.replace("\n", " ").strip()
    if len(text) > SNIPPET_LEN:
        text = text[: SNIPPET_LEN - 1] + "…"
    return redact(text, key)


class AeroAPIAdapter:
    name = "aeroapi"

    def __init__(
        self,
        key,
        fetch=None,
        budget_usd=5.0,
        inbound_window_h=DEFAULT_INBOUND_WINDOW_H,
        log=None,
        cost_per_call=DEFAULT_COST_PER_CALL,
    ):
        self.key = key
        self.budget = float(budget_usd)
        self.fetch = fetch or self._http
        self.inbound_window = dt.timedelta(hours=inbound_window_h)
        self.calls = log if log is not None else []
        self.cost_per_call = float(cost_per_call)
        self.fallback = ScheduleAdapter()

    @property
    def spent(self) -> float:
        return round(sum(c["cost_usd"] for c in self.calls), 4)

    def _http(self, path, params):
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(
            url,
            headers={"x-apikey": self.key, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_S) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read()
            except Exception:
                raw = b""
            body = {}
            if raw:
                try:
                    body = json.loads(raw.decode())
                except Exception:
                    body = {"detail": raw.decode("utf-8", "replace")[:SNIPPET_LEN]}
            return exc.code, body
        except Exception:
            return 0, {}

    def call(self, path, params=None):
        cost = self.cost_per_call
        if self.spent + cost > self.budget + 1e-9:
            raise BudgetExceeded(f"budget ${self.budget} reached")
        t0 = time.time()
        try:
            status, body = self.fetch(path, params or {})
        except (BudgetExceeded, AeroAPIError):
            raise
        except Exception:
            status, body = 0, {}
        self.calls.append({
            "path": redact(path, self.key),
            "params": {k: redact(v, self.key) for k, v in (params or {}).items()},
            "http": int(status or 0),
            "cost_usd": cost,
            "ms": int((time.time() - t0) * 1000),
        })
        if status != 200:
            raise AeroAPIError(int(status or 0), _body_snippet(body, self.key))
        if not isinstance(body, dict):
            return {}
        return body

    @staticmethod
    def _iso(t: dt.datetime) -> str:
        return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def pick_instance(self, flights, day: dt.date, origin: Optional[str] = None):
        """Match the origin-local calendar date of scheduled_out only.

        UTC date is used only when the origin timezone is unknown.
        """
        flights = [f for f in flights if isinstance(f, dict)]
        if origin:
            origin = origin.upper()
            matched = [
                f for f in flights
                if ((f.get("origin") or {}).get("code_iata") or "").upper() == origin
            ]
            flights = matched or flights

        def sched(f):
            return parse_time(f.get("scheduled_out") or f.get("scheduled_off"))

        dated = []
        for f in flights:
            t = sched(f)
            if t is None:
                continue
            tzname = (f.get("origin") or {}).get("timezone")
            if tzname:
                try:
                    local_day = t.astimezone(ZoneInfo(tzname)).date()
                except Exception:
                    local_day = t.astimezone(UTC).date()
                if local_day == day:
                    dated.append(f)
            else:
                if t.astimezone(UTC).date() == day:
                    dated.append(f)
        if not dated:
            return None
        noon = dt.datetime.combine(day, dt.time(12, 0), tzinfo=UTC)
        return min(dated, key=lambda f: abs((sched(f) - noon).total_seconds()))

    def lookup(self, ident, date, *, origin=None, now=None, fetch_inbound=True, min_turn=None,
               origin_tz: str = "UTC"):
        ident = normalize_ident(ident)
        day = parse_date(date)
        origin = (origin or "").upper() or None
        now = now or dt.datetime.now(UTC)
        start, end = date_window(day, now, origin_tz=origin_tz)
        params = {
            "start": self._iso(start),
            "end": self._iso(end),
            "ident_type": "designator",
        }
        body = self.call(f"/flights/{urllib.parse.quote(ident)}", params)
        raw = self.pick_instance(body.get("flights") or [], day, origin)
        if not raw:
            return None
        flight = self.to_flight(raw)
        if (
            fetch_inbound
            and raw.get("inbound_fa_flight_id")
            and not flight.act_out
            and not flight.cancelled
            and flight.sched_out
            and flight.sched_out - now <= self.inbound_window
        ):
            inbound_id = raw["inbound_fa_flight_id"]
            try:
                ibody = self.call(
                    f"/flights/{urllib.parse.quote(inbound_id, safe='')}",
                    {"ident_type": "fa_flight_id"},
                )
            except AeroAPIError:
                ibody = {}
            fl = ibody.get("flights") or []
            if fl:
                inbound = fl[0]
                dest = inbound.get("destination") or {}
                dest_tz = dest.get("timezone") or flight.origin_tz
                dest_iata = dest.get("code_iata") or flight.origin
                flight.inbound = Inbound(
                    ident=inbound.get("ident_iata") or inbound.get("ident") or "",
                    est_arr=parse_time(
                        inbound.get("actual_in")
                        or inbound.get("estimated_in")
                        or inbound.get("scheduled_in")
                    ),
                    status=inbound.get("status") or "",
                    dest_tz=dest_tz,
                    dest_iata=dest_iata,
                )
        return flight

    @staticmethod
    def to_flight(f: dict) -> Flight:
        cancelled, diverted = bool(f.get("cancelled")), bool(f.get("diverted"))
        if cancelled:
            status = "cancelled"
        elif diverted:
            status = "diverted"
        elif f.get("actual_in") or f.get("actual_on"):
            status = "landed"
        elif f.get("actual_out") or f.get("actual_off"):
            status = "active"
        else:
            status = "scheduled"
        origin = f.get("origin") or {}
        dest = f.get("destination") or {}
        ident_iata = f.get("ident_iata") or ""
        ident_icao = f.get("ident_icao") or f.get("ident") or ""
        return Flight(
            ident=ident_iata or ident_icao,
            ident_iata=ident_iata,
            ident_icao=ident_icao,
            fa_flight_id=f.get("fa_flight_id") or "",
            origin=origin.get("code_iata") or "",
            destination=dest.get("code_iata") or "",
            origin_tz=origin.get("timezone") or "UTC",
            dest_tz=dest.get("timezone") or "UTC",
            sched_out=parse_time(f.get("scheduled_out")),
            est_out=parse_time(f.get("estimated_out")),
            act_out=parse_time(f.get("actual_out") or f.get("actual_off")),
            sched_in=parse_time(f.get("scheduled_in")),
            est_in=parse_time(f.get("estimated_in") or f.get("estimated_on")),
            act_in=parse_time(f.get("actual_in") or f.get("actual_on")),
            gate_origin=f.get("gate_origin") or "",
            gate_destination=f.get("gate_destination") or "",
            terminal_origin=f.get("terminal_origin") or "",
            terminal_destination=f.get("terminal_destination") or "",
            baggage_claim=str(f.get("baggage_claim") or ""),
            aircraft_type=f.get("aircraft_type") or "",
            registration=f.get("registration") or "",
            status=status,
            cancelled=cancelled,
            diverted=diverted,
            divert_to=(dest.get("code_iata") or "") if diverted else "",
            source="aeroapi",
            live=True,
        )
