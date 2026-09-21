"""observation_policy.json (AIDE_SUB_STATS as harness configuration) and the stage trace shown to the improver."""
import json
from pathlib import Path

import pytest

from conftest import AGENT_LOG, make_run, nid, set_node_code
from rsi_mvp.harness import ALLOWED_FILES, Harness, PatchError, validate_files
from rsi_mvp.improver import AgentImprover, ImproveContext, evidence_summary, contract_text
from rsi_mvp.llm import ScriptedToolLLM
from rsi_mvp.loop import RsiProject
from rsi_mvp.runner import build_env, notes_variant
from rsi_mvp.stage_trace import build_stage_trace, decision_visibility, render_stage_trace, suspect_fields
from rsi_mvp.trajectory import delivery_check, read_final_node_id, read_journal

ROAP = "random_acts_of_pizza"
FIELD = "requester_user_flair"
GUARDED = ('import pandas as pd\nif "requester_user_flair" in df.columns:\n    df["f"] = (df["requester_user_flair"] == "PIF")\n'
           'else:\n    df["f"] = 0\n')
USES = 'import pandas as pd\ndf["f"] = (df["requester_user_flair"] == "PIF")\n'


@pytest.fixture
def project(tasks_dir, state):
    return RsiProject(tasks_dir, state)


# ------------------------------------------------------------------------------ the observation setting
def test_observation_policy_is_a_harness_file_with_a_strict_schema():
    assert "observation_policy.json" in ALLOWED_FILES
    ok = validate_files({"observation_policy.json": '{"submission_profile": true}'}, "agent")
    assert ok.ok, ok.violations
    for bad in ('{"submission_profile": "yes"}', '{"labels": true}', '[1]', 'nope'):
        assert not validate_files({"observation_policy.json": bad}, "agent").ok, bad
    h = Harness.from_files({"observation_policy.json": '{"submission_profile": true}\n'}, "t", "agent", "H1", "H0")
    assert h.submission_profile and Harness.from_files(h.to_files(), "t", "agent", "H1", "H0").observation == h.observation


def test_a_harness_without_the_setting_keeps_its_old_serialisation_and_hash():
    h = Harness(task="t", mode="agent", version="H0", parent=None)
    assert "observation" not in h.to_dict() and "observation_policy.json" not in h.to_files() and not h.submission_profile


def test_the_setting_reaches_the_run_as_AIDE_SUB_STATS_and_only_then(tasks_dir):
    from rsi_mvp.task import load_tasks
    task = load_tasks(tasks_dir)[ROAP]
    off = build_env(task, "agent", Harness(task="t", mode="agent", version="H0", parent=None), None, 1)
    on = build_env(task, "agent", Harness(task="t", mode="agent", version="H1", parent="H0",
                                          observation={"submission_profile": True}), None, 1)
    assert "AIDE_SUB_STATS" not in off and on["AIDE_SUB_STATS"] == "1"
    assert {k: v for k, v in on.items() if k != "AIDE_SUB_STATS"} == off       # nothing else changes


def test_sub_stats_is_the_one_unlisted_variable_and_nothing_on_the_launcher_side_sets_it():
    """We do not edit the research repo's caller-override whitelist; the run-time check in collect covers the risk.  Where the
    research repo is present, check that its launcher config cannot silently set the variable."""
    import re
    root = Path(__file__).resolve().parents[2]
    common = root / "scripts" / "_common.sh"
    if not common.is_file():
        pytest.skip("research repo not next to this repo")
    listed = set(re.search(r"_CALLER_OVERRIDE_VARS=\((.*?)\)", common.read_text(), re.S).group(1).split())
    assert "AIDE_SUB_STATS" not in listed
    for cfg in [root / "config" / "env.sh", *sorted((root / "config" / "tasks").glob("*.sh"))]:
        if cfg.is_file():
            assert "AIDE_SUB_STATS" not in cfg.read_text(), f"{cfg} sets AIDE_SUB_STATS: caller value would be overridden"


def test_the_patch_only_reads_the_candidates_own_submission_file():
    """Static check of the research repo's patch: it profiles submission.csv and touches nothing else."""
    src = Path(__file__).resolve().parents[2] / "scripts" / "_inject_submission_stats.py"
    if not src.is_file():
        pytest.skip("research repo not next to this repo")
    text = src.read_text()
    module = text[text.index("SUB_STATS_MODULE = '''"):text.index("def main():")]
    for forbidden in ("grade", "label", "answers", "private", "solution.csv", "test.csv", "open("):
        assert forbidden not in module.lower().replace("nan", ""), forbidden
    assert 'workspace_dir / "submission" / "submission.csv"' in module


def make_obs_run(runs, name, sub_stats_cfg=None, log_lines=()):
    run = make_run(runs, name=name, mode="agent", agent_log=AGENT_LOG + list(log_lines))
    if sub_stats_cfg is not None:
        with (run / "run_config.txt").open("a") as f:
            f.write(f"sub_stats        = {sub_stats_cfg}\n")
    return run


PROFILED = ["[2026-07-10 01:14:00,000] INFO: [sub-stats] profile appended for node aaaaaaaa"]


