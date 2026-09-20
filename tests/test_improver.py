"""The improver edits harness FILES directly; the framework checks scope + runnability afterwards."""
import json
import os

import pytest

from conftest import AGENT_LOG, make_run
from rsi_mvp import hooks, meta_improver
from rsi_mvp.harness import ALLOWED_FILES, validate_files
from rsi_mvp.improver import AgentImprover, ImproveContext, MockImprover, WorkspaceTools, prepare_workspace
from rsi_mvp.llm import MockMetaLLM, ScriptedToolLLM
from rsi_mvp.loop import RsiProject
from rsi_mvp.runner import notes_variant

PHANTOM = [("a", 1, None, 0.60, "base"), ("b", 2, None, 1.0, "perfect score"), ("c", 3, "a", 0.65, "tuned")]
ROAP = "random_acts_of_pizza"

RENDER_HOOK = '''
def render_notes(ctx):
    parts = [ctx["prompt_notes"].strip()]
    lessons = [m["lesson"] for m in ctx["memory"]][:2]
    if lessons:
        parts.append("Checklist:\\n" + "\\n".join("- " + l for l in lessons))
    return "\\n\\n".join(p for p in parts if p)
'''
SELECT_HOOK = '''
def select_memory(records, ctx):
    order = sorted(range(len(records)), key=lambda i: records[i]["reward"])
    return order[:1]
'''


@pytest.fixture
def project(tasks_dir, state):
    return RsiProject(tasks_dir, state)


def collect_round0(project, runs, mode="agent", memory_render="none"):
    project.init_harness(ROAP, mode, memory_render=memory_render)
    project.collect_round(ROAP, mode, 0, make_run(runs, name="r0", mode=mode,
                                                  agent_log=AGENT_LOG if mode == "agent" else None,
                                                  nodes=PHANTOM, final="b", grade=0.5))


def edit(*writes, finish="done"):
    """A scripted improver session: write the given (path, content) files, check, finish."""
    turns = [[("write_file", {"path": p, "content": c})] for p, c in writes]
    turns += [[("check", {})], [("finish", {"summary": finish})]]
    return AgentImprover(ScriptedToolLLM(turns))


def tools_for(project, mode="agent"):
    """A workspace + tools over the round-0 evidence of the project."""
    rdir = project._round_dir(0, ROAP, mode)
    records = [json.loads(p.read_text()) for p in sorted(rdir.glob("*/run_record.json"))]
    h0 = project.store(ROAP, mode).load("H0")
    ctx = ImproveContext(h0, mode, records, [], meta_improver.build_input(h0, records, []), [])
    return WorkspaceTools(prepare_workspace(rdir / "ws_test", ctx), ctx)


# ------------------------------------------------------------------------------- direct editing end to end
def test_agent_edits_files_directly_and_the_framework_commits_a_new_version(project, runs):
    collect_round0(project, runs)
    res = project.improve(ROAP, "agent", 0, edit(
        ("harness/prompt_notes.md", "Report the fold-wise mean, not the last fold.\n"),
        ("harness/decision_policy.md", "Prefer the candidate with the best cross-validated score.\n"),
        ("harness/CHANGES.md", "Rewrote notes and policy.\n"), finish="rewrote two components"))
    assert res["status"] == "proposed" and res["new_version"] == "H1" and res["improver"] == "agent"
    assert res["changed_files"] == ["CHANGES.md", "decision_policy.md", "prompt_notes.md"]   # two components at once
    h1 = project.store(ROAP, "agent").load("H1")
    assert h1.prompt_notes.startswith("Report the fold-wise") and "cross-validated" in h1.decision_policy_text
    vdir = project.store(ROAP, "agent").version_dir("H1")
    assert "--- H0/harness/prompt_notes.md" in (vdir / "diff.patch").read_text()          # the diff is a record
    rec = json.loads((vdir / "improver_record.json").read_text())
    assert rec["summary"] == "rewrote two components" and rec["transcript"] and rec["scope"]["ok"]
    assert not (vdir / "patch.json").exists()                                            # no prescribed patch
    assert "Report the fold-wise" in project.render_notes(ROAP, "agent", "H1")


