"""python -m flightcheck check XX123 2026-12-10 [--onward YY456] [--budget 0.05] [--json] [--now ...]"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .check import check_flight, parse_request
from .exceptions import CheckError
from .sanitize import redact
from .times import parse_now


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="flightcheck",
        description="Look up a flight by ticket-printed origin-local date. Never convert that date to UTC.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="check a flight on a given ticket date")
    c.add_argument("flight", help="flight number, e.g. XX123 (IATA or ICAO; spaces/case ignored)")
    c.add_argument("date", help="origin-local departure date as printed on the ticket (YYYY-MM-DD)")
    c.add_argument("--origin", help="IATA departure airport, to disambiguate")
    c.add_argument("--onward", help="onward flight number")
    c.add_argument("--onward-date", help="onward ticket date YYYY-MM-DD (default: same as flight)")
    c.add_argument("--onward-origin", help="IATA for the onward departure airport")
    c.add_argument("--mct", type=int, default=45, help="minimum connection time in minutes")
    c.add_argument("--terminal-change", action="store_true")
    c.add_argument("--bag-recheck", action="store_true")
    c.add_argument("--separate-ticket", action="store_true")
    c.add_argument("--min-turn", type=int, default=None, help="override inbound min-turn minutes")
    c.add_argument("--budget", type=float, default=0.05, help="AeroAPI spend cap in USD (estimate)")
    c.add_argument("--json", action="store_true", help="print the structured result as JSON")
    c.add_argument("--now", dest="now", help="simulation clock: local or ISO timestamp (tests / replay)")
    c.add_argument("--at", dest="now", help=argparse.SUPPRESS)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd != "check":
        return 1
    payload = {
        "flight": args.flight,
        "date": args.date,
        "origin": args.origin,
        "budget": args.budget,
    }
    if args.min_turn is not None:
        payload["min_turn"] = args.min_turn
    if args.onward:
        payload["onward"] = {
            "flight": args.onward,
            "date": args.onward_date,
            "origin": args.onward_origin,
            "min_connect_min": args.mct,
            "terminal_change": args.terminal_change,
            "bags_recheck": args.bag_recheck,
            "separate_ticket": args.separate_ticket,
        }
    now = parse_now(args.now) if args.now else None
    try:
        req = parse_request(payload)
        result = check_flight(req, now=now)
    except CheckError as exc:
        print(redact(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(redact(exc), file=sys.stderr)
        return 1
    data = result.to_dict()
    f, p = data["flight"], data["prediction"]
    if not os.environ.get("FLIGHTAWARE_AEROAPI_KEY") and not f.get("live"):
        print("  (no FLIGHTAWARE_AEROAPI_KEY; no live lookup)", file=sys.stderr)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    ident = f.get("ident_iata") or f.get("ident") or args.flight
    route = f"{f.get('origin') or '?'}->{f.get('destination') or '?'}"
    print(data.get("summary") or f"{ident} {args.date} {route}  status={f['status']}  band={p['band']}")
    if p.get("reasons"):
        print("  " + "; ".join(p["reasons"]))
    if data.get("inbound"):
        ib = data["inbound"]
        eta = (ib.get("est_arr") or {}).get("human") or (ib.get("est_arr") or {}).get("local")
        print(f"  inbound {ib.get('ident')} eta {eta}")
    if data.get("connection"):
        c = data["connection"]
        print(f"  connection {c['band']} slack={c['slack_min']} min  {c['explain']}")
    if data.get("next_check_local"):
        print(f"  next check {data['next_check_local']['human']} (in {data.get('next_check_in_minutes')} min)")
    print(f"  calls={len(data['calls'])}  cost_usd_est={data['cost_usd_est']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