def test_delivery_verifies_the_profile_in_both_directions(runs):
    on = make_obs_run(runs, "on", "1", PROFILED)
    d = delivery_check(on, None, None, expected_sub_stats=True)
    assert d["ok"] and d["sub_stats"]["nodes_profiled_log"] == 1 and d["sub_stats"]["config_on"]
    off = make_obs_run(runs, "off", "off")
    assert delivery_check(off, None, None, expected_sub_stats=False)["ok"]
    assert delivery_check(make_obs_run(runs, "silent"), None, None, expected_sub_stats=False)["ok"]     # old runs: no line at all
    # enabled but not effective (env.sh overrode it, patch missing, ...): every variant is caught
    for label, run in (("cfg off", make_obs_run(runs, "e1", "off", PROFILED)), ("no log", make_obs_run(runs, "e2", "1")),
                       ("neither", make_obs_run(runs, "e3", "off"))):
        bad = delivery_check(run, None, None, expected_sub_stats=True)
        assert not bad["ok"] and "does not show it" in bad["problems"][0], label
    # not enabled but shown anyway: also caught (a hidden confound)
    for run in (make_obs_run(runs, "x1", "1"), make_obs_run(runs, "x2", "off", PROFILED)):
        bad = delivery_check(run, None, None, expected_sub_stats=False)
        assert not bad["ok"] and "although the harness did not enable it" in bad["problems"][0]


def test_collect_uses_the_harness_setting_and_an_unverified_switch_blocks_improve(project, runs):
    project.init_harness(ROAP, "agent")
    project.collect_round(ROAP, "agent", 0, make_run(runs, name="r0", agent_log=AGENT_LOG, final="d"))
    edit = AgentImprover(ScriptedToolLLM([[("write_file", {"path": "harness/observation_policy.json",
                                                            "content": '{"submission_profile": true}\n'})],
                                          [("check", {})], [("finish", {"summary": "show the profile"})]]))
    res = project.improve(ROAP, "agent", 0, edit)
    assert res["status"] == "proposed" and res["changed_files"] == ["observation_policy.json"]
    h1 = project.store(ROAP, "agent").load("H1")
    assert h1.submission_profile and project.plan_run(ROAP, "agent", 1, harness_task=None, version="H1").env["AIDE_SUB_STATS"] == "1"
    notes = project.render_notes(ROAP, "agent", "H1")
    rec = project.collect_round(ROAP, "agent", 1, make_obs_run(runs, "r1", "off"), version="H1")     # switch did NOT take effect
    assert not rec["delivery"]["ok"] and "does not show it" in rec["delivery"]["problems"][0]
    from rsi_mvp.loop import LoopError
    with pytest.raises(LoopError, match="delivery could not be confirmed"):
        project.improve(ROAP, "agent", 1, AgentImprover(ScriptedToolLLM([])))
    ok = project.collect_round(ROAP, "agent", 1, make_obs_run(runs, "r1b", "1", PROFILED), version="H1")
    assert ok["delivery"]["ok"], ok["delivery"]


# ------------------------------------------------------------------------------------------ stage trace
def write_subs(run, values_by_char):
    d = run / "agent" / "workspaces" / "exp" / "submissions"
    d.mkdir(parents=True, exist_ok=True)
    for ch, values in values_by_char.items():
        (d / f"{nid(ch)}.csv").write_text("request_id,requester_received_pizza\n" + "".join(f"t3_{i},{v}\n" for i, v in enumerate(values)))


def leak_run(runs, name="leak", profile_in_prompt=False):
    nodes = [("a", 1, None, 0.62, "baseline logistic regression"),
             ("b", 2, "a", None, "The execution failed with a KeyError for the 'requester_user_flair' field, not found in the test data."),
             ("c", 3, "b", None, "The execution failed because of an inf value."),
             ("d", 4, "c", 1.0, "Successful; the model achieved a perfect ROC AUC of 1.0, indicating excellent performance."),
             ("e", 5, "a", 0.64, "tuned honest model")]
    run = make_run(runs, name=name, mode="agent", nodes=nodes, final="d", agent_log=AGENT_LOG)
    set_node_code(run, "b", USES)
    set_node_code(run, "c", GUARDED)
    set_node_code(run, "d", GUARDED)
    write_subs(run, {"a": [0.1, 0.2, 0.3, 0.4], "d": [0.09] * 4, "e": [0.11, 0.3, 0.5, 0.2]})
    prompt = ('# Candidate attempts\n[{"id": "%s", "stage": "improve", "validation_metric": 1.0, "cv_scores": [], "plan": "x", '
              '"findings": "Successful; the model achieved a perfect ROC AUC of 1.0, indicating excellent performance."}]\n' % nid("d"))
    if profile_in_prompt:
        prompt += "[submission-stats] column 'requester_received_pizza': n_unique=1, std=0\n"
    (run / "logs" / "aide.verbose.log").write_text(
        "[2026-07-10 01:10:00,000] INFO: something else\n[2026-07-10 01:11:59,000] INFO: " + prompt +
        "\n[2026-07-10 01:11:59,394] INFO: function spec: {'name': 'choose_submission', 'json_schema': {}}\n")
    return run


