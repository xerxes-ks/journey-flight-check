"""python -m flightcheck check XX123 2026-12-10 [--onward YY456] [--budget 0.05] [--json]"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

from .check import check_flight, parse_request
from .exceptions import CheckError
from .model import UTC
from .sanitize import redact


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="flightcheck",
        description="Look up a flight, predict delay, and optionally score a connection.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="check a flight on a given date")
    c.add_argument("ident", help="flight number, e.g. XX123")
    c.add_argument("date", help="local/UTC date YYYY-MM-DD")
    c.add_argument("--origin", help="IATA departure airport, to disambiguate")
    c.add_argument("--onward", help="onward flight number")
    c.add_argument("--onward-date", help="onward date YYYY-MM-DD (default: same as ident)")
    c.add_argument("--onward-origin", help="IATA for the onward departure airport")
    c.add_argument("--mct", type=int, default=45, help="minimum connection time in minutes")
    c.add_argument("--terminal-change", action="store_true")
    c.add_argument("--bag-recheck", action="store_true")
    c.add_argument("--separate-ticket", action="store_true")
    c.add_argument("--min-turn", type=int, default=30, help="minimum turn minutes for inbound aircraft")
    c.add_argument("--budget", type=float, default=0.05, help="AeroAPI spend cap in USD (estimate)")
    c.add_argument("--json", action="store_true", help="print the structured result as JSON")
    c.add_argument("--at", help="ISO timestamp to treat as 'now' (tests / replay)")
    return p


def _now(at: str | None):
    if not at:
        return dt.datetime.now(UTC)
    from .model import parse_time
    return parse_time(at)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd != "check":
        return 1
    payload = {
        "ident": args.ident,
        "date": args.date,
        "origin": args.origin,
        "budget": args.budget,
        "min_turn": args.min_turn,
    }
    if args.onward:
        payload["onward"] = {
            "ident": args.onward,
            "date": args.onward_date,
            "origin": args.onward_origin,
            "min_connection_min": args.mct,
            "terminal_change": args.terminal_change,
            "bag_recheck": args.bag_recheck,
            "separate_ticket": args.separate_ticket,
        }
    try:
        req = parse_request(payload)
        result = check_flight(req, now=_now(args.at))
    except CheckError as exc:
        print(redact(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(redact(exc), file=sys.stderr)
        return 1
    data = result.to_dict()
    f, p = data["flight"], data["prediction"]
    if not os.environ.get("FLIGHTAWARE_AEROAPI_KEY") and f.get("source") == "schedule":
        print("  (no FLIGHTAWARE_AEROAPI_KEY; timetable-only fallback)", file=sys.stderr)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    ident = f.get("ident_iata") or f.get("ident") or args.ident
    route = f"{f.get('origin') or '?'}->{f.get('destination') or '?'}"
    print(f"{ident} {args.date} {route}  status={f['status']}  band={p['band']}")
    if p["reasons"]:
        print("  " + "; ".join(p["reasons"]))
    if data.get("inbound"):
        ib = data["inbound"]
        eta = (ib.get("est_arr") or {}).get("local") or (ib.get("est_arr") or {}).get("utc")
        print(f"  inbound {ib.get('ident')} eta {eta}")
    if data.get("connection"):
        c = data["connection"]
        print(f"  connection {c['band']} slack={c['slack_min']} min  {c['explain']}")
    print(f"  calls={len(data['calls'])}  cost_usd_est={data['cost_usd_est']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
