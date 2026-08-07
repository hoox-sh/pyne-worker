# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Security hardening tests — path traversal, SSRF webhooks, auth compare."""

from __future__ import annotations

import asyncio
import json

from alert_forwarder import forward_alerts
from alert_forwarder import group_alerts_by_webhook
from data_provider import _ohlcv_key
from data_provider import ingest_ohlcv_to_r2
from handler import handle_request
from middleware import validate_api_key
from security import sanitize_symbol
from security import sanitize_timeframe
from security import validate_webhook_url


class TestSanitizeSymbol:
    def test_ok(self) -> None:
        assert sanitize_symbol("btcusdt") == "BTCUSDT"
        assert sanitize_symbol("BINANCE:BTCUSDT") == "BINANCE:BTCUSDT"

    def test_path_traversal(self) -> None:
        assert sanitize_symbol("../evil") is None
        assert sanitize_symbol("foo/bar") is None
        assert sanitize_symbol("foo\\bar") is None
        assert sanitize_symbol("..") is None
        assert sanitize_symbol("") is None
        assert sanitize_symbol(None) is None

    def test_too_long(self) -> None:
        assert sanitize_symbol("A" * 40) is None


class TestSanitizeTimeframe:
    def test_ok(self) -> None:
        assert sanitize_timeframe("1m") == "1m"
        assert sanitize_timeframe("1d") == "1d"

    def test_reject(self) -> None:
        assert sanitize_timeframe("../1m") is None
        assert sanitize_timeframe("1min") is None
        assert sanitize_timeframe("") is None


class TestOhlcvKey:
    def test_builds(self) -> None:
        assert _ohlcv_key("btcusdt", "1m", 2026) == "data/BTCUSDT/1m/2026.jsonl"

    def test_rejects_traversal(self) -> None:
        try:
            _ohlcv_key("../x", "1m", 2026)
            assert False, "expected ValueError"
        except ValueError:
            pass
        try:
            _ohlcv_key("BTCUSDT", "../../etc", 2026)
            assert False, "expected ValueError"
        except ValueError:
            pass


class TestWebhookUrl:
    def test_https_public_ok(self) -> None:
        assert validate_webhook_url("https://hooks.example.com/pine") == (
            "https://hooks.example.com/pine"
        )

    def test_rejects_http(self) -> None:
        assert validate_webhook_url("http://hooks.example.com/pine") is None

    def test_rejects_private_ip(self) -> None:
        assert validate_webhook_url("https://127.0.0.1/hook") is None
        assert validate_webhook_url("https://10.0.0.5/hook") is None
        assert validate_webhook_url("https://192.168.1.1/hook") is None
        assert validate_webhook_url("https://169.254.169.254/latest") is None

    def test_rejects_localhost(self) -> None:
        assert validate_webhook_url("https://localhost/hook") is None
        assert validate_webhook_url("https://metadata.google.internal/") is None

    def test_rejects_credentials(self) -> None:
        assert validate_webhook_url("https://user:pass@hooks.example.com/") is None

    def test_rejects_file_scheme(self) -> None:
        assert validate_webhook_url("file:///etc/passwd") is None

    def test_forward_rejects_bad_url(self) -> None:
        async def post(url: str, body: dict) -> int:
            raise AssertionError(f"should not POST to {url}")

        meta = asyncio.run(
            forward_alerts(
                [{"message": "x"}],
                "https://127.0.0.1/evil",
                http_post_json=post,
            )
        )
        assert meta["failed"] >= 1
        assert meta["forwarded"] == 0
        assert any("rejected" in e for e in meta["errors"])

    def test_group_skips_private(self) -> None:
        g = group_alerts_by_webhook(
            [
                {"message": "a", "webhook_url": "https://127.0.0.1/x"},
                {"message": "b", "webhook_url": "https://ok.example/x"},
            ],
            default_url="https://default.example/",
        )
        assert set(g) == {"https://ok.example/x"}


class TestAuth:
    def test_constant_time_wrong(self) -> None:
        assert validate_api_key("wrong", "secret-key") is False

    def test_ok(self) -> None:
        assert validate_api_key("secret-key", "secret-key") is True

    def test_missing_client_key(self) -> None:
        assert validate_api_key(None, "secret-key") is False

    def test_dev_mode(self) -> None:
        assert validate_api_key(None, None) is True
        assert validate_api_key(None, "") is True

    def test_length_mismatch_no_raise(self) -> None:
        assert validate_api_key("ab", "abcdef") is False


class TestIngestPathSafety:
    class _FakeR2:
        def __init__(self) -> None:
            self.store: dict[str, str] = {}

        async def get(self, key: str):
            return None

        async def put(self, key: str, value: str) -> None:
            self.store[key] = value

    async def test_ingest_rejects_bad_symbol(self) -> None:
        payload, status, _ = await handle_request(
            "POST",
            "/ingest",
            json.dumps(
                {
                    "symbol": "../evil",
                    "timeframe": "1m",
                    "bars": [
                        {
                            "open": 1,
                            "high": 2,
                            "low": 0.5,
                            "close": 1.5,
                            "time": 1_700_000_000_000,
                        }
                    ],
                }
            ),
            r2_bucket=self._FakeR2(),
            api_key="k",
            expected_api_key="k",
        )
        assert status == 400
        assert "symbol" in payload.get("error", "").lower()

    async def test_ingest_writes_safe_key(self) -> None:
        r2 = self._FakeR2()
        n = await ingest_ohlcv_to_r2(
            r2,
            "btcusdt",
            "1m",
            [
                {
                    "open": 1,
                    "high": 2,
                    "low": 0.5,
                    "close": 1.5,
                    "time": 1_700_000_000_000,
                    "volume": 1,
                }
            ],
        )
        assert n == 1
        keys = list(r2.store)
        assert keys
        assert all(k.startswith("data/BTCUSDT/1m/") for k in keys)
        assert ".." not in keys[0]
