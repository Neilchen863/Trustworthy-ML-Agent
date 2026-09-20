"""Child-process side of the hook sandbox (see hooks.py).  Standard library only."""
import builtins
import importlib
import json
import sys
import types

_SAFE_BUILTINS = (
    "abs all any bool dict enumerate filter float int isinstance len list map max min range repr reversed round "
    "set slice sorted str sum tuple zip divmod pow chr ord format iter next bytes bytearray frozenset "
    "callable issubclass "
    "ArithmeticError AssertionError AttributeError Exception IndexError KeyError LookupError NameError "
    "RuntimeError StopIteration TypeError ValueError ZeroDivisionError True False None"
).split()


# stdlib modules that allowed modules import lazily inside functions: load them BEFORE opening of new files is
# forbidden, or e.g. Counter.most_common would fail on `import heapq`
_PRELOAD = ("heapq bisect operator functools copy enum numbers fractions decimal keyword reprlib random "
            "unicodedata codecs encodings.utf_8 encodings.ascii encodings.latin_1").split()
_HIDDEN_MEMBERS = {("string", "Formatter")}                # Formatter.get_field is getattr on arbitrary strings


def _facade(module):
    """What a hook sees instead of the real module: its public, non-module attributes.  The real module (and with
    it `_sys`, `__builtins__`, its globals) is not reachable from the facade."""
    members = {k: v for k, v in vars(module).items()
               if not k.startswith("_") and not isinstance(v, types.ModuleType)
               and (module.__name__, k) not in _HIDDEN_MEMBERS}
    return types.SimpleNamespace(**members)


def _limits(timeout):
    try:
        import resource
    except ImportError:
        return
    # NOFILE=3 (fds 0,1,2 only): no new file, socket or pipe can be opened, whatever Python-level trick is used
    for name, value in (("RLIMIT_CPU", int(timeout) + 1), ("RLIMIT_AS", 2 * 1024 ** 3),
                        ("RLIMIT_FSIZE", 0), ("RLIMIT_NPROC", 0), ("RLIMIT_NOFILE", 3)):
        try:                                                        # best effort, one at a time: an OS may refuse some
            limit = getattr(resource, name)
            resource.setrlimit(limit, (value, value))
        except Exception:
            pass


def main():
    request = json.loads(sys.stdin.read())
    from rsi_mvp import hooks                                       # static policy again, inside the child
    problems = hooks.check_source(request["source"], request["entry"])
    if problems:
        print("RESULT " + json.dumps({"ok": False, "error": "policy: " + "; ".join(problems)}))
        return
    facades = {name: _facade(importlib.import_module(name)) for name in sorted(hooks.ALLOWED_IMPORTS)}
    for name in _PRELOAD:
        try:
            importlib.import_module(name)
        except Exception:
            pass
    _limits(request["timeout"])

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        top = name.split(".")[0]
        if top not in facades or level or "." in name:
            raise ImportError("import of %r is not allowed" % name)
        return facades[top]

    safe = {n: getattr(builtins, n) for n in _SAFE_BUILTINS if hasattr(builtins, n)}
    safe["__import__"] = guarded_import
    namespace = {"__builtins__": safe, "__name__": "harness_hook"}
    try:
        exec(compile(request["source"], "<hook>", "exec"), namespace)
        value = namespace[request["entry"]](*request["args"])
        text = json.dumps(value)
        if len(text) > hooks.MAX_OUTPUT_CHARS:
            raise ValueError("result longer than %d characters" % hooks.MAX_OUTPUT_CHARS)
        print("RESULT " + json.dumps({"ok": True, "value": value}))
    except BaseException as exc:                                    # report, never crash silently
        print("RESULT " + json.dumps({"ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:300])}))
