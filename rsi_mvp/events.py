"""Unified decision events from an existing AIDE run (spec section 6).

AIDE is not modified.  Both modes leave the same durable evidence:

  * logs/journal.json   -> every node, its parent, buggy flag, metric, analysis
  * code/node_id.txt    -> the node that was finally published
  * logs/aide.log       -> agent mode only: the LLM's per-step choice + reasoning

So events are reconstructed *post hoc*.  Where the agent logged a reasoning it is
copied verbatim (`rationale_source="logged"`); where it did not (rule mode has no
reasoning at all) the rationale is a deterministic description of the rule that
must have fired (`"inferred"`).  Verifiers can therefore tell agent reasoning
from reconstruction.
"""
from __future__ import annotations

import re
from pathlib import Path

from .schemas import make_event
from .trajectory import metric_value, node_maximize, read_final_node_id, read_journal

_SEARCH_LLM = re.compile(r"\[agent search\] LLM chose action=(\w+) node=([0-9a-f]*) \((.*)\)\s*$")
_SEARCH_NOTHING = re.compile(r"\[agent search\] nothing to improve/debug -> draft")
_SEARCH_FALLBACK = re.compile(r"\[agent search\] failed \((.*)\) -> falling back to rule-based policy")
_SUBMIT_LLM = re.compile(r"\[agent submit\] LLM chose node=([0-9a-f]+) \((.*)\)\s*$")

_TRUNC = "...[truncated]"


def parse_agent_log(run_dir: Path) -> list[dict]:
    """One bucket per search step, in log order.  A bucket starts at the step's
    search decision and collects the submit decision(s) that follow it."""
    path = Path(run_dir) / "logs" / "aide.log"
    if not path.is_file():
        return []
    buckets: list[dict] = []
    for line in path.read_text(errors="ignore").splitlines():
        m = _SEARCH_LLM.search(line)
        if m:
            buckets.append({"action": m.group(1), "node": m.group(2), "reason": _clean(m.group(3)),
                            "fallback": None, "submit_node": None, "submit_reason": None})
            continue
        if _SEARCH_NOTHING.search(line):
            buckets.append({"action": "draft", "node": "", "reason": None,
                            "fallback": None, "submit_node": None, "submit_reason": None})
            continue
        m = _SEARCH_FALLBACK.search(line)
        if m and buckets:
            buckets[-1]["fallback"] = m.group(1)
            continue
        m = _SUBMIT_LLM.search(line)
        if m and buckets:
            buckets[-1]["submit_node"] = m.group(1)
            buckets[-1]["submit_reason"] = _clean(m.group(2))
    return buckets


def _clean(text: str) -> str:
    return text.replace(" ...[truncated]", _TRUNC).strip()


def node_stage(node: dict, by_id: dict[str, dict]) -> str:
    parent = node.get("parent")
    if not parent or parent in ("None", "null"):
        return "draft"
    return "debug" if by_id.get(parent, {}).get("is_buggy") else "improve"


def _short(text: str | None, n: int = 240) -> str | None:
    if text is None:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n] + "..."


def _best(nodes: list[dict], maximize: bool) -> tuple[float | None, str | None]:
    scored = [(metric_value(n), n["id"]) for n in nodes if metric_value(n) is not None]
    if not scored:
        return None, None
    return (max if maximize else min)(scored, key=lambda t: t[0])


def _state(before: list[dict], maximize: bool) -> str:
    val, nid = _best(before, maximize)
    best = "none yet" if val is None else f"{val:.6g} (node {nid[:8]})"
    last = before[-1] if before else None
    tail = "" if last is None else ("; latest candidate failed" if last.get("is_buggy") else "; latest candidate ok")
    return f"{len(before)} nodes; best val={best}{tail}"


def _inferred_rationale(stage: str, node: dict, before: list[dict], maximize: bool, n_lead: int) -> str:
    if stage == "draft":
        if len([n for n in before if n.get("parent") in (None, "None")]) < n_lead or not before:
            return "rule: initial draft (fewer than num_drafts root nodes exist)"
        return "rule: fresh draft (stagnation guardrail fired or no working node to improve)"
    if stage == "debug":
        return "rule: debug a buggy leaf chosen at random (debug_prob branch)"
    _, best_id = _best(before, maximize)
    if node.get("parent") == best_id:
        return "rule: greedy improve of the best-validation node"
    return "rule: improve (parent is not the current best-validation node)"


def _leading_drafts(nodes: list[dict]) -> int:
    n = 0
    for node in nodes:
        if node.get("parent") in (None, "None"):
            n += 1
        else:
            break
    return n


