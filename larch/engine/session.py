"""Orchestration of one verification run (one function)."""
from __future__ import annotations

import json
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from ..config import Config
from ..lean.checker import Checker
from ..lean.toolchain import find_toolchain
from ..lean.workspace import LeanWorkspace
from ..lang import ExtractError, RuntimeEnvError, language_for
from ..llm.base import LLM, Ledger, UsageLimitError
from ..llm.providers import make_provider
from ..report import MutationSummary, ProofResult, Report, SpecResult
from ..spec import FormalSpec
from ..ui import UI
from ..util import cache_root, slug
from .context import RunContext
from .runner import JobRunner
from .findings import (adjudicate, classify, count_kind, diagnose_systematic, make_finding, propose_fix, repair_model,
                       systematic, SYSTEMATIC_RATE)
from .formalize import FormalizeError, build_lean, formalize
from .prove import finalize_proofs, prove_all
from .testing import exhaustive_size, make_mutants, run_drt, run_examples, run_exhaustive, run_mutants
from .store import load_approved, save_approved


class CallMismatch(Exception):
    """Nearly every input disagreed because the implementation is not being called the way
    it is actually used (types, argument order, values it cannot take): no verdict on its logic."""

    def __init__(self, cause: str, diag: dict):
        super().__init__(cause)
        self.cause, self.diag = cause, diag


def artifacts_root(cfg: Config) -> Path:
    return (Path(cfg.artifacts).expanduser() if cfg.artifacts else cache_root() / "runs").resolve()


def verify_function(path: Path, func: str, cfg: Config, ui: UI | None = None, *, llm: LLM | None = None,
                    spec_override: FormalSpec | None = None, subject=None) -> Report:
    """Verify one function. `subject` is its entry in LARCH.md (larch.contracts.Subject),
    whose contracts become mandatory specs."""
    ui = ui or UI()
    t_start = time.monotonic()
    path = Path(path)
    report = Report(function=func, file=str(path), config=cfg.to_dict())
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # Unique even when several runs of the same function start in the same second
    # (the workspace holds Lean modules and worker files that must not be shared).
    artifacts_root(cfg).mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=f"{stamp}-{slug(path.stem, 20)}-{slug(func, 30)}-", dir=artifacts_root(cfg)))
    report.artifacts_dir = str(run_dir)
    ledger = llm.ledger if llm else Ledger(cfg.budget_usd)
    ctx: RunContext | None = None
    try:
        lang = language_for(path)
        if lang is None:
            raise ExtractError(f"{path.suffix or path.name}: unsupported file type")
        info = lang.extract(path, func)
        report.language = lang.name
        report.line = info.lineno
        runtime = lang.runtime(info, cfg)
        report.runtime = runtime.describe()
        runner = JobRunner(runtime, run_dir / "py")
        tc = find_toolchain()
        llm = llm or LLM(make_provider(cfg.provider, cache=cfg.cache), ledger)
        ws = LeanWorkspace(run_dir / "lean", tc)
        ctx = RunContext(cfg=cfg, llm=llm, tc=tc, ws=ws, runner=runner, checker=Checker(tc), info=info, ui=ui,
                         run_dir=run_dir, lang=lang, contracts=list(subject.contracts) if subject else [],
                         subject=subject)
        ui.header(target=f"{path}::{func}", model=f"{cfg.model} via {llm.provider.describe()}", lean=tc.version,
                  runtime=runtime.describe())

        with ui.step(f"Read {path.name}::{func}") as st:
            # Load the code in the project's own runtime before spending anything on it.
            st.update(f"loading with {runtime.display}")
            chk = runner.check()
            if not chk.get("ok"):
                st.done("cannot load the code in the project's environment", status="fail")
                raise RuntimeEnvError(lang.explain_load_error(runtime, info, chk))
            n_lines = info.end_lineno - info.lineno + 1
            st.done(f"{info.signature}  ({n_lines} lines) · {runtime.display}")
            for w in info.warnings + runtime.warnings:
                st.line(f"[yellow]{w}[/]")
                report.warnings.append(w)
        _run(ctx, report, spec_override)
    except (RuntimeEnvError, ExtractError) as e:
        report.verdict = "error"
        report.error = str(e)
        report.headline = (
            f"Could not run {path.name} in the project's environment." if isinstance(e, RuntimeEnvError)
            else f"Cannot verify {func}."
        )
        (run_dir / "error.txt").write_text(str(e) + "\n")
    except CallMismatch as e:
        report.verdict = "error"
        d = e.diag
        report.headline = (f"Larch could not call {func} the way it is really used: {d['dis']:,} of {d['valid']:,} inputs "
                           "went wrong for the same reason, so nothing was concluded about its logic.")
        report.error = e.cause + (f" (the implementation raised: {d['uniform_crash']})" if d.get("uniform_crash") else "")
        report.drt = {"valid": d["valid"], "disagreements": d["dis"]}
    except FormalizeError as e:
        report.verdict = "error"
        report.error = f"{e}: " + "; ".join(" ".join(ln.strip() for ln in p.splitlines()[:4] if ln.strip())[:300] for p in e.problems[:3])
        report.headline = "Could not formalize this function."
    except UsageLimitError:
        raise  # not a property of the function: let the caller pause or report it
    except KeyboardInterrupt:
        report.verdict = "error"
        report.error = "interrupted"
        report.headline = "Interrupted."
    except Exception as e:  # noqa: BLE001 - reported, never a traceback for users
        report.verdict = "error"
        report.error = f"{type(e).__name__}: {e}"
        report.headline = "Larch hit an internal error."
        import traceback

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


