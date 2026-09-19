"""Verifier Bank (spec section 7): global, reusable, configured task by task.

Every verifier returns {verifier, decision_steps, reward, evidence, explanation}.
The bank returns the raw vector - it never collapses rewards into one scalar
(spec section 8) and it never hides a verifier that could not run.
"""
from __future__ import annotations

from ..schemas import make_verifier_result
from ..task import TaskPackage
from ..trajectory import Trajectory
from . import issue_scanner_adapter as adapter

# Listed in the spec but not implemented in the v0.3 MVP: it needs paired runs
# that differ only in the evidence shown to the decision agent.
NOT_IMPLEMENTED = {
    "decision_insensitive_prompting":
        "needs paired prompt A/B runs (same tree, different evidence); not implemented in v0.3 MVP",
}


class UnknownVerifier(ValueError):
    pass


def available() -> list[str]:
    return list(adapter.IMPLEMENTED) + list(NOT_IMPLEMENTED)


def run_bank(traj: Trajectory, task: TaskPackage) -> dict:
    """Run the verifiers enabled for `task` on `traj`.

    Returns {"results": [...], "reward_vector": {...}, "unmapped_detectors": [...]}.
    """
    enabled = task.enabled_verifiers
    unknown = [v for v in enabled if v not in adapter.IMPLEMENTED and v not in NOT_IMPLEMENTED]
    if unknown:
        raise UnknownVerifier(f"verifier_config enables unknown verifier(s): {unknown}; available: {available()}")

    node_step = {n["id"]: n["step"] for n in traj.nodes}
    final_step = None
    if traj.final_node_id and traj.final_node_id in node_step:
        final_step = node_step[traj.final_node_id]
    elif traj.nodes:
        final_step = traj.nodes[-1]["step"]
    scan = adapter.IssueScannerRun(traj.run_dir, node_step, final_step)

    overrides = task.verifier.get("overrides", {})
    results = []
    for name in enabled:
        if name in NOT_IMPLEMENTED:
            results.append(make_verifier_result(name, [], 0.0, [], NOT_IMPLEMENTED[name], status="not_applicable"))
        else:
            results.append(adapter.verify(name, scan, task.reward_map, task.min_severity, overrides))
    return {
        "results": results,
        "reward_vector": {r["verifier"]: r["reward"] for r in results if r["status"] == "ok"},
        "unmapped_detectors": scan.unmapped,
    }
