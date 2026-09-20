"""Unattended, resumable driver for the training rounds (spec section 11 loop).

    for round r in 0..last_round, per mode (modes progress independently):
        submit (harness H_r) -> wait for the graded run -> collect -> improve -> H_{r+1}

No held-out step and no freeze: those stay explicit manual commands.

The driver derives its position from files on disk (runs_index.jsonl, rounds/, harness
versions), so it can be killed and restarted at any point without double-submitting.  It
HALTS - writes rounds/DRIVER_HALT.json and exits non-zero - on anything it cannot explain,
because it spends real compute and money without a human watching:

  * a job left the queue without an official grade
  * the harness was not delivered to the run (PROMPT_VARIANT / notes mismatch)
  * the meta-improver returned anything other than a valid, applied patch
  * the harness version on disk is not the version this round expects
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .loop import LoopError, RsiProject


class DriverHalt(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Driver:
    def __init__(self, project: RsiProject, backend, llm, task: str, modes: list[str], last_round: int,
                 aide_root: Path | None = None, poll_secs: int = 300, stale_polls: int = 3,
                 sleep=time.sleep, log=print, replicates: list[int] | None = None):
        self.p, self.backend, self.llm = project, backend, llm
        self.replicates = list(replicates or project.task(task).replicates)
        self.task, self.modes, self.last = task, list(modes), last_round
        self.aide_root, self.poll, self.stale_polls = aide_root, poll_secs, stale_polls
        self.sleep, self.log = sleep, log
        self.competition = project.task(task).competition_id
        self._stale: dict[tuple, int] = {}

    # ------------------------------------------------------------------ state
    def _rdir(self, mode: str, r: int) -> Path:
        return self.p._round_dir(r, self.task, mode)

    def _collected_replicates(self, mode: str, r: int) -> set[int]:
        return {json.loads(p.read_text()).get("replicate", 1)
                for p in self._rdir(mode, r).glob("*/run_record.json")}

    def _collected(self, mode: str, r: int) -> bool:
        """Every replicate of this (mode, round) has been collected."""
        return set(self.replicates) <= self._collected_replicates(mode, r)

    def _improved(self, mode: str, r: int) -> bool:
        path = self._rdir(mode, r) / "meta_improver.json"
        return path.is_file() and json.loads(path.read_text()).get("new_version") is not None

    def current_round(self, mode: str) -> int | None:
        for r in range(self.last + 1):
            if not self._collected(mode, r):
                return r
            if r < self.last and not self._improved(mode, r):
                return r
        return None

    def _entry(self, mode: str, r: int, replicate: int) -> dict | None:
        rows = [x for x in self.p.index() if x["task"] == self.task and x["mode"] == mode
                and x["round"] == r and x.get("replicate", 1) == replicate]
        return rows[-1] if rows else None

    # --------------------------------------------------------------- one step
    def advance(self, mode: str) -> str:
        """Returns 'done' | 'waiting' | 'progress'.  Raises DriverHalt.

        One round of one mode = K replicate runs of the SAME harness version, submitted together; the
        meta-improver runs once, over all K of them."""
        r = self.current_round(mode)
        if r is None:
            return "done"
        store = self.p.store(self.task, mode)

        missing = [k for k in self.replicates if self._entry(mode, r, k) is None]
        if missing:                                                  # submit every replicate not yet submitted
            if store.latest() != f"H{r}":
                raise DriverHalt(f"{mode} round {r} expects harness H{r} but latest on disk is {store.latest()}")
            for k in missing:
                plan = self.p.plan_run(self.task, mode, r, k, None, None, self.aide_root)
                res = self.p.submit(plan, self.backend)
                self.log(f"[{_now()}] {mode} r{r} rep{k}: submitted job {res['job_id']} with H{r} "
                         f"variant={plan.variant} seed={plan.seed_sha256 and plan.seed_sha256[:10]}")
            return "progress"

        done_reps = self._collected_replicates(mode, r)
        progressed = waiting = False
        for k in self.replicates:
            if k in done_reps:
                continue
            entry = self._entry(mode, r, k)
            version = entry["harness"].rsplit("/", 1)[-1]
            state = self.backend.job_state(self.competition, entry["job_id"])
            if state == "graded":
                run_dir = self.backend.find_run_dir(self.competition, entry["job_id"])
                rec = self.p.collect_round(self.task, mode, r, run_dir, version, replicate=k)
                self.log(f"[{_now()}] {mode} r{r} rep{k}: collected {rec['run_id']} score="
                         f"{rec['task_performance'] and rec['task_performance']['score']} "
                         f"reward_vector={rec['reward_vector']} delivery_ok={rec['delivery']['ok']}")
                self._stale.pop((mode, k), None)
                if not rec["delivery"]["ok"]:
                    # Checked here, not only via improve(): the last round has no improve step, and a
                    # run that never received its harness must not be counted as an H_r result.
                    raise DriverHalt(f"{mode} r{r} rep{k}: harness delivery could not be confirmed for "
                                     f"{rec['run_id']}: {rec['delivery']['problems']}")
                progressed = True
            elif state == "running" or state.startswith("unknown (qstat"):
                self._stale.pop((mode, k), None)
                waiting = True
            else:                                                    # left the queue, no grade yet
                self._stale[(mode, k)] = self._stale.get((mode, k), 0) + 1
                if self._stale[(mode, k)] >= self.stale_polls:
                    raise DriverHalt(f"{mode} r{r} rep{k}: job {entry['job_id']} is {state!r}: it left the "
                                     "queue without an official grade (crashed or was killed)")
                waiting = True
        if progressed:
            return "progress"
        if waiting:
            return "waiting"

        if r < self.last and not self._improved(mode, r):            # all K replicates collected
            try:
                res = self.p.improve(self.task, mode, r, self.llm)
            except LoopError as exc:
                raise DriverHalt(f"{mode} r{r}: improve refused: {exc}") from exc
            if res["status"] != "proposed" or not res.get("new_version"):
                raise DriverHalt(f"{mode} r{r}: meta-improver status={res['status']!r} reason={res.get('reason')!r}; "
                                 "a human should look before spending another round")
            self.log(f"[{_now()}] {mode} r{r}: {res['provider']} proposed {res['new_version']}: "
                     f"{res['patch']['target_component']} {json.dumps(res['patch']['proposed_patch'])[:200]}")
            return "progress"
        return "waiting"

    # ------------------------------------------------------------------- run
    def _heartbeat(self, status: str, detail: str = "") -> None:
        path = self.p.rounds_root / "driver_heartbeat.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"time": _now(), "status": status, "detail": detail,
                                    "rounds": {m: self.current_round(m) for m in self.modes}}) + "\n")

    def run(self, max_polls: int | None = None) -> str:
        polls = 0
        stale_halt = self.p.rounds_root / "DRIVER_HALT.json"
        if stale_halt.exists():                       # a restart means a human has looked at the last halt
            stale_halt.rename(stale_halt.with_name(f"DRIVER_HALT.{_now().replace(':', '')}.json"))
        while True:
            try:
                results = {m: self.advance(m) for m in self.modes}
            except DriverHalt as exc:
                self.p.rounds_root.mkdir(parents=True, exist_ok=True)   # may halt before the first submit
                (self.p.rounds_root / "DRIVER_HALT.json").write_text(
                    json.dumps({"time": _now(), "reason": str(exc)}, indent=2) + "\n")
                self._heartbeat("halted", str(exc))
                self.log(f"[{_now()}] HALT: {exc}")
                return "halted"
            if all(v == "done" for v in results.values()):
                self._heartbeat("done")
                self.log(f"[{_now()}] all modes finished round {self.last}")
                return "done"
            self._heartbeat("running", json.dumps(results))
            if "progress" in results.values():
                continue                                             # act again immediately, no sleep
            polls += 1
            if max_polls is not None and polls >= max_polls:
                return "max_polls"
            self.sleep(self.poll)