def _run(ctx: RunContext, report: Report, spec_override: FormalSpec | None) -> None:
    cfg, ui = ctx.cfg, ctx.ui

    # ---- 1. formalize + 2. review --------------------------------------------------------
    spec: FormalSpec | None = spec_override
    reused = False
    if spec is None and not cfg.extra.get("fresh"):
        saved = load_approved(ctx.info)
        if saved is not None and saved[0].contracts != [c.text for c in ctx.contracts]:
            saved = None  # LARCH.md changed since approval: formalize the new contracts
        if saved is not None and (cfg.auto_approve or cfg.extra.get("reuse") or ui.confirm(
            f"Reuse the specification you approved on {saved[1]}?", default=True)):
            spec = saved[0]
            reused = True
    if spec is not None:
        with ui.step("Load approved specification") as st, ctx.timed("formalize"):
            problems = build_lean(ctx, spec)
            if problems:
                raise FormalizeError("the saved specification no longer compiles", problems)
            st.done(f"{len(spec.spec_names())} specs" + (" (previously approved)" if reused else ""))
    else:
        feedback, previous = None, None
        for _round in range(4):
            with ui.step("Formalize: write Lean model and specs") as st, ctx.timed("formalize"):
                spec, san, rounds = formalize(ctx, feedback=feedback, previous=previous, step=st)
                n_post, n_prop = len(spec.active_posts()), len(spec.active_props())
                st.done(f"{n_post} postconditions, {n_prop} properties" + (f" · {rounds} rounds" if rounds > 1 else ""))
                st.line(f"model compiles · tested on {san.valid_inputs} inputs, no spec violations by the model")
                for w in san.warnings:
                    if w.startswith("dropped spec"):
                        st.line(f"[yellow]{w}[/]")
                        report.warnings.append(w)
                if san.impl_disagreements:
                    st.line(f"[yellow]implementation already disagrees with the model on {san.impl_disagreements} of 200 quick-test inputs[/]")
            if cfg.auto_approve:
                for p in spec.postconditions + spec.properties:
                    p.status = "auto-approved"
                ui.show_spec(spec, san.as_dict(), auto=True)
                break
            decision = ui.review(spec, san.as_dict())
            if decision.abort:
                report.verdict = "error"
                report.error = "specification not approved" if not ui.interactive else "specification rejected by reviewer"
                report.headline = "Stopped at spec review: nothing is verified until a person approves the specs (or --yes is given)."
                _fill_specs(report, spec, {})
                return
            if decision.feedback:
                feedback, previous = decision.feedback, spec
                continue
            for p in spec.postconditions + spec.properties:
                p.status = "rejected" if p.name in decision.rejected else "approved"
            _write_back(ctx, spec, report)
            break
        assert spec is not None
        if not spec.spec_names():
            report.verdict = "error"
            report.error = "all specs were rejected"
            report.headline = "Nothing to verify."
            _fill_specs(report, spec, {})
            return
        if any(p.status == "rejected" for p in spec.postconditions + spec.properties):
            problems = build_lean(ctx, spec)
            if problems:
                raise FormalizeError("model no longer compiles after removing rejected specs", problems)
        save_approved(ctx.info, spec, auto=cfg.auto_approve)
    report.understanding = spec.understanding
    report.precondition = spec.pre_english
    (ctx.run_dir / "spec.json").write_text(json.dumps(spec.to_json(), indent=2, ensure_ascii=False))

    # ---- 3. differential testing, adjudication, model repair --------------------------------
    findings = []
    drt: dict = {}
    for repair_round in range(cfg.model_repairs + 1):
        size = exhaustive_size(ctx, spec)
        exhaustive = size is not None and size <= cfg.exhaustive_limit
        title = (f"Exhaustive test: implementation vs. model on all {size:,} inputs" if exhaustive
                 else "Differential test: implementation vs. model")
        with ui.step(title) as st, ctx.timed("test"):
            drt = run_exhaustive(ctx, spec) if exhaustive else run_drt(ctx, spec, n=cfg.tests, vacuity=True)
            if not drt.get("ok"):
                raise RuntimeError(f"differential testing failed: {drt.get('error')}")
            c = drt.get("counts", {})
            valid = sum(c.get(k, 0) for k in ("agree", "value", "crash", "timeout", "type"))
            dis = sum(c.get(k, 0) for k in ("value", "crash", "timeout", "type"))
            st.done(f"{valid:,} valid inputs · {dis} disagreements", status="ok" if dis == 0 else "warn")
            if drt.get("stopped_early"):
                st.line(f"[yellow]{drt['stopped_early']}[/]")
            if drt.get("strategy_note"):
                st.line(f"[yellow]{drt['strategy_note']}[/]")
        cls = classify(drt)
        findings = [make_finding(r, "spec_violation" if r.get("impl_violates") else r["kind"], "confirmed",
                                 count=count_kind(drt, r)) for r in cls.confirmed]
        for r in cls.spec_problems[:3]:
            msg = (f"spec {', '.join(r['model_violates'])} is false on input ({r.get('args_repr')}): the verified model "
                   f"returns {r.get('model')} and violates it too, so the spec (not the code) is wrong")
            if msg not in report.warnings:
                report.warnings.append(msg)
        for ex in run_examples(ctx, spec):
            if ex.get("impl_ok") is False and ex.get("model_ok"):
                findings.insert(0, make_finding(
                    {"args_repr": ex["args_repr"], "impl": ex.get("impl", ""), "model": ex.get("model", "")},
                    "doc_example", "confirmed",
                    f"the documentation says {ctx.info.name.split('.')[-1]}({ex['args_repr']}) = {ex['expected']}",
                ))
        model_issues: list[str] = []
        # Most inputs disagreeing almost never means the code is wrong everywhere: look for
        # the one cause (a misread model, a representation, Larch calling it wrong) first.
        diag = systematic(drt) if cfg.adjudicate else None
        if diag:
            with ui.step(f"Diagnose: {diag['dis']:,} of {diag['valid']:,} inputs disagree") as st, ctx.timed("adjudicate"):
                sys_verdict, cause = diagnose_systematic(ctx, spec, diag)
                st.done(f"{sys_verdict.replace('_', ' ')}: {cause[:110]}", status="warn")
            if sys_verdict in ("model_misread", "representation"):
                model_issues.append(
                    f"On {diag['dis']:,} of {diag['valid']:,} inputs the model disagrees with the implementation, which "
                    f"matches the documentation. Common cause: {cause}\nExamples:\n"
                    + "\n".join(f"- ({x.get('args_repr')}): implementation {x.get('impl')}, model {x.get('model')}" for x in diag["samples"])
                )
            elif sys_verdict == "larch_calls_it_wrong":
                raise CallMismatch(cause, diag)
        if cls.unexplained and not model_issues:
            with ui.step("Adjudicate disagreements") as st, ctx.timed("adjudicate"):
                for r in cls.unexplained[:3]:
                    if cfg.adjudicate:
                        verdict, why = adjudicate(ctx, spec, r)
                    else:
                        verdict, why = "implementation_bug", ""
                    st.line(f"{ctx.info.name}({r.get('args_repr')}): impl {r.get('impl')} vs model {r.get('model')} → [bold]{verdict.replace('_', ' ')}[/]")
                    if verdict == "implementation_bug":
                        findings.append(make_finding(r, r["kind"], "likely", why, count=count_kind(drt, r)))
                    elif verdict == "model_bug":
                        model_issues.append(
                            f"On input ({r.get('args_repr')}) the model returns {r.get('model')} but the implementation returns "
                            f"{r.get('impl')}, which is correct per the documentation: {why}"
                        )
                    else:
                        findings.append(make_finding(r, r["kind"], "possible", why, count=count_kind(drt, r)))
                st.done(f"{len(cls.unexplained[:3])} case(s)")
        if model_issues and repair_round < cfg.model_repairs:
            with ui.step("Revise model (the model, not the code, was wrong)") as st, ctx.timed("formalize"):
                new, why, conflict = repair_model(ctx, spec, model_issues)
                if new is None:
                    st.done("could not revise the model", status="warn")
                    for mi in model_issues:
                        report.warnings.append("unresolved model disagreement: " + mi[:200])
                    break
                spec = new
                (ctx.run_dir / "spec.json").write_text(json.dumps(spec.to_json(), indent=2, ensure_ascii=False))
                report.model_revisions.append(why)
                st.done(why[:120])
                if conflict:
                    report.warnings.append(f"the model writer believes an approved spec may be wrong: {conflict}")
            continue
        if model_issues:
            for mi in model_issues:
                report.warnings.append("unresolved model disagreement: " + mi[:200])
        break
    c = drt.get("counts", {})
    report.drt = {
        "valid": sum(c.get(k, 0) for k in ("agree", "value", "crash", "timeout", "type")),
        "disagreements": sum(c.get(k, 0) for k in ("value", "crash", "timeout", "type")),
        "pre_false": c.get("pre_false", 0),
        "counts": c,
        "inputs": drt.get("inputs", 0),
        "exhaustive": bool(drt.get("exhaustive")) and not drt.get("stopped_early"),
    }
    vac = drt.get("vacuity", {})
    impl_viol_counts: dict[str, int] = {}
    for f in drt.get("failures", []):
        for p in f.get("impl_violates", []):
            impl_viol_counts[p] = impl_viol_counts.get(p, 0) + 1

    # ---- 4. prove (and mutation analysis in parallel) -----------------------------------------
    mut_result: dict = {}
    mutants = make_mutants(ctx) if cfg.run_mutation else []

    def _mutation():
        t0 = time.monotonic()
        try:
            mut_result.update(run_mutants(ctx, spec, mutants))
        except Exception as e:  # noqa: BLE001
            mut_result.update({"ok": False, "error": str(e)})
        ctx.stage_seconds["mutation"] = time.monotonic() - t0

    mt = threading.Thread(target=_mutation, daemon=True) if mutants else None
    if mt:
        mt.start()
    if cfg.run_proofs:
        proofs = _prove_stage(ctx, spec)
    else:
        proofs = {n: ProofResult(name=n, status="not_attempted") for n in spec.spec_names()}
    _write_proofs(ctx, spec, proofs)

    if mt:
        with ui.step("Mutation analysis: are the specs and tests strong enough?") as st:
            mt.join()
            ms = MutationSummary()
            muts = mut_result.get("mutants", [])
            ms.inputs = int(mut_result.get("valid_inputs") or 0)
            by_id = {m.id: m for m in mutants}
            invalid = [r for r in muts if r.get("invalid")]
            muts = [r for r in muts if not r.get("invalid")]
            ms.invalid = len(invalid)
            for r in muts:
                if r.get("killed"):
                    ms.killed += 1
                if r.get("specs"):
                    ms.killed_by_specs += 1
                if not r.get("killed"):
                    if r.get("likely_equivalent"):
                        ms.likely_equivalent += 1
                    m = by_id.get(r["id"])
                    if m:
                        # On buggy code, a mutant that agrees with the verified model is not
                        # "equivalent": it is a one-token candidate fix.
                        tag = " (agrees with the model: a candidate fix)" if findings else " (likely equivalent)"
                        ms.survivors.append(m.description + (tag if r.get("likely_equivalent") else ""))
            ms.total = len(muts)
            report.mutation = ms
            if ms.total:
                eq = f" · {ms.likely_equivalent} likely equivalent" if ms.likely_equivalent else ""
                st.done(f"{ms.killed}/{ms.total} mutants detected ({ms.score:.0%}){eq} · {ms.killed_by_specs} caught by the specs alone",
                        status="ok" if ms.adjusted_score >= 0.8 else "warn")
                for s in ms.survivors[:5]:
                    st.line(f"[dim]survived: {s}[/]")
            else:
                st.done("no mutants generated" if not mut_result.get("error") else f"failed: {mut_result.get('error')}", status="warn")
            spec_kills: dict[str, int] = {}
            for r in muts:
                for s in r.get("specs", []):
                    spec_kills[s] = spec_kills.get(s, 0) + 1

    else:
        spec_kills = {}

    _fill_specs(report, spec, proofs, vac, spec_kills, impl_viol_counts)
    for s in report.specs:
        if s.possibly_vacuous:
            report.warnings.append(f"spec `{s.name}` accepted every perturbed output in testing: it may be vacuous")

    # ---- 5. fixes -------------------------------------------------------------------------------
    report.findings = findings
    actionable = [f for f in findings if f.confidence in ("confirmed", "likely")]
    if actionable and cfg.propose_fixes:
        with ui.step("Propose a fix (validated against the verified model)") as st, ctx.timed("fix"):
            recs = [r for r in (drt.get("minimal"), *drt.get("failures", [])) if r][:3]
            fix = propose_fix(ctx, spec, recs)
            if fix is not None:
                patch = ctx.run_dir / "fix.patch"
                patch.write_text(fix.diff)
                fix.patch_path = str(patch)
                actionable[0].fix = fix
                st.done("validated" if fix.validated else "could not validate a fix", status="ok" if fix.validated else "warn")
            else:
                st.done("no fix proposed", status="warn")

    # ---- verdict --------------------------------------------------------------------------------
    total = len(report.active_specs)
    proved = report.proved
    valid = report.drt.get("valid", 0)
    domain = next((s for s in report.specs if s.name == "input_domain"), None)
    complete = report.drt.get("exhaustive") and (domain is None or (domain.proof and domain.proof.status == "proved"))
    report.drt["complete"] = bool(complete)
    # On a complete domain, a postcondition checked on the implementation's output for
    # every input holds for the code itself, proof or not; properties still need proofs.
    unproved_props = [s for s in report.active_specs if s.kind == "property" and not (s.proof and s.proof.status == "proved")]
    dis = report.drt.get("disagreements", 0)
    if actionable:
        report.verdict = "bug"
        f = actionable[0]
        report.headline = f"{f.title()}: {ctx.info.name.split('.')[-1]}({f.args_repr}) returned {f.impl}, expected {f.model}."
        if valid and dis >= SYSTEMATIC_RATE * valid:
            report.warnings.insert(0, f"the implementation disagrees with the model on {dis:,} of {valid:,} inputs ({dis / valid:.0%}). "
                                      "When almost every input disagrees, the model has usually misread the function: check "
                                      "Larch's understanding above before changing the code.")
    elif dis:
        # Never "passed" with disagreements nobody could attribute.
        report.verdict = "partial"
        report.headline = (f"{proved}/{total} specs proved; the implementation disagrees with the model on {dis:,} of {valid:,} "
                           "inputs and Larch could not tell which of them is wrong (see below).")
    elif complete and valid > 0 and not unproved_props:
        report.verdict = "passed"
        report.headline = (
            f"Checked on every one of the {valid:,} valid inputs (input domain proved complete): the implementation "
            f"agrees with the verified model and satisfies all {total} specs; {proved}/{total} also proved about the model."
        )
    elif proved == total and total > 0 and valid > 0:
        report.verdict = "passed"
        report.headline = (
            f"All {total} specs proved in Lean (no sorry, no axioms); the implementation agrees with the "
            f"verified model on {valid:,} random inputs."
        )
    else:
        report.verdict = "partial"
        report.headline = f"{proved}/{total} specs proved; the implementation agrees with the model on {valid - dis:,} random inputs."
        if valid == 0:
            report.warnings.append("no generated input satisfied the precondition; the implementation was not tested")
    possible = [f for f in findings if f.confidence == "possible"]
    if possible and report.verdict != "bug":
        report.warnings.append(f"{len(possible)} disagreement(s) could not be attributed to the code or the model; review them")


