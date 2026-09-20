"""Child-process side of the hook sandbox (see hooks.py).  Standard library only; runs under `python -I`."""
import builtins
import json
import sys

_SAFE_BUILTINS = (
    "abs all any bool dict enumerate filter float int isinstance len list map max min range repr reversed round "
    "set slice sorted str sum tuple zip divmod pow hash chr ord format iter next bytes bytearray frozenset "
    "callable id issubclass "
    "ArithmeticError AssertionError AttributeError Exception IndexError KeyError LookupError NameError "
    "RuntimeError StopIteration TypeError ValueError ZeroDivisionError True False None"
).split()


def _limits(timeout):
    try:
        import resource
    except ImportError:
        return
    for name, value in (("RLIMIT_CPU", int(timeout) + 1), ("RLIMIT_AS", 2 * 1024 ** 3),
                        ("RLIMIT_FSIZE", 0), ("RLIMIT_NPROC", 0)):
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
    _limits(request["timeout"])
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.split(".")[0] not in hooks.ALLOWED_IMPORTS or level:
            raise ImportError("import of %r is not allowed" % name)
        return real_import(name, globals, locals, fromlist, level)

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
