"""Sequence testing of stateful components (runs inside the driver process).

A test case is a constructor call followed by a sequence of method calls. The Lean
harness replays it on the model; the adapter replays it on a real instance. After the
constructor and after every call Larch compares the outcome (value, or "raised") and
every observer, so a method that corrupts state is caught at the first observation that
depends on it. The first divergence ends the case; Hypothesis shrinks failing cases to
the shortest sequence that still fails.
"""
from __future__ import annotations

import itertools
import json
import random
import re
import time
import warnings

from larch.engine.driver import _ints_within
from larch.lean.harness_client import HarnessClient, HarnessError, HarnessTimeout
from larch.lean.types import EncodeError, LType, encode, parse_type, strategy_for


def _flat(value, t: LType):
    """A right-nested product value [a, [b, c]] as the flat list [a, b, c]."""
    if t.head != "Prod":
        return [value]
    n = len(t.prod_components())
    out, cur = [], value
    for i in range(n - 1):
        if not isinstance(cur, (list, tuple)) or len(cur) != 2:
            return [value]
        out.append(cur[0])
        cur = cur[1]
    out.append(cur)
    return out


def _r(v, limit: int = 200) -> str:
    try:
        s = repr(v)
    except Exception:  # noqa: BLE001
        s = "<unrepresentable>"
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _http_ok(value, where: str) -> bool:
    """Whether a value can be sent as-is in this part of an HTTP request. Control
    characters, header text outside printable ASCII, and path segments that are empty or
    would be re-split or normalized (`/`, `.`, `..`) are outside what a client can send."""
    if isinstance(value, str):
        if any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
            return False
        if where == "header":
            return value.isascii() and value == value.strip()
        if where == "path":
            return value not in ("", ".", "..") and "/" not in value and "\\" not in value
        return True
    if isinstance(value, (list, tuple)):
        return all(_http_ok(v, "body") for v in value)
    return True


def _coarse(text, is_service: bool) -> str:
    """The class of an outcome, for telling distinct bugs apart: the status code for a
    service; raised / returned for a class."""
    t = str(text)
    if is_service:
        m = re.search(r"\b([1-5]\d\d)\b", t)
        return m.group(1) if m else t[:20]
    return "raises" if t.startswith(("raise", "raised")) else "returns"


def _call_repr(var: str, method: str, args: list) -> str:
    return f"{var}.{method}({', '.join(_r(a) for a in args)})"


