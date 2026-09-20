"""Synthetic AIDE run directories in the exact layout scripts/run_aide.sh leaves.

Nothing here needs CRC, an LLM or the research repo: every test builds the run it
needs, so the suite is self-contained (the repo is vendored on purpose).
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("GIT_AUTHOR_NAME", "t")
os.environ.setdefault("GIT_AUTHOR_EMAIL", "t@example.com")
os.environ.setdefault("GIT_COMMITTER_NAME", "t")
os.environ.setdefault("GIT_COMMITTER_EMAIL", "t@example.com")


def nid(ch: str) -> str:
    return ch * 8 + "0" * 24


# (char, step, parent_char|None, val|None(buggy), analysis)
DEFAULT_NODES = [
    ("a", 1, None, 0.60, "baseline logistic regression"),
    ("b", 2, None, None, "crashed with KeyError; fix the column name"),
    ("c", 3, "a", 0.66, "adding tf-idf helped"),
    ("d", 4, "c", 0.70, "gradient boosting helped further"),
]


def make_run(base: Path, name: str = "run_a", mode: str = "agent", nodes=None, final: str = "d",
             grade: float | None = 0.65, constant_submission: bool = False, variant: str | None = None,
             notes: str | None = None, agent_log: list[str] | None = None,
             competition: str = "random-acts-of-pizza", legacy_parent_field: bool = False) -> Path:
    """Default journal layout = what real AIDE writes: node["parent"] empty, tree in node2parent.
    legacy_parent_field=True sets node["parent"] directly instead."""
    nodes = nodes or DEFAULT_NODES
    run = base / name
    (run / "logs").mkdir(parents=True)
    (run / "code").mkdir()
    (run / "submission").mkdir()
    (run / "agent").mkdir()
    (run / "run_config.txt").write_text(
        f"competition      = {competition}\nrun_id           = {name}\nselection_mode   = {mode}\n"
        f"prompt_variant   = {variant or 'none'}\n")
    journal, node2parent = [], {}
    for ch, step, parent, val, analysis in nodes:
        buggy = val is None
        journal.append({
            "code": "import pandas as pd\nprint('fit')", "plan": f"plan {ch}", "step": str(step), "id": nid(ch),
            "parent": nid(parent) if (parent and legacy_parent_field) else None, "children": [],
            "_term_out": ["Traceback (most recent call last):\nKeyError: 'x'\n"] if buggy
            else [f"Validation AUC: {val}\n"],
            "exec_time": 12.5, "exc_type": "KeyError" if buggy else None, "analysis": analysis,
            "metric": {"value": None, "maximize": None} if buggy else {"value": val, "maximize": True},
            "is_buggy": buggy})
        if parent:
            node2parent[nid(ch)] = nid(parent)
    (run / "logs" / "journal.json").write_text(
        json.dumps({"nodes": journal, "node2parent": node2parent, "__version": "2"}))
    (run / "code" / "node_id.txt").write_text(nid(final))
    rng = random.Random(0)
    rows = ["request_id,requester_received_pizza"]
    for i in range(200):
        rows.append(f"t3_{i},{0.5 if constant_submission else round(rng.random(), 6)}")
    (run / "submission" / "submission.csv").write_text("\n".join(rows) + "\n")
    if grade is not None:
        (run / "grade_report.txt").write_text(
            '{\n "competition_id": "%s",\n "score": %s,\n "gold_threshold": 0.97908,\n'
            ' "median_threshold": 0.5995,\n "any_medal": false,\n "above_median": true,\n'
            ' "valid_submission": true,\n "is_lower_better": false\n}\n' % (competition, grade))
    if notes is not None:
        (run / "agent" / "additional_notes.txt").write_text("base notes\n------\n" + notes)
    if agent_log is not None:
        (run / "logs" / "aide.log").write_text("\n".join(agent_log) + "\n")
    return run


AGENT_LOG = [
    "[2026-07-10 01:13:23,845] INFO: [agent search] nothing to improve/debug -> draft",
    "[2026-07-10 01:16:39,753] INFO: [agent submit] submission set to node aaaaaaaa",
    "[2026-07-10 01:16:41,000] INFO: [agent search] LLM chose action=draft node= (try another family)",
    "[2026-07-10 01:20:00,000] INFO: [agent search] LLM chose action=improve node=aaaaaaaa (a is the best so far)",
    "[2026-07-10 01:20:38,000] INFO: [agent submit] LLM chose node=cccccccc (c beats a on validation)",
    "[2026-07-10 01:25:00,000] INFO: [agent search] LLM chose action=improve node=cccccccc (keep refining c)",
    "[2026-07-10 01:26:00,000] INFO: [agent submit] LLM chose node=dddddddd (d has the highest validation AUC)",
]


@pytest.fixture
def runs(tmp_path):
    return tmp_path / "runs"


@pytest.fixture
def tasks_dir():
    return ROOT / "tasks"


@pytest.fixture
def state(tmp_path):
    """Isolated state root that is itself a git repo (harness versions get committed)."""
    root = tmp_path / "state"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    return root


M2_CODE = ("from sklearn.preprocessing import StandardScaler\nfrom sklearn.model_selection import train_test_split\n"
           "scaler = StandardScaler()\nXs = scaler.fit_transform(data_all)\ntr, va = train_test_split(Xs)\n")


def set_node_code(run: Path, chars, code: str = M2_CODE) -> None:
    """Give the nodes whose id char is in `chars` code that the scanner's M2 (fit-before-split) flags."""
    path = run / "logs" / "journal.json"
    j = json.loads(path.read_text())
    for n in j["nodes"]:
        if n["id"][0] in chars:
            n["code"] = code
    path.write_text(json.dumps(j))
