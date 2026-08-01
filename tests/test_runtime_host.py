# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Host-surface tests for pyne-worker Runtime (SoT parity with backend/runtime).

Covers R5–R6 dual-host items: error_kind, inputs→interpret under auto,
compile fail-cache, and host compile success cache.
"""

from __future__ import annotations

import pytest

from pynescript_backend import runtime as runtime_mod
from pynescript_backend.runtime import Runtime


def _bars(n: int = 20) -> list[dict]:
    out: list[dict] = []
    price = 100.0
    for i in range(n):
        o = price
        c = price + (1.0 if i % 2 == 0 else -0.5)
        h = max(o, c) + 0.5
        l = min(o, c) - 0.5
        out.append(
            {
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "time": 1_700_000_000 + i * 86_400,
                "volume": 1000.0,
            }
        )
        price = c
    return out


@pytest.fixture(autouse=True)
def _clear_host_caches() -> None:
    runtime_mod._HOST_COMPILE_CACHE.clear()
    runtime_mod._HOST_COMPILE_FAIL_CACHE.clear()
    runtime_mod._PARSE_CACHE.clear()
    runtime_mod._HAS_COMPILER = None
    yield
    runtime_mod._HOST_COMPILE_CACHE.clear()
    runtime_mod._HOST_COMPILE_FAIL_CACHE.clear()


class TestErrorKind:
    def test_parse_error_kind(self) -> None:
        rt = Runtime()
        r = rt.run("this is not pine @@@", _bars(5), mode="interpret")
        assert "error" in r
        assert r.get("error_kind") == "parse"

    def test_bad_bars_data_kind(self) -> None:
        rt = Runtime()
        r = rt.run(
            "//@version=5\nindicator('t')\nplot(close)",
            [{"open": 1}],  # missing fields
            mode="interpret",
        )
        assert "error" in r
        assert r.get("error_kind") == "data"

    def test_unknown_mode_kind(self) -> None:
        rt = Runtime()
        r = rt.run("//@version=5\nindicator('t')\nplot(close)", _bars(3), mode="nope")
        assert "error" in r
        assert r.get("error_kind") == "mode"


class TestAutoInputs:
    def test_inputs_force_interpret(self) -> None:
        script = "//@version=5\nindicator('t')\nplot(close)"
        rt = Runtime()
        r = rt.run(script, _bars(30), mode="auto", inputs={"Length": 14})
        assert "error" not in r
        assert r.get("auto_backend") == "interpret"
        assert r.get("compile_fallback_reason") == "input.* overrides require interpret path"
        assert r.get("mode") == "interpret"


class TestAutoPrefilter:
    def test_request_falls_back_interpret(self) -> None:
        script = (
            "//@version=5\n"
            "indicator('req')\n"
            "s = request.security(syminfo.tickerid, 'D', close)\n"
            "plot(s)"
        )
        rt = Runtime()
        # Force eligibility past package probe when compiler is missing
        runtime_mod._HAS_COMPILER = True
        r = rt.run(script, _bars(40), mode="auto")
        assert r.get("auto_backend") == "interpret"
        reason = (r.get("compile_fallback_reason") or "").lower()
        assert "request" in reason or "compiler package unavailable" in reason


class TestCompileFailCache:
    def test_auto_compile_fail_cache_skips_recompile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Deterministic compile failures are remembered for subsequent auto runs."""
        try:
            from pynescript.compiler import engine as eng
        except ImportError:
            pytest.skip("compiler package unavailable")

        calls = {"n": 0}

        def boom(source: str, **kwargs):  # noqa: ARG001
            calls["n"] += 1
            raise RuntimeError("forced compile fail for test")

        monkeypatch.setattr(eng, "compile_script", boom)
        runtime_mod._HOST_COMPILE_CACHE.clear()
        runtime_mod._HOST_COMPILE_FAIL_CACHE.clear()
        runtime_mod._HAS_COMPILER = True

        script = "//@version=5\nindicator('x')\nplot(close)"
        rt = Runtime()
        r1 = rt.run(script, _bars(10), mode="auto")
        assert r1.get("auto_backend") == "interpret"
        reason1 = r1.get("compile_fallback_reason") or ""
        assert "forced compile" in reason1.lower()
        assert calls["n"] == 1
        key = Runtime._source_cache_key(script)
        assert key in runtime_mod._HOST_COMPILE_FAIL_CACHE

        r2 = rt.run(script, _bars(10), mode="auto")
        assert r2.get("auto_backend") == "interpret"
        assert r2.get("compile_fallback_reason") == reason1
        assert calls["n"] == 1  # no re-transpile


class TestCompileSuccessCache:
    def test_second_compile_hits_host_cache(self) -> None:
        script = "//@version=5\nindicator('sma')\nplot(ta.sma(close, 5))"
        rt = Runtime()
        r1 = rt.run(script, _bars(50), mode="compile")
        if "error" in r1:
            # Pure-numeric may need Numba; object-mode or missing numba is OK to skip.
            pytest.skip(f"compile unavailable: {r1['error']}")
        assert r1.get("mode") == "compile"
        assert r1.get("compile_cached") is False

        r2 = rt.run(script, _bars(50), mode="compile")
        assert "error" not in r2
        assert r2.get("compile_cached") is True
