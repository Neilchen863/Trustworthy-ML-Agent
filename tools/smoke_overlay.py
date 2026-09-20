#!/usr/bin/env python3
"""Smoke checks for a dedicated agent-fix overlay (run on CRC).  Python 3.9 compatible, stdlib only.

  static  : inside the container, parse the same lines with the parser found in each overlay.  The SHARED overlay
            is the control: it must still show the 5.0 bug, otherwise the check has no discriminating power.
  submit  : submit a tiny real job (1 step, 15 min cap) whose first draft prints a "N-fold CV" summary line, with a
            chosen overlay.  The job's recorded validation metric is 0.6323 with the fix and 5.0 without it.
  read    : read that job's journal and the job's log line naming the overlay it mounted.

    python tools/smoke_overlay.py static --sif SIF --overlay NEW --control SHARED
    python tools/smoke_overlay.py submit --aide-root ROOT --overlay NEW --label patched
    python tools/smoke_overlay.py read   --aide-root ROOT --job JOBID --expect 0.6323
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys

# line -> the metric a correct parser must return
CASES = {
    "Mean AUC over 5-fold CV: 0.6323": 0.6323,
    "Mean AUC score (5-fold CV): 0.6222062365826799": 0.6222062365826799,
    "Cross-validation AUC (mean, 5 folds): 0.6323": 0.6323,
    "Average ROC AUC across 5 folds: 0.6323": 0.6323,
    "The mean AUC score from 5-fold cross-validation is 0.6065": 0.6065,
    "Mean CV AUC (10-fold): 0.71 +/- 0.02": 0.71,
    "5-fold CV mean AUC: 0.6323": 0.6323,          # these two were already parsed correctly before the fix
    "Mean CV AUC: 0.6323": 0.6323,
}
PROBE_LINE = "Mean AUC over 5-fold CV: 0.6323"
PROBE_SEED = '''import json
import os

import pandas as pd

# Smoke probe: prints the exact "N-fold CV" phrasing that the metric guard used to read as 5.0.
os.makedirs("./submission", exist_ok=True)
test = pd.DataFrame(json.load(open("./input/test/test.json")))
pd.DataFrame({"request_id": test["request_id"], "requester_received_pizza": 0.25}).to_csv(
    "./submission/submission.csv", index=False)
print("%s")
''' % PROBE_LINE

CONTAINER_SNIPPET = (
    "import json, sys\n"
    "from aide._metric_parser import parse_metric_evidence\n"
    "cases = json.loads(sys.stdin.read())\n"
    "print('RESULT ' + json.dumps({c: parse_metric_evidence(c).get('aggregate') for c in cases}))\n"
)


def evaluate_static(parsed: dict, expected: dict = CASES) -> dict:
    """{'wrong': [(line, got, want)], 'ok': bool}.  Pure, so it can be unit-tested."""
    wrong = [(line, parsed.get(line), want) for line, want in expected.items()
             if parsed.get(line) is None or abs(parsed[line] - want) > 1e-9]
    return {"wrong": wrong, "ok": not wrong}


def parse_in_container(sif: str, overlay: str) -> dict:
    cmd = ["apptainer", "exec", "--overlay", overlay + ":ro", sif, "bash", "-lc",
           "source /opt/conda/etc/profile.d/conda.sh && conda activate agent && python -c \"$SNIPPET\""]
    proc = subprocess.run(cmd, input=json.dumps(list(CASES)), capture_output=True, text=True,
                          env={**os.environ, "SNIPPET": CONTAINER_SNIPPET})
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    raise SystemExit("container parse failed: " + (proc.stderr or proc.stdout)[-400:])


def cmd_static(args) -> int:
    new = evaluate_static(parse_in_container(args.sif, args.overlay))
    ctl = evaluate_static(parse_in_container(args.sif, args.control))
    print(f"dedicated overlay {args.overlay}: {'OK - every line parsed correctly' if new['ok'] else 'WRONG'}")
    for line, got, want in new["wrong"]:
        print(f"   x {line!r} -> {got} (want {want})")
    bug_lines = [w[0] for w in ctl["wrong"]]
    print(f"control (shared) overlay {args.control}: {len(bug_lines)} line(s) still misparsed -> "
          + ("the bug is visible, so this check can fail" if bug_lines else "NO misparse: the check is not discriminating"))
    for line, got, want in ctl["wrong"]:
        print(f"   . {line!r} -> {got} (want {want})")
    return 0 if new["ok"] and bug_lines else 1


def cmd_submit(args) -> int:
    root = os.path.expanduser(args.aide_root)
    seed = os.path.join(root, "config", "seeds", "rsi_smoke_probe.code.py")
    if os.path.exists(seed) and open(seed).read() != PROBE_SEED:
        raise SystemExit(f"{seed} exists with different content")
    os.makedirs(os.path.dirname(seed), exist_ok=True)
    open(seed, "w").write(PROBE_SEED)
    env = {**os.environ, "OVERLAY_PATH": os.path.abspath(os.path.expanduser(args.overlay)), "AIDE_SEED_CODE": seed,
           "AIDE_STEPS": "1", "TIME_LIMIT_SECS": "900", "EXEC_TIMEOUT": "300", "REQ_CPUS": "2",
           "AIDE_SELECTION_MODE": "rule", "SKIP_TASK_NOTES": "1", "AIDE_FEEDBACK": "0", "AIDE_TEST_FEEDBACK": "0",
           "AIDE_TEST_EXPOSE": "none", "AIDE_CODE_MODEL": "gpt-4o-2024-08-06",
           "AIDE_FEEDBACK_MODEL": "gpt-4o-2024-08-06"}
    proc = subprocess.run(["bash", "sge/submit.sh", "random-acts-of-pizza"], cwd=root, env=env,
                          capture_output=True, text=True)
    m = re.search(r"[Yy]our job (\d+)", proc.stdout + proc.stderr)
    if proc.returncode or not m:
        raise SystemExit("submit failed: " + (proc.stdout + proc.stderr)[-400:])
    print(f"submitted {args.label} probe: job {m.group(1)} overlay={env['OVERLAY_PATH']}")
    return 0


def cmd_read(args) -> int:
    root = os.path.expanduser(args.aide_root)
    runs = sorted(glob.glob(os.path.join(root, "runs", "random-acts-of-pizza", f"*_j{args.job}")))
    if not runs:
        print("no run directory yet")
        return 2
    run = runs[-1]
    journal = os.path.join(run, "logs", "journal.json")
    if not os.path.isfile(journal):
        print("run started, no journal yet")
        return 2
    node = json.load(open(journal))["nodes"][0]
    value = node.get("metric", {}).get("value")
    analysis = node.get("analysis") or ""
    guard = re.search(r"\[metric-guard\][^\n]*", analysis)
    mounted = set()
    for path in glob.glob(os.path.join(run, "logs", "*.log")) + glob.glob(os.path.join(root, f"sge-{args.job}*.out")) \
            + glob.glob(os.path.join(root, "runs", f"*{args.job}*.out")):
        mounted.update(re.findall(r"Using agent-fix overlay: (\S+)", open(path, errors="ignore").read()))
    print(f"run: {os.path.basename(run)}")
    print(f"recorded metric of the probe node: {value}")
    print(f"metric-guard note: {guard.group(0) if guard else '(none: guard agreed with the reviewer)'}")
    print(f"overlay mounted by the job: {sorted(mounted) or 'not found in logs'}")
    ok = value is not None and abs(value - args.expect) < 1e-9
    print(("OK" if ok else "MISMATCH") + f" (expected {args.expect})")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("static"); s.add_argument("--sif", required=True); s.add_argument("--overlay", required=True)
    s.add_argument("--control", required=True); s.set_defaults(fn=cmd_static)
    s = sub.add_parser("submit"); s.add_argument("--aide-root", required=True); s.add_argument("--overlay", required=True)
    s.add_argument("--label", default="probe"); s.set_defaults(fn=cmd_submit)
    s = sub.add_parser("read"); s.add_argument("--aide-root", required=True); s.add_argument("--job", required=True)
    s.add_argument("--expect", type=float, required=True); s.set_defaults(fn=cmd_read)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
