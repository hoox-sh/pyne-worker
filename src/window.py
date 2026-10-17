# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Response windowing for the ``/run`` envelope (Pro API parity).

A full run over N bars returns every plot value for every bar. Callers that
only draw the visible range (or only need the newest bars) pay for the whole
history to read a screenful — and on the edge that also means the 5 MB
envelope cap and Workers response-size limits.

This module trims the **serialized envelope only**. The bar loop still runs
over every bar, so indicator values, strategy state, and NaN warm-up are
computed exactly as before — nothing about evaluation changes.

Windowing is opt-in via the ``from_bar`` / ``to_bar`` request fields. Absent,
the envelope is byte-identical to today.

Index convention matches Pine's own ``[]`` operator and AXIS plot apply:
``from_bar``/``to_bar`` are inclusive bar indices into the submitted data.
Negative indices count from the end (``-1`` is the last bar).

Ported from ``backend/middleware/window.py`` in hoox-sh/pyne (0.6.9) so the
edge host keeps the same ``window`` metadata contract.
"""

from __future__ import annotations

from typing import Any

# Envelope members that are per-bar vectors and therefore safe to slice.
# NB: no "bars" here — the worker envelope carries ``bars`` as a bar *count*
# (the Pro API has vector members with that name), and slicing an int would
# corrupt the response.
_SERIES_KEYS = ("series", "plots", "seriesMap")

# Envelope members carrying bar-indexed objects (dict or list).
# ``alerts`` is deliberately absent: firings must never be trimmed away.
_BARINDEXED_KEYS = ("events", "drawings", "tables", "fills", "boxes", "labels", "lines")

# Meta describing the window actually returned, so a client never has to guess
# whether it got everything.
WINDOW_META_KEY = "window"


def normalize_window(
    from_bar: Any,
    to_bar: Any,
    n_bars: int,
) -> tuple[int, int] | None:
    """Resolve requested indices to an inclusive ``(lo, hi)`` window.

    Returns ``None`` when no trimming should happen (absent request, whole
    dataset, or a degenerate range).

    Negative indices follow Python slicing: ``-1`` is the last bar. ``None``
    leaves that edge open.
    """
    if n_bars <= 0:
        return None
    if from_bar is None and to_bar is None:
        return None

    def _resolve(value: Any, fallback: int) -> int:
        if value is None:
            return fallback
        try:
            n = int(value)
        except (TypeError, ValueError):
            return fallback
        if n < 0:
            n += n_bars
        return max(0, min(n_bars - 1, n))

    lo = _resolve(from_bar, 0)
    hi = _resolve(to_bar, n_bars - 1)
    if hi < lo:
        lo, hi = hi, lo
    if lo == 0 and hi == n_bars - 1:
        return None  # already the whole thing — return the envelope untouched
    return lo, hi


def _slice_bars(value: Any, lo: int, hi: int) -> Any:
    """Slice a per-bar container (leaving anything else untouched).

    ``series`` is a dict keyed by opaque plot ids (``plot_0``, ``rsi``), so
    for dicts the keys are kept and each value array is sliced — filtering
    keys by bar index would empty the whole series.
    """
    if isinstance(value, list):
        return value[lo : hi + 1]
    if isinstance(value, dict):
        return {k: (v[lo : hi + 1] if isinstance(v, list) else v) for k, v in value.items()}
    return value


def _slice_bindexed(value: Any, lo: int, hi: int) -> Any:
    """Keep only bar-indexed objects inside ``[lo, hi]``.

    Objects without a recognizable index are kept: dropping them would
    silently lose a fill or label the chart still needs.
    """
    if not isinstance(value, list):
        return value
    kept: list[Any] = []
    for item in value:
        idx = _object_bar_index(item)
        if idx is None or lo <= idx <= hi:
            kept.append(item)
    return kept


def _object_bar_index(item: Any) -> int | None:
    if not isinstance(item, dict):
        return None
    for key in ("bar_index", "barIndex", "index", "bar"):
        if key in item:
            try:
                return int(item[key])
            except (TypeError, ValueError):
                return None
    # Strategy events carry bar_time, not an index — keep them (return None).
    return None


def _envelope_bar_count(envelope: dict[str, Any]) -> int:
    """Bars in this envelope — the engine's own count, else a series length."""
    n_bars = envelope.get("count")
    if isinstance(n_bars, int) and n_bars > 0:
        return n_bars
    series = envelope.get("series")
    if isinstance(series, dict) and series:
        first = next(iter(series.values()))
        if isinstance(first, list):
            return len(first)
    return 0


def _mark_profile_windowed(out: dict[str, Any], lo: int, hi: int, n_bars: int) -> None:
    """Rewrite `meta.profile.bars` to the windowed length, and flag it."""
    meta = out.get("meta")
    if not isinstance(meta, dict):
        return
    meta = dict(meta)
    profile = meta.get("profile")
    if isinstance(profile, dict) and profile.get("bars") == n_bars:
        profile = dict(profile)
        profile["bars"] = hi - lo + 1
        profile["windowed"] = True
        meta["profile"] = profile
    out["meta"] = meta


def apply_window(
    envelope: dict[str, Any],
    from_bar: Any = None,
    to_bar: Any = None,
) -> dict[str, Any]:
    """Return ``envelope`` trimmed to the requested inclusive bar window.

    Returns the original object unchanged when no window applies, so the
    common (unwindowed) path costs nothing and cannot change behaviour.
    """
    if not isinstance(envelope, dict):
        return envelope

    n_bars = _envelope_bar_count(envelope)
    if n_bars <= 0:
        return envelope

    win = normalize_window(from_bar, to_bar, n_bars)
    if win is None:
        return envelope
    lo, hi = win

    out = dict(envelope)
    for key in _SERIES_KEYS:
        if key in out:
            out[key] = _slice_bars(out[key], lo, hi)
    for key in _BARINDEXED_KEYS:
        if key in out:
            out[key] = _slice_bindexed(out[key], lo, hi)

    _mark_profile_windowed(out, lo, hi, n_bars)
    out[WINDOW_META_KEY] = {
        "from_bar": lo,
        "to_bar": hi,
        "bars": hi - lo + 1,
        "total_bars": n_bars,
    }
    return out


__all__ = ["WINDOW_META_KEY", "apply_window", "normalize_window"]
