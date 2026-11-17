"""Connection-risk scoring between a resolved inbound flight and an onward flight.

Human explanations use airport-local times, never UTC.
"""

from __future__ import annotations

import datetime as dt

from .model import ConnectionRisk, Flight, OnwardRequest, Prediction, mins
from .times import human_form


def assess(
    inbound: Flight,
    pred: Prediction,
    onward: Flight,
    spec: OnwardRequest,
) -> ConnectionRisk:
    protected = not spec.separate_ticket
    required = spec.min_connection_min + (spec.terminal_change_min if spec.terminal_change else 0)
    # Bag-drop / check-in close applies when re-checking bags or on a separate ticket.
    close = spec.checkin_close_min if (spec.bag_recheck or spec.separate_ticket) else 0
    onward_name = onward.ident_iata or onward.ident
    arr_tz, arr_iata = inbound.dest_tz, inbound.destination
    dep_tz, dep_iata = onward.origin_tz, onward.origin

    first = (pred.reasons or [""])[0]
    broken = first.startswith("cancelled") or first.startswith("diverted") or inbound.cancelled or inbound.diverted

    later = 0
    if onward.est_out and onward.sched_out:
        later = max(0, mins(onward.est_out - onward.sched_out))
    next_dep = None
    latest = None
    slack = 0
    if onward.sched_out:
        next_dep = onward.sched_out + dt.timedelta(minutes=later)
        latest = next_dep - dt.timedelta(minutes=close + required)
        if pred.pred_in:
            slack = mins(latest - pred.pred_in)

    if onward.cancelled:
        return ConnectionRisk(
            band="at_risk",
            slack_min=slack,
            required_min=required,
            protected=protected,
            explain=f"{onward_name} itself is cancelled; this connection no longer exists.",
            onward_ident=onward_name,
        )

    margin = 15 if (protected and not spec.bag_recheck) else 30
    if broken:
        band = "at_risk"
    elif next_dep is None or pred.pred_in is None:
        band = "watch"
    elif slack < 0:
        band = "at_risk"
    elif slack < margin:
        band = "watch"
    else:
        band = "on_track"

    parts = [f"{onward_name} leaves {human_form(next_dep, dep_iata, dep_tz) if next_dep else 'unknown'}"]
    if later:
        parts.append(f" (airline shows +{later} min)")
    parts.append(f"; you need about {required} min")
    if spec.terminal_change:
        parts.append(f" including a terminal change ({spec.terminal_change_min} min)")
    if close:
        parts.append(f" and check-in/bag-drop closes {close} min before")
    why = "".join(parts)

    if broken:
        kind = "cancelled" if inbound.cancelled or first.startswith("cancelled") else "diverted"
        explain = f"at risk because {inbound.ident_iata or inbound.ident} is {kind}. {why}."
        return ConnectionRisk(band, slack, required, protected, explain, onward_name)

    if latest and pred.pred_in:
        spare = f"{'+' if slack >= 0 else ''}{slack} min spare"
        explain = (
            f"{why}. Latest workable arrival {human_form(latest, arr_iata, arr_tz)}; "
            f"predicted {human_form(pred.pred_in, arr_iata, arr_tz)} ({spare})."
        )
    else:
        explain = f"{why}. Not enough schedule data to compute slack."
    if not protected:
        explain += " Separate tickets: a missed onward flight is not rebooked for free."
    return ConnectionRisk(band, slack, required, protected, explain, onward_name)
