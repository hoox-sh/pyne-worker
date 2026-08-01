# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert engine tests for pyne-worker Runtime + helpers."""

from __future__ import annotations

from alert_engine import alerts_summary
from alert_engine import filter_alerts_for_bar
from pynescript_backend import Runtime


def _bars(n: int = 20) -> list[dict]:
    out: list[dict] = []
    price = 100.0
    for i in range(n):
        o = price
        c = price + (0.5 if i % 2 == 0 else -0.3)
        out.append(
            {
                "open": o,
                "high": max(o, c) + 0.5,
                "low": min(o, c) - 0.5,
                "close": c,
                "time": 1_700_000_000_000 + i * 60_000,
                "volume": 1000.0,
            }
        )
        price = c
    return out


class TestRuntimeAlerts:
    def test_alert_collected(self) -> None:
        src = """//@version=5
indicator("a")
if bar_index == 5
    alert("hello")
plot(close)
"""
        r = Runtime().run(src, _bars(10), mode="interpret")
        assert "error" not in r, r.get("error")
        alerts = r.get("alerts") or []
        assert len(alerts) >= 1
        assert any(a.get("message") == "hello" for a in alerts)
        a0 = next(a for a in alerts if a.get("message") == "hello")
        assert a0.get("bar_index") == 5
        assert a0.get("source") == "alert"
        assert a0.get("freq") in ("once_per_bar", "freq_once_per_bar")

    def test_alert_once_per_bar_dedup(self) -> None:
        src = """//@version=5
indicator("a")
alert("x")
alert("x")
plot(close)
"""
        r = Runtime().run(src, _bars(3), mode="interpret")
        assert "error" not in r, r.get("error")
        alerts = r.get("alerts") or []
        # one per bar, not two per bar
        by_bar: dict[int, int] = {}
        for a in alerts:
            if a.get("message") == "x":
                bi = int(a.get("bar_index") or 0)
                by_bar[bi] = by_bar.get(bi, 0) + 1
        assert by_bar
        assert all(c == 1 for c in by_bar.values())

    def test_alertcondition_fires_when_true(self) -> None:
        src = """//@version=5
indicator("a")
alertcondition(bar_index == 2, "t", "msg")
plot(close)
"""
        r = Runtime().run(src, _bars(5), mode="interpret")
        assert "error" not in r, r.get("error")
        alerts = r.get("alerts") or []
        fired = [a for a in alerts if a.get("source") == "alertcondition"]
        assert any(a.get("message") == "msg" for a in fired)
        conds = r.get("alert_conditions") or []
        assert any(c.get("condition") is True for c in conds)

    def test_freq_all_allows_multi(self) -> None:
        src = """//@version=5
indicator("a")
alert("m", alert.freq_all)
alert("m", alert.freq_all)
plot(close)
"""
        r = Runtime().run(src, _bars(2), mode="interpret")
        assert "error" not in r, r.get("error")
        alerts = [a for a in (r.get("alerts") or []) if a.get("message") == "m"]
        # 2 bars × 2 calls = 4 when freq_all
        assert len(alerts) >= 4


class TestAlertEngineHelpers:
    def test_filter_by_bar_time(self) -> None:
        alerts = [
            {"message": "a", "time": 100, "bar_index": 0},
            {"message": "b", "time": 200, "bar_index": 1},
            {"message": "c", "time": 200, "bar_index": 1},
        ]
        out = filter_alerts_for_bar(alerts, 200)
        assert len(out) == 2
        assert {a["message"] for a in out} == {"b", "c"}

    def test_summary(self) -> None:
        s = alerts_summary(
            [
                {"message": "a", "source": "alert", "freq": "once_per_bar"},
                {"message": "b", "source": "alertcondition", "freq": "all"},
            ]
        )
        assert s["count"] == 2
        assert s["by_source"]["alert"] == 1
        assert s["by_source"]["alertcondition"] == 1
