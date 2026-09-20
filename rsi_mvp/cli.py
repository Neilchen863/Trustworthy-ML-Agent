"""`rsi` command line.

    rsi init   --task random_acts_of_pizza --mode all
    rsi submit --task random_acts_of_pizza --mode rule --round 0 [--dry-run]
    rsi status
    rsi collect --task ... --mode ... --round 0 --job-id 1234567
    rsi improve --task ... --mode ... --round 0 --llm openai
    rsi freeze  --task ... --mode ...
    rsi submit  --task insults_heldout --mode rule --harness-task random_acts_of_pizza --harness H0 --round -1
    rsi heldout-collect ... ; rsi heldout-report ...
    rsi replay-demo --task ... --mode ... --runs RUN_A RUN_B RUN_C     (offline, mock LLM)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .guards import ContaminationError
from .harness import PatchError
from .llm import LLMError, MockMetaLLM, OpenAIChat
from .loop import LoopError, RsiProject
from .runner import DryRunBackend, RunnerError, SgeBackend
from .task import TaskError

REPO = Path(__file__).resolve().parents[1]


def _project(args) -> RsiProject:
    return RsiProject(args.tasks_dir, args.state_root)


def _llm(args):
    if args.llm == "mock":
        return MockMetaLLM()
    return OpenAIChat(model=args.meta_model)


def _run_dir(project: RsiProject, args, task_name: str) -> Path:
    if args.run_dir:
        return Path(args.run_dir)
    if not args.job_id:
        raise LoopError("give --run-dir or --job-id")
    backend = SgeBackend(args.aide_root)
    found = backend.find_run_dir(project.task(task_name).competition_id, args.job_id)
    if found is None:
        raise LoopError(f"no run directory for job {args.job_id} yet "
                        f"(state: {backend.job_state(project.task(task_name).competition_id, args.job_id)})")
    return found


def cmd_init(args) -> None:
    p = _project(args)
    modes = p.task(args.task).modes if args.mode == "all" else (args.mode,)
    for mode in modes:
        h0 = p.init_harness(args.task, mode, args.memory_render)
        print(f"created {args.task}/{mode}/{h0.version} (memory render: {h0.memory_render})")


def cmd_submit(args) -> None:
    p = _project(args)
    plan = p.plan_run(args.task, args.mode, args.round, args.replicate, args.harness_task, args.harness,
                      Path(args.aide_root) if args.aide_root else None)
    backend = DryRunBackend() if args.dry_run else SgeBackend(args.aide_root)
    print(json.dumps(p.submit(plan, backend), indent=2))


def cmd_status(args) -> None:
    p = _project(args)
    for row in p.index():
        try:
            state = SgeBackend(args.aide_root).job_state(row["competition_id"], row["job_id"])
        except RunnerError as exc:
            state = f"unknown ({exc})"
        print(f"{row['job_id']:>10}  {row['task']}/{row['mode']}  {row['harness']}  round={row['round']}  {state}")
    for name in p.tasks:
        for mode in p.task(name).modes:
            s = p.store(name, mode)
            if s.versions():
                froz = p.frozen(name, mode)
                print(f"harness {name}/{mode}: {s.versions()}" + (f"  FROZEN {froz['version']}" if froz else ""))


def cmd_collect(args) -> None:
    p = _project(args)
    rec = p.collect_round(args.task, args.mode, args.round, _run_dir(p, args, args.task), args.harness)
    print(json.dumps({"run_id": rec["run_id"], "task_performance": rec["task_performance"],
                      "reward_vector": rec["reward_vector"], "delivery": rec["delivery"]}, indent=2))


def cmd_improve(args) -> None:
    res = _project(args).improve(args.task, args.mode, args.round, _llm(args))
    print(json.dumps({k: res[k] for k in ("status", "reason", "patch", "new_version", "provider", "model")}, indent=2))


def cmd_freeze(args) -> None:
    print(json.dumps(_project(args).freeze(args.task, args.mode, args.harness), indent=2))


def cmd_heldout_collect(args) -> None:
    p = _project(args)
    rec = p.heldout_collect(args.task, args.mode, args.harness_task, args.harness,
                            _run_dir(p, args, args.task), args.replicate)
    print(json.dumps({k: rec[k] for k in ("run_id", "harness_version", "score", "delivery")}, indent=2))


def cmd_heldout_report(args) -> None:
    print(json.dumps(_project(args).heldout_report(args.harness_task, args.mode), indent=2))


def cmd_drive(args) -> None:
    """Unattended training rounds (no freeze, no held-out).  Resumable; halts on anything unexpected."""
    from .driver import Driver
    p = _project(args)
    driver = Driver(p, SgeBackend(args.aide_root), _llm(args), args.task, args.modes, args.last_round,
                    Path(args.aide_root) if args.aide_root else None, args.poll_secs,
                    log=lambda m: print(m, flush=True), replicates=args.replicates)
    outcome = driver.run()
    print(f"driver finished: {outcome}", flush=True)
    sys.exit(0 if outcome == "done" else 3)


def cmd_summarize(args) -> None:
    p = _project(args)
    for mode in (p.task(args.task).modes if args.mode == "all" else (args.mode,)):
        print(f"== {args.task}/{mode}")
        for row in p.summarize(args.task, mode):
            f = lambda v: "n/a" if v is None else f"{v:.4f}"
            print(f"  round {row['round']}  {row['harness_version']}  runs={row['n_runs']} reps={row['replicates']}  "
                  f"score mean={f(row['score_mean'])} sd={f(row['score_sd'])} "
                  f"[{f(row['score_min'])}..{f(row['score_max'])}]  mirage rate (lower bound) mean="
                  f"{f(row['mirage_rate_mean'])}  delivery_ok={row['delivery_ok']}")


def cmd_replay_demo(args) -> None:
    """Offline H0 -> H1 -> H2 over archived runs with the mock meta-improver.

    NOT an experiment: the archived runs were not produced by H1/H2, so nothing
    here says whether a patch helps.  It proves the plumbing end to end."""
    work = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="rsi_replay_"))
    p = RsiProject(args.tasks_dir, work)
    p.init_harness(args.task, args.mode)
    llm = MockMetaLLM()
    print(f"# replay workdir: {work}")
    for round_, run_dir in enumerate(args.runs):
        version = p.store(args.task, args.mode).latest()
        rec = p.collect_round(args.task, args.mode, round_, run_dir, version, replay=True)
        print(f"round {round_}  harness {version}  run {rec['run_id']}  "
              f"score={rec['task_performance'] and rec['task_performance']['score']}")
        print(f"   reward_vector: {rec['reward_vector']}")
        if round_ < len(args.runs) - 1:
            res = p.improve(args.task, args.mode, round_, llm)
            print(f"   meta-improver[{res['provider']}] -> {res['status']} {res['new_version'] or ''} "
                  f"{json.dumps(res['patch']['proposed_patch'])[:150] if res['patch'] else res['reason']}")
    print("harness versions:", p.store(args.task, args.mode).versions())


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rsi", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks-dir", default=str(REPO / "tasks"))
    ap.add_argument("--state-root", default=str(REPO), help="where harness_versions/, rounds/, heldout/ live")
    ap.add_argument("--aide-root", default=os.environ.get("MLEBENCH_AIDE_ROOT"),
                    help="research repo checkout on CRC (default: $MLEBENCH_AIDE_ROOT)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        sp = sub.add_parser(name, **kw)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("init", cmd_init, help="create H0 (stock harness)")
    sp.add_argument("--task", required=True); sp.add_argument("--mode", default="all", choices=["all", "rule", "agent"])
    sp.add_argument("--memory-render", default="none", choices=["none", "lessons"],
                    help="none (default): memory feeds only the meta-improver; lessons: also injected into the "
                         "prompt notes (the first pilot's behaviour)")

    sp = add("submit", cmd_submit, help="submit one run to CRC (or --dry-run)")
    sp.add_argument("--task", required=True); sp.add_argument("--mode", required=True, choices=["rule", "agent"])
    sp.add_argument("--round", type=int, required=True); sp.add_argument("--replicate", type=int, default=1)
    sp.add_argument("--harness-task", default=None, help="task whose harness to use (held-out runs)")
    sp.add_argument("--harness", default=None, help="harness version (default: latest)")
    sp.add_argument("--dry-run", action="store_true")

    add("status", cmd_status, help="submitted jobs + harness versions")

    for name, fn in (("collect", cmd_collect), ("heldout-collect", cmd_heldout_collect)):
        sp = add(name, fn, help="collect a finished run")
        sp.add_argument("--task", required=True); sp.add_argument("--mode", required=True, choices=["rule", "agent"])
        sp.add_argument("--run-dir"); sp.add_argument("--job-id"); sp.add_argument("--harness", default=None)
        if name == "collect":
            sp.add_argument("--round", type=int, required=True)
        else:
            sp.add_argument("--harness-task", required=True); sp.add_argument("--replicate", type=int, default=1)
            sp.set_defaults(harness=None)

    sp = add("improve", cmd_improve, help="meta-improver: propose H_{t+1}")
    sp.add_argument("--task", required=True); sp.add_argument("--mode", required=True, choices=["rule", "agent"])
    sp.add_argument("--round", type=int, required=True)
    sp.add_argument("--llm", choices=["mock", "openai"], default="openai")
    sp.add_argument("--meta-model", default="gpt-4o-2024-08-06")

    sp = add("freeze", cmd_freeze, help="freeze H_final (+ memory snapshot)")
    sp.add_argument("--task", required=True); sp.add_argument("--mode", required=True, choices=["rule", "agent"])
    sp.add_argument("--harness", default=None)

    sp = add("heldout-report", cmd_heldout_report, help="H0 vs H_final on held-out tasks")
    sp.add_argument("--harness-task", required=True); sp.add_argument("--mode", required=True, choices=["rule", "agent"])

    sp = add("drive", cmd_drive, help="unattended submit/collect/improve rounds (no freeze, no held-out)")
    sp.add_argument("--task", required=True)
    sp.add_argument("--modes", nargs="+", default=["rule", "agent"], choices=["rule", "agent"])
    sp.add_argument("--last-round", type=int, default=2, help="final round index; improves happen before it")
    sp.add_argument("--poll-secs", type=int, default=300)
    sp.add_argument("--replicates", type=int, nargs="+", default=None,
                    help="replicate ids per (mode, round); default = task.yaml `replicates`")
    sp.add_argument("--llm", choices=["mock", "openai"], default="openai")
    sp.add_argument("--meta-model", default="gpt-4o-2024-08-06")

    sp = add("summarize", cmd_summarize, help="per-round score mean/sd and mirage rate over replicates (train tasks)")
    sp.add_argument("--task", required=True); sp.add_argument("--mode", default="all", choices=["all", "rule", "agent"])

    sp = add("replay-demo", cmd_replay_demo, help="offline H0->H1->H2 over archived runs (mock LLM)")
    sp.add_argument("--task", required=True); sp.add_argument("--mode", required=True, choices=["rule", "agent"])
    sp.add_argument("--runs", nargs="+", required=True); sp.add_argument("--workdir")
    return ap


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (LoopError, RunnerError, TaskError, PatchError, LLMError, ContaminationError) as exc:
        print(f"rsi: error: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
