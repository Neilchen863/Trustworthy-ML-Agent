"""Stage-aware account of a train-only-field failure, for the improver (no LLM, no code execution).

The verifier bank says WHAT went wrong (a leaky field was used, the submission is constant, the submitted node is a phantom).
It does not say HOW the failure survived, or which of the facts existed for the LLM that had to decide.  This module rebuilds
the chain from one finished run

    field first used -> crash on the test side -> repair -> perfect validation feedback -> degraded predictions -> final choice

and, for each stage, checks whether that fact is present in the prompt of the LAST submission-choosing decision (measured by
searching the logged prompt, never assumed).  That lets the improver tell "the instruction was not specific enough" from "the
information was never shown to the LLM that had to decide".

It reads only what a run already leaves behind: the journal, the per-node archived submission files (candidate predictions, no
labels), aide.log and aide.verbose.log.  It never reads the official grade.  Compute it for training tasks only.
"""
from __future__ import annotations

import csv
import json
import re
import statistics
from pathlib import Path

PERFECT = 0.99                                  # a validation metric at or above this is treated as "perfect"
_RECORD = re.compile(r"^\[20\d\d-\d\d-\d\d \d\d:\d\d:\d\d,\d+\] (?:INFO|DEBUG|WARNING|ERROR)", re.M)


def suspect_fields(verifier_results: list[dict]) -> list[str]:
    """Field names the leaky-field detector named (`M7_leaky_field` findings quote them in backticks)."""
    out: list[str] = []
    for v in verifier_results:
        for e in v.get("evidence", []) or []:
            if "M7_leaky_field" in str(e):
                for name in re.findall(r"`([A-Za-z_][\w.]*)`", str(e)):
                    if name not in out:
                        out.append(name)
    return out


def _kind(node: dict, by_id: dict) -> str:
    parent = by_id.get(node.get("parent"))
    return "draft" if parent is None else ("debug" if parent.get("is_buggy") else "improve")


def _metric(node: dict):
    m = node.get("metric")
    v = m.get("value") if isinstance(m, dict) else None
    return float(v) if isinstance(v, (int, float)) and not node.get("is_buggy") else None


def _predictions(run_dir: Path, node: dict) -> dict | None:
    path = run_dir / "agent" / "workspaces" / "exp" / "submissions" / (str(node["id"]).replace("-", "") + ".csv")
    if not path.is_file():
        return None
    try:
        rows = list(csv.reader(path.open()))
        col = len(rows[0]) - 1                                  # prediction column of an `id,prediction` file
        values = [float(r[col]) for r in rows[1:] if len(r) > col and r[col] != ""]
    except (OSError, ValueError, IndexError):
        return None
    if not values:
        return None
    return {"n_unique": len(set(values)), "std": round(statistics.pstdev(values), 6), "rows": len(values),
            "min": min(values), "max": max(values)}


def _last_submit_prompt(run_dir: Path) -> str | None:
    """The logged prompt of the last `choose_submission` call, or None (rule mode, or no verbose log)."""
    path = run_dir / "logs" / "aide.verbose.log"
    if not path.is_file():
        return None
    text = path.read_text(errors="ignore")
    at = text.rfind("'name': 'choose_submission'")
    if at < 0:
        return None
    starts = [m.start() for m in _RECORD.finditer(text)]
    k = max((i for i, s in enumerate(starts) if s <= at), default=None)
    if k is None or k == 0:
        return None
    return text[starts[k - 1]:starts[k]]


def profile_exposure(run_dir: Path) -> dict:
    """Where AIDE's submission profile actually appeared: the launcher's setting and the number of logged LLM prompts that
    contain it (measured on aide.verbose.log; the submit-choice prompt is checked separately in decision_visibility)."""
    cfg = ""
    cfg_path = run_dir / "run_config.txt"
    if cfg_path.is_file():
        m = re.search(r"^sub_stats\s*=\s*(\S+)", cfg_path.read_text(errors="ignore"), re.M)
        cfg = m.group(1) if m else ""
    log = run_dir / "logs" / "aide.verbose.log"
    prompts = None
    if log.is_file():
        starts = [m.start() for m in _RECORD.finditer(log.read_text(errors="ignore"))]
        text = log.read_text(errors="ignore")
        prompts = sum("[submission-stats]" in text[a:b] for a, b in zip(starts, starts[1:] + [len(text)]))
    return {"sub_stats_setting": cfg or "off", "prompts_containing_profile": prompts}


