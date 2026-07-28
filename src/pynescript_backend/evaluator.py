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

from typing import Any

from pynescript.ast.evaluator import NodeLiteralEvaluator


class _NaValue:
    """PineScript ``na`` value: returns None when used bare, checks is-None when called."""

    def __call__(self, x=None):
        return x is None or isinstance(x, _NaValue)

    def __bool__(self):
        return False

    def __eq__(self, other):
        return other is None or isinstance(other, _NaValue)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return "na"

    def __float__(self):
        # float(na) → nan so math ops degrade instead of TypeError
        return float("nan")

    def __int__(self):
        raise ValueError("cannot convert na to int")

    def __add__(self, other):
        return self

    def __radd__(self, other):
        return self

    def __sub__(self, other):
        return self

    def __rsub__(self, other):
        return self

    def __mul__(self, other):
        return self

    def __rmul__(self, other):
        return self

    def __truediv__(self, other):
        return self

    def __rtruediv__(self, other):
        return self

    def __lt__(self, other):
        return False

    def __le__(self, other):
        return False

    def __gt__(self, other):
        return False

    def __ge__(self, other):
        return False

    def __neg__(self):
        return self

    def __abs__(self):
        return self

    @property
    def current(self):
        return None


class _SeriesResult:
    """Wrapper for builtin results that preserves full history.

    Builtins like ``ta.sma`` return the entire history (a list of all SMA
    values). In the per-bar model we need:
    - The current bar's scalar value (``.current``) for comparisons.
    - The full history (``.history``) so nested builtins like
      ``ta.ema(ta.sma(close, 14), 10)`` work correctly and history
      references like ``x[1]`` give the previous bar's value.
    """

    __slots__ = ("history", "current")

    def __init__(self, values: list[Any]) -> None:
        # Store history in PineSeries format: most-recent-first so that
        # ``_as_series`` (which does ``reversed(history)``) produces a
        # chronological list that builtins like ``_sma`` / ``_ema`` expect.
        self.history = list(reversed(values))
        self.current = values[-1] if values else None

    def _val(self, other: Any) -> Any:
        if isinstance(other, _SeriesResult):
            return other.current
        if hasattr(other, "current"):
            return other.current if other.current is not None else None
        return other

    def _safe_op(self, op, other):
        other_val = self._val(other)
        if self.current is None or other_val is None:
            return None
        try:
            return op(self.current, other_val)
        except TypeError:
            return None

    def __gt__(self, other):
        return self._safe_op(operator.gt, other)

    def __lt__(self, other):
        return self._safe_op(operator.lt, other)

    def __ge__(self, other):
        return self._safe_op(operator.ge, other)

    def __le__(self, other):
        return self._safe_op(operator.le, other)

    def __eq__(self, other):
        return self._safe_op(operator.eq, other)

    def __ne__(self, other):
        return self._safe_op(operator.ne, other)

    def __add__(self, other):
        return self._safe_op(operator.add, other)

    def __sub__(self, other):
        return self._safe_op(operator.sub, other)

    def __mul__(self, other):
        return self._safe_op(operator.mul, other)

    def __rmul__(self, other):
        return self._safe_op(lambda a, b: operator.mul(b, a), other)

    def __truediv__(self, other):
        return self._safe_op(operator.truediv, other)

    def __rtruediv__(self, other):
        return self._safe_op(lambda a, b: operator.truediv(b, a), other)

    def __radd__(self, other):
        return self._safe_op(lambda a, b: operator.add(b, a), other)

    def __rsub__(self, other):
        return self._safe_op(lambda a, b: operator.sub(b, a), other)

    def __abs__(self):
        if self.current is None:
            return None
        return abs(self.current)

    def __neg__(self):
        if self.current is None:
            return None
        return -self.current

    def __bool__(self):
        return self.current is not None and bool(self.current)

    def __iter__(self):
        return iter(self.history)

    def __getitem__(self, index):
        # Pine allows float indices that are whole numbers (e.g. length/2)
        if isinstance(index, float) and index == int(index):
            index = int(index)
        try:
            return self.history[index]
        except (TypeError, IndexError):
            return None

    def __len__(self):
        return len(self.history)

    def __str__(self):
        return str(self.current)

    def __repr__(self):
        return f"_SeriesResult({self.current})"

    def __float__(self):
        if self.current is None:
            return float("nan")
        return float(self.current)

    def __int__(self):
        if self.current is None:
            return 0
        return int(self.current)


