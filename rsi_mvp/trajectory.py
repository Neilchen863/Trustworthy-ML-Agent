"""Read a finished AIDE run directory into a Trajectory (spec section 6).

A run directory is what scripts/run_aide.sh leaves behind:

    run_config.txt          key = value lines (competition, selection_mode, ...)
    logs/journal.json       {"nodes": [...]}  -- the solution tree
    logs/aide.log           agent-mode decision lines ([agent search]/[agent submit])
    code/node_id.txt        id of the node whose code/submission was published
    grade_report.txt        official MLE-bench grade (only after grading)
    agent/additional_notes.txt   notes actually staged into the prompt

Nothing here calls an LLM and nothing executes candidate code.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def read_run_config(run_dir: Path) -> dict[str, str]:
    cfg: dict[str, str] = {}
    path = run_dir / "run_config.txt"
    if path.is_file():
        for line in path.read_text(errors="ignore").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                cfg[key.strip()] = value.strip()
    return cfg


def read_journal(run_dir: Path) -> list[dict]:
    """Nodes sorted by step.  Falls back to filtered_journal.json exactly as the
    vendored issue scanner does, so both see the same tree."""
    for name in ("journal.json", "filtered_journal.json"):
        path = run_dir / "logs" / name
        if not path.is_file() or path.stat().st_size == 0:
            continue
        try:
            data = json.loads(path.read_text(errors="ignore"))
        except json.JSONDecodeError:
            continue
        nodes = data["nodes"] if isinstance(data, dict) else data
        node2parent = data.get("node2parent", {}) if isinstance(data, dict) else {}
        for n in nodes:
            n["step"] = int(n.get("step") or 0)
            # Real AIDE journals leave every node's own `parent` empty; the tree is only in
            # the top-level node2parent map.  Older/legacy journals set `parent` directly.
            if not n.get("parent") and n.get("id") in node2parent:
                n["parent"] = node2parent[n["id"]]
        return sorted(nodes, key=lambda n: n["step"])
    return []


def read_grade(run_dir: Path) -> dict | None:
    """Official grade from grade_report.txt, or None if the run is not graded."""
    path = Path(run_dir) / "grade_report.txt"
    if not path.is_file():
        return None
    text = path.read_text(errors="ignore")
    score = re.search(r'"score":\s*([-\d.eE+]+|null)', text)
    if not score:
        return None
    out: dict[str, Any] = {"score": None if score.group(1) == "null" else float(score.group(1))}
    for key in ("any_medal", "above_median", "valid_submission", "is_lower_better", "submission_exists"):
        m = re.search(rf'"{key}":\s*(true|false)', text)
        out[key] = (m.group(1) == "true") if m else None
    for key in ("gold_threshold", "median_threshold"):
        m = re.search(rf'"{key}":\s*([-\d.eE+]+)', text)
        out[key] = float(m.group(1)) if m else None
    return out


def read_final_node_id(run_dir: Path) -> str | None:
    for rel in ("code/node_id.txt", "agent/workspaces/exp/best_solution/node_id.txt"):
        path = run_dir / rel
        if path.is_file() and path.read_text().strip():
            return path.read_text().strip()
    return None


def metric_value(node: dict) -> float | None:
    m = node.get("metric")
    if isinstance(m, dict) and isinstance(m.get("value"), (int, float)) and not node.get("is_buggy"):
        return float(m["value"])
    return None


def node_maximize(nodes: list[dict]) -> bool:
    for n in nodes:
        m = n.get("metric")
        if isinstance(m, dict) and m.get("maximize") is not None and metric_value(n) is not None:
            return bool(m["maximize"])
    return True


SUB_STATS_ON = ("1", "true", "on", "yes")


def sub_stats_evidence(run_dir: Path) -> dict:
    """Did AIDE's submission-profile display actually run?  Three independent signals: the run config line
    `sub_stats` (what the launcher exported), the agent log line the patch writes for every profiled node, and the
    profile text in the journal's node outputs (which the journal often omits, so it can only confirm, never refute)."""
    cfg = read_run_config(run_dir)
    log_path = Path(run_dir) / "logs" / "aide.log"
    log = log_path.read_text(errors="ignore") if log_path.is_file() else ""
    journal = sum("[submission-stats]" in "".join(n["_term_out"] if isinstance(n.get("_term_out"), list) else [str(n.get("_term_out") or "")])
                  for n in read_journal(Path(run_dir)))
    return {"run_config": cfg.get("sub_stats", "off"), "config_on": cfg.get("sub_stats", "off").lower() in SUB_STATS_ON,
            "nodes_profiled_log": len(re.findall(r"\[sub-stats\] profile appended", log)), "nodes_profiled_journal": journal}


