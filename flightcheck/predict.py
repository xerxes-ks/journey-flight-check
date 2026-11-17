"""Delay prediction for one flight. Deterministic: every number has a reason.

pred_dep_delay = max(airline estimate, inbound propagation, historical prior)
  inbound propagation = max(0, inbound ETA at gate + min turn - STD)
pred_arr_delay = pred_dep_delay - schedule padding (prior dep median - arr median, 0..30 min)
Once airborne, the live arrival estimate wins. Landed = actual times.
Human reasons use origin/destination local airport time, never UTC.
"""

from __future__ import annotations

import datetime as dt

from .model import (
    DEFAULT_ALERT_DELAY_MIN,
    ON_TRACK_MAX,
    UTC,
    Flight,
    Prediction,
    Prior,
    mins,
    worst,
)
from .times import clock, format_delay


def band_for(arr_delay: int, alert_delay_min: int = DEFAULT_ALERT_DELAY_MIN) -> str:
    if arr_delay <= ON_TRACK_MAX:
        return "on_track"
    if arr_delay <= int(alert_delay_min):
        return "watch"
    return "at_risk"


def predict(
    flight: Flight,
    *,
    min_turn: int = 35,
    prior: Prior | None = None,
    alert_delay_min: int = DEFAULT_ALERT_DELAY_MIN,
    confirm_cancel: bool = False,
    now: dt.datetime | None = None,
) -> Prediction:
    std, sta = flight.sched_out, flight.sched_in
    now = now or dt.datetime.now(UTC)
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
        return pack(0, 0, "unknown", ["more than 24 hours before departure; live data is not queried yet"])
    if flight.cancelled:
        return pack(0, 0, "at_risk", ["shows as cancelled"], final=bool(confirm_cancel))
    if flight.diverted:
        where = flight.divert_to or "another airport"
        return pack(0, 0, "at_risk", [f"diverted to {where}"], final=True)
    if flight.status == "unknown" or (not flight.live and flight.source != "aeroapi"):
        if not std or not sta:
            return pack(0, 0, "unknown", ["no live data for this flight"], schedule_only=True)
    if (flight.act_in or flight.act_on) and sta:
        arrived = flight.act_in or flight.act_on
        arr_ref = sta
        if flight.est_in and abs(mins(flight.est_in - sta)) >= 30:
            arr_ref = flight.est_in
        dep_ref = std
        if flight.est_out and std and abs(mins(flight.est_out - std)) >= 30:
            dep_ref = flight.est_out
        dd = mins(flight.act_out - dep_ref) if flight.act_out and dep_ref else 0
        ad = mins(arrived - arr_ref)
        return pack(
            dd, ad, band_for(ad, alert_delay_min),
            [f"landed {clock(arrived, dest_tz, d, vs=arr_ref)}"],
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
            band = band_for(ad, alert_delay_min) if pr.n else "unknown"
            return pack(dd, ad, band, why, schedule_only=True)

    padding = max(0, min(30, pr.dep_median - pr.arr_median)) if pr.n else 0
    cands = []
    airline_dd = None
    if flight.act_off or flight.act_out:
        left = flight.act_off or flight.act_out
        dd = mins(left - std)
        if flight.act_off:
            cands.append((dd, f"took off at {clock(flight.act_off, origin_tz, o, vs=std)}"))
        else:
            cands.append((dd, f"left the gate at {clock(flight.act_out, origin_tz, o, vs=std)}"))
    else:
        if std and now.astimezone(std.tzinfo) > std:
            late = mins(now.astimezone(std.tzinfo) - std)
            if flight.est_out:
                late = max(late, mins(flight.est_out - std))
            cands.append((late, f"departure delayed {format_delay(late)} past scheduled"))
        if flight.est_out:
            airline_dd = mins(flight.est_out - std)
            cands.append((airline_dd, f"airline now shows departure {clock(flight.est_out, origin_tz, o, vs=std)}"))
        if flight.inbound and flight.inbound.est_arr:
            ready = flight.inbound.est_arr + dt.timedelta(minutes=min_turn)
            prop = max(0, mins(ready - std))
            ident = flight.inbound.ident or "inbound"
            in_tz = flight.inbound.dest_tz or origin_tz
            in_iata = flight.inbound.dest_iata or o
            if flight.inbound.cancelled:
                cands.append((
                    max(prop, 1),
                    f"incoming aircraft ({ident}) is cancelled",
                ))
            elif flight.inbound.diverted:
                cands.append((
                    max(prop, 1),
                    f"incoming aircraft ({ident}) is diverted",
                ))
            else:
                cands.append((
                    prop,
                    f"incoming aircraft ({ident}) reaches the gate {clock(flight.inbound.est_arr, in_tz, in_iata, vs=std)}; "
                    f"with a {min_turn}-min turn the earliest departure is {clock(ready, origin_tz, o, vs=std)}",
                ))
        elif flight.inbound and (flight.inbound.cancelled or flight.inbound.diverted):
            ident = flight.inbound.ident or "inbound"
            kind = "cancelled" if flight.inbound.cancelled else "diverted"
            cands.append((1, f"incoming aircraft ({ident}) is {kind}"))
        if pr.n:
            cands.append((pr.dep_median, f"this flight usually leaves {pr.dep_median:+d} min (n={pr.n})"))
    dd, main = max(cands, key=lambda c: c[0]) if cands else (0, "no change reported")

    if flight.est_in and flight.act_off:
        ad = mins(flight.est_in - sta)
        reasons = [f"in the air, estimated arrival {clock(flight.est_in, dest_tz, d, vs=sta)}"]
    elif flight.act_out and not flight.act_off:
        ad = dd - padding
        if flight.est_in:
            ad = max(ad, mins(flight.est_in - sta))
        reasons = [f"left the gate, still taxiing; {main}"]
    else:
        ad = dd - padding
        if flight.est_in:
            ad = max(ad, mins(flight.est_in - sta))
        reasons = [main]

    band = band_for(ad, alert_delay_min)
    if flight.inbound and (flight.inbound.cancelled or flight.inbound.diverted):
        band = worst(band, "watch")
        kind = "cancelled" if flight.inbound.cancelled else "diverted"
        ident = flight.inbound.ident or "inbound"
        extra = f"incoming aircraft ({ident}) is {kind}"
        if extra not in reasons[0]:
            reasons = [extra] + reasons
    p = pack(dd, ad, band, reasons)
    p.airline_dep_delay = airline_dd
    return p
