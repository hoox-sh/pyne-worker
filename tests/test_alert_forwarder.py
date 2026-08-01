# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for alert webhook productization (L2)."""

from __future__ import annotations

import asyncio

from alert_forwarder import build_alert_payload
from alert_forwarder import forward_alert_groups
from alert_forwarder import forward_alerts
from alert_forwarder import group_alerts_by_webhook


class TestBuildPayload:
    def test_basic(self) -> None:
        p = build_alert_payload(
            {
                "message": "hi",
                "freq": "once_per_bar",
                "source": "alert",
                "bar_index": 3,
                "time": 100,
                "symbol": "BTCUSDT",
            }
        )
        assert p["type"] == "pine_alert"
        assert p["message"] == "hi"
        assert p["content"] == "hi"
        assert p["bar_index"] == 3
        assert p["symbol"] == "BTCUSDT"
        assert p["alert_source"] == "alert"

    def test_title_content(self) -> None:
        p = build_alert_payload({"message": "m", "title": "T", "source": "alertcondition"})
        assert p["content"] == "**T**: m"
        assert p["alert_source"] == "alertcondition"


class TestGroupAndForward:
    def test_group_by_url(self) -> None:
        alerts = [
            {"message": "a", "webhook_url": "https://a.example/"},
            {"message": "b", "webhook_url": "https://b.example/"},
            {"message": "c", "webhook_url": "https://a.example/"},
            {"message": "skip", "forward_alerts": False},
        ]
        g = group_alerts_by_webhook(alerts, default_url="https://default/")
        assert set(g) == {"https://a.example/", "https://b.example/"}
        assert len(g["https://a.example/"]) == 2

    def test_default_url(self) -> None:
        g = group_alerts_by_webhook(
            [{"message": "x"}],
            default_url="https://default/",
        )
        assert list(g.keys()) == ["https://default/"]

    def test_forward_batch(self) -> None:
        posted: list[tuple[str, dict]] = []

        async def post(url: str, body: dict) -> int:
            posted.append((url, body))
            return 200

        meta = asyncio.run(
            forward_alerts(
                [
                    {"message": "one", "bar_index": 1},
                    {"message": "two", "bar_index": 2},
                ],
                "https://hooks.test/pine",
                http_post_json=post,
                batch=True,
            )
        )
        assert meta["forwarded"] == 2
        assert meta["failed"] == 0
        assert len(posted) == 1
        assert posted[0][1]["type"] == "pine_alert_batch"
        assert posted[0][1]["count"] == 2

    def test_forward_groups(self) -> None:
        posted: list[str] = []

        async def post(url: str, body: dict) -> int:
            posted.append(url)
            return 204

        meta = asyncio.run(
            forward_alert_groups(
                [
                    {"message": "a", "webhook_url": "https://a/"},
                    {"message": "b"},
                ],
                default_url="https://default/",
                http_post_json=post,
            )
        )
        assert meta["destinations"] == 2
        assert meta["forwarded"] == 2
        assert set(posted) == {"https://a/", "https://default/"}

    def test_http_failure(self) -> None:
        async def post(url: str, body: dict) -> int:
            return 500

        meta = asyncio.run(
            forward_alerts(
                [{"message": "x"}],
                "https://bad/",
                http_post_json=post,
            )
        )
        assert meta["failed"] == 1
        assert meta["forwarded"] == 0
