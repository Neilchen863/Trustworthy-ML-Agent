import json

import pytest

from conftest import make_run
from rsi_mvp.driver import Driver, DriverHalt
from rsi_mvp.llm import MockMetaLLM
from rsi_mvp.loop import RsiProject
from rsi_mvp.runner import notes_variant

PHANTOM = [("a", 1, None, 0.60, "base"), ("b", 2, None, 1.0, "perfect"), ("c", 3, "a", 0.65, "tuned")]
ROAP = "random_acts_of_pizza"


class FakeBackend:
    """Stands in for SgeBackend: 'runs' finish after `latency` job_state polls."""

    def __init__(self, project, runs, latency=2, fail_jobs=(), undelivered=False):
        self.p, self.runs, self.latency, self.fail, self.undelivered = project, runs, latency, set(fail_jobs), undelivered
        self.jobs, self.polls, self.n = {}, {}, 1000

    def submit(self, plan):
        self.n += 1
        self.jobs[str(self.n)] = plan
        return {"dry_run": False, "job_id": str(self.n), **plan.describe()}

    def find_run_dir(self, comp, job_id):
        return self.runs / f"run_{job_id}" if (self.runs / f"run_{job_id}").exists() else None

    def job_state(self, comp, job_id):
        self.polls[job_id] = self.polls.get(job_id, 0) + 1
        if self.polls[job_id] <= self.latency:
            return "running"
        if job_id in self.fail:
            return "finished-ungraded"
        plan = self.jobs[job_id]
        if not (self.runs / f"run_{job_id}").exists():
            notes = plan.notes_text
            variant = None if self.undelivered else notes_variant(notes)
            # a different failure mode each round so the (deterministic) mock always has something new to patch
            make_run(self.runs, name=f"run_{job_id}", mode=plan.mode, nodes=PHANTOM, final="b", grade=0.55,
                     constant_submission=plan.round >= 1,
                     variant=variant, notes=None if self.undelivered else (notes or None),
                     agent_log=None)
        return "graded"


@pytest.fixture
def project(tasks_dir, state):
    p = RsiProject(tasks_dir, state)
    for m in ("rule", "agent"):
        p.init_harness(ROAP, m)
    return p


def driver(project, backend, last=2, **kw):
    return Driver(project, backend, MockMetaLLM(), ROAP, ["rule", "agent"], last, poll_secs=0,
                  sleep=lambda s: None, log=lambda m: None, **kw)


def test_full_h0_h1_h2_for_both_modes_without_human_input(project, runs):
    out = driver(project, FakeBackend(project, runs)).run()
    assert out == "done"
    for m in ("rule", "agent"):
        assert project.store(ROAP, m).versions() == ["H0", "H1", "H2"]
        assert all(any(project._round_dir(r, ROAP, m).glob("*/run_record.json")) for r in (0, 1, 2))
    hb = json.loads((project.rounds_root / "driver_heartbeat.json").read_text())
    assert hb["status"] == "done"
    assert not (project.rounds_root / "DRIVER_HALT.json").exists()


def test_each_round_uses_the_harness_the_previous_round_produced(project, runs):
    be = FakeBackend(project, runs)
    driver(project, be).run()
    versions = sorted((p.mode, p.round, p.harness_version) for p in be.jobs.values())
    assert versions == [("agent", 0, "H0"), ("agent", 1, "H1"), ("agent", 2, "H2"),
                        ("rule", 0, "H0"), ("rule", 1, "H1"), ("rule", 2, "H2")]


def test_no_freeze_no_heldout(project, runs):
    driver(project, FakeBackend(project, runs)).run()
    assert project.frozen(ROAP, "rule") is None and not project.heldout_root.exists()
    assert all(r["competition_id"] == "random-acts-of-pizza" for r in project.index())


def test_restart_resumes_without_double_submitting(project, runs):
    be = FakeBackend(project, runs, latency=3)
    d = driver(project, be)
    assert d.run(max_polls=1) == "max_polls"                    # stopped mid-round-0
    submitted_before = len(be.jobs)
    assert driver(project, be).run() == "done"                  # a fresh driver process
    assert submitted_before == 2 and len(be.jobs) == 6, "each (mode, round) is submitted exactly once"


def test_halts_when_a_job_leaves_the_queue_without_a_grade(project, runs):
    be = FakeBackend(project, runs, fail_jobs={"1001"})
    assert driver(project, be, stale_polls=2).run() == "halted"
    halt = json.loads((project.rounds_root / "DRIVER_HALT.json").read_text())
    assert "without an official grade" in halt["reason"]


def test_halts_when_harness_delivery_cannot_be_confirmed_even_in_the_last_round(project, runs):
    """H0 has empty notes so 'undelivered' is indistinguishable there; from round 1 (H1 has notes) it is
    detectable.  last=1 means round 1 has no improve step, which used to let this slip through."""
    be = FakeBackend(project, runs, undelivered=True)
    assert driver(project, be, last=1).run() == "halted"
    reason = json.loads((project.rounds_root / "DRIVER_HALT.json").read_text())["reason"]
    assert "delivery could not be confirmed" in reason and "r1" in reason


def test_halt_file_is_written_even_before_anything_was_submitted(project, runs, tmp_path):
    import shutil
    shutil.rmtree(project.rounds_root, ignore_errors=True)
    from rsi_mvp.harness import Harness
    project.store(ROAP, "rule").commit_patch(project.store(ROAP, "rule").load("H0"),
        {"target_component": "prompt", "proposed_patch": {"op": "append", "text": "x y z"},
         "expected_effect": "e", "evidence": ["e"]})
    d = Driver(project, FakeBackend(project, runs), MockMetaLLM(), ROAP, ["rule"], 2, poll_secs=0,
               sleep=lambda s: None, log=lambda m: None)
    assert d.run() == "halted" and (project.rounds_root / "DRIVER_HALT.json").is_file()


def test_halts_instead_of_repeating_when_meta_improver_has_nothing_to_change(project, runs):
    class Quiet(MockMetaLLM):
        def complete(self, s, u):
            return '{"no_change": true, "reason": "fine"}'
    d = Driver(project, FakeBackend(project, runs), Quiet(), ROAP, ["rule"], 2, poll_secs=0,
               sleep=lambda s: None, log=lambda m: None)
    assert d.run() == "halted"
    assert "no_change" in json.loads((project.rounds_root / "DRIVER_HALT.json").read_text())["reason"]


def test_halts_if_disk_harness_is_not_the_expected_version(project, runs):
    from rsi_mvp.harness import Harness
    project.store(ROAP, "rule").commit_patch(project.store(ROAP, "rule").load("H0"),
        {"target_component": "prompt", "proposed_patch": {"op": "append", "text": "x y z"},
         "expected_effect": "e", "evidence": ["e"]})           # H1 exists before round 0 ran
    d = Driver(project, FakeBackend(project, runs), MockMetaLLM(), ROAP, ["rule"], 2, poll_secs=0,
               sleep=lambda s: None, log=lambda m: None)
    assert d.run() == "halted"
    assert "expects harness H0" in json.loads((project.rounds_root / "DRIVER_HALT.json").read_text())["reason"]