class Component:
    def __init__(self, job: dict, harness: HarnessClient, impl=None):
        c = job["component"]
        self.cls = c["name"]
        self.var = c.get("var") or (self.cls[:1].lower() + self.cls[1:]) or "obj"
        self.init_types = [parse_type(t) for t in c["init_params"]]
        self.ops = {o["method"]: ([parse_type(t) for t in o["params"]], parse_type(o["returns"])) for o in c["operations"]}
        # Response fields with random values (UUIDs, tokens): compared by order of first appearance.
        self.opaque = {o["method"]: set(o.get("opaque", [])) for o in c["operations"]}
        self.observers = c["observers"]
        self.obs_types = {o["name"]: parse_type(o["lean_type"]) for o in self.observers}
        self.harness = harness
        self.impl = impl
        self.call_timeout = float(job.get("call_timeout", 1.0))
        self.int_bound = job.get("int_bound")
        # For a service: where each operation's parameters travel (path, query, header, body).
        http_ops = (job.get("impl") or {}).get("operations") or {}
        self.wheres = {m: [p.get("in", "body") for p in op.get("params", [])] for m, op in http_ops.items()}

    # -- encoding -------------------------------------------------------------------------------
    def _enc(self, args, types: list[LType]):
        if not isinstance(args, (list, tuple)) or len(args) != len(types):
            return None
        try:
            return [encode(a, t) for a, t in zip(args, types)]
        except EncodeError:
            return None

    def encode_case(self, case) -> dict | None:
        init, steps = case
        e_init = self._enc(list(init), self.init_types)
        if e_init is None:
            return None
        e_steps = []
        for m, args in steps:
            if m not in self.ops:
                return None
            if m in self.wheres and not all(_http_ok(a, w) for a, w in zip(args, self.wheres[m])):
                return None
            e = self._enc(list(args), self.ops[m][0])
            if e is None:
                return None
            e_steps.append([m, e])
        return {"init": e_init, "steps": e_steps}

    # -- running ---------------------------------------------------------------------------------
    def model(self, enc: dict) -> dict:
        return self.harness.request({"op": "seq", **enc}, timeout=max(3.0, 0.5 * (1 + len(enc["steps"]))))

    def _observe(self) -> tuple[dict, str | None]:
        """Impl observer values as Lean JSON, or an error description."""
        if not self.observers:
            return {}, None
        resp = self.impl.observe([{"name": o["name"], "access": o.get("access", "attribute")} for o in self.observers], self.call_timeout)
        if resp.get("lost") or resp.get("status") != "ok":
            return {}, f"reading the observers failed: {resp.get('exc', resp.get('status'))}"
        out = {}
        for name, r in resp["observers"].items():
            if r.get("status") != "ok":
                return {}, f"`{name}` raised {r.get('exc', r.get('status'))}"
            try:
                out[name] = encode(r["value"], self.obs_types[name])
            except EncodeError as e:
                return {}, f"`{name}` returned {_r(r.get('value'))}, which does not fit {self.obs_types[name]}: {e}"
        return out, None

    def evaluate(self, case, *, with_impl: bool = True) -> dict:
        enc = self.encode_case(case)
        if enc is None:
            return {"kind": "domain"}
        try:
            m = self.model(enc)
        except HarnessTimeout:
            return {"kind": "model_timeout"}
        except HarnessError as e:
            return {"kind": "harness_error", "error": str(e)}
        if "error" in m:
            return {"kind": "harness_error", "error": m["error"]}
        if not m.get("pre"):
            return {"kind": "pre_false"}
        if self.int_bound is not None and not _ints_within([m.get("init"), m.get("steps")], int(self.int_bound)):
            return {"kind": "domain"}  # a value the target runtime cannot represent exactly
        init, steps = case
        lines = [f"{self.var} = {self.cls}({', '.join(_r(a) for a in init)})"]
        model_viol = self._model_violations(m)
        if model_viol:
            return {"kind": "model_violation", "model_violates": model_viol[1], "args_repr": "\n".join(lines + model_viol[0])}
        if not with_impl:
            return {"kind": "model_only", "steps": len(steps)}
        rec = self._run_impl(case, m, lines)
        rec["model_json"] = m
        return rec

    def _model_violations(self, m: dict):
        init, steps = m.get("init"), m.get("steps") or []
        if isinstance(init, dict):
            bad = [n for n, ok in init.get("inv", []) if not ok]
            if bad:
                return [], bad
        for k, st in enumerate(steps):
            bad = [n for n, ok in st.get("inv", []) if not ok] + [n for n, ok in st.get("posts", []) if not ok]
            if bad:
                return [f"# step {k + 1}"], bad
        return None

    def _run_impl(self, case, m: dict, lines: list[str]) -> dict:
        init, steps = case
        model_init = m["init"]
        self._seen_impl, self._seen_model = {}, {}
        res = self.impl.new(list(init), timeout=max(2.0, self.call_timeout))
        if model_init == "raises":
            if res.get("status") == "exception":
                return {"kind": "agree", "steps": 0, "args_repr": "\n".join(lines)}
            return self._div("value", lines, "constructed normally", "raises", "the constructor should raise")
        if res.get("status") == "timeout":
            return self._div("timeout", lines, f"did not return within {self.call_timeout:g}s", "returns")
        if res.get("status") != "ok":
            return self._div("crash", lines, f"raised {res.get('exc')}", "returns")
        obs, err = self._observe()
        if err:
            return self._div("crash", lines, err, "")
        for name, v in model_init["obs"].items():
            if obs.get(name) != v:
                return self._div("value", lines, _r(obs.get(name)), _r(v), f"right after construction, `{name}` differs")
        for k, (method, args) in enumerate(steps):
            mstep = m["steps"][k]
            call = _call_repr(self.var, method, list(args))
            lines.append(call)
            ret = self.ops[method][1]
            r = self.impl.invoke(method, list(args), self.call_timeout)
            model_raises = "error" in mstep
            if r.get("status") == "timeout":
                return self._div("timeout", lines, f"did not return within {self.call_timeout:g}s", "returns" if not model_raises else "raises", steps_done=k)
            if r.get("lost"):
                return self._div("crash", lines, r.get("exc", "the interpreter died"), "", steps_done=k)
            if r.get("status") == "exception":
                if not model_raises:
                    return self._div("crash", lines, f"raised {r.get('exc')}", _r(mstep.get("ok")), steps_done=k)
            else:
                if model_raises:
                    return self._div("value", lines, f"returned {_r(r.get('value'))}", "raises", steps_done=k)
                try:
                    v = encode(r.get("value"), ret)
                except EncodeError as e:
                    return self._div("type", lines, _r(r.get("value")), _r(mstep.get("ok")), f"does not fit {ret}: {e}", steps_done=k)
                if self.opaque.get(method):
                    v, mv = self._canon(method, v, ret, impl_side=True), self._canon(method, mstep.get("ok"), ret, impl_side=False)
                else:
                    mv = mstep.get("ok")
                if v != mv:
                    shown = r.get("raw") or r.get("value")
                    return self._div("value", lines, _r(shown), _r(_flat(mstep.get("ok"), ret)), steps_done=k)
            obs, err = self._observe()
            if err:
                return self._div("crash", lines, err, "", steps_done=k)
            for name, mv in mstep["obs"].items():
                if obs.get(name) != mv:
                    acc = next((o.get("access") for o in self.observers if o["name"] == name), "attribute")
                    lines.append(f"{self.var}.{name}{'()' if acc == 'call' else ''}")
                    return self._div("value", lines, _r(obs.get(name)), _r(mv),
                                     f"after `{call}`, `{name}` is {_r(obs.get(name))} but should be {_r(mv)}", steps_done=k + 1)
        return {"kind": "agree", "steps": len(steps)}

    def _canon(self, method: str, value, ret: LType, *, impl_side: bool):
        """Replace opaque fields by the order in which each distinct value first appeared
        in this sequence (separately for the implementation and the model)."""
        seen = self._seen_impl if impl_side else self._seen_model
        flat = list(_flat(value, ret))
        for i in self.opaque[method]:
            if i < len(flat) and flat[i] is not None:
                key = json.dumps(flat[i], sort_keys=True)
                flat[i] = {"$opaque": seen.setdefault(key, len(seen))}
        return flat

    def _div(self, kind, lines, impl, model, detail: str = "", steps_done: int = 0) -> dict:
        last = lines[-1] if lines else ""
        method = last.split("(", 1)[0].rsplit(".", 1)[-1]
        return {"kind": kind, "args_repr": "\n".join(lines), "impl": impl, "model": model, "detail": detail,
                "length": len(lines) - 1, "impl_violates": [],
                "group": f"{kind}:{method}:{_coarse(impl, bool(self.wheres))}:{_coarse(model, bool(self.wheres))}"}


