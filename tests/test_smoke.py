"""Run scripts/smoke.sh against the stdlib ThreadingHTTPServer."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_smoke_script_against_dev_server():
    port = _free_port()
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": ROOT,
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "FLIGHTCHECK_TOKEN": "smoke-token",
        "FLIGHTCHECK_WEBHOOK_SECRET": "smoke-secret",
        "FLIGHTCHECK_STORAGE": "memory",
        "FLIGHTCHECK_MODE": "live",
        "CRON_SECRET": "smoke-cron",
    })
    env.pop("BLOB_READ_WRITE_TOKEN", None)
    env.pop("FLIGHTAWARE_AEROAPI_KEY", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "flightcheck.dev"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 8
        last = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=1) as resp:
                    if resp.status == 200:
                        break
            except Exception as exc:
                last = exc
                time.sleep(0.1)
        else:
            pytest.fail(f"dev server did not start: {last}")
        smoke = subprocess.run(
            ["bash", os.path.join(ROOT, "scripts/smoke.sh")],
            cwd=ROOT,
            env={**env, "BASE": base, "FLIGHTCHECK_TOKEN": "smoke-token"},
            capture_output=True,
            text=True,
            timeout=60,
        )
        if smoke.returncode != 0:
            pytest.fail(
                "smoke.sh failed\n"
                f"stdout:\n{smoke.stdout}\n"
                f"stderr:\n{smoke.stderr}"
            )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
