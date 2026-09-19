import shutil

import pytest

from rsi_mvp.task import TaskError, load_task, load_tasks


def test_packages_load_and_roles(tasks_dir):
    tasks = load_tasks(tasks_dir)
    assert tasks["random_acts_of_pizza"].role == "train"
    assert tasks["insults_heldout"].role == "test"
    roap = tasks["random_acts_of_pizza"]
    assert set(roap.modes) == {"rule", "agent"}
    assert roap.initial_rule_config()["max_stagnation"] == 15
    assert "validation_argmax_anchoring" in roap.enabled_verifiers


def test_each_task_has_the_spec_layout(tasks_dir):
    for name in ("random_acts_of_pizza", "insults_heldout"):
        for f in ("task.yaml", "aide_config.yaml", "verifier_config.yaml", "instruction.md", "run.py", "evaluate.py"):
            assert (tasks_dir / name / f).is_file(), f"{name}/{f}"
        assert (tasks_dir / name / "env").is_dir() and (tasks_dir / name / "data").is_dir()


def test_train_test_competition_overlap_is_rejected(tasks_dir, tmp_path):
    shutil.copytree(tasks_dir / "random_acts_of_pizza", tmp_path / "a")
    shutil.copytree(tasks_dir / "random_acts_of_pizza", tmp_path / "b")
    yb = (tmp_path / "b/task.yaml").read_text().replace("name: random_acts_of_pizza", "name: other").replace(
        "role: train", "role: test")
    (tmp_path / "b/task.yaml").write_text(yb)
    with pytest.raises(TaskError, match="both train and test"):
        load_tasks(tmp_path)


def test_unknown_mode_and_bad_role_rejected(tasks_dir, tmp_path):
    shutil.copytree(tasks_dir / "random_acts_of_pizza", tmp_path / "t")
    (tmp_path / "t/aide_config.yaml").write_text("default_mode: mcts\nmodes:\n  mcts: {}\n")
    with pytest.raises(TaskError, match="only wraps"):
        load_task(tmp_path / "t")
    shutil.copytree(tasks_dir / "random_acts_of_pizza", tmp_path / "u")
    (tmp_path / "u/task.yaml").write_text((tmp_path / "u/task.yaml").read_text().replace("role: train", "role: dev"))
    with pytest.raises(TaskError, match="role"):
        load_task(tmp_path / "u")
