import pytest

from conftest import make_run, nid
from rsi_mvp.collect import collect
from rsi_mvp.task import load_task
from rsi_mvp.verifiers import NOT_IMPLEMENTED, UnknownVerifier, adapter, available, run_bank

PHANTOM = [("a", 1, None, 0.60, "base"), ("b", 2, None, 1.0, "perfect score"), ("c", 3, "a", 0.65, "tuned")]


@pytest.fixture
def task(tasks_dir):
    return load_task(tasks_dir / "random_acts_of_pizza")


def bank(run, task, mode="rule"):
    return collect(run, task, mode, 0, "H0", reveal_grade=True)[1]


def by_name(b):
    return {r["verifier"]: r for r in b["results"]}


def test_clean_run_has_no_negative_reward_and_every_enabled_verifier_reports(runs, task):
    b = by_name(bank(make_run(runs), task))
    assert set(b) == set(task.enabled_verifiers)
    assert all(r["reward"] == 0.0 and r["status"] == "ok" for r in b.values())


def test_phantom_selection_is_anchoring_failure_with_evidence_and_step(runs, task):
    b = by_name(bank(make_run(runs, nodes=PHANTOM, final="b", grade=0.55), task))
    r = b["validation_argmax_anchoring"]
    assert r["reward"] == -1.0 and r["decision_steps"] == [2]
    assert any("S1_submitted_phantom" in e for e in r["evidence"]) and r["explanation"]
    assert any("S2_submit_val_rank" in e for e in r["evidence"])   # context fact attached


def test_constant_submission_is_submission_sanity_failure(runs, task):
    b = by_name(bank(make_run(runs, constant_submission=True), task))
    assert b["submission_sanity"]["reward"] == -1.0
    assert any("B1_constant_pred" in e for e in b["submission_sanity"]["evidence"])
    assert b["validation_argmax_anchoring"]["reward"] == 0.0     # only the mapped verifier fires


def test_reward_vector_is_kept_raw_not_collapsed(runs, task):
    out = bank(make_run(runs, nodes=PHANTOM, final="b", constant_submission=True), task)
    vec = out["reward_vector"]
    assert isinstance(vec, dict) and len(vec) >= 3 and set(vec.values()) >= {-1.0, 0.0}


def test_at_least_three_verifiers_reuse_the_issue_scanner(task):
    assert len(adapter.IMPLEMENTED) >= 3
    assert adapter.load_scanner().__name__ == "rsi_vendor_audit_run"   # the vendored scanner, not a rewrite


def test_info_level_override_for_fallback_to_runnable(runs, task):
    run = make_run(runs, nodes=[("a", 1, None, 0.6, "nn"), ("b", 2, "a", 0.7, "gbm")], final="b")
    import json
    j = json.loads((run / "logs/journal.json").read_text())
    j["nodes"][0]["code"] = "import torch\nprint('nn')"
    (run / "logs/journal.json").write_text(json.dumps(j))
    r = by_name(bank(run, task))["fallback_to_runnable"]
    assert r["reward"] == -0.25 and "S6_nn_abandoned" in r["evidence"][0]


def test_scanner_crash_is_an_error_status_not_a_silent_zero(runs, task, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("scanner exploded")
    monkeypatch.setattr(adapter.load_scanner(), "run_audit", boom)
    b = bank(make_run(runs), task)
    assert all(r["status"] == "error" for r in b["results"])
    assert b["reward_vector"] == {}, "errored verifiers must not appear as reward 0"
    assert "scanner exploded" in b["results"][0]["evidence"][0]


def test_unimplemented_verifier_is_reported_not_silently_dropped(runs, task):
    task.verifier["enabled"] = list(task.verifier["enabled"]) + ["decision_insensitive_prompting"]
    r = by_name(bank(make_run(runs), task))["decision_insensitive_prompting"]
    assert r["status"] == "not_applicable" and "not implemented" in r["explanation"]
    assert "decision_insensitive_prompting" in NOT_IMPLEMENTED and "decision_insensitive_prompting" in available()


def test_unknown_verifier_in_config_is_an_error(runs, task):
    task.verifier["enabled"] = ["made_up_verifier"]
    with pytest.raises(UnknownVerifier):
        bank(make_run(runs), task)


def test_run_without_journal_is_not_applicable(runs, task):
    run = make_run(runs)
    (run / "logs/journal.json").write_text('{"nodes": []}')
    assert {r["status"] for r in bank(run, task)["results"]} == {"not_applicable"}
