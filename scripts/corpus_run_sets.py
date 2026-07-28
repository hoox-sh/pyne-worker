#!/usr/bin/env python3
# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run pynescript corpus set01–set04 through pyne-worker Runtime.

Each file is sanitized (same helper as pynescript corpus), then executed with
``Runtime.run`` on synthetic OHLCV. Process pool + hard timeout (same pattern
as pynescript ``corpus_parse_sets.py``).

Usage (from pyne-worker root)::

    python scripts/corpus_run_sets.py
    python scripts/corpus_run_sets.py --sets set01,set02 --timeout 8 --mode parse
    python scripts/corpus_run_sets.py --sets set01,set02,set03 --mode compile --timeout 20
    python scripts/corpus_run_sets.py --resume --workers 4

Modes:
  parse    — parse+unparse only (via pynescript in this env)
  run      — full Runtime evaluation over synthetic bars (interpret, default)
  compile  — Runtime with mode=compile (Numba/object bar loop)

Writes under ``.cache/``:
  pyne_corpus_set01_set04.csv
  pyne_corpus_set01_set04_summary.txt
"""

from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT = _HERE.parent
_PYNESCRIPT = Path("/mnt/data/home/jango/Git/pynescript")
if not _PYNESCRIPT.exists():
    _PYNESCRIPT = _PROJECT.parent / "pynescript"

# Worker processes need these paths at import time of _run_one
_SRC = str(_PROJECT / "src")
_PYNE_SRC = str(_PYNESCRIPT / "src")

DATA = _PYNESCRIPT / "tests" / "data"
CACHE = _PROJECT / ".cache"


def _make_bars(n: int = 50) -> list[dict]:
    bars: list[dict] = []
    price = 100.0
    for i in range(n):
        o = round(price, 2)
        c = round(price + (1 if i % 3 else -0.5), 2)
        h = round(max(o, c) + 0.8, 2)
        l = round(min(o, c) - 0.8, 2)
        bars.append(
            {
                "open": o,
                "high": h,
                "low": max(l, 0.01),
                "close": c,
                "time": 1_000_000 + i * 86_400_000,
                "volume": 1000.0 + i,
            }
        )
        price = c
    return bars


# Shared in workers after first use
_BARS_CACHE: dict[int, list[dict]] = {}


def _run_one(args: tuple[str, str, int]) -> tuple[str, str, str, int]:
    """Return (path, status, error, ms). Runs in worker process."""
    path_str, mode, n_bars = args
    if _SRC not in sys.path:
        sys.path.insert(0, _SRC)
    if _PYNE_SRC not in sys.path:
        sys.path.insert(0, _PYNE_SRC)

    t0 = time.perf_counter()
    try:
        from pynescript.ast.helper import parse, unparse
        from pynescript.util.corpus_sanitize import sanitize_corpus_source

        raw = Path(path_str).read_text(encoding="utf-8", errors="replace")
        src = sanitize_corpus_source(raw)

        if mode == "parse":
            unparse(parse(src))
            return path_str, "OK", "", int((time.perf_counter() - t0) * 1000)

        from pynescript_backend import Runtime

        if n_bars not in _BARS_CACHE:
            _BARS_CACHE[n_bars] = _make_bars(n_bars)
        run_mode = "compile" if mode == "compile" else "interpret"
        result = Runtime(symbol="BTCUSDT").run(
            src, _BARS_CACHE[n_bars], mode=run_mode
        )
        ms = int((time.perf_counter() - t0) * 1000)
        err = result.get("error")
        if err:
            err_s = str(err)[:200]
            if err_s.startswith("Syntax Error") or err_s.startswith("Parse Error"):
                return path_str, "PARSE_FAIL", err_s, ms
            if result.get("timed_out"):
                return path_str, "TIMEOUT", err_s, ms
            return path_str, "RUN_FAIL", err_s, ms
        return path_str, "OK", "", ms
    except Exception as e:  # noqa: BLE001
        ms = int((time.perf_counter() - t0) * 1000)
        msg = f"{type(e).__name__}: {str(e).split(chr(10))[0][:180]}"
        return path_str, "FAIL", msg, ms


def _set_of(path: Path) -> str:
    try:
        return path.relative_to(DATA).parts[0]
    except ValueError:
        return "?"


def _rel_of(path: Path) -> str:
    try:
        return str(path.relative_to(DATA))
    except ValueError:
        return str(path)


def _load_done(csv_path: Path) -> set[str]:
    done: set[str] = set()
    if not csv_path.exists():
        return done
    try:
        with csv_path.open(encoding="utf-8", newline="") as fp:
            for row in csv.DictReader(fp):
                f = (row.get("file") or "").strip()
                if f:
                    done.add(f)
    except Exception as e:  # noqa: BLE001
        print(f"warning: resume CSV: {e}", flush=True)
    return done


def _write_summary(
    summary_path: Path,
    total_all: int,
    ok: int,
    fail: int,
    timeout_n: int,
    parse_fail: int,
    run_fail: int,
    by_set: dict[str, Counter],
    err_bucket: Counter,
    sets: list[str],
    elapsed: float,
    skipped: int,
    mode: str,
) -> str:
    processed = ok + fail
    lines = [
        f"mode={mode} total_corpus={total_all} processed={processed} skipped_resume={skipped} "
        f"OK={ok} PARSE_FAIL={parse_fail} RUN_FAIL={run_fail} FAIL={fail} TIMEOUT={timeout_n} "
        f"rate={100 * ok / max(processed, 1):.2f}% elapsed_s={elapsed:.1f}",
        "by_set:",
    ]
    for s in sets:
        c = by_set.get(s, Counter())
        n = sum(c.values())
        lines.append(
            f"  {s}: OK={c['OK']} PARSE_FAIL={c['PARSE_FAIL']} RUN_FAIL={c['RUN_FAIL']} "
            f"TIMEOUT={c['TIMEOUT']} FAIL={c['FAIL']} total={n}"
        )
    lines.append("top_errors:")
    for msg, n in err_bucket.most_common(40):
        lines.append(f"  {n:5}  {msg}")
    text = "\n".join(lines) + "\n"
    summary_path.write_text(text, encoding="utf-8")
    return text


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sets", default="set01,set02,set03,set04")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--bars", type=int, default=50)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument(
        "--mode",
        choices=("run", "parse", "compile"),
        default="run",
        help="parse | run (interpret) | compile (Numba/object path)",
    )
    ap.add_argument("--out", type=Path, default=CACHE / "pyne_corpus_set01_set04.csv")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--progress-every", type=int, default=25)
    args = ap.parse_args()
    sets = [s.strip() for s in args.sets.split(",") if s.strip()]

    if not DATA.is_dir():
        print(f"error: pynescript data dir missing: {DATA}", flush=True)
        sys.exit(1)

    all_files: list[Path] = []
    for s in sets:
        d = DATA / s
        if not d.is_dir():
            print(f"warning: missing {d}", flush=True)
            continue
        all_files.extend(sorted(d.rglob("*.pine")))
    total_all = len(all_files)

    done_paths = _load_done(args.out) if args.resume else set()
    files = [p for p in all_files if _rel_of(p) not in done_paths]
    skipped = total_all - len(files)

    print(
        f"pyne-worker corpus: {len(files)} scripts "
        f"(corpus={total_all}, resume_skip={skipped}) "
        f"sets={sets} mode={args.mode} timeout={args.timeout}s "
        f"bars={args.bars} workers={args.workers}",
        flush=True,
    )
    if not files:
        print("Nothing to do.", flush=True)
        return

    CACHE.mkdir(parents=True, exist_ok=True)
    ok = fail = timeout_n = parse_fail = run_fail = 0
    by_set: dict[str, Counter] = {s: Counter() for s in sets}
    err_bucket: Counter = Counter()

    if args.resume and args.out.exists():
        with args.out.open(encoding="utf-8", newline="") as fp:
            for row in csv.DictReader(fp):
                st = row.get("status") or ""
                sn = row.get("set") or "?"
                by_set.setdefault(sn, Counter())[st] += 1
                if st == "OK":
                    ok += 1
                elif st == "TIMEOUT":
                    timeout_n += 1
                    fail += 1
                    err_bucket[(row.get("error") or "TIMEOUT")[:100]] += 1
                elif st == "PARSE_FAIL":
                    parse_fail += 1
                    fail += 1
                    err_bucket[(row.get("error") or "")[:100]] += 1
                elif st == "RUN_FAIL":
                    run_fail += 1
                    fail += 1
                    err_bucket[(row.get("error") or "")[:100]] += 1
                else:
                    fail += 1
                    err_bucket[(row.get("error") or "")[:100]] += 1

    t_all = time.perf_counter()
    mode_w = "a" if (args.resume and args.out.exists()) else "w"
    write_header = mode_w == "w" or not args.out.exists() or args.out.stat().st_size == 0
    summary_path = args.out.with_name(args.out.stem + "_summary.txt")
    done_new = 0
    total_new = len(files)
    queue = list(files)

    ctx = mp.get_context("spawn")

    def new_pool() -> mp.pool.Pool:
        return ctx.Pool(processes=args.workers, maxtasksperchild=30)

    pool = new_pool()

    def kill_pool() -> None:
        nonlocal pool
        try:
            pool.terminate()
        except Exception:  # noqa: BLE001
            pass
        try:
            pool.join()
        except Exception:  # noqa: BLE001
            pass

    try:
        with args.out.open(mode_w, newline="", encoding="utf-8") as fp:
            w = csv.DictWriter(fp, fieldnames=["file", "set", "status", "ms", "error"])
            if write_header:
                w.writeheader()
                fp.flush()

            in_flight: list[tuple[Path, mp.pool.AsyncResult, float]] = []

            def submit(p: Path) -> None:
                ar = pool.apply_async(_run_one, ((str(p), args.mode, args.bars),))
                in_flight.append((p, ar, time.perf_counter()))

            def fill() -> None:
                while len(in_flight) < args.workers and queue:
                    submit(queue.pop(0))

            fill()

            while in_flight or queue:
                if not in_flight:
                    fill()
                    if not in_flight:
                        break

                completed_idx = None
                for i, (_p, ar, _t0) in enumerate(in_flight):
                    if ar.ready():
                        completed_idx = i
                        break

                if completed_idx is not None:
                    p, ar, t0 = in_flight.pop(completed_idx)
                    try:
                        _path, status, error, ms = ar.get(timeout=0)
                    except Exception as e:  # noqa: BLE001
                        status = "FAIL"
                        error = f"{type(e).__name__}: {e}"[:200]
                        ms = int((time.perf_counter() - t0) * 1000)
                else:
                    p, ar, t0 = in_flight[0]
                    remaining = args.timeout - (time.perf_counter() - t0)
                    if remaining <= 0:
                        remaining = 0.05
                    try:
                        _path, status, error, ms = ar.get(timeout=remaining)
                        in_flight.pop(0)
                    except mp.TimeoutError:
                        status = "TIMEOUT"
                        error = f"exceeded {args.timeout:.0f}s"
                        ms = int((time.perf_counter() - t0) * 1000)
                        rest = [x[0] for x in in_flight[1:]]
                        in_flight.clear()
                        kill_pool()
                        pool = new_pool()
                        for rp in reversed(rest):
                            queue.insert(0, rp)
                    except Exception as e:  # noqa: BLE001
                        status = "FAIL"
                        error = f"{type(e).__name__}: {e}"[:200]
                        ms = int((time.perf_counter() - t0) * 1000)
                        rest = [x[0] for x in in_flight[1:]]
                        in_flight.clear()
                        kill_pool()
                        pool = new_pool()
                        for rp in reversed(rest):
                            queue.insert(0, rp)

                set_name = _set_of(p)
                rel = _rel_of(p)
                by_set.setdefault(set_name, Counter())[status] += 1
                if status == "OK":
                    ok += 1
                elif status == "TIMEOUT":
                    timeout_n += 1
                    fail += 1
                    err_bucket[error[:100]] += 1
                elif status == "PARSE_FAIL":
                    parse_fail += 1
                    fail += 1
                    err_bucket[error[:100]] += 1
                elif status == "RUN_FAIL":
                    run_fail += 1
                    fail += 1
                    err_bucket[error[:100]] += 1
                else:
                    fail += 1
                    err_bucket[error[:100]] += 1

                w.writerow(
                    {"file": rel, "set": set_name, "status": status, "ms": ms, "error": error}
                )
                done_new += 1
                if done_new % 10 == 0:
                    fp.flush()

                fill()

                processed = ok + fail
                if (
                    done_new % args.progress_every == 0
                    or status != "OK"
                    or done_new == 1
                    or done_new == total_new
                ):
                    rate = 100 * ok / max(processed, 1)
                    elapsed = time.perf_counter() - t_all
                    left = total_new - done_new
                    eta = (elapsed / max(done_new, 1)) * left
                    print(
                        f"  [{done_new}/{total_new} new | {processed}/{total_all} all] "
                        f"OK={ok} PARSE={parse_fail} RUN={run_fail} T/O={timeout_n} "
                        f"{rate:.1f}% {ms}ms eta={eta / 60:.1f}m  {status:10} {rel[:50]}",
                        flush=True,
                    )

                if done_new % 200 == 0:
                    _write_summary(
                        summary_path,
                        total_all,
                        ok,
                        fail,
                        timeout_n,
                        parse_fail,
                        run_fail,
                        by_set,
                        err_bucket,
                        sets,
                        time.perf_counter() - t_all,
                        skipped,
                        args.mode,
                    )
    except Exception:
        print("FATAL in main loop:", flush=True)
        traceback.print_exc()
    finally:
        kill_pool()

    elapsed = time.perf_counter() - t_all
    text = _write_summary(
        summary_path,
        total_all,
        ok,
        fail,
        timeout_n,
        parse_fail,
        run_fail,
        by_set,
        err_bucket,
        sets,
        elapsed,
        skipped,
        args.mode,
    )
    print(text, flush=True)
    print(f"Wrote {args.out}", flush=True)
    print(f"Wrote {summary_path}", flush=True)


if __name__ == "__main__":
    mp.freeze_support()
    main()
