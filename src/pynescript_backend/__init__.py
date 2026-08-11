# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pynescript runtime glue for the Cloudflare edge host.

**H1 (2026-08):** bar-loop SoT is the installable package
(:mod:`pynescript.runtime`). This package is a thin edge wrap:

- :class:`Runtime` — package Runtime + strict OHLCV validation
- ``timeout_seconds`` — package Runtime circuit breaker (shared with Pro API)

Keep ``./scripts/sync_vendor.sh`` in the deploy path so ``python_modules/``
ships the same package tree Wrangler vendors.
"""

from .runtime import Runtime

__all__ = ["Runtime"]
