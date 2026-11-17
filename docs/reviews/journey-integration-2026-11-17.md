# Journey integration review — 17 November 2026

Reviewed checker main `a0081dbcbf7ada3cb300cd2312646961cc4f1c75` after human
merges of #3 and #4. This report contains redacted aggregate test evidence and
synthetic examples only. Real flight responses, credentials and trip/booking data
remain outside Git. No hosted setting, live subscription or Telegram recipient was
changed by this review.

## Verified and fixed already

- Production `/api/health` returned HTTP 200 with that exact SHA. It reported live
  mode, Blob storage and four configured-key flags. Flags do not prove values or
  preview configuration, and health is not a complete operational acceptance test.
- Authenticated hosted simulation passed normal travel, delay, cancellation then
  reinstatement, and upstream outage. Reported actual simulation cost was zero.
- Hosted stateless fixture connection returned the independent expected 30-minute
  spare margin; missing auth returned 401, invalid date 400, wrong origin 404,
  unknown route 404. No real AeroAPI requests were made by fixture probes.
- Current-main suite: 181 passed locally. The follow-up health-probe suite brings
  the total to 198; tests inject provider and storage transports.
- A real tracked-carrier connection control succeeded. Across all live control
  requests, eight upstream result sets totalled an estimated USD 0.04. This is the
  checker's estimate, not independent billing verification. Connection allowances
  and ticket protection were test inputs, not verified booking properties.
- Private Blob PUT headers and strict origin matching are fixed in #4. Origin
  rejection was independently confirmed with hosted synthetic data. Actual hosted
  Blob write/delete and scheduled cron delivery were not exercised here.

## This code follow-up: make deployment evidence trustworthy

The old health probe returned success when Vercel Authentication blocked access,
accepted insufficient response evidence, and did not bind the result to an
expected deployed SHA. Its redirect handling could also forward a bypass header.

The follow-up requires HTTP 200, the typed health contract and the requested SHA;
blocked, redirected, stale, oversized or malformed responses fail. Destinations
are limited to this project's known Vercel hosts. It prints no response bodies,
URLs or credentials. It adds manual workflow dispatch because this repository
currently receives Vercel **commit statuses**, while the GitHub deployments query
returns zero records: a `deployment_status` trigger alone may never run.

The workflow uses trusted default-branch checker code, read-only GitHub access
and non-persisted checkout credentials. It does not execute deployment-branch code
with the bypass secret. Probe success establishes endpoint shape/revision, not
correct flight data, key permissions, storage writes or reliable notifications.

## Remaining producer work for Grok

These are pre-automation acceptance requirements. They are not fixed by this
health-probe PR and should not be described as resolved by #3/#4.

### 1. Isolate stateful fixtures from live tracking — high priority

`flightcheck/routes.py:54–61,129–140` changes the fetch/clock for fixture requests,
but `start_track`/`tick` still use the configured store. A synthetic authenticated
fixture registration in a live-mode process persisted into the same supplied
store; the next live tick scans the shared track namespace.

**Smallest safe change:** reject stateful fixture Track/Tick against a shared live
store until an isolated fixture namespace/store is explicit. Keep `/api/simulate`
and stateless fixture Check available. Do not solve this with an in-process flag
that another serverless invocation cannot observe.

**Acceptance:** fixture traffic cannot create, advance or stop live subscriptions;
live cron cannot poll fixture subscriptions. Tests must include both modes sharing
one fake backing store and the operator console's default fixture flow.

### 2. Fail closed on numeric budgets and reserve before spending — high priority

`flightcheck/check.py:127–131` accepts float-convertible request values;
`flightcheck/adapters/aeroapi.py:146–149` compares those floats. Injected calls with
`NaN`/infinite budgets bypassed the comparison. Strings containing those values
also convert, so standard JSON strings can reach this path.

**Smallest change:** validate finite, bounded, nonnegative limits at HTTP and
library boundaries; keep unit pricing server-owned. A budget of zero must mean
zero, not fall through to a default. Include registration and ad hoc Check in
spending controls; the current daily Tick cap does not cover every paid path.

**Acceptance:** malformed limits cause no provider calls; overlapping reservations
cannot exceed the agreed allowance; ambiguous request outcomes are accounted for.

### 3. Detect subscription configuration conflicts — medium priority

`flightcheck/track.py:20–27,160–184` keys idempotency on flight/date/origin/webhook.
A second synthetic registration changing `ref`, cadence and onward flight returned
`idempotent: true` while silently retaining the first configuration.

**Smallest change:** compare an immutable request fingerprint and return a clear
conflict on changed settings, or add explicit client registration identity. Keep
subscriber identity separate from lookup deduplication so several current booking
bindings can share a provider lookup without inheriting each other's policy.

**Acceptance:** exact retries reuse the subscription; changed onward/policy/ref
never silently succeed with the old configuration. Test retry after an ambiguous
registration response and replacement after cancellation.

### 4. Retain failed callback deliveries — medium priority

`flightcheck/track.py:301–314` persists state/event then ignores the sender result.
With a fake sender returning 503, the first callback failed; an unchanged later
check made no second delivery attempt. The log is useful, but is not a retry queue.

**Smallest change:** persist pending delivery state and retry it independently of
flight-change detection, with a stable event identity and bounded retention.
Journey can reconcile the event log as a fallback; do not claim complete recovery
without handling log truncation/gaps and delivery cursors.

**Acceptance:** a lost callback is recovered without another paid flight lookup;
retries cannot create duplicate user notifications after receiver acknowledgement.

### 5. Prove concurrency safety before automatic polling — high priority

`flightcheck/store.py:66–77` implements read/check/unconditional-write; BlobStore
inherits that path. This is not atomic compare-and-swap. Tick scans and processes
without a cross-invocation lease, and daily budget updates occur after lookups
(`flightcheck/track.py:388–407`). Code inspection establishes the race; this review
did not run a concurrent hosted-Blob stress test.

**Smallest next decision:** use an actual conditional claim/reservation supported
by the chosen store, or demonstrate serialized execution. Do not add a new store
or provider without owner approval; an in-memory mutex does not serialize multiple
Vercel instances. Keep scope small and test the selected approach with a fake store
that deliberately interleaves competing workers.

**Acceptance:** overlapping ticks cannot duplicate paid work, lose budget charges
or regress the persisted checkpoint.

## Journey-owned consumption boundary

The owner confirms Ryukyu flights are untracked. That is a coverage limitation,
not an outage or cancellation. Keep approved schedule/manual status distinct from
live observations, and avoid repeated paid lookups for known unsupported segments.
An unavailable/untracked onward leg should expose an explicit reason instead of
an unexplained `connection: null`; partial inbound success is not connection
success. Preserve marketing and operating flight identities before interpreting
an operational record.

Journey still needs the confirmed booking-version bindings, typed flight adapter,
cache/freshness checks, durable verified inbox, notification selection, registered
bounded model-gateway operation and Telegram outbox. The checker owns polling;
the model does not decide access, recipients or booking changes. Live observations
never rewrite the published itinerary. Read-only/ad hoc integration can progress
against this boundary while reliable automatic tracking is completed.
