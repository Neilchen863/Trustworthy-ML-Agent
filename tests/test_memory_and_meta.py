import json

import pytest

from conftest import make_run
from rsi_mvp import meta_improver
from rsi_mvp.collect import collect
from rsi_mvp.guards import ContaminationError
from rsi_mvp.harness import Harness
from rsi_mvp.llm import LLMError, MockMetaLLM
from rsi_mvp.memory import CLEAN_LESSON, LESSONS, MemoryStore
from rsi_mvp.task import load_task

PHANTOM = [("a", 1, None, 0.60, "base"), ("b", 2, None, 1.0, "perfect score"), ("c", 3, "a", 0.65, "tuned")]


@pytest.fixture
def task(tasks_dir):
    return load_task(tasks_dir / "random_acts_of_pizza")


def run_record(runs, task, mode="rule", role="train", **kw):
    run = make_run(runs, mode=mode, **kw)
    traj, bank = collect(run, task, mode, 0, "H0", reveal_grade=True)
    return traj, bank, {
        "run_id": run.name, "task": task.name, "role": role, "mode": mode, "round": 0, "harness_version": "H0",
        "task_performance": traj.grade, "trajectory": traj.summary(), "reward_vector": bank["reward_vector"],
        "verifier_results": bank["results"], "delivery": {"ok": True}}


def test_memory_records_follow_the_spec_schema(runs, task, tmp_path):
    traj, bank, _ = run_record(runs, task, nodes=PHANTOM, final="b", grade=0.55)
    mem = MemoryStore(tmp_path / "memory.jsonl")
    recs = mem.records_from_run(traj, bank, 0)
    mem.append(recs)
    got = mem.all()
    assert {"situation", "decision_outcome", "verifier", "reward", "evidence", "lesson"} <= set(got[0])
    anchor = next(r for r in got if r["verifier"] == "validation_argmax_anchoring")
    assert anchor["reward"] == -1.0 and "official score 0.55" in anchor["decision_outcome"]
    assert anchor["lesson"] == LESSONS["validation_argmax_anchoring"]
    assert anchor["run_id"] == traj.run_dir.name and anchor["harness_version"] == "H0"


def test_clean_run_writes_one_clean_record_and_memory_persists(runs, task, tmp_path):
    traj, bank, _ = run_record(runs, task)
    path = tmp_path / "m.jsonl"
    MemoryStore(path).append(MemoryStore(path).records_from_run(traj, bank, 0))
    assert [r["lesson"] for r in MemoryStore(path).all()] == [CLEAN_LESSON]    # a fresh store re-reads the file
    assert MemoryStore(path).retrieve(5) == []                                  # clean records are not lessons


def test_retrieve_dedupes_lessons_and_orders_worst_first(tmp_path):
    from rsi_mvp.schemas import make_memory_record as mk
    mem = MemoryStore(tmp_path / "m.jsonl")
    mem.append([mk("s", "o", "v1", -0.5, "e", "L1"), mk("s", "o", "v2", -1.0, "e", "L2"),
                mk("s", "o", "v1", -1.0, "e", "L1"), mk("s", "o", "v3", -0.25, "e", "L3")])
    # worst reward first; the duplicate L1 (-0.5) is dropped in favour of its worse (-1.0) copy
    assert [(r["lesson"], r["reward"]) for r in mem.retrieve(10)] == [("L1", -1.0), ("L2", -1.0), ("L3", -0.25)]
    assert len(mem.retrieve(2)) == 2
    assert mem.render_lessons(10, 10_000).count("\n") == 2
    assert len(mem.render_lessons(10, 8)) <= 8


def test_mock_meta_improver_proposes_a_valid_bounded_patch(runs, task):
    traj, bank, rec = run_record(runs, task, mode="agent", nodes=PHANTOM, final="b", grade=0.55)
    h = Harness(task="t", mode="agent", version="H0", parent=None)
    out = meta_improver.propose(MockMetaLLM(), h, [rec], [])
    assert out["status"] == "proposed" and out["patch"]["target_component"] == "decision_policy"
    assert out["patch"]["evidence"] and out["input_sha256"] and out["provider"] == "mock"
    assert json.dumps(out["input"])                      # full input kept for audit


def test_rule_mode_anchoring_maps_to_rule_config_patch(runs, task):
    traj, bank, rec = run_record(runs, task, mode="rule", nodes=PHANTOM, final="b", grade=0.55,
                                 constant_submission=False)
    h = Harness(task="t", mode="rule", version="H0", parent=None,
                rule_config={"max_stagnation": 15, "debug_prob": 1.0, "max_debug_depth": 20, "num_drafts": 5})
    # make anchoring the single worst signal so the mock targets it
    rec["reward_vector"] = {"validation_argmax_anchoring": -1.0}
    out = meta_improver.propose(MockMetaLLM(), h, [rec], [])
    assert out["patch"]["proposed_patch"] == {"op": "set", "values": {"max_stagnation": 7}}


def test_no_change_bad_json_invalid_patch_and_llm_error(runs, task):
    _, _, rec = run_record(runs, task)
    h = Harness(task="t", mode="agent", version="H0", parent=None)

    class Fixed:
        provider, model = "fixed", "x"
        def __init__(self, text): self.text = text
        def complete(self, s, u): 
            if self.text is None:
                raise LLMError("no key")
            return self.text

    assert meta_improver.propose(Fixed('{"no_change": true, "reason": "fine"}'), h, [rec], [])["status"] == "no_change"
    assert meta_improver.propose(Fixed("not json"), h, [rec], [])["status"] == "rejected"
    assert meta_improver.propose(Fixed('{"target_component": "code"}'), h, [rec], [])["status"] == "rejected"
    err = meta_improver.propose(Fixed(None), h, [rec], [])
    assert err["status"] == "error" and "no key" in err["reason"]


def test_meta_improver_input_refuses_a_heldout_run(runs, task):
    _, _, rec = run_record(runs, task, role="test")
    h = Harness(task="t", mode="agent", version="H0", parent=None)
    with pytest.raises(ContaminationError, match="only role='train'"):
        meta_improver.build_input(h, [rec], [])
