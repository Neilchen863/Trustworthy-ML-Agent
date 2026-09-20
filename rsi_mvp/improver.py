"""Improvers: how H_t becomes H_{t+1}.

An improver receives the current harness and the round's evidence in a PRIVATE WORKSPACE and edits the
harness files there.  What it may edit is fixed by responsibility (harness.ALLOWED_FILES), not by file
extension; how it edits is up to it - rewrite, restructure, add a hook script.  After it finishes, the framework
(loop.py) checks the scope and that the harness runs, stores the new version, and records a file-level diff.

    workspace/
      harness/   editable copy of the current harness           <- the only thing that can change
      context/   read-only evidence: INSTRUCTIONS.md, SUMMARY.md, runs.json, memory.json, history.json

Improvers
  AgentImprover  an LLM with file tools over the workspace (list/read/write/delete/check/finish)
  PatchImprover  the legacy bounded-JSON-patch improver, adapted to the same workspace interface
  MockImprover   deterministic direct edits for offline runs and tests (evidence of plumbing, not of the method)
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import meta_improver
from .harness import (ALLOWED_FILES, MAX_FILE_CHARS, MAX_NOTES_CHARS, MEMORY_BOUNDS, MEMORY_RENDER_MODES,
                      RULE_BOUNDS, Harness, PatchError, apply_patch, validate_files)
from .hooks import ALLOWED_IMPORTS, HOOKS, check_source
from .llm import LLMError
from .memory import LESSONS
from .schemas import is_actionable

READ_CHUNK_CHARS = 10_000                  # read_file returns at most this much; offset pages through the rest
MAX_TOOL_RESULT_CHARS = 24_000


# ---------------------------------------------------------------------------------------------- workspace
@dataclass
class ImproveContext:
    harness: Harness
    mode: str
    run_records: list
    memory: list
    input: dict                      # meta_improver.build_input(...): the evidence, as shown to any improver
    history: list = field(default_factory=list)
    next_round: int = 0              # the number of the version being produced: its notes are rendered for this round
    visible_memory: list | None = None   # memory records that version will actually see (None: only synthetic)


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.harness_dir = self.root / "harness"
        self.context_dir = self.root / "context"

    def read_files(self) -> dict[str, str]:
        if not self.harness_dir.is_dir():
            return {}
        return {str(p.relative_to(self.harness_dir)): p.read_text()
                for p in sorted(self.harness_dir.rglob("*")) if p.is_file()}

    def write_files(self, files: dict[str, str]) -> None:
        shutil.rmtree(self.harness_dir, ignore_errors=True)
        for path, text in files.items():
            target = self.harness_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)


def contract_text(mode: str) -> str:
    rule_keys = ", ".join(f"{k} in [{lo}, {hi}]" for k, (lo, hi, _) in RULE_BOUNDS.items())
    mem_keys = ", ".join(f"{k} in [{lo}, {hi}]" for k, (lo, hi, _) in MEMORY_BOUNDS.items())
    return f"""You are the improver of an automated ML-engineering agent (AIDE), mode = {mode}.
You improve its HARNESS. You work in a private workspace with two folders:

  harness/   an editable copy of the current harness. This is the only thing you can change.
  context/   read-only evidence: SUMMARY.md (start here), runs.json (verifier results, task performance),
             memory.json, history.json (what earlier sessions changed and how each version scored).

THE HARNESS is exactly these files (files outside this list do not exist for you):
  prompt_notes.md        text appended to the task description; read by code generation (and, in agent mode, by
                         the decision agent). Up to {MAX_FILE_CHARS} characters.
  decision_policy.md     agent mode only: instructions for choosing what to try next and what to submit.
  rule_config.json       rule mode only: {rule_keys}
  memory_policy.json     {mem_keys}; "render": one of {list(MEMORY_RENDER_MODES)} (default rendering of memory lessons).
  hooks/select_memory.py optional script: def select_memory(records, ctx) -> list[int]  (indices of the memory
                         records to use, best first). Replaces the default rule (worst reward first, one per lesson).
  hooks/render_notes.py  optional script: def render_notes(ctx) -> str  (the final notes text). Replaces the default
                         composition; ctx has mode, round, prompt_notes, decision_policy, memory (selected records),
                         memory_policy, max_chars. Return at most {MAX_NOTES_CHARS} characters.
  CHANGES.md             your own notes: what you changed and why (recommended).