def test_edits_are_not_limited_to_one_component_or_a_few_hundred_characters(project, runs):
    collect_round0(project, runs)
    big = "Guidance line.\n" * 500                                                        # 7500 chars
    res = project.improve(ROAP, "agent", 0, edit(("harness/prompt_notes.md", big)))
    assert res["status"] == "proposed" and len(project.store(ROAP, "agent").load("H1").prompt_notes) == len(big)


def test_the_improver_can_restructure_context_with_a_hook_script(project, runs):
    collect_round0(project, runs)
    res = project.improve(ROAP, "agent", 0, edit(
        ("harness/prompt_notes.md", "Be careful with validation.\n"),
        ("harness/hooks/render_notes.py", RENDER_HOOK),
        ("harness/hooks/select_memory.py", SELECT_HOOK)))
    assert res["status"] == "proposed", res
    notes = project.render_notes(ROAP, "agent", "H1")
    assert notes.startswith("Be careful with validation.") and "Checklist:" in notes      # memory used via the hook
    assert "hooks/render_notes.py" in res["changed_files"]


# --------------------------------------------------------------------------- the boundary
def test_only_harness_files_can_be_written_and_nothing_outside_the_workspace_can_be_named(project, runs):
    collect_round0(project, runs)
    tools = tools_for(project)
    for bad in ("harness/aide/agent.py", "harness/../context/runs.json", "context/runs.json", "/etc/passwd",
                "harness/data/train.csv", "harness/evaluator.py"):
        assert json.loads(tools.call("write_file", {"path": bad, "content": "x"}))["ok"] is False, bad
    assert set(json.loads(tools.call("list_files", {}))["editable"]) == {f"harness/{f}" for f in ALLOWED_FILES}


def test_reads_are_confined_and_context_is_read_only(project, runs):
    collect_round0(project, runs)
    tools = tools_for(project)
    assert json.loads(tools.call("read_file", {"path": "context/runs.json"}))["ok"]
    for bad in ("../state/rounds/exposure_ledger.jsonl", "/etc/passwd", "harness/../../x"):
        assert json.loads(tools.call("read_file", {"path": bad}))["ok"] is False
    assert not os.access(tools.ws.context_dir / "runs.json", os.W_OK)


def test_files_the_improver_drops_on_disk_outside_the_boundary_are_rejected_by_the_framework(project, runs):
    class Sneaky:
        name, provider, model = "sneaky", "test", "t"

        def improve(self, ws, ctx):
            (ws.harness_dir / "prompt_notes.md").write_text("fine\n")
            (ws.harness_dir / "aide_patch.py").write_text("print('patched AIDE')\n")     # not a harness file
            return {"outcome": "edited", "summary": "s", "steps": 1, "transcript": []}

    collect_round0(project, runs)
    res = project.improve(ROAP, "agent", 0, Sneaky())
    assert res["status"] == "rejected" and "outside the harness boundary" in res["reason"]
    assert project.store(ROAP, "agent").versions() == ["H0"]


def test_an_improver_that_rewrites_the_evidence_is_rejected(project, runs):
    class Tamper:
        name, provider, model = "tamper", "test", "t"

        def improve(self, ws, ctx):
            (ws.harness_dir / "prompt_notes.md").write_text("fine\n")
            os.chmod(ws.context_dir / "runs.json", 0o644)
            (ws.context_dir / "runs.json").write_text("{}")
            return {"outcome": "edited", "summary": "s", "steps": 1, "transcript": []}

    collect_round0(project, runs)
    res = project.improve(ROAP, "agent", 0, Tamper())
    assert res["status"] == "rejected" and "context/" in res["reason"]


def test_bounds_and_forbidden_guidance_still_apply_to_direct_edits(project, runs):
    collect_round0(project, runs, mode="rule")
    res = project.improve(ROAP, "rule", 0, edit(("harness/rule_config.json", '{"max_stagnation": 9999}\n')))
    assert res["status"] == "rejected" and project.store(ROAP, "rule").versions() == ["H0"]
    report = validate_files({"prompt_notes.md": "Look at answers.csv for hints"}, "agent")
    assert not report.ok and "answers.csv" in report.violations[0]


