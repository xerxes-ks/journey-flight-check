#!/usr/bin/env bash
# End-to-end fixture smoke against $BASE.
# Requires: curl, python3, $FLIGHTCHECK_TOKEN
# Optional: $BASE (default http://127.0.0.1:8000)
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
BASE="${BASE%/}"
TOKEN="${FLIGHTCHECK_TOKEN:?set FLIGHTCHECK_TOKEN}"
AUTH=( -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" -H "X-Flightcheck-Mode: fixtures" )

fail() { echo "smoke FAIL: $*" >&2; exit 1; }
ok() { echo "smoke ok: $*"; }

py() {
  python3 -c "$1"
}

# curl -> writes body to $1, prints HTTP status on stdout
req() {
  local out="$1" method="$2" path="$3"; shift 3
  local tmp status
  tmp="$(mktemp)"
  status="$(curl -sS -o "$tmp" -w "%{http_code}" -X "$method" "$@" "${BASE}${path}" || true)"
  cat "$tmp" > "$out"
  rm -f "$tmp"
  echo "$status"
}

BODY="$(mktemp)"
trap 'rm -f "$BODY"' EXIT

# 1. health, no auth
status="$(req "$BODY" GET /api/health)"
[[ "$status" == "200" ]] || fail "health HTTP $status $(cat "$BODY")"
py "
import json,sys
d=json.load(open('$BODY'))
assert d.get('ok') is True, d
assert d.get('storage') in ('blob','memory','redis','none'), d
for k in ('aeroapi_key_configured','token_configured','webhook_secret_configured','cron_secret_configured'):
    assert isinstance(d.get(k), bool), (k,d)
print(d.get('storage'), d.get('mode'), d.get('sha') or d.get('version'))
" || fail "health body $(cat "$BODY")"
ok "health $(cat "$BODY" | tr '\n' ' ')"

# 2. 401 without token
status="$(req "$BODY" POST /api/check -H "Content-Type: application/json" --data '{"flight":"XX123","date":"2027-03-15","origin":"XXA"}')"
[[ "$status" == "401" ]] || fail "expected 401 without token, got $status $(cat "$BODY")"
ok "401 without token"

# 3. fixture check at T-24h
status="$(req "$BODY" POST /api/check "${AUTH[@]}" --data '{"flight":"XX123","date":"2027-03-15","origin":"XXA","now":"2027-03-14T18:00:00Z"}')"
[[ "$status" == "200" ]] || fail "check HTTP $status $(cat "$BODY")"
py "
import json
d=json.load(open('$BODY'))
assert d.get('ok') is True or d.get('status'), d
assert d.get('error') in (None, {}, []) or d.get('ok') is True or d.get('status') in ('scheduled','too_early','unknown','active'), d
" || fail "check body $(cat "$BODY")"
ok "fixture check"

# 4. track → tick at T-24h / T-3h / takeoff / landing → events → sink → untrack
HOOK="${BASE}/api/webhook-sink"
status="$(req "$BODY" POST /api/track "${AUTH[@]}" --data "{\"flight\":\"XX123\",\"date\":\"2027-03-15\",\"origin\":\"XXA\",\"webhook_url\":\"${HOOK}\",\"ref\":\"smoke\",\"now\":\"2027-03-14T18:00:00Z\"}")"
[[ "$status" == "200" ]] || fail "track HTTP $status $(cat "$BODY")"
TID="$(py "
import json
d=json.load(open('$BODY'))
assert d.get('ok') is True, d
assert d.get('tracking_id'), d
print(d['tracking_id'])
")" || fail "track body $(cat "$BODY")"
ok "track $TID"

tick_at() {
  local label="$1" now="$2"
  status="$(req "$BODY" POST /api/tick "${AUTH[@]}" --data "{\"now\":\"${now}\"}")"
  [[ "$status" == "200" ]] || fail "tick $label HTTP $status $(cat "$BODY")"
  py "
import json
d=json.load(open('$BODY'))
assert d.get('ok') is True, d
" || fail "tick $label body $(cat "$BODY")"
  ok "tick $label"
}

tick_at "T-24h" "2027-03-14T18:00:00Z"
tick_at "T-3h" "2027-03-15T15:00:00Z"
tick_at "takeoff" "2027-03-15T18:45:00Z"
tick_at "landing" "2027-03-15T21:00:00Z"

status="$(req "$BODY" GET "/api/tracked/${TID}/events" "${AUTH[@]}")"
[[ "$status" == "200" ]] || fail "events HTTP $status $(cat "$BODY")"
py "
import json
d=json.load(open('$BODY'))
assert d.get('ok') is True, d
ev=d.get('events') or []
assert ev, d
assert any(e.get('event')=='tracking_started' for e in ev), d
" || fail "events body $(cat "$BODY")"
ok "events"

status="$(req "$BODY" GET /api/webhook-sink "${AUTH[@]}")"
[[ "$status" == "200" ]] || fail "sink HTTP $status $(cat "$BODY")"
py "
import json
d=json.load(open('$BODY'))
assert d.get('ok') is True, d
items=d.get('deliveries') or []
assert items, 'no sink deliveries: %r' % d
assert any(i.get('signature_ok') for i in items), d
assert any(i.get('tracking_id')=='$TID' or (i.get('payload') or {}).get('tracking_id')=='$TID' for i in items), d
" || fail "sink body $(cat "$BODY")"
ok "webhook-sink signed deliveries"

status="$(req "$BODY" POST /api/untrack "${AUTH[@]}" --data "{\"tracking_id\":\"${TID}\"}")"
[[ "$status" == "200" ]] || fail "untrack HTTP $status $(cat "$BODY")"
py "
import json
d=json.load(open('$BODY'))
assert d.get('ok') is True, d
" || fail "untrack body $(cat "$BODY")"
ok "untrack"

echo "smoke PASS against $BASE"
