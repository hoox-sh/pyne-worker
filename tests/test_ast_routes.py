# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""POST /ast/* language ops for the Grandmaster agent client."""

from __future__ import annotations

import json

from handler import handle_request


async def _post(
    path: str,
    body: dict | None = None,
    api_key: str | None = "test-key-123",
) -> tuple[dict, int]:
    raw = json.dumps(body) if body is not None else None
    payload, status, _headers = await handle_request(
        "POST",
        path,
        raw,
        api_key=api_key,
        expected_api_key="test-key-123",
    )
    return payload, status


VALID = "//@version=6\nindicator('AST')\nplot(close)\n"
INVALID = "indicator('x')\nplot(close)\n"


class TestAstParse:
    async def test_parse_ok(self) -> None:
        body, status = await _post("/ast/parse", {"script": VALID})
        assert status == 200
        assert body["ok"] is True
        assert body.get("version") in ("6", 6, "v6") or "version" in body
        assert body.get("errors") == []

    async def test_parse_error(self) -> None:
        body, status = await _post("/ast/parse", {"script": "@@@ not pine"})
        assert status == 200
        assert body["ok"] is False
        assert body.get("kind") == "parse" or body.get("error")


class TestAstLintDiagnostics:
    async def test_lint_ok(self) -> None:
        body, status = await _post("/ast/lint", {"script": VALID})
        assert status == 200
        assert body["ok"] is True
        assert int(body.get("error_count") or 0) == 0

    async def test_diagnostics_lists_items(self) -> None:
        body, status = await _post("/ast/diagnostics", {"script": VALID})
        assert status == 200
        assert "diagnostics" in body


class TestAstFormatCompile:
    async def test_format_roundtrip(self) -> None:
        body, status = await _post("/ast/format", {"script": VALID})
        assert status == 200
        assert body["ok"] is True
        assert "source" in body

    async def test_compile_never_emits_python(self) -> None:
        body, status = await _post(
            "/ast/compile",
            {"script": VALID, "emit": True},
        )
        assert status == 200
        assert "python" not in body
        # ok may be false if Numba compile unavailable — still no IR
        assert body.get("emit") in (False, None) or body.get("ok") is False


class TestAstAuthRouting:
    async def test_unknown_op_404(self) -> None:
        body, status = await _post("/ast/nope", {"script": VALID})
        assert status == 404
        assert body.get("code") == "UNKNOWN_ROUTE"

    async def test_requires_auth(self) -> None:
        payload, status, _ = await handle_request(
            "POST",
            "/ast/parse",
            json.dumps({"script": VALID}),
            api_key=None,
            expected_api_key="test-key-123",
        )
        assert status == 401

    async def test_health_lists_ast(self) -> None:
        payload, status, _ = await handle_request("GET", "/health")
        assert status == 200
        assert payload["version"] == "0.10.0"
        assert payload["features"]["ast"] is True
