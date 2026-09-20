import json
import shutil
import stat

import pytest
import yaml

from conftest import make_run
from rsi_mvp.driver import Driver, DriverHalt
from rsi_mvp.harness import Harness
from rsi_mvp.llm import MockMetaLLM
from rsi_mvp.loop import RsiProject
from rsi_mvp.runner import RunnerError, SgeBackend, make_plan, notes_variant
from rsi_mvp.task import TaskError, load_task
from rsi_mvp.trajectory import delivery_check

ROAP = "random_acts_of_pizza"
PHANTOM = [("a", 1, None, 0.60, "base"), ("b", 2, None, 1.0, "perfect"), ("c", 3, "a", 0.65, "tuned")]


@pytest.fixture
def pinned(pinned_tasks_dir):
    return load_task(pinned_tasks_dir / ROAP)


def h0(mode="rule"):
    return Harness(task=ROAP, mode=mode, version="H0", parent=None,
                   rule_config={"max_stagnation": 15, "debug_prob": 1.0, "max_debug_depth": 20, "num_drafts": 5}
                   if mode == "rule" else None)


# ------------------------------------------------------------------------------- the seed itself
def test_roap_pins_a_neutral_first_draft_and_three_replicates(pinned):
    code, plan = pinned.pinned_first_draft()
    assert pinned.replicates == [1, 2, 3] and plan
    # leak-free: nothing that exists only in the training file may be read
    for train_only in ("requester_received_pizza\"]  #", "_at_retrieval", "requester_user_flair", "post_was_edited",
                       "\"request_text\"", "giver_username"):
        assert train_only not in code, train_only
    assert "request_text_edit_aware" in code and "Pipeline" in code
    # prints the explicit metric sentinels the research repo's guard parses first, and avoids the
    # "N-fold CV" phrasing that its 5.0 bug trips on
    assert "[metric] name=roc_auc value=" in code and "[fold] index=" in code and "-fold" not in code


def test_task_validation_of_replicates_and_pinned_files(tasks_dir, tmp_path):
    for i, (patch, msg) in enumerate((({"replicates": []}, "replicates"), ({"replicates": [1, 1]}, "replicates"),
                                      ({"replicates": [0]}, "replicates"),
                                      ({"pinned_first_draft": {"code": "seeds/nope.py"}}, "not found"),
                                      ({"pinned_first_draft": {"plan": "x"}}, "needs a 'code'"))):
        d = tmp_path / f"t{i}"
        shutil.copytree(tasks_dir / ROAP, d)
        cfg = yaml.safe_load((d / "task.yaml").read_text())
        cfg.update(patch)
        (d / "task.yaml").write_text(yaml.safe_dump(cfg))
        with pytest.raises(TaskError, match=msg):
            load_task(d)


# ---------------------------------------------------------------------------------- seed staging
def fake_root(tmp_path):
    root = tmp_path / "aide"
    for d in ("sge", "scripts", "config/tasks"):
        (root / d).mkdir(parents=True)
    (root / "scripts/run_aide.sh").write_text("")
    (root / "scripts/_common.sh").write_text("")
    sub = root / "sge/submit.sh"
    sub.write_text('#!/bin/bash\nenv | grep -E "^AIDE_SEED_(CODE|PLAN)=" > "$PWD/seen.env"\n'
                   'echo "Your job 99 (\\"x\\") has been submitted"\n')
    sub.chmod(sub.stat().st_mode | stat.S_IEXEC)
    return root


def test_the_seed_is_staged_content_addressed_and_pointed_at_by_aide_seed_vars(pinned, tmp_path):
    root = fake_root(tmp_path)
    plan = make_plan(pinned, "rule", h0(), "", 0, 2)
    res = SgeBackend(root).submit(plan)
    code_path = root / "config/seeds" / f"rsi_{plan.seed_sha256[:10]}.code.py"
    assert code_path.read_text() == plan.seed_code_text
    seen = (root / "seen.env").read_text()
    assert f"AIDE_SEED_CODE={code_path}" in seen and "AIDE_SEED_PLAN=" in seen
    assert res["env"]["AIDE_SEED_CODE"] == str(code_path) and res["seed_sha256"] == plan.seed_sha256
    assert res["replicate"] == 2


def test_replicates_of_one_round_share_the_same_seed_and_notes(pinned):
    a, b = (make_plan(pinned, "agent", h0("agent"), "same notes", 1, k) for k in (1, 3))
    assert a.seed_sha256 == b.seed_sha256 and a.variant == b.variant and a.replicate != b.replicate