# --------------------------------------------------------------------------- hook scripts and runnability
def test_a_broken_hook_is_refused_at_write_time(project, runs):
    collect_round0(project, runs)
    tools = tools_for(project)
    for src, why in (("import os\ndef render_notes(ctx):\n    return os.getcwd()\n", "not allowed"),
                     ("def render_notes(ctx):\n    return open('/etc/passwd').read()\n", "not allowed"),
                     ("def render_notes(ctx):\n    return ().__class__\n", "not allowed"),
                     ("def wrong_name(ctx):\n    return ''\n", "must define")):
        out = json.loads(tools.call("write_file", {"path": "harness/hooks/render_notes.py", "content": src}))
        assert out["ok"] is False and why in out["error"], src


def test_a_hook_that_loops_forever_fails_the_runnability_check_and_finish_refuses(project, runs, monkeypatch):
    monkeypatch.setattr(hooks, "TIMEOUT_SECS", 2)
    monkeypatch.setattr(hooks.run_hook, "__defaults__", (2,))
    collect_round0(project, runs)
    tools = tools_for(project)
    loop = "def render_notes(ctx):\n    while True:\n        pass\n"
    assert json.loads(tools.call("write_file", {"path": "harness/hooks/render_notes.py", "content": loop}))["ok"]
    chk = json.loads(tools.call("check", {}))
    assert chk["ok"] is False and any("does not run" in v for v in chk["violations"])
    fin = json.loads(tools.call("finish", {"summary": "x"}))
    assert fin["ok"] is False and not tools.finished


def test_the_hook_sandbox_has_no_files_network_or_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    assert hooks.run_hook("def render_notes(ctx):\n    return 'x'\n", "render_notes", [{}]) == "x"
    for bad in ("import os\ndef render_notes(ctx):\n    return os.environ['OPENAI_API_KEY']\n",
                "def render_notes(ctx):\n    return __import__('os').environ\n"):
        with pytest.raises(hooks.HookError):
            hooks.run_hook(bad, "render_notes", [{}])


# ------------------------------------------------------------------------------------------ outcomes
def test_an_agent_that_never_finishes_creates_no_version(project, runs):
    collect_round0(project, runs)
    turns = [[("write_file", {"path": "harness/prompt_notes.md", "content": "text\n"})]] + ["still thinking"] * 5
    res = project.improve(ROAP, "agent", 0, AgentImprover(ScriptedToolLLM(turns), max_steps=4))
    assert res["status"] == "rejected" and res["new_version"] is None
    assert project.store(ROAP, "agent").versions() == ["H0"]


def test_finishing_without_a_material_change_is_no_change_not_a_new_version(project, runs):
    collect_round0(project, runs)
    res = project.improve(ROAP, "agent", 0, edit(("harness/CHANGES.md", "looked, nothing to do\n")))
    assert res["status"] == "no_change" and project.store(ROAP, "agent").versions() == ["H0"]
    res = project.improve(ROAP, "agent", 0, AgentImprover(ScriptedToolLLM([[("finish", {"summary": "no change"})]])))
    assert res["status"] == "no_change"


def test_an_improver_crash_is_recorded_not_raised(project, runs):
    class Boom:
        name, provider, model = "boom", "test", "t"

        def improve(self, ws, ctx):
            raise KeyError("kaboom")

    collect_round0(project, runs)
    res = project.improve(ROAP, "agent", 0, Boom())
    assert res["status"] == "error" and "kaboom" in res["reason"]
    assert (project._round_dir(0, ROAP, "agent") / "meta_improver.json").is_file()


def test_switching_memory_rendering_on_is_allowed_but_flagged_as_a_confound(project, runs):
    collect_round0(project, runs)
    res = project.improve(ROAP, "agent", 0, edit(
        ("harness/memory_policy.json", '{"max_records": 3, "max_chars": 800, "render": "lessons"}\n')))
    assert res["status"] == "proposed" and any("confounded" in w for w in res["warnings"])


