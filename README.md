# flightcheck

A small, general-purpose, **stateless** flight-status tool. Given a flight number and date (and an optional departure airport), it returns structured live status, a delay prediction, optional connection risk versus an onward flight, and the AeroAPI calls made with an estimated cost.

v0.1 is a Python library, a CLI, and a Vercel Python Function. It does **not** schedule rechecks, send messages, or store trip manifests — those belong to callers. An MCP server wrapper is **out of scope** for v0.1 (future work).

## What it returns

- **Resolved flight:** scheduled, estimated, and actual gate times in UTC and local; origin/destination; gates; terminals; baggage claim; aircraft type; registration; status; cancelled; diverted.
- **Inbound aircraft:** ident and ETA when departure is within *N* hours (default 6).
- **Delay prediction:** expected departure and arrival delay in minutes, a band (`on_track` / `watch` / `at_risk`), and human-readable reasons. Order of evidence: airline estimate first, then inbound ETA plus a configurable minimum turn, then an optional historical prior. Airborne flights use the live arrival estimate; landed flights use actual times.
- **Connection risk** (optional onward flight): minimum connection time, terminal-change, bag-recheck, and separate-ticket flags.
- **Calls and cost:** each AeroAPI request with an estimated USD cost.

## Architecture

1. **Adapter** — FlightAware AeroAPI v4, or a timetable-only fallback when nothing is returned. Tests inject `fetch`; there is no network in CI.
2. **Prediction** — `max(airline estimate, inbound gate-in + min turn − STD, historical prior)`, minus schedule padding from the prior (0–30 min). Bands: arrival delay ≤15 on track, ≤45 watch, otherwise at risk. Cancelled / diverted are at risk.
3. **Connection scoring** — slack = (onward departure − bag-drop close if re-checking − required) − predicted arrival. Required = MCT + terminal-change minutes. A later onward departure counts only when the airline has published an estimate. Separate tickets are unprotected. `at_risk` if slack < 0 or the inbound is cancelled/diverted; `watch` if slack is under 30 min (self-transfer / bag recheck) or 15 min (protected).
4. **Surfaces** — library (`flightcheck.check_flight`), CLI (`python -m flightcheck check …`), HTTP (`GET`/`POST /api/check`).

```
ident + date [+ origin] [+ onward]
        │
        ▼
  AeroAPI adapter ── /flights/{ident}?start&end
                  └── /flights/{fa_flight_id}   (inbound, inside the window)
        │
        ▼
     predict ──► optional connection score
        │
        ▼
  library / CLI / Vercel JSON  (+ calls, cost_usd_est)
```

## AeroAPI endpoints

| Call | When |
|---|---|
| `GET /flights/{ident}?start={iso}&end={iso}&ident_type=designator` | Always, for the requested date. `start`/`end` are a ± window around the UTC date so origin-local midnight is covered. |
| `GET /flights/{fa_flight_id}?ident_type=fa_flight_id` | Inbound aircraft, only when `inbound_fa_flight_id` is present, the flight has not pushed back, and departure is within `inbound_window_h` (default 6). |

API only; this tool does not scrape FlightAware’s website. The key is read from **`FLIGHTAWARE_AEROAPI_KEY`** and is sent as the `x-apikey` header. It is never written to logs, call records, or HTTP responses. Upstream error bodies are discarded.

## Cost model

About **$0.005 per result set** (configurable via `cost_per_call`). This is an **estimate** for the Personal-tier list price of the endpoints above — confirm against the [FlightAware AeroAPI usage page](https://www.flightaware.com/commercial/aeroapi/) before relying on it. Each check enforces a hard budget (CLI/library `--budget` / `budget_usd`, default `$0.05`; the HTTP surface also caps at `$0.50`).

## Recheck cadence

Callers that poll can use the pure helper:

```python
from flightcheck import poll_interval

poll_interval(sched_dep, now, landed=False, sched_arr=None)
# → minutes until the next fetch, or None to skip
```

| When | Interval |
|---|---|
| Further out than T−48h | do not poll |
| T−48h to T−6h | every 3 hours |
| T−6h to T−3h | every hour |
| T−3h to departure | every 15 minutes |
| Airborne | 30 min if block time ≤ 4h, else 60 min |
| Landed | stop |

This package does not run a cron.

## Install and CLI

Python 3.12+. Runtime: standard library only.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/python -m flightcheck check XX123 2026-12-10 --origin XXA --budget 0.05 --json
.venv/bin/python -m flightcheck check XX123 2026-12-10 --onward YY456 --mct 60 --terminal-change --bag-recheck --separate-ticket
```

Without `FLIGHTAWARE_AEROAPI_KEY`, the CLI uses the timetable-only fallback (no live times).

## Library

```python
from datetime import datetime, timezone
from flightcheck import check_flight, poll_interval

result = check_flight(
    {
        "ident": "XX123",
        "date": "2026-12-10",
        "origin": "XXA",
        "onward": {
            "ident": "YY456",
            "min_connection_min": 60,
            "terminal_change": True,
            "bag_recheck": True,
            "separate_ticket": True,
        },
        "budget_usd": 0.05,
        "min_turn": 30,
    }
)
print(result.to_dict())
```

Tests inject `fetch=callable(path, params) -> (status, body)` so nothing on the network is required.

## JSON Schema (LLM tool calling)

`schema/check_flight.tool.json` is a combined tool descriptor: `name`, `description`, `inputSchema`, `outputSchema`. Point a tool-calling agent at it directly. A contract test validates live outputs against `outputSchema`.

Example tool call:

```json
{
  "name": "check_flight",
  "arguments": {
    "ident": "XX123",
    "date": "2026-12-10",
    "origin": "XXA",
    "onward": {
      "ident": "YY456",
      "min_connection_min": 60,
      "terminal_change": true,
      "bag_recheck": true,
      "separate_ticket": true
    },
    "budget_usd": 0.05
  }
}
```

## Vercel Function

`api/check.py` is a Python serverless handler (`GET` and `POST` `/api/check`).

1. Create a Vercel project from this repo (Python runtime; `api/` detected automatically).
2. Set environment variables:
   - `FLIGHTAWARE_AEROAPI_KEY` — AeroAPI key
   - `FLIGHTCHECK_TOKEN` — bearer token the handler requires
3. Deploy. `vercel.json` sets `maxDuration` 30s on `api/check.py`.

```bash
curl -sS -H "Authorization: Bearer $FLIGHTCHECK_TOKEN" \
  "https://YOUR_DOMAIN/api/check?ident=XX123&date=2026-12-10&origin=XXA"
```

`POST` with a JSON body matching `inputSchema` is equivalent. Missing or wrong bearer token → 401. Budget exceeded → 402.

Copy `.env.example` to `.env` for local experiments. `.env*` is gitignored; the example file contains placeholders only.

## Layout

| Path | What |
|---|---|
| `flightcheck/adapters/aeroapi.py` | AeroAPI client, budget guard, inbound lookup |
| `flightcheck/predict.py` | Delay prediction |
| `flightcheck/connections.py` | Connection-risk scoring |
| `flightcheck/cadence.py` | `poll_interval` |
| `flightcheck/check.py` | Orchestrator |
| `flightcheck/cli.py` | `python -m flightcheck` |
| `api/check.py` | Vercel Function |
| `schema/check_flight.tool.json` | Tool input/output JSON Schema |
| `tests/` | Synthetic fixtures and scenario tests (no network) |

## Future work

- MCP server wrapper around `check_flight` (same schema).
- Optional historical-prior fetch from AeroAPI (not in v0.1; pass `prior` yourself).

## License

MIT.
