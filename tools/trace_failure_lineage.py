#!/usr/bin/env python3
"""Free, read-only trace of ONE finished AIDE run: which nodes use a given field, how the run's nodes relate, and how
each node's own test predictions look (from the per-node submission archive).  Reproduces docs/v3_failure_mechanism_20260921.md.

    python tools/trace_failure_lineage.py RUN_DIR [--field requester_user_flair]
"""
import argparse
import csv
import json
import os
import re
import statistics


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--field", default="requester_user_flair")
    a = ap.parse_args()
    j = json.load(open(os.path.join(a.run_dir, "logs", "journal.json")))
    nodes, n2p = j["nodes"], j["node2parent"]
    idx = {n["id"]: i for i, n in enumerate(nodes)}
    print(f"{a.run_dir}\nnode | parent | val | exc | uses `{a.field}` | test-pred distinct | std | min..max")
    for i, n in enumerate(nodes):
        par = idx.get(n2p.get(n["id"]))
        m = n["metric"]["value"] if n.get("metric") and n["metric"].get("value") is not None else None
        uses = a.field in n["code"]
        path = os.path.join(a.run_dir, "agent", "workspaces", "exp", "submissions", n["id"].replace("-", "") + ".csv")
        if os.path.exists(path):
            v = [float(r["requester_received_pizza"]) for r in csv.DictReader(open(path))]
            pred = f"{len(set(v)):5d} | {statistics.pstdev(v):.4f} | {min(v):.4f}..{max(v):.4f}"
        else:
            pred = "  (no submission archived)"
        print(f"{i:4d} | {str(par):6s} | {'None' if m is None else round(m, 4)} | {n.get('exc_type')} | {uses} | {pred}")
    final = open(os.path.join(a.run_dir, "code", "node_id.txt")).read().strip()
    print("final node:", idx[final], "| nodes that use the field:", [i for i, n in enumerate(nodes) if a.field in n["code"]])
    chain, cur = [final], final
    while cur in n2p:
        cur = n2p[cur]
        chain.append(cur)
    print("lineage of the final node (node indices, child first):", [idx[c] for c in chain])


if __name__ == "__main__":
    main()
