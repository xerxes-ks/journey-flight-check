# flightcheck

A small, general-purpose, **stateless** flight-status tool. Given a flight number and the **origin-local date printed on the ticket**, it returns structured live status, a delay prediction, optional connection risk, a one-sentence `summary`, and `next_check_local` so a cron can self-schedule.

v0.1 is a Python library, a CLI, and a Vercel Python Function. It does **not** schedule rechecks, send messages, or store trip manifests — those belong to callers. An MCP server wrapper is **out of scope** for v0.1 (future work).

## Local-time contract

Tickets print local times. Callers (an LLM concierge or a cron) should **never convert to UTC**.

- **Input `date`** is the departure date as printed on the ticket (the origin airport’s local calendar date). There is no timezone parameter. Optional `origin` (IATA) disambiguates duplicate flight numbers and is used to resolve timezone *before* the first AeroAPI call.
- **Output times** are ISO-8601 strings **with the airport’s offset**, plus `tz` (IANA name) and a short `human` form such as `02:55 Sun 13 Dec (DEL time)`. Departures use the origin tz; arrivals use the destination tz. Reason strings use those local clocks, never UTC.
- **`next_check_local`** is in the origin’s local time; **`next_check_in_minutes`** is the wait from `now`. It is **never null unless the result is final** (cancelled, diverted, landed). Unknown / no_data before departure retries in 60 minutes, or at the time the local day could first appear in AeroAPI’s ~47h window. When `scheduled_out` is known, T is that time.
- **`summary`** is a short paragraph a chat bot can relay as-is (human band words: on track / worth watching / at risk). Top-level `status`, `band`, and `final` duplicate the fields a cron or LLM is most likely to branch on.

### Timezones

After a live lookup, timezones always come from AeroAPI `origin.timezone` / `destination.timezone`.

The **too_early** path makes **no AeroAPI call**, so it cannot read those fields. It resolves `origin` through a small bundled IATA→tz map (`flightcheck/airports.py`, major public airports, stdlib only — not the `airportsdata` package). Unknown IATA falls back to UTC. Without `origin`, too_early uses UTC midnight of the ticket date as a stand-in for T.

## What it returns

- **Resolved flight:** scheduled, estimated, and actual gate times (local); origin/destination; gates; terminals; baggage; aircraft type; registration; status (`scheduled` / `active` / `landed` / `cancelled` / `diverted` / `unknown` / `too_early`).
- **Inbound aircraft:** ident and ETA when departure is within *N* hours (default 6).
- **Delay prediction:** expected departure and arrival delay in minutes, a band (`on_track` / `watch` / `at_risk` / `unknown`), and local-time reasons. Evidence order: airline estimate, then inbound ETA + min turn, then an optional historical prior. Default min turn is **35 min**, or **60 min** for wide-body/long-haul (`B77x`/`B78x`/`A33x`/`A35x`/`A38x`, or block time > 5h). Overridable via `min_turn`.
- **Connection risk** (optional `onward`): `min_connect_min`, `bags_recheck`, `separate_ticket`, plus `terminal_change` / `terminal_change_min` and `checkin_close_min` (bag-drop or check-in close, used for separate tickets too). The summary mentions the connection when `onward` is given. Connection times include day and date (overnight connections).
- **Calls and cost:** each AeroAPI request with an estimated USD cost.
- **Errors:** structured `{code, message, http?, snippet?}` (snippet sanitized). Empty AeroAPI lists are `status: unknown`, band `unknown`, `ok: false` — not on_track.

## Architecture

1. **Adapter** — FlightAware AeroAPI v4. Tests inject `fetch`; there is no network in CI. The fake fetch rejects windows the way AeroAPI does (`end` more than ~47h ahead → HTTP 400).
2. **Prediction** — as above. Cancelled / diverted / landed are `final`.
3. **Connection scoring** — slack = (onward departure − bag-drop close if re-checking − required) − predicted arrival.
4. **Surfaces** — library (`flightcheck.check_flight`), CLI, HTTP `GET`/`POST /api/check`.

If the ticket date’s origin-local midnight is more than **48h** out, the adapter is **not** called: status `too_early`, `next_check_local` = T−48h (T approximated as that local midnight until `scheduled_out` is known). Query `end` is clamped to `now+47h` so AeroAPI does not 400. A 02:55 or 23:30 local departure can still miss that first window; the result is `unknown` with `next_check` in 60 minutes so a cron never dies.

```
flight + ticket-local date [+ origin] [+ onward]
        │
        ├─ too_early?  (local midnight − 48h) ──► status too_early, no API call
        ▼
  AeroAPI adapter ── /flights/{ident}?start&end   (end ≤ now+47h)
                  └── /flights/{fa_flight_id}     (inbound, inside the window)
        │
        ▼
     predict ──► optional connection score ──► summary + next_check_local
```

## AeroAPI endpoints

| Call | When |
|---|---|
| `GET /flights/{ident}?start={iso}&end={iso}&ident_type=designator` | When the ticket date is within 48h of origin-local midnight. `start`/`end` cover that local day and are clamped to AeroAPI’s ~2-day-ahead limit. |
| `GET /flights/{fa_flight_id}?ident_type=fa_flight_id` | Inbound aircraft, only when `inbound_fa_flight_id` is present, the flight has not pushed back, and departure is within `inbound_window_h` (default 6). |

The instance is chosen by the **origin-local** calendar date of `scheduled_out`. UTC date is used only when the origin tz is unknown.

API only; this tool does not scrape FlightAware’s website. The key is read from **`FLIGHTAWARE_AEROAPI_KEY`** (`x-apikey`). It never appears in logs, call records, or HTTP responses. Upstream error bodies are kept as a short **sanitized** snippet and mapped: `400→bad_request`, `404→not_found`, `429→rate_limited`, `5xx→upstream_error`.

