"""Delay prediction for one flight. Deterministic: every number has a reason.

pred_dep_delay = max(airline estimate, inbound propagation, historical prior)
  inbound propagation = max(0, inbound ETA at gate + min turn - STD)
pred_arr_delay = pred_dep_delay - schedule padding (prior dep median - arr median, 0..30 min)
Once airborne, the live arrival estimate wins. Landed = actual times.
Human reasons use origin/destination local airport time, never UTC.
"""

from __future__ import annotations

import datetime as dt

from .model import Flight, Prediction, Prior, mins
from .times import clock

ON_TRACK_MAX, WATCH_MAX = 15, 45


def band_for(arr_delay: int) -> str:
    if arr_delay <= ON_TRACK_MAX:
        return "on_track"
    if arr_delay <= WATCH_MAX:
        return "watch"
    return "at_risk"


def predict(flight: Flight, *, min_turn: int = 35, prior: Prior | None = None) -> Prediction:
    std, sta = flight.sched_out, flight.sched_in
    pr = prior or Prior()
    origin_tz, dest_tz = flight.origin_tz, flight.dest_tz
    o, d = flight.origin, flight.destination

    def pack(dd, ad, band, reasons, **kw) -> Prediction:
        pred_out = (std + dt.timedelta(minutes=dd)) if std else None
        pred_in = (sta + dt.timedelta(minutes=ad)) if sta else None
        return Prediction(
            dd, ad, pred_out, pred_in, band, reasons,
            origin_tz=origin_tz, dest_tz=dest_tz, origin=o, destination=d, **kw,
        )

    if flight.status == "too_early":
        return pack(0, 0, "unknown", ["more than 48 hours before departure; live data is not queried yet"])
    if flight.cancelled:
        return pack(0, 0, "at_risk", ["cancelled by the airline"], final=True)
    if flight.diverted:
        where = flight.divert_to or "another airport"
        return pack(0, 0, "at_risk", [f"diverted to {where}"], final=True)
    if flight.status == "unknown" or (not flight.live and flight.source != "aeroapi"):
        if not std or not sta:
            return pack(0, 0, "unknown", ["no live data for this flight"], schedule_only=True)
    if flight.act_in and sta:
        dd = mins(flight.act_out - std) if flight.act_out and std else 0
        ad = mins(flight.act_in - sta)
        return pack(
            dd, ad, band_for(ad),
            [f"landed {clock(flight.act_in, dest_tz, d)}"],
            final=True,
        )

    if not std or not sta or not flight.live:
        dd = pr.dep_median if pr.n else 0
        padding = max(0, min(30, pr.dep_median - pr.arr_median)) if pr.n else 0
        ad = max(dd - padding, pr.arr_median) if pr.n else 0
        if not flight.live:
            why = (
                [f"no live feed; usual arrival delay {pr.arr_median:+d} min (n={pr.n})"]
                if pr.n
                else ["no live data for this flight"]
            )
            band = band_for(ad) if pr.n else "unknown"
            return pack(dd, ad, band, why, schedule_only=True)

    padding = max(0, min(30, pr.dep_median - pr.arr_median)) if pr.n else 0
    cands = []
    airline_dd = None
    if flight.act_out:
        dd = mins(flight.act_out - std)
        cands.append((dd, f"left the gate at {clock(flight.act_out, origin_tz, o)}"))
    else:
        if flight.est_out:
            airline_dd = mins(flight.est_out - std)
            cands.append((airline_dd, f"airline now shows departure {clock(flight.est_out, origin_tz, o)}"))
        if flight.inbound and flight.inbound.est_arr:
            ready = flight.inbound.est_arr + dt.timedelta(minutes=min_turn)
            prop = max(0, mins(ready - std))
            ident = flight.inbound.ident or "inbound"
            in_tz = flight.inbound.dest_tz or origin_tz
            in_iata = flight.inbound.dest_iata or o
            cands.append((
                prop,
                f"incoming aircraft ({ident}) reaches the gate {clock(flight.inbound.est_arr, in_tz, in_iata)}; "
                f"with a {min_turn}-min turn the earliest departure is {clock(ready, origin_tz, o)}",
            ))
        if pr.n:
            cands.append((pr.dep_median, f"this flight usually leaves {pr.dep_median:+d} min (n={pr.n})"))
    dd, main = max(cands, key=lambda c: c[0]) if cands else (0, "no change reported")

    if flight.est_in and (flight.act_out or flight.status == "active"):
        ad = mins(flight.est_in - sta)
        reasons = [f"in the air, estimated arrival {clock(flight.est_in, dest_tz, d)}"]
    else:
        ad = dd - padding
        if flight.est_in:
            ad = max(ad, mins(flight.est_in - sta))
        reasons = [main]

    p = pack(dd, ad, band_for(ad), reasons)
    p.airline_dep_delay = airline_dd
    return p
