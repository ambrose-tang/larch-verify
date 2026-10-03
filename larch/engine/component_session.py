"""Verification of a stateful component (a class): the counterpart of session.py for
functions. Same stages, same report; the model is a Lean state machine and the tests
are call sequences (see larch/component.py and larch/engine/seqdriver.py)."""
from __future__ import annotations

import difflib
import json
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from ..component import ComponentSpec, Invariant, Observer, Operation, StepContract, harness_text, module_text
from ..config import Config
from ..lang import ComponentInfo, ExtractError, RuntimeEnvError, language_for
from ..lean.checker import Checker
from ..lean.lint import lint_lean
from ..lean.toolchain import find_toolchain
from ..lean.workspace import LeanWorkspace
from ..llm.base import LLM, BudgetExceeded, Ledger, LLMError, LLMRequest, UsageLimitError
from ..llm.providers import make_provider
from ..prompts import COMPONENT_SCHEMA, FIX_SCHEMA, FIX_SYSTEM, FORMALIZE_REPAIR, PYTHON_TYPE_GUIDE, component_system, component_user
from ..report import FixProposal, MutationSummary, ProofResult, Report, SpecResult
from ..spec import Param
from ..ui import UI
from ..util import slug, unescape_code
from .context import RunContext
from .findings import adjudicate, classify, count_kind, make_finding
from .formalize import FormalizeError, Sanity, _code_only, _numbered
from .prove import finalize_proofs, prove_all
from .runner import JobRunner, WorkerError
from .session import _first_line, _write_proofs, artifacts_root
from .store import load_approved, save_approved

DIVERGENT = ("value", "crash", "timeout", "type")


def verify_component(path: Path, name: str, cfg: Config, ui: UI | None = None, *, llm: LLM | None = None,
                     subject=None, spec_override: ComponentSpec | None = None) -> Report:
    ui = ui or UI()
    t_start = time.monotonic()
    path = Path(path)
    report = Report(function=name, file=str(path), config=cfg.to_dict())
    report.kind = "component"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    artifacts_root(cfg).mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=f"{stamp}-{slug(path.stem, 20)}-{slug(name, 30)}-", dir=artifacts_root(cfg)))
    report.artifacts_dir = str(run_dir)
    ledger = llm.ledger if llm else Ledger(cfg.budget_usd)
    ctx: RunContext | None = None
    try:
        lang = language_for(path)
        if lang is None:
            raise ExtractError(f"{path.suffix or path.name}: unsupported file type")
        info = lang.extract_class(path, name)
        from ..repo import numeric_problem

        prob = numeric_problem(info)
        if prob:
            raise ExtractError(f"{name} {prob}.")
        report.language = lang.name
        report.line = info.lineno
        runtime = lang.runtime(info, cfg)
        report.runtime = runtime.describe()
        runner = JobRunner(runtime, run_dir / "py")
        tc = find_toolchain()
        llm = llm or LLM(make_provider(cfg.provider, cache=cfg.cache), ledger)
        ws = LeanWorkspace(run_dir / "lean", tc)
        ctx = RunContext(cfg=cfg, llm=llm, tc=tc, ws=ws, runner=runner, checker=Checker(tc), info=info, ui=ui,
                         run_dir=run_dir, lang=lang, contracts=list(subject.contracts) if subject else [], subject=subject)
        ui.header(target=f"{path}::{name}", model=f"{cfg.model} via {llm.provider.describe()}", lean=tc.version,
                  runtime=runtime.describe())
        with ui.step(f"Read {path.name}::{name}") as st:
            st.update(f"loading with {runtime.display}")
            chk = runner.check()
            if not chk.get("ok"):
                st.done("cannot load the code in the project's environment", status="fail")
                raise RuntimeEnvError(lang.explain_load_error(runtime, info, chk))
            st.done(f"{info.signature} · {runtime.display}")
            for w in info.warnings + runtime.warnings:
                st.line(f"[yellow]{w}[/]")
                report.warnings.append(w)
        _run(ctx, report, spec_override)
    except (RuntimeEnvError, ExtractError) as e:
        report.verdict, report.error = "error", str(e)
        report.headline = f"Cannot verify {name}."
    except FormalizeError as e:
        report.verdict = "error"
        report.error = f"{e}: " + "; ".join(" ".join(ln.strip() for ln in p.splitlines()[:4] if ln.strip())[:300] for p in e.problems[:3])
        report.headline = "Could not formalize this class."
    except UsageLimitError:
        raise
    except KeyboardInterrupt:
        report.verdict, report.error, report.headline = "error", "interrupted", "Interrupted."
    except Exception as e:  # noqa: BLE001
        import traceback

        report.verdict, report.error, report.headline = "error", f"{type(e).__name__}: {e}", "Larch hit an internal error."
        (run_dir / "error.txt").write_text(traceback.format_exc())
    report.elapsed_s = time.monotonic() - t_start
    report.cost_usd = ledger.spent()
    report.nominal_cost_usd = ledger.nominal()
    report.llm_calls = len(ledger.calls)
    report.cost_by_stage = ledger.by_stage()
    if ctx is not None:
        report.stage_seconds = {k: round(v, 2) for k, v in ctx.stage_seconds.items()}
    report.save(run_dir)
    return report


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def job_base(ctx: RunContext, spec: ComponentSpec) -> dict:
    return {
        "harness": {"cmd": ctx.ws.harness_cmd(), "env": ctx.ws.harness_env(), "cwd": str(ctx.ws.root), "timeout": 5.0},
        "target": {"path": str(ctx.info.path), "function": ctx.info.name},
        "component": {
            "name": spec.component,
            "init_params": [p.lean_type for p in spec.init_params],
            "operations": [{"method": o.method, "params": [p.lean_type for p in o.params], "returns": o.returns,
                            "opaque": [i + 1 for i, f in enumerate(spec.http.get(o.method, {}).get("fields", [])) if f.get("opaque")]}
                           for o in spec.operations],
            "observers": [{"name": o.name, "lean_type": o.lean_type, "access": o.access} for o in spec.observers],
        },
        "strategy": {"code": spec.strategy_code},
        **({"impl": ctx.service_impl} if getattr(ctx, "service_impl", None) else {}),
        "exhaustive_domains": spec.exhaustive_domains,
        "seed": ctx.cfg.seed,
        "call_timeout": ctx.cfg.call_timeout,
        "max_steps": ctx.cfg.max_steps,
    }


