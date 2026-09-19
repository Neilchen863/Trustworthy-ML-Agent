import json

import pytest

from conftest import AGENT_LOG, make_run
from rsi_mvp.guards import ContaminationError
from rsi_mvp.llm import MockMetaLLM
from rsi_mvp.loop import LoopError, RsiProject
from rsi_mvp.runner import DryRunBackend

PHANTOM = [("a", 1, None, 0.60, "base"), ("b", 2, None, 1.0, "perfect score"), ("c", 3, "a", 0.65, "tuned")]
ROAP, HELD = "random_acts_of_pizza", "insults_heldout"


@pytest.fixture
def project(tasks_dir, state):
    return RsiProject(tasks_dir, state)


def test_h0_h1_h2_chain_over_three_rounds(project, runs):
    project.init_harness(ROAP, "agent")
    llm, versions = MockMetaLLM(), []
    for r, kw in enumerate([dict(nodes=PHANTOM, final="b", grade=0.5),
                            dict(nodes=PHANTOM, final="b", grade=0.55, constant_submission=True)]):
        v = project.store(ROAP, "agent").latest()
        notes = project.render_notes(ROAP, "agent", v)
        from rsi_mvp.runner import notes_variant
        run = make_run(runs, name=f"r{r}", mode="agent", agent_log=AGENT_LOG, variant=notes_variant(notes),
                       notes=notes, **kw)
        rec = project.collect_round(ROAP, "agent", r, run)
        assert rec["harness_version"] == v and rec["delivery"]["ok"], rec["delivery"]
        res = project.improve(ROAP, "agent", r, llm)
        assert res["status"] == "proposed" and res["new_version"] == f"H{r + 1}"
        versions.append(res["new_version"])
    assert project.store(ROAP, "agent").versions() == ["H0", "H1", "H2"] and versions == ["H1", "H2"]
    assert (project.store(ROAP, "agent").version_dir("H2") / "diff.patch").read_text().startswith("---")
    # the round record keeps everything the meta-improver saw and said
    saved = json.loads((project._round_dir(0, ROAP, "agent") / "meta_improver.json").read_text())
    assert saved["status"] == "proposed" and saved["input"]["runs"][0]["task_performance"]["score"] == 0.5


def test_memory_persists_and_is_rendered_into_the_next_rounds_notes(project, runs):
    project.init_harness(ROAP, "rule")
    project.collect_round(ROAP, "rule", 0, make_run(runs, nodes=PHANTOM, final="b", grade=0.5))
    assert project.render_notes(ROAP, "rule", "H0") == ""            # H0 stays the memory-free control
    res = project.improve(ROAP, "rule", 0, MockMetaLLM())
    assert res["new_version"] == "H1"
    notes = project.render_notes(ROAP, "rule", "H1")
    assert "Lessons from earlier runs" in notes and "validation" in notes.lower()


def test_undelivered_harness_blocks_improve(project, runs):
    project.init_harness(ROAP, "agent")
    project.collect_round(ROAP, "agent", 0, make_run(runs, name="r0", nodes=PHANTOM, final="b"))
    h1 = project.improve(ROAP, "agent", 0, MockMetaLLM())["new_version"]
    assert h1 == "H1"
    # round 1 run claims prompt_variant none even though H1 has notes -> delivery not confirmed
    project.collect_round(ROAP, "agent", 1, make_run(runs, name="r1", nodes=PHANTOM, final="b"))
    with pytest.raises(LoopError, match="delivery could not be confirmed"):
        project.improve(ROAP, "agent", 1, MockMetaLLM())


def test_chain_must_stay_linear(project, runs):
    project.init_harness(ROAP, "agent")
    project.collect_round(ROAP, "agent", 0, make_run(runs, name="r0", nodes=PHANTOM, final="b"))
    project.improve(ROAP, "agent", 0, MockMetaLLM())          # latest is now H1
    with pytest.raises(LoopError, match="linear"):
        project.improve(ROAP, "agent", 0, MockMetaLLM())      # round-0 runs used H0


def test_recollecting_a_run_is_refused_to_avoid_duplicate_memory(project, runs):
    project.init_harness(ROAP, "rule")
    run = make_run(runs, nodes=PHANTOM, final="b")
    project.collect_round(ROAP, "rule", 0, run)
    with pytest.raises(LoopError, match="already collected"):
        project.collect_round(ROAP, "rule", 0, run)


def test_no_change_creates_no_version(project, runs):
    project.init_harness(ROAP, "rule")
    project.collect_round(ROAP, "rule", 0, make_run(runs))           # clean run: no negative reward
    res = project.improve(ROAP, "rule", 0, MockMetaLLM())
    assert res["status"] == "no_change" and res["new_version"] is None
    assert project.store(ROAP, "rule").versions() == ["H0"]


# ---------------------------------------------------------------- train/test discipline
def test_heldout_task_cannot_be_collected_into_a_training_round(project, runs):
    with pytest.raises(ContaminationError, match="role is 'test'"):
        project.collect_round(HELD, "rule", 0, make_run(runs, competition="detecting-insults-in-social-commentary"))


def test_heldout_task_cannot_get_a_harness_or_meta_improver(project):
    with pytest.raises(ContaminationError):
        project.init_harness(HELD, "rule")
    with pytest.raises(ContaminationError):
        project.improve(HELD, "rule", 0, MockMetaLLM())
    with pytest.raises(ContaminationError):
        project.freeze(HELD, "rule")


