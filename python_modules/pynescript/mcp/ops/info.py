# Copyright (C) 2024-2026 jango_blockchained
#
# This file is part of pynescript.
#
# pynescript is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pynescript is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with pynescript.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Version and optional-extra payload (CLI ``pyne info --json``)."""

from __future__ import annotations

import importlib.util
import platform

from typing import Any

from pynescript.mcp.serialize import ok


def has_mcp_sdk() -> bool:
    """True when the official ``mcp`` package is importable."""
    return importlib.util.find_spec("mcp") is not None


def info_payload() -> dict[str, Any]:
    """Package identity plus optional compile/MCP extras."""
    from pynescript import __version__

    try:
        from pynescript.compiler.engine import has_numba

        numba = has_numba()
    except Exception:
        numba = False
    rich_ok = importlib.util.find_spec("rich") is not None
    return ok(
        name="pynescript",
        distribution="hoox-pyne",
        version=__version__,
        python=platform.python_version(),
        platform=platform.platform(),
        numba=numba,
        rich=rich_ok,
        mcp=has_mcp_sdk(),
        entry_points={
            "cli": "pyne",
            "cli_alias": "pynescript",
            "lsp": "pyne-lsp",
            "lsp_alias": "pynescript-lsp",
            "mcp": "pyne-mcp",
        },
        docs="https://hoox.sh/pyne",
    )