def run_seq(ctx: RunContext, spec: ComponentSpec, *, n: int, model_only: bool = False, source: str | None = None,
            exhaustive: bool = False, seed: int | None = None, shrink: bool = True, cases: list | None = None) -> dict:
    job = job_base(ctx, spec)
    job.update(kind="seq", max_examples=n, model_only=model_only, exhaustive=exhaustive, shrink=shrink,
               exhaustive_limit=ctx.cfg.exhaustive_sequences)
    if cases is not None:
        job.update(cases=cases, shrink=False, exhaustive=False)
    if seed is not None:
        job["seed"] = seed
    if source is not None:
        job["target"] = dict(job["target"], source=source)
    try:
        return ctx.runner.run(job, timeout=1800)
    except WorkerError as e:
        return {"ok": False, "error": str(e)}


def exhaustive_possible(spec: ComponentSpec) -> bool:
    doms = spec.exhaustive_domains or {}
    return bool(doms) and all(o.method in doms for o in spec.operations) and ("init" in doms or not spec.init_params)


# ---------------------------------------------------------------------------
# Formalization
# ---------------------------------------------------------------------------

def spec_from_data(ctx: RunContext, data) -> tuple[ComponentSpec | None, list[str]]:
    info: ComponentInfo = ctx.info  # type: ignore[assignment]
    if not isinstance(data, dict):
        return None, ["the response was not a JSON object"]
    problems: list[str] = []
    ctor = data.get("constructor") or {}
    expected = [p.lean_name for p in info.params]
    got = [str(p.get("name", "")) for p in ctor.get("params") or [] if isinstance(p, dict)]
    if got != expected:
        return None, [f"constructor params must be exactly {expected} in this order (got {got})"]
    init_params = [Param(p.lean_name, str(rp.get("lean_type", "")).strip(), p.name) for p, rp in zip(info.params, ctor.get("params") or [])]
    pre = ctor.get("precondition") or {}
    public = {m.name for m in info.methods}
    ops = []
    for o in data.get("operations") or []:
        if not isinstance(o, dict):
            continue
        method = str(o.get("method", "")).strip()
        if method not in public:
            problems.append(f"operation `{method}` is not a public method of {info.name} (methods: {sorted(public)})")
            continue
        ops.append(Operation(method, [Param(str(p.get("name", "")), str(p.get("lean_type", "")).strip())
                                      for p in o.get("params") or [] if isinstance(p, dict)], str(o.get("returns", "Unit")).strip() or "Unit"))
    # How an observer is read follows from the class itself: a method is called, a
    # property/getter or field is read. It must exist: an invented observer makes every
    # sequence fail at its first read.
    kinds = {m.name: m.kind for m in info.methods}
    fields = public_fields(info)
    observers = []
    for o in data.get("observers") or []:
        if not isinstance(o, dict):
            continue
        name = str(o.get("name", "")).strip()
        if name not in kinds and name not in fields:
            problems.append(f"observer `{name}` is not a public method, property or attribute of {info.name} (it has: "
                            f"{', '.join(sorted(set(kinds) | fields)) or 'none'}); observe only what the class exposes, "
                            "or use no observers")
            continue
        access = {"method": "call", "property": "attribute"}.get(kinds.get(name, ""), "attribute")
        observers.append(Observer(name, str(o.get("lean_type", "")).strip(), access))
    contracts = list(ctx.contracts)
    covered: set[int] = set()

    def origin(item: dict, obj) -> None:
        try:
            k = int(item.get("contract", -1))
        except (TypeError, ValueError):
            k = -1
        if 0 <= k < len(contracts):
            covered.add(k)
            obj.origin, obj.contract = "contract", contracts[k].text
            if contracts[k].lean:
                obj.lean = contracts[k].lean

    invs, steps = [], []
    for i in data.get("invariants") or []:
        if isinstance(i, dict):
            inv = Invariant(_norm(i.get("name")), str(i.get("english", "")).strip(), unescape_code(str(i.get("lean", ""))).strip())
            origin(i, inv)
            invs.append(inv)
    for c in data.get("operation_contracts") or []:
        if isinstance(c, dict):
            stc = StepContract(_norm(c.get("name")), str(c.get("english", "")).strip(), str(c.get("operation", "")).strip(),
                               unescape_code(str(c.get("lean", ""))).strip())
            origin(c, stc)
            steps.append(stc)
    for k, c in enumerate(contracts):
        if k not in covered:
            problems.append(f"the developer's contract {k} (\"{c.text}\") was not formalized: give it an invariant or an "
                            f"operation contract with \"contract\": {k}")
    try:
        doms = json.loads(data.get("exhaustive_domains") or "{}") or {}
        if not isinstance(doms, dict):
            doms = {}
    except (TypeError, ValueError):
        doms = {}
    spec = ComponentSpec(
        component=info.name, understanding=str(data.get("understanding", "")).strip(), init_params=init_params,
        init_pre_english=str(pre.get("english", "")).strip(), init_pre_lean=unescape_code(str(pre.get("lean", "True"))).strip() or "True",
        operations=ops, observers=observers,
        model_code=_strip_ns(unescape_code(str(data.get("model", "")))), invariants=invs, steps=steps,
        strategy_code=unescape_code(_code_only(str(data.get("input_generator", "")))),
        exhaustive_domains={k: v for k, v in doms.items() if isinstance(v, list) and v},
        notes=str(data.get("notes", "")), contracts=[c.text for c in contracts],
    )
    problems += spec.validate()
    return spec, problems