def _norm(s: str) -> str:
    return " ".join(s.replace('\\"', '"').replace("\\n", " ").split())


def decision_visibility(prompt: str | None, fields: list[str], chosen: dict | None) -> dict:
    """What the submission-choosing LLM could read, measured on the logged prompt."""
    if prompt is None:
        return {"known": False}
    low = _norm(prompt)
    keys = sorted(set(re.findall(r'"(\w+)":', prompt)) & {"validation_metric", "plan", "findings", "cv_scores", "cv_mean",
                                                          "code", "stage", "parent_id", "is_buggy", "metric_source", "term_out"})
    analysis = _norm(str((chosen or {}).get("analysis") or ""))[:50]
    return {"known": True, "candidate_fields": keys,
            "shows_code": '"code":' in prompt or "import pandas" in prompt,
            "shows_prediction_profile": "[submission-stats]" in prompt or "n_unique" in prompt,
            "mentions_suspect_field": any(f in prompt for f in fields),
            "mentions_keyerror": "KeyError" in prompt,
            "chosen_feedback_visible": bool(analysis) and analysis in low,
            "chosen_feedback_praises_perfect": bool(re.search(r"perfect|excellent|impressive", low, re.I))}


def build_stage_trace(run_dir: str | Path, nodes: list[dict], final_node_id: str | None, fields: list[str]) -> dict | None:
    """None when no suspect field was named.  Steps are AIDE journal steps."""
    if not fields:
        return None
    run_dir = Path(run_dir)
    nodes = sorted(nodes, key=lambda n: int(n.get("step") or 0))
    by_id = {n["id"]: n for n in nodes}
    uses = lambda n, f=None: any(x in n.get("code", "") for x in ([f] if f else fields))
    users = [n for n in nodes if uses(n)]
    if not users:
        return None
    stages: list[dict] = []
    first = users[0]
    field = next(f for f in fields if f in first["code"])
    stages.append({"stage": "field_first_used", "steps": [first["step"]],
                   "what": f"step {first['step']} ({_kind(first, by_id)}) first uses `{field}`"})

    crashed = [n for n in nodes if n.get("is_buggy") and n.get("exc_type") == "KeyError"
               and any(f in (n.get("analysis") or "") for f in fields)]
    if crashed:
        stages.append({"stage": "crash_on_missing_field", "steps": [n["step"] for n in crashed],
                       "what": f"{len(crashed)} node(s) crashed with KeyError naming the field; feedback text: "
                               + _norm(crashed[0].get("analysis") or "")[:160]})
        crashed_ids = {n["id"] for n in crashed}

        def descends_from_crash(n):
            seen, cur = set(), n
            while cur is not None and cur["id"] not in seen:
                if cur["id"] in crashed_ids and cur is not n:
                    return True
                seen.add(cur["id"]); cur = by_id.get(cur.get("parent"))
            return False

        guard_re = [re.compile(rf"[\"']{re.escape(f)}[\"']\s+in\s+\w+\.columns") for f in fields]
        guard = next((n for n in nodes if uses(n) and descends_from_crash(n)
                      and any(r.search(n["code"]) for r in guard_re)), None)
        ran = next((n for n in nodes if not n.get("is_buggy") and uses(n) and descends_from_crash(n)
                    and (guard is None or n["step"] >= guard["step"])), None)
        if guard is not None or ran is not None:
            what = []
            if guard is not None:
                what.append(f"step {guard['step']} ({_kind(guard, by_id)}) keeps the field and adds a column-presence guard "
                            "that falls back to a constant when the column is absent instead of removing the feature")
            if ran is not None:
                what.append(f"the field-using code first runs to completion at step {ran['step']}")
            stages.append({"stage": "repair_kept_field", "steps": sorted({n["step"] for n in (guard, ran) if n is not None}),
                           "what": "; ".join(what)})
    perfect = [n for n in nodes if (_metric(n) or 0) >= PERFECT]
    if perfect:
        pn = perfect[0]
        stages.append({"stage": "perfect_validation", "steps": [n["step"] for n in perfect],
                       "what": f"{len(perfect)} node(s) with validation >= {PERFECT}; first is step {pn['step']} "
                               f"({_metric(pn):.4f}); its feedback text: " + _norm(pn.get("analysis") or "")[:160]})
    prof = [(n, _predictions(run_dir, n)) for n in nodes]
    prof = [(n, p) for n, p in prof if p]
    with_f = [(n, p) for n, p in prof if uses(n)]
    without = [(n, p) for n, p in prof if not uses(n)]
    if with_f:
        const = [n["step"] for n, p in with_f if p["n_unique"] == 1]
        stages.append({"stage": "degraded_test_predictions", "steps": [n["step"] for n, _ in with_f],
                       "what": f"{len(with_f)} field-using node(s) with archived predictions: "
                               + ", ".join(f"step {n['step']} n_unique={p['n_unique']} std={p['std']}" for n, p in with_f)
                               + (f"; exactly constant at steps {const}" if const else "")
                               + (f". Nodes without the field: std {min(p['std'] for _, p in without)}-"
                                  f"{max(p['std'] for _, p in without)}, n_unique >= {min(p['n_unique'] for _, p in without)}"
                                  if without else "")})
    final = by_id.get(final_node_id)
    if final is not None:
        honest = sorted((n for n in nodes if _metric(n) is not None and not uses(n)), key=lambda n: -_metric(n))[:2]
        fp = _predictions(run_dir, final)
        stages.append({"stage": "final_selection", "steps": [final["step"]],
                       "what": f"submitted step {final['step']} (validation {_metric(final)}, "
                               f"{'uses' if uses(final) else 'does not use'} the field"
                               + (f", n_unique={fp['n_unique']}" if fp else "") + ")"
                               + (f"; alternatives without the field: "
                                  + ", ".join(f"step {n['step']} ({_metric(n):.4f})" for n in honest) if honest else "")})
    log = (run_dir / "logs" / "aide.log")
    reasoning = None
    if log.is_file() and final is not None:
        hits = re.findall(rf"\[agent submit\] LLM chose node={re.escape(str(final['id'])[:8])} \((.*)", log.read_text(errors="ignore"))
        reasoning = _norm(hits[-1])[:220] if hits else None
    vis = decision_visibility(_last_submit_prompt(run_dir), fields, final)
    not_visible = []
    if vis.get("known"):
        if not vis["shows_code"]:
            not_visible.append("candidate code (so a defaulted or repaired feature is invisible)")
        if not vis["shows_prediction_profile"]:
            not_visible.append("the candidates' test-prediction profile (n_unique / std)")
        if not vis["mentions_suspect_field"]:
            not_visible.append(f"any mention of the suspect field(s) {fields}")
        if not vis["mentions_keyerror"]:
            not_visible.append("the test-side KeyError that revealed the field was missing at prediction time")
    return {"fields": fields, "stages": stages, "decision_prompt": vis, "not_visible_to_decision_llm": not_visible,
            "decision_reasoning_excerpt": reasoning, "profile_exposure": profile_exposure(run_dir),
            "note": "Steps are journal steps; visibility is measured on the logged prompt of the last choose_submission call."}


