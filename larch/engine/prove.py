"""Stage 3: proving each approved spec about the model.

Strategies (compared in EVALS.md):
  llm              LLM writes a proof; Lean errors are fed back for up to N attempts.
  portfolio+llm    first try a fixed portfolio of automation scripts (grind/simp/omega/
                   induction), then fall back to the LLM loop, seeded with the automation's
                   failure output.
  portfolio+sketch portfolio, then draft-sketch-prove: the LLM writes helper lemmas with
                   `sorry`, Larch checks the skeleton, then proves each lemma separately.

Nothing here decides acceptance. A tentative success must pass the out-of-process
checker in `finalize_proofs` (exact statement, no sorry, no non-standard axioms, kernel
replay) before anything is reported as proved.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..lean.lint import lint_lean
from ..llm.base import BudgetExceeded, LLMError, LLMRequest
from ..prompts import LEMMA_USER, PROVE_SYSTEM, SKETCH_USER_SUFFIX, prove_user
from ..report import ProofResult
from ..spec import FormalSpec, model_module, proof_module, spec_constant, statement_text, theorem_header, theorem_name
from ..util import extract_code_block, truncate
from .context import RunContext

PROOF_LINE_OFFSET = 5  # lines proof_module() adds before the LLM's block


def _def_names(spec: FormalSpec) -> list[str]:
    names = re.findall(r"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+)?def\s+([A-Za-z_][A-Za-z0-9_'.]*)", spec.model_code, re.M)
    out = []
    for n in names:
        if n not in out:
            out.append(n)
    if "model" not in out:
        out.append("model")
    # `where` helpers are named model.go etc.
    for n in re.findall(r"^\s*where\s+([A-Za-z_][A-Za-z0-9_']*)", spec.model_code, re.M):
        out.append(f"model.{n}")
    return out


def portfolio_scripts(spec: FormalSpec, name: str) -> list[tuple[str, str]]:
    """Generic automation attempts for spec `name` (label, proof body)."""
    header = theorem_header(name)
    defs = ", ".join(_def_names(spec))
    kind = spec.spec_kind(name)
    if kind == "postcondition":
        binders = " ".join(p.name for p in spec.params)
        intro = f"intro {binders} h_pre"
        unf = f"simp only [pre, post_{name}] at *"
        list_params = [p.name for p in spec.params if p.lean_type.startswith(("List", "Array"))]
    else:
        prop = next(q for q in spec.active_props() if q.name == name)
        binders = " ".join(p.name for p in prop.params)
        intro = f"intro {binders}" if binders else "skip"
        unf = f"simp only [prop_{name}]"
        list_params = [p.name for p in prop.params if p.lean_type.startswith(("List", "Array"))]
        binders_list = [p.name for p in prop.params]
    scripts = [
        ("grind", f"{header} := by\n  {intro}\n  {unf}\n  grind [{defs}]"),
        ("simp_all+omega", f"{header} := by\n  {intro}\n  {unf}\n  simp_all [{defs}]\n  all_goals (first | omega | grind)"),
        ("split+grind", f"{header} := by\n  {intro}\n  {unf}\n  unfold model\n  repeat' split\n  all_goals (first | omega | grind [{defs}])"),
    ]
    if list_params:
        xs = list_params[0]
        others = [b for b in (spec.params if kind == "postcondition" else []) if b.name != xs]
        gen = (" generalizing " + " ".join(o.name for o in others)) if others else ""
        scripts.append((
            "induction+grind",
            f"{header} := by\n  {intro}\n  {unf}\n  induction {xs}{gen} with\n  | nil => grind [{defs}]\n  | cons hd tl ih => grind [{defs}]",
        ))
    scripts.append(("decide", f"{header} := by\n  unfold spec_{name}\n  decide"))
    return scripts


class Prover:
    def __init__(self, ctx: RunContext, spec: FormalSpec):
        self.ctx = ctx
        self.spec = spec
        self.cfg = ctx.cfg
        self.model_text = model_module(spec)

    # -- single attempt checking ------------------------------------------------------------
    def try_block(self, name: str, block: str, *, allow_sorry: bool = False, timeout: float = 120.0) -> tuple[bool, str]:
        issues = [i for i in lint_lean(block) if allow_sorry is False or i.token not in ("sorry",)]
        if issues:
            return False, "Larch rejected the proof before running Lean:\n" + "\n".join(str(i) for i in issues)
        header = theorem_header(name)
        if not re.search(re.escape(header) + r"\s*:=", block):
            return False, f"The block must contain the target theorem with exactly this header: `{header} :=`"
        res = self.ctx.ws.check_text(proof_module(name, block), stem=f"Try_{name}", timeout=timeout)
        if res.ok:
            sorry_warn = any("sorry" in m.text for m in res.messages if m.severity == "warning")
            if sorry_warn and not allow_sorry:
                return False, "The proof still contains `sorry`."
            return True, ""
        return False, res.error_text(line_offset=PROOF_LINE_OFFSET)

    # -- strategies -----------------------------------------------------------------------------
    def portfolio(self, name: str) -> tuple[ProofResult | None, str]:
        scripts = portfolio_scripts(self.spec, name)
        first_err = ""
        results: dict[str, tuple[bool, str]] = {}
        with ThreadPoolExecutor(max_workers=min(len(scripts), 4)) as ex:
            futs = {ex.submit(self.try_block, name, body, timeout=60.0): (label, body) for label, body in scripts}
            for fut in as_completed(futs):
                label, body = futs[fut]
                results[label] = fut.result()
        for label, body in scripts:
            ok, err = results[label]
            if ok:
                return ProofResult(name=name, status="proved", method=f"auto: {label}", attempts=0, proof=body), ""
            if label == "grind":
                first_err = err
        return None, truncate(first_err, 2500)

    def llm_loop(self, name: str, automation: str | None, budget_attempts: int | None = None) -> ProofResult:
        header = theorem_header(name)
        statement = statement_text(self.spec, name)
        english = self.spec.english_of(name)
        attempts: list[tuple[str, str]] = []
        n = budget_attempts or self.cfg.proof_attempts
        for i in range(n):
            prompt = prove_user(self.model_text, name, statement, english, header, automation, attempts[-2:])
            if len(attempts) > 2:
                prompt += f"\n\n({len(attempts) - 2} earlier attempts also failed.)"
            try:
                resp = self.ctx.ask(LLMRequest(system=PROVE_SYSTEM, prompt=prompt, model=self.cfg.prover, stage="prove", effort=self.cfg.effort))
            except BudgetExceeded:
                break
            except LLMError as e:
                attempts.append(("", f"(LLM error: {e})"))
                continue
            code = extract_code_block(resp.text)
            if not code:
                attempts.append(("(no code block)", "Your reply did not contain a ```lean code block."))
                continue
            ok, err = self.try_block(name, code)
            if ok:
                return ProofResult(name=name, status="proved", method=f"llm ({i + 1} attempt{'s' if i else ''})", attempts=i + 1, proof=code)
            attempts.append((code, truncate(err, 3500)))
        last = attempts[-1][1] if attempts else "no attempts"
        return ProofResult(name=name, status="unproved", method="llm", attempts=len(attempts), error=truncate(last, 1500))

    def sketch(self, name: str, automation: str | None) -> ProofResult:
        """Draft-sketch-prove decomposition."""
        header = theorem_header(name)
        statement = statement_text(self.spec, name)
        english = self.spec.english_of(name)
        attempts: list[tuple[str, str]] = []
        for i in range(2):
            prompt = prove_user(self.model_text, name, statement, english, header, automation, attempts[-1:]) + SKETCH_USER_SUFFIX
            try:
                resp = self.ctx.ask(LLMRequest(system=PROVE_SYSTEM, prompt=prompt, model=self.cfg.prover, stage="prove-sketch", effort=self.cfg.effort))
            except (BudgetExceeded, LLMError):
                break
            code = extract_code_block(resp.text) or ""
            ok, err = self.try_block(name, code, allow_sorry=True)
            if not ok:
                attempts.append((code, truncate(err, 3000)))
                continue
            chunks = split_decls(code)
            helpers = [c for c in chunks if _is_sorry_theorem(c)]
            main = [c for c in chunks if re.search(re.escape(header) + r"\s*:=", c)]
            if not main or "sorry" in main[0]:
                attempts.append((code, "The main theorem itself must not use sorry; only helper lemmas may."))
                continue
            proven: list[str] = [c for c in chunks if c not in helpers and c not in main]
            failed = None
            for h in helpers:
                done = self._prove_helper(name, h, proven)
                if done is None:
                    failed = h
                    break
                proven.append(done)
            if failed is None:
                full = "\n\n".join(proven + main)
                ok, err = self.try_block(name, full)
                if ok:
                    return ProofResult(name=name, status="proved", method=f"sketch ({len(helpers)} lemmas)", attempts=i + 1, proof=full)
                attempts.append((full, truncate(err, 3000)))
            else:
                attempts.append((code, f"Larch could not prove this helper lemma, so the sketch is not usable:\n{failed.strip()[:600]}"))
        # Fall back to direct proving with what we learned.
        res = self.llm_loop(name, automation, budget_attempts=max(1, self.cfg.proof_attempts - 2))
        if res.status == "proved":
            res.method = "sketch→" + res.method
        return res

    def _prove_helper(self, name: str, helper: str, proven: list[str]) -> str | None:
        stmt = re.sub(r":=\s*by\s+sorry\s*$", "", helper.strip(), flags=re.S).rstrip()
        m = re.search(r"theorem\s+([A-Za-z_][A-Za-z0-9_'.]*)", stmt)
        if not m:
            return None
        hname = m.group(1)
        # Automation first.
        for tac in ("grind", "simp_all <;> omega", "omega", "simp [*] at *", "decide"):
            candidate = f"{stmt} := by\n  {tac}"
            if self._check_with_context(name, proven, candidate):
                return candidate
        context = ""
        if proven:
            context = "Already proved lemmas you may use:\n```lean\n" + "\n\n".join(proven) + "\n```\n"
        attempts_txt = ""
        for _ in range(max(2, self.cfg.proof_attempts - 1)):
            prompt = LEMMA_USER.format(model_text=self.model_text, context=context, lemma=stmt + " := by\n  sorry", attempts=attempts_txt)
            try:
                resp = self.ctx.ask(LLMRequest(system=PROVE_SYSTEM, prompt=prompt, model=self.cfg.prover, stage="prove-lemma", effort=self.cfg.effort))
            except (BudgetExceeded, LLMError):
                return None
            code = extract_code_block(resp.text) or ""
            if not re.search(r"theorem\s+" + re.escape(hname) + r"\b", code):
                attempts_txt = f"\n## Previous attempt was rejected\nIt did not contain `theorem {hname}`."
                continue
            ok, err = self._check_with_context(name, proven, code, return_err=True)
            if ok:
                return code
            attempts_txt = f"\n## Previous attempt\n```lean\n{code}\n```\nLean reported:\n```\n{truncate(err, 2500)}\n```\nFix it."
        return None

    def _check_with_context(self, name: str, proven: list[str], candidate: str, return_err: bool = False):
        body = "\n\n".join(proven + [candidate])
        issues = lint_lean(body)
        if issues:
            return (False, "\n".join(map(str, issues))) if return_err else False
        res = self.ctx.ws.check_text(proof_module(name, body), stem=f"Lemma_{name}", timeout=90.0)
        ok = res.ok and not any("sorry" in m.text for m in res.messages if m.severity == "warning")
        if return_err:
            return ok, res.error_text(line_offset=PROOF_LINE_OFFSET)
        return ok

    # -- driver ---------------------------------------------------------------------------------
    def prove(self, name: str) -> ProofResult:
        t0 = time.monotonic()
        strategy = self.cfg.proof_strategy
        automation = None
        res: ProofResult | None = None
        if strategy.startswith("portfolio"):
            res, automation = self.portfolio(name)
        if res is None:
            if strategy.endswith("sketch"):
                res = self.sketch(name, automation)
            elif strategy == "portfolio":
                res = ProofResult(name=name, status="unproved", method="auto", error=automation or "")
            else:
                res = self.llm_loop(name, automation)
        res.elapsed = time.monotonic() - t0
        return res


def split_decls(code: str) -> list[str]:
    """Split a Lean snippet into top-level declarations (with preceding comments)."""
    starts = re.compile(r"^(?:@\[[^\n]*\]\s*)?(?:private\s+|protected\s+|noncomputable\s+)?(?:theorem|lemma|def|abbrev|example|instance)\b")
    chunks: list[list[str]] = []
    cur: list[str] = []
    for line in code.splitlines():
        if starts.match(line) and cur and any(starts.match(x) for x in cur):
            chunks.append(cur)
            cur = []
        cur.append(line)
    if cur:
        chunks.append(cur)
    return ["\n".join(c).strip() for c in chunks if "\n".join(c).strip()]


def _is_sorry_theorem(chunk: str) -> bool:
    return bool(re.search(r"^(?:@\[[^\n]*\]\s*)?(?:private\s+)?theorem\b", chunk, re.M)) and bool(
        re.search(r":=\s*by\s+sorry\s*$", chunk.strip(), re.S)
    )


def prove_all(ctx: RunContext, spec: FormalSpec, on_done=None) -> dict[str, ProofResult]:
    prover = Prover(ctx, spec)
    names = spec.spec_names()
    out: dict[str, ProofResult] = {}
    with ThreadPoolExecutor(max_workers=max(1, ctx.cfg.parallel)) as ex:
        futs = {ex.submit(prover.prove, n): n for n in names}
        for fut in as_completed(futs):
            n = futs[fut]
            try:
                out[n] = fut.result()
            except Exception as e:  # noqa: BLE001 - a crash in one proof must not sink the run
                out[n] = ProofResult(name=n, status="unproved", error=f"internal error: {e}")
            if on_done:
                on_done(out[n])
    return out


def finalize_proofs(ctx: RunContext, spec: FormalSpec, results: dict[str, ProofResult]) -> dict[str, ProofResult]:
    """Authoritative acceptance: compile all tentatively-proved blocks into one module
    and run the out-of-process checker. Anything not accepted is downgraded."""
    ok = {n: r for n, r in results.items() if r.status == "proved"}
    if not ok:
        return results
    blocks = []
    for n, r in ok.items():
        blocks.append(proof_module(n, r.proof).replace("import LarchModel\n", "").replace("set_option linter.unusedVariables false\n", ""))
    text = "import LarchModel\nset_option linter.unusedVariables false\n\n" + "\n\n".join(blocks)
    comp = ctx.ws.compile_module("LarchProofs", text, timeout=600)
    modules: dict[str, str] = {}
    if comp.ok:
        modules = {n: "LarchProofs" for n in ok}
    else:
        # Fall back to one module per proof (isolates any interaction).
        for n, r in ok.items():
            mod = f"LarchProof_{n}"
            c = ctx.ws.compile_module(mod, proof_module(n, r.proof), timeout=300)
            if c.ok:
                modules[n] = mod
            else:
                r.status = "unproved"
                r.error = "failed to compile during final check: " + c.error_text(PROOF_LINE_OFFSET)[:800]
    by_module: dict[str, list[str]] = {}
    for n, mod in modules.items():
        by_module.setdefault(mod, []).append(n)
    for mod, names in by_module.items():
        pairs = [(theorem_name(n), spec_constant(n)) for n in names]
        checks = ctx.checker.check(mod, pairs, lean_path=ctx.ws.lean_path, cwd=ctx.ws.root)
        for n, chk in zip(names, checks):
            r = results[n]
            r.axioms = chk.axioms
            if not chk.accepted:
                r.status = "unproved"
                r.error = f"rejected by the proof checker: {chk.reason()}"
    return results