def public_fields(info) -> set[str]:
    """Public instance attributes the class assigns (`self.x = ...` / `this.x = ...`, or
    class-level field declarations in TypeScript)."""
    import re as _re

    names = set(_re.findall(r"\b(?:self|this)\.([A-Za-z][A-Za-z0-9_]*)\s*(?::[^=\n]*)?=(?!=)", info.source))
    if info.language != "python":
        names |= set(_re.findall(r"^\s*(?:public\s+|readonly\s+)*([A-Za-z][A-Za-z0-9_]*)\s*[:=;]", info.source, _re.M))
    return {n for n in names if not n.startswith("_")}


def _norm(name) -> str:
    base = re.sub(r"[^a-z0-9_]+", "_", str(name or "").lower()).strip("_")
    for prefix in ("spec_", "inv_", "post_"):  # Larch adds these itself
        if base.startswith(prefix) and len(base) > len(prefix):
            base = base[len(prefix):]
    return (base if base and base[0].isalpha() else f"c_{base}" if base else "contract")[:40]


def _strip_ns(code: str) -> str:
    return "\n".join(ln for ln in code.strip().splitlines() if ln.strip() not in ("namespace Larch", "end Larch", "open Larch"))


def build(ctx: RunContext, spec: ComponentSpec) -> list[str]:
    problems = [f"model code {i}" for i in lint_lean(spec.model_code, model_file=True)]
    text = "\n".join([spec.init_pre_lean] + [i.lean for i in spec.invariants] + [s.lean for s in spec.steps])
    problems += [f"contract {i}" for i in lint_lean(text, model_file=True)]
    if problems:
        return problems
    mod = module_text(spec)
    res = ctx.ws.compile_module(ctx.ws.MODEL, mod)
    if not res.ok:
        return ["Lean rejected the generated LarchModel.lean:\n" + res.error_text()
                + "\n\nThe generated file, with line numbers:\n```lean\n" + _numbered(mod) + "\n```"]
    h = harness_text(spec)
    hres = ctx.ws.check_text(h, stem="HarnessCheck")
    if not hres.ok:
        err = hres.error_text()
        hint = ("\nHint: every invariant and contract must be decidable (no `match`, bounded quantifiers only); "
                "results and observers need ToJson (use Int, Nat, Bool, String, List, Option, × only).")
        return ["The test harness generated from your model does not compile:\n" + err + hint]
    ctx.ws.write(ctx.ws.HARNESS, h)
    return []


