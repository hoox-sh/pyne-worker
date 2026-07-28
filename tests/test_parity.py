"""Parity corpus tests — run each ``.pine`` through ``Runtime.run`` and
compare the emitted events against the expected JSON fixture.

The fixture scripts and expected outputs live in the ``pynescript`` repo at
``tests/fixtures/parity/``. These tests reuse the same parity oracle shared
with the TypeScript port (``pine-worker``).

Regenerate the JSON fixtures from ``pynescript`` by running::

    python tests/fixtures/parity/generate_fixtures.py
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
import os
import sys
from pathlib import Path

import pytest

from pynescript_backend import Runtime


# ---------------------------------------------------------------------------
# Locate the parity fixtures in the sibling pynescript repo
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_PYNESCRIPT_ROOT = _HERE.parents[2] / "pynescript"  # ../../pynescript
_FIXTURE_DIR = _PYNESCRIPT_ROOT / "tests" / "fixtures" / "parity"

# If pynescript isn't at the expected relative path, try an absolute guess
if not _FIXTURE_DIR.exists():
    _FIXTURE_DIR = Path("/mnt/data/home/jango/Git/pynescript/tests/fixtures/parity")

assert _FIXTURE_DIR.exists(), (
    f"Parity fixtures not found at {_FIXTURE_DIR}. Ensure pynescript repo is cloned alongside pyne-worker."
)

_PINE_DIR = _FIXTURE_DIR / "pine"
_JSON_DIR = _FIXTURE_DIR / "json"

# Ensure the OHLCV module can be imported (pynescript root, not just src/)
_PYNESCRIPT_ROOT_STR = str(_PYNESCRIPT_ROOT)
if _PYNESCRIPT_ROOT.exists() and _PYNESCRIPT_ROOT_STR not in sys.path:
    sys.path.insert(0, _PYNESCRIPT_ROOT_STR)

from tests.fixtures.parity.ohlcv import OHLCV  # type: ignore[import-untyped]


# ---------------------------------------------------------------------------
# Fixture discovery
# ---------------------------------------------------------------------------


def _discover_scripts() -> list[Path]:
    """Return all ``.pine`` files in the parity corpus, sorted."""
    return sorted(_PINE_DIR.glob("*.pine"))


def _strip_unstable_keys(events: list[dict]) -> list[dict]:
    """Remove ``script_id`` and ``run_id`` which differ per invocation."""
    for ev in events:
        ev.pop("script_id", None)
        ev.pop("run_id", None)
    return events


_scripts = _discover_scripts()


def _param_id(path: Path) -> str:
    return path.stem


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pine_path", _scripts, ids=_param_id)
def test_parity_corpus(pine_path: Path) -> None:
    """Run a parity fixture script through ``Runtime.run`` and compare
    the emitted events to the expected JSON fixture."""
    script_id = pine_path.stem

    # -- Load source --------------------------------------------------------
    source = pine_path.read_text(encoding="utf-8")

    # -- Load expected JSON -------------------------------------------------
    json_path = _JSON_DIR / f"{script_id}.json"
    assert json_path.exists(), (
        f"Expected JSON fixture not found: {json_path}.\n"
        f"Run 'python tests/fixtures/parity/generate_fixtures.py' to create it."
    )
    expected = json.loads(json_path.read_text(encoding="utf-8"))

    # -- Execute ------------------------------------------------------------
    result: dict = Runtime().run(source, OHLCV)

    # -- Assert no error ----------------------------------------------------
    assert "error" not in result, f"Runtime error: {result['error']}"

    # -- Compare events -----------------------------------------------------
    events: list[dict] = result["events"]
    actual = _strip_unstable_keys(events)

    assert actual == expected, (
        f"Events mismatch for {script_id}\n"
        f"  Expected ({len(expected)} events): {json.dumps(expected, indent=2)}\n"
        f"  Actual   ({len(actual)} events):   {json.dumps(actual, indent=2)}"
    )


# ---------------------------------------------------------------------------
# Smoke test: minimal strategy using common builtins
# ---------------------------------------------------------------------------


def test_minimal_strategy_pipeline() -> None:
    """Run a minimal strategy through the full pipeline: parse → evaluate →
    emit events → serialize. Asserts event shapes are well-formed."""
    source = """//@version=6
strategy("PipelineTest", overlay=true)

// -- Entry / exit -------------------------------------------------------
if bar_index == 0
    strategy.entry("L1", strategy.long, qty=10.0, comment="buy")
if bar_index == 1
    strategy.exit("L1", qty=5.0, stop=90.0, limit=110.0)
if bar_index == 2
    strategy.close("L1")
"""
    result: dict = Runtime().run(source, OHLCV)

    # -- No runtime error -------------------------------------------------
    assert "error" not in result, f"Unexpected error: {result['error']}"

    # -- Events are present and well-typed --------------------------------
    events: list[dict] = result["events"]
    assert len(events) >= 3, f"Expected at least 3 events, got {len(events)}"

    kinds = [e["kind"] for e in events[:3]]
    assert kinds == ["entry", "exit", "close"], f"Unexpected kinds: {kinds}"

    # -- bar_index is threaded from context -------------------------------
    assert events[0]["bar_index"] == 0
    assert events[1]["bar_index"] == 1
    assert events[2]["bar_index"] == 2

    # -- script_id and run_id are present ---------------------------------
    for ev in events[:3]:
        assert ev.get("script_id"), f"Missing script_id in {ev['kind']}"
        assert ev.get("run_id"), f"Missing run_id in {ev['kind']}"
