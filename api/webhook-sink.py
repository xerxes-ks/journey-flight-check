"""POST /api/webhook-sink — verify signature, keep last 50 deliveries.
GET lists them (Bearer token).
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from flightcheck.httpsvc import make_handler  # noqa: E402
from flightcheck.routes import handle_webhook_sink  # noqa: E402

handle = handle_webhook_sink
handler = make_handler(handle_webhook_sink, "api/webhook-sink")