def sanity(ctx: RunContext, spec: ComponentSpec) -> tuple[list[str], Sanity]:
    san = Sanity()
    res = run_seq(ctx, spec, n=min(400, ctx.cfg.sequences), model_only=True)
    if not res.get("ok"):
        return [f"testing the model failed: {res.get('error')}"], san
    c = res.get("counts", {})
    san.valid_inputs = c.get("model_only", 0)
    san.pre_false = c.get("pre_false", 0)
    san.domain_errors = c.get("domain", 0)
    if res.get("strategy_note"):
        san.warnings.append(res["strategy_note"])
    problems = []
    if c.get("harness_error"):
        problems.append("evaluating the model failed on some call sequences (check argument types)")
    if c.get("model_timeout"):
        problems.append("evaluating the model is too slow on some sequences: keep every operation cheap")
    mv = res.get("minimal_model_violation") or (res.get("model_violations") or [None])[0]
    if mv:
        problems.append(f"The model violates its own contract(s) {mv.get('model_violates')} after:\n{mv.get('args_repr')}\n"
                        "Either the model or the contract is wrong.")
    if san.valid_inputs == 0 and not problems:
        problems.append("no generated call sequence satisfies the constructor precondition")
    if problems:
        return problems, san
    impl = run_seq(ctx, spec, n=100, shrink=False)
    if impl.get("ok"):
        ci = impl.get("counts", {})
        san.impl_disagreements = sum(ci.get(k, 0) for k in DIVERGENT)
        total = san.impl_disagreements + ci.get("agree", 0)
        fails = impl.get("failures") or []
        if total >= 20 and san.impl_disagreements >= 0.9 * total and fails:
            from collections import Counter

            group, n = Counter(f.get("group") for f in fails).most_common(1)[0]
            if n >= 0.9 * len(fails):
                f = next(x for x in fails if x.get("group") == group)
                # The class and the model disagree the same way on nearly every sequence:
                # the model does not describe this class (an invented observer, a wrong
                # constructor or return shape). Repair now instead of testing for minutes.
                return [f"The real class disagrees with your model on {san.impl_disagreements} of {total} quick-test "
                        f"sequences, all in the same way, for example:\n{f.get('args_repr')}\nclass: {f.get('impl')}; "
                        f"model: {f.get('model')}" + (f" ({f['detail']})" if f.get("detail") else "") +
                        "\nA disagreement this uniform means the model does not describe this class. Model what the "
                        "class actually exposes and returns."], san
    return [], san


def formalize(ctx: RunContext, *, feedback: str | None = None, previous: ComponentSpec | None = None, step=None):
    cfg = ctx.cfg
    base = component_user(ctx.info, ctx.contracts)
    prompt = base
    if feedback and previous is not None:
        prompt += f"\n## Reviewer feedback on your previous formalization\n{feedback}\n\nPrevious model:\n```lean\n{previous.model_code}\n```\n"
    last: list[str] = []
    for attempt in range(cfg.formalize_repairs + 1):
        if step:
            step.update("writing the state-machine model" if attempt == 0 else f"repairing (round {attempt})")
        resp = ctx.ask(LLMRequest(system=component_system(ctx.lang.type_guide), prompt=prompt, model=cfg.model,
                                  stage="formalize", effort=cfg.effort, json_schema=COMPONENT_SCHEMA))
        spec, problems = spec_from_data(ctx, resp.data)
        san = Sanity()
        if spec is not None and not problems:
            if step:
                step.update("compiling Lean model")
            problems = build(ctx, spec)
        if spec is not None and not problems:
            if step:
                step.update("testing the model against its contracts")
            problems, san = sanity(ctx, spec)
        if spec is not None and not problems:
            if step:
                step.update("checking the contracts say what you wrote")
            problems = fidelity_problems(ctx, spec)
            if not problems or attempt == cfg.formalize_repairs:
                ctx.fidelity_warnings = problems  # type: ignore[attr-defined]
                return spec, san, attempt + 1
        last = problems
        prev = json.dumps(resp.data, indent=2, ensure_ascii=False) if isinstance(resp.data, dict) else str(resp.data)
        prompt = base + "\n" + FORMALIZE_REPAIR.format(previous="```json\n" + prev[:12000] + "\n```",
                                                       problems="\n".join(f"- {p}" for p in problems))
    raise FormalizeError("could not produce a consistent state-machine model", last)


def fidelity_problems(ctx: RunContext, spec: ComponentSpec) -> list[str]:
    """The developer's contracts whose formal reading does not mean what they wrote
    (narrowed, widened or changed), as judged by a separate review call."""
    from ..prompts import FIDELITY_SCHEMA, FIDELITY_SYSTEM, fidelity_user

    mine = [x for x in list(spec.invariants) + list(spec.steps) if x.origin == "contract" and x.contract]
    if not mine:
        return []
    try:
        resp = ctx.ask(LLMRequest(system=FIDELITY_SYSTEM, prompt=fidelity_user([(x.contract, x.english, x.lean) for x in mine]),
                                  model=ctx.cfg.model, stage="fidelity", effort=ctx.cfg.effort, json_schema=FIDELITY_SCHEMA))
    except (LLMError, BudgetExceeded):
        return []
    items = (resp.data or {}).get("items") if isinstance(resp.data, dict) else None
    out = []
    for it in items or []:
        try:
            x = mine[int(it.get("index", -1))]
        except (ValueError, TypeError, IndexError):
            continue
        if it.get("faithful") is False and str(it.get("problem", "")).strip():
            out.append(f"`{x.name}` does not say what the developer wrote (\"{x.contract}\"): {str(it['problem']).strip()}. "
                       "Follow the developer's words, even where the code does something else.")
    return out


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

