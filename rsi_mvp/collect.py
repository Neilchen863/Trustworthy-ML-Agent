"""Run directory -> Trajectory (+ verifier vector).  No LLM, no code execution."""
from __future__ import annotations

from pathlib import Path

from .events import extract_events
from .task import TaskPackage
from .trajectory import (Trajectory, delivery_check, read_final_node_id, read_grade,
                         read_journal, read_run_config)
from .verifiers import run_bank


def build_trajectory(
    run_dir: str | Path,
    task: TaskPackage,
    mode: str,
    round_: int,
    harness_version: str,
    reveal_grade: bool,
    expected_variant: str | None = None,
    notes_first_line: str | None = None,
    expected_seed_code: str | None = None,
) -> Trajectory:
    """`reveal_grade` is decided by the caller (loop.py enforces the train/test
    rule); when False the grade is neither read into the trajectory nor placed in
    any event outcome."""
    run_dir = Path(run_dir)
    grade = read_grade(run_dir) if reveal_grade else None
    events = extract_events(run_dir, mode, grade_outcome=grade)
    return Trajectory(
        run_dir=run_dir, task=task.name, mode=mode, round=round_, harness_version=harness_version,
        nodes=read_journal(run_dir), events=events, final_node_id=read_final_node_id(run_dir),
        grade=grade, grade_revealed=reveal_grade,
        delivery=delivery_check(run_dir, expected_variant, notes_first_line, expected_seed_code),
        run_config=read_run_config(run_dir),
    )


def collect(run_dir, task: TaskPackage, mode: str, round_: int, harness_version: str,
            reveal_grade: bool, **kw) -> tuple[Trajectory, dict]:
    traj = build_trajectory(run_dir, task, mode, round_, harness_version, reveal_grade, **kw)
    return traj, run_bank(traj, task)
