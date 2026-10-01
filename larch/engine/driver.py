"""Test driver: runs the code under test against the Lean model.

Invoked as `python -m larch.engine.driver JOB.json RESULT.json` with LARCH'S OWN
interpreter, so hypothesis and the rest of Larch are always importable and never
leak into the project being verified. The code under test runs in a separate
adapter process in the project's own runtime (see larch/engine/impl_client.py and
larch/py/adapter.py): it may print, hang, crash or mutate its arguments without
affecting the driver.

Job kinds
  drt      differential test: implementation vs. model (+ postconditions on impl output)
  mutants  run many implementation variants against a fixed input sample
  props    test model-level properties on random inputs (no implementation involved)
  probe    evaluate explicit inputs and return full records
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
import traceback
import warnings
from pathlib import Path

from larch.engine.impl_client import ImplClient, LoadError
from larch.lean.harness_client import HarnessClient, HarnessError, HarnessTimeout
from larch.lean.types import EncodeError, decode, encode, parse_type, perturb, strategy_for

DIVERGENT = ("value", "crash", "timeout", "type")


# ---------------------------------------------------------------------------
# The code under test (behind the adapter)
# ---------------------------------------------------------------------------

class Impl:
    """The function under test, loaded in the project's runtime."""

    def __init__(self, job: dict):
        spec = job["impl"]
        self.load_req = dict(spec["load"])
        self.client = ImplClient(spec["cmd"], spec["env"], spec["cwd"], log_path=job.get("log_path"))

    def start(self) -> None:
        self.client.start()

    def load(self, source: str | None = None) -> None:
        self.client.load(self.load_req, source)

    def call(self, args: list, timeout: float) -> dict:
        return self.client.call(args, timeout)

    def close(self) -> None:
        self.client.close()


def _ints_within(v, bound: int) -> bool:
    if isinstance(v, bool):
        return True
    if isinstance(v, int):
        return -bound <= v <= bound
    if isinstance(v, (list, tuple)):
        return all(_ints_within(x, bound) for x in v)
    return True


