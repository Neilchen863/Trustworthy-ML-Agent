from conftest import AGENT_LOG, make_run, nid
from rsi_mvp.events import extract_events, parse_agent_log
from rsi_mvp.schemas import DECISION_TYPES, validate_event


def types(events):
    return [e["decision_type"] for e in events]


def test_agent_mode_uses_logged_rationale_and_selection_events(runs):
    run = make_run(runs, mode="agent", agent_log=AGENT_LOG)
    ev = extract_events(run, "agent")
    assert all(validate_event(e) for e in ev)
    props = [e for e in ev if e["decision_type"] == "EXPERIMENT_PROPOSAL"]
    assert [p["action"] for p in props] == ["draft", "draft", "improve:aaaaaaaa", "improve:cccccccc"]
    assert props[2]["rationale"] == "a is the best so far" and props[2]["rationale_source"] == "logged"
    sel = [e for e in ev if e["decision_type"] == "CANDIDATE_SELECTION"]
    assert [s["action"] for s in sel] == ["select:cccccccc", "select:dddddddd"]
    assert all(s["outcome"]["is_val_argmax"] for s in sel)
    final = [e for e in ev if e["decision_type"] == "FINAL_SELECTION"][0]
    assert final["action"] == "submit:dddddddd" and final["rationale_source"] == "logged"
    assert final["outcome"]["val_rank"] == 1


def test_rule_mode_has_inferred_rationale_and_same_schema(runs):
    run = make_run(runs, mode="rule")
    ev = extract_events(run, "rule")
    assert all(validate_event(e) for e in ev)
    props = [e for e in ev if e["decision_type"] == "EXPERIMENT_PROPOSAL"]
    assert all(p["rationale_source"] == "inferred" for p in props)
    assert "greedy improve" in props[3]["rationale"]
    final = [e for e in ev if e["decision_type"] == "FINAL_SELECTION"][0]
    assert "argmax" in final["rationale"] and final["rationale_source"] == "inferred"
    assert not [e for e in ev if e["decision_type"] == "CANDIDATE_SELECTION"]


def test_failure_diagnosis_only_for_buggy_nodes_and_decision_types_are_valid(runs):
    ev = extract_events(make_run(runs, mode="rule"), "rule")
    diag = [e for e in ev if e["decision_type"] == "FAILURE_DIAGNOSIS"]
    assert len(diag) == 1 and diag[0]["step"] == 2
    assert set(types(ev)) <= set(DECISION_TYPES)


def test_fallback_event_for_draft_after_initial_drafts(runs):
    nodes = [("a", 1, None, 0.6, "a"), ("b", 2, "a", 0.62, "b"), ("c", 3, None, 0.7, "fresh restart")]
    ev = extract_events(make_run(runs, nodes=nodes, final="c", mode="rule"), "rule")
    fb = [e for e in ev if e["decision_type"] == "FALLBACK"]
    assert len(fb) == 1 and fb[0]["step"] == 3


def test_misaligned_agent_log_never_attaches_wrong_rationale(runs):
    bad_log = [l.replace("action=improve node=aaaaaaaa", "action=improve node=ffffffff") for l in AGENT_LOG]
    ev = extract_events(make_run(runs, mode="agent", agent_log=bad_log), "agent")
    prop = [e for e in ev if e["decision_type"] == "EXPERIMENT_PROPOSAL"][2]
    assert prop["rationale"] is None and prop["rationale_source"] == "unavailable"


def test_parse_agent_log_marks_fallback(runs):
    log = ["INFO: [agent search] LLM chose action=debug node=abcd (x)",
           "WARNING: [agent search] failed (agent search produced an unusable choice: debug abcd) -> falling back to rule-based policy"]
    run = make_run(runs, mode="agent", agent_log=log)
    assert parse_agent_log(run)[0]["fallback"].startswith("agent search produced")