def _sequence_test(ctx: RunContext, spec: ComponentSpec, what: str) -> tuple[dict, dict | None]:
    cfg, ui = ctx.cfg, ctx.ui
    exhaustive = exhaustive_possible(spec)
    with ui.step(f"Sequence test: the {what} vs. the model") as st, ctx.timed("test"):
        res = run_seq(ctx, spec, n=cfg.sequences)
        if not res.get("ok"):
            raise RuntimeError(f"sequence testing failed: {res.get('error')}")
        exh = run_seq(ctx, spec, n=0, exhaustive=True) if exhaustive else None
        if exh is not None and exh.get("ok"):
            res["failures"] = (exh.get("failures") or []) + res.get("failures", [])
            if exh.get("minimal") and not res.get("minimal"):
                res["minimal"] = exh["minimal"]
            for k, v in exh.get("counts", {}).items():
                res["counts"][k] = res["counts"].get(k, 0) + v
        c = res.get("counts", {})
        valid = sum(c.get(k, 0) for k in ("agree",) + DIVERGENT)
        dis = sum(c.get(k, 0) for k in DIVERGENT)
        st.done(f"{valid:,} call sequences · {dis} disagreements", status="ok" if dis == 0 else "warn")
        if exh is not None and exh.get("ok"):
            st.line(f"every sequence of up to {exh.get('depth')} calls over the small domains: {exh.get('inputs'):,} sequences")
        if res.get("strategy_note"):
            st.line(f"[yellow]{res['strategy_note']}[/]")
    return res, exh


def revise_model(ctx: RunContext, spec: ComponentSpec, issues: list[str]) -> tuple[ComponentSpec | None, str]:
    """A corrected model (same operations, observers and contracts), compiled and checked
    against the contracts on random sequences; None if no acceptable revision was found."""
    from ..prompts import COMPONENT_MODEL_REPAIR_SCHEMA, component_model_repair_user

    problems: list[str] = []
    for _ in range(2):
        prompt = component_model_repair_user(ctx.info, module_text(spec), issues, problems)
        try:
            resp = ctx.ask(LLMRequest(system=component_system(ctx.lang.type_guide if ctx.lang else PYTHON_TYPE_GUIDE),
                                      prompt=prompt, model=ctx.cfg.model, stage="model-repair", effort=ctx.cfg.effort,
                                      json_schema=COMPONENT_MODEL_REPAIR_SCHEMA))
        except (LLMError, BudgetExceeded):
            return None, ""
        d = resp.data if isinstance(resp.data, dict) else {}
        new = ComponentSpec.from_json(spec.to_json())
        new.model_code = _strip_ns(unescape_code(str(d.get("model", ""))))
        problems = new.validate() or build(ctx, new)
        if not problems:
            problems, _ = sanity(ctx, new)
        if not problems:
            return new, str(d.get("explanation", "")).strip() or "model revised"
    return None, ""


