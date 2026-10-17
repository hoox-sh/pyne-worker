# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for ``/run`` response windowing (Pro API parity, pynescript 0.6.9).

Two properties matter and are pinned here:

1. The **bar loop is untouched**. Windowing happens after evaluation, so a
   windowed envelope is exactly the corresponding slice of the full one — not
   a cheaper computation.
2. **Absent window ⇒ identical output.** The unwindowed path must not change
   one byte of the envelope.
"""

from __future__ import annotations

import asyncio
import json

from handler import handle_request
from window import WINDOW_META_KEY
from window import apply_window
from window import normalize_window


def _envelope(n: int = 10) -> dict:
    """A representative edge envelope: series, plots, events, drawings, and
    the worker's integer ``bars`` count (must never be sliced)."""
    return {
        "status": "success",
        "count": n,
        "bars": n,
        "series": {"plot_0": list(range(n)), "plot_1": [v * 2 for v in range(n)]},
        "plots": list(range(n)),
        "events": [{"kind": "entry", "bar_index": i, "direction": "long"} for i in range(n)],
        "drawings": [{"id": f"d{i}", "bar_index": i} for i in range(n)],
        "alerts": [{"message": "m", "bar_index": i} for i in range(n)],
        "meta": {"profile": {"bars": n, "total_ms": 12.5}},
    }


class TestNormalizeWindow:
    def test_none_none_is_no_window(self) -> None:
        assert normalize_window(None, None, 100) is None

    def test_full_range_is_no_window(self) -> None:
        assert normalize_window(0, 99, 100) is None

    def test_explicit_slice(self) -> None:
        assert normalize_window(10, 19, 100) == (10, 19)

    def test_negative_indices_count_from_end(self) -> None:
        assert normalize_window(-10, -1, 100) == (90, 99)

    def test_open_edges(self) -> None:
        assert normalize_window(None, 49, 100) == (0, 49)
        assert normalize_window(50, None, 100) == (50, 99)

    def test_reversed_range_is_normalized(self) -> None:
        assert normalize_window(80, 20, 100) == (20, 80)

    def test_out_of_range_is_clamped(self) -> None:
        assert normalize_window(-500, 5000, 100) is None

    def test_garbage_falls_back_to_no_window(self) -> None:
        assert normalize_window("abc", None, 100) is None

    def test_zero_bars(self) -> None:
        assert normalize_window(0, 10, 0) is None


class TestApplyWindow:
    def test_no_window_returns_same_object(self) -> None:
        env = _envelope()
        assert apply_window(env) is env
        assert WINDOW_META_KEY not in env

    def test_full_range_window_returns_same_object(self) -> None:
        env = _envelope()
        assert apply_window(env, 0, 9) is env

    def test_series_are_sliced(self) -> None:
        out = apply_window(_envelope(), 2, 4)
        assert out["series"]["plot_0"] == [2, 3, 4]
        assert out["series"]["plot_1"] == [4, 6, 8]

    def test_plots_are_sliced(self) -> None:
        out = apply_window(_envelope(), 2, 4)
        assert out["plots"] == [2, 3, 4]

    def test_bars_count_is_untouched(self) -> None:
        # The worker envelope carries ``bars`` as an evaluated-bar count, not a
        # per-bar vector; slicing it would corrupt the response.
        assert apply_window(_envelope(), 2, 4)["bars"] == 10

    def test_bar_indexed_events_are_filtered(self) -> None:
        out = apply_window(_envelope(), 2, 4)
        assert [e["bar_index"] for e in out["events"]] == [2, 3, 4]

    def test_drawings_are_filtered(self) -> None:
        out = apply_window(_envelope(), 2, 4)
        assert [d["bar_index"] for d in out["drawings"]] == [2, 3, 4]

    def test_alerts_are_never_trimmed(self) -> None:
        # Firings must survive a window: dropping them could silently lose a
        # webhook notification.
        out = apply_window(_envelope(), 2, 4)
        assert len(out["alerts"]) == 10

    def test_events_without_index_are_kept(self) -> None:
        env = _envelope()
        env["events"].append({"kind": "exit", "bar_time": 1700000000})
        out = apply_window(env, 2, 4)
        assert any("bar_time" in e for e in out["events"])

    def test_window_meta_reports_the_trim(self) -> None:
        out = apply_window(_envelope(), 2, 4)
        assert out[WINDOW_META_KEY] == {
            "from_bar": 2,
            "to_bar": 4,
            "bars": 3,
            "total_bars": 10,
        }

    def test_profile_bars_reflects_the_window(self) -> None:
        out = apply_window(_envelope(), 2, 4)
        assert out["meta"]["profile"]["bars"] == 3
        assert out["meta"]["profile"]["windowed"] is True

    def test_windowed_slice_equals_full_slice(self) -> None:
        full = _envelope()
        out = apply_window(full, 3, 6)
        assert out["series"]["plot_0"] == full["series"]["plot_0"][3:7]
        assert out["events"] == full["events"][3:7]


