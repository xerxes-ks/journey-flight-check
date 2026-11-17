# How an LLM should read flightcheck events

The flight-check service owns tracking. Callers keep trip context (who is travelling, PNR, chat thread) and only store the opaque `ref` they sent to `POST /api/track`.

When a webhook arrives, **relay `summary` as-is**. Do not convert times. Every timestamp in `times.*.human` is already airport-local. `times.*.local` is ISO-8601 with the airport offset — not UTC unless the airport is on UTC.

## Envelope

```json
{
  "event": "delay_likely",
  "event_id": "evt_…",
  "seq": 2,
  "tracking_id": "trk_…",
  "ref": "your-opaque-id",
  "flight": "XX123",
  "checkpoint": "t_minus_3h",
  "phase": "scheduled",
  "band": "watch",
  "impact_band": null,
  "summary": "XX123 LHR→SIN is worth watching. …",
  "times": {
    "scheduled_out": { "human": "11:20 Tue 9 Mar (XXC time)", "local": "2027-03-09T11:20:00+13:00" }
  },
  "checked_at": "2027-12-12T23:55:00Z",
  "snapshot": {}
}
```

Verify `X-Flightcheck-Signature: sha256=<hex>` with HMAC-SHA256 of the raw body and `FLIGHTCHECK_WEBHOOK_SECRET`. Treat `event_id` as the idempotency key: a retried delivery after a 5xx uses the same `event_id` and `seq`. After you acknowledge with HTTP 2xx the service will not send that event again.

Delivery is HTTPS only, does not follow redirects, times out at 3 seconds, and is not attempted when the webhook secret is missing. Hosts must be listed in `FLIGHTCHECK_WEBHOOK_ALLOWED_HOSTS` (the Vercel deployment host is also allowed). Failed deliveries stay in a bounded pending queue and are retried on later ticks without another paid flight lookup.

## Events

| Event | Meaning | What to tell a person |
|---|---|---|
| `tracking_started` | The service accepted the flight and scheduled the next essential checkpoint. | “I’m watching this flight.” |
| `delay_likely` | Punctuality band moved to `watch` or `at_risk`. | Relay `summary`. Do not invent a new delay number. |
| `delay_cleared` | Band returned to `on_track`. | Relay `summary`. |
| `retimed` | Scheduled vs estimated departure moved by 30 minutes or more. | Use `times.estimated_out.human`. |
| `gate_changed` | Origin gate changed since the last snapshot. | Use the gate in `snapshot.flight.gate_origin`. |
| `cancelled_unconfirmed` | AeroAPI **shows as cancelled**. This is **not** final. The service rechecks in 30 minutes. | “It currently shows as cancelled; I’m rechecking shortly.” Never say the flight is definitely cancelled. |
| `cancelled` | Cancelled after the confirm recheck. Tracking ends. | “This flight is cancelled.” |
| `diverted` | Diverted. Tracking ends. | Relay `summary` (includes the divert airport when known). |
| `departed` | Airborne (`phase=airborne`). Includes expected landing in `times`. | “It’s away. Estimated landing …” — never say this while `phase` is `taxiing`. |
| `landed` | Runway on or at the arrival gate. Terminal / gate / belt are in `snapshot.flight` when known. | Relay `summary`. |
| `connection_at_risk` | `impact_band` is `at_risk` (only if `onward` was given). | Relay `summary`; do not lowercase the onward flight number. If `snapshot.connection` is null, read `snapshot.connection_reason` (`untracked` or `not_found`) — an unavailable onward leg is not a live connection. |
| `tracking_ended` | No more checks (`final`). | Stop expecting updates. |
| `check_failed` | Live status failed after retries. | “Live status is temporarily unavailable.” Never quote raw upstream text. |

Silent checkpoints (no webhook) are normal. The cron still ran; nothing material changed.

## Cadence (do not reimplement)

The service follows the essential schedule: T−24h, T−6h, T−3h, T−1h, T−30m, takeoff, landing. About 7–10 AeroAPI calls per flight (9 on time). The landing check that sees on-block or runway-on is final. You do not need to poll `/api/check` while a `tracking_id` is active.

## What the service does not store

No passenger names, PNRs, chat ids, or trip manifests. Only: flight ident, ticket date, origin, policy, alert options, webhook URL, `ref`, last snapshot, event log.
