# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Live tenant verify + usage flush tests (all network mocked).

Covers ``tenant_verify.verify_tenant`` (in-isolate cache, hash-on-wire,
negative cache) and the ``handle_request`` wiring (401 INVALID_KEY, 402
ENTITLEMENT_REQUIRED, quota headers, degraded fail-open/closed, usage
payload + idempotency keys, raw keys never in logs).
"""

# pyne-worker — Python Cloudflare Worker for Pine Script evaluation
# Copyright (C) 2024-2026  jango-blockchained
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

import hashlib
import json
import re

import pytest

import tenant_verify as tv
from handler import handle_request
from middleware import tenant_key_hash_prefix

_TENANT_KEY = "hx_live_verifytestkey001"
_CONSOLE = "https://console.hoox.sh"
_RUN_BODY = json.dumps(
    {
        "script": "//@version=5\nindicator('test')\nplot(close)",
        "ohlcv": [
            {"open": 100, "high": 105, "low": 95, "close": 102, "time": 1000},
            {"open": 102, "high": 108, "low": 101, "close": 106, "time": 2000},
        ],
    }
)
_IDEM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}:[0-9a-f]{16}:\d+$")


def _key_hash(key: str = _TENANT_KEY) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _valid_body(**overrides):
    base = {
        "ok": True,
        "tid": "ws_test123",
        "plan": "pro",
        "scopes": ["pyne:run", "trade"],
        "limits": {"calls_per_min": 60},
    }
    base.update(overrides)
    return base


def _fake_get(status, body, calls=None):
    calls = calls if calls is not None else {}

    async def fake(url, headers, timeout=5.0):
        calls["n"] = calls.get("n", 0) + 1
        calls["url"] = url
        calls["headers"] = dict(headers)
        return status, body

    return fake, calls


def _fake_post(status=200, calls=None):
    calls = calls if calls is not None else {}

    async def fake(url, headers, payload, timeout=5.0):
        calls["n"] = calls.get("n", 0) + 1
        calls["url"] = url
        calls["headers"] = dict(headers)
        calls["payload"] = payload
        return status

    return fake, calls


@pytest.fixture(autouse=True)
def _clean_state():
    tv.clear_verify_cache()
    tv.clear_usage_queue()
    yield
    tv.clear_verify_cache()
    tv.clear_usage_queue()


class TestVerifyTenant:
    async def test_cache_hit_skips_network(self, monkeypatch) -> None:
        fake, calls = _fake_get(200, _valid_body())
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        first = await tv.verify_tenant(_CONSOLE, _TENANT_KEY)
        second = await tv.verify_tenant(_CONSOLE, _TENANT_KEY)
        assert calls["n"] == 1
        assert first.outcome == "valid"
        assert second.outcome == "valid"
        assert first.tid == "ws_test123"
        assert first.plan == "pro"

    async def test_negative_cache_skips_network(self, monkeypatch) -> None:
        fake, calls = _fake_get(200, {"ok": False})
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        assert (await tv.verify_tenant(_CONSOLE, _TENANT_KEY)).outcome == "invalid"
        assert (await tv.verify_tenant(_CONSOLE, _TENANT_KEY)).outcome == "invalid"
        assert calls["n"] == 1

    async def test_negative_cache_expires(self, monkeypatch) -> None:
        fake, calls = _fake_get(200, {"ok": False})
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        await tv.verify_tenant(_CONSOLE, _TENANT_KEY)
        assert calls["n"] == 1
        # Force expiry of every entry, then verify again → refetch.
        for key in list(tv._VERIFY_CACHE):
            _, result = tv._VERIFY_CACHE[key]
            tv._VERIFY_CACHE[key] = (0.0, result)
        await tv.verify_tenant(_CONSOLE, _TENANT_KEY)
        assert calls["n"] == 2

    async def test_hash_on_wire_never_raw(self, monkeypatch) -> None:
        fake, calls = _fake_get(200, _valid_body())
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        await tv.verify_tenant(_CONSOLE, _TENANT_KEY)
        headers = calls["headers"]
        assert headers["Authorization"] == "Bearer " + _key_hash()
        assert _TENANT_KEY not in calls["url"]
        assert _TENANT_KEY not in json.dumps(headers)
        assert "scope=pyne%3Arun" in calls["url"]
        assert calls["url"].startswith(_CONSOLE + "/api/v1/verify?")

    async def test_no_console_url_degraded_without_network(self, monkeypatch) -> None:
        async def explode(url, headers, timeout=5.0):  # pragma: no cover
            raise AssertionError("no network allowed")

        monkeypatch.setattr(tv, "_fetch_json_get", explode)
        assert (await tv.verify_tenant(None, _TENANT_KEY)).outcome == "degraded"
        assert (await tv.verify_tenant("", _TENANT_KEY)).outcome == "degraded"
        assert (await tv.verify_tenant({}, _TENANT_KEY)).outcome == "degraded"

    async def test_transport_failure_degraded(self, monkeypatch) -> None:
        fake, _ = _fake_get(0, None)
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        assert (await tv.verify_tenant(_CONSOLE, _TENANT_KEY)).outcome == "degraded"

    async def test_unparseable_body_degraded(self, monkeypatch) -> None:
        fake, _ = _fake_get(200, None)
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        assert (await tv.verify_tenant(_CONSOLE, _TENANT_KEY)).outcome == "degraded"

    async def test_console_429_maps_to_limited_with_body_retry_after(self, monkeypatch) -> None:
        fake, _ = _fake_get(429, {"ok": False, "retry_after": 45})
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        result = await tv.verify_tenant(_CONSOLE, _TENANT_KEY)
        assert result.outcome == "limited"
        assert result.retry_after == 45

    async def test_console_429_retry_after_header(self, monkeypatch) -> None:
        async def fake(url, headers, timeout=5.0):
            return 429, {"ok": False}, {"retry-after": "30"}

        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        result = await tv.verify_tenant(_CONSOLE, _TENANT_KEY)
        assert result.outcome == "limited"
        assert result.retry_after == 30

    async def test_console_429_without_retry_after(self, monkeypatch) -> None:
        fake, _ = _fake_get(429, {"ok": False})
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        result = await tv.verify_tenant(_CONSOLE, _TENANT_KEY)
        assert result.outcome == "limited"
        assert result.retry_after is None

    async def test_scope_keyed_cache(self, monkeypatch) -> None:
        fake, calls = _fake_get(200, _valid_body())
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        first = await tv.verify_tenant(_CONSOLE, _TENANT_KEY)
        assert first.outcome == "valid"
        assert calls["n"] == 1
        # Same scope → cache hit, no new network call.
        second = await tv.verify_tenant(_CONSOLE, _TENANT_KEY)
        assert second.outcome == "valid"
        assert calls["n"] == 1
        # Different scope → cache miss (verdicts never cross scopes).
        third = await tv.verify_tenant(_CONSOLE, _TENANT_KEY, scope="other:scope")
        assert third.outcome == "valid"
        assert calls["n"] == 2
        assert "scope=other%3Ascope" in calls["url"]
        # And the other scope is now cached too.
        await tv.verify_tenant(_CONSOLE, _TENANT_KEY, scope="other:scope")
        assert calls["n"] == 2


class TestVerifyWiring:
    async def test_invalid_key_401_shape(self, monkeypatch) -> None:
        fake, _ = _fake_get(200, {"ok": False})
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        payload, status, _ = await handle_request(
            "POST",
            "/run",
            _RUN_BODY,
            api_key=None,
            expected_api_key="secret-123",
            authorization=f"Bearer {_TENANT_KEY}",
            console_url=_CONSOLE,
        )
        assert status == 401
        assert payload == {"ok": False, "error": {"code": "INVALID_KEY"}}

    async def test_console_401_maps_to_invalid(self, monkeypatch) -> None:
        fake, _ = _fake_get(401, {"error": "Unauthorized"})
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        _, status, _ = await handle_request(
            "POST",
            "/run",
            _RUN_BODY,
            expected_api_key="secret-123",
            authorization=f"Bearer {_TENANT_KEY}",
            console_url=_CONSOLE,
        )
        assert status == 401

    async def test_scope_mismatch_402_shape(self, monkeypatch) -> None:
        fake, _ = _fake_get(200, _valid_body(scopes=["trade"]))
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        payload, status, _ = await handle_request(
            "POST",
            "/run",
            _RUN_BODY,
            expected_api_key="secret-123",
            authorization=f"Bearer {_TENANT_KEY}",
            console_url=_CONSOLE,
        )
        assert status == 402
        assert payload == {
            "ok": False,
            "error": {
                "code": "ENTITLEMENT_REQUIRED",
                "scope": "pyne:run",
                "upgrade": "https://console.hoox.sh/billing",
            },
        }

    async def test_console_403_maps_to_402(self, monkeypatch) -> None:
        fake, _ = _fake_get(403, {"error": "Missing required scope"})
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        payload, status, _ = await handle_request(
            "POST",
            "/run",
            _RUN_BODY,
            expected_api_key="secret-123",
            authorization=f"Bearer {_TENANT_KEY}",
            console_url=_CONSOLE,
        )
        assert status == 402
        assert payload["error"]["code"] == "ENTITLEMENT_REQUIRED"

    async def test_valid_scopeless_still_402(self, monkeypatch) -> None:
        # outcome == "valid" but the required scope is absent → 402
        # (regression guard for the degraded-ordering fix: only "valid"
        # verdicts are scope-checked, so degraded can still fail open).
        fake, _ = _fake_get(200, _valid_body(scopes=[]))
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        payload, status, _ = await handle_request(
            "POST",
            "/run",
            _RUN_BODY,
            expected_api_key="secret-123",
            authorization=f"Bearer {_TENANT_KEY}",
            console_url=_CONSOLE,
        )
        assert status == 402
        assert payload["error"]["code"] == "ENTITLEMENT_REQUIRED"

    async def test_console_429_maps_to_429_shape(self, monkeypatch) -> None:
        # Quota spent must surface as 429 (never 401 — the key is good,
        # so clients must not rotate it) with Retry-After forwarded.
        async def fake(url, headers, timeout=5.0):
            return 429, {"ok": False, "retry_after": 45}, {"retry-after": "45"}

        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        payload, status, headers = await handle_request(
            "POST",
            "/run",
            _RUN_BODY,
            expected_api_key="secret-123",
            authorization=f"Bearer {_TENANT_KEY}",
            console_url=_CONSOLE,
        )
        assert status == 429
        assert payload == {"ok": False, "error": {"code": "usage.quota_exceeded", "retry_after": 45}}
        assert headers.get("Retry-After") == "45"

    async def test_console_429_without_retry_after_omits_header(self, monkeypatch) -> None:
        fake, _ = _fake_get(429, {"ok": False})
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        payload, status, headers = await handle_request(
            "POST",
            "/run",
            _RUN_BODY,
            expected_api_key="secret-123",
            authorization=f"Bearer {_TENANT_KEY}",
            console_url=_CONSOLE,
        )
        assert status == 429
        assert payload == {"ok": False, "error": {"code": "usage.quota_exceeded"}}
        assert "Retry-After" not in headers

    async def test_legacy_rate_key_never_raw(self) -> None:
        import handler as _handler_mod

        buckets = _handler_mod._rate_limiter._buckets
        saved = dict(buckets)
        buckets.clear()
        try:
            raw = "secret-legacy-rate-key"
            payload, status, _ = await handle_request(
                "POST",
                "/run",
                _RUN_BODY,
                api_key=raw,
                expected_api_key=raw,
            )
            assert status == 200
            assert payload.get("status") == "success"
            assert raw not in buckets
            assert f"legacy:{tenant_key_hash_prefix(raw)}" in buckets
        finally:
            buckets.clear()
            buckets.update(saved)

    async def test_quota_limit_overrides_rate_headers(self, monkeypatch) -> None:
        fake, _ = _fake_get(200, _valid_body(limits={"calls_per_min": 7}))
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        post, _ = _fake_post()
        monkeypatch.setattr(tv, "_post_json", post)
        _, status, headers = await handle_request(
            "POST",
            "/run",
            _RUN_BODY,
            expected_api_key="secret-123",
            authorization=f"Bearer {_TENANT_KEY}",
            console_url=_CONSOLE,
        )
        assert status == 200
        assert headers["X-RateLimit-Limit"] == "7"
        assert "X-RateLimit-Remaining" in headers

    async def test_degraded_fail_open_with_legacy_match(self, monkeypatch) -> None:
        fake, _ = _fake_get(0, None)  # console unreachable
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        payload, status, headers = await handle_request(
            "POST",
            "/run",
            _RUN_BODY,
            api_key="secret-123",
            expected_api_key="secret-123",
            authorization=f"Bearer {_TENANT_KEY}",
            console_url=_CONSOLE,
        )
        assert status == 200
        assert payload.get("status") == "success"
        assert headers.get("X-Hoox-Verify") == "degraded"

    async def test_degraded_fail_closed_without_legacy(self, monkeypatch) -> None:
        fake, _ = _fake_get(0, None)  # console unreachable
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        payload, status, _ = await handle_request(
            "POST",
            "/run",
            _RUN_BODY,
            api_key=None,
            expected_api_key="secret-123",
            authorization=f"Bearer {_TENANT_KEY}",
            console_url=_CONSOLE,
        )
        assert status == 401
        assert payload == {"ok": False, "error": {"code": "INVALID_KEY"}}

    async def test_no_console_url_keeps_passthrough(self) -> None:
        payload, status, _ = await handle_request(
            "POST",
            "/run",
            _RUN_BODY,
            api_key=None,
            expected_api_key="secret-123",
            authorization=f"Bearer {_TENANT_KEY}",
        )
        assert status == 200
        assert payload.get("status") == "success"

    async def test_raw_key_never_logged(self, monkeypatch, capsys) -> None:
        fake, _ = _fake_get(200, _valid_body())
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        post, _ = _fake_post()
        monkeypatch.setattr(tv, "_post_json", post)
        await handle_request(
            "POST",
            "/run",
            _RUN_BODY,
            expected_api_key="secret-123",
            authorization=f"Bearer {_TENANT_KEY}",
            console_url=_CONSOLE,
            request_id="verify-log-test",
        )
        out = capsys.readouterr().out
        assert _TENANT_KEY not in out
        assert _key_hash() not in out  # full wire hash stays out of logs too
        assert tenant_key_hash_prefix(_TENANT_KEY) in out


class TestUsageFlush:
    async def test_usage_queued_and_flushed_on_run(self, monkeypatch) -> None:
        fake, _ = _fake_get(200, _valid_body())
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        post, calls = _fake_post()
        monkeypatch.setattr(tv, "_post_json", post)
        payload, status, _ = await handle_request(
            "POST",
            "/run",
            _RUN_BODY,
            expected_api_key="secret-123",
            authorization=f"Bearer {_TENANT_KEY}",
            console_url=_CONSOLE,
        )
        assert status == 200
        assert payload.get("status") == "success"
        assert calls["n"] == 1
        assert calls["url"] == _CONSOLE + "/api/v1/usage"
        events = calls["payload"]["events"]
        assert len(events) == 1
        event = events[0]
        assert event["tid"] == "ws_test123"
        assert event["scope"] == "pyne:run"
        assert event["units"] == {"calls": 1, "bars": 2}
        assert _IDEM_RE.match(event["idem"]), event["idem"]
        assert event["idem"].split(":")[1] == _key_hash()[:16]
        # Service auth falls back to the tenant key hash (hash-on-wire).
        assert calls["headers"]["Authorization"] == "Bearer " + _key_hash()
        assert _TENANT_KEY not in json.dumps(calls["payload"])
        assert tv.usage_queue_depth() == 0

    async def test_usage_service_auth_override(self, monkeypatch) -> None:
        fake, _ = _fake_get(200, _valid_body())
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        post, calls = _fake_post()
        monkeypatch.setattr(tv, "_post_json", post)
        _, status, _ = await handle_request(
            "POST",
            "/run",
            _RUN_BODY,
            expected_api_key="secret-123",
            authorization=f"Bearer {_TENANT_KEY}",
            console_url=_CONSOLE,
            usage_auth="svc_usage_abc",
        )
        assert status == 200
        assert calls["headers"]["Authorization"] == "Bearer svc_usage_abc"

    async def test_usage_failure_never_fails_run(self, monkeypatch) -> None:
        fake, _ = _fake_get(200, _valid_body())
        monkeypatch.setattr(tv, "_fetch_json_get", fake)

        async def boom(url, headers, payload, timeout=5.0):
            raise TimeoutError("console down")

        monkeypatch.setattr(tv, "_post_json", boom)
        payload, status, _ = await handle_request(
            "POST",
            "/run",
            _RUN_BODY,
            expected_api_key="secret-123",
            authorization=f"Bearer {_TENANT_KEY}",
            console_url=_CONSOLE,
        )
        assert status == 200
        assert payload.get("status") == "success"

    async def test_failed_run_queues_nothing(self, monkeypatch) -> None:
        fake, _ = _fake_get(200, _valid_body())
        monkeypatch.setattr(tv, "_fetch_json_get", fake)
        post, calls = _fake_post()
        monkeypatch.setattr(tv, "_post_json", post)
        # 501 (no R2) is not a successful ingest → no usage, no flush.
        _, status, _ = await handle_request(
            "POST",
            "/ingest",
            json.dumps(
                {
                    "symbol": "BTCUSDT",
                    "timeframe": "1d",
                    "bars": [
                        {"open": 100, "high": 105, "low": 95, "close": 102, "time": 1000},
                    ],
                }
            ),
            r2_bucket=None,
            expected_api_key="secret-123",
            authorization=f"Bearer {_TENANT_KEY}",
            console_url=_CONSOLE,
        )
        assert status == 501
        assert calls.get("n", 0) == 0
        assert tv.usage_queue_depth() == 0

    async def test_idempotency_key_format(self) -> None:
        event = tv.queue_usage_event("ws_1", "pyne:run", 1, 10, "9f2ac4110be3d4e5")
        assert _IDEM_RE.match(event["idem"]), event["idem"]
        assert event["units"] == {"calls": 1, "bars": 10}