def _run(ctx: RunContext, report: Report, spec_override: ComponentSpec | None) -> None:
    cfg, ui = ctx.cfg, ctx.ui
    spec = spec_override
    reused = False
    if spec is None and not cfg.extra.get("fresh"):
        saved = load_approved(ctx.info)
        if saved is not None and (getattr(saved[0], "kind", "") != "component" or saved[0].contracts != [c.text for c in ctx.contracts]):
            saved = None
        if saved is not None and (cfg.auto_approve or cfg.extra.get("reuse") or ui.confirm(
                f"Reuse the specification you approved on {saved[1]}?", default=True)):
            spec, reused = saved[0], True
    if spec is not None:
        with ui.step("Load approved specification") as st, ctx.timed("formalize"):
            problems = build(ctx, spec)
            if problems:
                raise FormalizeError("the saved specification no longer compiles", problems)
            st.done(f"{len(spec.spec_names())} contracts" + (" (previously approved)" if reused else ""))
    else:
        feedback = previous = None
        for _ in range(4):
            with ui.step("Formalize: write Lean state-machine model and contracts") as st, ctx.timed("formalize"):
                spec, san, rounds = getattr(ctx, "hooks", {}).get("formalize", formalize)(ctx, feedback=feedback, previous=previous, step=st)
                st.done(f"{len(spec.operations)} operations, {len(spec.active_props())} invariants, "
                        f"{len(spec.active_posts())} operation contracts" + (f" · {rounds} rounds" if rounds > 1 else ""))
                st.line(f"model compiles · {san.valid_inputs} call sequences, no contract violated by the model")
                for w in getattr(ctx, "fidelity_warnings", None) or []:
                    st.line(f"[yellow]⚠ {w}[/]")
                    report.warnings.append("possible mistranslation: " + w)
                if san.impl_disagreements:
                    st.line(f"[yellow]the class already disagrees with the model on {san.impl_disagreements} of 100 quick-test sequences[/]")
            if cfg.auto_approve:
                for x in spec.invariants + spec.steps:
                    x.status = "auto-approved"
                ui.show_spec(spec, san.as_dict(), auto=True)
                break
            decision = ui.review(spec, san.as_dict())
            if decision.abort:
                report.verdict, report.error = "error", "specification not approved"
                report.headline = "Stopped at spec review: nothing is verified until a person approves the contracts (or --yes is given)."
                return
            if decision.feedback:
                feedback, previous = decision.feedback, spec
                continue
            for x in spec.invariants + spec.steps:
                x.status = "rejected" if x.name in decision.rejected else "approved"
            from .session import _write_back

            _write_back(ctx, spec, report)
            break
        if not spec.spec_names():
            report.verdict, report.error, report.headline = "error", "all contracts were rejected", "Nothing to verify."
            return
        if any(x.status == "rejected" for x in spec.invariants + spec.steps):
            problems = build(ctx, spec)
            if problems:
                raise FormalizeError("model no longer compiles after removing rejected contracts", problems)
        save_approved(ctx.info, spec, auto=cfg.auto_approve)
    report.understanding = spec.understanding
    report.precondition = spec.init_pre_english
    (ctx.run_dir / "spec.json").write_text(json.dumps(spec.to_json(), indent=2, ensure_ascii=False))

    # ---- sequence testing (revising the model when it, not the code, is wrong) -----------------
    what = "service" if getattr(ctx, "service_impl", None) else "class"
    for repair_round in range(cfg.model_repairs + 1):
        res, exh = _sequence_test(ctx, spec, what)
        c = res.get("counts", {})
        valid = sum(c.get(k, 0) for k in ("agree",) + DIVERGENT)
        dis = sum(c.get(k, 0) for k in DIVERGENT)
        report.drt = {"valid": valid, "disagreements": dis, "counts": c, "inputs": res.get("inputs", 0), "kind": "sequences",
                      "exhaustive_depth": exh.get("depth") if exh and exh.get("ok") else None,
                      "exhaustive_cases": exh.get("inputs") if exh and exh.get("ok") else 0}
        cls = classify(res)
        findings, model_issues = [], []
        if cls.unexplained:
            with ui.step("Adjudicate disagreements") as st, ctx.timed("adjudicate"):
                for r in cls.unexplained[:3]:
                    verdict, why = adjudicate(ctx, spec, r) if cfg.adjudicate else ("implementation_bug", "")
                    st.line(f"{r.get('args_repr', '').splitlines()[-1]}: {what} {r.get('impl')} vs model {r.get('model')} → [bold]{verdict.replace('_', ' ')}[/]")
                    conf = {"implementation_bug": "likely", "model_bug": None}.get(verdict, "possible")
                    if conf:
                        findings.append(make_finding(r, r["kind"], conf, why or r.get("detail", ""), count=count_kind(res, r)))
                    else:
                        model_issues.append(f"After\n```\n{r.get('args_repr')}\n```\nthe {what} gives {r.get('impl')} but the model "
                                            f"gives {r.get('model')}. Reviewer: {why}")
                st.done(f"{len(cls.unexplained[:3])} case(s)")
        if model_issues and not findings and repair_round < cfg.model_repairs:
            with ui.step("Revise model (the model, not the code, was wrong)") as st, ctx.timed("formalize"):
                new, why = revise_model(ctx, spec, model_issues)
                if new is not None:
                    spec = new
                    (ctx.run_dir / "spec.json").write_text(json.dumps(spec.to_json(), indent=2, ensure_ascii=False))
                    report.model_revisions.append(why)
                    st.done(why[:120])
                    continue
                st.done("could not revise the model", status="warn")
        for mi in model_issues:
            report.warnings.append(f"the model (not the {what}) looks wrong here: " + mi.split("Reviewer: ")[-1][:200])
        break
    for r in cls.spec_problems[:2]:
        report.warnings.append(f"contract {', '.join(r.get('model_violates', []))} fails on the model after:\n{r.get('args_repr')}")

    # ---- prove + mutation -----------------------------------------------------------------------
    mut: dict = {}
    hooks = getattr(ctx, "hooks", {})
    if not cfg.run_mutation:
        mutants = []
    elif "mutants" in hooks:
        mutants = hooks["mutants"](ctx, spec)
    else:
        mutants = ctx.lang.generate_class_mutants(ctx.info, max_mutants=cfg.mutants, seed=cfg.seed)

    def _mutation():
        t0 = time.monotonic()
        try:
            if "run_mutants" in hooks:
                mut.update(hooks["run_mutants"](ctx, spec, mutants))
            else:
                job = job_base(ctx, spec)
                job.update(kind="seq_mutants", max_examples=min(150, cfg.sequences),
                           mutants=[{"id": m.id, "source": m.module_source} for m in mutants])
                mut.update(ctx.runner.run_mutants(job, timeout_per_mutant=30.0))
        except Exception as e:  # noqa: BLE001
            mut.update({"ok": False, "error": str(e)})
        ctx.stage_seconds["mutation"] = time.monotonic() - t0

    mt = threading.Thread(target=_mutation, daemon=True) if mutants else None
    if mt:
        mt.start()
    proofs = {n: ProofResult(name=n, status="not_attempted") for n in spec.spec_names()}
    if cfg.run_proofs:
        with ui.step(f"Prove {len(spec.spec_names())} contracts in Lean") as st, ctx.timed("prove"):
            done: list[str] = []
            proofs = prove_all(ctx, spec, on_done=lambda r: (done.append(r.name), st.update(f"{len(done)}/{len(spec.spec_names())} done")))
            st.update("checking proofs (axioms, statements, kernel replay)")
            proofs = finalize_proofs(ctx, spec, proofs)
            n_ok = sum(1 for p in proofs.values() if p.status == "proved")
            st.done(f"{n_ok}/{len(proofs)} proved", status="ok" if n_ok == len(proofs) else "warn")
            for n in spec.spec_names():
                p = proofs[n]
                st.line(f"[green]✓[/] {n}  [dim]{p.method} · {p.elapsed:.0f}s[/]" if p.status == "proved"
                        else f"[yellow]✗[/] {n}  [dim]{_first_line(p.error)}[/]")
    _write_proofs(ctx, spec, proofs)
    if mt:
        with ui.step("Mutation analysis: would the tests catch injected bugs?") as st:
            mt.join()
            ms = MutationSummary()
            muts = [r for r in mut.get("mutants", []) if not r.get("invalid")]
            ms.invalid = len(mut.get("mutants", [])) - len(muts)
            by_id = {m.id: m for m in mutants}
            for r in muts:
                if r.get("killed"):
                    ms.killed += 1
                elif r.get("likely_equivalent"):
                    ms.likely_equivalent += 1
                    m = by_id.get(r["id"])
                    if m:
                        tag = " (agrees with the model: a candidate fix)" if findings else " (likely equivalent)"
                        ms.survivors.append(m.description + tag)
            ms.total = len(muts)
            ms.inputs = int(mut.get("valid_inputs") or 0)
            report.mutation = ms
            st.done(f"{ms.killed}/{ms.total} mutants detected ({ms.score:.0%})" if ms.total else "no mutants generated",
                    status="ok" if not ms.total or ms.adjusted_score >= 0.8 else "warn")
            for s in ms.survivors[:5]:
                st.line(f"[dim]survived: {s}[/]")
    report.specs = [SpecResult(name=i.name, kind="invariant", english=i.english, lean=i.lean,
                               approval=i.status if i.status != "proposed" else "approved", proof=proofs.get(i.name),
                               origin=i.origin, contract=i.contract) for i in spec.invariants] + \
                   [SpecResult(name=s.name, kind=f"operation {s.operation}", english=s.english, lean=s.lean,
                               approval=s.status if s.status != "proposed" else "approved", proof=proofs.get(s.name),
                               origin=s.origin, contract=s.contract) for s in spec.steps]
    report.findings = findings
    actionable = [f for f in findings if f.confidence in ("confirmed", "likely")]
    if actionable and cfg.propose_fixes:
        with ui.step("Propose a fix (validated against the verified model)") as st, ctx.timed("fix"):
            fix = hooks.get("fix", propose_fix)(ctx, spec, fix_examples(res))
            if fix is not None:
                patch = ctx.run_dir / "fix.patch"
                patch.write_text(fix.diff)
                fix.patch_path = str(patch)
                fixed = set(getattr(fix, "fixes", []) or [])
                next((f for f in actionable if f.args_repr in fixed), actionable[0]).fix = fix
                st.done("validated" if fix.validated else "could not validate a fix", status="ok" if fix.validated else "warn")
            else:
                st.done("no fix proposed", status="warn")

    total, proved = len(report.active_specs), report.proved
    if actionable:
        f = actionable[0]
        report.verdict = "bug"
        report.headline = f"{f.title()}: after `{f.args_repr.splitlines()[-1]}` the {what} gives {f.impl}, the model {f.model}."
        if valid and dis >= 0.5 * valid:
            report.warnings.insert(0, f"the {what} disagrees with the model on {dis:,} of {valid:,} call sequences "
                                      f"({dis / valid:.0%}). When almost every sequence disagrees, the model has usually "
                                      "misread the code: check Larch's understanding above before changing it.")
    elif dis:
        # Never "passed" with disagreements nobody could attribute.
        report.verdict = "partial"
        report.headline = (f"{proved}/{total} contracts proved; the {what} disagrees with the model on {dis:,} of {valid:,} "
                           "call sequences and Larch could not tell which of them is wrong (see below).")
    elif proved == total and total and valid:
        report.verdict = "passed"
        depth = report.drt.get("exhaustive_depth")
        extra = f", and on every sequence of up to {depth} calls over the small domains" if depth else ""
        report.headline = (f"All {total} contracts proved for every reachable state (no sorry, no axioms); the {what} agrees "
                           f"with the verified model on {valid:,} call sequences{extra}.")
    else:
        report.verdict = "partial"
        report.headline = f"{proved}/{total} contracts proved; the {what} agrees with the model on {valid:,} call sequences."