def safe_repr(v, limit: int = 400) -> str:
    try:
        r = repr(v)
    except Exception as e:  # pragma: no cover
        r = f"<unrepresentable {type(v).__name__}: {e}>"
    return r if len(r) <= limit else r[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# Evaluation of one input
# ---------------------------------------------------------------------------

class Evaluator:
    def __init__(self, job: dict, harness: HarnessClient, fn=None):
        spec = job["spec"]
        self.params = [parse_type(p["lean_type"]) for p in spec["params"]]
        self.ret = parse_type(spec["return_type"])
        self.exceptions = bool(spec["exceptions"])
        self.posts: list[str] = list(spec["posts"])
        self.harness = harness
        self.fn = fn
        self.call_timeout = float(job.get("call_timeout", 1.0))
        self.timeouts = 0
        # Integers the target runtime represents exactly (e.g. JavaScript numbers);
        # inputs outside it are outside the function's domain, not bugs.
        self.int_bound = job.get("int_bound")

    def encode_args(self, args) -> list | None:
        if not isinstance(args, (list, tuple)) or len(args) != len(self.params):
            return None
        if self.int_bound is not None and not _ints_within(args, int(self.int_bound)):
            return None
        try:
            return [encode(a, t) for a, t in zip(args, self.params)]
        except EncodeError:
            return None

    def decode_model(self, j):
        if self.exceptions:
            if isinstance(j, dict) and "error" in j:
                return {"raises": j["error"]}
            return decode(j.get("ok") if isinstance(j, dict) else j, self.ret)
        return decode(j, self.ret)

    def model_repr(self, j) -> str:
        v = self.decode_model(j)
        if isinstance(v, dict) and "raises" in v:
            return f"raises ({v['raises']})"
        return safe_repr(v)

    def evaluate(self, args, *, with_impl: bool = True, impl_override=None) -> dict:
        args = list(args) if isinstance(args, (list, tuple)) else args
        enc = self.encode_args(args)
        if enc is None:
            return {"kind": "domain"}
        req: dict = {"op": "case", "args": enc}
        implres = None
        if impl_override is not None:
            req["impl"] = impl_override
        elif with_impl:
            implres = self.fn.call(args, self.call_timeout)
            if implres["status"] == "ok":
                try:
                    v = encode(implres["value"], self.ret)
                    req["impl"] = {"ok": v} if self.exceptions else v
                except EncodeError as e:
                    implres["encode_error"] = str(e)
            elif implres["status"] == "exception" and self.exceptions:
                req["impl"] = {"error": implres["exc"]}
        try:
            resp = self.harness.request(req)
        except HarnessTimeout:
            return {"kind": "model_timeout", "args": args}
        except HarnessError as e:
            return {"kind": "harness_error", "error": str(e), "args": args}
        if "error" in resp:
            return {"kind": "harness_error", "error": resp["error"], "args": args}
        if not resp.get("pre"):
            return {"kind": "pre_false"}
        rec: dict = {
            "args": args,
            "args_repr": ", ".join(safe_repr(a) for a in args),
            "model_json": resp["model"],
            "model": self.model_repr(resp["model"]),
            "post_model": dict(zip(self.posts, resp.get("post_model", []))),
        }
        rec["model_violates"] = [p for p, ok in rec["post_model"].items() if not ok]
        if impl_override is not None:
            rec["post_impl"] = dict(zip(self.posts, resp.get("post_impl", [])))
            rec["kind"] = "override"
            return rec
        if not with_impl:
            rec["kind"] = "model_only"
            return rec
        assert implres is not None
        if implres["status"] == "timeout":
            self.timeouts += 1
            rec["kind"] = "timeout"
            rec["impl"] = f"did not return within {self.call_timeout:g}s"
        elif implres["status"] == "exception" and not self.exceptions:
            rec["kind"] = "crash"
            rec["impl"] = f"raised {implres['exc']}"
        elif "encode_error" in implres:
            rec["kind"] = "type"
            rec["impl"] = safe_repr(implres["value"])
            rec["detail"] = f"return value does not fit the model's type {self.ret}: {implres['encode_error']}"
        elif "impl_decode_error" in resp:
            rec["kind"] = "type"
            rec["impl"] = safe_repr(implres.get("value"))
            rec["detail"] = resp["impl_decode_error"]
        else:
            if implres["status"] == "exception":
                rec["impl"] = f"raises ({implres['exc']})"
            else:
                rec["impl"] = safe_repr(implres["value"])
            rec["post_impl"] = dict(zip(self.posts, resp.get("post_impl", [])))
            rec["kind"] = "agree" if resp.get("eq") else "value"
        rec["impl_violates"] = [p for p, ok in rec.get("post_impl", {}).items() if not ok]
        return rec


# ---------------------------------------------------------------------------
# Input generation
# ---------------------------------------------------------------------------

def build_strategy(job: dict, param_types, *, which: str | None = None):
    from hypothesis import strategies as st

    typed = st.tuples(*[strategy_for(t) for t in param_types]) if param_types else st.just(())
    sconf = job.get("strategy") or {}
    mode = which or sconf.get("mode", "typed")
    note = None
    if mode in ("llm", "mixed") and sconf.get("code"):
        try:
            ns: dict = {}
            exec(compile(sconf["code"], "<larch-strategy>", "exec"), ns)
            llm = ns["strategy"](st)
            llm = llm.map(lambda v: tuple(v) if isinstance(v, (list, tuple)) else (v,))
            if mode == "llm":
                return llm, None
            return st.one_of(llm, llm, typed), None  # ~2/3 targeted, 1/3 type-directed
        except Exception as e:  # noqa: BLE001
            note = f"LLM input strategy failed ({type(e).__name__}: {e}); using type-directed inputs"
    return typed, note


def collect_inputs(strategy, n: int, seed: int) -> list:
    from hypothesis import HealthCheck, Phase, Verbosity, given, settings
    from hypothesis import seed as hseed

    out: list = []
    if n <= 0:
        return out

    @hseed(seed)
    @settings(
        max_examples=n,
        database=None,
        deadline=None,
        phases=[Phase.generate],
        suppress_health_check=list(HealthCheck),
        verbosity=Verbosity.quiet,
    )
    @given(strategy)
    def _collect(x):
        out.append(x)

    try:
        _collect()
    except Exception as e:  # noqa: BLE001 - e.g. an LLM strategy raising while drawing
        if not out:
            raise RuntimeError(f"input generation failed: {type(e).__name__}: {e}") from e
    return out


def shrink(strategy, predicate, budget: int, seed: int):
    from hypothesis import HealthCheck, find, settings
    from hypothesis.errors import NoSuchExample

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return find(
                strategy,
                predicate,
                settings=settings(
                    max_examples=budget,
                    database=None,
                    deadline=None,
                    suppress_health_check=list(HealthCheck),
                ),
                random=random.Random(seed),
            )
    except (NoSuchExample, Exception):  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def _dedupe(inputs: list) -> list:
    seen = set()
    out = []
    for x in inputs:
        try:
            key = json.dumps(x, sort_keys=True, default=repr)
        except Exception:  # noqa: BLE001
            key = repr(x)
        if key in seen:
            continue
        seen.add(key)
        out.append(x)
    return out


def _target(job: dict, impl: Impl | None) -> Impl:
    """The function under test, loaded (raises LoadError if it does not import)."""
    if impl is None:
        raise RuntimeError("this job needs the code under test, but no adapter was configured")
    impl.load(job.get("target", {}).get("source"))
    return impl


def job_drt(job: dict, harness: HarnessClient, impl: Impl | None = None) -> dict:
    t0 = time.monotonic()
    model_only = bool(job.get("model_only"))
    fn = None if model_only else _target(job, impl)
    ev = Evaluator(job, harness, fn)
    strategy, note = build_strategy(job, ev.params)
    seed = int(job.get("seed", 0))
    n = int(job.get("max_examples", 500))
    edge = [list(e) for e in job.get("edge_cases", []) if isinstance(e, (list, tuple))]
    inputs = _dedupe(edge + collect_inputs(strategy, n, seed))
    counts: dict[str, int] = {}
    failures: list[dict] = []
    model_violations: list[dict] = []
    agreeing: list[dict] = []
    slow_models: list = []
    stopped_early = None
    deadline = t0 + float(job.get("time_budget", 600))
    for args in inputs:
        rec = ev.evaluate(args, with_impl=not model_only)
        k = rec["kind"]
        counts[k] = counts.get(k, 0) + 1
        if rec.get("model_violates"):
            if len(model_violations) < 10:
                model_violations.append(rec)
        if k in DIVERGENT or rec.get("impl_violates"):
            if len(failures) < 25:
                failures.append(rec)
        if k in ("agree", "model_only", "value", "crash", "timeout", "type") and "model_json" in rec:
            # Valid inputs with a model answer; used for the vacuity check below.
            # Divergent inputs are kept too: they are often exactly where a
            # conditional spec's premise holds.
            if len(agreeing) < 400:
                agreeing.append(rec)
        if ev.timeouts >= int(job.get("max_timeouts", 4)):
            stopped_early = f"stopped after {ev.timeouts} timeouts (implementation appears to hang)"
            break
        if k == "model_timeout":
            slow_models.append(args)
            if len(slow_models) >= int(job.get("max_model_timeouts", 3)):
                stopped_early = "the Lean model is too slow on some inputs"
                break
        if time.monotonic() > deadline:
            stopped_early = "time budget exhausted"
            break

    result: dict = {
        "ok": True,
        "counts": counts,
        "inputs": len(inputs),
        "strategy_note": note,
        "stopped_early": stopped_early,
        "failures": failures,
        "model_violations": model_violations,
        "slow_model_inputs": [", ".join(safe_repr(a) for a in x) for x in slow_models[:3]],
    }

    # Minimal counterexamples (hypothesis shrinking) for the report.
    if job.get("shrink", True) and time.monotonic() < deadline:
        budget = int(job.get("shrink_budget", 300))
        if failures and not model_only:
            first = failures[0]
            target_kind = first["kind"]
            viol = bool(first.get("impl_violates"))

            def is_failure(a):
                r = ev.evaluate(a)
                if viol:
                    return bool(r.get("impl_violates"))
                return r["kind"] == target_kind

            if target_kind != "timeout":
                m = shrink(strategy, is_failure, budget, seed)
                if m is not None:
                    rec = ev.evaluate(m)
                    rec["shrunk"] = True
                    result["minimal"] = rec
        if model_violations:
            names = set(model_violations[0]["model_violates"])

            def model_fails(a):
                r = ev.evaluate(a, with_impl=False)
                return bool(set(r.get("model_violates", [])) & names)

            m = shrink(strategy, model_fails, budget, seed + 1)
            if m is not None:
                rec = ev.evaluate(m, with_impl=False)
                rec["shrunk"] = True
                result["minimal_model_violation"] = rec

    # Vacuity: does each postcondition reject perturbed versions of correct outputs?
    if job.get("vacuity") and ev.posts and agreeing:
        rng = random.Random(seed)
        stats = {p: {"tried": 0, "rejected": 0} for p in ev.posts}
        # Prefer a mix: divergent records first (rare premises), then a spread of others.
        sample = [r for r in agreeing if r["kind"] != "agree" and r["kind"] != "model_only"][:40]
        rest = [r for r in agreeing if r["kind"] in ("agree", "model_only")]
        step_ = max(1, len(rest) // 120)
        sample += rest[::step_][:120]
        for rec in sample:
            mv = ev.decode_model(rec["model_json"])
            if isinstance(mv, dict) and "raises" in mv:
                continue
            for alt in perturb(mv, ev.ret, rng)[:4]:
                try:
                    enc_alt = encode(alt, ev.ret)
                except EncodeError:
                    continue
                r2 = ev.evaluate(rec["args"], impl_override={"ok": enc_alt} if ev.exceptions else enc_alt)
                for p, ok in (r2.get("post_impl") or {}).items():
                    stats[p]["tried"] += 1
                    if not ok:
                        stats[p]["rejected"] += 1
        result["vacuity"] = stats

    result["elapsed"] = time.monotonic() - t0
    return result


def job_probe(job: dict, harness: HarnessClient, impl: Impl | None = None) -> dict:
    ev = Evaluator(job, harness, _target(job, impl))
    return {"ok": True, "records": [ev.evaluate(list(a)) for a in job.get("inputs", [])]}


def job_mutants(job: dict, harness: HarnessClient, result_path: str, impl: Impl | None = None) -> dict:
    t0 = time.monotonic()
    ev = Evaluator(job, harness, None)
    strategy, note = build_strategy(job, ev.params)
    seed = int(job.get("seed", 0))
    edge = [list(e) for e in job.get("edge_cases", []) if isinstance(e, (list, tuple))]
    inputs = _dedupe(edge + collect_inputs(strategy, int(job.get("max_examples", 300)), seed))
    valid = []
    for a in inputs:
        r = ev.evaluate(a, with_impl=False)
        if r["kind"] == "model_only":
            valid.append(list(a))
    # Extra inputs (different seed, both generators) used only for surviving mutants.
    extra: list = []
    n_extra = int(job.get("survivor_examples", 1000))
    if n_extra:
        typed, _ = build_strategy(job, ev.params, which="typed")
        pool = _dedupe(collect_inputs(strategy, n_extra // 2, seed + 101) + collect_inputs(typed, n_extra // 2, seed + 202))
        for a in pool:
            r = ev.evaluate(a, with_impl=False)
            if r["kind"] == "model_only":
                extra.append(list(a))
    out: list[dict] = []
    stream = open(result_path + ".partial", "a")
    done_ids = set(job.get("skip_ids", []))
    for m in job["mutants"]:
        if m["id"] in done_ids:
            continue
        res = {"id": m["id"], "killed": False, "by": None, "specs": [], "counterexample": None}
        if impl is None:
            raise RuntimeError("mutation analysis needs the code under test, but no adapter was configured")
        try:
            impl.load(m["source"])
        except LoadError as e:
            # Not a detected bug: the mutant is not a valid program in the target runtime.
            res.update(invalid=True, by="load_error", detail=str(e)[:200])
            out.append(res)
            stream.write(json.dumps(res) + "\n")
            stream.flush()
            continue
        ev.fn = impl
        ev.timeouts = 0
        specs: set[str] = set()
        for a in valid:
            rec = ev.evaluate(a)
            k = rec["kind"]
            specs.update(rec.get("impl_violates", []))
            if (k in DIVERGENT) and not res["killed"]:
                res["killed"] = True
                res["by"] = k
                res["counterexample"] = {
                    "args_repr": rec.get("args_repr"),
                    "impl": rec.get("impl"),
                    "model": rec.get("model"),
                }
            if res["killed"] and (len(specs) == len(ev.posts) or ev.timeouts >= 2 or k == "timeout"):
                break
        res["specs"] = sorted(specs)
        if not res["killed"] and extra:
            # Survivor: search harder before calling it (likely) equivalent.
            for a in extra:
                rec = ev.evaluate(a)
                if rec["kind"] in DIVERGENT:
                    res.update(killed=True, by=rec["kind"], extended=True, counterexample={
                        "args_repr": rec.get("args_repr"), "impl": rec.get("impl"), "model": rec.get("model")})
                    res["specs"] = sorted(set(res["specs"]) | set(rec.get("impl_violates", [])))
                    break
            else:
                res["likely_equivalent"] = True
        out.append(res)
        stream.write(json.dumps(res) + "\n")
        stream.flush()
    stream.close()
    return {"ok": True, "valid_inputs": len(valid), "mutants": out, "strategy_note": note, "elapsed": time.monotonic() - t0}


def job_examples(job: dict, harness: HarnessClient, impl: Impl | None = None) -> dict:
    """Check documented examples (args -> expected, or "raises") against the model
    and, unless model_only, against the implementation."""
    model_only = bool(job.get("model_only"))
    fn = None if model_only else _target(job, impl)
    ev = Evaluator(job, harness, fn)
    out = []
    for ex in job.get("examples", []):
        args = list(ex.get("args", []))
        exp = ex.get("expected")
        raises = exp == "raises"
        rec: dict = {"args_repr": ", ".join(safe_repr(a) for a in args), "expected": "raises" if raises else safe_repr(exp)}
        enc = ev.encode_args(args)
        if enc is None:
            rec["skipped"] = "arguments do not fit the parameter types"
            out.append(rec)
            continue
        exp_json = None
        if not raises:
            try:
                exp_json = encode(exp, ev.ret)
            except EncodeError as e:
                rec["skipped"] = f"expected value does not fit the return type: {e}"
                out.append(rec)
                continue
        try:
            resp = harness.request({"op": "case", "args": enc})
        except HarnessError as e:
            rec["skipped"] = f"model evaluation failed: {e}"
            out.append(rec)
            continue
        if not resp.get("pre"):
            rec["skipped"] = "outside the precondition"
            out.append(rec)
            continue
        mj = resp.get("model")
        model_raises = ev.exceptions and isinstance(mj, dict) and "error" in mj
        model_val = mj.get("ok") if (ev.exceptions and isinstance(mj, dict)) else mj
        rec["model"] = ev.model_repr(mj)
        rec["model_ok"] = (raises and model_raises) or (not raises and not model_raises and model_val == exp_json)
        if fn is not None:
            r = fn.call(args, ev.call_timeout)
            if r["status"] == "ok":
                rec["impl"] = safe_repr(r["value"])
                try:
                    rec["impl_ok"] = (not raises) and encode(r["value"], ev.ret) == exp_json
                except EncodeError:
                    rec["impl_ok"] = False
            elif r["status"] == "exception":
                rec["impl"] = f"raised {r['exc']}"
                rec["impl_ok"] = raises
            else:
                rec["impl"] = f"did not return within {ev.call_timeout:g}s"
                rec["impl_ok"] = False
        out.append(rec)
    return {"ok": True, "examples": out}


def job_props(job: dict, harness: HarnessClient) -> dict:
    from hypothesis import strategies as st

    t0 = time.monotonic()
    seed = int(job.get("seed", 0))
    n = int(job.get("max_examples", 300))
    results = {}
    for prop in job.get("props", []):
        types_ = [parse_type(p["lean_type"]) for p in prop["params"]]
        strat = st.tuples(*[strategy_for(t) for t in types_]) if types_ else st.just(())

        def holds(a, _name=prop["name"], _types=types_):
            try:
                enc = [encode(x, t) for x, t in zip(a, _types)]
            except EncodeError:
                return None
            try:
                r = harness.request({"op": "prop", "name": _name, "args": enc})
            except HarnessError:
                return None
            if "error" in r:
                return None
            return bool(r.get("holds"))

        tested, failed_first = 0, None
        for a in collect_inputs(strat, n, seed):
            h = holds(a)
            if h is None:
                continue
            tested += 1
            if h is False:
                failed_first = a
                break
        entry: dict = {"tested": tested, "holds": failed_first is None}
        if failed_first is not None:
            m = shrink(strat, lambda a: holds(a) is False, 200, seed)
            ce = m if m is not None else failed_first
            entry["counterexample"] = ", ".join(safe_repr(x) for x in ce)
        results[prop["name"]] = entry
    return {"ok": True, "props": results, "elapsed": time.monotonic() - t0}


def main(argv: list[str]) -> int:
    job_path, result_path = argv[1], argv[2]
    job = json.loads(Path(job_path).read_text())
    # Protect our result channel from user code: stdout/stderr -> log file, stdin -> null.
    log = os.open(job.get("log_path", os.devnull), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    null_in = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null_in, 0)
    os.dup2(log, 1)
    os.dup2(log, 2)
    sys.dont_write_bytecode = True
    h = job["harness"]
    harness = HarnessClient(h["cmd"], h["env"], h["cwd"], timeout=float(h.get("timeout", 10.0)), stderr_path=job.get("log_path"))
    impl = Impl(job) if job.get("impl") else None
    try:
        harness.start()
        kind = job["kind"]
        if kind == "drt":
            res = job_drt(job, harness, impl)
        elif kind == "mutants":
            res = job_mutants(job, harness, result_path, impl)
        elif kind == "props":
            res = job_props(job, harness)
        elif kind == "probe":
            res = job_probe(job, harness, impl)
        elif kind == "examples":
            res = job_examples(job, harness, impl)
        else:
            res = {"ok": False, "error": f"unknown job kind {kind}"}
    except LoadError as e:
        res = {"ok": False, "error": f"the code under test could not be loaded: {e}", "load_error": e.info}
    except BaseException as e:  # noqa: BLE001
        res = {"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()[-3000:]}
    finally:
        harness.close()
        if impl is not None:
            impl.close()
    tmp = result_path + ".tmp"
    Path(tmp).write_text(json.dumps(res, default=repr))
    os.replace(tmp, result_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