def extract_events(run_dir: str | Path, mode: str, grade_outcome: dict | None = None) -> list[dict]:
    run_dir = Path(run_dir)
    nodes = read_journal(run_dir)
    if not nodes:
        return []
    by_id = {n["id"]: n for n in nodes}
    maximize = node_maximize(nodes)
    buckets = parse_agent_log(run_dir) if mode == "agent" else []
    aligned = len(buckets) >= len(nodes) > 0
    n_lead = max(_leading_drafts(nodes), 1)
    events: list[dict] = []
    last_submit_reason: dict[str, str] = {}

    for i, node in enumerate(nodes):
        step = node["step"]
        before = nodes[:i]
        stage = node_stage(node, by_id)
        state = _state(before, maximize)
        parent = node.get("parent") if stage != "draft" else None
        nid8 = node["id"][:8]
        bucket = buckets[i] if aligned else None
        # A bucket is only trusted if it agrees with what the journal says happened.
        if bucket and not (bucket["action"] == stage and (stage == "draft" or node["parent"].startswith(bucket["node"]))):
            if not bucket["fallback"]:
                bucket = None
        if bucket and bucket["reason"] and not bucket["fallback"]:
            rationale, source = bucket["reason"], "logged"
        elif bucket and bucket["fallback"]:
            rationale, source = f"agent choice unusable ({_short(bucket['fallback'], 120)}); rule fallback used", "logged"
        elif mode == "agent":
            rationale, source = None, "unavailable"
        else:
            rationale, source = _inferred_rationale(stage, node, before, maximize, n_lead), "inferred"

        action = "draft" if stage == "draft" else f"{stage}:{parent[:8]}"
        events.append(make_event(step, state, "EXPERIMENT_PROPOSAL", action, rationale,
                                 [f"logs/journal.json#node={nid8}"], None, source))
        if stage == "draft" and i >= n_lead:
            events.append(make_event(step, state, "FALLBACK", "fresh_draft_after_initial_drafts", rationale,
                                     [f"logs/journal.json#node={nid8}"], None, source))
        events.append(make_event(
            step, state, "EXPERIMENT_LAUNCH", f"execute:{nid8}", None,
            [f"logs/journal.json#node={nid8}"],
            {"exec_time": node.get("exec_time"), "exc_type": node.get("exc_type")}))

        val = metric_value(node)
        state_after = _state(nodes[: i + 1], maximize)
        events.append(make_event(
            step, state_after, "RESULT_INTERPRETATION", f"record_metric:{nid8}",
            _short(node.get("analysis")), [f"logs/journal.json#node={nid8}"],
            {"val": val, "buggy": bool(node.get("is_buggy"))}, "logged"))
        if node.get("is_buggy"):
            events.append(make_event(
                step, state_after, "FAILURE_DIAGNOSIS", f"diagnose_failure:{nid8}",
                _short(node.get("analysis")), [f"logs/journal.json#node={nid8}"],
                {"exc_type": node.get("exc_type")}, "logged"))

        if bucket and bucket["submit_node"]:
            chosen = next((n for n in nodes[: i + 1] if n["id"].startswith(bucket["submit_node"])), None)
            best_val, best_id = _best(nodes[: i + 1], maximize)
            events.append(make_event(
                step, state_after, "CANDIDATE_SELECTION", f"select:{bucket['submit_node'][:8]}",
                bucket["submit_reason"], ["logs/aide.log"],
                {"is_val_argmax": bool(chosen and chosen["id"] == best_id)}, "logged"))
            last_submit_reason[bucket["submit_node"][:8]] = bucket["submit_reason"] or ""

    events.extend(_final_events(run_dir, mode, nodes, maximize, last_submit_reason, grade_outcome))
    return sorted(events, key=lambda e: e["step"])  # stable: keeps per-step insertion order


def _final_events(run_dir, mode, nodes, maximize, submit_reasons, grade_outcome):
    final_id = read_final_node_id(run_dir)
    last_step = nodes[-1]["step"]
    if not final_id:
        return []
    final = next((n for n in nodes if n["id"] == final_id), None)
    scored = sorted(((metric_value(n), n["id"]) for n in nodes if metric_value(n) is not None),
                    key=lambda t: t[0], reverse=maximize)
    rank = next((k + 1 for k, (_, nid) in enumerate(scored) if nid == final_id), None)
    val = metric_value(final) if final else None
    state = (f"{len(nodes)} nodes; {len(scored)} working; final node {final_id[:8]} "
             f"val={val if val is not None else 'n/a'} val_rank={rank}/{len(scored)}")
    if mode == "agent" and final_id[:8] in submit_reasons:
        rationale, source = submit_reasons[final_id[:8]], "logged"
    elif mode == "rule":
        rationale, source = "rule: submit the argmax-validation node (Journal.get_best_node)", "inferred"
    else:
        rationale, source = None, "unavailable"
    ev = [
        make_event(last_step, state, "FINAL_SELECTION", f"submit:{final_id[:8]}", rationale,
                   ["code/node_id.txt", "logs/journal.json"],
                   {"val_rank": rank, "n_working": len(scored)}, source),
        make_event(last_step, state, "SUBMISSION", "write_submission_csv", None,
                   ["submission/submission.csv", "grade_report.txt"], grade_outcome, "unavailable"),
    ]
    return ev
