"""Larch call adapter for Python: runs inside the PROJECT's interpreter.

Started as `<project python> /path/to/adapter.py`. It imports nothing but the
standard library, and never puts Larch's own packages on sys.path, so it works on
any CPython >= 3.7 and cannot shadow or be shadowed by the project's dependencies.
Everything else (input generation, the Lean model, shrinking) happens in Larch's
driver process; this file only loads the function under test and calls it.

Protocol: one JSON object per line on the original stdin/stdout.
  {"op": "ping"}                                -> {"ok": true, "python": [3, 11, 4], ...}
  {"op": "load", "path", "function", "module", "package", "sys_path", "source"?}
                                                -> {"ok": true} | {"ok": false, "error", "error_type", "missing"?}
  {"op": "call", "args": [...], "timeout": 1.0} -> {"status": "ok", "value"} | {"status": "exception", "exc"}
                                                   | {"status": "timeout"}
  {"op": "new", "args"}                         -> construct the loaded class (stateful components)
  {"op": "invoke", "method", "args", "timeout"} -> like "call", on that instance
  {"op": "observe", "observers": [{"name", "access": "attribute"|"call"}]}
                                                -> {"status": "ok", "observers": {name: call-like result}}
Values use the tagged JSON format documented in larch/wire.py.
The user's code gets stdout/stderr redirected to a log file and stdin from /dev/null.
"""
import json
import os
import signal
import sys
import traceback
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
# Running a script puts its directory first on sys.path; never let Larch's own
# modules (or this directory) be importable by the code under test.
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != _HERE]

SAFE_INT = 2 ** 53 - 1


class CallTimeout(BaseException):
    """Raised by SIGALRM inside user code (BaseException so `except Exception` in user
    code does not swallow it)."""


def _on_alarm(signum, frame):  # pragma: no cover - signal handler
    raise CallTimeout()


def _repr(v, limit=400):
    try:
        r = repr(v)
    except Exception as e:  # noqa: BLE001
        r = "<unrepresentable %s: %s>" % (type(v).__name__, e)
    return r if len(r) <= limit else r[: limit - 3] + "..."


def to_wire(v, depth=0):
    if depth > 200:
        return {"$repr": _repr(v), "$type": type(v).__name__}
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return int(v) if -SAFE_INT <= v <= SAFE_INT else {"$int": str(int(v))}
    if isinstance(v, float):
        return {"$float": repr(v)}
    if type(v) is tuple:
        return {"$tuple": [to_wire(x, depth + 1) for x in v]}
    if type(v) is list:
        return [to_wire(x, depth + 1) for x in v]
    if type(v) is dict:
        return {"$dict": [[to_wire(k, depth + 1), to_wire(x, depth + 1)] for k, x in v.items()]}
    return {"$repr": _repr(v), "$type": type(v).__name__}


def from_wire(j):
    if isinstance(j, list):
        return [from_wire(x) for x in j]
    if not isinstance(j, dict):
        return j
    if "$tuple" in j:
        return tuple(from_wire(x) for x in j["$tuple"])
    if "$int" in j:
        return int(j["$int"])
    if "$float" in j:
        return float(j["$float"])
    if "$dict" in j:
        return dict((from_wire(k), from_wire(v)) for k, v in j["$dict"])
    raise ValueError("cannot pass %r to Python" % (j,))


