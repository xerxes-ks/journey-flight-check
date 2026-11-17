# HTTP API

Copy-paste the examples after setting:

```bash
export BASE="https://YOUR_DEPLOYMENT"
export FLIGHTCHECK_TOKEN="…"
```

Preview deployments may be behind **Vercel Authentication**. `curl` without a bypass cookie or `x-vercel-protection-bypass` header gets an HTML login page, not JSON. `GET /api/health` is unauthenticated at the app layer but still blocked by that gate. See `docs/TESTING.md`.

Synthetic fixtures only: `XX123` / `YY456` / `XX754`. Never send live trip data.

## Auth

| Header | Used by |
|---|---|
| `Authorization: Bearer $FLIGHTCHECK_TOKEN` | `/api/check`, `/api/track`, `/api/untrack`, `/api/tracked`, `/api/events`, `/api/simulate`, `GET /api/webhook-sink`, optional `/api/tick` |
| `Authorization: Bearer $CRON_SECRET` | `/api/tick` (Vercel Cron) |
| `X-Flightcheck-Signature: sha256=<hex>` | inbound `POST /api/webhook-sink` (HMAC-SHA256 of the **raw** body with `FLIGHTCHECK_WEBHOOK_SECRET`) |
| `X-Flightcheck-Mode: fixtures` | with a valid token, use bundled AeroAPI fixtures and honour `now` |

Live mode **ignores** `now`. Fixture mode: `FLIGHTCHECK_MODE=fixtures` on the process, or the header above.

## Endpoints

### `GET /api/health`

No auth. Booleans only — never secret values.

```bash
curl -sS "$BASE/api/health"
```

```json
{
  "ok": true,
  "version": "0.1.0",
  "sha": "abc123",
  "storage": "blob",
  "aeroapi_key_configured": true,
  "token_configured": true,
  "webhook_secret_configured": true,
  "cron_secret_configured": true,
  "mode": "live"
}
```

`storage` is `blob` | `memory` | `redis` | `none`. `mode` is `live` | `fixtures` (from `FLIGHTCHECK_MODE`, not the per-request header).

### `POST /api/check` (GET supported)

Stateless check. Auth: token.

```bash
curl -sS -H "Authorization: Bearer $FLIGHTCHECK_TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Flightcheck-Mode: fixtures" \
  -d '{"flight":"XX123","date":"2027-03-15","origin":"XXA","now":"2027-03-14T18:00:00Z"}' \
  "$BASE/api/check"
```

Request JSON:

| Field | Required | Notes |
|---|---|---|
| `flight` / `ident` | yes | IATA or ICAO |
| `date` | yes | origin-local ticket date `YYYY-MM-DD` |
| `origin` | no | IATA; also the tz for a naive `now` |
| `cadence` | no | `essential` (default) or `dense` |
| `alert_delay_min` | no | default 45 |
| `onward` | no | connection scoring |
| `now` | no | ISO local or offset; **fixtures only** |

Success: `ok`, `status`, `band`, `summary`, `flight`, `prediction`, `next_check_local`, `checked_at`, `checkpoint`, `final`, `calls`, `cost_usd_est`. Errors: `{ok:false, error:{code,message,retryable,...}}`.

### `POST /api/track`

Start tracking. Idempotent on ident+date+origin+webhook. Auth: token.

```bash
curl -sS -H "Authorization: Bearer $FLIGHTCHECK_TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Flightcheck-Mode: fixtures" \
  -d '{"flight":"XX123","date":"2027-03-15","origin":"XXA","webhook_url":"'"$BASE"'/api/webhook-sink","ref":"smoke","now":"2027-03-14T18:00:00Z"}' \
  "$BASE/api/track"
```

```json
{"ok": true, "idempotent": false, "tracking_id": "trk_…", "next_check_local": {}, "checkpoint": "t_minus_24h"}
```

### `DELETE /api/track/{id}` or `POST /api/untrack`

Auth: token.

