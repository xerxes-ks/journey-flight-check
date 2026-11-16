# flightcheck

A small, general-purpose, **stateless** flight-status tool. Given a flight number and the **origin-local date printed on the ticket**, it returns structured live status, a delay prediction, optional connection risk, a one-sentence `summary`, and `next_check_local` so a cron can self-schedule.

v0.1 is a Python library, a CLI, and a Vercel Python Function. It does **not** schedule rechecks, send messages, or store trip manifests — those belong to callers. An MCP server wrapper is **out of scope** for v0.1 (future work).

## Local-time contract

Tickets print local times. Callers (an LLM concierge or a cron) should **never convert to UTC**.

- **Input `date`** is the departure date as printed on the ticket (the origin airport’s local calendar date). There is no timezone parameter. Optional `origin` (IATA) disambiguates duplicate flight numbers and is used to resolve timezone *before* the first AeroAPI call.
- **Output times** are ISO-8601 strings **with the airport’s offset**, plus `tz` (IANA name) and a short `human` form such as `02:55 Sun 13 Dec (DEL time)`. Departures use the origin tz; arrivals use the destination tz. Reason strings use those local clocks, never UTC.
- **`checked_at`** is the UTC time of this check (ISO with `Z`). Store it with the result to judge freshness.
- **`next_check_local`** is in the origin’s local time; **`next_check_in_minutes`** / **`next_poll_min`** (same integer) is the wait from `now`. **Null only when the result is `final`**. `too_early` is not final: `next_check` is **T−24h**. Echo `checkpoint` and `attempts` on the next call so cancelled / takeoff / landing retries are counted.
- **`summary`** is a short paragraph a chat bot can relay as-is (human band words: on track / worth watching / at risk). Top-level `status`, `band`, and `final` duplicate the fields a cron or LLM is most likely to branch on.

### Timezones

After a live lookup, timezones always come from AeroAPI `origin.timezone` / `destination.timezone`.

The **too_early** path makes **no AeroAPI call**, so it cannot read those fields. It resolves `origin` through a small bundled IATA→tz map (`flightcheck/airports.py`, major public airports, stdlib only — not the `airportsdata` package). Unknown IATA falls back to UTC. Without `origin`, too_early uses UTC midnight of the ticket date as a stand-in for T.

## What it returns

- **Resolved flight:** scheduled, estimated, and actual **gate** times (`actual_out` / `actual_in`) plus **runway** times (`actual_off` / `actual_on`); AeroAPI `status_text` and `progress_percent`; movement `phase` (`scheduled` / `at_gate_late` / `taxiing` / `airborne` / `landed` / `at_arrival_gate`). Never “in the air” while taxiing.
- **Inbound aircraft:** ident and ETA only at the T−3h and T−1h checkpoints (essential cadence). Cancelled/diverted inbound raises the punctuality band to at least `watch`.
- **Delay prediction:** expected departure and arrival delay in minutes, a band (`on_track` / `watch` / `at_risk` / `unknown`), and local-time reasons. Evidence order: airline estimate, then inbound ETA + min turn, then an optional historical prior. Default min turn is **35 min**, or **60 min** for wide-body/long-haul (`B77x`/`B78x`/`A33x`/`A35x`/`A38x`, or block time > 5h). Overridable via `min_turn`.
- **Connection risk** (optional `onward`): default `min_connect_min` is **90 min** for a self-transfer (`separate_ticket` or `bags_recheck`), otherwise 45. `impact_band` follows connection risk. Punctuality `band` uses `alert_delay_min` (default 45). The summary mentions the connection when `onward` is given.
- **Calls and cost:** each AeroAPI request with an estimated USD cost.
- **Errors:** structured `{code, message, retryable, http?, snippet?}`. `retryable` is **true** for `rate_limited`, `upstream_error`, and `timeout`; **false** for `bad_request`, `not_found`, `flight_not_found`, `budget_exceeded`, and `unauthorized`. Empty AeroAPI lists are **not** on_track: HTTP 200, `ok: false`, `status`/`band` `unknown`, `error.code: flight_not_found`. AeroAPI HTTP 404 is `not_found`. An 8s urlopen hang is `timeout`. The HTTP route never returns a bare 502.

## Architecture

1. **Adapter** — FlightAware AeroAPI v4. Tests inject `fetch`; there is no network in CI. The fake fetch rejects windows the way AeroAPI does (`end` more than ~47h ahead → HTTP 400).
2. **Prediction** — as above. Diverted / landed are `final`. Cancelled is confirmed after one 30-min recheck.
3. **Connection scoring** — slack = (onward departure − bag-drop close if re-checking − required) − predicted arrival.
4. **Surfaces** — library (`flightcheck.check_flight`), CLI, HTTP `GET`/`POST /api/check`.

If `now` is before **T−24h**, the adapter is **not** called: status `too_early`, `checkpoint` `t_minus_24h`, `next_check` at T−24h (T is origin-local midnight of the ticket date until `scheduled_out` is known, then checkpoints recompute from STD). Query `end` is still clamped to `now+47h`. If a later inbound/onward call would exceed the per-request budget, the flight already fetched is returned with a `warning` — it is not dropped.