## Cost model

About **$0.005 per result set** (configurable via `cost_per_call`). This is an **estimate** — confirm against the [FlightAware AeroAPI usage page](https://www.flightaware.com/commercial/aeroapi/). Hard budget per check (default `$0.05`; HTTP also caps at `$0.50`).

## Recheck cadence

```python
from flightcheck import poll_interval

poll_interval(sched_dep, now, landed=False, sched_arr=None, actual_dep=None)
```

| When | Interval |
|---|---|
| Further out than T−48h | `too_early` / next_check at T−48h (never null) |
| T−48h to T−6h | every 3 hours |
| T−6h to T−3h | every hour |
| T−3h through scheduled departure, still at gate | every 15 minutes, until **actual** off |
| Airborne | 30 min if block time ≤ 4h, else 60 min; **15 min** once estimated arrival is within 45 min |
| Landed / cancelled / diverted | stop (`final`, `next_check_local: null`) |
| unknown / no_data before departure | 60 min, or when the local day becomes visible |

## Install and CLI

Python 3.12+. Runtime: standard library only.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/python -m flightcheck check XX123 2026-12-10 --origin LAX --budget 0.05 --json
.venv/bin/python -m flightcheck check XX123 2026-12-10 --onward YY456 --mct 60 --bag-recheck --separate-ticket
.venv/bin/python -m flightcheck check XX754 2027-12-13 --origin DEL --now 2027-12-11T12:00:00Z --json
.venv/bin/python -m flightcheck check XX754 2027-12-13 --origin DEL --now 2027-12-11T12:00:00Z --live --json
```

`--now` is a simulation clock (local or ISO) and **stays offline** (dry run / fixtures) unless **`--live`** is passed explicitly. A dry run prints one line to stderr and costs $0. Without `FLIGHTAWARE_AEROAPI_KEY`, there is no live lookup.

## Library

```python
from flightcheck import check_flight, poll_interval

result = check_flight(
    {
        "flight": "XX123",
        "date": "2026-12-10",
        "origin": "LAX",
        "onward": {
            "flight": "YY456",
            "date": "2026-12-10",
            "min_connect_min": 60,
            "bags_recheck": True,
            "separate_ticket": True,
        },
    }
)
print(result.to_dict()["summary"])
```

Tests inject `fetch=callable(path, params) -> (status, body)`.

## JSON Schema (LLM tool calling)

`schema/check_flight.tool.json` — required `flight` and `date`; optional `origin` and `onward`. The description tells the model to pass the ticket-printed local date and never convert to UTC.

Example tool call:

```json
{
  "name": "check_flight",
  "arguments": {
    "flight": "XX123",
    "date": "2026-12-10",
    "origin": "LAX",
    "onward": {
      "flight": "YY456",
      "date": "2026-12-10",
      "min_connect_min": 60,
      "bags_recheck": true,
      "separate_ticket": true
    }
  }
}
```

Example output (on-time, truncated):

```json
{
  "ok": true,
  "status": "scheduled",
  "band": "on_track",
  "final": false,
  "summary": "XX123 XXA→XXB is on track, departing 14:00 Mon 15 Mar (XXA time).",
  "flight": {
    "ident_iata": "XX123",
    "origin": "XXA",
    "destination": "XXB",
    "scheduled_out": {
      "local": "2027-03-15T14:00:00-04:00",
      "tz": "America/New_York",
      "human": "14:00 Mon 15 Mar (XXA time)"
    },
    "status": "scheduled"
  },
  "prediction": { "band": "on_track", "dep_delay_min": 0, "final": false },
  "next_check_local": {
    "local": "2027-03-15T12:45:00-04:00",
    "tz": "America/New_York",
    "human": "12:45 Mon 15 Mar (XXA time)"
  },
  "next_check_in_minutes": 15,
  "error": null
}
```

`too_early` (no API call): `status: "too_early"`, `next_check_local` at T−48h in origin local time.

## Vercel Function

`api/check.py` — `GET`/`POST /api/check`.

1. Create a Vercel project from this repo.
2. Set `FLIGHTAWARE_AEROAPI_KEY` and `FLIGHTCHECK_TOKEN`.
3. Deploy. `vercel.json` sets `maxDuration` 30s.

Auth failures are 401. Missing `flight`/`date` is 400. Budget exceeded is 402. Upstream AeroAPI errors are **structured JSON** (`error.code` / `error.snippet`), not a bare 502.

```bash
curl -sS -H "Authorization: Bearer $FLIGHTCHECK_TOKEN" \
  "https://YOUR_DOMAIN/api/check?flight=XX123&date=2026-12-10&origin=LAX"
```

Copy `.env.example` to `.env` for local experiments. `.env*` is gitignored.

## Layout

| Path | What |
|---|---|
| `flightcheck/adapters/aeroapi.py` | AeroAPI client, window clamp, origin-local pick |
| `flightcheck/airports.py` | Bundled IATA→tz map (too_early only) |
| `flightcheck/predict.py` | Delay prediction |
| `flightcheck/connections.py` | Connection-risk scoring |
| `flightcheck/cadence.py` | `poll_interval` |
| `flightcheck/cli.py` | `python -m flightcheck` (`--now` offline unless `--live`) |
| `api/check.py` | Vercel Function |
| `schema/check_flight.tool.json` | Tiny tool input/output JSON Schema |

## Future work

- MCP server wrapper around `check_flight` (same schema).
- Optional historical-prior fetch from AeroAPI (pass `prior` yourself in v0.1).

## License

MIT.
