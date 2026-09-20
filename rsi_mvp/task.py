"""Task package loader (spec section 4).

A task package is a directory holding task.yaml, aide_config.yaml and
verifier_config.yaml next to the fixed environment.  The environment itself
(data, image, MLE-bench grader) lives on CRC and is never modified by RSI; the
package only *names* it.

`role` encodes the train/test discipline the RSI loop enforces:
  train : official grade may be shown to the meta-improver
  test  : official grade must never reach the meta-improver (see loop.py)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .schemas import MODES, ROLES


class TaskError(ValueError):
    pass


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        raise TaskError(f"missing task file: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise TaskError(f"{path} must contain a mapping")
    return data


@dataclass
class TaskPackage:
    root: Path
    task: dict
    aide: dict
    verifier: dict

    # ---- identity
    @property
    def name(self) -> str:
        return self.task["name"]

    @property
    def competition_id(self) -> str:
        return self.task["competition_id"]

    @property
    def role(self) -> str:
        return self.task["role"]

    @property
    def budget(self) -> dict:
        return self.task["budget"]

    @property
    def metric(self) -> dict:
        return self.task["metric"]

    # ---- AIDE mode configuration
    @property
    def modes(self) -> tuple[str, ...]:
        return tuple(self.aide["modes"].keys())

    @property
    def default_mode(self) -> str:
        return self.aide["default_mode"]

    def common_settings(self) -> dict:
        return dict(self.aide.get("common", {}))

    def initial_rule_config(self) -> dict:
        return dict(self.aide["modes"]["rule"].get("initial_rule_config", {}))

    def mode_settings(self, mode: str) -> dict:
        if mode not in self.aide["modes"]:
            raise TaskError(f"task {self.name!r} does not enable mode {mode!r} (has {self.modes})")
        return dict(self.aide["modes"][mode])

    # ---- verifiers
    @property
    def enabled_verifiers(self) -> list[str]:
        return list(self.verifier["enabled"])

    @property
    def reward_map(self) -> dict[str, float]:
        return {k: float(v) for k, v in self.verifier["reward_map"].items()}

    @property
    def min_severity(self) -> str:
        return self.verifier.get("min_severity", "warn")

    def resolve(self, relative: str) -> Path:
        return (self.root / relative).resolve()


def load_task(root: str | Path) -> TaskPackage:
    root = Path(root)
    task = _load_yaml(root / "task.yaml")
    aide = _load_yaml(root / "aide_config.yaml")
    verifier = _load_yaml(root / "verifier_config.yaml")
    pkg = TaskPackage(root=root, task=task, aide=aide, verifier=verifier)
    _validate(pkg)
    return pkg


def _validate(pkg: TaskPackage) -> None:
    for key in ("name", "competition_id", "role", "metric", "budget"):
        if key not in pkg.task:
            raise TaskError(f"{pkg.root}/task.yaml missing '{key}'")
    if pkg.role not in ROLES:
        raise TaskError(f"task.role must be one of {ROLES}, got {pkg.role!r}")
    for key in ("run_secs", "steps"):
        if not isinstance(pkg.budget.get(key), int) or pkg.budget[key] <= 0:
            raise TaskError(f"task.budget.{key} must be a positive int")
    aide_modes = pkg.aide.get("modes")
    if not isinstance(aide_modes, dict) or not aide_modes:
        raise TaskError("aide_config.yaml needs a non-empty 'modes' mapping")
    for mode in aide_modes:
        if mode not in MODES:
            raise TaskError(f"unknown AIDE mode {mode!r}; the MVP only wraps {MODES}")
    if pkg.default_mode not in aide_modes:
        raise TaskError("aide_config.default_mode must be one of the enabled modes")
    for key in ("enabled", "reward_map"):
        if key not in pkg.verifier:
            raise TaskError(f"verifier_config.yaml missing '{key}'")
    if pkg.min_severity not in ("warn", "fail"):
        raise TaskError("verifier_config.min_severity must be 'warn' or 'fail'")
    for name, unit in pkg.verifier.get("units", {}).items():
        if unit not in ("node", "run"):
            raise TaskError(f"verifier_config.units.{name} must be 'node' or 'run', got {unit!r}")
        if name not in pkg.enabled_verifiers:
            raise TaskError(f"verifier_config.units names {name!r}, which is not an enabled verifier")
    rate = pkg.verifier.get("actionable_rate", 0.01)
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0.0 < rate <= 1.0:
        raise TaskError("verifier_config.actionable_rate must be a number in (0, 1]")


def load_tasks(tasks_dir: str | Path) -> dict[str, TaskPackage]:
    """Load every package under tasks_dir, refusing a train/test overlap: a
    competition that is both a training task and a held-out task would defeat
    the whole point of the held-out split."""
    tasks: dict[str, TaskPackage] = {}
    for child in sorted(Path(tasks_dir).iterdir()):
        if (child / "task.yaml").is_file():
            pkg = load_task(child)
            if pkg.name in tasks:
                raise TaskError(f"duplicate task name {pkg.name!r}")
            tasks[pkg.name] = pkg
    by_comp: dict[str, set[str]] = {}
    for pkg in tasks.values():
        by_comp.setdefault(pkg.competition_id, set()).add(pkg.role)
    overlap = [c for c, roles in by_comp.items() if len(roles) > 1]
    if overlap:
        raise TaskError(f"competition(s) {overlap} appear as both train and test tasks")
    return tasks
