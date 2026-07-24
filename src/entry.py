"""pyne-worker — experimental Python Cloudflare Worker for Pine Script evaluation.

Runs TradingView Pine Script strategy scripts via the pynescript reference
evaluator and emits structured trade events. Mirrors the pine-worker API
surface (``/health``, ``POST /run``) while staying in 100% Python.

Plan 2 sibling to pine-worker (TypeScript port):
https://github.com/jango-blockchained/pynescript
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from workers import Response
from workers import WorkerEntrypoint

from handler import handle_request
from trade_forwarder import forward_events


class Default(WorkerEntrypoint):
    TRADE_SERVICE: object  # service binding, set by the runtime
    OHLCV_DATA: object  # R2 bucket binding, set by the runtime

    async def fetch(self, request):
        path = urlparse(request.url).path or "/"

        body = None
        if request.method == "POST":
            body = await request.text()

        payload, status = handle_request(
            request.method,
            path,
            body,
            r2_bucket=getattr(self.env, "OHLCV_DATA", None),
        )

        # Forward strategy events to trade-worker if present
        events = payload.get("events", [])
        if events and hasattr(self.env, "TRADE_SERVICE"):
            symbol = payload.get("symbol", "BTCUSDT")
            try:
                fwd = await forward_events(
                    events,
                    self.env.TRADE_SERVICE,
                    symbol=symbol,
                )
                if fwd.get("failed", 0) > 0:
                    payload["forward_errors"] = fwd.get("errors", [])
                    payload["forwarded"] = fwd.get("forwarded", 0)
                    payload["forward_failed"] = fwd.get("failed", 0)
            except Exception as e:
                payload["forward_error"] = str(e)

        return Response(
            json.dumps(payload),
            status=status,
            headers={"Content-Type": "application/json"},
        )
