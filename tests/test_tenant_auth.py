# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tenant Bearer passthrough tests — Phase 2 thin slice (no enforcement).

``Authorization: Bearer hx_live_…`` is accepted as tenant passthrough
(no remote verify, no metering, no quota yet). Legacy ``X-API-Key`` behavior
is unchanged; invalid keys still 401; raw keys are never logged.
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

import json

from handler import handle_request
from middleware import extract_bearer_token
from middleware import is_tenant_key
from middleware import tenant_key_hash_prefix

_TENANT_KEY = "hx_live_test456789abcdef"
_VALID_RUN_BODY = json.dumps(
    {
        "script": "//@version=5\nindicator('test')\nplot(close)",
        "ohlcv": [
            {"open": 100, "high": 105, "low": 95, "close": 102, "time": 1000},
            {"open": 102, "high": 108, "low": 101, "close": 106, "time": 2000},
        ],
    }
)


class TestExtractBearerToken:
    def test_bearer_ok(self) -> None:
        assert extract_bearer_token("Bearer abc123") == "abc123"

    def test_scheme_case_insensitive(self) -> None:
        assert extract_bearer_token("bearer abc123") == "abc123"
        assert extract_bearer_token("BEARER abc123") == "abc123"

    def test_trims_whitespace(self) -> None:
        assert extract_bearer_token("Bearer   abc123  ") == "abc123"

    def test_non_bearer_none(self) -> None:
        assert extract_bearer_token("Basic abc123") is None
        assert extract_bearer_token("hx_live_abc") is None

    def test_missing_or_empty_none(self) -> None:
        assert extract_bearer_token(None) is None
        assert extract_bearer_token("") is None
        assert extract_bearer_token("Bearer ") is None
        assert extract_bearer_token("Bearer") is None


class TestIsTenantKey:
    def test_hx_live_accepted(self) -> None:
        assert is_tenant_key("hx_live_abc123") is True

    def test_bare_prefix_rejected(self) -> None:
        assert is_tenant_key("hx_live_") is False

    def test_legacy_rejected(self) -> None:
        assert is_tenant_key("secret-key") is False
        assert is_tenant_key("") is False

    def test_none_rejected(self) -> None:
        assert is_tenant_key(None) is False


class TestTenantKeyHashPrefix:
    def test_deterministic_hex(self) -> None:
        first = tenant_key_hash_prefix(_TENANT_KEY)
        assert first == tenant_key_hash_prefix(_TENANT_KEY)
        assert len(first) == 8
        int(first, 16)  # valid hex

    def test_custom_length(self) -> None:
        assert len(tenant_key_hash_prefix(_TENANT_KEY, 16)) == 16

    def test_hides_raw_key(self) -> None:
        assert _TENANT_KEY not in tenant_key_hash_prefix(_TENANT_KEY, 64)


class TestTenantPassthrough:
    async def test_bearer_tenant_accepted_as_passthrough(self) -> None:
        payload, status, _ = await handle_request(
            "POST",
            "/run",
            _VALID_RUN_BODY,
            api_key=None,
            expected_api_key="secret-123",
            authorization=f"Bearer {_TENANT_KEY}",
        )
        assert status == 200
        assert payload.get("status") == "success"

    async def test_bearer_tenant_passes_auth_without_r2(self) -> None:
        # 501 (not 401) proves auth passed; R2 simply isn't configured here.
        payload, status, _ = await handle_request(
            "GET",
            "/scripts",
            None,
            r2_bucket=None,
            api_key=None,
            expected_api_key="secret-123",
            authorization=f"Bearer {_TENANT_KEY}",
        )
        assert status == 501
        assert "not configured" in payload.get("error", "").lower()

    async def test_non_tenant_bearer_still_401(self) -> None:
        payload, status, _ = await handle_request(
            "POST",
            "/run",
            _VALID_RUN_BODY,
            api_key=None,
            expected_api_key="secret-123",
            authorization="Bearer wrong-key",
        )
        assert status == 401
        assert "Unauthorized" in payload.get("error", "")

    async def test_missing_auth_still_401(self) -> None:
        payload, status, _ = await handle_request(
            "POST",
            "/run",
            _VALID_RUN_BODY,
            api_key=None,
            expected_api_key="secret-123",
        )
        assert status == 401

    async def test_legacy_x_api_key_untouched(self) -> None:
        payload, status, _ = await handle_request(
            "POST",
            "/run",
            _VALID_RUN_BODY,
            api_key="secret-123",
            expected_api_key="secret-123",
        )
        assert status == 200

    async def test_wrong_legacy_key_still_401(self) -> None:
        payload, status, _ = await handle_request(
            "POST",
            "/run",
            _VALID_RUN_BODY,
            api_key="wrong-key",
            expected_api_key="secret-123",
        )
        assert status == 401

    async def test_health_stays_open(self) -> None:
        payload, status, _ = await handle_request("GET", "/health")
        assert status == 200
        assert payload["status"] == "ok"

    async def test_raw_key_never_logged(self, capsys) -> None:
        await handle_request(
            "GET",
            "/scripts",
            None,
            r2_bucket=None,
            api_key=None,
            expected_api_key="secret-123",
            authorization=f"Bearer {_TENANT_KEY}",
            request_id="test-req-1",
        )
        out = capsys.readouterr().out
        assert _TENANT_KEY not in out
        assert tenant_key_hash_prefix(_TENANT_KEY) in out
