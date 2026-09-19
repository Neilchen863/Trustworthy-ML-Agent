import json

import pytest

from conftest import make_run
from rsi_mvp import cli

PHANTOM = [("a", 1, None, 0.60, "base"), ("b", 2, None, 1.0, "perfect score"), ("c", 3, "a", 0.65, "tuned")]


def run_cli(tasks_dir, state, *argv):
    cli.main(["--tasks-dir", str(tasks_dir), "--state-root", str(state), *argv])


def test_init_and_dry_run_submit(tasks_dir, state, capsys):
    run_cli(tasks_dir, state, "init", "--task", "random_acts_of_pizza")
    assert "created random_acts_of_pizza/rule/H0" in capsys.readouterr().out
    run_cli(tasks_dir, state, "submit", "--task", "random_acts_of_pizza", "--mode", "agent", "--round", "0", "--dry-run")
    plan = json.loads(capsys.readouterr().out)
    assert plan["dry_run"] and plan["env"]["AIDE_SELECTION_MODE"] == "agent" and plan["variant"] is None


def test_heldout_submit_before_freeze_exits_nonzero(tasks_dir, state, capsys):
    run_cli(tasks_dir, state, "init", "--task", "random_acts_of_pizza")
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        run_cli(tasks_dir, state, "submit", "--task", "insults_heldout", "--mode", "rule", "--round", "-1",
                "--harness-task", "random_acts_of_pizza", "--harness", "H0", "--dry-run")
    assert e.value.code == 2 and "freeze" in capsys.readouterr().err


def test_replay_demo_end_to_end(tasks_dir, state, tmp_path, capsys):
    runs = [make_run(tmp_path / "runs", name=f"r{i}", mode="rule", nodes=PHANTOM, final="b", grade=0.5 + i / 10,
                     constant_submission=(i == 1)) for i in range(3)]
    run_cli(tasks_dir, state, "replay-demo", "--task", "random_acts_of_pizza", "--mode", "rule",
            "--workdir", str(tmp_path / "w"), "--runs", *map(str, runs))
    out = capsys.readouterr().out
    assert "harness versions: ['H0', 'H1', 'H2']" in out and out.count("meta-improver[mock]") == 2