# ---------------------------------------------------------------------------
# Input generation
# ---------------------------------------------------------------------------

def case_strategy(job: dict, comp: Component):
    """Constructor arguments plus a list of calls, from the formalizer's generator where it
    has one (per operation), otherwise type-directed."""
    from hypothesis import strategies as st

    bound = job.get("int_bound")

    def typed(types):
        return st.tuples(*[strategy_for(t, int_bound=bound) for t in types]) if types else st.just(())

    custom: dict = {}
    note = None
    code = (job.get("strategy") or {}).get("code")
    if code:
        try:
            ns: dict = {}
            exec(compile(code, "<larch-strategy>", "exec"), ns)
            custom = ns["strategy"](st) or {}
            if not isinstance(custom, dict):
                raise TypeError("strategy(st) must return a dict of strategies")
        except Exception as e:  # noqa: BLE001
            custom, note = {}, f"LLM input strategy failed ({type(e).__name__}: {e}); using type-directed inputs"

    def norm(s):
        return s.map(lambda v: tuple(v) if isinstance(v, (list, tuple)) else (v,))

    init = norm(custom["init"]) if "init" in custom else typed(comp.init_types)
    calls = []
    for m, (types, _ret) in comp.ops.items():
        if m in custom:
            # Mostly the formalizer's values: calls only interact (the same account, order or
            # key) when their arguments come from shared small pools. Some type-directed
            # values keep the unexpected inputs in play.
            c, t = norm(custom[m]), typed(types)
            if m in comp.wheres:
                t = t.filter(lambda a, w=comp.wheres[m]: all(_http_ok(x, k) for x, k in zip(a, w)))
            args = st.integers(0, 9).flatmap(lambda i, c=c, t=t: t if i == 0 else c)
        else:
            args = typed(types)
            if m in comp.wheres:
                args = args.filter(lambda a, w=comp.wheres[m]: all(_http_ok(x, k) for x, k in zip(a, w)))
        calls.append(st.tuples(st.just(m), args))
    step = st.one_of(*calls) if calls else st.nothing()
    max_steps = int(job.get("max_steps", 10))
    # Half the sequences are long: bugs in stateful code often need several calls to set up.
    seqs = st.one_of(st.lists(step, max_size=max_steps), st.lists(step, min_size=max(1, max_steps // 2), max_size=max_steps))
    return st.tuples(init, seqs), note


def exhaustive_cases(job: dict, comp: Component, limit: int) -> tuple[list, int] | None:
    """Every case built from the finite domains: all constructor arguments, then every
    sequence of calls up to the longest length that fits in `limit` cases."""
    doms = job.get("exhaustive_domains") or {}
    inits = doms.get("init", [[]] if not comp.init_types else None)
    if inits is None or any(m not in doms for m in comp.ops):
        return None
    calls = [(m, list(a)) for m in comp.ops for a in doms[m]]
    if not calls:
        return None
    total, k = len(inits), 0
    while k < int(job.get("max_steps", 10)):
        nxt = total + len(inits) * len(calls) ** (k + 1)
        if nxt > limit:
            break
        total, k = nxt, k + 1
    cases = []
    for length in range(k + 1):
        for init in inits:
            for seq in itertools.product(calls, repeat=length):
                cases.append((tuple(init), list(seq)))
    return cases, k


def collect(strategy, n: int, seed: int) -> list:
    from hypothesis import HealthCheck, Phase, Verbosity, given, settings
    from hypothesis import seed as hseed

    out: list = []

    @hseed(seed)
    @settings(max_examples=n, database=None, deadline=None, phases=[Phase.generate],
              suppress_health_check=list(HealthCheck), verbosity=Verbosity.quiet)
    @given(strategy)
    def _c(x):
        out.append(x)

    try:
        _c()
    except Exception as e:  # noqa: BLE001
        if not out:
            raise RuntimeError(f"input generation failed: {type(e).__name__}: {e}") from e
    return out


def shrink(strategy, predicate, budget: int, seed: int):
    from hypothesis import HealthCheck, find, settings

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return find(strategy, predicate, settings=settings(max_examples=budget, database=None, deadline=None,
                                                               suppress_health_check=list(HealthCheck)),
                        random=random.Random(seed))
    except Exception:  # noqa: BLE001
        return None


DIVERGENT = ("value", "crash", "timeout", "type")


# ---------------------------------------------------------------------------
# Coverage-guided generation
# ---------------------------------------------------------------------------

def _outcome(value, is_service: bool):
    """A coarse class of one step's result in the model: the HTTP status for a service;
    for a class, raised / a boolean / a small integer / anything else."""
    if isinstance(value, dict) and "error" in value:
        return "raises"
    v = value.get("ok") if isinstance(value, dict) else None
    if is_service and isinstance(v, list) and v and isinstance(v[0], int):
        return v[0]
    if is_service and isinstance(v, int) and not isinstance(v, bool):
        return v
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return max(-2, min(2, v))
    return "ok"


def features(comp: Component, case) -> set:
    """What a sequence exercises, according to the model: each step's (operation, outcome)
    and each run of two and three consecutive ones. Empty if the case is outside the domain."""
    enc = comp.encode_case(case)
    if enc is None:
        return set()
    try:
        m = comp.model(enc)
    except (HarnessError, HarnessTimeout):
        return set()
    if "error" in m or not m.get("pre"):
        return set()
    is_service = bool(comp.wheres)
    out, prev2, prev = set(), ("init",), ("init",)
    for (meth, _), st_ in zip(case[1], m.get("steps") or []):
        cur = (meth, _outcome(st_, is_service))
        out.update((cur, (prev, cur), (prev2, prev, cur)))
        prev2, prev = prev, cur
    return out


def guided_cases(comp: Component, strategy, n: int, seed: int, max_steps: int, *, budget_factor: int = 10,
                 deadline: float | None = None) -> list:
    """Half random sequences; the other half grown, by appending or replacing steps, from
    the sequences that reached the rarest model behaviour (operation outcomes and runs of
    them), then chosen by that rarity. Deep states (an order paid, then paid again) are
    reached in a few generations instead of by chance. Only the model runs here, which is
    cheap; the implementation runs on the chosen cases."""
    rng = random.Random(seed)
    base = collect(strategy, max(1, n // 2), seed)
    pool = [step for c in base for step in c[1]]
    if not pool or n - len(base) <= 0:
        return base
    count: dict = {}
    corpus: list[tuple[tuple, frozenset]] = []

    def add(case, f) -> bool:
        novel = any(x not in count for x in f)
        for x in f:
            count[x] = count.get(x, 0) + 1
        if novel:
            corpus.append((case, frozenset(f)))
        return novel

    def rarity(f) -> float:
        return sum(1.0 / count[x] for x in f)

    for c in base:
        add(c, features(comp, c))
    for evals in range(n * budget_factor):
        if not corpus or (deadline is not None and time.monotonic() > deadline):
            break
        if evals % 50 == 0:
            weights = [rarity(f) + 1e-9 for _, f in corpus]
        parent = rng.choices(corpus[: len(weights)], weights=weights)[0][0]
        init, steps = parent[0], list(parent[1])
        for _ in range(rng.randint(1, 3)):
            if len(steps) < max_steps and rng.random() < 0.8:
                steps.append(rng.choice(pool))
            elif steps:
                steps[rng.randrange(len(steps))] = rng.choice(pool)
        cand = (init, steps)
        add(cand, features(comp, cand))
    base_ids = {id(c) for c in base}
    grown = sorted((cf for cf in corpus if id(cf[0]) not in base_ids), key=lambda cf: -rarity(cf[1]))
    return base + [c for c, _ in grown[: n - len(base)]]


def shrink_case(case, fails, budget: int):
    """Greedy step deletion on a concrete failing case: the shortest sequence (by removing
    whole calls) that still fails the same way."""
    init, steps = case[0], list(case[1])
    tries, changed = 0, True
    while changed and tries < budget:
        changed = False
        for i in range(len(steps) - 1, -1, -1):
            if tries >= budget:
                break
            cand = steps[:i] + steps[i + 1:]
            tries += 1
            if fails((init, cand)):
                steps, changed = cand, True
    return (init, steps)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def job_seq(job: dict, harness: HarnessClient, impl) -> dict:
    t0 = time.monotonic()
    model_only = bool(job.get("model_only"))
    if not model_only:
        impl.load(job.get("target", {}).get("source"))
    comp = Component(job, harness, None if model_only else impl.client)
    strategy, note = case_strategy(job, comp)
    seed = int(job.get("seed", 0))
    exhaustive = bool(job.get("exhaustive"))
    depth = None
    if exhaustive:
        ex = exhaustive_cases(job, comp, int(job.get("exhaustive_limit", 20_000)))
        if ex is None:
            return {"ok": False, "error": "no finite domains for exhaustive sequence testing"}
        cases, depth = ex
    elif job.get("cases") is not None:  # replay given cases (e.g. a fix's failures, on the original code)
        cases = [(tuple(i), [(m, tuple(a)) for m, a in steps]) for i, steps in job["cases"]]
    elif model_only or not job.get("guided", True):
        cases = collect(strategy, int(job.get("max_examples", 300)), seed)
    else:
        cases = guided_cases(comp, strategy, int(job.get("max_examples", 300)), seed, int(job.get("max_steps", 10)),
                             deadline=t0 + float(job.get("time_budget", 600)) / 3)
    counts: dict[str, int] = {}
    failures, model_violations, failing_cases = [], [], []
    deadline = t0 + float(job.get("time_budget", 600))
    total_steps = 0
    stopped = None
    for case in cases:
        rec = comp.evaluate(case, with_impl=not model_only)
        k = rec["kind"]
        counts[k] = counts.get(k, 0) + 1
        total_steps += rec.get("steps", 0) if isinstance(rec.get("steps"), int) else 0
        if k == "model_violation" and len(model_violations) < 10:
            model_violations.append(rec)
        if k in DIVERGENT and len(failures) < 25:
            rec["case"] = [list(case[0]), [[m, list(a)] for m, a in case[1]]]
            failures.append(rec)
            failing_cases.append(case)
        if time.monotonic() > deadline:
            stopped = "time budget exhausted"
            break
    result = {"ok": True, "counts": counts, "inputs": len(cases), "calls": total_steps, "failures": failures,
              "model_violations": model_violations, "strategy_note": note, "exhaustive": exhaustive,
              "depth": depth, "stopped_early": stopped}
    if failures and not exhaustive and job.get("shrink", True) and time.monotonic() < deadline:
        kind = failures[0]["kind"]

        def fails(c):
            return comp.evaluate(c)["kind"] == kind

        budget = int(job.get("shrink_budget", 200))
        same = [c for c, r in zip(failing_cases, failures) if r["kind"] == kind]
        best = shrink_case(min(same, key=lambda c: len(c[1])), fails, budget // 2)
        small = shrink(strategy, fails, budget // 2, seed)
        if small is not None and len(small[1]) < len(best[1]):
            best = small
        r = comp.evaluate(best)
        if r["kind"] in DIVERGENT:
            r["shrunk"] = True
            result["minimal"] = r
        else:  # not reproducible (e.g. a flaky service): report the shortest observed failure
            result["minimal"] = dict(min(failures, key=lambda r: r.get("length", 99)), shrunk=False)
        # One shrunk example per other kind of disagreement (another operation, other outcomes):
        # several bugs in one class or service are reported separately.
        minimals, groups = [result["minimal"]], {result["minimal"].get("group")}
        for c, rec in sorted(zip(failing_cases, failures), key=lambda cr: len(cr[0][1])):
            g = rec.get("group")
            if g in groups or len(minimals) >= 3 or time.monotonic() > deadline:
                continue
            groups.add(g)
            small = shrink_case(c, lambda x, g=g: comp.evaluate(x).get("group") == g, budget // 4)
            r = comp.evaluate(small)
            minimals.append(dict(r, shrunk=True) if r.get("group") == g else rec)
        result["minimals"] = minimals
    elif failures:
        result["minimal"] = dict(min(failures, key=lambda r: r.get("length", 99)), shrunk=True)
    if model_violations and job.get("shrink", True):

        def mfails(c):
            return comp.evaluate(c, with_impl=False)["kind"] == "model_violation"

        small = shrink(strategy, mfails, 150, seed + 1)
        if small is not None:
            result["minimal_model_violation"] = comp.evaluate(small, with_impl=False)
    result["elapsed"] = time.monotonic() - t0
    return result


def job_seq_mutants(job: dict, harness: HarnessClient, impl, result_path: str) -> dict:
    t0 = time.monotonic()
    comp = Component(job, harness, impl.client)
    strategy, note = case_strategy(job, comp)
    seed = int(job.get("seed", 0))
    cases = [c for c in collect(strategy, int(job.get("max_examples", 150)), seed)
             if comp.evaluate(c, with_impl=False)["kind"] == "model_only"]
    extra = [c for c in collect(strategy, int(job.get("survivor_examples", 400)), seed + 101)
             if comp.evaluate(c, with_impl=False)["kind"] == "model_only"]
    from larch.engine.impl_client import LoadError

    out = []
    stream = open(result_path + ".partial", "a")
    for mu in job["mutants"]:
        res = {"id": mu["id"], "killed": False, "by": None, "specs": [], "counterexample": None}
        try:
            impl.load(mu["source"])
        except LoadError as e:
            res.update(invalid=True, by="load_error", detail=str(e)[:200])
        else:
            for c in cases + extra:
                r = comp.evaluate(c)
                if r["kind"] in DIVERGENT:
                    res.update(killed=True, by=r["kind"], extended=c in extra,
                               counterexample={"args_repr": r.get("args_repr"), "impl": r.get("impl"), "model": r.get("model")})
                    break
            else:
                res["likely_equivalent"] = True
        out.append(res)
        stream.write(json.dumps(res) + "\n")
        stream.flush()
    stream.close()
    return {"ok": True, "valid_inputs": len(cases), "mutants": out, "strategy_note": note, "elapsed": time.monotonic() - t0}
