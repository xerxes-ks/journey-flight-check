"""Orchestrator: adapter -> prediction -> connection scoring. Stateless."""

from __future__ import annotations

import datetime as dt
import os
from typing import Optional
from zoneinfo import ZoneInfo

from .adapters.aeroapi import AeroAPIAdapter
from .adapters.schedule import ScheduleAdapter
from .airports import tz_for_iata
from .cadence import poll_interval
from .connections import assess
from .exceptions import AeroAPIError, BudgetExceeded, CheckError
from .model import (
    UTC,
    TOO_EARLY_HOURS,
    CheckRequest,
    CheckResult,
    Flight,
    OnwardRequest,
    Prior,
    parse_date,
)
from .predict import predict
from .sanitize import redact, secret_values
from .times import local_dict, parse_now_maybe
from .turn import default_min_turn


def _as_bool(value, default=False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_float(value, default):
    if value is None or value == "":
        return default
    return float(value)


def _as_int(value, default):
    if value is None or value == "":
        return default
    return int(value)


def parse_onward(data: Optional[dict]) -> Optional[OnwardRequest]:
    if not data:
        return None
    ident = data.get("flight") or data.get("ident") or data.get("onward")
    if not ident:
        return None
    return OnwardRequest(
        ident=str(ident),
        date=data.get("date") or data.get("onward_date"),
        origin=data.get("origin") or data.get("onward_origin"),
        min_connection_min=_as_int(
            data.get("min_connect_min") or data.get("min_connection_min") or data.get("mct"),
            45,
        ),
        terminal_change=_as_bool(data.get("terminal_change")),
        terminal_change_min=_as_int(data.get("terminal_change_min"), 20),
        bag_recheck=_as_bool(data.get("bags_recheck") if "bags_recheck" in data else data.get("bag_recheck")),
        checkin_close_min=_as_int(data.get("checkin_close_min"), 45),
        separate_ticket=_as_bool(data.get("separate_ticket")),
    )


def parse_request(data: dict) -> CheckRequest:
    ident = data.get("flight") or data.get("ident")
    date = data.get("date")
    if not ident or not date:
        raise ValueError("flight and date are required")
    parse_date(date)
    prior = None
    raw_prior = data.get("prior")
    if isinstance(raw_prior, dict) and int(raw_prior.get("n") or 0) > 0:
        prior = Prior(
            dep_median=int(raw_prior.get("dep_median") or 0),
            arr_median=int(raw_prior.get("arr_median") or 0),
            n=int(raw_prior.get("n") or 0),
            source=str(raw_prior.get("source") or ""),
        )
    onward = data.get("onward")
    if isinstance(onward, str):
        onward = {"flight": onward, **{k: data[k] for k in (
            "onward_date", "onward_origin", "min_connect_min", "min_connection_min", "mct",
            "terminal_change", "terminal_change_min", "bags_recheck", "bag_recheck",
            "checkin_close_min", "separate_ticket",
        ) if k in data}}
    min_turn = None
    if data.get("min_turn") not in (None, ""):
        min_turn = int(data["min_turn"])
    return CheckRequest(
        ident=str(ident),
        date=str(date)[:10],
        origin=(str(data["origin"]).upper() if data.get("origin") else None),
        onward=parse_onward(onward if isinstance(onward, dict) else None),
        budget_usd=_as_float(data.get("budget_usd") or data.get("budget"), 0.05),
        min_turn=min_turn,
        inbound_window_h=_as_float(data.get("inbound_window_h"), 6),
        prior=prior,
        cost_per_call=_as_float(data.get("cost_per_call"), 0.005),
        now=parse_now_maybe(data.get("now")),
    )


def make_adapter(req: CheckRequest, fetch=None, key=None):
    api_key = key if key is not None else os.environ.get("FLIGHTAWARE_AEROAPI_KEY")
    kwargs = dict(
        budget_usd=req.budget_usd,
        inbound_window_h=req.inbound_window_h,
        cost_per_call=req.cost_per_call,
    )
    if fetch is not None:
        return AeroAPIAdapter(api_key or "unused", fetch=fetch, **kwargs)
    if api_key:
        return AeroAPIAdapter(api_key, **kwargs)
    return ScheduleAdapter()


def _local_midnight(day: dt.date, tzname: str) -> dt.datetime:
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        tz = UTC
    return dt.datetime.combine(day, dt.time.min, tzinfo=tz)


def too_early_gate(day: dt.date, now: dt.datetime, origin: Optional[str]):
    """If origin-local midnight of the ticket date is more than 48h out, skip AeroAPI.

    T is approximated as origin-local midnight of the ticket date (the exact STD is
    unknown until we call). next_check is T-48h in that local zone.
    """
    tzname = tz_for_iata(origin) if origin else "UTC"
    t = _local_midnight(day, tzname)
    t_minus = t - dt.timedelta(hours=TOO_EARLY_HOURS)
    if now.astimezone(t.tzinfo) < t_minus:
        return t_minus, tzname
    return None, tzname


def _skeleton(ident, date, origin, status="unknown", tzname="UTC") -> Flight:
    return Flight(
        ident=ident,
        ident_iata=ident,
        origin=origin or "",
        origin_tz=tzname,
        status=status,
        source="none",
        live=False,
    )


def _summarize(flight: Flight, pred, conn=None, error=None) -> str:
    name = flight.ident_iata or flight.ident
    route = f"{flight.origin}→{flight.destination}" if flight.origin and flight.destination else name
    if error and error.get("code") == "no_data":
        return f"No live data for {name} on {flight.origin or 'the requested date'}."
    if error:
        return error.get("message") or f"Could not look up {name}."
    if flight.status == "too_early":
        return (
            f"{name} on the ticket date is more than 48 hours out; "
            f"live status is not queried yet."
        )
    if flight.status == "unknown":
        return f"No live data for {name}."
    if flight.cancelled:
        return f"{name} has been cancelled."
    if flight.diverted:
        dest = flight.divert_to or "another airport"
        return f"{name} has been diverted to {dest}."
    if flight.status == "landed" and pred.reasons:
        return f"{name} {pred.reasons[0]}."
    if conn and conn.band == "at_risk":
        return f"{name} connection is at risk. {conn.explain}"
    if pred.band == "on_track":
        when = ""
        if pred.pred_out:
            from .times import human_form
            when = f", departing {human_form(pred.pred_out, flight.origin, flight.origin_tz)}"
        return f"{route} is on track{when}."
    if pred.reasons:
        delay = f" expected departure delay {pred.dep_delay_min} min." if pred.dep_delay_min else ""
        return f"{name} is {pred.band.replace('_', ' ')}: {pred.reasons[0]}{delay}"
    return f"{name} status is {flight.status}."


def _next_check(flight: Flight, pred, now: dt.datetime, too_early_at=None):
    origin = flight.origin
    tzname = flight.origin_tz or "UTC"
    if pred.final or flight.status in ("cancelled", "diverted", "landed"):
        return None, None
    if too_early_at is not None:
        delta = too_early_at - now.astimezone(too_early_at.tzinfo)
        minutes = max(0, int(delta.total_seconds() // 60))
        return local_dict(too_early_at.astimezone(UTC), tzname, origin), minutes
    if not flight.sched_out:
        return None, None
    interval = poll_interval(
        flight.sched_out,
        now,
        landed=pred.final,
        sched_arr=flight.sched_in,
        actual_dep=flight.act_out,
    )
    if interval is None:
        return None, None
    nxt = now + dt.timedelta(minutes=interval)
    return local_dict(nxt, tzname, origin), interval


def check_flight(req: CheckRequest | dict, *, fetch=None, adapter=None, now=None, key=None) -> CheckResult:
    if not isinstance(req, CheckRequest):
        req = parse_request(req)
    now = now or req.now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    ident = req.ident
    day = parse_date(req.date)
    origin = req.origin
    tzname = tz_for_iata(origin) if origin else "UTC"

    gate, tzname = too_early_gate(day, now, origin)
    if gate is not None:
        flight = _skeleton(ident, req.date, origin, status="too_early", tzname=tzname)
        pred = predict(flight)
        nxt, minutes = _next_check(flight, pred, now, too_early_at=gate)
        return CheckResult(
            flight=flight,
            inbound=None,
            prediction=pred,
            connection=None,
            calls=[],
            cost_usd_est=0.0,
            ok=True,
            summary=_summarize(flight, pred),
            next_check_local=nxt,
            next_check_in_minutes=minutes,
        )

    adapter = adapter or make_adapter(req, fetch=fetch, key=key)
    error = None
    flight = None
    try:
        if hasattr(adapter, "lookup"):
            flight = adapter.lookup(
                ident, req.date,
                origin=origin, now=now, fetch_inbound=True,
                origin_tz=tzname,
            )
        else:
            flight = None
    except BudgetExceeded:
        raise
    except AeroAPIError as exc:
        error = exc.to_dict()
        error["message"] = redact(exc, key, getattr(adapter, "key", None))
        if exc.snippet:
            error["snippet"] = redact(exc.snippet, key, getattr(adapter, "key", None))
        flight = None
    except CheckError as exc:
        error = exc.to_dict()
        error["message"] = redact(exc, key)
        flight = None

    if flight is None:
        flight = _skeleton(ident, req.date, origin, status="unknown", tzname=tzname)
        if error is None:
            error = {
                "code": "no_data",
                "message": f"AeroAPI returned no flights for {ident} on {req.date}",
            }

    turn = req.min_turn
    if turn is None:
        turn = default_min_turn(flight.aircraft_type, flight.sched_out, flight.sched_in)
    pred = predict(flight, min_turn=turn, prior=req.prior)
    conn = None
    if req.onward and flight.live and not error:
        onward_date = req.onward.date or req.date
        onward_tz = tz_for_iata(req.onward.origin) if req.onward.origin else tzname
        try:
            onward = adapter.lookup(
                req.onward.ident, onward_date,
                origin=req.onward.origin, now=now, fetch_inbound=False,
                origin_tz=onward_tz,
            )
        except (AeroAPIError, CheckError):
            onward = None
        if onward:
            conn = assess(flight, pred, onward, req.onward)

    calls = list(getattr(adapter, "calls", []))
    spent = float(getattr(adapter, "spent", 0.0))
    secrets = secret_values(getattr(adapter, "key", None), key)
    for call in calls:
        if isinstance(call, dict):
            call["path"] = redact(call.get("path", ""), *secrets)
            params = call.get("params") or {}
            call["params"] = {k: redact(v, *secrets) for k, v in params.items()}

    nxt, minutes = _next_check(flight, pred, now)
    ok = error is None and flight.status not in ("unknown",)
    return CheckResult(
        flight=flight,
        inbound=flight.inbound,
        prediction=pred,
        connection=conn,
        calls=calls,
        cost_usd_est=round(spent, 4),
        ok=ok,
        summary=_summarize(flight, pred, conn, error),
        next_check_local=nxt,
        next_check_in_minutes=minutes,
        error=error,
    )