def fix_examples(res: dict) -> list[dict]:
    """One shrunk failing sequence per distinct disagreement, then other failures."""
    recs, seen = [], set()
    for r in [*(res.get("minimals") or [res.get("minimal")]), *res.get("failures", [])]:
        if r and r.get("group", id(r)) not in seen:
            seen.add(r.get("group", id(r)))
            recs.append(r)
    return recs[:4]


def fix_accepted(res: dict, exh: dict | None, targets: list[dict], run_original) -> tuple[bool, str, list[str]]:
    """Whether a fix is accepted, why, and which of the shown failures (`targets`, by
    their call sequence) it fixes. Accepted if the fixed code agrees with the model, or
    if it removes at least one shown disagreement and every remaining one is in the
    original code too, failing in exactly the same way (another bug, reported separately)."""
    exh = exh or {"ok": True, "counts": {}}
    c = dict(res.get("counts", {}))
    for k, v in (exh.get("counts") or {}).items():
        c[k] = c.get(k, 0) + v
    bad = sum(c.get(k, 0) for k in DIVERGENT)
    if not (res.get("ok") and exh.get("ok")) or c.get("agree", 0) == 0:
        return False, res.get("error") or exh.get("error") or "no sequence agreed with the model", []
    if bad == 0:
        return True, f"agrees with the verified model on {c.get('agree', 0):,} sequences", [t.get("args_repr", "") for t in targets]
    failures = (exh.get("failures") or []) + (res.get("failures") or [])
    remaining = {f.get("group") for f in failures}
    fixed_targets = [t for t in targets if t.get("group") and t.get("group") not in remaining]
    if not fixed_targets:
        return False, "the disagreements it was meant to fix remain", []
    fixed = {json.dumps(f["case"]): f.get("group") for f in failures if f.get("case")}
    if not fixed:
        return False, f"{bad} disagreement(s) remain", []
    sample = list(fixed)[:20]
    orig = run_original([json.loads(k) for k in sample])
    before = {json.dumps(f["case"]): f.get("group") for f in orig.get("failures") or [] if f.get("case")}
    known = set(before.values()) | {t.get("group") for t in targets}
    # Each remaining failure must be one the original code has too (it may fail earlier
    # there, on the bug that was fixed), and of a kind the original code shows.
    if not orig.get("ok") or any(k not in before for k in sample) or any(fixed[k] not in known for k in sample):
        return False, "the fix changes behaviour that the original code got right, or fails differently", []
    return True, (f"fixes {len(fixed_targets)} of the reported disagreements; agrees with the verified model on "
                  f"{c.get('agree', 0):,} sequences; {bad} other disagreement(s) are in the original code too "
                  "(a separate problem)"), [t.get("args_repr", "") for t in fixed_targets]