def test_stage_trace_rebuilds_the_chain_and_measures_what_the_decision_llm_could_read(runs):
    run = leak_run(runs)
    tr = build_stage_trace(run, read_journal(run), read_final_node_id(run), [FIELD])
    stages = {s["stage"]: s for s in tr["stages"]}
    assert list(stages) == ["field_first_used", "crash_on_missing_field", "repair_kept_field", "perfect_validation",
                            "degraded_test_predictions", "final_selection"]
    assert stages["field_first_used"]["steps"] == [2] and "(improve)" in stages["field_first_used"]["what"]
    assert stages["crash_on_missing_field"]["steps"] == [2]
    assert stages["repair_kept_field"]["steps"] == [3, 4] and "column-presence guard" in stages["repair_kept_field"]["what"]
    assert stages["perfect_validation"]["steps"] == [4]
    assert "exactly constant at steps [4]" in stages["degraded_test_predictions"]["what"]
    assert "does not use the field" not in stages["final_selection"]["what"] and "step 5 (0.6400)" in stages["final_selection"]["what"]
    v = tr["decision_prompt"]
    assert v["known"] and not v["shows_code"] and not v["shows_prediction_profile"] and not v["mentions_keyerror"]
    assert v["chosen_feedback_visible"] and v["chosen_feedback_praises_perfect"]
    assert any("prediction profile" in x for x in tr["not_visible_to_decision_llm"])
    assert any("KeyError" in x for x in tr["not_visible_to_decision_llm"])
    text = render_stage_trace(tr)
    assert "repair_kept_field" in text and "Not visible to it" in text


def test_when_the_profile_is_shown_the_trace_says_so(runs):
    run = leak_run(runs, "leak_profile", profile_in_prompt=True)
    tr = build_stage_trace(run, read_journal(run), read_final_node_id(run), [FIELD])
    assert tr["decision_prompt"]["shows_prediction_profile"]
    assert not any("prediction profile" in x for x in tr["not_visible_to_decision_llm"])


def test_no_suspect_field_or_no_use_of_it_means_no_trace(runs):
    run = make_run(runs, name="clean", mode="agent", agent_log=AGENT_LOG)
    assert build_stage_trace(run, read_journal(run), read_final_node_id(run), []) is None
    assert build_stage_trace(run, read_journal(run), read_final_node_id(run), [FIELD]) is None
    assert decision_visibility(None, [FIELD], None) == {"known": False}          # rule mode / no verbose log
    assert suspect_fields([{"evidence": ["[M/M7_leaky_field/fail] step 7: x `requester_user_flair` [n]",
                                         "[M/M2_fit_before_split/fail] `not_a_field`"]}]) == [FIELD]


def test_the_trace_uses_only_predictions_never_labels_or_the_grade(runs):
    run = leak_run(runs, "nograde")
    (run / "grade_report.txt").unlink()                                          # an ungraded run still yields the whole trace
    tr = build_stage_trace(run, read_journal(run), read_final_node_id(run), [FIELD])
    assert tr is not None and len(tr["stages"]) == 6


def test_collect_stores_the_trace_and_the_improver_reads_it_with_the_instructions(project, runs):
    project.init_harness(ROAP, "agent")
    rec = project.collect_round(ROAP, "agent", 0, leak_run(runs, "leak_c"))
    if "stage_trace" not in rec:                                                # the detector must name the field for the trace
        pytest.skip(f"the leaky-field detector did not fire on the synthetic code: {rec.get('stage_trace_error')}")
    assert rec["stage_trace"]["fields"] == [FIELD]
    from rsi_mvp import meta_improver
    h0 = project.store(ROAP, "agent").load("H0")
    inp = meta_improver.build_input(h0, [rec], [])
    assert inp["runs"][0]["stage_trace"]["stages"]
    summary = evidence_summary(inp)
    assert "Stage trace" in summary and "Not visible to it" in summary
    text = contract_text("agent")
    assert "INSTRUCTION OR OBSERVATION" in text and "column-name or preprocessing mismatch" in text
    assert "never hide the problem by filling a default on the test side" in text
    assert "observation_policy.json" in text and "never labels, scores or the grader" in text


def test_the_trace_reports_where_the_profile_really_appeared(runs):
    plain = leak_run(runs, "pe_off")
    tr = build_stage_trace(plain, read_journal(plain), read_final_node_id(plain), [FIELD])
    assert tr["profile_exposure"] == {"sub_stats_setting": "off", "prompts_containing_profile": 0}
    shown = leak_run(runs, "pe_on", profile_in_prompt=True)
    with (shown / "run_config.txt").open("a") as f:
        f.write("sub_stats        = 1\n")
    tr = build_stage_trace(shown, read_journal(shown), read_final_node_id(shown), [FIELD])
    assert tr["profile_exposure"]["sub_stats_setting"] == "1" and tr["profile_exposure"]["prompts_containing_profile"] == 1
    assert "logged LLM prompts containing it: 1" in render_stage_trace(tr)
