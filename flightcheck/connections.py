"""Connection-risk scoring between a resolved inbound flight and an onward flight.

required time = MCT + terminal-change minutes (if flagged)
deadline      = onward departure (counting a later airline estimate only when published)
                minus bag-drop close when bags must be re-checked
slack         = (deadline - required) - predicted arrival
band          = at_risk if the inbound is cancelled/diverted or slack < 0
                watch   if slack < margin (30 min self-transfer / 15 min protected)
Separate tickets are unprotected: the onward airline owes nothing if the connection is missed.
"""

from __future__ import annotations

import datetime as dt

from .model import ConnectionRisk, Flight, OnwardRequest, Prediction, mins
from .predict import hhmm


def assess(
    inbound: Flight,
    pred: Prediction,
    onward: Flight,
    spec: OnwardRequest,
) -> ConnectionRisk:
    protected = not spec.separate_ticket
    required = spec.min_connection_min + (spec.terminal_change_min if spec.terminal_change else 0)
    close = spec.checkin_close_min if spec.bag_recheck else 0
    onward_name = onward.ident_iata or onward.ident

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

    tz = inbound.dest_tz
    parts = [f"{onward_name} leaves {hhmm(next_dep) if next_dep else 'unknown'}"]
    if later:
        parts.append(f" (airline shows +{later} min)")
    parts.append(f"; you need about {required} min")
    if spec.terminal_change:
        parts.append(" incl. a terminal change")
    if close:
        parts.append(f" and bag drop closes {close} min before")
    why = "".join(parts)

    if broken:
        kind = "cancelled" if inbound.cancelled or first.startswith("cancelled") else "diverted"
        explain = f"at risk because {inbound.ident_iata or inbound.ident} is {kind}. {why}."
        return ConnectionRisk(band, slack, required, protected, explain, onward_name)

    if latest and pred.pred_in:
        spare = f"{'+' if slack >= 0 else ''}{slack} min spare"
        explain = (
            f"{why}. Latest workable arrival {hhmm(latest)}; "
            f"predicted {hhmm(pred.pred_in)} ({spare})."
        )
    else:
        explain = f"{why}. Not enough schedule data to compute slack."
    if not protected:
        explain += " Separate tickets: a missed onward flight is not rebooked for free."
    return ConnectionRisk(band, slack, required, protected, explain, onward_name)