def test_staged_seed_files_are_immutable(pinned, tmp_path):
    root = fake_root(tmp_path)
    plan = make_plan(pinned, "rule", h0(), "", 0, 1)
    SgeBackend(root).submit(plan)
    (root / "config/seeds" / f"rsi_{plan.seed_sha256[:10]}.code.py").write_text("tampered")
    with pytest.raises(RunnerError, match="immutable"):
        SgeBackend(root).submit(plan)


def test_unpinned_task_stages_no_seed(tasks_dir, tmp_path):
    root = fake_root(tmp_path)
    plan = make_plan(load_task(tasks_dir / ROAP), "rule", h0(), "", 0, 1)
    SgeBackend(root).submit(plan)
    assert plan.seed_sha256 is None and not (root / "config/seeds").exists()
    assert (root / "seen.env").read_text() == ""


# ------------------------------------------------------------------------------ seed delivery
def test_seed_delivery_is_checked_against_the_first_node_and_the_run_config(runs):
    seed = "print('seed')\n"
    ok = make_run(runs, name="ok", seed_code=seed)
    assert delivery_check(ok, None, None, seed)["ok"]
    wrong = make_run(runs, name="wrong", seed_code="print('something else')\n")
    d = delivery_check(wrong, None, None, seed)
    assert not d["ok"] and "not the pinned first-draft seed" in " ".join(d["problems"])
    unstaged = make_run(runs, name="unstaged")                     # organic draft, seed_code = none
    d = delivery_check(unstaged, None, None, "print('seed')\n")
    assert not d["ok"] and any("seed_code" in p for p in d["problems"])


def test_collect_round_flags_a_run_that_never_received_the_pinned_seed(pinned_tasks_dir, state, runs):
    p = RsiProject(pinned_tasks_dir, state)
    p.init_harness(ROAP, "rule")
    seed = p.task(ROAP).pinned_first_draft()[0]
    good = p.collect_round(ROAP, "rule", 0, make_run(runs, name="g", mode="rule", seed_code=seed), replicate=1)
    bad = p.collect_round(ROAP, "rule", 0, make_run(runs, name="b", mode="rule"), replicate=2)
    assert good["delivery"]["ok"] and good["replicate"] == 1
    assert not bad["delivery"]["ok"] and bad["replicate"] == 2


# --------------------------------------------------------------------------- driver replicates
class FakeBackend:
    def __init__(self, project, runs, latency=1, fail=(), seed=None):
        self.p, self.runs, self.latency, self.fail, self.seed = project, runs, latency, set(fail), seed
        self.jobs, self.polls, self.n = {}, {}, 1000

    def submit(self, plan):
        self.n += 1
        self.jobs[str(self.n)] = plan
        return {"dry_run": False, "job_id": str(self.n), **plan.describe()}

    def find_run_dir(self, comp, jid):
        d = self.runs / f"run_{jid}"
        return d if d.exists() else None

    def job_state(self, comp, jid):
        self.polls[jid] = self.polls.get(jid, 0) + 1
        if self.polls[jid] <= self.latency:
            return "running"
        if jid in self.fail:
            return "finished-ungraded"
        plan = self.jobs[jid]
        if not (self.runs / f"run_{jid}").exists():
            make_run(self.runs, name=f"run_{jid}", mode=plan.mode, nodes=PHANTOM, final="b", grade=0.55 + plan.replicate / 100,
                     constant_submission=plan.round >= 1, variant=notes_variant(plan.notes_text),
                     notes=plan.notes_text or None, seed_code=self.seed)
        return "graded"


@pytest.fixture
def project(tasks_dir, state):
    p = RsiProject(tasks_dir, state)
    for m in ("rule", "agent"):
        p.init_harness(ROAP, m)
    return p


def drv(project, backend, last=2, reps=(1, 2, 3), modes=("rule", "agent"), **kw):
    return Driver(project, backend, MockMetaLLM(), ROAP, list(modes), last, poll_secs=0, sleep=lambda s: None,
                  log=lambda m: None, replicates=list(reps), **kw)


def test_each_round_submits_all_replicates_of_the_same_harness_then_improves_once(project, runs):
    be = FakeBackend(project, runs)
    assert drv(project, be).run() == "done"
    assert len(be.jobs) == 2 * 3 * 3                                   # modes x rounds x replicates
    by_cell = {}
    for pl in be.jobs.values():
        by_cell.setdefault((pl.mode, pl.round), []).append((pl.harness_version, pl.replicate))
    for (mode, r), cell in by_cell.items():
        assert sorted(cell) == [(f"H{r}", 1), (f"H{r}", 2), (f"H{r}", 3)]
    for m in ("rule", "agent"):
        assert project.store(ROAP, m).versions() == ["H0", "H1", "H2"]      # one patch per round, not per run
        saved = json.loads((project._round_dir(0, ROAP, m) / "meta_improver.json").read_text())
        assert len(saved["input"]["runs"]) == 3, "the meta-improver saw all three replicates"