def _to_scalar(value: Any) -> Any:
    """Extract the current bar's scalar from a potential series result."""
    if isinstance(value, _SeriesResult):
        return value.current
    if isinstance(value, list) and len(value) > 0:
        return value[-1]
    if hasattr(value, "current"):
        return value.current
    return value


class CustomEvaluator(NodeLiteralEvaluator):
    """Evaluator that captures plot commands."""

    def __init__(self, context=None):
        super().__init__(context)
        self.plot_outputs = []
        # Align with pynescript backend Runtime: bar-mode scalars + incremental TA.
        # Nested ``ta.ema(ta.sma(...), n)`` stays correct because each call site
        # keeps its own incremental state across bars (see TechnicalHelpers).
        self._pine_bar_mode = True
        self._pine_ta_incremental = True
        self.current_series: dict[str, list] = {}

    def _call_builtin(self, name: str, args: list[Any], kwargs: dict[str, Any] | None = None) -> Any:
        # For non-technical builtins, unwrap PineSeries/SeriesResult args to scalars.
        # Technical builtins (ta.*) and array builtins need full series/list objects.
        # Bare v3/v4 aliases (macd, sma, …) are registered as the same handlers —
        # treat them like ta.* so series args are not collapsed to scalars.
        # request.security's expression arg may be a tuple/matrix/array — keep intact.
        dispatch = self._builtin_dispatch
        if dispatch is None:
            dispatch = self._build_builtin_map()
            self._builtin_dispatch = dispatch
        is_ta_alias = (
            not name.startswith("ta.")
            and f"ta.{name}" in dispatch
            and dispatch.get(name) is dispatch.get(f"ta.{name}")
        )
        if (
            name.startswith("ta.")
            or is_ta_alias
            or name.startswith("array.")
            or name.startswith("matrix.")
            or name.startswith("map.")
        ):
            cleaned_args = args
            # Keep ta.* kwargs so base._call_builtin can merge source=/length=/…
            # into positional args. Unknown plot-style kwargs are dropped there.
        elif name.startswith("request."):
            cleaned_args = [
                a if i >= 2 else _to_scalar(a)
                for i, a in enumerate(args)
            ]
        elif name in ("input", "input.source"):
            # Preserve PineSeries / list series for source inputs so
            # ``src = input(close)`` / ``input.source(close)`` keeps history
            # for downstream ta.* (macd, sma, …).
            cleaned_args = [
                a if (hasattr(a, "history") or isinstance(a, list)) else _to_scalar(a)
                for a in args
            ]
        else:
            cleaned_args = [_to_scalar(a) for a in args]
        result = super()._call_builtin(name, cleaned_args, kwargs=kwargs)
        # Unwrap dict results from input.* handlers back to plain values
        if isinstance(result, dict) and "default" in result:
            return result["default"]
        # Keep array/matrix/map results as plain lists/objects (instance methods need lists)
        if name.startswith("array.") or name.startswith("matrix.") or name.startswith("map."):
            return result
        # request.* may return tuples/lists of mixed objects (array + matrix, etc.)
        # — never wrap those as numeric series history.
        if name.startswith("request."):
            return result
        # In bar mode, ta.* returns scalars (or short tuples like macd). Only wrap
        # full numeric lists when incremental/bar-mode is off (legacy nested path).
        if isinstance(result, list) and len(result) > 0:
            if all(x is None or isinstance(x, (int, float, bool)) for x in result):
                if getattr(self, "_pine_bar_mode", False) and len(result) == 1:
                    return result[0]
                if not getattr(self, "_pine_bar_mode", False):
                    return _SeriesResult(result)
                # Multi-value numeric list (e.g. non-incremental full series): wrap
                # so nested ta / [1] history still works when bar_mode but full list.
                return _SeriesResult(result)
            return result
        return result

    def _builtin_plot(self, args: list[Any], kwargs: dict[str, Any] | None = None) -> None:
        if not args:
            return None
        self.plot_outputs.append({"type": "plot", "value": _to_scalar(args[0])})
        return None

    def reset_plots(self):
        # Reuse list to cut per-bar allocations (values are copied into results)
        self.plot_outputs.clear()
