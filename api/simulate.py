"""POST /api/simulate  — zero-cost essential-cadence replay. Requires token."""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from flightcheck.httpsvc import make_handler  # noqa: E402
from flightcheck.routes import handle_simulate  # noqa: E402

handle = handle_simulate
handler = make_handler(handle_simulate, "api/simulate")
