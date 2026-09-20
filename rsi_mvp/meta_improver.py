"""Meta-improver (spec section 10): H_t + verifier rewards/findings + task
performance + memory  ->  one small, bounded harness patch.

The LLM only ever *proposes*.  Its output is validated and bounded by
harness.apply_patch; anything invalid, unparsable or out of bounds yields
status="rejected" and NO new harness version.  Everything it saw and said is
returned so the caller can persist the full record next to the diff.
"""
from __future__ import annotations

import hashlib
import json

from .guards import assert_train
from .schemas import is_actionable
from .harness import (MAX_APPEND_CHARS, MAX_RULE_KEYS_PER_PATCH, MEMORY_BOUNDS, RULE_BOUNDS, Harness,
                      PatchError, validate_patch)
from .llm import LLM, LLMError

MAX_EVIDENCE_PER_VERIFIER = 3


def build_input(harness: Harness, runs: list[dict], memory: list[dict]) -> dict:
    """`runs` are the collected run records of THIS round for (task, mode).

    Each must carry role == "train": the official grade is part of the input, so
    a held-out task here is a contamination bug and raises immediately."""
    for run in runs:
        assert_train(run["role"], f"pass run {run['run_id']} of task {run['task']} to the meta-improver")
    agg: dict[str, list[float]] = {}
    for run in runs:
        for name, reward in run["reward_vector"].items():
            agg.setdefault(name, []).append(reward)
    return {
        "harness": harness.to_dict(),
        "runs": [{
            "run_id": r["run_id"], "task": r["task"], "round": r["round"],
            "harness_version": r["harness_version"],
            "task_performance": r["task_performance"],
            "trajectory": r["trajectory"],
            "reward_vector": r["reward_vector"],
            "verifier_results": [
                {**v, "evidence": v["evidence"][:MAX_EVIDENCE_PER_VERIFIER]}
                for v in r["verifier_results"] if is_actionable(v)],
            "delivery_ok": r["delivery"].get("ok", False),
        } for r in runs],
        "aggregate_reward_vector": {k: sum(v) / len(v) for k, v in sorted(agg.items())},
        "memory": [{k: m[k] for k in ("verifier", "reward", "lesson", "situation")} for m in memory],
    }


def system_prompt(mode: str) -> str:
    if mode == "rule":
        policy = (f"decision_policy (rule mode): proposed_patch = {{\"op\":\"set\",\"values\":{{...}}}} with 1 to "
                  f"{MAX_RULE_KEYS_PER_PATCH} of these whitelisted keys and bounds: "
                  + ", ".join(f"{k} in [{lo}, {hi}]" for k, (lo, hi, _) in RULE_BOUNDS.items()))
    else:
        policy = ("decision_policy (agent mode): proposed_patch = {\"op\":\"append\"|\"replace\",\"text\":\"...\"} "
                  "editing the instructions the decision agent reads when it chooses what to try next and what to submit")
    return (
        "You improve the harness of an automated ML-engineering agent (AIDE) between rounds. You see the current "
        "harness, verifier rewards with evidence (negative reward = a detected failure mode), task performance and "
        "memory. Propose ONE small patch to ONE component that you expect to fix the most important detected failure "
        "mode. Do not rewrite the agent, the task or the evaluator; you cannot.\n\n"
        "Rewards: a node-level verifier reports reward = -(fraction of working nodes it flagged) together with "
        "`rate`, `n_flagged`, `n_units`; a run-level verifier reports -1/-0.5/0 by severity. Only verifiers with "
        "`actionable` true are listed as failures; a small rate that is not listed is noise - do not patch for it. "
        "If nothing is listed, reply no_change.\n\n"
        "Components you may patch (target_component):\n"
        f"- prompt: {{\"op\":\"append\"|\"replace\",\"text\":\"...\"}}; append at most {MAX_APPEND_CHARS} characters of "
        "generic guidance; replace must stay similar to the current text.\n"
        f"- {policy}\n"
        "- memory_policy: {\"op\":\"set\",\"values\":{...}} with keys "
        + ", ".join(f"{k} in [{lo}, {hi}]" for k, (lo, hi, _) in MEMORY_BOUNDS.items()) + ".\n\n"
        "Rules: guidance must be generic and actionable; never mention graders, hidden data, answer files or test labels; "
        "cite evidence from the given runs. Reply with a single JSON object: "
        "{\"target_component\":..., \"proposed_patch\":..., \"expected_effect\":\"...\", \"evidence\":[\"...\"]}. "
        "If no change is justified reply {\"no_change\": true, \"reason\": \"...\"}.")


def propose(llm: LLM, harness: Harness, runs: list[dict], memory: list[dict]) -> dict:
    """Returns a record: status in {"proposed","no_change","rejected","error"}."""
    payload = build_input(harness, runs, memory)
    system, user = system_prompt(harness.mode), json.dumps(payload, ensure_ascii=False, sort_keys=True)
    record = {"provider": llm.provider, "model": llm.model, "mode": harness.mode,
              "from_version": harness.version,
              "input_sha256": hashlib.sha256((system + user).encode()).hexdigest(),
              "input": payload, "raw_response": None, "patch": None, "reason": None}
    try:
        raw = llm.complete(system, user)
    except LLMError as exc:
        return {**record, "status": "error", "reason": str(exc)}
    record["raw_response"] = raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {**record, "status": "rejected", "reason": f"response is not JSON: {exc}"}
    if isinstance(data, dict) and data.get("no_change"):
        return {**record, "status": "no_change", "reason": str(data.get("reason", ""))[:300]}
    try:
        validate_patch(data)
    except PatchError as exc:
        return {**record, "status": "rejected", "reason": str(exc)}
    return {**record, "status": "proposed", "patch": data}
