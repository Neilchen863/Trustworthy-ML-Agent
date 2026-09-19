"""Train/test discipline for the RSI loop.

Rule (decided with the project owner): an official grade may reach the
meta-improver only for a task with role == "train".  A held-out ("test") task
must be one whose official grade has NEVER been shown to the meta-improver, and
its runs happen only after the harness is frozen.

The rule is enforced in three places rather than trusted:

  1. `assert_train` is called wherever a run's grade would be placed in
     meta-improver input;
  2. every run whose grade *is* shown is written to an append-only exposure
     ledger;
  3. `verify_heldout_clean` re-reads the ledger before a held-out report and
     fails if any test-role task (or its competition) appears in it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class ContaminationError(RuntimeError):
    pass


def assert_train(role: str, what: str) -> None:
    if role != "train":
        raise ContaminationError(
            f"refusing to {what}: task role is {role!r}; only role='train' tasks may expose "
            "an official grade to the meta-improver")


class ExposureLedger:
    """Append-only JSONL of every (task, run) whose official grade was shown to the meta-improver."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def record(self, task: str, competition_id: str, run_id: str, round_: int, harness_version: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "task": task,
               "competition_id": competition_id, "run_id": run_id, "round": round_,
               "harness_version": harness_version}
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")

    def rows(self) -> list[dict]:
        if not self.path.is_file():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def exposed_competitions(self) -> set[str]:
        return {r["competition_id"] for r in self.rows()}


def verify_heldout_clean(ledger: ExposureLedger, test_competitions: set[str]) -> None:
    leaked = ledger.exposed_competitions() & set(test_competitions)
    if leaked:
        raise ContaminationError(
            f"held-out competition(s) {sorted(leaked)} appear in the exposure ledger "
            f"({ledger.path}); they are not valid held-out tasks")
