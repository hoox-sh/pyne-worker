# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Vendored engine snapshot must stay in lockstep with the sibling checkout.

Wrangler deploys ``python_modules/pynescript`` (Vendored Modules) — the
editable install pytest reaches through ``pythonpath`` is invisible to the
edge. The deploy tree once shipped 0.3.8 code under a 0.4.4 dist-info label,
which made the edge disagree with itself and with the sibling repo. These
tests pin the invariants instead of trusting ``sync_vendor.sh`` to be re-run.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_VENDOR_PKG = _ROOT / "python_modules" / "pynescript"
_SIBLING_PKG = _ROOT.parent / "pynescript" / "src" / "pynescript"

_VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.M)


def _about_version(pkg: Path) -> str:
    m = _VERSION_RE.search((pkg / "__about__.py").read_text(encoding="utf-8"))
    assert m, f"no __version__ in {pkg / '__about__.py'}"
    return m.group(1)


class TestVendoredSnapshot:
    def test_dist_info_label_matches_package_version(self) -> None:
        ver = _about_version(_VENDOR_PKG)
        labels = sorted(
            p.name for p in (_ROOT / "python_modules").glob("pynescript-*.dist-info")
        )
        assert labels == [f"pynescript-{ver}.dist-info"]

    def test_dist_info_metadata_version_matches(self) -> None:
        ver = _about_version(_VENDOR_PKG)
        metadata = (
            _ROOT / "python_modules" / f"pynescript-{ver}.dist-info" / "METADATA"
        ).read_text(encoding="utf-8")
        m = re.search(r"(?m)^Version: (.+)$", metadata)
        assert m, "METADATA has no Version line"
        assert m.group(1).strip() == ver

    def test_vendored_tree_imports_standalone(self) -> None:
        # ``-S`` skips site-packages so only python_modules can satisfy the
        # import — the same isolation Wrangler gives the edge tree.
        proc = subprocess.run(
            [
                sys.executable,
                "-S",
                "-c",
                "import pynescript; import pynescript.runtime.host; "
                "print(pynescript.__version__)",
            ],
            env={**os.environ, "PYTHONPATH": str(_ROOT / "python_modules")},
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip().splitlines()[-1] == _about_version(_VENDOR_PKG)

    def test_sibling_checkout_is_synced(self) -> None:
        if not (_SIBLING_PKG / "__about__.py").is_file():
            pytest.skip("pynescript sibling checkout not present")
        vendored = _about_version(_VENDOR_PKG)
        live = _about_version(_SIBLING_PKG)
        assert vendored == live, (
            f"vendored pynescript {vendored} != sibling {live} — "
            "run ./scripts/sync_vendor.sh"
        )