def _write_back(ctx: RunContext, spec: FormalSpec, report: Report) -> None:
    """A LARCH.md heading without bullets asked Larch to propose contracts: record the
    ones a person approved there, in English, so the file stays the source of truth."""
    from ..contracts import write_back

    subject = getattr(ctx, "subject", None)
    if subject is None or subject.contracts or ctx.cfg.auto_approve:
        return
    bullets = [p.english for p in spec.postconditions + spec.properties if p.status == "approved" and p.origin != "larch"]
    if bullets and write_back(subject, bullets):
        report.warnings.append(f"recorded {len(bullets)} approved contract(s) in {subject.file.name}")


def _prove_stage(ctx: RunContext, spec: FormalSpec) -> dict[str, ProofResult]:
    ui = ctx.ui
    with ui.step(f"Prove {len(spec.spec_names())} specs in Lean") as st, ctx.timed("prove"):
        done_names: list[str] = []

        def _on_done(r: ProofResult):
            done_names.append(r.name)
            st.update(f"{len(done_names)}/{len(spec.spec_names())} done")

        proofs = prove_all(ctx, spec, on_done=_on_done)
        st.update("checking proofs (axioms, statements, kernel replay)")
        proofs = finalize_proofs(ctx, spec, proofs)
        n_ok = sum(1 for p in proofs.values() if p.status == "proved")
        st.done(f"{n_ok}/{len(proofs)} proved", status="ok" if n_ok == len(proofs) else "warn")
        for name in spec.spec_names():
            p = proofs[name]
            if p.status == "proved":
                st.line(f"[green]✓[/] {name}  [dim]{p.method} · {p.elapsed:.0f}s[/]")
            else:
                st.line(f"[yellow]✗[/] {name}  [dim]{_first_line(p.error)}[/]")
    return proofs


