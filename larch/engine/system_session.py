"""System rules: guarantees about several components together (`# System rules` in LARCH.md).

Each component named in a rule's `(uses: ...)` has already been verified in this run: its
Lean model is checked against the code by differential testing, and its contracts
(`spec_*`) are proved about the model or at least tested. Larch puts the models side by
side, each in its own namespace, has the rule written as one Lean proposition over them,
and proves

    contract₁ → contract₂ → … → rule

The rule then holds for any implementation that meets those contracts (assume–guarantee
reasoning), and the report says exactly which contracts it rests on and how each of them
was established."""
from __future__ import annotations

import json
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path

from ..config import Config
from ..lean.checker import Checker
from ..lean.lint import lint_lean
from ..lean.toolchain import find_toolchain
from ..lean.workspace import LeanWorkspace
from ..llm.base import LLM, BudgetExceeded, Ledger, LLMError, LLMRequest, UsageLimitError
from ..llm.providers import make_provider
from ..prompts import PROVE_SYSTEM, RULE_SCHEMA, RULE_SYSTEM, rule_prove_user, rule_user
from ..report import ProofResult, Report, SpecResult
from ..ui import UI
from ..util import extract_code_block, slug, truncate
from .context import RunContext
from .session import artifacts_root

MODULE = "LarchSystem"
PROOF_MODULE = "LarchSystemProof"
HEADER = "theorem rule_holds : rule"
_PROOF_PREFIX = "import LarchSystem\nset_option linter.unusedVariables false\nnamespace Larch.System.Proof\n\n"
PROOF_LINE_OFFSET = _PROOF_PREFIX.count("\n")


class RuleError(Exception):
    pass


def namespace_of(subject) -> str:
    """The Lean namespace a component's model lives in: its name as an identifier."""
    n = re.sub(r"\W", "_", subject.name)
    return n if re.match(r"^[A-Za-z_]", n) else f"c_{n}"


def namespaced(module_text: str, ns: str) -> str:
    text = re.sub(r"^namespace Larch$", f"namespace Larch.{ns}", module_text, count=1, flags=re.M)
    text = re.sub(r"^end Larch$", f"end Larch.{ns}", text, flags=re.M)
    # Explicit `Larch.name` references to the module's own definitions move with it.
    own = set(re.findall(r"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+)?(?:def|abbrev|theorem|structure|inductive)\s+"
                         r"([A-Za-z_][A-Za-z0-9_']*)", text, re.M))
    if own:
        text = re.sub(r"\bLarch\.(" + "|".join(sorted(map(re.escape, own), key=len, reverse=True)) + r")\b",
                      lambda m: f"Larch.{ns}.{m.group(1)}", text)
    return re.sub(r"^set_option linter\.unusedVariables false\n", "", text, flags=re.M).strip()


def load_spec(report: Report):
    """The formal spec a component's run produced (its model and contracts)."""
    p = Path(report.artifacts_dir or "") / "spec.json"
    if not report.artifacts_dir or not p.exists():
        return None
    d = json.loads(p.read_text())
    if d.get("kind") == "component":
        from ..component import ComponentSpec

        return ComponentSpec.from_json(d)
    from ..spec import FormalSpec

    return FormalSpec.from_json(d)


def _status(s: SpecResult) -> str:
    if s.proof and s.proof.status == "proved":
        return "proved"
    return "tested, not proved"


