import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_cli_help():
    out = subprocess.run(
        [sys.executable, "-m", "flightcheck", "check", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "flight" in out.stdout
    assert "--budget" in out.stdout
    assert "--json" in out.stdout
    assert "--onward" in out.stdout
    assert "--now" in out.stdout


def test_cli_without_key_is_unknown():
    env = {k: v for k, v in os.environ.items() if k != "FLIGHTAWARE_AEROAPI_KEY"}
    proc = subprocess.run(
        [sys.executable, "-m", "flightcheck", "check", "XX123", "2027-03-15",
         "--json", "--now", "2027-03-15T16:30:00Z"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)
    assert data["flight"]["status"] in ("unknown", "too_early")
    assert data["prediction"]["band"] == "unknown"
    assert "offline" in proc.stderr.lower()
    assert data["cost_usd_est"] == 0


def test_cli_help_mentions_live():
    out = subprocess.run(
        [sys.executable, "-m", "flightcheck", "check", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--live" in out.stdout
    assert "--now" in out.stdout


def test_cli_now_stays_offline_without_live(monkeypatch, capsys):
    monkeypatch.setenv("FLIGHTAWARE_AEROAPI_KEY", "fake-live-key")
    hits = []

    def boom(self, path, params):
        hits.append(path)
        raise AssertionError("network should not run")

    monkeypatch.setattr("flightcheck.adapters.aeroapi.AeroAPIAdapter._http", boom)
    from flightcheck.cli import main

    rc = main(["check", "XX123", "2027-03-15", "--now", "2027-03-15T16:30:00Z", "--json"])
    assert rc == 0
    assert hits == []
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["flight"]["status"] in ("unknown", "too_early")
    assert data["next_check_local"] is not None
    assert data["cost_usd_est"] == 0
    assert "offline" in captured.err.lower()


def test_cli_now_with_live_calls_adapter(monkeypatch, capsys):
    monkeypatch.setenv("FLIGHTAWARE_AEROAPI_KEY", "fake-live-key")
    hits = []

    def fake_http(self, path, params):
        hits.append(path)
        return 200, {"flights": []}

    monkeypatch.setattr("flightcheck.adapters.aeroapi.AeroAPIAdapter._http", fake_http)
    from flightcheck.cli import main

    rc = main([
        "check", "XX123", "2027-03-15",
        "--now", "2027-03-15T16:30:00Z", "--live", "--json",
    ])
    assert rc == 0
    assert hits
    data = json.loads(capsys.readouterr().out)
    assert data["flight"]["status"] in ("unknown", "too_early")
