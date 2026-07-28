#!/usr/bin/env python3
# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Benchmark pyne-worker Runtime throughput.

Measures runs-per-minute by executing a complex Pine Script strategy
against real BTCUSDT data locally (no HTTP). Appends results to CSV.

Usage:
    python scripts/benchmark.py [--duration 300] [--warmup 10] [--csv results.csv]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT = _HERE.parent

# Ensure src/ is on the path
sys.path.insert(0, str(_PROJECT / "src"))
sys.path.insert(0, str(_PROJECT))

# Also ensure pynescript can be imported
_PYNESCRIPT = _PROJECT.parent / "pynescript"
if _PYNESCRIPT.exists():
    sys.path.insert(0, str(_PYNESCRIPT))


def load_ohlcv(path: str | Path) -> list[dict]:
    """Load BTCUSDT daily bars from gzipped JSONL."""
    import gzip

    path = Path(path)
    if path.suffix == ".gz":
        open_f = gzip.open(path, "rt")
    else:
        open_f = open(path, "r")
    bars: list[dict] = []
    with open_f as f:
        for line in f:
            line = line.strip()
            if line:
                bars.append(json.loads(line))
    return bars


def load_scripts() -> dict[str, str]:
    """Load Pine Script sources."""
    scripts: dict[str, str] = {}

    big = _HERE / "big_strategy.pine"
    if big.exists():
        scripts["big_strategy"] = big.read_text()

    # Minimal script for comparison
    scripts["minimal"] = """//@version=5
indicator("Minimal")
plot(close)
"""

    return scripts


def run_and_measure(
    runtime: object,
    source: str,
    ohlcv: list[dict],
    n: int = 100,
    warmup: int = 5,
) -> dict:
    """Run a script ``n`` times and return timing stats.

    Args:
        runtime: A ``Runtime`` instance.
        source: Pine Script source.
        ohlcv: OHLCV bar data.
        n: Number of timed iterations.
        warmup: Number of warm-up iterations (not timed).

    Returns:
        Dict with ``name``, ``n``, ``bars``, ``total_ms``, ``avg_ms``,
        ``min_ms``, ``max_ms``, ``runs_per_minute``.
    """
    from pynescript_backend import Runtime as _Runtime

    if runtime is None:
        runtime = _Runtime(symbol="BENCH")

    bars_count = len(ohlcv)

    # Warmup
    for _ in range(warmup):
        _ = runtime.run(source, ohlcv)

    # Timed runs
    times: list[float] = []
    errors = 0
    for _ in range(n):
        t0 = time.perf_counter()
        result = runtime.run(source, ohlcv)
        elapsed = time.perf_counter() - t0
        times.append(elapsed * 1000)  # ms
        if "error" in result:
            errors += 1

    total_ms = sum(times)
    avg_ms = total_ms / n
    min_ms = min(times)
    max_ms = max(times)
    rpm = 60_000 / avg_ms if avg_ms > 0 else 0

    return {
        "n": n,
        "bars": bars_count,
        "errors": errors,
        "total_ms": round(total_ms, 2),
        "avg_ms": round(avg_ms, 2),
        "min_ms": round(min_ms, 2),
        "max_ms": round(max_ms, 2),
        "runs_per_minute": round(rpm, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark pyne-worker Runtime")
    parser.add_argument("--duration", type=int, default=300, help="Target duration in seconds")
    parser.add_argument("--warmup", type=int, default=10, help="Warmup iterations")
    parser.add_argument("--iterations", type=int, default=0, help="Override: fixed iterations (disables duration)")
    parser.add_argument("--csv", default=str(_PROJECT / "benchmark_results.csv"), help="Output CSV path")
    args = parser.parse_args()

    from pynescript_backend import Runtime

    # Load data
    print("Loading data ...")
    data_dir = _PROJECT / "data" / "BTCUSDT" / "1d"
    all_bars: list[dict] = []
    for f in sorted(data_dir.glob("*.jsonl*")):
        all_bars.extend(load_ohlcv(f))
    print(f"  {len(all_bars)} bars loaded")

    # Load scripts
    scripts = load_scripts()
    print(f"  {len(scripts)} scripts loaded: {', '.join(scripts.keys())}")

    runtime = Runtime(symbol="BENCH")

    # Determine number of iterations
    n = args.iterations or 0
    if n == 0:
        # Estimate: run a few to gauge speed
        print("Estimating iterations ...")
        t0 = time.perf_counter()
        for _ in range(5):
            _ = runtime.run(scripts["big_strategy"], all_bars)
        elapsed = time.perf_counter() - t0
        est_per_run = elapsed / 5
        n = max(10, int(args.duration / est_per_run))
        print(f"  ~{est_per_run * 1000:.1f} ms/run → {n} iterations in {args.duration}s")

    # Run benchmark for each script
    results: list[dict] = []
    for name, source in scripts.items():
        print(f"\nBenchmarking '{name}' ({n} iterations) ...")
        stats = run_and_measure(runtime, source, all_bars, n=n, warmup=args.warmup)
        stats["script"] = name
        results.append(stats)
        print(
            f"  avg={stats['avg_ms']}ms  min={stats['min_ms']}ms  max={stats['max_ms']}ms  "
            f"rpm={stats['runs_per_minute']}  errors={stats['errors']}"
        )

    # Write CSV
    csv_path = Path(args.csv)
    fieldnames = ["script", "n", "bars", "errors", "total_ms", "avg_ms", "min_ms", "max_ms", "runs_per_minute"]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    print(f"\nResults written to {csv_path}")
    print("Done.")


if __name__ == "__main__":
    main()
