import importlib.util
import json
import stat
from pathlib import Path

import pytest

from rsi_mvp.harness import Harness
from rsi_mvp.runner import RunnerError, SgeBackend, make_plan
from rsi_mvp.task import load_task

ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    spec = importlib.util.spec_from_file_location("smoke_overlay", ROOT / "tools" / "smoke_overlay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_root(tmp_path):
    root = tmp_path / "aide"
    for d in ("sge", "scripts", "config/tasks"):
        (root / d).mkdir(parents=True)
    (root / "scripts/run_aide.sh").write_text("")
    (root / "scripts/_common.sh").write_text("")
    sub = root / "sge/submit.sh"
    sub.write_text('#!/bin/bash\nenv | grep -E "^OVERLAY_PATH=" > "$PWD/seen.env"\n'
                   'echo "Your job 7 (\\"x\\") has been submitted"\n')
    sub.chmod(sub.stat().st_mode | stat.S_IEXEC)
    return root


def plan(tasks_dir):
    h = Harness(task="random_acts_of_pizza", mode="agent", version="H0", parent=None)
    return make_plan(load_task(tasks_dir / "random_acts_of_pizza"), "agent", h, "", 0, 1)


def test_the_dedicated_overlay_reaches_the_job_and_is_recorded(tasks_dir, tmp_path):
    root, overlay = fake_root(tmp_path), tmp_path / "v2.overlay"
    overlay.write_bytes(b"x")
    res = SgeBackend(root, overlay).submit(plan(tasks_dir))
    assert f"OVERLAY_PATH={overlay.resolve()}" in (root / "seen.env").read_text()
    assert res["env"]["OVERLAY_PATH"] == str(overlay.resolve())


def test_without_a_dedicated_overlay_the_shared_one_is_left_alone(tasks_dir, tmp_path, monkeypatch):
    monkeypatch.delenv("RSI_OVERLAY_PATH", raising=False)
    monkeypatch.delenv("OVERLAY_PATH", raising=False)
    root = fake_root(tmp_path)
    res = SgeBackend(root).submit(plan(tasks_dir))
    assert (root / "seen.env").read_text() == "" and "OVERLAY_PATH" not in res["env"]


def test_a_missing_dedicated_overlay_is_an_error_not_a_silent_fallback(tasks_dir, tmp_path):
    with pytest.raises(RunnerError, match="dedicated overlay not found"):
        SgeBackend(fake_root(tmp_path), tmp_path / "nope.overlay").submit(plan(tasks_dir))


def test_the_env_var_can_supply_the_overlay(tmp_path, monkeypatch):
    overlay = tmp_path / "e.overlay"
    overlay.write_bytes(b"x")
    monkeypatch.setenv("RSI_OVERLAY_PATH", str(overlay))
    assert SgeBackend(fake_root(tmp_path)).overlay_path == overlay.resolve()


def test_smoke_static_check_discriminates_a_fixed_parser_from_a_buggy_one():
    tool = load_tool()
    fixed = dict(tool.CASES)
    buggy = {**fixed, "Mean AUC over 5-fold CV: 0.6323": 5.0, "Average ROC AUC across 5 folds: 0.6323": 5.0}
    assert tool.evaluate_static(fixed)["ok"]
    bad = tool.evaluate_static(buggy)
    assert not bad["ok"] and {w[0] for w in bad["wrong"]} == {"Mean AUC over 5-fold CV: 0.6323",
                                                              "Average ROC AUC across 5 folds: 0.6323"}
    assert not tool.evaluate_static({})["ok"], "a missing parse must never pass"


def test_probe_seed_prints_the_phrase_that_used_to_parse_as_five():
    tool = load_tool()
    assert tool.PROBE_LINE in tool.PROBE_SEED and "5-fold" in tool.PROBE_LINE
    compile(tool.PROBE_SEED, "probe", "exec")


def test_meta_improver_sees_the_interval_and_is_told_not_to_take_a_midpoint():
    from rsi_mvp import meta_improver
    rec = {"run_id": "r", "task": "t", "role": "train", "round": 0, "harness_version": "H0",
           "task_performance": None, "trajectory": {}, "delivery": {"ok": True},
           "reward_vector": {"validation_mirage": -0.05},
           "verifier_results": [{"verifier": "validation_mirage", "status": "ok", "reward": -0.05, "evidence": ["e"],
                                 "decision_steps": [], "explanation": "x", "unit": "node", "rate": 0.05,
                                 "rate_upper": 0.15, "n_flagged": 25, "n_units": 500, "actionable": True}]}
    data = meta_improver.build_input(Harness(task="t", mode="agent", version="H0", parent=None), [rec, rec], [])
    assert data["node_rate_intervals"]["validation_mirage"] == {"lower_mean": 0.05, "upper_mean": 0.15, "n_runs": 2}
    assert data["runs"][0]["verifier_results"][0]["rate_upper"] == 0.15
    prompt = meta_improver.system_prompt("agent")
    assert "LOWER bound" in prompt and "rate_upper" in prompt and "midpoint" in prompt
