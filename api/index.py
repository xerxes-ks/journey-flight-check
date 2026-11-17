"""Vercel Python entrypoint. Dispatches every /api/* path.

[tool.vercel] entrypoint = "api.index:handler"
Do not point the entrypoint at api.check:handler — that made health and cron 401.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from flightcheck.dispatch import dispatch  # noqa: E402
from flightcheck.httpsvc import make_handler  # noqa: E402

handle = dispatch
handler = make_handler(dispatch, "api")