```
flight + ticket-local date [+ origin] [+ onward]
        │
        ├─ too_early?  (T−24h) ──► status too_early, no API call
        ▼
  AeroAPI adapter ── /flights/{ident}?start&end   (end ≤ now+47h)
                  └── /flights/{fa_flight_id}     (inbound at T−3h and T−1h only)
        │
        ▼
     predict ──► optional connection score ──► summary + next_check_local
```

## AeroAPI endpoints

| Call | When |
|---|---|
| `GET /flights/{ident}?start={iso}&end={iso}&ident_type=designator` | Each essential checkpoint from T−24h. `start`/`end` cover that local day and are clamped to AeroAPI’s ~2-day-ahead limit. |
| `GET /flights/{fa_flight_id}?ident_type=fa_flight_id` | Inbound aircraft at **T−3h** and **T−1h** only (essential). Dense cadence still uses the 6h inbound window. |

The instance is chosen by the **origin-local** calendar date of `scheduled_out`. UTC date is used only when the origin tz is unknown.

API only; this tool does not scrape FlightAware’s website. The key is read from **`FLIGHTAWARE_AEROAPI_KEY`** (`x-apikey`). It never appears in logs, `calls[].params`, or HTTP responses. Upstream error bodies are kept as a short **sanitized** snippet and mapped: `400→bad_request`, `404→not_found`, `429→rate_limited`, urlopen timeout → `timeout`, `5xx→upstream_error`.

## Cost model

About **$0.005 per result set** (configurable via `cost_per_call`). This is an **estimate** — confirm against the [FlightAware AeroAPI usage page](https://www.flightaware.com/commercial/aeroapi/). Hard budget is **per request** (library default `$0.05`; HTTP `HTTP_BUDGET_CAP` `$0.50`). A **daily** cap is the **caller's** job; this tool does not enforce one.

## Recheck cadence

Default is **`cadence=essential`**: about **7–9 AeroAPI result sets** per flight. Pass `cadence=dense` (or `poll_interval`) for the old 15-minute table.

| Checkpoint | When (origin-local STD) | Calls |
|---|---|---|
| `t_minus_24h` | T−24h | 1 (flight) |
| `t_minus_6h` | T−6h | 1 |
| `t_minus_3h` | T−3h | 2 (flight + inbound) |
| `t_minus_1h` | T−1h | 2 |
| `takeoff` | expected off + 30 min (STD+30 if on time). If not airborne, one retry 30 min later, then the landing check. | 1 |
| `landing` | expected arrival + 10 min. If not landed, retry at the new ETA+10, max 2 retries, then `final`. | 1 |
| `done` | landed, diverted, or cancelled after the 30-min confirm | 0 |

Nothing before T−24h (`too_early`). A retime recomputes later checkpoints from the new STD / expected off. Cancelled **shows as cancelled** on first sight, rechecks once after 30 min, then `final`. Retryable upstream errors set `next_check` to 15 min and never put raw AeroAPI text in `summary`.

Example, synthetic **02:55** local departure (STD known):

```
T-24h  02:55 previous local day   1 call
T-6h   20:55 previous local day   1 call
T-3h   23:55 previous local day   2 calls
T-1h   01:55                      2 calls
takeoff 03:25 (STD+30)            1 call  (+ optional +30 retry)
landing expected arrival + 10     1 call  (+ up to 2 ETA+10 retries)
```

~7 calls if the flight runs on time; ~9 with both retries.

## Install and CLI

Python 3.12+. Runtime: standard library only.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/python -m flightcheck check XX123 2026-12-10 --origin LAX --budget 0.05 --cadence essential --json
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

`schema/check_flight.tool.json` — required `flight` and `date`; optional `origin` and `onward`. The description tells the model to pass the ticket-printed local date and never convert to UTC. Output requires `checked_at` (UTC, `Z`) and `next_poll_min` (same integer as `next_check_in_minutes`; null only when `final`). `error` always includes `retryable`.

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
  "checked_at": "2027-03-15T16:30:00Z",
  "next_check_in_minutes": 15,
  "next_poll_min": 15,
  "error": null
}
```

`too_early` (no API call): `status: "too_early"`, `final: false`, `next_check_local` at visibility (not null). Schedulers must keep polling; only `final` results set `next_check` / `next_poll_min` to null.

## Vercel Function

`api/check.py` — **`POST /api/check` is the contract** (JSON body). `GET` is supported for convenience; query strings can end up in access logs, so callers should prefer POST.

1. Create a Vercel project from this repo. Connect it to **`main`** after this PR is merged (the default branch currently has no function).
2. Set `FLIGHTAWARE_AEROAPI_KEY` and `FLIGHTCHECK_TOKEN`.
3. Deploy. `pyproject.toml` sets `[tool.vercel] entrypoint = "api.check:handler"`. `vercel.json` sets `maxDuration` 30s. Upstream AeroAPI calls time out at **8s** so a client with a ~10s timeout can treat a hang as retryable.

Auth failures are 401. Missing `flight`/`date` is 400. Budget exceeded is 402. Upstream AeroAPI errors are **structured JSON** (`error.code` / `error.retryable` / `error.snippet`), not a bare 502.

```bash
curl -sS -H "Authorization: Bearer $FLIGHTCHECK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"flight":"XX123","date":"2026-12-10","origin":"LAX"}' \
  "https://YOUR_DOMAIN/api/check"
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