def verify_rule(rule, number: int, md: Path, used: list[tuple[object, Report]], cfg: Config, ui: UI | None = None,
                *, llm: LLM | None = None) -> Report:
    ui = ui or UI()
    t0 = time.monotonic()
    report = Report(function=f"rule {number}", file=str(md), config=cfg.to_dict())
    report.kind, report.language, report.line = "rule", "lean", rule.line
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    artifacts_root(cfg).mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=f"{stamp}-rule-{number}-{slug(rule.text, 30)}-", dir=artifacts_root(cfg)))
    report.artifacts_dir = str(run_dir)
    ledger = llm.ledger if llm else Ledger(cfg.budget_usd)
    try:
        tc = find_toolchain()
        llm = llm or LLM(make_provider(cfg.provider, cache=cfg.cache), ledger)
        ui.header(target=f"system rule {number}: {rule.text}", model=f"{cfg.model} via {llm.provider.describe()}",
                  lean=tc.version, runtime="uses " + ", ".join(s.label for s, _ in used))
        ws = LeanWorkspace(run_dir / "lean", tc)
        ctx = RunContext(cfg=cfg, llm=llm, tc=tc, ws=ws, runner=None, checker=Checker(tc), info=None, ui=ui,  # type: ignore[arg-type]
                         run_dir=run_dir)
        _verify(ctx, rule, used, report)
    except RuleError as e:
        report.verdict, report.error = "error", str(e)
        report.headline = report.headline or "Could not check this rule."
    except UsageLimitError:
        raise
    except KeyboardInterrupt:
        report.verdict, report.error, report.headline = "error", "interrupted", "Interrupted."
    except Exception as e:  # noqa: BLE001
        import traceback

        report.verdict, report.error, report.headline = "error", f"{type(e).__name__}: {e}", "Larch hit an internal error."
        (run_dir / "error.txt").write_text(traceback.format_exc())
    report.elapsed_s = time.monotonic() - t0
    report.cost_usd = ledger.spent()
    report.nominal_cost_usd = ledger.nominal()
    report.llm_calls = len(ledger.calls)
    report.cost_by_stage = ledger.by_stage()
    report.save(run_dir)
    return report


def _verify(ctx: RunContext, rule, used: list[tuple[object, Report]], report: Report) -> None:
    ui, cfg = ctx.ui, ctx.cfg
    modules, inventory, by_name = [], [], {}
    for subj, rep in used:
        spec = load_spec(rep) if rep.verdict != "error" else None
        if spec is None:
            report.headline = f"{subj.label} could not be verified, so the rule cannot be checked."
            raise RuleError(f"{subj.label}: {rep.error or 'no model was produced'}")
        from .prove import _module_text

        ns = namespace_of(subj)
        modules.append(namespaced(_module_text(spec), ns))
        for s in rep.active_specs:
            full = f"{ns}.spec_{s.name}"
            inventory.append((full, s.english, _status(s)))
            by_name[full] = (subj, rep, s)
    base = "set_option linter.unusedVariables false\n\n" + "\n\n".join(modules) + "\n"

    with ui.step("Formalize the rule over the component models") as st:
        body, english, assumes = _formalize(ctx, rule, base, inventory, by_name, st)
        st.done(f"rests on {len(assumes)} component contract{'s' if len(assumes) != 1 else ''}")
    ui.note(f"  You wrote:   {rule.text}\n  Larch reads: {english}\n  Assumes:     " + (", ".join(assumes) or "nothing"))
    approval = "auto-approved" if cfg.auto_approve else "approved"
    if not cfg.auto_approve and not ui.confirm("Is this what the rule means?", default=True):
        report.verdict, report.headline = "partial", "The reading of the rule was rejected; nothing was proved."
        report.specs = [SpecResult("rule", "rule", english, body, "rejected", origin="contract", contract=rule.text)]
        return

    with ui.step("Prove the rule from the contracts") as st:
        res = _prove(ctx, st)
        st.done(f"proved · {res.method}" if res.status == "proved" else "not proved", status="ok" if res.status == "proved" else "warn")

    report.specs = [SpecResult("rule", "rule", english, body, approval, proof=res, origin="contract", contract=rule.text)]
    for full in assumes:
        _, _, s = by_name[full]
        report.specs.append(SpecResult(full, "assumption", s.english, s.lean, s.approval, proof=s.proof, origin=s.origin,
                                       contract=s.contract))
    weak = [f for f in assumes if _status(by_name[f][2]) != "proved"]
    buggy = [subj.label for subj, rep in used if rep.verdict == "bug"]
    tests = sum(int(rep.drt.get("valid", 0) or 0) for _, rep in used)
    report.drt = {"valid": tests, "disagreements": sum(int(rep.drt.get("disagreements", 0) or 0) for _, rep in used)}
    names = ", ".join(s.label for s, _ in used)
    if res.status != "proved":
        report.verdict = "partial"
        report.headline = "The rule was not proved from the component contracts."
        report.warnings.append("A missing assumption, a contract too weak for the rule, or a rule that does not hold can all cause this; "
                               "see the Lean error and the rule's reading above.")
    elif buggy:
        report.verdict = "bug"
        report.headline = (f"The rule follows from the contracts, but {', '.join(buggy)} disagree{'s' if len(buggy) == 1 else ''} "
                           "with its verified model, so the code may break it (see that report).")
    elif weak:
        report.verdict = "partial"
        report.headline = (f"Proved from {len(assumes)} contract(s); {len(weak)} of them ({', '.join(weak)}) are tested but not proved.")
    else:
        report.verdict = "passed"
        report.headline = (f"Proved from {len(assumes)} proved contract(s) of {names}, whose models agree with the code on "
                           f"{tests:,} tests.")


