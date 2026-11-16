"""POST /api/track  — start tracking. DELETE /api/track?id= for untrack."""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from flightcheck.httpsvc import make_handler  # noqa: E402
from flightcheck.routes import handle_track  # noqa: E402

handle = handle_track
handler = make_handler(handle_track, "api/track")
