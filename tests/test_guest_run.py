# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Guest POST /run from AXIS product / Pages preview Origins."""

from __future__ import annotations

import json

from handler import handle_request


AXIS = "https://axis.hoox.sh"
PREVIEW = "https://fix-honest-review-findings.pynescript-axis.pages.dev"
EVIL = "https://evil.pages.dev"

_GUEST_RUN_BODY = json.dumps(
    {
        "script": '//@version=6\nindicator("t")\nplot(close)',
        "data": [
            {"open": 100, "high": 105, "low": 95, "close": 102, "time": 1000},
            {"open": 102, "high": 108, "low": 101, "close": 106, "time": 2000},
            {"open": 106, "high": 110, "low": 104, "close": 108, "time": 3000},
        ],
        "mode": "interpret",
    }
)


async def _run(*, origin, allow_guest_run="1", path="/run", expected="secret", api_key=None):
    return await handle_request(
        "POST",
        path,
        _GUEST_RUN_BODY,
        api_key=api_key,
        expected_api_key=expected,
        origin=origin,
        allow_guest_run=allow_guest_run,
    )


class TestGuestRunAuth:
    async def test_preview_origin_allowed(self) -> None:
        payload, status, headers = await _run(origin=PREVIEW)
        assert status == 200, payload
        assert payload.get("status") == "success"
        assert headers["Access-Control-Allow-Origin"] == PREVIEW

    async def test_axis_production_origin_allowed(self) -> None:
        payload, status, headers = await _run(origin=AXIS)
        assert status == 200, payload
        assert payload.get("status") == "success"
        assert headers["Access-Control-Allow-Origin"] == AXIS

    async def test_evil_origin_still_401(self) -> None:
        payload, status, headers = await _run(origin=EVIL)
        assert status == 401
        assert payload.get("error") == "Unauthorized"
        assert "Access-Control-Allow-Origin" not in headers

    async def test_no_origin_still_401(self) -> None:
        payload, status, _headers = await _run(origin=None)
        assert status == 401
        assert payload.get("error") == "Unauthorized"

    async def test_flag_off_still_401(self) -> None:
        payload, status, headers = await _run(origin=PREVIEW, allow_guest_run=None)
        assert status == 401
        assert payload.get("error") == "Unauthorized"
        assert headers["Access-Control-Allow-Origin"] == PREVIEW

    async def test_management_route_still_401(self) -> None:
        payload, status, _headers = await handle_request(
            "GET",
            "/scripts",
            api_key=None,
            expected_api_key="secret",
            origin=PREVIEW,
            allow_guest_run="1",
        )
        assert status == 401
        assert payload.get("error") == "Unauthorized"

    async def test_legacy_key_still_works_without_origin(self) -> None:
        payload, status, _headers = await _run(
            origin=None, api_key="secret", allow_guest_run="1"
        )
        assert status == 200, payload
        assert payload.get("status") == "success"