def _formalize(ctx: RunContext, rule, base: str, inventory, by_name, step) -> tuple[str, str, list[str]]:
    cfg = ctx.cfg
    if not rule.uses:
        raise RuleError("say which components the rule relies on: end the bullet with `(uses: A, B)`")
    text = rule.text + (f"\nExact Lean (use VERBATIM as \"lean\"): `{' '.join(rule.lean.split())}`" if rule.lean else "")
    feedback, last = "", []
    for attempt in range(cfg.formalize_repairs + 1):
        if attempt:
            step.update(f"repairing (round {attempt})")
        try:
            resp = ctx.ask(LLMRequest(system=RULE_SYSTEM, prompt=rule_user(text, base, inventory, feedback), model=cfg.model,
                                      stage="formalize", effort=cfg.effort, json_schema=RULE_SCHEMA))
        except (LLMError, BudgetExceeded) as e:
            raise RuleError(f"formalizing the rule failed: {e}") from e
        d = resp.data if isinstance(resp.data, dict) else {}
        body = (rule.lean or str(d.get("lean", ""))).strip()
        english = str(d.get("english", "")).strip()
        assumes, problems = [], []
        for a in d.get("assumes") or []:
            a = str(a).strip().strip("`").removeprefix("Larch.")
            if a in by_name:
                assumes.append(a)
            else:
                problems.append(f"`{a}` is not one of the listed component contracts")
        assumes = list(dict.fromkeys(assumes))
        if not body:
            problems.append("\"lean\" is empty")
        problems += [str(i) for i in lint_lean(body)]
        if re.search(r"^\s*(def|theorem|lemma|namespace)\b", body, re.M):
            problems.append("\"lean\" must be a proposition, not declarations")
        if not problems:
            res = ctx.ws.compile_module(MODULE, _system_module(base, body, assumes))
            if not res.ok:
                problems.append("Lean rejected the rule:\n" + res.error_text()[:3000])
        if not problems:
            ctx.rule_statement = _statement(body, assumes)  # type: ignore[attr-defined]
            ctx.rule_english = english  # type: ignore[attr-defined]
            ctx.rule_base = base  # type: ignore[attr-defined]
            ctx.rule_assumes = assumes  # type: ignore[attr-defined]
            return body, english, assumes
        last = problems
        feedback = "\n".join(f"- {p}" for p in problems) + f"\n\nYour previous answer:\n```json\n{json.dumps(d, indent=2)[:6000]}\n```"
    raise RuleError("could not state the rule in Lean: " + "; ".join(p.splitlines()[0] for p in last[:3]))


def _statement(body: str, assumes: list[str]) -> str:
    hyps = "".join(f"{a} →\n  " for a in assumes)
    return f"{hyps}({body})"


def _system_module(base: str, body: str, assumes: list[str]) -> str:
    return (f"{base}\nnamespace Larch.System\n\n/-- The system rule, assuming the listed component contracts. -/\n"
            f"def rule : Prop :=\n  {_statement(body, assumes)}\n\nend Larch.System\n")