def _bars(n: int = 30) -> list[dict]:
    return [
        {
            "open": 100 + i,
            "high": 102 + i,
            "low": 99 + i,
            "close": 101 + i,
            "time": 1_700_000_000 + i * 86_400,
            "volume": 10.0,
        }
        for i in range(n)
    ]


async def _post_run(body: dict) -> tuple[dict, int]:
    payload, status, _headers = await handle_request(
        "POST",
        "/run",
        json.dumps(body),
        api_key="test-key-123",
        expected_api_key="test-key-123",
    )
    return payload, status


class TestRunWindowIntegration:
    SCRIPT = "//@version=6\nindicator('t')\nplot(close, 'C')"

    async def test_no_window_keeps_envelope_unchanged(self) -> None:
        body, status = await _post_run({"script": self.SCRIPT, "ohlcv": _bars()})
        assert status == 200
        assert WINDOW_META_KEY not in body
        assert body["count"] == 30

    async def test_window_trims_series_and_reports_meta(self) -> None:
        body, status = await _post_run(
            {"script": self.SCRIPT, "ohlcv": _bars(), "from_bar": -5}
        )
        assert status == 200
        assert body["window"] == {
            "from_bar": 25,
            "to_bar": 29,
            "bars": 5,
            "total_bars": 30,
        }
        # Count stays bars *evaluated* — the window only trims the envelope.
        assert body["count"] == 30
        assert len(body["plots"]) == 5
        assert len(body["series"]["C"]) == 5

    async def test_windowed_tail_equals_full_tail(self) -> None:
        full, _ = await _post_run({"script": self.SCRIPT, "ohlcv": _bars()})
        tail, _ = await _post_run(
            {"script": self.SCRIPT, "ohlcv": _bars(), "from_bar": -5}
        )
        assert tail["series"]["C"] == full["series"]["C"][-5:]

    async def test_invalid_window_type_400(self) -> None:
        body, status = await _post_run(
            {"script": self.SCRIPT, "ohlcv": _bars(), "from_bar": "abc"}
        )
        assert status == 400
        assert "from_bar" in body.get("error", "")

    async def test_bool_window_rejected(self) -> None:
        body, status = await _post_run(
            {"script": self.SCRIPT, "ohlcv": _bars(), "to_bar": True}
        )
        assert status == 400
        assert "to_bar" in body.get("error", "")

    async def test_reversed_and_full_windows_are_normalized(self) -> None:
        body, status = await _post_run(
            {"script": self.SCRIPT, "ohlcv": _bars(), "from_bar": 0, "to_bar": 29}
        )
        assert status == 200
        assert WINDOW_META_KEY not in body  # whole range → untouched envelope

        body, status = await _post_run(
            {"script": self.SCRIPT, "ohlcv": _bars(), "from_bar": 4, "to_bar": 2}
        )
        assert status == 200
        assert body["window"]["from_bar"] == 2
        assert body["window"]["to_bar"] == 4
