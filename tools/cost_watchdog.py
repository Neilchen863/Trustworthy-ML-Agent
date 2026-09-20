#!/usr/bin/env python3
"""Hard spending cap for ONE running AIDE job (run on CRC beside the job).  Python 3.9, stdlib only.

Sums the run's token_usage.jsonl at list price and `qdel`s the job once the total reaches --cap.  The cap is
enforced by wall-clock polling, so it can overshoot by what one poll interval of AIDE spends (~$0.1/min at most).

    python tools/cost_watchdog.py --aide-root ~/mlebench-aide --competition random-acts-of-pizza --job 1234567 --cap 7.5
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time

PRICES = {"gpt-4o": (2.5, 10.0), "gpt-4o-mini": (0.15, 0.6)}      # USD per 1M tokens (in, out)
PESSIMISTIC = (15.0, 60.0)                                         # unknown model: assume it is expensive


def price(model: str) -> tuple:
    best = None
    for name, p in PRICES.items():                                 # longest matching prefix wins (gpt-4o-mini vs gpt-4o)
        if model.startswith(name) and (best is None or len(name) > len(best[0])):
            best = (name, p)
    return best[1] if best else PESSIMISTIC


def cost_of(rows) -> float:
    total = 0.0
    for r in rows:
        pin, pout = price(str(r.get("model", "")))
        total += r.get("in", 0) * pin / 1e6 + r.get("out", 0) * pout / 1e6
    return total


def read_rows(path: str) -> list:
    rows = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass                                       # a half-written last line
    except OSError:
        pass
    return rows


def run_dir_for(aide_root: str, competition: str, job: str):
    hits = glob.glob(os.path.join(aide_root, "runs", competition, f"*_j{job}"))
    return hits[0] if hits else None


def job_alive(job: str) -> bool:
    out = subprocess.run(["qstat"], capture_output=True, text=True).stdout
    return any(line.split()[:1] == [job] for line in out.splitlines())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aide-root", required=True)
    ap.add_argument("--competition", required=True)
    ap.add_argument("--job", required=True)
    ap.add_argument("--cap", type=float, required=True, help="USD; the job is killed when its cost reaches this")
    ap.add_argument("--poll", type=int, default=45)
    args = ap.parse_args()
    root = os.path.expanduser(args.aide_root)
    started = time.time()
    while True:
        run = run_dir_for(root, args.competition, args.job)
        spent = cost_of(read_rows(os.path.join(run, "logs", "token_usage.jsonl"))) if run else 0.0
        alive = job_alive(args.job)
        print(f"[{time.strftime('%H:%M:%S')}] job {args.job} alive={alive} spent=${spent:.2f} / cap ${args.cap:.2f}",
              flush=True)
        if spent >= args.cap:
            subprocess.run(["qdel", args.job])
            if run:
                with open(os.path.join(run, "STOPPED_BY_COST_WATCHDOG.txt"), "w") as f:
                    f.write(f"killed at ${spent:.2f} (cap ${args.cap:.2f})\n")
            print(f"CAP REACHED: qdel {args.job} at ${spent:.2f}", flush=True)
            return 4
        if not alive and time.time() - started > 120:
            print(f"job {args.job} left the queue; final cost ${spent:.2f}", flush=True)
            return 0
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