def _proof_file(block: str) -> str:
    return f"{_PROOF_PREFIX}{block.strip()}\n\nend Larch.System.Proof\n"


def _try(ctx: RunContext, block: str) -> tuple[bool, str]:
    issues = lint_lean(block)
    if issues:
        return False, "Larch rejected the proof before running Lean:\n" + "\n".join(str(i) for i in issues)
    if not re.search(re.escape(HEADER) + r"\s*:=", block):
        return False, f"The block must contain the target theorem with exactly this header: `{HEADER} :=`"
    res = ctx.ws.check_text(_proof_file(block), stem="Try_rule", timeout=120)
    if res.ok:
        if any("sorry" in m.text for m in res.messages if m.severity == "warning"):
            return False, "The proof still contains `sorry`."
        return True, ""
    return False, res.error_text(line_offset=PROOF_LINE_OFFSET)


def automation(assumes: list[str]) -> list[tuple[str, str]]:
    unf = " ".join(["rule", *assumes])
    return [
        ("auto: grind", f"{HEADER} := by\n  unfold rule\n  intros\n  grind"),
        ("auto: unfold+grind", f"{HEADER} := by\n  unfold {unf}\n  intros\n  grind"),
        ("auto: simp_all", f"{HEADER} := by\n  unfold {unf}\n  intros\n  simp_all"),
    ]


def _prove(ctx: RunContext, step) -> ProofResult:
    t0 = time.monotonic()
    assumes = ctx.rule_assumes  # type: ignore[attr-defined]
    tried = []
    for method, block in automation(assumes):
        step.update(method)
        ok, err = _try(ctx, block)
        if ok:
            return _final(ctx, ProofResult("rule", "proved", method, 0, time.monotonic() - t0, block))
        tried.append(f"- `{block.splitlines()[-1].strip()}`: {truncate(err, 300)}")
    attempts: list[tuple[str, str]] = []
    for i in range(2 * ctx.cfg.proof_attempts):  # one rule, worth more effort than one of many specs
        step.update(f"LLM proof attempt {i + 1}")
        prompt = rule_prove_user(ctx.rule_base, ctx.rule_statement, ctx.rule_english, HEADER, attempts[-2:])  # type: ignore[attr-defined]
        try:
            resp = ctx.ask(LLMRequest(system=PROVE_SYSTEM, prompt=prompt, model=ctx.cfg.prover, stage="prove", effort=ctx.cfg.effort))
        except UsageLimitError:
            raise
        except (LLMError, BudgetExceeded) as e:
            attempts.append(("", f"(LLM error: {e})"))
            break
        code = extract_code_block(resp.text)
        if not code:
            attempts.append(("(no code block)", "Your reply did not contain a ```lean code block."))
            continue
        ok, err = _try(ctx, code)
        if ok:
            return _final(ctx, ProofResult("rule", "proved", f"llm ({i + 1} attempt{'s' if i else ''})", i + 1,
                                           time.monotonic() - t0, code))
        attempts.append((code, truncate(err, 3500)))
    last = attempts[-1][1] if attempts else "\n".join(tried)
    return ProofResult("rule", "unproved", "llm", len(attempts), time.monotonic() - t0, error=truncate(last, 1500))


def _final(ctx: RunContext, res: ProofResult) -> ProofResult:
    """Authoritative acceptance by the out-of-process proof checker."""
    comp = ctx.ws.compile_module(PROOF_MODULE, _proof_file(res.proof), timeout=300)
    if not comp.ok:
        res.status, res.error = "unproved", "failed to compile during final check: " + comp.error_text(PROOF_LINE_OFFSET)[:800]
        return res
    chk = ctx.checker.check(PROOF_MODULE, [("Larch.System.Proof.rule_holds", "Larch.System.rule")],
                            lean_path=ctx.ws.lean_path, cwd=ctx.ws.root)[0]
    res.axioms = chk.axioms
    if not chk.accepted:
        res.status, res.error = "unproved", f"rejected by the proof checker: {chk.reason()}"
    return res
