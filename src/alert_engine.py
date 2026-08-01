# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert engine helpers for pyne-worker.

Runtime collects ``alert()`` / true ``alertcondition()`` firings from the
pynescript evaluator. Cron jobs typically only want firings on the **latest
closed bar** so webhooks do not re-fire historical history every minute.
"""

from __future__ import annotations

from typing import Any


def filter_alerts_for_bar(
    alerts: list[Any],
    bar_time: int | None,
    *,
    bar_index: int | None = None,
) -> list[dict[str, Any]]:
    """Return alert dicts that fired on *bar_time* (preferred) or *bar_index*.

    When *bar_time* is set, match ``alert["time"]``. Otherwise match
    ``alert["bar_index"]``. If both filters are ``None``, return a shallow
    copy of all dict alerts (full-history mode for ``POST /run``).
    """
    if not alerts:
        return []
    out: list[dict[str, Any]] = []
    for a in alerts:
        if not isinstance(a, dict):
            continue
        if bar_time is not None:
            try:
                at = int(a.get("time") if a.get("time") is not None else -1)
            except (TypeError, ValueError):
                at = -1
            if at != int(bar_time):
                continue
        elif bar_index is not None:
            try:
                bi = int(a.get("bar_index") if a.get("bar_index") is not None else -1)
            except (TypeError, ValueError):
                bi = -1
            if bi != int(bar_index):
                continue
        out.append(dict(a))
    return out


def alerts_summary(alerts: list[Any]) -> dict[str, Any]:
    """Compact counts for health / cron summaries."""
    if not alerts:
        return {"count": 0, "by_source": {}, "by_freq": {}}
    by_source: dict[str, int] = {}
    by_freq: dict[str, int] = {}
    n = 0
    for a in alerts:
        if not isinstance(a, dict):
            continue
        n += 1
        src = str(a.get("source") or "alert")
        freq = str(a.get("freq") or "once_per_bar")
        by_source[src] = by_source.get(src, 0) + 1
        by_freq[freq] = by_freq.get(freq, 0) + 1
    return {"count": n, "by_source": by_source, "by_freq": by_freq}