```bash
curl -sS -X DELETE -H "Authorization: Bearer $FLIGHTCHECK_TOKEN" \
  "$BASE/api/track/$TRACKING_ID"

curl -sS -H "Authorization: Bearer $FLIGHTCHECK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tracking_id":"'"$TRACKING_ID"'"}' \
  "$BASE/api/untrack"
```

### `GET /api/tracked`

Auth: token. `{ok, count, tracks:[…]}`.

```bash
curl -sS -H "Authorization: Bearer $FLIGHTCHECK_TOKEN" "$BASE/api/tracked"
```

### `GET /api/tracked/{id}/events`

Auth: token. `{ok, tracking_id, events:[…]}` newest first.

```bash
curl -sS -H "Authorization: Bearer $FLIGHTCHECK_TOKEN" \
  "$BASE/api/tracked/$TRACKING_ID/events"
```

### `GET` / `POST /api/tick`

Due checkpoints. Auth: `CRON_SECRET` or `FLIGHTCHECK_TOKEN`. Vercel Cron hits this every 5 minutes (GET, Bearer cron secret). `now` is honoured only in fixture mode.

```bash
curl -sS -X POST -H "Authorization: Bearer $FLIGHTCHECK_TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Flightcheck-Mode: fixtures" \
  -d '{"now":"2027-03-15T15:00:00Z"}' \
  "$BASE/api/tick"
```

```json
{"ok": true, "due": 1, "ran": 1, "skipped_budget": 0, "events": 0, "spent_usd": 0.005, "budget_usd": 0.5, "checked_at": "…Z"}
```

### `GET` / `POST /api/simulate`

Zero-cost cadence replay against synthetic scenarios. Auth: token. GET lists scenario names. POST body: `scenario`, `flight`, `date`, `origin`, `std_local`.

```bash
curl -sS -H "Authorization: Bearer $FLIGHTCHECK_TOKEN" "$BASE/api/simulate"

curl -sS -H "Authorization: Bearer $FLIGHTCHECK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"scenario":"on_time","flight":"XX123","date":"2027-06-22","origin":"LHR","std_local":"09:40"}' \
  "$BASE/api/simulate"
```

### `POST /api/webhook-sink` and `GET /api/webhook-sink`

POST captures a signed delivery (no bearer; HMAC of the raw body). GET lists the last 50 (bearer token).

```bash
curl -sS -H "Authorization: Bearer $FLIGHTCHECK_TOKEN" "$BASE/api/webhook-sink"
```

`{ok, count, deliveries:[{received_at, signature_ok, event, tracking_id, payload}]}`.

### `GET /test`

Static operator console (`public/test/index.html`). The page itself has no API auth; calls it makes still need the token.

## Error codes

| code | HTTP | retryable | when |
|---|---|---|---|
| `unauthorized` | 401 | false | missing/wrong bearer |
| `bad_request` | 400 | false | missing fields, invalid JSON |
| `not_found` | 404 | false | unknown tracking_id or AeroAPI 404 |
| `flight_not_found` | 200 | false | empty AeroAPI list |
| `storage_not_configured` | 503 | false | no `BLOB_READ_WRITE_TOKEN` (and not redis/memory) |
| `budget_exceeded` | 402 | false | per-request AeroAPI cap |
| `method_not_allowed` | 405 | false | wrong HTTP method |
| `rate_limited` | 429 | true | AeroAPI 429 |
| `timeout` | 200 | true | urlopen hang |
| `upstream_error` | 200 | true | AeroAPI 5xx |
| `error` | 200 | true | unexpected |

## Storage

Default: **private Vercel Blob** when `BLOB_READ_WRITE_TOKEN` is set.

| `FLIGHTCHECK_STORAGE` | backend |
|---|---|
| unset + blob token | `blob` |
| `memory` | process memory (local smoke / tests) |
| `redis` + KV/Upstash env | optional Redis |

Layout: `tracks/{id}.json`, `events/{id}/{ts}-{seq}.json`, `sink/{ts}-{seq}.json`, `budget/{YYYY-MM-DD}.json`. Track JSON carries a `version` counter; a tick skips the write if a re-read shows a newer version.
