#!/usr/bin/env python3
"""Replay the feedback REVIEWER on recorded nodes, with and without AIDE's submission profile (run on CRC; needs OPENAI_API_KEY).

Question: when a candidate's test predictions are degenerate (constant / near constant), does the reviewer (whose findings are the
only thing the submission-choosing LLM reads about a node) mention it once the profile is in its execution output?

Design (fixed before any call): take the reviewer prompt AIDE really sent for a node (aide.verbose.log), append the profile exactly as the
patch does (after the execution output, computed from that node's archived submission.csv), and call the same model (gpt-4o-2024-08-06,
temperature 0.5) with the same `submit_review` function.  Conditions: WITH the profile on every chosen node; WITHOUT it on the --plain
nodes (the prompt unchanged).  The recorded original review is listed for reference.

Pre-registered readouts per response (nothing else is scored, nothing is dropped):
  mentions_degenerate  the summary matches constant|identical|same value|single value|n_unique|unique value|no variation|zero variance|
                       std 0|degenerate|all predictions
  is_bug               the reviewer marks the run as buggy
  metric               the validation metric it reports (a reviewer that still reports 1.0 has not discounted the run)
Hard cost cap enforced by counting returned token usage at list price.

    python tools/replay_reviewer.py --run-dir RUN --nodes 20 16 7 14 --plain 20 --n 3 --cap 0.40 --out replay.json
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rsi_mvp.llm import LLMError, OpenAIChat                      # noqa: E402
from rsi_mvp.stage_trace import _RECORD                            # noqa: E402

DEGENERATE = re.compile(r"constant|identical|same (?:value|prediction)|single (?:value|unique)|n_unique|unique value|no variation|"
                        r"zero variance|std\s*(?:=|of)?\s*0\b|degenerate|all predictions", re.I)


def profile_block(csv_path: Path) -> str:
    import csv
    rows = list(csv.reader(csv_path.open()))
    head = rows[0]
    lines = [f"[submission-stats] submission/submission.csv: {len(rows) - 1} rows x {len(head)} cols"]
    for j, name in enumerate(head[:8]):
        col = [r[j] for r in rows[1:]]
        try:
            v = [float(x) for x in col]
        except ValueError:
            lines.append(f"[submission-stats] column '{name}': non-numeric, n_unique={len(set(col))}, NaN=0")
            continue
        sd = statistics.stdev(v) if len(v) > 1 else float("nan")
        lines.append(f"[submission-stats] column '{name}': n_unique={len(set(v))}, min={min(v):.6g}, max={max(v):.6g}, "
                     f"mean={statistics.mean(v):.6g}, std={sd:.6g}, NaN=0")
    return "\n".join(lines)


def find_review(records: list, code: str):
    for i, r in enumerate(records):
        if "You have written code to solve" in r[:400] and code.strip()[:400] in r:
            spec = records[i + 1]
            m = re.match(r"\[[^\]]+\] INFO: function spec: (\{.*\})", spec.strip().splitlines()[0])
            return r.split("INFO: system: ", 1)[1].rstrip("\n") + "\n", ast.literal_eval(m.group(1))
    raise SystemExit("no reviewer prompt found for this node's code")


def with_profile(prompt: str, block: str) -> str:
    k = prompt.rfind("```")
    return prompt[:k] + block + "\n\n" + prompt[k:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--nodes", type=int, nargs="+", required=True, help="journal steps")
    ap.add_argument("--plain", type=int, nargs="*", default=[], help="steps that also get the UNCHANGED prompt")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--cap", type=float, default=0.40)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    run = Path(a.run_dir)
    nodes = {int(n["step"]): n for n in json.load(open(run / "logs" / "journal.json"))["nodes"]}
    text = (run / "logs" / "aide.verbose.log").read_text(errors="ignore")
    starts = [m.start() for m in _RECORD.finditer(text)]
    records = [text[x:y] for x, y in zip(starts, starts[1:] + [len(text)])]
    chat = OpenAIChat(model="gpt-4o-2024-08-06", temperature=0.5, max_cost_usd=a.cap)
    results = []
    for step in a.nodes:
        node = nodes[step]
        prompt, spec = find_review(records, node["code"])
        block = profile_block(run / "agent" / "workspaces" / "exp" / "submissions" / (node["id"].replace("-", "") + ".csv"))
        conds = [("with_profile", with_profile(prompt, block))] + ([("plain", prompt)] if step in a.plain else [])
        tools = [{"type": "function", "function": {"name": spec["name"], "description": spec["description"], "parameters": spec["json_schema"]}}]
        entry = {"step": step, "profile": block.splitlines()[-1], "original_review": re.sub(r"\[metric-guard\].*", "", " ".join((node.get("analysis") or "").split()))[:400],
                 "samples": []}
        for cond, content in conds:
            for k in range(a.n):
                try:
                    data = chat._post({"model": chat.model, "temperature": 0.5, "messages": [{"role": "system", "content": content}],
                                       "tools": tools, "tool_choice": {"type": "function", "function": {"name": spec["name"]}}})
                    args = json.loads(data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
                except (LLMError, KeyError, IndexError, json.JSONDecodeError) as exc:
                    entry["samples"].append({"condition": cond, "error": str(exc)})
                    if "cost cap" in str(exc):
                        break
                    continue
                entry["samples"].append({"condition": cond, "mentions_degenerate": bool(DEGENERATE.search(args.get("summary", ""))),
                                         "is_bug": args.get("is_bug"), "metric": args.get("metric"), "summary": args.get("summary")})
        results.append(entry)
    out = {"model": chat.model, "temperature": 0.5, "n_per_condition": a.n, "calls": chat.calls, "tokens_in": chat.tokens_in,
           "tokens_out": chat.tokens_out, "cost_usd": round(chat.cost_usd, 4), "cap_usd": a.cap, "results": results}
    json.dump(out, open(a.out, "w"), indent=2)
    print(json.dumps({k: out[k] for k in ("calls", "cost_usd", "cap_usd")}))
    for e in results:
        for cond in ("with_profile", "plain"):
            ss = [s for s in e["samples"] if s["condition"] == cond and "error" not in s]
            if ss:
                print(f"step {e['step']:2d} {cond:12s} n={len(ss)} mentions_degenerate={sum(s['mentions_degenerate'] for s in ss)}/{len(ss)} "
                      f"is_bug={sum(bool(s['is_bug']) for s in ss)}/{len(ss)} metric={[s['metric'] for s in ss]}")


if __name__ == "__main__":
    main()
