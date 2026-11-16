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
    assert "FLIGHTAWARE_AEROAPI_KEY" in proc.stderr
