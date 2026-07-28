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

import operator

from collections import deque
from collections.abc import Callable
from typing import Any


class PineSeries:
    """
    Represents a Pine Script series variable.
    Effectively behaves like the 'current value' (scalar) for math operations,
    but supports indexing [x] to access historical values.
    """

    __hash__ = None  # type: ignore

    def __init__(self, initial_value: Any = None, history_length: int = 1000):
        # Start empty so the first update() is bar 0 — do not seed a fake na bar.
        self.history: deque[Any] = deque(maxlen=history_length)
        self.current = initial_value
        if initial_value is not None:
            self.history.appendleft(initial_value)

    def update(self, new_value: Any):
        """Push a new value for the current bar."""
        self.current = new_value
        self.history.appendleft(new_value)

    def __getitem__(self, index: int | float):
        """Access historical values. series[0] is current, series[1] is previous.

        Float offsets (e.g. ``close[depth / 2]``) are truncated toward zero,
        matching Pine's int coercion for series subscripts.
        """
        if isinstance(index, float):
            if index != index:  # NaN
                return None
            index = int(index)
        if not isinstance(index, int):
            msg = f"Pine series index must be int, got {type(index).__name__}"
            raise TypeError(msg)
        if index < 0:
            msg = "Pine Script does not support negative indexing"
            raise ValueError(msg)
        if index >= len(self.history):
            return None  # na
        return self.history[index]

    def _binary_op(self, other: Any, op: Callable) -> Any:
        other_val = (
            other.current if isinstance(other, PineSeries) else (other.current if hasattr(other, "current") else other)
        )

        if self.current is None or other_val is None:
            return None

        try:
            return op(self.current, other_val)
        except TypeError:
            return None

    # Arithmetic Operations
    def __add__(self, other):
        return self._binary_op(other, operator.add)

    def __sub__(self, other):
        return self._binary_op(other, operator.sub)

    def __mul__(self, other):
        return self._binary_op(other, operator.mul)

    def __truediv__(self, other):
        return self._binary_op(other, operator.truediv)

    def __floordiv__(self, other):
        return self._binary_op(other, operator.floordiv)

    def __mod__(self, other):
        return self._binary_op(other, operator.mod)

    def __pow__(self, other):
        return self._binary_op(other, operator.pow)

    # Reverse Arithmetic
    def __radd__(self, other):
        return self._binary_op(other, lambda a, b: operator.add(b, a))

    def __rsub__(self, other):
        return self._binary_op(other, lambda a, b: operator.sub(b, a))

    def __rmul__(self, other):
        return self._binary_op(other, lambda a, b: operator.mul(b, a))

    def __rtruediv__(self, other):
        return self._binary_op(other, lambda a, b: operator.truediv(b, a))

    # Comparison
    def __eq__(self, other):
        return self._binary_op(other, operator.eq)

    def __ne__(self, other):
        return self._binary_op(other, operator.ne)

    def __lt__(self, other):
        return self._binary_op(other, operator.lt)

    def __le__(self, other):
        return self._binary_op(other, operator.le)

    def __gt__(self, other):
        return self._binary_op(other, operator.gt)

    def __ge__(self, other):
        return self._binary_op(other, operator.ge)

    # Boolean
    def __bool__(self):
        return bool(self.current)

    def __str__(self):
        return str(self.current)

    def __repr__(self):
        return f"PineSeries({self.current})"

    def __float__(self):
        """Allow Python ``float(series)`` — used by some numeric coercions."""
        if self.current is None:
            return float("nan")
        return float(self.current)

    def __int__(self):
        if self.current is None:
            return 0
        return int(self.current)

    def __index__(self):
        """Permit use as array/series index when current is whole number."""
        return int(self)
