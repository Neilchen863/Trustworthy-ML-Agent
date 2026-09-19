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
        for n in nodes:
            n["step"] = int(n.get("step") or 0)
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


def delivery_check(run_dir: Path, expected_variant: str | None, notes_first_line: str | None) -> dict:
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
    return {"ok": not problems, "problems": problems, "prompt_variant": got_variant}


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