class Adapter:
    def __init__(self):
        self.fn = None
        self.n_loads = 0

    def ping(self, req):
        return {
            "ok": True,
            "python": list(sys.version_info[:3]),
            "implementation": sys.implementation.name,
            "executable": sys.executable,
            "prefix": sys.prefix,
        }

    def load(self, req):
        self.fn = None
        path = os.path.abspath(req["path"])
        for entry in reversed(req.get("sys_path") or []):
            if entry in sys.path:
                sys.path.remove(entry)
            sys.path.insert(0, entry)
        modname = req.get("module") or os.path.splitext(os.path.basename(path))[0]
        source = req.get("source")
        try:
            if source is None:
                with open(path, "rb") as f:
                    source = f.read()
            code = compile(source, path, "exec")
            # Mutants and fixes reload the same module name: drop the previous copy so
            # each load starts from a fresh module object.
            mod = types.ModuleType(modname)
            mod.__file__ = path
            mod.__package__ = req.get("package") or ""
            sys.modules[modname] = mod
            exec(code, mod.__dict__)
            obj = mod
            for part in req["function"].split("."):
                obj = getattr(obj, part)
            if not callable(obj):
                raise TypeError("%s is not callable" % req["function"])
        except BaseException as e:  # noqa: BLE001 - user code may raise anything at import
            out = {
                "ok": False,
                "error_type": type(e).__name__,
                "error": ("%s: %s" % (type(e).__name__, e))[:2000],
                "traceback": traceback.format_exc()[-4000:],
            }
            if isinstance(e, ImportError) and getattr(e, "name", None):
                out["missing"] = e.name
            if isinstance(e, SyntaxError):
                out["line"] = e.lineno
            return out
        self.fn = obj
        self.n_loads += 1
        return {"ok": True}

    def _run(self, thunk, timeout):
        """Run user code with a timeout; return a protocol result."""
        use_alarm = hasattr(signal, "setitimer")
        try:
            if use_alarm:
                signal.setitimer(signal.ITIMER_REAL, timeout)
            try:
                value = thunk()
            finally:
                if use_alarm:
                    signal.setitimer(signal.ITIMER_REAL, 0)
        except CallTimeout:
            return {"status": "timeout"}, None
        except RecursionError as e:
            return {"status": "exception", "exc": ("RecursionError: %s" % e)[:300]}, None
        except BaseException as e:  # noqa: BLE001 - user code may raise anything (even SystemExit)
            return {"status": "exception", "exc": ("%s: %s" % (type(e).__name__, e))[:300]}, None
        try:
            return {"status": "ok", "value": to_wire(value)}, value
        except RecursionError:
            return {"status": "ok", "value": {"$repr": _repr(value), "$type": type(value).__name__}}, value

    def _args(self, req):
        return [from_wire(a) for a in req.get("args", [])]

    def call(self, req):
        if self.fn is None:
            return {"status": "exception", "exc": "LarchError: no function loaded"}
        try:
            args = self._args(req)
        except Exception as e:  # noqa: BLE001
            return {"status": "exception", "exc": "LarchError: %s" % e}
        return self._run(lambda: self.fn(*args), float(req.get("timeout", 1.0)))[0]

    # -- objects (stateful components) ------------------------------------------------------
    def new(self, req):
        """Construct an instance of the loaded class (replacing any previous one)."""
        self.obj = None
        if self.fn is None:
            return {"status": "exception", "exc": "LarchError: no class loaded"}
        args = self._args(req)
        res, value = self._run(lambda: self.fn(*args), float(req.get("timeout", 2.0)))
        if res["status"] == "ok":
            self.obj = value
            return {"status": "ok"}
        return res

    def invoke(self, req):
        obj = getattr(self, "obj", None)
        if obj is None:
            return {"status": "exception", "exc": "LarchError: no instance"}
        try:
            args = self._args(req)
            method = getattr(obj, req["method"])
        except Exception as e:  # noqa: BLE001
            return {"status": "exception", "exc": "%s: %s" % (type(e).__name__, e)}
        return self._run(lambda: method(*args), float(req.get("timeout", 1.0)))[0]

    def observe(self, req):
        """Read observers (attributes, properties, or zero-argument methods)."""
        obj = getattr(self, "obj", None)
        if obj is None:
            return {"status": "exception", "exc": "LarchError: no instance"}
        out = {}
        timeout = float(req.get("timeout", 1.0))
        for o in req.get("observers", []):
            name, access = o["name"], o.get("access", "attribute")
            thunk = (lambda n=name: getattr(obj, n)()) if access == "call" else (lambda n=name: getattr(obj, n))
            out[name] = self._run(thunk, timeout)[0]
        return {"status": "ok", "observers": out}


def main():
    log_path = sys.argv[1] if len(sys.argv) > 1 else os.devnull
    # Keep private copies of the protocol channel, then point fds 0/1/2 elsewhere so
    # prints (even from C extensions) cannot corrupt it.
    proto_in = os.fdopen(os.dup(0), "r", encoding="utf-8")
    proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    null_in = os.open(os.devnull, os.O_RDONLY)
    log = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(null_in, 0)
    os.dup2(log, 1)
    os.dup2(log, 2)
    sys.dont_write_bytecode = True
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _on_alarm)
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 3000))
    adapter = Adapter()
    handlers = {"ping": adapter.ping, "load": adapter.load, "call": adapter.call,
                "new": adapter.new, "invoke": adapter.invoke, "observe": adapter.observe}
    for line in proto_in:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            h = handlers.get(req.get("op"))
            resp = h(req) if h else {"ok": False, "error": "unknown op %r" % req.get("op")}
        except Exception as e:  # noqa: BLE001
            resp = {"ok": False, "error": "adapter error: %s: %s" % (type(e).__name__, e)}
        try:
            data = json.dumps(resp)
        except (TypeError, ValueError) as e:
            data = json.dumps({"status": "exception", "exc": "LarchError: unserializable result: %s" % e})
        proto_out.write(data + "\n")
        proto_out.flush()


if __name__ == "__main__":
    main()
