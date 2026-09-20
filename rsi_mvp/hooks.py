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

What the layers are, exactly:
  * the hook sees FACADES of the allowed modules (public, non-module attributes only), never the real modules: no
    `collections._sys`, no `string.Formatter.get_field` (which would reach the real module namespace);
  * frame / generator / code / class-hierarchy introspection attributes and every `_private` attribute are refused;
  * inside the child, RLIMIT_NOFILE is set so that no new file, socket or pipe can be opened even if a Python-level
    escape were found; forking is limited by RLIMIT_NPROC; env is empty (no API key); cwd is an empty temp dir;
  * output is deterministic: PYTHONHASHSEED=0, and `hash`/`id` are not available.  Because a hash seed does not give
    the same set order on every Python build, the framework ALSO persists the rendered notes of a version
    (harness.py / loop.py) and never re-executes hooks to reproduce them.

This is defence in depth against accidents by our own improver, NOT a security boundary against a determined
adversary (Python cannot offer one; that needs OS-level isolation such as a container).  The RSI framework never
gives hooks the AIDE source, task data or the evaluator, and never runs a hook with the credentials of the experiment.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ALLOWED_IMPORTS = frozenset({"json", "re", "math", "textwrap", "collections", "itertools", "statistics", "string"})
FORBIDDEN_NAMES = frozenset({
    "open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "getattr", "setattr",
    "delattr", "input", "breakpoint", "memoryview", "help", "exit", "quit", "classmethod", "staticmethod",
    "property", "super", "type", "object", "hash", "id",       # hash/id differ between runs: notes must not
})
FORBIDDEN_ATTRS = frozenset({                                   # frame / code / generator / class-hierarchy access
    "gi_frame", "gi_code", "gi_yieldfrom", "cr_frame", "cr_code", "cr_await", "cr_origin", "ag_frame", "ag_code",
    "ag_await", "f_back", "f_globals", "f_locals", "f_builtins", "f_code", "f_trace", "tb_frame", "tb_next",
    "func_globals", "func_code", "mro", "get_field",
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
        elif isinstance(node, ast.Attribute) and (node.attr.startswith("_") or node.attr in FORBIDDEN_ATTRS):
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
    launcher = ("import sys; sys.path[:] = [p for p in sys.path if p]; sys.path.insert(0, %r); "
                "from rsi_mvp import hook_runner; hook_runner.main()" % str(_ROOT))
    with tempfile.TemporaryDirectory(prefix="rsi_hook_") as cwd:      # an empty directory: nothing to see, nothing to import
        try:
            # NOT `-I`: isolated mode ignores PYTHONHASHSEED, and the notes must not depend on a random hash seed
            proc = subprocess.run([sys.executable, "-s", "-B", "-c", launcher], input=payload, capture_output=True,
                                  text=True, timeout=timeout + 5, cwd=cwd,
                                  env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"})
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