def _first_line(s: str) -> str:
    for ln in (s or "").splitlines():
        if ln.strip():
            return ln.strip()[:110]
    return ""


def _fill_specs(report: Report, spec: FormalSpec, proofs: dict, vac: dict | None = None,
                kills: dict | None = None, impl_viol: dict | None = None) -> None:
    vac = vac or {}
    kills = kills or {}
    impl_viol = impl_viol or {}
    report.specs = []
    for p in spec.postconditions:
        v = vac.get(p.name, {})
        report.specs.append(SpecResult(
            name=p.name, kind="postcondition", english=p.english, lean=p.lean,
            approval=p.status if p.status != "proposed" else "approved", proof=proofs.get(p.name),
            mutants_caught=kills.get(p.name, 0), vacuity_tried=v.get("tried", 0), vacuity_rejected=v.get("rejected", 0),
            impl_violations=impl_viol.get(p.name, 0), origin=p.origin, contract=p.contract,
        ))
    for q in spec.properties:
        report.specs.append(SpecResult(
            name=q.name, kind="property", english=q.english, lean=q.lean,
            approval=q.status if q.status != "proposed" else "approved", proof=proofs.get(q.name),
            origin=q.origin, contract=q.contract,
        ))


def _write_proofs(ctx: RunContext, spec: FormalSpec, proofs: dict[str, ProofResult]) -> None:
    out = ctx.run_dir / "proofs"
    out.mkdir(exist_ok=True)
    for name, p in proofs.items():
        (out / f"{name}.lean").write_text(p.proof or f"-- not proved: {p.error}")
