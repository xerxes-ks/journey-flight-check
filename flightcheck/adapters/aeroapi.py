"""FlightAware AeroAPI v4 adapter. API only; never scrape the website.

Calls:
  GET /flights/{ident}?start=&end=&ident_type=designator
  GET /flights/{fa_flight_id}  (inbound aircraft, only inside the inbound window)

Every call is logged with an estimated cost. A hard budget stops the next call
when it would exceed the cap. Upstream error bodies are discarded so a key
echoed by the provider cannot appear in responses or logs.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from ..exceptions import AeroAPIError, BudgetExceeded
from ..model import (
    DEFAULT_COST_PER_CALL,
    DEFAULT_INBOUND_WINDOW_H,
    UTC,
    Flight,
    Inbound,
    Instant,
    normalize_ident,
    parse_date,
    parse_time,
)
from ..sanitize import redact
from .schedule import ScheduleAdapter

BASE = "https://aeroapi.flightaware.com/aeroapi"


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
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                exc.read()  # drain; body is discarded and never returned
            except Exception:
                pass
            return exc.code, {}
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
            # Never surface fetch/HTTP exception text (it may contain the key).
            status, body = 0, {}
        self.calls.append({
            "path": redact(path, self.key),
            "params": {k: redact(v, self.key) for k, v in (params or {}).items()},
            "http": int(status or 0),
            "cost_usd": cost,
            "ms": int((time.time() - t0) * 1000),
        })
        if status != 200:
            raise AeroAPIError(int(status or 0))
        if not isinstance(body, dict):
            return {}
        return body

    @staticmethod
    def _iso(t: dt.datetime) -> str:
        return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def date_window(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
        start = dt.datetime.combine(day, dt.time.min, tzinfo=UTC) - dt.timedelta(hours=14)
        end = dt.datetime.combine(day, dt.time.min, tzinfo=UTC) + dt.timedelta(hours=38)
        return start, end

    def pick_instance(self, flights, day: dt.date, origin: Optional[str] = None):
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
            local = Instant.from_dt(t, tzname)
            local_day = dt.date.fromisoformat(local.local[:10]) if local else t.date()
            if local_day == day or t.astimezone(UTC).date() == day:
                dated.append(f)
        cands = dated or [f for f in flights if sched(f)]
        if not cands:
            return None
        noon = dt.datetime.combine(day, dt.time(12, 0), tzinfo=UTC)
        return min(cands, key=lambda f: abs((sched(f) - noon).total_seconds()))

    def lookup(self, ident, date, *, origin=None, now=None, fetch_inbound=True, min_turn=30):
        ident = normalize_ident(ident)
        day = parse_date(date)
        origin = (origin or "").upper() or None
        now = now or dt.datetime.now(UTC)
        start, end = self.date_window(day)
        params = {
            "start": self._iso(start),
            "end": self._iso(end),
            "ident_type": "designator",
        }
        try:
            body = self.call(f"/flights/{urllib.parse.quote(ident)}", params)
        except AeroAPIError as exc:
            if exc.http_status in (0, 404):
                return self.fallback.lookup(ident, date, origin=origin, now=now)
            raise
        raw = self.pick_instance(body.get("flights") or [], day, origin)
        if not raw:
            return self.fallback.lookup(ident, date, origin=origin, now=now)
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
                dest_tz = ((inbound.get("destination") or {}).get("timezone")) or flight.origin_tz
                flight.inbound = Inbound(
                    ident=inbound.get("ident_iata") or inbound.get("ident") or "",
                    est_arr=parse_time(
                        inbound.get("actual_in")
                        or inbound.get("estimated_in")
                        or inbound.get("scheduled_in")
                    ),
                    status=inbound.get("status") or "",
                    origin_tz=dest_tz,
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