def render_stage_trace(trace: dict | None) -> str:
    if not trace:
        return ""
    lines = ["Stage trace (how the failure survived; from this run's own files):"]
    for i, s in enumerate(trace["stages"], 1):
        lines.append(f"  {i}. {s['stage']}: {s['what']}")
    v = trace["decision_prompt"]
    if v.get("known"):
        lines.append("  What the submission-choosing LLM could read: candidate fields " + ", ".join(v["candidate_fields"])
                     + f"; code shown={v['shows_code']}; prediction profile shown={v['shows_prediction_profile']}; "
                       f"suspect field mentioned={v['mentions_suspect_field']}; chosen node's feedback visible={v['chosen_feedback_visible']}"
                       f" (praises a perfect/excellent result={v['chosen_feedback_praises_perfect']}).")
    else:
        lines.append("  What the submission-choosing LLM could read: unknown (no logged decision prompt).")
    pe = trace.get("profile_exposure") or {}
    lines.append(f"  Submission profile setting: {pe.get('sub_stats_setting')}; logged LLM prompts containing it: "
                 f"{pe.get('prompts_containing_profile')} (the submit-choice prompt is reported above).")
    if trace["not_visible_to_decision_llm"]:
        lines.append("  Not visible to it: " + "; ".join(trace["not_visible_to_decision_llm"]) + ".")
    if trace.get("decision_reasoning_excerpt"):
        lines.append("  Its stated reason: " + trace["decision_reasoning_excerpt"])
    return "\n".join(lines) + "\n"
