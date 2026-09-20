"""Task wrapper around the EXISTING AIDE runner (spec sections 1, 12).

Both AIDE modes are invoked the same way: through `sge/submit.sh` of the
research repo (`$MLEBENCH_AIDE_ROOT`), configured only by environment variables
and a notes file - exactly the channels the runner already exposes.  Neither
mode is reimplemented and no file of the research repo is edited, except that
the harness notes for a run are staged as a new immutable
`config/tasks/<competition>.notes.<variant>.txt` (the runner's own
PROMPT_VARIANT mechanism).

How each harness component reaches AIDE (all verified against scripts/_common.sh
`_CALLER_OVERRIDE_VARS`, so env.sh cannot silently overwrite them):

  prompt_notes / decision_policy_text / memory lessons
        -> PROMPT_VARIANT notes file -> additional_notes -> task description,
           read by code-generation prompts and (agent mode) decision prompts
  rule_config   -> AIDE_MAX_STAGNATION, AIDE_DEBUG_PROB, AIDE_MAX_DEBUG_DEPTH,
                   AIDE_EXTRA_KWARGS="agent.search.num_drafts=N"
  mode          -> AIDE_SELECTION_MODE=rule|agent
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .harness import Harness
from .task import TaskPackage


class RunnerError(RuntimeError):
    pass


def notes_variant(notes_text: str) -> str | None:
    """PROMPT_VARIANT name: content-addressed, so a variant file is immutable and
    two different harnesses can never share a name.  None for the stock (empty) notes."""
    if not notes_text.strip():
        return None
    return "rsi_" + hashlib.sha256(notes_text.encode()).hexdigest()[:10]


def build_env(task: TaskPackage, mode: str, harness: Harness, variant: str | None,
              replicate: int, aide_root: Path | None = None) -> dict[str, str]:
    """Environment for one run.  Never contains a secret: the runner reads the API
    key from its own config/env.sh on the compute node."""
    common, settings, b = task.common_settings(), task.mode_settings(mode), task.budget
    env = {
        "TIME_LIMIT_SECS": str(b["run_secs"]),
        "AIDE_STEPS": str(b["steps"]),
        "EXEC_TIMEOUT": str(b.get("exec_timeout", 1200)),
        "REQ_CPUS": str(b.get("cpus", 8)),
        "REQ_GPUS": str(b.get("gpus", 0)),
        "AIDE_SELECTION_MODE": mode,
        "AIDE_FEEDBACK": str(common.get("feedback", "0")),
        "AIDE_TEST_FEEDBACK": str(common.get("test_feedback", "0")),
        "AIDE_TEST_EXPOSE": str(common.get("test_expose", "none")),
        "SKIP_TASK_NOTES": "1",   # the harness is the only source of notes
    }
    if common.get("code_model"):
        env["AIDE_CODE_MODEL"] = str(common["code_model"])
    if common.get("feedback_model"):
        env["AIDE_FEEDBACK_MODEL"] = str(common["feedback_model"])
    if mode == "agent":
        env["AIDE_TREE_TOPK"] = str(settings.get("tree_topk", 5))
        env["AIDE_TREE_RECENT"] = str(settings.get("tree_recent", 5))
    else:
        rc = {**task.initial_rule_config(), **(harness.rule_config or {})}
        env["AIDE_MAX_STAGNATION"] = str(rc["max_stagnation"])
        env["AIDE_DEBUG_PROB"] = str(rc["debug_prob"])
        env["AIDE_MAX_DEBUG_DEPTH"] = str(rc["max_debug_depth"])
        env["AIDE_EXTRA_KWARGS"] = f"agent.search.num_drafts={rc['num_drafts']}"
    if variant:
        env["PROMPT_VARIANT"] = variant
    # AIDE_SEED_CODE / AIDE_SEED_PLAN are added by SgeBackend once the seed files are staged on the
    # runner's host (their paths are only known there).
    env["RSI_REPLICATE"] = str(replicate)   # label only; the runner ignores it
    return env


@dataclass
class RunPlan:
    task: str
    competition_id: str
    mode: str
    harness_task: str
    harness_version: str
    round: int
    replicate: int
    env: dict[str, str]
    variant: str | None
    notes_text: str
    notes_sha256: str
    seed_code_text: str | None = None
    seed_plan_text: str | None = None
    command: list[str] = field(default_factory=lambda: ["bash", "sge/submit.sh"])

    @property
    def seed_sha256(self) -> str | None:
        return hashlib.sha256(self.seed_code_text.encode()).hexdigest() if self.seed_code_text else None

    def describe(self) -> dict:
        return {
            "task": self.task, "competition_id": self.competition_id, "mode": self.mode,
            "harness": f"{self.harness_task}/{self.mode}/{self.harness_version}", "round": self.round,
            "replicate": self.replicate, "variant": self.variant, "notes_sha256": self.notes_sha256,
            "seed_sha256": self.seed_sha256,
            "command": self.command + [self.competition_id], "env": self.env,
        }


def make_plan(task: TaskPackage, mode: str, harness: Harness, notes_text: str, round_: int,
              replicate: int, aide_root: Path | None = None) -> RunPlan:
    variant = notes_variant(notes_text)
    seed = task.pinned_first_draft()
    return RunPlan(
        task=task.name, competition_id=task.competition_id, mode=mode, harness_task=harness.task,
        harness_version=harness.version, round=round_, replicate=replicate,
        env=build_env(task, mode, harness, variant, replicate, aide_root), variant=variant,
        notes_text=notes_text, notes_sha256=hashlib.sha256(notes_text.encode()).hexdigest(),
        seed_code_text=seed[0] if seed else None, seed_plan_text=(seed[1] if seed else None) or None)


class DryRunBackend:
    """Prints what would be submitted; touches nothing."""

    def submit(self, plan: RunPlan) -> dict:
        return {"dry_run": True, **plan.describe()}


class SgeBackend:
    def __init__(self, aide_root: str | Path | None = None):
        root = aide_root or os.environ.get("MLEBENCH_AIDE_ROOT")
        if not root:
            raise RunnerError("set MLEBENCH_AIDE_ROOT (or --aide-root) to the research repo checkout on CRC")
        self.root = Path(root)

    def preflight(self) -> None:
        for rel in ("sge/submit.sh", "scripts/run_aide.sh", "scripts/_common.sh"):
            if not (self.root / rel).is_file():
                raise RunnerError(f"{self.root} is not a MLE-bench_AIDE checkout (missing {rel})")

    def stage_notes(self, plan: RunPlan) -> Path | None:
        if not plan.variant:
            return None
        path = self.root / "config" / "tasks" / f"{plan.competition_id}.notes.{plan.variant}.txt"
        if path.exists():
            if path.read_text() != plan.notes_text:
                raise RunnerError(f"{path} exists with different content; variant files are immutable")
        else:
            path.write_text(plan.notes_text)
        return path

    def stage_seed(self, plan: RunPlan) -> dict[str, str]:
        """Write the pinned first draft to <root>/config/seeds/rsi_<hash>.{code.py,plan.txt} (content-addressed,
        immutable) and return the AIDE_SEED_* variables that point at it."""
        if not plan.seed_code_text:
            return {}
        seeds = self.root / "config" / "seeds"
        seeds.mkdir(parents=True, exist_ok=True)
        stem = f"rsi_{plan.seed_sha256[:10]}"
        env = {}
        for suffix, text, var in ((".code.py", plan.seed_code_text, "AIDE_SEED_CODE"),
                                  (".plan.txt", plan.seed_plan_text, "AIDE_SEED_PLAN")):
            if not text:
                continue
            path = seeds / (stem + suffix)
            if path.exists() and path.read_text() != text:
                raise RunnerError(f"{path} exists with different content; seed files are immutable")
            path.write_text(text)
            env[var] = str(path)
        return env

    def submit(self, plan: RunPlan) -> dict:
        self.preflight()
        notes_path = self.stage_notes(plan)
        seed_env = self.stage_seed(plan)
        proc = subprocess.run(plan.command + [plan.competition_id], cwd=self.root,
                              env={**os.environ, **plan.env, **seed_env}, capture_output=True, text=True)
        out = proc.stdout + proc.stderr
        if proc.returncode != 0:
            raise RunnerError(f"submit.sh failed ({proc.returncode}): {out[-400:]}")
        m = re.search(r"[Yy]our job (\d+)", out)
        if not m:
            raise RunnerError(f"could not find a job id in submit.sh output: {out[-400:]}")
        described = plan.describe()
        described["env"] = {**described["env"], **seed_env}
        return {"dry_run": False, "job_id": m.group(1), "notes_path": str(notes_path) if notes_path else None,
                **described}

    def find_run_dir(self, competition_id: str, job_id: str) -> Path | None:
        matches = sorted((self.root / "runs" / competition_id).glob(f"*_j{job_id}"))
        return matches[-1] if matches else None

    def job_state(self, competition_id: str, job_id: str) -> str:
        run_dir = self.find_run_dir(competition_id, job_id)
        if run_dir is not None and (run_dir / "grade_report.txt").is_file():
            return "graded"
        try:
            queued = subprocess.run(["qstat", "-j", job_id], capture_output=True, text=True).returncode == 0
        except FileNotFoundError:
            return "unknown (qstat unavailable)"
        return "running" if queued else ("finished-ungraded" if run_dir else "unknown")
