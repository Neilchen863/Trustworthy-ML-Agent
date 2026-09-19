"""Plain-dict schemas shared by every component (spec sections 6, 7, 9).

Everything that crosses a component boundary is a JSON-native dict so it can be
written to JSONL as-is and diffed by a human.  Validators raise SchemaError so a
malformed record fails loudly instead of silently skewing an RSI round.
"""
from __future__ import annotations

from typing import Any, Iterable

DECISION_TYPES = (
    "EXPERIMENT_PROPOSAL",
    "EXPERIMENT_LAUNCH",
    "RESULT_INTERPRETATION",
    "FAILURE_DIAGNOSIS",
    "CANDIDATE_SELECTION",
    "FALLBACK",
    "MEMORY_WRITE",
    "FINAL_SELECTION",
    "SUBMISSION",
)

MODES = ("rule", "agent")
ROLES = ("train", "test")
RATIONALE_SOURCES = ("logged", "inferred", "unavailable")
VERIFIER_STATUSES = ("ok", "not_applicable", "error")


class SchemaError(ValueError):
    pass


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SchemaError(msg)


# --------------------------------------------------------------------- events
def make_event(
    step: int,
    state_summary: str,
    decision_type: str,
    action: str,
    rationale: str | None = None,
    evidence: Iterable[str] = (),
    outcome: Any = None,
    rationale_source: str = "unavailable",
) -> dict:
    """Spec section 6 event.  `rationale_source` is an extra field that says
    whether the rationale was logged by the agent or inferred from the journal;
    a verifier must not treat an inferred rationale as agent reasoning."""
    _require(not isinstance(evidence, (str, bytes)), "event.evidence must be a list of str, not a bare str")
    ev = {
        "step": int(step),
        "state_summary": state_summary,
        "decision_type": decision_type,
        "action": action,
        "rationale": rationale,
        "evidence": list(evidence),
        "outcome": outcome,
        "rationale_source": rationale_source,
    }
    return validate_event(ev)


def validate_event(ev: dict) -> dict:
    _require(isinstance(ev, dict), "event must be a dict")
    for key in ("step", "state_summary", "decision_type", "action", "evidence"):
        _require(key in ev, f"event missing '{key}'")
    _require(isinstance(ev["step"], int) and not isinstance(ev["step"], bool), "event.step must be int")
    _require(isinstance(ev["state_summary"], str), "event.state_summary must be str")
    _require(ev["decision_type"] in DECISION_TYPES, f"unknown decision_type {ev['decision_type']!r}")
    _require(isinstance(ev["action"], str) and ev["action"], "event.action must be a non-empty str")
    _require(ev.get("rationale") is None or isinstance(ev["rationale"], str), "event.rationale must be str|None")
    _require(isinstance(ev["evidence"], list) and all(isinstance(e, str) for e in ev["evidence"]),
             "event.evidence must be list[str]")
    _require(ev.get("rationale_source", "unavailable") in RATIONALE_SOURCES, "bad rationale_source")
    return ev


# ------------------------------------------------------------------ verifiers
def make_verifier_result(
    verifier: str,
    decision_steps: Iterable[int],
    reward: float,
    evidence: Iterable[str],
    explanation: str,
    status: str = "ok",
) -> dict:
    res = {
        "verifier": verifier,
        "decision_steps": sorted({int(s) for s in decision_steps}),
        "reward": float(reward),
        "evidence": [str(e) for e in evidence],
        "explanation": explanation,
        "status": status,
    }
    return validate_verifier_result(res)


def validate_verifier_result(res: dict) -> dict:
    _require(isinstance(res, dict), "verifier result must be a dict")
    for key in ("verifier", "decision_steps", "reward", "evidence", "explanation"):
        _require(key in res, f"verifier result missing '{key}'")
    _require(isinstance(res["verifier"], str) and res["verifier"], "verifier name required")
    _require(isinstance(res["reward"], (int, float)) and not isinstance(res["reward"], bool),
             "reward must be a number")
    _require(isinstance(res["decision_steps"], list), "decision_steps must be a list")
    _require(isinstance(res["evidence"], list), "evidence must be a list")
    _require(isinstance(res["explanation"], str), "explanation must be str")
    _require(res.get("status", "ok") in VERIFIER_STATUSES, "bad verifier status")
    return res


# --------------------------------------------------------------------- memory
MEMORY_FIELDS = ("situation", "decision_outcome", "verifier", "reward", "evidence", "lesson")


def make_memory_record(
    situation: str,
    decision_outcome: str,
    verifier: str,
    reward: float,
    evidence: str,
    lesson: str,
    **provenance: Any,
) -> dict:
    """Spec section 9 record + provenance (task/mode/round/harness_version/run_id)."""
    rec = {
        "situation": situation,
        "decision_outcome": decision_outcome,
        "verifier": verifier,
        "reward": float(reward),
        "evidence": evidence,
        "lesson": lesson,
    }
    rec.update(provenance)
    return validate_memory_record(rec)


def validate_memory_record(rec: dict) -> dict:
    _require(isinstance(rec, dict), "memory record must be a dict")
    for key in MEMORY_FIELDS:
        _require(key in rec, f"memory record missing '{key}'")
    for key in ("situation", "decision_outcome", "verifier", "evidence", "lesson"):
        _require(isinstance(rec[key], str), f"memory.{key} must be str")
    _require(isinstance(rec["reward"], (int, float)) and not isinstance(rec["reward"], bool),
             "memory.reward must be a number")
    return rec