def propose_fix(ctx: RunContext, spec: ComponentSpec, recs: list[dict]) -> FixProposal | None:
    info = ctx.info
    failing = "\n\n".join(f"```\n{r.get('args_repr')}\n```\nclass: {r.get('impl')}; model: {r.get('model')}"
                          + (f" ({r['detail']})" if r.get("detail") else "") for r in recs)
    contracts = "\n".join(f"- {x.name}: {x.english}" for x in list(spec.active_props()) + list(spec.active_posts()))
    base = (f"## Class\n```{ctx.lang.fence}\n{info.source}\n```\n\n## Intended behaviour\n{spec.understanding}\nContracts "
            f"(proved about the reference model):\n{contracts}\n\n## Reference model (Lean)\n```lean\n{spec.model_code}\n```\n\n"
            f"## Failing call sequences\n{failing}\n\n## Runtime constraints\n{ctx.lang.fix_constraints(ctx.runner.runtime)}\n"
            "Return the complete fixed CLASS definition in `fixed_function`.\n")
    feedback, last = None, None
    for _ in range(2):
        try:
            resp = ctx.ask(LLMRequest(system=FIX_SYSTEM, prompt=base + (f"\n## Your previous fix was rejected\n{feedback}\n" if feedback else ""),
                                      model=ctx.cfg.model, stage="fix", effort=ctx.cfg.effort, json_schema=FIX_SCHEMA))
        except UsageLimitError:
            raise
        except (LLMError, BudgetExceeded):
            return last
        d = resp.data or {}
        fixed = str(d.get("fixed_function", "")).strip("\n")
        if not fixed.strip():
            return last
        new_source = ctx.lang.splice_function(info, fixed)
        diff = "".join(difflib.unified_diff(info.module_source.splitlines(keepends=True), new_source.splitlines(keepends=True),
                                            fromfile=f"a/{info.path.name}", tofile=f"b/{info.path.name}"))
        chk = ctx.runner.check(new_source)
        if not chk.get("ok"):
            feedback = f"The fixed module does not load: {chk.get('error')}"
            last = FixProposal(str(d.get("explanation", "")), diff, False, feedback)
            continue
        res = run_seq(ctx, spec, n=max(200, ctx.cfg.sequences // 2), source=new_source, seed=ctx.cfg.seed + 7)
        exh = run_seq(ctx, spec, n=0, source=new_source, exhaustive=True, shrink=False) if exhaustive_possible(spec) else {"ok": True, "counts": {}}
        c = dict(res.get("counts", {}))
        for k, v in (exh.get("counts") or {}).items():
            c[k] = c.get(k, 0) + v
        ok, note, fixes = fix_accepted(res, exh, recs, lambda cases: run_seq(ctx, spec, n=0, cases=cases))
        if ok:
            from ..util import sha256

            fix = FixProposal(str(d.get("explanation", "")).strip(), diff, True, note,
                              new_source=new_source, base_sha256=sha256(info.module_source))
            fix.fixes = fixes  # type: ignore[attr-defined]
            return fix
        m = res.get("minimal") or (res.get("failures") or [{}])[0]
        feedback = f"After your fix the class still disagrees with the model:\n```\n{m.get('args_repr')}\n```\nclass {m.get('impl')}, model {m.get('model')}"
        last = FixProposal(str(d.get("explanation", "")).strip(), diff, False, feedback)
    return last
