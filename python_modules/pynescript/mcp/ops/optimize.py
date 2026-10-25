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

"""Strategy hyperparameter search (CLI ``pyne optimize``)."""

from __future__ import annotations

import json

from typing import Any

from pynescript.mcp.ops.common import load_source
from pynescript.mcp.ops.common import synthetic_ohlcv_rows
from pynescript.mcp.serialize import clamp_bars
from pynescript.mcp.serialize import fail
from pynescript.mcp.serialize import ok


def optimize_source(
    source: str | None = None,
    path: str | None = None,
    *,
    encoding: str = "utf-8",
    space: dict[str, Any] | None = None,
    space_json: str = "",
    bars: int = 120,
    trials: int = 20,
    sampler: str = "auto",
    objective: str = "composite",
    validation: str = "holdout",
    train_bars: int | None = None,
    test_bars: int | None = None,
    step_bars: int | None = None,
    holdout_frac: float | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Search ``input.*`` values over interpret Runtime runs."""
    from pynescript.optimize import run_study
    from pynescript.optimize import space_from_payload
    from pynescript.optimize.types import ValidationSpec
    from pynescript.optimize.walk_forward import rolling_windows

    loaded = load_source(source, path, encoding=encoding)
    if isinstance(loaded, dict):
        return loaded
    text, label = loaded
    payload = space
    if payload is None:
        raw = (space_json or "").strip()
        if not raw:
            return fail("space or space_json is required", kind="io")
        try:
            loaded_space = json.loads(raw)
        except json.JSONDecodeError as exc:
            return fail(f"invalid space_json: {exc}", kind="io")
        if not isinstance(loaded_space, dict):
            return fail("space_json must be an object", kind="io")
        payload = loaded_space
    try:
        search = space_from_payload(payload)
    except (TypeError, ValueError) as exc:
        return fail(f"invalid space: {exc}", kind="io")
    n = clamp_bars(max(bars, 8))
    frac = 0.3 if holdout_frac is None else holdout_frac
    if validation == "walk-forward":
        train = train_bars if train_bars is not None else max(8, n * 2 // 3)
        test = test_bars if test_bars is not None else max(4, n // 6)
        step = step_bars if step_bars is not None else max(1, test)
    else:
        train = train_bars if train_bars is not None else 200
        test = test_bars if test_bars is not None else 50
        step = step_bars if step_bars is not None else 50
    spec = ValidationSpec(
        mode=validation,  # type: ignore[arg-type]
        holdout_frac=frac,
        train_bars=train,
        test_bars=test,
        step_bars=step,
    )
    if validation == "walk-forward" and not rolling_windows(n, spec):
        return fail(f"walk-forward needs bars >= train+test ({train}+{test}); got {n}", kind="io")
    study = run_study(
        text,
        synthetic_ohlcv_rows(n),
        search,
        n_trials=trials,
        sampler=sampler,
        objective=objective,
        validation=spec,
        seed=seed,
        min_trades=0 if validation == "in-sample" else 5,
    )
    data = study.to_dict()
    all_errored = bool(study.trials) and all(t.error for t in study.trials)
    failed = study.status == "error" or all_errored or (study.best_params is None and all_errored)
    if failed:
        return fail(study.error or "optimize failed", kind="runtime", file=label, study=data)
    return ok(file=label, study=data)
