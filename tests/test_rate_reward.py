"""Node-level rate reward: 1 flagged node in 445 must not look like 30 in 445."""
import pytest

from conftest import make_run, nid, set_node_code
from rsi_mvp.collect import collect
from rsi_mvp.memory import CLEAN_LESSON, MemoryStore
from rsi_mvp.schemas import is_actionable, make_verifier_result
from rsi_mvp.task import TaskError, load_task
from rsi_mvp.verifiers import adapter

MANY = [(chr(97 + i % 26) + str(i // 26), i + 1, None, 0.60 + i / 10000, f"n{i}") for i in range(200)]


@pytest.fixture
def task(tasks_dir):
    return load_task(tasks_dir / "random_acts_of_pizza")


def mirage(run, task, mode="rule"):
    traj, bank = collect(run, task, mode, 0, "H0", reveal_grade=True)
    return traj, bank, next(r for r in bank["results"] if r["verifier"] == "validation_mirage")


def test_rate_tells_one_in_four_from_none(runs, task):
    run = make_run(runs)
    set_node_code(run, "a")                                    # 1 of the 4 nodes with code (a, b, c, d)
    _, bank, r = mirage(run, task)
    assert r["unit"] == "node" and r["n_flagged"] == 1 and r["n_units"] == 4
    assert r["rate"] == pytest.approx(0.25) and r["reward"] == pytest.approx(-0.25)
    assert r["actionable"] is True and r["severity_reward"] == -1.0    # the old binary reward, kept
    assert "1/4 nodes flagged by the largest pattern (25.0%" in r["explanation"]


def test_the_scanner_keeps_one_exemplar_per_pattern_so_the_hit_count_comes_from_its_message(runs, task):
    """Three flagged nodes yield ONE M2 finding ('[共 3 个节点命中此模式]'); counting findings would say 1."""
    run = make_run(runs)
    set_node_code(run, "acd")
    _, bank, r = mirage(run, task)
    assert r["n_flagged"] == 3 and r["n_units"] == 4 and r["rate"] == pytest.approx(0.75)
    assert r["rate_upper"] == pytest.approx(0.75)


def test_one_flagged_node_in_two_hundred_is_below_the_actionable_threshold(runs, task):
    """The pilot's shape: a single flagged node used to make the binary reward -1 for the whole run."""
    nodes = [(f"{chr(97 + i % 26)}{i // 26}", i + 1, None, 0.6 + i / 10000, "n") for i in range(200)]
    run = make_run(runs, nodes=nodes, final=nodes[0][0])
    set_node_code(run, "a")
    j = __import__("json").loads((run / "logs/journal.json").read_text())
    for n in j["nodes"][1:]:                                   # keep exactly ONE flagged node
        n["code"] = "print(1)"
    (run / "logs/journal.json").write_text(__import__("json").dumps(j))
    traj, bank, r = mirage(run, task)
    assert r["n_flagged"] == 1 and r["n_units"] == 200
    assert r["rate"] == pytest.approx(0.005) and r["reward"] == pytest.approx(-0.005)
    assert r["actionable"] is False and r["severity_reward"] == -1.0 and not is_actionable(r)
    recs = MemoryStore(runs / "m.jsonl").records_from_run(traj, bank, 0)
    assert [x["verifier"] for x in recs] == ["none"] and recs[0]["lesson"] == CLEAN_LESSON


def test_above_threshold_becomes_a_memory_record_with_the_rate_in_its_evidence(runs, task):
    run = make_run(runs)
    set_node_code(run, "acd")
    traj, bank, r = mirage(run, task)
    rec = next(x for x in MemoryStore(runs / "m.jsonl").records_from_run(traj, bank, 0)
               if x["verifier"] == "validation_mirage")
    assert rec["reward"] == pytest.approx(-0.75) and rec["evidence"].startswith("3/4 nodes (>= 75.0%)")


def stub_scan(findings, n_working=100):
    class Scan:
        error, n_nodes = None, n_working
        def __init__(self): self.findings, self.n_working = findings, n_working
        def steps_for(self, fs): return [1]
    return Scan()


def f(detector, layer, severity, node=None, hits=1):
    msg = detector + (f" [共 {hits} 个节点命中此模式]" if hits > 1 else "")
    return {"layer": layer, "detector": detector, "severity": severity, "node": node, "message": msg,
            "evidence": None}


def test_an_aggregate_failure_with_no_node_is_never_diluted_to_a_tiny_rate():
    scan = stub_scan([f("M2_fit_before_split", "M", "fail", nid("a")), f("S3_val_test_chasm", "S", "fail")])
    r = adapter.verify("validation_mirage", scan, {"fail": -1.0, "warn": -0.5, "clean": 0.0}, "warn",
                       units={"validation_mirage": "node"})
    assert r["rate"] == pytest.approx(0.01) and r["reward"] == -1.0 and r["actionable"] is True
    assert "aggregate finding" in r["explanation"]


def test_overlapping_patterns_give_a_lower_and_an_upper_bound_not_a_double_count():
    scan = stub_scan([f("M2_fit_before_split", "M", "fail", nid("a"), hits=30),
                      f("M7_leaky_field", "M", "fail", nid("b"), hits=20)], n_working=100)
    r = adapter.verify("validation_mirage", scan, {"fail": -1.0, "warn": -0.5, "clean": 0.0}, "warn",
                       units={"validation_mirage": "node"})
    assert r["n_flagged"] == 30 and r["rate"] == pytest.approx(0.30) and r["reward"] == pytest.approx(-0.30)
    assert r["rate_upper"] == pytest.approx(0.50)         # the union is somewhere in [30%, 50%]


def test_only_an_aggregate_failure_still_counts(runs):
    scan = stub_scan([f("S3_val_test_chasm", "S", "fail")])
    r = adapter.verify("validation_mirage", scan, {"fail": -1.0, "warn": -0.5, "clean": 0.0}, "warn",
                       units={"validation_mirage": "node"})
    assert r["reward"] == -1.0 and r["n_flagged"] == 0 and r["actionable"] is True


def test_run_level_verifiers_are_unchanged(runs, task):
    run = make_run(runs, constant_submission=True)
    _, bank = collect(run, task, "rule", 0, "H0", reveal_grade=True)
    r = next(x for x in bank["results"] if x["verifier"] == "submission_sanity")
    assert r["unit"] == "run" and r["rate"] is None and r["reward"] == -1.0 and r["actionable"] is True


def test_is_actionable_for_results_written_before_rates_existed():
    old_bad = make_verifier_result("v", [], -1.0, [], "e")
    old_ok = make_verifier_result("v", [], 0.0, [], "e")
    errored = make_verifier_result("v", [], -1.0, [], "e", status="error")
    assert is_actionable(old_bad) and not is_actionable(old_ok) and not is_actionable(errored)


def test_rate_and_flag_schema_is_validated():
    from rsi_mvp.schemas import SchemaError
    with pytest.raises(SchemaError):
        make_verifier_result("v", [], -0.1, [], "e", unit="node", rate=1.5)
    with pytest.raises(SchemaError):
        make_verifier_result("v", [], -0.1, [], "e", unit="batch")


def test_verifier_config_units_are_validated(tasks_dir, tmp_path):
    import shutil
    for bad, msg in (("units:\n  validation_mirage: batch\n", "'node' or 'run'"),
                     ("units:\n  no_such_verifier: node\n", "not an enabled verifier"),
                     ("actionable_rate: 0\n", "actionable_rate")):
        d = tmp_path / str(abs(hash(bad)))
        shutil.copytree(tasks_dir / "random_acts_of_pizza", d)
        cfg = (d / "verifier_config.yaml").read_text()
        cfg = cfg.replace("units:\n  validation_mirage: node\n", "").replace("actionable_rate: 0.01\n", "") + bad
        (d / "verifier_config.yaml").write_text(cfg)
        with pytest.raises(TaskError, match=msg):
            load_task(d)