def test_improve_waits_until_every_replicate_is_collected(project, runs):
    be = FakeBackend(project, runs, latency=2)
    d = drv(project, be, modes=("rule",))
    d.run(max_polls=1)                                                # first pass: 3 submitted, none graded
    assert len(be.jobs) == 3 and project.store(ROAP, "rule").versions() == ["H0"]
    be.polls["1001"] = 10                                             # only replicate 1 finishes
    d.advance("rule")
    assert len(d._collected_replicates("rule", 0)) == 1 and project.store(ROAP, "rule").versions() == ["H0"]
    assert d.run() == "done" and project.store(ROAP, "rule").versions() == ["H0", "H1", "H2"]


def test_restart_does_not_resubmit_replicates(project, runs):
    be = FakeBackend(project, runs, latency=3)
    drv(project, be, modes=("rule",)).run(max_polls=1)
    before = len(be.jobs)
    assert drv(project, be, modes=("rule",)).run() == "done"
    assert before == 3 and len(be.jobs) == 9


def test_one_failed_replicate_halts_instead_of_improving_on_partial_data(project, runs):
    be = FakeBackend(project, runs, fail={"1002"})                   # replicate 2 of rule round 0 dies
    assert drv(project, be, modes=("rule",), stale_polls=2).run() == "halted"
    halt = json.loads((project.rounds_root / "DRIVER_HALT.json").read_text())
    assert "rep2" in halt["reason"] and "without an official grade" in halt["reason"]
    assert project.store(ROAP, "rule").versions() == ["H0"]


def test_pinned_runs_pass_delivery_through_the_driver(pinned_tasks_dir, state, runs):
    p = RsiProject(pinned_tasks_dir, state)
    p.init_harness(ROAP, "rule")
    seed = p.task(ROAP).pinned_first_draft()[0]
    d = Driver(p, FakeBackend(p, runs, seed=seed), MockMetaLLM(), ROAP, ["rule"], 1, poll_secs=0,
               sleep=lambda s: None, log=lambda m: None)              # replicates default to task.yaml: 1,2,3
    assert d.replicates == [1, 2, 3] and d.run() == "done"
    p2 = RsiProject(pinned_tasks_dir, state.parent / "s2")            # fresh state AND a fresh runs dir: job ids
    p2.init_harness(ROAP, "rule")                                     # restart at 1001 and must not reuse seeded runs
    unseeded = Driver(p2, FakeBackend(p2, runs.parent / "runs_unseeded"), MockMetaLLM(), ROAP, ["rule"], 0,
                      poll_secs=0, sleep=lambda s: None, log=lambda m: None)
    assert unseeded.run() == "halted"                                # runs without the seed are refused
    reason = json.loads((p2.rounds_root / "DRIVER_HALT.json").read_text())["reason"]
    assert "delivery could not be confirmed" in reason and "pinned first-draft seed" in reason


# ------------------------------------------------------------------------------------ summarize
def test_summarize_reports_mean_sd_and_mirage_rate_over_replicates(project, runs):
    be = FakeBackend(project, runs)
    drv(project, be, last=1, modes=("rule",)).run()
    rows = project.summarize(ROAP, "rule")
    assert [r["harness_version"] for r in rows] == ["H0", "H1"] and rows[0]["n_runs"] == 3
    assert rows[0]["replicates"] == [1, 2, 3]
    assert rows[0]["score_mean"] == pytest.approx(0.57) and rows[0]["score_sd"] == pytest.approx(0.01)
    assert rows[0]["mirage_rate_mean"] == 0.0 and rows[0]["delivery_ok"] is True


def test_summarize_refuses_a_heldout_task(project):
    from rsi_mvp.guards import ContaminationError
    with pytest.raises(ContaminationError):
        project.summarize("insults_heldout", "rule")


def test_cli_drive_parses_replicates_and_summarize_prints(project, runs, tasks_dir, state, capsys):
    from rsi_mvp import cli
    args = cli.build_parser().parse_args(["--tasks-dir", str(tasks_dir), "--state-root", str(state), "drive",
                                          "--task", ROAP, "--replicates", "1", "2", "--llm", "mock"])
    assert args.replicates == [1, 2]
    drv(project, FakeBackend(project, runs), last=0, modes=("rule",), reps=(1, 2)).run()
    cli.main(["--tasks-dir", str(tasks_dir), "--state-root", str(state), "summarize", "--task", ROAP, "--mode", "rule"])
    assert "runs=2 reps=[1, 2]" in capsys.readouterr().out