def test_heldout_runs_are_blocked_until_freeze_and_limited_to_h0_or_final(project, runs):
    project.init_harness(ROAP, "rule")
    with pytest.raises(LoopError, match="after `rsi freeze`"):
        project.plan_run(HELD, "rule", -1, harness_task=ROAP, version="H0")
    project.collect_round(ROAP, "rule", 0, make_run(runs, nodes=PHANTOM, final="b"))
    project.improve(ROAP, "rule", 0, MockMetaLLM())
    project.freeze(ROAP, "rule", "H1")
    assert project.plan_run(HELD, "rule", -1, harness_task=ROAP, version="H0")
    assert project.plan_run(HELD, "rule", -1, harness_task=ROAP, version="H1")
    with pytest.raises(LoopError, match="only use H0 or the frozen H1"):
        project.plan_run(HELD, "rule", -1, harness_task=ROAP, version="H2")
    with pytest.raises(LoopError, match="already frozen"):
        project.freeze(ROAP, "rule")


def test_training_ends_at_freeze(project, runs):
    project.init_harness(ROAP, "rule")
    project.collect_round(ROAP, "rule", 0, make_run(runs, nodes=PHANTOM, final="b"))
    project.improve(ROAP, "rule", 0, MockMetaLLM())
    project.freeze(ROAP, "rule", "H1")
    with pytest.raises(LoopError, match="is frozen at H1"):
        project.improve(ROAP, "rule", 0, MockMetaLLM())
    with pytest.raises(LoopError, match="is frozen at H1"):
        project.collect_round(ROAP, "rule", 1, make_run(runs, name="late", nodes=PHANTOM, final="b"), replay=True)
    assert project.store(ROAP, "rule").versions() == ["H0", "H1"]


def test_frozen_harness_uses_its_memory_snapshot_not_live_memory(project, runs):
    project.init_harness(ROAP, "rule")
    project.collect_round(ROAP, "rule", 0, make_run(runs, nodes=PHANTOM, final="b", constant_submission=True))
    project.improve(ROAP, "rule", 0, MockMetaLLM())
    frozen = project.freeze(ROAP, "rule", "H1")
    before = project.render_notes(ROAP, "rule", "H1")
    project.memory(ROAP, "rule").append([{"situation": "s", "decision_outcome": "o", "verifier": "v",
                                          "reward": -1.0, "evidence": "e", "lesson": "A brand new lesson"}])
    assert project.render_notes(ROAP, "rule", "H1") == before and "brand new" not in before
    assert frozen["memory_sha256"]


def full_heldout_setup(project, runs):
    project.init_harness(ROAP, "rule")
    project.collect_round(ROAP, "rule", 0, make_run(runs, nodes=PHANTOM, final="b", grade=0.5))
    project.improve(ROAP, "rule", 0, MockMetaLLM())
    project.freeze(ROAP, "rule", "H1")


def test_heldout_report_h0_vs_final_matched_and_ledger_clean(project, runs):
    full_heldout_setup(project, runs)
    comp = "detecting-insults-in-social-commentary"
    for version, grade in (("H0", 0.70), ("H1", 0.75)):
        notes = project.render_notes(ROAP, "rule", version)
        from rsi_mvp.runner import notes_variant
        run = make_run(runs, name=f"held_{version}", mode="rule", competition=comp, grade=grade,
                       variant=notes_variant(notes), notes=notes or None)
        rec = project.heldout_collect(HELD, "rule", ROAP, version, run, replicate=1)
        assert rec["score"] == grade and rec["delivery"]["ok"]
    rep = project.heldout_report(ROAP, "rule")["tasks"][HELD]
    assert rep["matched_replicates"] == [1] and rep["improvement_per_replicate"] == pytest.approx([0.05])
    ledger = [r["competition_id"] for r in project.ledger.rows()]
    assert ledger and comp not in ledger and set(ledger) == {"random-acts-of-pizza"}


def test_heldout_report_fails_if_a_heldout_grade_ever_reached_the_meta_improver(project, runs):
    full_heldout_setup(project, runs)
    project.ledger.record(HELD, "detecting-insults-in-social-commentary", "leaked_run", 0, "H0")
    with pytest.raises(ContaminationError, match="not valid held-out"):
        project.heldout_report(ROAP, "rule")


def test_heldout_report_requires_freeze(project):
    project.init_harness(ROAP, "rule")
    with pytest.raises(LoopError, match="freeze"):
        project.heldout_report(ROAP, "rule")


def test_submit_writes_run_index_only_for_real_submissions(project):
    project.init_harness(ROAP, "rule")
    plan = project.plan_run(ROAP, "rule", 0)
    project.submit(plan, DryRunBackend())
    assert project.index() == []

    class Fake:
        def submit(self, p):
            return {"dry_run": False, "job_id": "77", **p.describe()}
    project.submit(plan, Fake())
    row = project.index()[0]
    assert row["job_id"] == "77" and row["harness"] == f"{ROAP}/rule/H0"
    assert not any("KEY" in k for k in row["env"])


def test_replay_is_deterministic(tasks_dir, tmp_path, runs):
    """Same archived runs + mock meta-improver => byte-identical harness versions."""
    outs = []
    for i in range(2):
        proj = RsiProject(tasks_dir, tmp_path / f"s{i}")
        proj.init_harness(ROAP, "rule")
        for r in range(2):
            run = make_run(tmp_path / f"runs{i}", name=f"r{r}", nodes=PHANTOM, final="b", grade=0.5, constant_submission=bool(r))
            proj.collect_round(ROAP, "rule", r, run, replay=True)
            proj.improve(ROAP, "rule", r, MockMetaLLM())
        outs.append([proj.store(ROAP, "rule").load(v).sha256() for v in ("H0", "H1", "H2")])
    assert outs[0] == outs[1] and len(set(outs[0])) == 3
