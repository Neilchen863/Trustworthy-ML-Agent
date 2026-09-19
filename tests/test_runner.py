import os
import stat

import pytest

from rsi_mvp.harness import Harness
from rsi_mvp.runner import DryRunBackend, RunnerError, SgeBackend, build_env, make_plan, notes_variant
from rsi_mvp.task import load_task


@pytest.fixture
def task(tasks_dir):
    return load_task(tasks_dir / "random_acts_of_pizza")


def rule_h(**rc):
    base = {"max_stagnation": 15, "debug_prob": 1.0, "max_debug_depth": 20, "num_drafts": 5}
    return Harness(task="random_acts_of_pizza", mode="rule", version="H0", parent=None, rule_config={**base, **rc})


def test_h0_is_the_stock_control_no_prompt_variant(task):
    plan = make_plan(task, "rule", rule_h(), "", 0, 1)
    assert plan.variant is None and "PROMPT_VARIANT" not in plan.env
    assert plan.env["AIDE_SELECTION_MODE"] == "rule" and plan.env["SKIP_TASK_NOTES"] == "1"


def test_variant_is_content_addressed():
    assert notes_variant("") is None and notes_variant("  \n") is None
    a, b = notes_variant("alpha"), notes_variant("beta")
    assert a.startswith("rsi_") and a != b and a == notes_variant("alpha")


def test_rule_config_reaches_the_runner_only_through_caller_overridable_vars(task):
    env = build_env(task, "rule", rule_h(max_stagnation=8, debug_prob=0.3, num_drafts=3), None, 1)
    assert env["AIDE_MAX_STAGNATION"] == "8" and env["AIDE_DEBUG_PROB"] == "0.3"
    assert env["AIDE_EXTRA_KWARGS"] == "agent.search.num_drafts=3"
    # every var we set that env.sh also sets must be in the research repo's caller-override whitelist
    whitelist = {"AIDE_MAX_STAGNATION", "AIDE_DEBUG_PROB", "AIDE_MAX_DEBUG_DEPTH", "AIDE_EXTRA_KWARGS",
                 "TIME_LIMIT_SECS", "AIDE_STEPS", "EXEC_TIMEOUT", "REQ_CPUS", "REQ_GPUS", "AIDE_CODE_MODEL",
                 "AIDE_FEEDBACK_MODEL", "AIDE_SELECTION_MODE", "AIDE_FEEDBACK", "AIDE_TEST_FEEDBACK",
                 "AIDE_TEST_EXPOSE", "AIDE_TREE_TOPK", "AIDE_TREE_RECENT", "PROMPT_VARIANT", "SKIP_TASK_NOTES",
                 "AIDE_SEED_CODE", "AIDE_SEED_PLAN"}
    assert set(env) - {"RSI_REPLICATE"} <= whitelist


def test_agent_mode_env_and_inner_agent_never_sees_test_signal(task):
    h = Harness(task="t", mode="agent", version="H0", parent=None)
    env = build_env(task, "agent", h, "rsi_x", 2)
    assert env["AIDE_SELECTION_MODE"] == "agent" and env["PROMPT_VARIANT"] == "rsi_x"
    assert env["AIDE_TEST_FEEDBACK"] == "0" and env["AIDE_TEST_EXPOSE"] == "none" and env["AIDE_FEEDBACK"] == "0"
    assert env["TIME_LIMIT_SECS"] == "14400" and env["AIDE_STEPS"] == "500"
    assert "AIDE_MAX_STAGNATION" not in env


def test_env_never_carries_a_secret(task):
    os.environ["OPENAI_API_KEY"] = "sk-should-not-be-copied"
    try:
        env = build_env(task, "rule", rule_h(), "rsi_x", 1)
    finally:
        del os.environ["OPENAI_API_KEY"]
    assert not any("sk-" in v or "KEY" in k for k, v in env.items())


def test_dry_run_touches_nothing(task, tmp_path):
    out = DryRunBackend().submit(make_plan(task, "rule", rule_h(), "note", 0, 1))
    assert out["dry_run"] and out["command"] == ["bash", "sge/submit.sh", "random-acts-of-pizza"]
    assert list(tmp_path.iterdir()) == []


