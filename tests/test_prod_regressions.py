"""Regression tests for the production bugs found on main 83d0e0d."""

from __future__ import annotations

from flightcheck import check_flight
from flightcheck.exceptions import InvalidDate, StorageError, UNAUTH_MESSAGE
from flightcheck.model import parse_date, parse_time
from flightcheck.simulate import run_simulate
from flightcheck.store import reset_process_memory, use_store
from tests.conftest import NEAR_DEP, make_fetch, scenario_fetch

TOKEN = "prod-reg-token"


def test_track_storage_permission_error_is_503(monkeypatch):
    monkeypatch.setenv("FLIGHTCHECK_TOKEN", TOKEN)
    monkeypatch.setenv("FLIGHTCHECK_STORAGE", "memory")
    use_store(reset_process_memory())

    def boom(*a, **k):
        raise StorageError("Cannot use public access on a private store")

    monkeypatch.setattr("flightcheck.routes.start_track", boom)
    from flightcheck.routes import handle_track

    status, payload = handle_track(
        "POST",
        {"Authorization": f"Bearer {TOKEN}"},
        {},
        {"flight": "XX123", "date": "2027-03-15", "webhook_url": "https://example.test/h"},
        path="/api/track",
    )
    assert status == 503
    assert payload["error"]["code"] == "storage_error"
    assert payload["error"]["retryable"] is False
    use_store(None)


def test_origin_mismatch_is_not_success():
    r = check_flight(
        {"flight": "XX123", "date": "2027-03-15", "origin": "LHR"},
        fetch=scenario_fetch(),
        now=parse_time(NEAR_DEP),
    )
    assert r.ok is False
    assert r.error["code"] == "origin_mismatch"
    assert r.error["retryable"] is False
    assert "LHR" in r.error["message"]
    assert r.summary
    assert "temporarily unavailable" not in r.summary.lower()


def test_origin_mismatch_check_route_404(monkeypatch):
    import flightcheck.check as chk
    from api.check import handle

    monkeypatch.setenv("FLIGHTCHECK_TOKEN", TOKEN)
    orig = chk.check_flight

    def wrapped(req, **kw):
        kw.setdefault("fetch", scenario_fetch())
        kw.setdefault("now", parse_time(NEAR_DEP))
        return orig(req, **kw)

    monkeypatch.setattr(chk, "check_flight", wrapped)
    status, payload = handle(
        "POST",
        {"Authorization": f"Bearer {TOKEN}"},
        {},
        {"flight": "XX123", "date": "2027-03-15", "origin": "LHR"},
    )
    assert status == 404
    assert payload["error"]["code"] == "origin_mismatch"
    assert payload["ok"] is False


def test_401_wording_is_consistent(monkeypatch):
    monkeypatch.setenv("FLIGHTCHECK_TOKEN", TOKEN)
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    from api.check import handle as check_handle
    from flightcheck.routes import handle_tick, handle_track, handle_untrack

    cases = [
        check_handle("GET", {}, {"flight": "XX123", "date": "2027-03-15"}, None),
        handle_track("POST", {}, {}, {"flight": "XX123", "date": "2027-03-15"}),
        handle_untrack("POST", {}, {}, {"tracking_id": "trk_x"}),
        handle_tick("GET", {}, {}, {}, path="/api/tick"),
    ]
    for status, payload in cases:
        assert status == 401
        assert payload["error"]["code"] == "unauthorized"
        assert payload["error"]["message"] == UNAUTH_MESSAGE
        assert payload["error"]["retryable"] is False


def test_unknown_untrack_is_404(monkeypatch):
    monkeypatch.setenv("FLIGHTCHECK_TOKEN", TOKEN)
    monkeypatch.setenv("FLIGHTCHECK_STORAGE", "memory")
    use_store(reset_process_memory())
    from flightcheck.routes import handle_track, handle_untrack

    headers = {"Authorization": f"Bearer {TOKEN}"}
    status, payload = handle_untrack("POST", headers, {}, {"tracking_id": "trk_missing"})
    assert status == 404
    assert payload["error"]["code"] == "not_found"
    status, payload = handle_track(
        "DELETE", headers, {}, {}, path="/api/track/trk_missing",
    )
    assert status == 404
    assert payload["error"]["code"] == "not_found"
    use_store(None)


