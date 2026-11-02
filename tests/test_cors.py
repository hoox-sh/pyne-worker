# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""CORS for AXIS browser clients (product hosts + /run preflight)."""

from __future__ import annotations

from cors import cors_headers_for
from cors import origin_allowed
from cors import path_is_free_cors
from handler import handle_request


AXIS = "https://axis.hoox.sh"
PAGES = "https://feat-onchain.axis.pages.dev"
LEGACY_PAGES = "https://e2fceb51.pynescript-axis.pages.dev"
LOCAL = "http://localhost:3000"
EVIL_PAGES = "https://evil.pages.dev"
AI_RETIRED = "https://pynescript.ai"


class TestOriginAllowlist:
    def test_product_hosts(self) -> None:
        assert origin_allowed(AXIS)
        assert origin_allowed("https://hoox.sh")
        assert origin_allowed("https://pynescript.online")
        assert origin_allowed("https://app.pynescript.online")

    def test_pages_project_scoped(self) -> None:
        assert origin_allowed(PAGES)
        assert origin_allowed("https://axis.pages.dev")
        assert origin_allowed(LEGACY_PAGES)
        assert not origin_allowed(EVIL_PAGES)

    def test_localhost_any_port(self) -> None:
        assert origin_allowed(LOCAL)
        assert origin_allowed("http://127.0.0.1:8081")
        assert origin_allowed("https://localhost")
        assert not origin_allowed("http://0.0.0.0:8081")

    def test_retired_ai_and_header_injection(self) -> None:
        assert not origin_allowed(AI_RETIRED)
        assert not origin_allowed("https://evil.example")
        assert not origin_allowed("https://axis.hoox.sh\r\nX-Injected: 1")
        assert not origin_allowed(None)
        assert not origin_allowed("null")

    def test_extra_allowlist(self) -> None:
        extra = "https://a.example,https://b.example"
        assert origin_allowed("https://b.example", extra)
        assert not origin_allowed("https://c.example", extra)

    def test_star_opens_authenticated_routes(self) -> None:
        assert origin_allowed("https://random.example", "*")


class TestFreeHealthPath:
    def test_health_is_free(self) -> None:
        assert path_is_free_cors("/health")
        assert path_is_free_cors("/")
        assert not path_is_free_cors("/run")

    def test_health_echoes_any_https_origin(self) -> None:
        h = cors_headers_for(EVIL_PAGES, "/health")
        assert h["Access-Control-Allow-Origin"] == EVIL_PAGES

    def test_run_does_not_echo_foreign_pages(self) -> None:
        h = cors_headers_for(EVIL_PAGES, "/run")
        assert "Access-Control-Allow-Origin" not in h
        assert h.get("Vary") == "Origin"


class TestHandleRequestCors:
    async def test_options_run_no_auth(self) -> None:
        payload, status, headers = await handle_request(
            "OPTIONS",
            "/run",
            origin=AXIS,
        )
        assert status == 204
        assert payload == {}
        assert headers["Access-Control-Allow-Origin"] == AXIS
        allow = headers["Access-Control-Allow-Headers"].lower()
        assert "x-api-key" in allow
        assert "authorization" in allow
        assert "content-type" in allow
        assert "POST" in headers["Access-Control-Allow-Methods"]

    async def test_options_run_rejects_evil_pages(self) -> None:
        _payload, status, headers = await handle_request(
            "OPTIONS",
            "/run",
            origin=EVIL_PAGES,
        )
        assert status == 204
        assert "Access-Control-Allow-Origin" not in headers

    async def test_health_echoes_axis(self) -> None:
        payload, status, headers = await handle_request(
            "GET",
            "/health",
            origin=AXIS,
        )
        assert status == 200
        assert payload["features"].get("cors") is True
        assert headers["Access-Control-Allow-Origin"] == AXIS
        assert headers.get("Vary") == "Origin"

    async def test_unauthorized_run_still_has_cors(self) -> None:
        """Browser must be able to read a 401 (not a opaque CORS failure)."""
        _payload, status, headers = await handle_request(
            "POST",
            "/run",
            '{"script":"x"}',
            api_key=None,
            expected_api_key="secret",
            origin=AXIS,
        )
        assert status == 401
        assert headers["Access-Control-Allow-Origin"] == AXIS

    async def test_localhost_run_preflight(self) -> None:
        _payload, status, headers = await handle_request(
            "OPTIONS",
            "/run",
            origin=LOCAL,
        )
        assert status == 204
        assert headers["Access-Control-Allow-Origin"] == LOCAL

    async def test_allowed_origin_var(self) -> None:
        extra = "https://custom.example"
        _payload, status, headers = await handle_request(
            "OPTIONS",
            "/run",
            origin="https://custom.example",
            allowed_origin=extra,
        )
        assert status == 204
        assert headers["Access-Control-Allow-Origin"] == "https://custom.example"
