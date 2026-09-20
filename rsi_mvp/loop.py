"""Main RSI loop (spec section 11) as explicit, resumable steps.

    per round t, per training task:
        submit   run the existing AIDE mode with H_t          (runner.py, on CRC)
        collect  trajectory + verifier rewards + performance   -> memory update
        improve  meta-improver proposes a bounded patch        -> H_{t+1} (versioned)
    freeze       fix H_final and its memory snapshot
    heldout      run/compare H0 vs H_final on tasks whose official grade the
                 meta-improver never saw

Every step reads and writes files under `state_root`, so any step can be
re-run or inspected on its own and a crashed CRC session loses nothing.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

from . import meta_improver
from .collect import collect
from .guards import ExposureLedger, assert_train, verify_heldout_clean
from .harness import DEFAULT_MEMORY_POLICY, Harness, HarnessStore, PatchError
from .memory import MemoryStore
from .runner import DryRunBackend, RunPlan, SgeBackend, make_plan
from .schemas import make_event
from .task import TaskPackage, load_tasks


class LoopError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n")


class RsiProject:
    def __init__(self, tasks_dir: str | Path, state_root: str | Path):
        self.tasks: dict[str, TaskPackage] = load_tasks(tasks_dir)
        self.state = Path(state_root)
        self.harness_root = self.state / "harness_versions"
        self.rounds_root = self.state / "rounds"
        self.heldout_root = self.state / "heldout"
        self.ledger = ExposureLedger(self.rounds_root / "exposure_ledger.jsonl")
        self.index_path = self.rounds_root / "runs_index.jsonl"

    # ------------------------------------------------------------------ lookup
    def task(self, name: str) -> TaskPackage:
        if name not in self.tasks:
            raise LoopError(f"unknown task {name!r}; have {sorted(self.tasks)}")
        return self.tasks[name]

    def store(self, task: str, mode: str) -> HarnessStore:
        return HarnessStore(self.harness_root, task, mode)

    def memory(self, task: str, mode: str) -> MemoryStore:
        return MemoryStore(self.store(task, mode).memory_path)

    def _round_dir(self, round_: int, task: str, mode: str) -> Path:
        return self.rounds_root / f"round_{round_:02d}" / task / mode

    # -------------------------------------------------------------------- init
    def init_harness(self, task_name: str, mode: str, memory_render: str = "none") -> Harness:
        """Create H0 = the stock AIDE harness for this task/mode (empty notes).

        `memory_render="none"` (default) keeps memory out of the agent's prompt: it only feeds the
        meta-improver.  "lessons" reproduces the first pilot, where lessons were also injected."""
        task = self.task(task_name)
        assert_train(task.role, "create a trainable harness")
        settings = task.mode_settings(mode)
        h0 = Harness(task=task_name, mode=mode, version="H0", parent=None,
                     rule_config=settings.get("initial_rule_config") if mode == "rule" else None,
                     memory_policy={**DEFAULT_MEMORY_POLICY, "render": memory_render})
        self.store(task_name, mode).create_initial(h0)
        return h0

    # ------------------------------------------------------------------ render
    def render_notes(self, harness_task: str, mode: str, version: str) -> str:
        """Notes text for a run.  Training runs use live memory; a frozen harness
        uses the memory snapshot taken at freeze time; H0 is the memory-free control."""
        store = self.store(harness_task, mode)
        harness = store.load(version)
        frozen = self.frozen(harness_task, mode)
        if version == "H0" or harness.memory_render == "none":
            lessons = ""
        elif frozen and frozen["version"] == version:
            lessons = frozen["memory_lessons"]
        else:
            mp = harness.memory_policy
            lessons = self.memory(harness_task, mode).render_lessons(mp["max_records"], mp["max_chars"])
        return harness.render_notes(lessons)

    # ------------------------------------------------------------------ submit
    def plan_run(self, task_name: str, mode: str, round_: int, replicate: int = 1,
                 harness_task: str | None = None, version: str | None = None,
                 aide_root: Path | None = None) -> RunPlan:
        task = self.task(task_name)
        harness_task = harness_task or task_name
        store = self.store(harness_task, mode)
        version = version or store.latest()
        if version is None:
            raise LoopError(f"no harness for {harness_task}/{mode}; run `rsi init` first")
        if task.role == "test":
            frozen = self.frozen(harness_task, mode)
            if frozen is None:
                raise LoopError("held-out task runs are only allowed after `rsi freeze` "
                                f"(no frozen harness for {harness_task}/{mode})")
            if version not in ("H0", frozen["version"]):
                raise LoopError(f"held-out runs may only use H0 or the frozen {frozen['version']}, not {version}")
            assert_train(self.task(harness_task).role, "borrow a harness trained on a held-out task")
        harness = store.load(version)
        notes = self.render_notes(harness_task, mode, version)
        return make_plan(task, mode, harness, notes, round_, replicate, aide_root)

    def submit(self, plan: RunPlan, backend: DryRunBackend | SgeBackend) -> dict:
        result = backend.submit(plan)
        if not result.get("dry_run"):
            self.rounds_root.mkdir(parents=True, exist_ok=True)
            with self.index_path.open("a") as f:
                # record the environment the backend ACTUALLY used (it adds the staged seed and the dedicated
                # overlay); plan.env alone would leave those two out of the provenance record
                f.write(json.dumps({"time": _now(), **{k: v for k, v in result.items() if k != "env"},
                                    "env": dict(result.get("env", plan.env))}) + "\n")
        return result

    def index(self) -> list[dict]:
        if not self.index_path.is_file():
            return []
        return [json.loads(l) for l in self.index_path.read_text().splitlines() if l.strip()]

    # ----------------------------------------------------------------- collect
    def collect_round(self, task_name: str, mode: str, round_: int, run_dir: str | Path,
                      version: str | None = None, force: bool = False, replay: bool = False,
                      replicate: int = 1) -> dict:
        """`replay=True` is for offline demos over ARCHIVED runs that were not produced by
        `version`: delivery cannot be confirmed, so it is recorded as a replay explicitly."""
        task = self.task(task_name)
        assert_train(task.role, "collect a run into an RSI round")
        self._assert_not_frozen(task_name, mode, "collect into a training round")
        store = self.store(task_name, mode)
        version = version or store.latest()
        run_dir = Path(run_dir)
        out_dir = self._round_dir(round_, task_name, mode) / run_dir.name
        if out_dir.exists() and not force:
            raise LoopError(f"{out_dir} already collected (use force to redo; memory would be duplicated)")
        notes = self.render_notes(task_name, mode, version)
        first_line = next((l for l in notes.splitlines() if l.strip()), None)
        from .runner import notes_variant
        seed = task.pinned_first_draft()
        traj, bank = collect(run_dir, task, mode, round_, version, reveal_grade=True,
                             expected_variant=notes_variant(notes), notes_first_line=first_line,
                             expected_seed_code=seed[0] if seed else None)

        mem = self.memory(task_name, mode)
        records = mem.records_from_run(traj, bank, round_)
        mem.append(records)
        final_step = traj.nodes[-1]["step"] if traj.nodes else 0
        events = list(traj.events) + [
            make_event(final_step, f"round {round_}: memory updated", "MEMORY_WRITE",
                       f"memory_write:{r['verifier']}", r["lesson"], [str(mem.path)], {"reward": r["reward"]},
                       "logged") for r in records]

        if replay:
            traj.delivery = {"ok": True, "replayed_archived_run": True, "problems": []}
        record = {
            "run_id": run_dir.name, "task": task_name, "role": task.role, "mode": mode, "round": round_,
            "replicate": replicate, "harness_version": version, "task_performance": traj.grade, "trajectory": traj.summary(),
            "reward_vector": bank["reward_vector"], "verifier_results": bank["results"],
            "unmapped_detectors": bank["unmapped_detectors"], "delivery": traj.delivery,
            "collected_at": _now(),
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))
        _write_json(out_dir / "run_record.json", record)
        return record

    # ----------------------------------------------------------------- improve
    def improve(self, task_name: str, mode: str, round_: int, llm) -> dict:
        task = self.task(task_name)
        assert_train(task.role, "run the meta-improver")
        self._assert_not_frozen(task_name, mode, "run the meta-improver")
        rdir = self._round_dir(round_, task_name, mode)
        records = [json.loads(p.read_text()) for p in sorted(rdir.glob("*/run_record.json"))]
        if not records:
            raise LoopError(f"no collected runs in {rdir}; run `rsi collect` first")
        store = self.store(task_name, mode)
        versions = {r["harness_version"] for r in records}
        if versions != {store.latest()}:
            raise LoopError(f"round {round_} runs used {sorted(versions)} but latest harness is "
                            f"{store.latest()}; the H0->H1->H2 chain must stay linear")
        bad = [r["run_id"] for r in records if not r["delivery"].get("ok")]
        if bad:
            raise LoopError(f"harness delivery could not be confirmed for {bad}; "
                            "an undelivered harness would make this round meaningless")
        harness = store.load(store.latest())
        memory = self.memory(task_name, mode).all()
        # the official grades are about to be shown to the meta-improver: log it first
        for r in records:
            self.ledger.record(task_name, task.competition_id, r["run_id"], round_, r["harness_version"])
        result = meta_improver.propose(llm, harness, records, memory)
        new_version = None
        if result["status"] == "proposed":
            try:
                new = store.commit_patch(harness, result["patch"])
                new_version = new.version
            except PatchError as exc:
                result["status"], result["reason"] = "rejected", str(exc)
        result["new_version"] = new_version
        _write_json(rdir / "meta_improver.json", result)
        return result

    # ------------------------------------------------------------------ freeze
    def frozen_path(self, task: str, mode: str) -> Path:
        return self.store(task, mode).dir / "FROZEN.json"

    def _assert_not_frozen(self, task: str, mode: str, what: str) -> None:
        frozen = self.frozen(task, mode)
        if frozen:
            raise LoopError(f"cannot {what}: {task}/{mode} is frozen at {frozen['version']} "
                            "(training ended; only held-out evaluation remains)")

    def frozen(self, task: str, mode: str) -> dict | None:
        path = self.frozen_path(task, mode)
        return json.loads(path.read_text()) if path.is_file() else None

    def freeze(self, task_name: str, mode: str, version: str | None = None) -> dict:
        task = self.task(task_name)
        assert_train(task.role, "freeze a harness")
        store = self.store(task_name, mode)
        if self.frozen(task_name, mode):
            raise LoopError(f"{task_name}/{mode} is already frozen; a frozen harness is final")
        version = version or store.latest()
        if version not in store.versions():
            raise LoopError(f"unknown harness version {version!r}")
        harness = store.load(version)
        mp = harness.memory_policy
        lessons = (self.memory(task_name, mode).render_lessons(mp["max_records"], mp["max_chars"])
                   if version != "H0" and harness.memory_render == "lessons" else "")
        info = {"task": task_name, "mode": mode, "version": version, "harness_sha256": harness.sha256(),
                "memory_lessons": lessons, "memory_sha256": hashlib.sha256(lessons.encode()).hexdigest(),
                "frozen_at": _now()}
        _write_json(self.frozen_path(task_name, mode), info)
        return info

    # ----------------------------------------------------------------- held-out
    def heldout_collect(self, test_task: str, mode: str, harness_task: str, version: str,
                        run_dir: str | Path, replicate: int = 1) -> dict:
        task = self.task(test_task)
        if task.role != "test":
            raise LoopError(f"{test_task} is not a held-out (role=test) task")
        frozen = self.frozen(harness_task, mode)
        if frozen is None or version not in ("H0", frozen["version"]):
            raise LoopError("held-out results can only be collected for H0 or the frozen harness")
        run_dir = Path(run_dir)
        notes = self.render_notes(harness_task, mode, version)
        from .runner import notes_variant
        traj, bank = collect(run_dir, task, mode, -1, version, reveal_grade=True,
                             expected_variant=notes_variant(notes),
                             notes_first_line=next((l for l in notes.splitlines() if l.strip()), None))
        rec = {"run_id": run_dir.name, "task": test_task, "mode": mode, "harness_task": harness_task,
               "harness_version": version, "replicate": replicate,
               "score": traj.grade["score"] if traj.grade else None, "task_performance": traj.grade,
               "reward_vector": bank["reward_vector"], "delivery": traj.delivery, "collected_at": _now()}
        _write_json(self.heldout_root / test_task / mode / version / f"{run_dir.name}.json", rec)
        return rec

    def heldout_report(self, harness_task: str, mode: str) -> dict:
        """H0 vs frozen H_final on every held-out task, matched by replicate."""
        frozen = self.frozen(harness_task, mode)
        if frozen is None:
            raise LoopError("freeze the harness before reporting held-out results")
        test_tasks = {n: t for n, t in self.tasks.items() if t.role == "test"}
        verify_heldout_clean(self.ledger, {t.competition_id for t in test_tasks.values()})
        report = {"harness": f"{harness_task}/{mode}", "final_version": frozen["version"],
                  "generated_at": _now(), "tasks": {}}
        for name, task in test_tasks.items():
            per_version = {}
            for version in ("H0", frozen["version"]):
                rows = [json.loads(p.read_text())
                        for p in sorted((self.heldout_root / name / mode / version).glob("*.json"))
                        if json.loads(p.read_text())["harness_task"] == harness_task]
                per_version[version] = {r["replicate"]: r["score"] for r in rows if r["score"] is not None}
            h0, hf = per_version["H0"], per_version[frozen["version"]]
            matched = sorted(set(h0) & set(hf))
            higher = task.metric["direction"] == "maximize"
            deltas = [(hf[k] - h0[k]) * (1 if higher else -1) for k in matched]
            report["tasks"][name] = {
                "competition_id": task.competition_id, "metric": task.metric,
                "H0": h0, frozen["version"]: hf, "matched_replicates": matched,
                "improvement_per_replicate": deltas,
                "mean_improvement": statistics.mean(deltas) if deltas else None,
                "n_matched": len(matched)}
        report["exposure_ledger_clean"] = True   # verify_heldout_clean raised otherwise
        _write_json(self.heldout_root / f"report_{harness_task}_{mode}.json", report)
        return report

    # ---------------------------------------------------------------- summary
    def summarize(self, task_name: str, mode: str) -> list[dict]:
        """Per (round, harness version): replicate count, official score mean/sd/min/max and the mean
        node-level mirage rate (lower bound), for a human reading the training runs.  Train tasks only:
        this prints official grades, which a held-out task must never do before its report."""
        task = self.task(task_name)
        assert_train(task.role, "summarize official grades of a task")
        rows = []
        for rdir in sorted(self.rounds_root.glob(f"round_*/{task_name}/{mode}")):
            recs = [json.loads(p.read_text()) for p in sorted(rdir.glob("*/run_record.json"))]
            if not recs:
                continue
            scores = [r["task_performance"]["score"] for r in recs
                      if r.get("task_performance") and r["task_performance"].get("score") is not None]
            rates = [v["rate"] for r in recs for v in r["verifier_results"]
                     if v["verifier"] == "validation_mirage" and v.get("rate") is not None]
            rows.append({
                "round": recs[0]["round"], "harness_version": recs[0]["harness_version"], "n_runs": len(recs),
                "replicates": sorted(r.get("replicate", 1) for r in recs),
                "score_mean": statistics.mean(scores) if scores else None,
                "score_sd": statistics.stdev(scores) if len(scores) > 1 else None,
                "score_min": min(scores) if scores else None, "score_max": max(scores) if scores else None,
                "mirage_rate_mean": statistics.mean(rates) if rates else None,
                "delivery_ok": all(r["delivery"].get("ok") for r in recs)})
        return rows
