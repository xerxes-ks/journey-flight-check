# HTTP API

Copy-paste the examples after setting:

```bash
export BASE="https://YOUR_DEPLOYMENT"
export FLIGHTCHECK_TOKEN="…"
```

The production alias is public and token-gated. **Deployment / preview URLs** may be behind **Vercel Authentication**. `curl` against a preview without a bypass cookie or `x-vercel-protection-bypass` header gets an HTML login page, not JSON. `GET /api/health` is unauthenticated at the app layer but still blocked by that preview gate. See `docs/TESTING.md`.

Synthetic fixtures only: `XX123` / `YY456` / `XX321`. Never send live trip data.

## Auth

| Header | Used by |
|---|---|
| `Authorization: Bearer $FLIGHTCHECK_TOKEN` | `/api/check`, `/api/track`, `/api/untrack`, `/api/tracked`, `/api/events`, `/api/simulate`, `GET /api/webhook-sink`, optional `/api/tick` |
| `Authorization: Bearer $CRON_SECRET` | `/api/tick` (Vercel Cron) |
| `X-Flightcheck-Signature: sha256=<hex>` | inbound `POST /api/webhook-sink` (HMAC-SHA256 of the **raw** body with `FLIGHTCHECK_WEBHOOK_SECRET`) |
| `X-Flightcheck-Mode: fixtures` | with a valid token, use bundled AeroAPI fixtures and honour `now` |

Live mode **ignores** `now`. Fixture mode: `FLIGHTCHECK_MODE=fixtures` on the process, or the header above.

Stateful fixture Track/Tick is stored under the `fixtures/` prefix on the same backing store. Live cron lists `tracks/` only and never polls fixture subscriptions. Fixture requests never create, advance or stop live tracks. `/api/simulate` and stateless `/api/check` are unchanged. The `/test` console’s default fixture flow uses that isolated namespace.

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

Success: `ok`, `status`, `band`, `summary`, `flight`, `prediction`, `next_check_local`, `checked_at`, `checkpoint`, `final`, `calls`, `cost_usd_est`. When `onward` was requested but `connection` is null, `connection_reason` is `untracked` (not looked up) or `not_found` (looked up, missing). Errors: `{ok:false, error:{code,message,retryable,...}}`.

`budget` / `budget_usd` must be finite and nonnegative (`0` means zero provider calls). `NaN`, `Infinity` and their string forms are `400 bad_request`. Unit pricing is server-owned (`$0.005` estimate); clients cannot set `cost_per_call`. HTTP clamps the per-request cap at `$0.50`. The daily cap `FLIGHTCHECK_DAILY_BUDGET_USD` (default `$0.50`) is reserved **before** each paid lookup on track registration, ad hoc `/api/check`, and tick. Ambiguous outcomes (timeouts, 5xx) keep the reservation.

### `POST /api/track`

Start tracking. Lookup key is ident+date+origin+webhook. An exact retry returns `idempotent: true` and the existing `tracking_id`. A retry that changes `ref`, onward flight, policy/cadence or `alert_delay_min` returns **409 `conflict`** instead of silently keeping the old configuration. Untrack and re-register to replace. Auth: token.

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
{"ok": true, "due": 1, "ran": 1, "skipped_budget": 0, "skipped_claim": 0, "events": 0, "spent_usd": 0.005, "budget_usd": 0.5, "checked_at": "…Z"}

Overlapping ticks take a claim object (`locks/tick.json`, 45s expiry) via a Blob `x-if-match` / `x-if-none-match` conditional PUT (current `@vercel/blob` putOptionHeaderMap.ifMatch, API v13). The loser returns `skipped_claim: 1` and does no paid lookups. Checkpoints never move backwards. Daily spend is reserved before the adapter call, so two workers cannot exceed the cap or drop a charge.
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

Outbound callbacks persist as `pending/{tracking_id}/{event_id}.json` with a stable `event_id` and `seq`. A later tick retries them **without** a new paid lookup. Retention is bounded (40 per track, 7 days). Delivery is HTTPS only (except loopback HTTP when `VERCEL_ENV` is unset, for local `python -m flightcheck.dev`), does not follow redirects, uses a 3s timeout, and is skipped when `FLIGHTCHECK_WEBHOOK_SECRET` is missing. Hosts must appear in `FLIGHTCHECK_WEBHOOK_ALLOWED_HOSTS` (comma-separated); `VERCEL_URL` / `VERCEL_BRANCH_URL` / `VERCEL_PROJECT_PRODUCTION_URL` are also allowed so the `/test` sink works on a deployment. After the receiver returns 2xx the pending record is marked `acked` and is not sent again.

```bash
curl -sS -H "Authorization: Bearer $FLIGHTCHECK_TOKEN" "$BASE/api/webhook-sink"
```

`{ok, count, deliveries:[{received_at, signature_ok, event, tracking_id, payload}]}`.

### `GET /test`

Static operator console (`public/test/index.html`). The page itself has no API auth; calls it makes still need the token.

## Error codes

| code | HTTP | retryable | when |
|---|---|---|---|
| `unauthorized` | 401 | false | missing/wrong bearer (`missing or invalid token`) |
| `bad_request` | 400 | false | missing fields, invalid JSON |
| `invalid_date` | 400 | false | ticket `date` is not YYYY-MM-DD |
| `not_found` | 404 | false | unknown tracking_id or AeroAPI 404 |
| `origin_mismatch` | 404 | false | requested origin matches no returned leg. Not evaluated on `too_early` (no AeroAPI call until T−24h), so a wrong origin still returns `too_early` until the first live lookup. |
| `flight_not_found` | 200 | false | empty AeroAPI list (`flight not found`) |
| `storage_not_configured` | 503 | false | no `BLOB_READ_WRITE_TOKEN` (and not redis/memory) |
| `storage_error` | 503 | false | Blob config/permission failure (private store PUT without `x-vercel-blob-access`) |
| `budget_exceeded` | 402 | false | per-request or daily AeroAPI cap |
| `conflict` | 409 | false | track retry with a different ref/onward/policy/threshold |
| `forbidden` | 403 | false | fixture tracking against the live store |
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

Layout (live): `tracks/{id}.json`, `events/{id}/{ts}-{seq}.json`, `pending/{id}/{event_id}.json`, `locks/tick.json`, `sink/{ts}-{seq}.json`, `budget/{YYYY-MM-DD}.json`. Fixture traffic is the same paths under `fixtures/`. Track JSON carries a `version` counter; Blob writes use `x-if-match` (and `x-if-none-match: *` on create) so a stale writer is rejected with HTTP 412. MemoryStore uses a mutex around the same version check. An in-process flag does **not** serialize Vercel instances.
