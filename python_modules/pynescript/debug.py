# Copyright (C) 2024-2026 jango_blockchained
#
# This file is part of pynescript.
#
# pynescript is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pynescript is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with pynescript.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Development-only debug logging toggle for the execution engine.

One env var — :data:`DEBUG_ENV` (``PYNE_DEBUG``) — turns on *detailed* engine
logging across :mod:`pynescript.runtime.host` (interpreter bar loop, delta
append / forming-bar ticks), :mod:`pynescript.compiler.engine` (compile cache,
prewarm) and :mod:`pynescript.util.datafeed` / :mod:`pynescript.util.data`.

Design rules
------------
1. **Off by default.** With the flag unset, :func:`scope_enabled` costs one
   memoized :func:`os.environ` lookup, and log payloads are never built. Call
   sites sit at run / batch / request boundaries, never per bar.
2. **Never raises.** A broken logging setup must not break ``/run``.
3. **Re-readable at runtime.** The cached value is keyed on the raw env string,
   so flipping ``PYNE_DEBUG`` in a live shell (or a test) takes effect without
   restarting the worker.

Example::

    PYNE_DEBUG=1 python -m backend.app
    PYNE_DEBUG=1 PYNE_DEBUG_SCOPE=pynescript.runtime,pynescript.compiler python -m backend.app
"""

from __future__ import annotations

import logging
import os

from typing import Any


__all__ = [
    "DEBUG_ENV",
    "SCOPE_ENV",
    "debug_enabled",
    "debug_scopes",
    "scope_enabled",
]


#: Environment variable that enables engine debug logging.
DEBUG_ENV = "PYNE_DEBUG"

#: Optional comma-separated allowlist of logger name prefixes.
#: Only consulted when ``PYNE_DEBUG`` is on. Empty = no filtering (all).
SCOPE_ENV = "PYNE_DEBUG_SCOPE"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off", ""})

# Cache keyed on the raw env string so a runtime change is picked up without a
# restart but a stable env costs one dict-compare per call.
#: Memoized env reads, keyed on the raw string so a runtime change is picked up
#: without a restart while a stable env costs one dict lookup. A dict (not
#: ``global`` scalars) keeps the mutation local and lint-clean.
_CACHE: dict[str, Any] = {"raw": None, "value": False, "scope_raw": None, "scopes": frozenset()}


def _parse_flag(raw: str | None) -> bool:
    """Parse a truthy env string. Unset/empty/``0``/``false``/``no``/``off`` -> False."""
    if raw is None:
        return False
    v = raw.strip().lower()
    if v in _TRUTHY:
        return True
    if v in _FALSY:
        return False
    # Unknown value: stay conservative (off) but keep it observable.
    logging.getLogger(__name__).debug("unrecognized %s=%r; treating as disabled", DEBUG_ENV, raw)
    return False


def debug_enabled() -> bool:
    """Whether detailed engine debug logging is on (``PYNE_DEBUG``)."""
    raw = os.environ.get(DEBUG_ENV)
    if raw != _CACHE["raw"]:
        _CACHE["raw"] = raw
        _CACHE["value"] = _parse_flag(raw)
    return bool(_CACHE["value"])


def debug_scopes() -> frozenset[str]:
    """Optional logger-prefix allowlist (``PYNE_DEBUG_SCOPE``).

    Empty frozenset means "no filter" — every ``pynescript.*`` logger is
    eligible. Multiple scopes are comma (or space) separated.
    """
    raw = os.environ.get(SCOPE_ENV)
    if raw != _CACHE["scope_raw"]:
        _CACHE["scope_raw"] = raw
        parts = [p for p in raw.replace(" ", ",").split(",") if p] if raw else []
        _CACHE["scopes"] = frozenset(parts)
    return _CACHE["scopes"]


def scope_enabled(logger_name: str) -> bool:
    """Whether ``logger_name`` passes the :func:`debug_scopes` allowlist.

    Disabled path: one memoized :func:`debug_enabled` lookup, nothing else.
    Call sites sit at run / batch / request boundaries — never inside a
    per-bar loop — so the gate stays negligible even on the hot session-append
    path.
    """
    if not debug_enabled():
        return False
    scopes = debug_scopes()
    if not scopes:
        return True
    return any(logger_name == s or logger_name.startswith(s) for s in scopes)


def reset_debug_caches() -> None:
    """Drop memoized env values (tests that mutate ``PYNE_DEBUG`` in-process)."""
    _CACHE.update({"raw": None, "value": False, "scope_raw": None, "scopes": frozenset()})