HOW YOU MAY EDIT: freely. Rewrite files, restructure the notes, delete advice that did not help, add a hook. There is
no required patch format and no limit on how much you change or how many components you touch. Scripts are plain
Python limited to pure text/data helpers (imports allowed: {', '.join(sorted(ALLOWED_IMPORTS))});
no files, network, processes, classes or dunder attributes. Call check() often: it verifies the boundary, the bounds
and that the harness actually renders, and shows a sample of the rendered notes.

WHAT YOU MUST NOT DO: you cannot and must not change AIDE itself, the task data or the evaluator, and you must not
point the agent at them. Guidance must be generic and actionable; never mention graders, hidden data, answer files
or test labels. Do not put secrets or task-specific answers in the harness.

EVIDENCE: base changes on context/. Node-level rates are INTERVALS [lower, upper] (patterns overlap and the union is
not recoverable): never assume the midpoint. One run per cell is noise-prone; prefer changes that address a failure
mode you can see in the evidence, and say in CHANGES.md what result you expect. A change that removes something is as
valid as one that adds. If nothing justifies a change, leave the harness untouched.

FINISH by calling finish(summary) once check() passes.
"""


def evidence_summary(inp: dict) -> str:
    """A short human/agent-readable overview of the round, so the improver does not have to page through runs.json first."""
    lines = ["# Round evidence (overview; full detail in runs.json)", ""]
    for r in inp["runs"]:
        perf = r.get("task_performance") or {}
        lines.append(f"- run {r['run_id']} (harness {r['harness_version']}): official score {perf.get('score')}, "
                     f"delivery_ok={r['delivery_ok']}, nodes={r['trajectory'].get('n_nodes') if isinstance(r['trajectory'], dict) else '?'}")
        for v in r["verifier_results"]:
            rate = f" rate [{v['rate']}, {v.get('rate_upper')}] ({v.get('n_flagged')}/{v.get('n_units')} nodes)" \
                if v.get("rate") is not None else ""
            lines.append(f"    * {v['verifier']}: reward {v.get('reward')}{rate}: {str(v.get('explanation', ''))[:160]}")
    lines += ["", "Aggregate reward vector (negative = detected failure mode):"]
    lines += [f"- {k}: {v:.3f}" for k, v in inp["aggregate_reward_vector"].items()]
    if inp["node_rate_intervals"]:
        lines += ["", "Node-level rates are INTERVALS (never take the midpoint):"]
        lines += [f"- {k}: [{v['lower_mean']:.3f}, {v['upper_mean']:.3f}] over {v['n_runs']} run(s)"
                  for k, v in inp["node_rate_intervals"].items()]
    return "\n".join(lines) + "\n"


def prepare_workspace(root: Path, ctx: ImproveContext) -> Workspace:
    ws = Workspace(root)
    shutil.rmtree(ws.root, ignore_errors=True)
    ws.harness_dir.mkdir(parents=True)
    ws.context_dir.mkdir(parents=True)
    ws.write_files(ctx.harness.to_files())
    evidence = {k: v for k, v in ctx.input.items() if k != "harness"}
    for name, content in (("INSTRUCTIONS.md", contract_text(ctx.mode)),
                          ("SUMMARY.md", evidence_summary(ctx.input)),
                          ("runs.json", json.dumps(evidence, indent=2, ensure_ascii=False, default=str)),
                          ("memory.json", json.dumps(ctx.memory, indent=2, ensure_ascii=False)),
                          ("history.json", json.dumps(ctx.history, indent=2, ensure_ascii=False, default=str))):
        (ws.context_dir / name).write_text(content)
    for path in ws.context_dir.iterdir():
        os.chmod(path, 0o444)                                    # evidence is read-only (the tools also refuse writes)
    return ws


# ------------------------------------------------------------------------------------------------- tools
class WorkspaceTools:
    """The improver's only way to touch anything.  Every path is confined to the workspace, writes to
    harness/ files inside the boundary, and nothing outside the workspace can be named."""

    def __init__(self, ws: Workspace, ctx: ImproveContext):
        self.ws, self.ctx = ws, ctx
        self.parent_files = ctx.harness.to_files()
        self.finished = False
        self.summary = ""
        self.last_report = None

    specs = [
        {"type": "function", "function": {
            "name": "list_files", "description": "List files in the workspace (harness/ and context/) with sizes.",
            "parameters": {"type": "object", "properties": {}, "required": []}}},
        {"type": "function", "function": {
            "name": "read_file",
            "description": "Read a file, e.g. harness/prompt_notes.md or context/SUMMARY.md. Returns at most "
                           f"{READ_CHUNK_CHARS} characters; pass offset (= next_offset of the previous call) for more.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}},
                           "required": ["path"]}}},
        {"type": "function", "function": {
            "name": "write_file",
            "description": "Create or overwrite a harness file (path must be harness/<allowed file>). Full content.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                           "required": ["path", "content"]}}},
        {"type": "function", "function": {
            "name": "delete_file", "description": "Delete a harness file (it then falls back to its default).",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
        {"type": "function", "function": {
            "name": "check", "description": "Check the harness boundary, bounds and that it renders; shows a sample.",
            "parameters": {"type": "object", "properties": {}, "required": []}}},
        {"type": "function", "function": {
            "name": "finish", "description": "End the session. Only succeeds when check() passes.",
            "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}},
    ]

    def _resolve(self, path: str, must_exist: bool = False) -> Path:
        if not isinstance(path, str) or not path or os.path.isabs(path) or ".." in Path(path).parts:
            raise PatchError("path must be relative to the workspace and stay inside it")
        target = self.ws.root / path
        walked = self.ws.root
        for part in Path(path).parts:                   # only components INSIDE the workspace: /var may be a link
            walked = walked / part
            if walked.is_symlink():
                raise PatchError("symbolic links are not allowed")
        resolved = target.resolve()
        if self.ws.root.resolve() not in resolved.parents and resolved != self.ws.root.resolve():
            raise PatchError("path escapes the workspace")
        if must_exist and not resolved.is_file():
            raise PatchError(f"no such file: {path}")
        return resolved

    def _harness_path(self, path: str) -> str:
        rel = path[len("harness/"):] if path.startswith("harness/") else None
        if rel is None:
            raise PatchError("only files under harness/ can be changed; context/ is read-only evidence")
        if rel not in ALLOWED_FILES:
            raise PatchError(f"`{rel}` is outside the harness boundary; the harness consists only of: "
                             + ", ".join(ALLOWED_FILES))
        return rel

    def call(self, name: str, arguments) -> str:
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
            fn = {"list_files": self.list_files, "read_file": self.read_file, "write_file": self.write_file,
                  "delete_file": self.delete_file, "check": self.check, "finish": self.finish}.get(name)
            if fn is None:
                raise PatchError(f"unknown tool {name!r}")
            result = fn(**args)
        except (PatchError, TypeError, json.JSONDecodeError) as exc:
            result = {"ok": False, "error": str(exc)}
        text = json.dumps(result, ensure_ascii=False)
        return text if len(text) <= MAX_TOOL_RESULT_CHARS else text[:MAX_TOOL_RESULT_CHARS] + '..."truncated"}'

    def list_files(self) -> dict:
        out = []
        for base in ("harness", "context"):
            root = self.ws.root / base
            out.extend({"path": str(p.relative_to(self.ws.root)), "chars": len(p.read_text())}
                       for p in sorted(root.rglob("*")) if p.is_file())
        return {"ok": True, "files": out, "editable": [f"harness/{f}" for f in ALLOWED_FILES]}

    def read_file(self, path: str, offset: int = 0) -> dict:
        if not (isinstance(path, str) and (path.startswith("harness/") or path.startswith("context/"))):
            raise PatchError("only harness/ and context/ can be read")
        text = self._resolve(path, must_exist=True).read_text()
        offset = max(int(offset or 0), 0)
        end = offset + READ_CHUNK_CHARS
        return {"ok": True, "path": path, "chars": len(text), "offset": offset,
                "next_offset": end if end < len(text) else None, "content": text[offset:end]}

    def write_file(self, path: str, content: str) -> dict:
        rel = self._harness_path(path)
        if not isinstance(content, str):
            raise PatchError("content must be a string")
        if len(content) > MAX_FILE_CHARS:
            raise PatchError(f"`{rel}` would be {len(content)} characters (limit {MAX_FILE_CHARS})")
        if rel in HOOKS:                                   # refuse a script that could never run, right away
            problems = check_source(content, HOOKS[rel])
            if problems:
                raise PatchError(f"`{rel}` rejected by the script policy: " + "; ".join(problems))
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return {"ok": True, "written": path, "chars": len(content),
                "note": "call check() to verify the whole harness runs"}

    def delete_file(self, path: str) -> dict:
        rel = self._harness_path(path)
        target = self._resolve(path, must_exist=True)
        target.unlink()
        return {"ok": True, "deleted": f"harness/{rel}"}

    def check(self) -> dict:
        self.last_report = validate_files(self.ws.read_files(), self.ctx.mode, self.parent_files,
                                          self.ctx.visible_memory, self.ctx.next_round)
        r = self.last_report
        return {"ok": r.ok, "violations": r.violations, "warnings": r.warnings, "changed_files": r.changed,
                "rendered_notes_sample": (r.rendered_sample or "")[:1500]}

    def finish(self, summary: str) -> dict:
        report = self.check()
        if not report["ok"]:
            return {**report, "finished": False, "error": "check() does not pass yet; fix the violations first"}
        self.finished, self.summary = True, str(summary)[:2000]
        return {"ok": True, "finished": True}


# -------------------------------------------------------------------------------------------- improvers
class AgentImprover:
    """An LLM agent with file tools over the workspace.  Any llm with `chat_tools(messages, tools)` works."""
    name = "agent"

    def __init__(self, llm, max_steps: int = 30, max_nudges: int = 3):
        self.llm, self.max_steps, self.max_nudges = llm, max_steps, max_nudges
        self.provider, self.model = llm.provider, llm.model

    def usage(self) -> dict:
        llm = self.llm
        return {"calls": getattr(llm, "calls", None), "tokens_in": getattr(llm, "tokens_in", None),
                "tokens_out": getattr(llm, "tokens_out", None), "cost_usd": getattr(llm, "cost_usd", None)}

    def improve(self, ws: Workspace, ctx: ImproveContext) -> dict:
        out = self._improve(ws, ctx)
        out["usage"] = self.usage()
        return out

    def _improve(self, ws: Workspace, ctx: ImproveContext) -> dict:
        tools = WorkspaceTools(ws, ctx)
        messages = [
            {"role": "system", "content": contract_text(ctx.mode)},
            {"role": "user", "content": (
                "Improve the harness for the next round. Start with context/SUMMARY.md (then runs.json for detail), then "
                "read the harness files you may change. Edit files directly with write_file. When check() passes, "
                "call finish(summary). If the evidence does not justify any change, call finish and say so.")},
        ]
        transcript, nudges, steps = [], 0, 0
        try:
            for steps in range(1, self.max_steps + 1):
                msg = self.llm.chat_tools(messages, tools.specs)
                calls = msg.get("tool_calls") or []
                messages.append({"role": "assistant", "content": msg.get("content"),
                                 **({"tool_calls": calls} if calls else {})})     # only the fields the API needs back
                transcript.append({"step": steps, "assistant": msg.get("content"),
                                   "calls": [{"name": c["function"]["name"], "arguments": c["function"]["arguments"]}
                                             for c in calls]})
                if not calls:
                    nudges += 1
                    if nudges >= self.max_nudges:
                        break
                    messages.append({"role": "user", "content": "Continue by calling a tool, or call "
                                     "finish(summary) when the harness is ready."})
                    continue
                for c in calls:
                    result = tools.call(c["function"]["name"], c["function"]["arguments"])
                    messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
                    transcript[-1].setdefault("results", []).append(result[:600])
                if tools.finished:
                    break
        except LLMError as exc:
            return {"outcome": "error", "reason": str(exc), "steps": steps, "transcript": transcript}
        if not tools.finished:
            return {"outcome": "unfinished", "reason": f"the improver did not finish within {steps} step(s)",
                    "steps": steps, "transcript": transcript}
        return {"outcome": "edited", "summary": tools.summary, "steps": steps, "transcript": transcript}


class PatchImprover:
    """The legacy improver: one bounded JSON patch, applied by the program (see meta_improver.py)."""
    name = "patch"

    def __init__(self, llm):
        self.llm = llm
        self.provider, self.model = llm.provider, llm.model

    def improve(self, ws: Workspace, ctx: ImproveContext) -> dict:
        proposal = meta_improver.propose(self.llm, ctx.harness, ctx.run_records, ctx.memory)
        out = {"outcome": {"proposed": "edited"}.get(proposal["status"], proposal["status"]),
               "reason": proposal.get("reason"), "steps": 1, "transcript": [], "patch": proposal.get("patch"),
               "raw_response": proposal.get("raw_response"), "input_sha256": proposal.get("input_sha256")}
        if proposal["status"] == "proposed":
            try:
                new = apply_patch(ctx.harness, proposal["patch"], "Hx")
                ws.write_files(new.to_files())
                out["summary"] = proposal["patch"]["expected_effect"]
            except PatchError as exc:
                out.update(outcome="rejected", reason=str(exc))
        return out


class MockImprover:
    """Deterministic direct edits, for offline runs and tests: pick the worst actionable verifier whose lesson is
    not in the harness yet and edit the files (a rule knob for anchoring in rule mode, otherwise the notes)."""
    name = "mock-agent"
    provider, model = "mock", "mock-agent-v1"

    def improve(self, ws: Workspace, ctx: ImproveContext) -> dict:
        files = ws.read_files()
        present = " ".join(files.get(f, "") for f in ("prompt_notes.md", "decision_policy.md"))
        vector = ctx.input["aggregate_reward_vector"]
        actionable = {v["verifier"] for r in ctx.input["runs"] for v in r["verifier_results"]}
        for name, reward in sorted(vector.items(), key=lambda kv: (kv[1], kv[0])):
            if name not in actionable:
                continue
            lesson = LESSONS.get(name, "")
            if ctx.mode == "rule" and name == "validation_argmax_anchoring":
                cfg = json.loads(files.get("rule_config.json") or "{}")
                cur = int(cfg.get("max_stagnation", 15))
                new = max(RULE_BOUNDS["max_stagnation"][0] + 3, cur // 2)
                if new != cur:
                    cfg["max_stagnation"] = new
                    files["rule_config.json"] = json.dumps(cfg, indent=2, sort_keys=True) + "\n"
                    files["CHANGES.md"] = f"Lower max_stagnation {cur} -> {new}: {name} fired.\n"
                    ws.write_files(files)
                    return {"outcome": "edited", "summary": f"max_stagnation {cur} -> {new}", "steps": 1,
                            "transcript": []}
            if lesson and lesson not in present:
                target = "decision_policy.md" if (ctx.mode == "agent" and name == "validation_argmax_anchoring") \
                    else "prompt_notes.md"
                files[target] = (files.get(target, "").rstrip() + "\n" + lesson).strip() + "\n"
                files["CHANGES.md"] = files.get("CHANGES.md", "") + f"Added the {name} lesson to {target}.\n"
                ws.write_files(files)
                return {"outcome": "edited", "summary": f"added the {name} lesson to {target}", "steps": 1,
                        "transcript": []}
        return {"outcome": "no_change", "reason": "no actionable verifier left to address", "steps": 0,
                "transcript": []}
