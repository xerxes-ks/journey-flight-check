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
from .exceptions import AeroAPIError, BudgetExceeded, CheckError, error_dict
from .model import (
    UTC,
    BAND_HUMAN,
    UNKNOWN_RETRY_MIN,
    WINDOW_END_MAX_AHEAD_H,
    CheckRequest,
    CheckResult,
    Flight,
    OnwardRequest,
    Prior,
    parse_date,
)
from .predict import predict
from .sanitize import redact, secret_values
from .times import format_delay, human_form, local_dict, parse_now_maybe
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
    """Skip AeroAPI until the local day could fall inside the ~47h window.

    Exact STD is unknown until a live lookup. Visibility is origin-local
    midnight of the ticket date minus 47h (or scheduled_out minus 47h once
    known). next_check jumps to that instant rather than retrying hourly.
    """
    tzname = tz_for_iata(origin) if origin else "UTC"
    visible = _visible_from(day, tzname)
    if now.astimezone(visible.tzinfo) < visible:
        return visible, tzname
    return None, tzname


def _visible_from(day: dt.date, tzname: str) -> dt.datetime:
    """Earliest instant the local day's 00:00 departure could fall inside now+47h."""
    return _local_midnight(day, tzname) - dt.timedelta(hours=WINDOW_END_MAX_AHEAD_H)


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


def _band_word(band: str) -> str:
    return BAND_HUMAN.get(band, (band or "unknown").replace("_", " "))


def _ident_route(flight: Flight) -> str:
    name = flight.ident_iata or flight.ident
    if flight.origin and flight.destination:
        return f"{name} {flight.origin}→{flight.destination}"
    return name


def _with_connection(sentence: str, conn=None, onward_ident: Optional[str] = None) -> str:
    sentence = (sentence or "").rstrip()
    if sentence and not sentence.endswith("."):
        sentence += "."
    if conn:
        name = conn.onward_ident or onward_ident or "the onward flight"
        if conn.band == "at_risk":
            if conn.slack_min < 0:
                gap = f"{format_delay(-conn.slack_min)} short"
            else:
                gap = f"{format_delay(conn.slack_min)} spare"
            extra = f" The connection to {name} is at risk ({gap})"
            reason = (conn.explain or "").split(".")[0].strip()
            if reason:
                extra += f": {reason[0].lower() + reason[1:]}"
            sentence += extra + "."
        else:
            sentence += f" The connection to {name} is {_band_word(conn.band)}."
    elif onward_ident:
        sentence += f" The connection to {onward_ident} has not been scored yet."
    return sentence


def _summarize(
    flight: Flight,
    pred,
    conn=None,
    error=None,
    next_check_local=None,
    onward_ident: Optional[str] = None,
    ticket_date: Optional[str] = None,
) -> str:
    name = _ident_route(flight)
    band_word = _band_word(pred.band)

    if flight.status == "too_early":
        open_at = (next_check_local or {}).get("human")
        msg = f"{name} is more than 48 hours from departure."
        if open_at:
            msg += f" Live checks open {open_at}."
        else:
            msg += " Live status is not queried yet."
        return _with_connection(msg, conn, onward_ident)

    if error and error.get("code") in ("no_data", "flight_not_found"):
        when = ticket_date or flight.origin or "the requested date"
        return _with_connection(f"No live data for {name} on {when}.", conn, onward_ident)
    if error and flight.status in ("unknown",):
        return _with_connection(error.get("message") or f"Could not look up {name}.", conn, onward_ident)
    if error and not flight.live:
        return _with_connection(error.get("message") or f"Could not look up {name}.", conn, onward_ident)

    if flight.status == "unknown":
        return _with_connection(f"No live data for {name}.", conn, onward_ident)
    if flight.cancelled:
        return _with_connection(f"{name} has been cancelled.", conn, onward_ident)
    if flight.diverted:
        dest = flight.divert_to or "another airport"
        return _with_connection(f"{name} has been diverted to {dest}.", conn, onward_ident)

    if flight.status == "landed":
        when = (
            human_form(flight.act_in, flight.destination, flight.dest_tz)
            if flight.act_in
            else "on arrival"
        )
        bits = []
        if flight.terminal_destination:
            bits.append(f"terminal {flight.terminal_destination}")
        if flight.gate_destination:
            bits.append(f"gate {flight.gate_destination}")
        if flight.baggage_claim:
            bits.append(f"baggage belt {flight.baggage_claim}")
        at = f" at {', '.join(bits)}" if bits else ""
        return _with_connection(f"{name} landed {when}{at}.", conn, onward_ident)

    if flight.status == "active" or (flight.act_out and not flight.act_in):
        land = pred.pred_in or flight.est_in
        extra = ""
        if land:
            extra = f", estimated arrival {human_form(land, flight.destination, flight.dest_tz)}"
        return _with_connection(f"{name} is {band_word}, in the air{extra}.", conn, onward_ident)

    if pred.band == "on_track":
        when = ""
        if pred.pred_out:
            when = f", departing {human_form(pred.pred_out, flight.origin, flight.origin_tz)}"
        return _with_connection(f"{name} is on track{when}.", conn, onward_ident)

    reason = (pred.reasons or [""])[0].rstrip(".")
    if reason:
        reason = reason[0].upper() + reason[1:]
    msg = f"{name} is {band_word}."
    if reason:
        msg += f" {reason}."
    if pred.dep_delay_min:
        msg += f" Expected departure delay {format_delay(pred.dep_delay_min)}."
    return _with_connection(msg, conn, onward_ident)