def fake_aide_root(tmp_path, job="4242", fail=False):
    root = tmp_path / "aide"
    (root / "sge").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "config/tasks").mkdir(parents=True)
    (root / "scripts/run_aide.sh").write_text("")
    (root / "scripts/_common.sh").write_text("")
    sub = root / "sge/submit.sh"
    sub.write_text("#!/bin/bash\n" + ("exit 3\n" if fail else
                   f'env | grep -E "^(PROMPT_VARIANT|AIDE_SELECTION_MODE|AIDE_MAX_STAGNATION)=" > "$PWD/seen.env"\n'
                   f'echo "Your job {job} (\\"aide-x\\") has been submitted"\n'))
    sub.chmod(sub.stat().st_mode | stat.S_IEXEC)
    return root


def test_sge_backend_stages_notes_and_forwards_env(task, tmp_path):
    root = fake_aide_root(tmp_path)
    plan = make_plan(task, "rule", rule_h(max_stagnation=9), "Check the output.", 0, 1)
    res = SgeBackend(root).submit(plan)
    assert res["job_id"] == "4242"
    notes = root / "config/tasks" / f"random-acts-of-pizza.notes.{plan.variant}.txt"
    assert notes.read_text() == "Check the output."
    seen = (root / "seen.env").read_text()
    assert f"PROMPT_VARIANT={plan.variant}" in seen and "AIDE_MAX_STAGNATION=9" in seen and "AIDE_SELECTION_MODE=rule" in seen


def test_variant_files_are_immutable(task, tmp_path):
    root = fake_aide_root(tmp_path)
    plan = make_plan(task, "rule", rule_h(), "Same text", 0, 1)
    SgeBackend(root).submit(plan)
    (root / "config/tasks" / f"random-acts-of-pizza.notes.{plan.variant}.txt").write_text("tampered")
    with pytest.raises(RunnerError, match="immutable"):
        SgeBackend(root).submit(plan)


def test_backend_errors_are_loud(task, tmp_path, monkeypatch):
    monkeypatch.delenv("MLEBENCH_AIDE_ROOT", raising=False)
    with pytest.raises(RunnerError, match="MLEBENCH_AIDE_ROOT"):
        SgeBackend()
    with pytest.raises(RunnerError, match="not a MLE-bench_AIDE checkout"):
        SgeBackend(tmp_path).preflight()
    with pytest.raises(RunnerError, match="submit.sh failed"):
        SgeBackend(fake_aide_root(tmp_path, fail=True)).submit(make_plan(task, "rule", rule_h(), "", 0, 1))


def test_find_run_dir_and_job_state(tmp_path):
    root = fake_aide_root(tmp_path)
    b = SgeBackend(root)
    assert b.find_run_dir("random-acts-of-pizza", "4242") is None
    run = root / "runs/random-acts-of-pizza/20260101_gpt4o_4h_j4242"
    run.mkdir(parents=True)
    assert b.find_run_dir("random-acts-of-pizza", "4242") == run
    (run / "grade_report.txt").write_text("{}")
    assert b.job_state("random-acts-of-pizza", "4242") == "graded"


def test_env_vars_match_the_research_repos_real_override_whitelist(task):
    """When the parent research repo is present, check against its actual
    scripts/_common.sh instead of the hand-copied list above (skipped otherwise)."""
    import re
    from pathlib import Path
    common = Path(__file__).resolve().parents[2] / "scripts" / "_common.sh"
    if not common.is_file():
        pytest.skip("research repo not next to this repo")
    block = re.search(r"_CALLER_OVERRIDE_VARS=\((.*?)\)", common.read_text(), re.S).group(1)
    real = set(block.split())
    h = Harness(task="t", mode="rule", version="H0", parent=None,
                rule_config={"max_stagnation": 15, "debug_prob": 1.0, "max_debug_depth": 20, "num_drafts": 5})
    for mode in ("rule", "agent"):
        env = build_env(task, mode, h if mode == "rule" else Harness(task="t", mode="agent", version="H0", parent=None),
                        "rsi_x", 1)
        assert set(env) - {"RSI_REPLICATE"} <= real, set(env) - {"RSI_REPLICATE"} - real