# ------------------------------------------------------------------------ history, memory, freeze, legacy
def test_the_improver_sees_earlier_sessions_and_scores_in_history(project, runs):
    collect_round0(project, runs)
    project.improve(ROAP, "agent", 0, edit(("harness/prompt_notes.md", "Verify the output.\n"),
                                           finish="added a verify note"))
    notes = project.render_notes(ROAP, "agent", "H1")
    project.collect_round(ROAP, "agent", 1, make_run(runs, name="r1", mode="agent", agent_log=AGENT_LOG, nodes=PHANTOM,
                                                     final="b", grade=0.55, variant=notes_variant(notes), notes=notes))
    seen = {}

    class Peek:
        name, provider, model = "peek", "test", "t"

        def improve(self, ws, ctx):
            seen["history"] = json.loads((ws.context_dir / "history.json").read_text())
            return {"outcome": "no_change", "reason": "peeked", "steps": 0, "transcript": []}

    project.improve(ROAP, "agent", 1, Peek())
    (h,) = seen["history"]
    assert h["round"] == 0 and h["summary"] == "added a verify note" and h["new_version"] == "H1"
    assert h["score_of_from_version"]["score_mean"] == 0.5


def test_memory_visible_to_a_version_does_not_change_while_replicates_are_collected(project, runs):
    """H1 may use only memory written before it existed: notes at plan time == notes at collect time."""
    collect_round0(project, runs, memory_render="lessons")
    project.improve(ROAP, "agent", 0, edit(("harness/prompt_notes.md", "x\n")))
    before = project.render_notes(ROAP, "agent", "H1")
    project.collect_round(ROAP, "agent", 1, make_run(runs, name="r1", mode="agent", agent_log=AGENT_LOG, nodes=PHANTOM,
                                                     final="b", variant=notes_variant(before), notes=before),
                          replicate=1)
    assert any(r["round"] == 1 for r in project.memory(ROAP, "agent").all()), "memory grew during round 1"
    assert project.render_notes(ROAP, "agent", "H1") == before


def test_freeze_snapshots_the_memory_a_hooked_harness_uses(project, runs):
    collect_round0(project, runs)
    project.improve(ROAP, "agent", 0, edit(("harness/hooks/render_notes.py", RENDER_HOOK),
                                           ("harness/prompt_notes.md", "Be careful.\n")))
    live = project.render_notes(ROAP, "agent", "H1")
    info = project.freeze(ROAP, "agent")
    assert info["memory_records"] and info["frozen_notes_sha256"]
    project.memory(ROAP, "agent").append([{"verifier": "x", "reward": -1, "lesson": "LATE", "situation": "s",
                                           "evidence": "e", "decision_outcome": "o", "round": 0,
                                           "harness_version": "H0", "run_id": "z"}])
    assert project.render_notes(ROAP, "agent", "H1") == live and "LATE" not in live


def test_legacy_patch_improver_still_works_through_the_same_workspace_check(project, runs):
    collect_round0(project, runs)
    res = project.improve(ROAP, "agent", 0, MockMetaLLM())
    assert res["status"] == "proposed" and res["improver"] == "patch" and res["patch"]["target_component"]
    vdir = project.store(ROAP, "agent").version_dir("H1")
    assert (vdir / "patch.json").is_file() and (vdir / "diff.patch").is_file()


def test_the_mock_agent_improver_edits_files_offline(project, runs):
    collect_round0(project, runs)
    res = project.improve(ROAP, "agent", 0, MockImprover())
    assert res["status"] == "proposed" and res["provider"] == "mock"
    assert "prompt_notes.md" in res["changed_files"] or "decision_policy.md" in res["changed_files"]
    assert not (project.store(ROAP, "agent").version_dir("H1") / "patch.json").exists()


def test_the_agent_loop_reports_its_transcript(project, runs):
    collect_round0(project, runs)
    res = project.improve(ROAP, "agent", 0, edit(("harness/prompt_notes.md", "Note.\n")))
    names = [c["name"] for step in res["transcript"] for c in step["calls"]]
    assert names == ["write_file", "check", "finish"] and res["steps"] == 3
