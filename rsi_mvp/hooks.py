"""Harness-layer hook scripts: static policy + sandboxed execution.

The improver agent may edit *scripts* that implement harness responsibilities - how memory is retrieved
(`hooks/select_memory.py`) and how the context that reaches the agent is organised
(`hooks/render_notes.py`).  Editing them is still editing the harness, but they are code that the RSI
framework executes on the host, so they run under three layers:

  1. a static policy (AST) checked when a file is written and again before every run: only pure text/data
     helpers may be imported, no file/process/network access, no dunder tricks, no classes;
  2. a fresh interpreter (`python -I`), cleared environment (no API key), a wall-clock timeout, CPU and memory
     limits, capped output;
  3. the hook receives all its data as arguments and can only return text / indices, so it cannot write
     anywhere.

This is defence in depth against accidents by our own improver, NOT a security boundary against a determined
adversary (Python cannot offer one).  The RSI framework never gives hooks the AIDE source, task data or the
evaluator, and never runs a hook with the credentials of the experiment.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ALLOWED_IMPORTS = frozenset({"json", "re", "math", "textwrap", "collections", "itertools", "statistics", "string"})
FORBIDDEN_NAMES = frozenset({
    "open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "getattr", "setattr",
    "delattr", "input", "breakpoint", "memoryview", "help", "exit", "quit", "classmethod", "staticmethod",
    "property", "super", "type", "object",
})
HOOKS = {
    "hooks/render_notes.py": "render_notes",     # (ctx: dict) -> str
    "hooks/select_memory.py": "select_memory",   # (records: list[dict], ctx: dict) -> list[int]
}
TIMEOUT_SECS = 10
MAX_OUTPUT_CHARS = 60_000
_ROOT = Path(__file__).resolve().parents[1]


class HookError(RuntimeError):
    pass


def check_source(source: str, entry: str) -> list[str]:
    """Static policy.  Returns a list of violations (empty = acceptable)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error: {exc.msg} (line {exc.lineno})"]
    problems: list[str] = []
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    if entry not in defined:
        problems.append(f"must define a top-level function `{entry}`")
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_IMPORTS:
                    problems.append(f"line {line}: import of `{alias.name}` is not allowed (allowed: "
                                    f"{', '.join(sorted(ALLOWED_IMPORTS))})")
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                problems.append(f"line {line}: import from `{node.module}` is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            problems.append(f"line {line}: attribute `{node.attr}` is not allowed")
        elif isinstance(node, ast.Name) and (node.id in FORBIDDEN_NAMES or node.id.startswith("__")):
            problems.append(f"line {line}: name `{node.id}` is not allowed")
        elif isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef, ast.Await, ast.Global, ast.Nonlocal)):
            problems.append(f"line {line}: `{type(node).__name__}` is not allowed")
    return problems


def run_hook(source: str, entry: str, args: list, timeout: int = TIMEOUT_SECS):
    """Run `entry(*args)` from `source` in the sandbox and return its JSON-serialisable result."""
    problems = check_source(source, entry)
    if problems:
        raise HookError("; ".join(problems))
    payload = json.dumps({"source": source, "entry": entry, "args": args, "timeout": timeout})
    launcher = ("import sys; sys.path.insert(0, %r); from rsi_mvp import hook_runner; hook_runner.main()"
                % str(_ROOT))
    try:
        proc = subprocess.run([sys.executable, "-I", "-c", launcher], input=payload, capture_output=True,
                              text=True, timeout=timeout + 5, env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "0"},
                              cwd="/")
    except subprocess.TimeoutExpired:
        raise HookError(f"`{entry}` exceeded the {timeout}s time limit")
    line = next((l for l in reversed(proc.stdout.splitlines()) if l.startswith("RESULT ")), None)
    if line is None and proc.returncode < 0:
        raise HookError(f"`{entry}` was killed by the sandbox (signal {-proc.returncode}): it used more CPU time or "
                        "memory than allowed")
    if line is None:
        raise HookError(f"`{entry}` failed: " + (proc.stderr or proc.stdout or "no output").strip()[-500:])
    out = json.loads(line[len("RESULT "):])
    if not out["ok"]:
        raise HookError(f"`{entry}` raised {out['error']}")
    return out["value"]