def delivery_check(run_dir: Path, expected_variant: str | None, notes_first_line: str | None,
                   expected_seed_code: str | None = None, expected_sub_stats: bool | None = None) -> dict:
    """Did the harness actually reach the run?  A silently undelivered harness
    would make H_t and H_{t+1} identical while the loop believes otherwise."""
    cfg = read_run_config(run_dir)
    problems = []
    got_variant = cfg.get("prompt_variant", "none")
    want_variant = expected_variant or "none"
    if got_variant != want_variant:
        problems.append(f"run_config prompt_variant={got_variant!r}, expected {want_variant!r}")
    if notes_first_line:
        staged = run_dir / "agent" / "additional_notes.txt"
        if not staged.is_file():
            problems.append("agent/additional_notes.txt missing; cannot confirm notes delivery")
        elif notes_first_line not in staged.read_text(errors="ignore"):
            problems.append("rendered harness notes not found in staged additional_notes.txt")
    if expected_seed_code:
        nodes = read_journal(run_dir)
        first = nodes[0].get("code", "") if nodes else ""
        if first.strip() != expected_seed_code.strip():
            problems.append("first node's code is not the pinned first-draft seed")
        if "rsi_" not in cfg.get("seed_code", "none"):
            problems.append(f"run_config seed_code={cfg.get('seed_code', 'none')!r} is not a staged rsi seed")
    out = {"ok": True, "problems": problems, "prompt_variant": got_variant}
    if expected_sub_stats is not None:                        # observation policy: verified in BOTH directions
        ev = sub_stats_evidence(run_dir)
        shown = ev["nodes_profiled_log"] > 0 or ev["nodes_profiled_journal"] > 0
        if expected_sub_stats and not (ev["config_on"] and shown):
            problems.append("the harness enables the submission profile but the run does not show it "
                            f"(run_config sub_stats={ev['run_config']!r}, profiled nodes in log={ev['nodes_profiled_log']}, "
                            f"in journal={ev['nodes_profiled_journal']})")
        if not expected_sub_stats and (ev["config_on"] or shown):
            problems.append("the submission profile appeared in the run although the harness did not enable it "
                            f"(run_config sub_stats={ev['run_config']!r}, profiled nodes in log={ev['nodes_profiled_log']})")
        out["sub_stats"] = {"expected": expected_sub_stats, **ev}
    out["ok"] = not problems
    return out


@dataclass
class Trajectory:
    run_dir: Path
    task: str
    mode: str
    round: int
    harness_version: str
    nodes: list[dict]
    events: list[dict]
    final_node_id: str | None
    grade: dict | None           # None when withheld (test role) or not graded
    grade_revealed: bool
    delivery: dict = field(default_factory=dict)
    run_config: dict = field(default_factory=dict)

    def summary(self) -> dict:
        good = [n for n in self.nodes if metric_value(n) is not None]
        return {
            "run_dir": str(self.run_dir),
            "task": self.task,
            "mode": self.mode,
            "round": self.round,
            "harness_version": self.harness_version,
            "n_nodes": len(self.nodes),
            "n_good_nodes": len(good),
            "n_buggy_nodes": sum(1 for n in self.nodes if n.get("is_buggy")),
            "final_node_id": self.final_node_id,
            "task_performance": self.grade if self.grade_revealed else None,
            "delivery": self.delivery,
        }
