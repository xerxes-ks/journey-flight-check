"""Orchestrator: adapter -> prediction -> connection scoring. Stateless."""

from __future__ import annotations

import datetime as dt
import os
from typing import Optional
from zoneinfo import ZoneInfo

from .adapters.aeroapi import AeroAPIAdapter
from .adapters.schedule import ScheduleAdapter
from .airports import tz_for_iata
from .cadence import RETRYABLE_RETRY, essential_plan, inbound_for_checkpoint, poll_interval
from .connections import assess
from .exceptions import AeroAPIError, BudgetExceeded, CheckError, error_dict, retryable_for
from .model import (
    UTC,
    BAND_HUMAN,
    DEFAULT_ALERT_DELAY_MIN,
    DEFAULT_BUDGET_USD,
    DEFAULT_COST_PER_CALL,
    DEFAULT_MCT,
    DEFAULT_SELF_TRANSFER_MCT,
    TOO_EARLY_HOURS,
    UNKNOWN_RETRY_MIN,
    WINDOW_END_MAX_AHEAD_H,
    CheckRequest,
    CheckResult,
    Flight,
    OnwardRequest,
    Prior,
    mins,
    parse_date,
)
from .money import MAX_BUDGET_USD, request_budget
from .phase import phase_for
from .predict import predict
from .sanitize import redact, secret_values
from .times import as_local, format_delay, human_form, local_dict, parse_now_maybe
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
    separate = _as_bool(data.get("separate_ticket"))
    bags = _as_bool(data.get("bags_recheck") if "bags_recheck" in data else data.get("bag_recheck"))
    raw_mct = data.get("min_connect_min")
    if raw_mct is None:
        raw_mct = data.get("min_connection_min")
    if raw_mct is None:
        raw_mct = data.get("mct")
    explicit = raw_mct not in (None, "")
    if explicit:
        mct = int(raw_mct)
    elif separate or bags:
        mct = DEFAULT_SELF_TRANSFER_MCT
    else:
        mct = DEFAULT_MCT
    return OnwardRequest(
        ident=str(ident),
        date=data.get("date") or data.get("onward_date"),
        origin=data.get("origin") or data.get("onward_origin"),
        min_connection_min=mct,
        terminal_change=_as_bool(data.get("terminal_change")),
        terminal_change_min=_as_int(data.get("terminal_change_min"), 20),
        bag_recheck=bags,
        checkin_close_min=_as_int(data.get("checkin_close_min"), 45),
        separate_ticket=separate,
        min_connect_explicit=explicit,
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
    cadence = str(data.get("cadence") or "essential").strip().lower()
    if cadence not in ("essential", "dense"):
        cadence = "essential"
    return CheckRequest(
        ident=str(ident),
        date=str(date)[:10],
        origin=(str(data["origin"]).upper() if data.get("origin") else None),
        onward=parse_onward(onward if isinstance(onward, dict) else None),
        budget_usd=request_budget(data, DEFAULT_BUDGET_USD, max_value=MAX_BUDGET_USD),
        min_turn=min_turn,
        inbound_window_h=_as_float(data.get("inbound_window_h"), 6),
        prior=prior,
        cost_per_call=DEFAULT_COST_PER_CALL,
        now=parse_now_maybe(data.get("now")),
        cadence=cadence,
        alert_delay_min=_as_int(data.get("alert_delay_min"), DEFAULT_ALERT_DELAY_MIN),
        previous_checkpoint=(str(data["previous_checkpoint"]) if data.get("previous_checkpoint") else None),
        previous_phase=(str(data["previous_phase"]) if data.get("previous_phase") else None),
        attempts=_as_int(data.get("attempts"), 0),
        previous_gate=(str(data["previous_gate"]) if data.get("previous_gate") else None),
    )


def make_adapter(req: CheckRequest, fetch=None, key=None, spend_guard=None):
    api_key = key if key is not None else os.environ.get("FLIGHTAWARE_AEROAPI_KEY")
    kwargs = dict(
        budget_usd=req.budget_usd,
        inbound_window_h=req.inbound_window_h,
        cost_per_call=DEFAULT_COST_PER_CALL,
        spend_guard=spend_guard,
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
    """Skip AeroAPI until T−24h. T is origin-local midnight of the ticket date
    until scheduled_out is known; then checkpoints recompute from STD.
    """
    tzname = tz_for_iata(origin) if origin else "UTC"
    visible = _visible_from(day, tzname)
    if now.astimezone(visible.tzinfo) < visible:
        return visible, tzname
    return None, tzname


def _visible_from(day: dt.date, tzname: str) -> dt.datetime:
    """First allowed lookup until STD is known: origin-local noon of the previous day.

    Using midnight of the ticket date as T made a wasted call at 00:00 local,
    before T−24h of a morning departure. Noon of D−1 is after T−24h for
    early flights and still before T−6h.
    """
    noon = _local_midnight(day, tzname) + dt.timedelta(hours=12)
    return noon - dt.timedelta(hours=TOO_EARLY_HOURS)


def _onward_visible(onward: OnwardRequest, now: dt.datetime, tzname: str) -> bool:
    try:
        day = parse_date(onward.date)
    except Exception:
        return True
    tz = tz_for_iata(onward.origin) if onward.origin else tzname
    start = _local_midnight(day, tz)
    horizon = now.astimezone(start.tzinfo) + dt.timedelta(hours=WINDOW_END_MAX_AHEAD_H)
    return start <= horizon


def _should_fetch_onward(req: CheckRequest, flight: Flight, pred, now, tzname, checkpoint) -> bool:
    if not req.onward or not flight.live:
        return False
    if not _onward_visible(req.onward, now, tzname):
        return False
    if checkpoint == "t_minus_3h":
        return True
    if flight.act_on or flight.act_in:
        return True
    if pred.band in ("watch", "at_risk"):
        return True
    return False


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
    dest = flight.destination
    if (
        flight.diverted
        and flight.divert_to
        and dest
        and dest.upper() == flight.divert_to.upper()
    ):
        dest = ""
    if flight.origin and dest:
        return f"{name} {flight.origin}→{dest}"
    if flight.origin:
        return f"{name} {flight.origin}"
    return name


def _connection_reason(explain: str) -> str:
    """First clause of a connection explain, never split on 'incl.'."""
    text = (explain or "").replace("including", "including").strip()
    if not text:
        return ""
    # Split on '. ' so abbreviations like 'incl.' cannot truncate the sentence.
    parts = text.split(". ")
    return parts[0].rstrip(".")


def _impact_lead(conn=None, onward_ident: Optional[str] = None) -> str:
    if not conn:
        return ""
    name = conn.onward_ident or onward_ident or "the onward flight"
    if conn.band == "at_risk":
        if conn.slack_min < 0:
            gap = f"{format_delay(-conn.slack_min)} short"
        else:
            gap = f"{format_delay(conn.slack_min)} spare"
        return f"Connection to {name} is at risk ({gap})."
    if conn.band == "watch":
        return f"Connection to {name} is worth watching."
    return f"Connection to {name} is safe."


def _with_connection(sentence: str, conn=None, onward_ident: Optional[str] = None) -> str:
    sentence = (sentence or "").rstrip()
    if sentence and not sentence.endswith("."):
        sentence += "."
    lead = _impact_lead(conn, onward_ident)
    if lead:
        return f"{lead} {sentence}".strip()
    return sentence


def _gate_retime_notes(flight: Flight, previous_gate: Optional[str] = None) -> list[str]:
    notes = []
    if (
        previous_gate
        and flight.gate_origin
        and previous_gate != flight.gate_origin
    ):
        notes.append(f"departure gate is now {flight.gate_origin} (was {previous_gate})")
    if flight.est_out and flight.sched_out:
        shift = mins(flight.est_out - flight.sched_out)
        if abs(shift) >= 30:
            a = as_local(flight.sched_out, flight.origin_tz)
            b = as_local(flight.est_out, flight.origin_tz)
            label = flight.origin or "local"
            notes.append(f"moved from {a:%H:%M} to {b:%H:%M} ({label} time)")
    return notes


def _summarize(
    flight: Flight,
    pred,
    conn=None,
    error=None,
    next_check_local=None,
    onward_ident: Optional[str] = None,
    ticket_date: Optional[str] = None,
    phase: str = "scheduled",
    previous_gate: Optional[str] = None,
    warning=None,
) -> str:
    name = _ident_route(flight)
    band_word = _band_word(pred.band)

    if flight.status == "too_early":
        open_at = (next_check_local or {}).get("human")
        msg = f"{name} is more than 24 hours from departure."
        if open_at:
            msg += f" Live checks open {open_at}."
        else:
            msg += " Live status is not queried yet."
        return _with_connection(msg, conn, onward_ident)

    if error and error.get("code") == "origin_mismatch":
        return _with_connection(
            error.get("message") or f"No matching {name} departure from the requested origin.",
            conn,
            onward_ident,
        )
    if error and error.get("code") in ("no_data", "flight_not_found", "not_found"):
        return _with_connection("flight not found.", conn, onward_ident)
    if error and (flight.status in ("unknown",) or not flight.live):
        return _with_connection(f"Live status is temporarily unavailable for {name}.", conn, onward_ident)

    if flight.status == "unknown":
        return _with_connection(f"No live data for {name}.", conn, onward_ident)
    if flight.cancelled:
        if pred.final:
            return _with_connection(f"{name} is confirmed cancelled.", conn, onward_ident)
        return _with_connection(f"{name} shows as cancelled. Rechecking in 30 min.", conn, onward_ident)
    if flight.diverted:
        dest = flight.divert_to or "another airport"
        return _with_connection(f"{name} has been diverted to {dest}.", conn, onward_ident)

    notes = _gate_retime_notes(flight, previous_gate)

    def _finish(msg: str) -> str:
        msg = (msg or "").rstrip()
        if msg and not msg.endswith("."):
            msg += "."
        for note in notes:
            if note and note.lower() not in msg.lower():
                cap = note[0].upper() + note[1:] if note else note
                msg = msg.rstrip(".") + ". " + cap + "."
        if warning and warning.get("code") == "budget_exceeded":
            if "skipped" not in msg.lower():
                msg = msg.rstrip(".") + ". Some live data was skipped to stay within budget."
        return _with_connection(msg, conn, onward_ident)

    if phase == "at_arrival_gate" or flight.status == "landed":
        when = (
            human_form(flight.act_in or flight.act_on, flight.destination, flight.dest_tz)
            if (flight.act_in or flight.act_on)
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
        return _finish(f"{name} landed {when}{at}.")

    if phase == "airborne" or (flight.act_off and not flight.act_on):
        land = pred.pred_in or flight.est_in
        extra = ""
        if land:
            extra = f", estimated arrival {human_form(land, flight.destination, flight.dest_tz)}"
        return _finish(f"{name} is {band_word}, in the air{extra}.")

    if phase == "taxiing":
        return _finish(f"{name} is {band_word}, taxiing.")

    if phase == "at_gate_late":
        delay = pred.dep_delay_min or 0
        return _finish(
            f"{name} is {band_word}, departure delayed {format_delay(delay)} past scheduled."
        )

    if pred.band == "on_track":
        when = ""
        if pred.pred_out:
            when = f", departing {human_form(pred.pred_out, flight.origin, flight.origin_tz)}"
        return _finish(f"{name} is on track{when}.")

    reason = (pred.reasons or [""])[0].rstrip(".")
    skip_reason = False
    if reason:
        low = reason.lower()
        if any(n.lower() in low or low.startswith("airline now") or "moved from" in low for n in notes):
            skip_reason = True
        elif low.startswith("departure delayed") and pred.dep_delay_min:
            skip_reason = True
    if reason and not skip_reason:
        reason = reason[0].upper() + reason[1:]
    msg = f"{name} is {band_word}."
    if reason and not skip_reason:
        msg += f" {reason}."
    if pred.dep_delay_min:
        msg += f" Expected departure delay {format_delay(pred.dep_delay_min)}."
    return _finish(msg)


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
    cadence: str = "essential",
    previous_checkpoint: Optional[str] = None,
    previous_phase: Optional[str] = None,
    attempts: int = 0,
    retryable_error: bool = False,
):
    origin = flight.origin or ""
    tzname = flight.origin_tz or (tz_for_iata(origin) if origin else "UTC") or "UTC"
    if too_early_at is not None:
        packed = _pack_next(too_early_at, now, tzname, origin)
        return packed[0], packed[1], "t_minus_24h", 0
    if retryable_error:
        packed = _pack_next(now + RETRYABLE_RETRY, now, tzname, origin)
        cp = previous_checkpoint
        if flight.sched_out:
            probe = essential_plan(
                now=now, std=flight.sched_out, sta=flight.sched_in,
                expected_off=flight.est_out or flight.sched_out,
                expected_arr=flight.est_in or flight.sched_in,
                actual_off=flight.act_off, actual_on=flight.act_on,
                actual_in=flight.act_in,
                previous_checkpoint=previous_checkpoint,
                previous_phase=previous_phase,
                attempts=attempts,
            )
            cp = probe.checkpoint
        return packed[0], packed[1], cp or "t_minus_24h", attempts
    if cadence == "dense":
        if pred.final or flight.status in ("diverted", "landed"):
            return None, None, "done", attempts
        if flight.cancelled and not pred.final:
            packed = _pack_next(now + dt.timedelta(minutes=30), now, tzname, origin)
            return packed[0], packed[1], previous_checkpoint or "t_minus_24h", 1
        if flight.cancelled and pred.final:
            return None, None, "done", attempts
        if flight.sched_out:
            interval = poll_interval(
                flight.sched_out,
                now,
                landed=pred.final,
                sched_arr=flight.sched_in,
                actual_dep=flight.act_off or flight.act_out,
                est_arr=flight.est_in or pred.pred_in,
            )
            if interval is None:
                packed = _pack_next(now + dt.timedelta(minutes=UNKNOWN_RETRY_MIN), now, tzname, origin)
                return packed[0], packed[1], "t_minus_24h", attempts
            packed = _pack_next(now + dt.timedelta(minutes=interval), now, tzname, origin)
            return packed[0], packed[1], "t_minus_1h", attempts
        if ticket_day is not None:
            visible = _visible_from(ticket_day, tzname)
            if now.astimezone(visible.tzinfo) < visible:
                packed = _pack_next(visible, now, tzname, origin)
                return packed[0], packed[1], "t_minus_24h", 0
        packed = _pack_next(now + dt.timedelta(minutes=UNKNOWN_RETRY_MIN), now, tzname, origin)
        return packed[0], packed[1], "t_minus_24h", attempts

    expected_off = pred.pred_out or flight.est_out or flight.sched_out
    expected_arr = pred.pred_in or flight.est_in or flight.sched_in
    plan = essential_plan(
        now=now,
        std=flight.sched_out,
        sta=flight.sched_in,
        expected_off=expected_off,
        expected_arr=expected_arr,
        actual_off=flight.act_off,
        actual_on=flight.act_on,
        actual_in=flight.act_in,
        cancelled=flight.cancelled,
        diverted=flight.diverted,
        previous_checkpoint=previous_checkpoint,
        previous_phase=previous_phase,
        attempts=attempts,
    )
    if plan.final or plan.next_at is None:
        if plan.final:
            return None, None, plan.checkpoint, plan.attempts
        if ticket_day is not None:
            visible = _visible_from(ticket_day, tzname)
            if now.astimezone(visible.tzinfo) < visible:
                packed = _pack_next(visible, now, tzname, origin)
                return packed[0], packed[1], plan.checkpoint, plan.attempts
        packed = _pack_next(now + dt.timedelta(minutes=UNKNOWN_RETRY_MIN), now, tzname, origin)
        return packed[0], packed[1], plan.checkpoint, plan.attempts
    packed = _pack_next(plan.next_at, now, tzname, origin)
    return packed[0], packed[1], plan.checkpoint, plan.attempts


def _onward_reason(req: CheckRequest, *, looked_up: bool, conn) -> Optional[str]:
    if not req.onward:
        return None
    if conn is not None:
        return None
    return "not_found" if looked_up else "untracked"


def check_flight(req: CheckRequest | dict, *, fetch=None, adapter=None, now=None, key=None, spend_guard=None) -> CheckResult:
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
        # too_early makes no AeroAPI call, so origin cannot be matched against
        # a live leg. origin_mismatch is evaluated on the first live lookup.
        flight = _skeleton(ident, req.date, origin, status="too_early", tzname=tzname)
        pred = predict(flight, alert_delay_min=req.alert_delay_min)
        nxt, minutes, checkpoint, attempts = _next_check(
            flight, pred, now, too_early_at=gate, ticket_day=day, cadence=req.cadence,
        )
        return CheckResult(
            flight=flight,
            inbound=None,
            prediction=pred,
            connection=None,
            connection_reason=_onward_reason(req, looked_up=False, conn=None),
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
            checkpoint=checkpoint,
            phase="scheduled",
            attempts=attempts,
        )

    adapter = adapter or make_adapter(req, fetch=fetch, key=key, spend_guard=spend_guard)
    if spend_guard is not None:
        adapter.spend_guard = spend_guard
    error = None
    warning = None
    flight = None
    try:
        if hasattr(adapter, "lookup"):
            flight = adapter.lookup(
                ident, req.date,
                origin=origin, now=now, fetch_inbound=False,
                origin_tz=tzname,
            )
        else:
            flight = None
    except BudgetExceeded as exc:
        error = error_dict("budget_exceeded", "The live-data budget is too small for a lookup.")
        flight = None
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
            error = error_dict("flight_not_found", "flight not found")

    want_inbound = False
    probe_checkpoint = None
    if flight.live and flight.sched_out:
        if req.cadence == "dense":
            want_inbound = True
        else:
            probe = essential_plan(
                now=now,
                std=flight.sched_out,
                sta=flight.sched_in,
                expected_off=flight.est_out or flight.sched_out,
                expected_arr=flight.est_in or flight.sched_in,
                actual_off=flight.act_off,
                actual_on=flight.act_on,
                actual_in=flight.act_in,
                cancelled=flight.cancelled,
                diverted=flight.diverted,
                previous_checkpoint=req.previous_checkpoint,
                previous_phase=req.previous_phase,
                attempts=req.attempts,
            )
            probe_checkpoint = probe.checkpoint
            want_inbound = inbound_for_checkpoint(probe.checkpoint)
    if want_inbound and hasattr(adapter, "attach_inbound") and flight.live:
        try:
            adapter.attach_inbound(flight, now=now)
        except BudgetExceeded as exc:
            warning = error_dict("budget_exceeded", redact(exc, key))
        except (AeroAPIError, CheckError):
            pass

    turn = req.min_turn
    if turn is None:
        turn = default_min_turn(flight.aircraft_type, flight.sched_out, flight.sched_in)
    confirm_cancel = flight.cancelled and (
        req.attempts >= 1 or req.previous_checkpoint == "done"
    )
    pred = predict(
        flight,
        min_turn=turn,
        prior=req.prior,
        alert_delay_min=req.alert_delay_min,
        confirm_cancel=confirm_cancel,
        now=now,
    )
    conn = None
    onward_looked_up = False
    if req.onward and flight.live and not error and _should_fetch_onward(
        req, flight, pred, now, tzname, probe_checkpoint,
    ):
        onward_looked_up = True
        onward_date = req.onward.date or req.date
        onward_tz = tz_for_iata(req.onward.origin) if req.onward.origin else tzname
        try:
            onward = adapter.lookup(
                req.onward.ident, onward_date,
                origin=req.onward.origin, now=now, fetch_inbound=False,
                origin_tz=onward_tz,
            )
        except BudgetExceeded as exc:
            warning = warning or error_dict("budget_exceeded", redact(exc, key))
            onward = None
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

    retryable_error = bool(error and retryable_for(error.get("code") or ""))
    nxt, minutes, checkpoint, attempts = _next_check(
        flight, pred, now,
        ticket_day=day,
        cadence=req.cadence,
        previous_checkpoint=req.previous_checkpoint,
        previous_phase=req.previous_phase,
        attempts=req.attempts,
        retryable_error=retryable_error,
    )
    pred.final = checkpoint == "done"
    phase = phase_for(flight, now)
    ok = error is None and flight.status not in ("unknown",)
    impact = conn.band if conn is not None else None
    return CheckResult(
        flight=flight,
        inbound=flight.inbound,
        prediction=pred,
        connection=conn,
        connection_reason=_onward_reason(req, looked_up=onward_looked_up, conn=conn),
        calls=calls,
        cost_usd_est=round(spent, 4),
        ok=ok,
        summary=_summarize(
            flight, pred, conn, error,
            next_check_local=nxt,
            onward_ident=onward_ident,
            ticket_date=req.date,
            phase=phase,
            previous_gate=req.previous_gate,
            warning=warning,
        ),
        next_check_local=nxt,
        next_check_in_minutes=minutes,
        checked_at=now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        error=error,
        warning=warning,
        checkpoint=checkpoint,
        phase=phase,
        impact_band=impact,
        attempts=attempts,
    )
