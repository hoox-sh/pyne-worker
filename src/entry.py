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


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        path = urlparse(request.url).path or "/"

        body = None
        if request.method == "POST":
            body = await request.text()

        payload, status = handle_request(request.method, path, body)
        return Response(
            json.dumps(payload),
            status=status,
            headers={"Content-Type": "application/json"},
        )