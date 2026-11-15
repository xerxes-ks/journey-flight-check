"""Orchestrator: adapter -> prediction -> connection scoring. Stateless."""

from __future__ import annotations

import datetime as dt
import os
from typing import Optional

from .adapters.aeroapi import AeroAPIAdapter
from .adapters.schedule import ScheduleAdapter
from .connections import assess
from .model import (
    UTC,
    CheckRequest,
    CheckResult,
    OnwardRequest,
    Prior,
    parse_date,
)
from .predict import predict
from .sanitize import redact, secret_values


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
    ident = data.get("ident") or data.get("onward")
    if not ident:
        return None
    return OnwardRequest(
        ident=str(ident),
        date=data.get("date") or data.get("onward_date"),
        origin=data.get("origin") or data.get("onward_origin"),
        min_connection_min=_as_int(data.get("min_connection_min") or data.get("mct"), 45),
        terminal_change=_as_bool(data.get("terminal_change")),
        terminal_change_min=_as_int(data.get("terminal_change_min"), 20),
        bag_recheck=_as_bool(data.get("bag_recheck")),
        checkin_close_min=_as_int(data.get("checkin_close_min"), 45),
        separate_ticket=_as_bool(data.get("separate_ticket")),
    )


def parse_request(data: dict) -> CheckRequest:
    ident = data.get("ident") or data.get("flight")
    date = data.get("date")
    if not ident or not date:
        raise ValueError("ident and date are required")
    parse_date(date)  # validate
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
        onward = {"ident": onward, **{k: data[k] for k in (
            "onward_date", "onward_origin", "min_connection_min", "mct",
            "terminal_change", "terminal_change_min", "bag_recheck",
            "checkin_close_min", "separate_ticket",
        ) if k in data}}
    return CheckRequest(
        ident=str(ident),
        date=str(date)[:10],
        origin=data.get("origin") or None,
        onward=parse_onward(onward if isinstance(onward, dict) else None),
        budget_usd=_as_float(data.get("budget_usd") or data.get("budget"), 0.05),
        min_turn=_as_int(data.get("min_turn"), 30),
        inbound_window_h=_as_float(data.get("inbound_window_h"), 6),
        prior=prior,
        cost_per_call=_as_float(data.get("cost_per_call"), 0.005),
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


def check_flight(req: CheckRequest | dict, *, fetch=None, adapter=None, now=None, key=None) -> CheckResult:
    if not isinstance(req, CheckRequest):
        req = parse_request(req)
    now = now or dt.datetime.now(UTC)
    adapter = adapter or make_adapter(req, fetch=fetch, key=key)
    lookup = getattr(adapter, "lookup")
    flight = lookup(
        req.ident, req.date,
        origin=req.origin, now=now, fetch_inbound=True, min_turn=req.min_turn,
    )
    pred = predict(flight, min_turn=req.min_turn, prior=req.prior)
    conn = None
    if req.onward:
        onward_date = req.onward.date or req.date
        onward = lookup(
            req.onward.ident, onward_date,
            origin=req.onward.origin, now=now, fetch_inbound=False,
        )
        conn = assess(flight, pred, onward, req.onward)
    calls = list(getattr(adapter, "calls", []))
    spent = float(getattr(adapter, "spent", 0.0))
    secrets = secret_values(getattr(adapter, "key", None), key)
    for call in calls:
        if isinstance(call, dict):
            call["path"] = redact(call.get("path", ""), *secrets)
            params = call.get("params") or {}
            call["params"] = {k: redact(v, *secrets) for k, v in params.items()}
    return CheckResult(
        flight=flight,
        inbound=flight.inbound,
        prediction=pred,
        connection=conn,
        calls=calls,
        cost_usd_est=round(spent, 4),
    )