def test_unknown_ident_says_flight_not_found_not_outage():
    empty = make_fetch({"/flights/": (200, {"flights": []})})
    r = check_flight(
        {"flight": "ZZ999", "date": "2027-03-15", "origin": "XXA"},
        fetch=empty,
        now=parse_time(NEAR_DEP),
    )
    assert r.ok is False
    assert r.error["code"] == "flight_not_found"
    assert r.error["message"] == "flight not found"
    assert "isoformat" not in r.error["message"].lower()
    assert "flight not found" in r.summary.lower()
    assert "temporarily unavailable" not in r.summary.lower()

    missing = make_fetch({"/flights/ZZ999": (404, {"title": "not found"})})
    r404 = check_flight(
        {"flight": "ZZ999", "date": "2027-03-15"},
        fetch=missing,
        now=parse_time(NEAR_DEP),
    )
    assert r404.ok is False
    assert r404.error["code"] == "not_found"
    assert "flight not found" in r404.summary.lower()
    assert "temporarily unavailable" not in r404.summary.lower()


def test_bad_date_is_invalid_date_without_isoformat():
    try:
        parse_date("not-a-date")
        assert False, "expected InvalidDate"
    except InvalidDate as exc:
        assert exc.code == "invalid_date"
        assert "isoformat" not in str(exc).lower()
        assert exc.to_dict()["retryable"] is False


def test_check_route_bad_date_is_400_invalid_date(monkeypatch):
    monkeypatch.setenv("FLIGHTCHECK_TOKEN", TOKEN)
    from api.check import handle

    status, payload = handle(
        "POST",
        {"Authorization": f"Bearer {TOKEN}"},
        {},
        {"flight": "XX123", "date": "15/03/2027"},
    )
    assert status == 400
    assert payload["error"]["code"] == "invalid_date"
    assert "isoformat" not in payload["error"]["message"].lower()
    assert payload["error"]["retryable"] is False


def test_track_route_bad_date_is_400_invalid_date(monkeypatch):
    monkeypatch.setenv("FLIGHTCHECK_TOKEN", TOKEN)
    monkeypatch.setenv("FLIGHTCHECK_STORAGE", "memory")
    use_store(reset_process_memory())
    from flightcheck.routes import handle_track

    status, payload = handle_track(
        "POST",
        {"Authorization": f"Bearer {TOKEN}"},
        {},
        {"flight": "XX123", "date": "bogus", "webhook_url": "https://example.test/h"},
        path="/api/track",
    )
    assert status == 400
    assert payload["error"]["code"] == "invalid_date"
    assert "isoformat" not in payload["error"]["message"].lower()
    use_store(None)


def test_console_has_health_badge_sink_inbox_and_clock_stepper():
    import os
    html = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "public/test/index.html")).read()
    assert 'id="healthBadge"' in html
    assert "/api/health" in html
    assert 'id="sinkInbox"' in html
    assert "/api/webhook-sink" in html
    assert 'id="clockStepper"' in html
    assert 'id="clockPlus15"' in html
    assert 'data-step="15"' in html


def test_testing_md_says_production_alias_is_public():
    import os
    text = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs/TESTING.md")).read()
    assert "public" in text.lower() and "token-gated" in text.lower()
    assert "preview" in text.lower()
    assert "SSO" in text or "Vercel Authentication" in text
    assert "journey-flight-check.vercel.app" in text
    # Must not claim production sits behind SSO.
    block = text.split("## Production alias vs preview SSO")[-1]
    assert "production alias" in block.lower()
    assert "token-gated" in block.lower()


def test_simulate_diverted_names_original_destination():
    out = run_simulate({
        "scenario": "diverted",
        "flight": "XX123",
        "date": "2027-06-22",
        "origin": "LHR",
        "destination": "SIN",
        "std_local": "09:40",
    })
    texts = " ".join(step.get("summary") or "" for step in out["timeline"])
    assert "diverted to CDG" in texts
    assert "LHR→CDG diverted to CDG" not in texts.replace(" ", "")
    assert "LHR→CDG" not in texts
    assert "LHR→SIN" in texts


def test_simulate_retimed_landed_band_is_on_track():
    out = run_simulate({
        "scenario": "retimed",
        "flight": "XX123",
        "date": "2027-06-22",
        "origin": "LHR",
        "destination": "SIN",
        "std_local": "09:40",
    })
    landed = [
        step for step in out["timeline"]
        if step.get("status") == "landed" or step.get("checkpoint") in ("landing", "done")
    ]
    assert landed, out["timeline"]
    assert landed[-1]["band"] == "on_track"


def test_simulate_second_cancel_says_confirmed_cancelled():
    out = run_simulate({
        "scenario": "cancelled_then_confirmed",
        "flight": "XX123",
        "date": "2027-06-22",
        "origin": "LHR",
        "destination": "SIN",
        "std_local": "09:40",
    })
    summaries = [step.get("summary") or "" for step in out["timeline"]]
    cancelish = [s for s in summaries if "cancel" in s.lower()]
    assert any("shows as cancelled" in s for s in cancelish)
    assert any("confirmed cancelled" in s for s in cancelish)
    assert cancelish[-1].lower().count("shows as cancelled") == 0 or "confirmed cancelled" in cancelish[-1]
