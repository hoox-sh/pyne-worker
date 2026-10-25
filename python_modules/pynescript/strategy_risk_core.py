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

"""Risk-state behaviour shared by the compile and interpret strategy paths.

``CompileStrategyBroker`` (:mod:`pynescript.compiler.strategy_broker`) and
``StrategyState`` (:mod:`pynescript.ast.evaluator.builtins.strategy`) are two
independent implementations of the same ``strategy.*`` state machine — one for
ahead-of-time compilation, one for tree-walking interpretation. Their
day-scoped risk rollover and their drawdown / consecutive-loss-day halt
predicate must agree exactly, so they live here instead of being copied.

``_day_bucket`` intentionally stays per class: the compile path reads the
chart timezone from the host, while the interpret path reads its own
``_timezone`` state.
"""

from __future__ import annotations

from typing import Any


class StrategyRiskCore:
    """Day-scoped risk rollover shared by both strategy state machines.

    Subclasses provide the state attributes below (set in their ``__init__``)
    and a ``_day_bucket`` implementation.
    """

    # Risk configuration (``strategy.risk.*``)
    max_drawdown_risk: float | None
    max_drawdown_risk_percent: float | None
    max_cons_loss_days: int | None

    # Running risk state
    _max_drawdown: float
    _max_drawdown_percent: float
    consecutive_loss_days: int
    entries_blocked: bool

    # Day-scoped counters
    _calendar_day: int | None
    _fills_day: int | None
    _last_trade_day: int | None
    _day_pnl: float
    _day_filled_orders: int

    def _day_bucket(self, ts: int, tz: Any = None) -> int:  # pragma: no cover - overridden
        """Timezone-aware calendar-day id; implemented per subclass."""
        raise NotImplementedError

    def _permanent_risk_halt(self) -> bool:
        """True when max_drawdown or max_cons_loss_days still forbids entries."""
        if self.max_drawdown_risk is not None and self._max_drawdown >= float(self.max_drawdown_risk):
            return True
        if self.max_drawdown_risk_percent is not None and self._max_drawdown_percent >= float(
            self.max_drawdown_risk_percent
        ):
            return True
        if self.max_cons_loss_days is not None and self.consecutive_loss_days >= int(self.max_cons_loss_days):
            return True
        return False

    def _ensure_calendar_day(self, ts: int) -> None:
        """Roll day-scoped risk when the tz calendar day changes.

        Resets day PnL / fill counters and clears ``max_intraday_loss`` halt.
        ``max_drawdown`` and ``max_cons_loss_days`` stay blocked.
        """
        day = self._day_bucket(ts)
        if self._calendar_day is None:
            self._calendar_day = day
            self._fills_day = day
            return
        if day == self._calendar_day:
            return
        if self._last_trade_day is not None and self._last_trade_day != day:
            if self._day_pnl < 0:
                self.consecutive_loss_days += 1
            elif self._day_pnl > 0:
                self.consecutive_loss_days = 0
            if self.max_cons_loss_days is not None and self.consecutive_loss_days >= int(self.max_cons_loss_days):
                self.entries_blocked = True
            self._last_trade_day = day
        self._calendar_day = day
        self._day_pnl = 0.0
        self._day_filled_orders = 0
        self._fills_day = day
        self.entries_blocked = self._permanent_risk_halt()


class StrategyBracketCore:
    """Exit-bracket price resolution shared by the compile and interpret paths.

    Subclasses provide ``_exit_entry_avg`` and ``_tick_offset_price``.
    """

    def _exit_entry_avg(self, from_entry: str | None) -> float | None:  # pragma: no cover - overridden
        """Average entry price of the entry identified by *from_entry*."""
        raise NotImplementedError

    def _tick_offset_price(
        self,
        ticks: float | None,
        entry_avg: float | None,
        *,
        is_long: bool,
        is_profit: bool,
    ) -> float | None:  # pragma: no cover - overridden
        """Convert a tick offset into an absolute price."""
        raise NotImplementedError

    def _merge_exit_bracket_prices(
        self,
        *,
        limit_p: float | None,
        stop_p: float | None,
        profit_ticks: float | None,
        loss_ticks: float | None,
        is_long: bool,
        from_entry: str | None,
    ) -> tuple[float | None, float | None]:
        """``limit``/``stop`` stay absolute; ``profit``/``loss`` fill missing legs.

        Tick offsets are ``ticks * mintick`` from the exit's entry average.
        When both a tick param and its absolute twin are set, the absolute
        price wins. First-touch of the resulting limit+stop pair is unchanged.
        """
        entry_avg = self._exit_entry_avg(from_entry)
        if limit_p is None:
            limit_p = self._tick_offset_price(profit_ticks, entry_avg, is_long=is_long, is_profit=True)
        if stop_p is None:
            stop_p = self._tick_offset_price(loss_ticks, entry_avg, is_long=is_long, is_profit=False)
        return limit_p, stop_p