def _pack_next(when: dt.datetime, now: dt.datetime, tzname: str, origin: str):
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = when - now.astimezone(when.tzinfo)
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        minutes = 1
        when = now.astimezone(when.tzinfo) + dt.timedelta(minutes=1)
    return local_dict(when.astimezone(UTC), tzname, origin), minutes


def _next_check(
    flight: Flight,
    pred,
    now: dt.datetime,
    *,
    too_early_at=None,
    ticket_day: Optional[dt.date] = None,
):
    origin = flight.origin or ""
    tzname = flight.origin_tz or (tz_for_iata(origin) if origin else "UTC") or "UTC"
    if pred.final or flight.status in ("cancelled", "diverted", "landed"):
        return None, None
    if too_early_at is not None:
        return _pack_next(too_early_at, now, tzname, origin)
    if flight.sched_out:
        visible = flight.sched_out - dt.timedelta(hours=WINDOW_END_MAX_AHEAD_H)
        if now.astimezone(flight.sched_out.tzinfo) < visible:
            return _pack_next(visible, now, tzname, origin)
        interval = poll_interval(
            flight.sched_out,
            now,
            landed=pred.final,
            sched_arr=flight.sched_in,
            actual_dep=flight.act_out,
            est_arr=flight.est_in or pred.pred_in,
        )
        if interval is None:
            return _pack_next(now + dt.timedelta(minutes=UNKNOWN_RETRY_MIN), now, tzname, origin)
        return _pack_next(now + dt.timedelta(minutes=interval), now, tzname, origin)
    # unknown / no_data and no STD yet: jump to visibility, never hourly before it.
    if ticket_day is not None:
        visible = _visible_from(ticket_day, tzname)
        if now.astimezone(visible.tzinfo) < visible:
            return _pack_next(visible, now, tzname, origin)
    nxt = now + dt.timedelta(minutes=UNKNOWN_RETRY_MIN)
    return _pack_next(nxt, now, tzname, origin)


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

    onward_ident = req.onward.ident if req.onward else None
    gate, tzname = too_early_gate(day, now, origin)
    if gate is not None:
        flight = _skeleton(ident, req.date, origin, status="too_early", tzname=tzname)
        pred = predict(flight)
        nxt, minutes = _next_check(flight, pred, now, too_early_at=gate, ticket_day=day)
        return CheckResult(
            flight=flight,
            inbound=None,
            prediction=pred,
            connection=None,
            calls=[],
            cost_usd_est=0.0,
            ok=True,
            summary=_summarize(
                flight, pred,
                next_check_local=nxt,
                onward_ident=onward_ident,
                ticket_date=req.date,
            ),
            next_check_local=nxt,
            next_check_in_minutes=minutes,
            checked_at=now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
            error = error_dict(
                "flight_not_found",
                f"AeroAPI returned no flights for {ident} on {req.date}",
            )

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

    nxt, minutes = _next_check(flight, pred, now, ticket_day=day)
    ok = error is None and flight.status not in ("unknown",)
    return CheckResult(
        flight=flight,
        inbound=flight.inbound,
        prediction=pred,
        connection=conn,
        calls=calls,
        cost_usd_est=round(spent, 4),
        ok=ok,
        summary=_summarize(
            flight, pred, conn, error,
            next_check_local=nxt,
            onward_ident=onward_ident,
            ticket_date=req.date,
        ),
        next_check_local=nxt,
        next_check_in_minutes=minutes,
        checked_at=now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        error=error,
    )
