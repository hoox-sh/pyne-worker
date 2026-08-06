# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pynescript runtime glue ported from pynescript/backend.

The installable ``pynescript`` / ``hoox-pyne`` wheel does not include
``backend/``; this package holds the bar-loop Runtime used by pyne-worker.

Keep in sync with ``pynescript/backend/{runtime,evaluator,series}.py``.
Worker-only deltas (edge contract):

- ``timeout_seconds`` wall-clock budget + ``timed_out`` result flag
- strict OHLCV bar validation (``error_kind=data``)
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

from .runtime import Runtime

__all__ = ["Runtime"]
