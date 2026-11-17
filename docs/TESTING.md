# Testing

Synthetic flights only (`XX123`, `YY456`, `XX321`). No live trip data, no secrets in the repo.

## pytest

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Stdlib-only runtime. Tests inject AeroAPI `fetch` and a fake Blob HTTP server; CI never calls FlightAware or Vercel Blob. `tests/test_vercel_routes.py` asserts there is **no** `[tool.vercel] entrypoint` (that bug served every `/api/*` path with `api/check.py`) and that each `api/*.py` function plus the `/api/tick` cron is declared. After a Vercel deploy, `.github/workflows/preview-health.yml` hits `/api/health` with `x-vercel-protection-bypass` when the `VERCEL_AUTOMATION_BYPASS_SECRET` GitHub secret is set.

## Stdlib dev server

Single process, threaded so `POST /api/track` can deliver to `$BASE/api/webhook-sink` on the same port.

```bash
export FLIGHTCHECK_TOKEN=dev
export FLIGHTCHECK_WEBHOOK_SECRET=dev
export FLIGHTCHECK_STORAGE=memory
export FLIGHTCHECK_MODE=live   # per-request X-Flightcheck-Mode: fixtures still works
export PORT=8000
python3 -m flightcheck.dev
```

Then `GET http://127.0.0.1:8000/test` and the `/api/*` routes.

## `vercel dev`

Copy `.env.example` to `.env.local`. Set `BLOB_READ_WRITE_TOKEN` (Vercel Blob private store), `FLIGHTCHECK_TOKEN`, `FLIGHTCHECK_WEBHOOK_SECRET`, `CRON_SECRET`. Leave `FLIGHTCHECK_STORAGE` unset so Blob is the default. `vercel.json` maps:

- `/test` → `public/test/index.html`
- `/api/*.py` Python functions (`check`, `health`, `track`, `tick`, `simulate`, `webhook-sink`, …)
- Cron `*/5 * * * *` → `/api/tick`

```bash
vercel dev
export BASE=http://localhost:3000
export FLIGHTCHECK_TOKEN=…   # same value as .env.local
bash scripts/smoke.sh
```

## Fixture walk (zero AeroAPI spend)

Send `X-Flightcheck-Mode: fixtures` with a valid token, or set `FLIGHTCHECK_MODE=fixtures`. Pass `now` (ISO; naive values use `origin`’s tz) on `/api/check` and `/api/tick`. Live mode ignores `now`.

Walk: track → tick at T−24h / T−3h / takeoff / landing → events → webhook-sink → untrack.

## `scripts/smoke.sh`

Exit non-zero on failure.

```bash
export BASE=http://127.0.0.1:8000
export FLIGHTCHECK_TOKEN=dev
bash scripts/smoke.sh
```

It hits health (no auth), expects 401 on `/api/check` without a token, then a fixture check, then the track/tick/events/sink/untrack walk against `XX123` on `2027-03-15`.

## Preview / Vercel Authentication

The production and preview deployments sit behind **Vercel Authentication** (SSO / login HTML). That gate runs *before* `/api/health`. An external tester (Codex, `curl`, CI) cannot reach JSON until one of:

1. A **Protection Bypass for Automation** header (`x-vercel-protection-bypass`) is configured on the project and shared out of band — not committed here.
2. The tester is logged into Vercel in a browser and uses the preview URL there.
3. `vercel curl` / `npx vercel curl` as a project member.

Self-webhook deliveries (`webhook_url=$BASE/api/webhook-sink`) from the preview also need that bypass; the function’s outbound POST has no SSO cookie. Local `python3 -m flightcheck.dev` and `vercel dev` do not have this gate.

Share `FLIGHTCHECK_TOKEN` and the bypass out of band. Never put them in this public repo.
