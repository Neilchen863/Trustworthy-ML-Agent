"""Tooling for a small, cost-capped real run: the decision-prompt fix, the cost watchdog, the improver cost cap."""
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    spec = importlib.util.spec_from_file_location(Path(path).stem, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


fix = load("patches/apply_agent_decision_fix.py")
dog = load("tools/cost_watchdog.py")

BUGGY_MODULE = '''
import json

def _ordered_selection_prompt(base, channel_sections):
    """Render v2 evidence channels in the order declared by ContextPolicy."""
    if not _selection_harness_v2():
        out = dict(base)
        out.update(channel_sections)
        return out
    return dict(base)
'''


def run_function(text, sections):
    ns = {"_selection_harness_v2": lambda: False}
    exec(text, ns)
    return ns["_ordered_selection_prompt"]({"Introduction": "i"}, sections)


# ------------------------------------------------------------------------------- decision prompt fix
def test_the_fix_turns_tuple_lists_into_title_to_text_and_is_idempotent():
    sections = {"tree": [("Solution tree", "T")], "mem": [("Memory", "M"), ("Extra", "E")]}
    before = run_function(BUGGY_MODULE, sections)
    assert not all(isinstance(v, str) for v in before.values())          # the bug: list values reach the renderer
    new, status = fix.patch_text(BUGGY_MODULE)
    assert status == "patched"
    assert run_function(new, sections) == {"Introduction": "i", "Solution tree": "T", "Memory": "M", "Extra": "E"}
    assert fix.patch_text(new) == (new, "already_patched")


def test_the_fix_refuses_a_file_where_the_block_is_missing_or_duplicated():
    assert fix.patch_text("def unrelated(): pass\n")[1] == "not_applicable"
    assert fix.patch_text(BUGGY_MODULE + BUGGY_MODULE)[1] == "not_applicable"


def test_the_fix_edits_exactly_one_file_of_a_package(tmp_path):
    pkg = tmp_path / "aide"
    pkg.mkdir()
    (pkg / "other.py").write_text("x = 1\n")
    (pkg / "decision.py").write_text(BUGGY_MODULE)
    assert fix.main(["--check", str(pkg)]) == 1 and "out.update" in (pkg / "decision.py").read_text()   # --check: no edit
    assert fix.main([str(pkg)]) == 0 and "setdefault" in (pkg / "decision.py").read_text()
    assert fix.main([str(pkg)]) == 0                                                                   # idempotent
    (pkg / "second.py").write_text(BUGGY_MODULE)                                                       # two definitions
    assert fix.main([str(pkg)]) == 2


@pytest.mark.skipif(not (ROOT.parent / "scripts" / "_inject_agent_decision.py").is_file(),
                    reason="research repo not next to this checkout")
def test_the_fix_applies_to_the_real_injector_source():
    src = (ROOT.parent / "scripts" / "_inject_agent_decision.py").read_text()
    new, status = fix.patch_text(src)
    assert status in ("patched", "already_patched")


# ------------------------------------------------------------------------------------- cost watchdog
def test_watchdog_prices_by_model_and_treats_unknown_models_as_expensive():
    rows = [{"model": "gpt-4o-2024-08-06", "in": 1_000_000, "out": 100_000},
            {"model": "gpt-4o-mini-2024-07-18", "in": 1_000_000, "out": 0}]
    assert dog.cost_of(rows) == pytest.approx(2.5 + 1.0 + 0.15)
    assert dog.cost_of([{"model": "mystery-model", "in": 1_000_000, "out": 0}]) == pytest.approx(15.0)


def test_watchdog_reads_a_growing_log_and_ignores_a_half_written_line(tmp_path):
    log = tmp_path / "token_usage.jsonl"
    log.write_text(json.dumps({"model": "gpt-4o", "in": 400_000, "out": 0}) + "\n" + '{"model": "gpt-4o", "in": 1')
    assert dog.cost_of(dog.read_rows(str(log))) == pytest.approx(1.0)
    assert dog.read_rows(str(tmp_path / "missing.jsonl")) == []


def test_watchdog_finds_the_run_directory_of_a_job(tmp_path):
    (tmp_path / "runs" / "comp" / "20260920_x_j123" / "logs").mkdir(parents=True)
    assert dog.run_dir_for(str(tmp_path), "comp", "123").endswith("_j123")
    assert dog.run_dir_for(str(tmp_path), "comp", "999") is None


# ---------------------------------------------------------------------------------- improver cost cap
class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_openai_chat_counts_tokens_and_stops_at_its_cost_cap(monkeypatch):
    from rsi_mvp import llm as llm_mod
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "hi"}}],
                       "usage": {"prompt_tokens": 100_000, "completion_tokens": 10_000}}).encode()
    sent = []
    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", lambda req, timeout=0: sent.append(1) or FakeResp(body))
    chat = llm_mod.OpenAIChat(model="gpt-4o-2024-08-06", max_cost_usd=0.5)
    chat.chat_tools([{"role": "user", "content": "x"}], [])           # $0.25 + $0.10 = $0.35
    assert chat.calls == 1 and chat.tokens_in == 100_000 and chat.cost_usd == pytest.approx(0.35)
    chat.chat_tools([{"role": "user", "content": "x"}], [])           # now $0.70 >= cap
    with pytest.raises(llm_mod.LLMError, match="cost cap"):
        chat.chat_tools([{"role": "user", "content": "x"}], [])
    assert len(sent) == 2                                             # the third call was never sent


def test_an_improver_stopped_by_the_cost_cap_is_recorded_as_error_not_a_new_version(tmp_path, tasks_dir, state, runs):
    from conftest import AGENT_LOG, make_run
    from rsi_mvp.improver import AgentImprover
    from rsi_mvp.loop import RsiProject

    class Capped:
        provider, model, calls, tokens_in, tokens_out, cost_usd = "test", "t", 1, 5, 5, 0.6

        def chat_tools(self, messages, tools):
            from rsi_mvp.llm import LLMError
            raise LLMError("cost cap reached: $0.600 >= $0.50; not sending another call")

    p = RsiProject(tasks_dir, state)
    p.init_harness("random_acts_of_pizza", "agent")
    p.collect_round("random_acts_of_pizza", "agent", 0, make_run(
        runs, name="r0", agent_log=AGENT_LOG, nodes=[("a", 1, None, 0.6, "b"), ("b", 2, None, 1.0, "p")], final="b"))
    res = p.improve("random_acts_of_pizza", "agent", 0, AgentImprover(Capped()))
    assert res["status"] == "error" and "cost cap" in res["reason"] and res["usage"]["cost_usd"] == 0.6
    assert p.store("random_acts_of_pizza", "agent").versions() == ["H0"]
