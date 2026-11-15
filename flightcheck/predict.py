"""Delay prediction for one flight. Deterministic: every number has a reason.

pred_dep_delay = max(airline estimate, inbound propagation, historical prior)
  inbound propagation = max(0, inbound ETA at gate + min turn - STD)
pred_arr_delay = pred_dep_delay - schedule padding (prior dep median - arr median, 0..30 min)
Once airborne, the live arrival estimate wins. Landed = actual times.
"""

from __future__ import annotations

import datetime as dt

from .model import Flight, Prediction, Prior, mins

ON_TRACK_MAX, WATCH_MAX = 15, 45


def band_for(arr_delay: int) -> str:
    if arr_delay <= ON_TRACK_MAX:
        return "on_track"
    if arr_delay <= WATCH_MAX:
        return "watch"
    return "at_risk"


def hhmm(t: dt.datetime, tz=None) -> str:
    return t.astimezone(tz).strftime("%H:%M") if tz else t.strftime("%H:%M")


def predict(flight: Flight, *, min_turn: int = 30, prior: Prior | None = None) -> Prediction:
    std, sta = flight.sched_out, flight.sched_in
    pr = prior or Prior()
    origin_tz = flight.origin_tz
    dest_tz = flight.dest_tz

    def pack(dd, ad, band, reasons, **kw) -> Prediction:
        pred_out = (std + dt.timedelta(minutes=dd)) if std else None
        pred_in = (sta + dt.timedelta(minutes=ad)) if sta else None
        return Prediction(
            dd, ad, pred_out, pred_in, band, reasons,
            origin_tz=origin_tz, dest_tz=dest_tz, **kw,
        )

    if flight.cancelled:
        return pack(0, 0, "at_risk", ["cancelled by the airline"])
    if flight.diverted:
        where = flight.divert_to or "another airport"
        return pack(0, 0, "at_risk", [f"diverted to {where}"])
    if flight.act_in and sta:
        dd = mins(flight.act_out - std) if flight.act_out and std else 0
        ad = mins(flight.act_in - sta)
        return pack(dd, ad, band_for(ad), [f"landed {hhmm(flight.act_in, sta.tzinfo)}"], final=True)

    if not std or not sta or flight.source == "schedule" or not flight.live:
        dd = pr.dep_median if pr.n else 0
        padding = max(0, min(30, pr.dep_median - pr.arr_median)) if pr.n else 0
        ad = max(dd - padding, pr.arr_median) if pr.n else 0
        why = (
            [f"no live feed; usual arrival delay {pr.arr_median:+d} min (n={pr.n})"]
            if pr.n
            else ["no live feed; timetable only"]
        )
        return pack(dd, ad, band_for(ad), why, schedule_only=True)

    padding = max(0, min(30, pr.dep_median - pr.arr_median)) if pr.n else 0
    cands = []
    airline_dd = None
    if flight.act_out:
        dd = mins(flight.act_out - std)
        cands.append((dd, f"left the gate at {hhmm(flight.act_out, std.tzinfo)}"))
    else:
        if flight.est_out:
            airline_dd = mins(flight.est_out - std)
            cands.append((airline_dd, f"airline now shows departure {hhmm(flight.est_out, std.tzinfo)}"))
        if flight.inbound and flight.inbound.est_arr:
            ready = flight.inbound.est_arr + dt.timedelta(minutes=min_turn)
            prop = max(0, mins(ready - std))
            ident = flight.inbound.ident or "inbound"
            cands.append((
                prop,
                f"incoming aircraft ({ident}) reaches the gate {hhmm(flight.inbound.est_arr, std.tzinfo)}; "
                f"with a {min_turn}-min turn the earliest departure is {hhmm(ready, std.tzinfo)}",
            ))
        if pr.n:
            cands.append((pr.dep_median, f"this flight usually leaves {pr.dep_median:+d} min (n={pr.n})"))
    dd, main = max(cands, key=lambda c: c[0]) if cands else (0, "no change reported")

    if flight.est_in and (flight.act_out or flight.status == "active"):
        ad = mins(flight.est_in - sta)
        reasons = [f"in the air, estimated arrival {hhmm(flight.est_in, sta.tzinfo)}"]
    else:
        ad = dd - padding
        if flight.est_in:
            ad = max(ad, mins(flight.est_in - sta))
        reasons = [main]

    p = pack(dd, ad, band_for(ad), reasons)
    p.airline_dep_delay = airline_dd
    return p
