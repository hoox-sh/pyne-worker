#!/usr/bin/env python3
"""Re-run FAIL rows from a pyne-worker corpus CSV with hard per-file kill.

Avoids infinite Runtime loops hanging the batch (soft timeouts cannot stop them).
"""

from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import sys
import time
from collections import Counter
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT = _HERE.parent
_PYNESCRIPT = Path("/mnt/data/home/jango/Git/pynescript")
if not _PYNESCRIPT.exists():
    _PYNESCRIPT = _PROJECT.parent / "pynescript"

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


def _worker(path_str: str, n_bars: int, q: mp.Queue) -> None:
    if _SRC not in sys.path:
        sys.path.insert(0, _SRC)
    if _PYNE_SRC not in sys.path:
        sys.path.insert(0, _PYNE_SRC)
    t0 = time.perf_counter()
    try:
        from pynescript.util.corpus_sanitize import sanitize_corpus_source
        from pynescript_backend import Runtime

        raw = Path(path_str).read_text(encoding="utf-8", errors="replace")
        src = sanitize_corpus_source(raw)
        result = Runtime(symbol="BTCUSDT").run(src, _make_bars(n_bars))
        ms = int((time.perf_counter() - t0) * 1000)
        err = result.get("error")
        if err:
            err_s = str(err)[:200]
            if err_s.startswith("Syntax Error") or err_s.startswith("Parse Error"):
                q.put(("PARSE_FAIL", err_s, ms))
            else:
                q.put(("RUN_FAIL", err_s, ms))
        else:
            q.put(("OK", "", ms))
    except Exception as e:  # noqa: BLE001
        ms = int((time.perf_counter() - t0) * 1000)
        q.put(("FAIL", f"{type(e).__name__}: {str(e).split(chr(10))[0][:180]}", ms))


def run_one(path: Path, n_bars: int, timeout: float) -> tuple[str, str, int]:
    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue(1)
    proc = ctx.Process(target=_worker, args=(str(path), n_bars, q))
    proc.start()
    proc.join(timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join(2)
        if proc.is_alive():
            proc.kill()
            proc.join(1)
        return "TIMEOUT", f"exceeded {timeout:.0f}s", int(timeout * 1000)
    if not q.empty():
        return q.get()
    return "FAIL", "worker exited without result", 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--base",
        type=Path,
        default=CACHE / "pyne_corpus_set01_set04.csv",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=CACHE / "pyne_corpus_set01_set04_rerun_fails.csv",
    )
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--bars", type=int, default=50)
    args = ap.parse_args()

    fails: list[str] = []
    base_ok = base_total = 0
    with args.base.open(encoding="utf-8") as fp:
        for row in csv.DictReader(fp):
            base_total += 1
            if row["status"] == "OK":
                base_ok += 1
            else:
                fails.append(row["file"])

    print(
        f"hard-timeout re-run of {len(fails)} fails "
        f"(base_ok={base_ok}/{base_total}, timeout={args.timeout}s)…",
        flush=True,
    )
    ok = parse_fail = run_fail = timeout_n = fail = 0
    err_bucket: Counter[str] = Counter()
    by_set: Counter[tuple[str, str]] = Counter()
    rows: list[dict[str, object]] = []
    t_all = time.perf_counter()

    for i, rel in enumerate(fails, 1):
        status, error, ms = run_one(DATA / rel, args.bars, args.timeout)
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
        sn = rel.split("/")[0]
        by_set[(sn, status)] += 1
        rows.append({"file": rel, "set": sn, "status": status, "ms": ms, "error": error})
        if i % 25 == 0 or status != "OK" and i <= 30 or i == len(fails):
            print(
                f"  [{i}/{len(fails)}] OK={ok} PARSE={parse_fail} RUN={run_fail} "
                f"T/O={timeout_n}  {status:10} {ms}ms  {rel[:52]}",
                flush=True,
            )

    elapsed = time.perf_counter() - t_all
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=["file", "set", "status", "ms", "error"])
        w.writeheader()
        w.writerows(rows)

    new_ok = base_ok + ok
    rate = 100 * new_ok / max(base_total, 1)
    lines = [
        f"rerun_fails={len(fails)} now_OK={ok} PARSE_FAIL={parse_fail} "
        f"RUN_FAIL={run_fail} TIMEOUT={timeout_n} elapsed_s={elapsed:.1f}",
        f"projected_overall OK={new_ok}/{base_total} rate={rate:.2f}% "
        f"(base OK={base_ok} rate={100 * base_ok / max(base_total, 1):.2f}%)",
        "by_set_status:",
    ]
    for (s, st), n in sorted(by_set.items()):
        lines.append(f"  {s} {st}: {n}")
    lines.append("top_errors:")
    for msg, n in err_bucket.most_common(30):
        lines.append(f"  {n:5}  {msg}")
    text = "\n".join(lines) + "\n"
    summary = args.out.with_name(args.out.stem + "_summary.txt")
    summary.write_text(text, encoding="utf-8")
    print(text, flush=True)
    print(f"Wrote {args.out}", flush=True)
    print(f"Wrote {summary}", flush=True)


if __name__ == "__main__":
    mp.freeze_support()
    main()
