"""GET /api/tracked  and GET /api/tracked?id=."""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from flightcheck.httpsvc import make_handler  # noqa: E402
from flightcheck.routes import handle_tracked  # noqa: E402

handle = handle_tracked
handler = make_handler(handle_tracked, "api/tracked")
